"""cs2beamng A3 gate, part 3 — the game's classification rules, and the real-city sweeps.

Run: `python tools/test_a3_city.py`. Split from `test_a3_objects.py` (Phase 4); the stage
tests live there and the writer rules in `test_a3_rules.py`. The real-bundle checks are
skipped when the ModsData export is absent.

* **the game's LOD answer** measured over the real bundle's meshes;
* **paint variants** — `tints.variant_index` agrees with the bake's own assignment;
* **tree vs plant** (A6.1) — the archetype rule, and the per-prefab split on the real city;
* **road clearance** (A6.4) — the declared deviation stays off, tree-only, and small;
* **orphans of absent owners** (A6.6) — the §10 discriminator, synthetic and real.
"""

import json
import os
import shutil
import sys
import tempfile
import types

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import check, finish, real_bundle, start  # noqa: E402


def test_real_bundle_lod():
    bundle = real_bundle()
    if not bundle:
        print("real bundle: absent, skipped")
        return
    print("the game's LOD answer over the real bundle's meshes")
    from csiibundle import glb, lod

    mesh_dir = os.path.join(bundle, "meshes")
    files = sorted(f for f in os.listdir(mesh_dir) if f.endswith(".glb"))[:120]
    biases, pixels = [], []
    for name in files:
        nodes = glb.parse_glb(os.path.join(mesh_dir, name))
        if not nodes:
            continue
        lo = numpy.array([numpy.inf] * 3)
        hi = numpy.array([-numpy.inf] * 3)
        area = 0.0
        for node in nodes:
            matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
            world = (
                numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
            )
            lo = numpy.minimum(lo, world.min(axis=0))
            hi = numpy.maximum(hi, world.max(axis=0))
            area += lod.surface_area(world, node["tris"])
        size = hi - lo
        radius = 0.5 * float(numpy.linalg.norm(size))
        biases.append(lod.lod_bias(size, area))
        pixels.append(lod.game_detail_px(radius, size, area))
    check(len(pixels) > 50, f"{len(pixels)} real meshes measured")
    check(all(b >= -1e-9 for b in biases), "the surface-area bias never pulls the distance OUT")
    check(
        all(0.5 < px < 40 for px in pixels),
        f"every threshold is a plausible pixel size ({min(pixels):.2f}..{max(pixels):.2f})",
    )
    median = sorted(pixels)[len(pixels) // 2]
    check(
        1.0 < median < 4.0,
        f"the median mesh is drawn to about {median:.2f} px - CS2's own 1.43 px scale",
    )


def test_variant_index_matches_the_bake():
    bundle = real_bundle()
    path = os.path.join(bundle, "derived", "paint_variants.json") if bundle else None
    if not path or not os.path.isfile(path):
        print("paint variants: absent, skipped")
        return
    print("variant_index agrees with the bake's own assignment")
    from csiibundle import tints

    with open(path, encoding="utf-8") as handle:
        prefabs = json.load(handle)["prefabs"]
    multi = {a: v for a, v in prefabs.items() if len(v["variants"]) > 1}
    check(bool(multi), f"{len(multi)} prefabs have more than one variant")
    # every centre must be the nearest centre to itself
    wrong = 0
    for _asset, entry in list(multi.items())[:400]:
        for index, centre in enumerate(entry["variants"]):
            attrs = {"colorSet": [[*list(c), 1.0] for c in centre]}
            if tints.variant_index(attrs, entry["variants"]) != index:
                wrong += 1
    check(wrong == 0, f"each centre is its own nearest ({wrong} wrong)")
    keys = [tints.variant_key(c) for c in next(iter(multi.values()))["variants"]]
    check(len(set(keys)) == len(keys), "variant keys are distinct")


def test_tree_vs_plant():
    """A6.1 — the `tree` kind is ALL vegetation, and only `is_tree` is CS2's tree.

    The rule is the game's own archetype construction, not a name or a height:
    `TreeObject.GetArchetypeComponents` adds `Game.Objects.Tree`, `PlantObject` does not, and
    the producer writes `attrs.growth` inside exactly that component's lookup.
    """
    print("tree vs plant (A6.1)")
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle import instances as bundle_instances

    oak = {"kind": "tree", "attrs": {"growth": 0.796, "treeState": "Adult"}}
    sapling = {"kind": "tree", "attrs": {"growth": 0.0}}  # falsy, and still a tree
    bush = {"kind": "tree", "attrs": {}}
    grass = {"kind": "tree"}  # no attrs at all
    prop = {"kind": "prop", "attrs": {}}

    check(bundle_instances.is_tree(oak), "a grown tree is a tree")
    check(
        bundle_instances.is_tree(sapling),
        "growth 0.0 is a SAPLING, not a shrub (a truth test would misread it)",
    )
    check(not bundle_instances.is_tree(bush), "a bush carries no growth")
    check(not bundle_instances.is_tree(grass), "a record with no attrs is not a tree")
    check(bundle_instances.vegetation_kind(oak) == "tree", "vegetation_kind: tree")
    check(bundle_instances.vegetation_kind(bush) == "plant", "vegetation_kind: plant")
    check(bundle_instances.vegetation_kind(prop) is None, "vegetation_kind: a prop is not planting")

    # the collider follows it: a plant gets nothing, a tree still gets its trunk
    options = {
        "collide": {"tree": True},
        "collision_preset": "simplified",
        "colmesh_max_tris": 1000,
    }
    mesh = types.SimpleNamespace(
        verts=numpy.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 4.0]]),
        tris=numpy.array([[0, 1, 2], [0, 1, 3]]),
        runs=[("bark", 2)],
    )
    plant_cols, plant_kind = shapes_stage._collider("p", mesh, mesh, "tree", options, is_tree=False)
    tree_cols, tree_kind = shapes_stage._collider("t", mesh, mesh, "tree", options, is_tree=True)
    check(plant_cols == () and plant_kind == "plant", "a plant gets no collider at all")
    check(len(tree_cols) == 1 and tree_kind == "trunk", "a tree still gets its trunk cylinder")


def test_tree_vs_plant_on_the_city():
    """On the real city the split is per PREFAB — no asset is ever classified both ways, which
    is what lets one flag per shape be exact rather than a vote."""
    from csiibundle import instances as bundle_instances

    bundle = real_bundle()
    if not bundle:
        print("tree vs plant on the city: real bundle absent, skipped")
        return
    by_asset = {}
    both = 0
    for record in bundle_instances.read_instances(bundle, kind="tree"):
        if record.get("kind") != "tree":
            continue
        kind = bundle_instances.vegetation_kind(record)
        asset = record.get("assetId")
        seen = by_asset.setdefault(asset, kind)
        if seen != kind:
            both += 1
    trees = sum(1 for k in by_asset.values() if k == "tree")
    check(both == 0, f"no prefab is both a tree and a plant ({both} disagreed)")
    check(
        trees and trees < len(by_asset),
        f"{trees} tree prefabs and {len(by_asset) - trees} plant prefabs",
    )


def test_road_clearance():
    """A6.4 — the declared road-clearance deviation.

    Not a game rule: CS2 clears objects when a road is BUILT, and whatever survives in the save
    is what the city contains. So the gate is that it is OFF when asked to be off, that it only
    ever touches trees, and that the numbers it reports are the numbers it acted on.
    """
    print("road clearance (A6.4)")
    from cs2beamng.beamng import placement
    from csiibundle import roadclear

    # the grid must agree with a brute-force answer — it replaces scipy, which the Blender
    # adapter cannot import
    rng = numpy.random.default_rng(3)
    pts = rng.uniform(-200, 200, size=(4000, 2))
    qs = rng.uniform(-200, 200, size=(1500, 2))
    for radius in (0.75, 1.5, 3.0):
        brute = numpy.array([bool(((pts - q) ** 2).sum(axis=1).min() <= radius**2) for q in qs])
        check(
            bool((roadclear.PointGrid(pts, radius).within(qs, radius) == brute).all()),
            f"PointGrid matches brute force at r={radius}",
        )
    check(
        not roadclear.PointGrid(numpy.zeros((0, 2)), 1.0).within(qs, 1.0).any(),
        "an empty lane layer clips nothing",
    )

    # only trees, and only within the clearance
    grid = roadclear.PointGrid(numpy.array([[0.0, 0.0]]), 2.0)
    tree_near = {"kind": "tree", "attrs": {"growth": 0.5}, "pos": [1.0, 0.0, 0.0], "id": 1}
    tree_far = {"kind": "tree", "attrs": {"growth": 0.5}, "pos": [9.0, 0.0, 0.0], "id": 2}
    bush_near = {"kind": "tree", "attrs": {}, "pos": [1.0, 0.0, 0.0], "id": 3}
    prop_near = {"kind": "prop", "attrs": {}, "pos": [1.0, 0.0, 0.0], "id": 4}
    hit = roadclear.obstructing([tree_near, tree_far, bush_near, prop_near], grid, 2.0)
    check(
        list(hit) == [True, False, False, False],
        "clips the near TREE only - a bush is driven through, a prop is not planting",
    )
    check(not roadclear.obstructing([tree_near], grid, 0.0).any(), "clearance 0 clips nothing")

    # the admission seam carries it, and names it
    settings = {"trees": True, "props": True, "buildings": True}
    check(
        placement.admit(tree_near, settings, None, {1}) == "roadclear",
        "placement.admit names the road-clearance reason",
    )
    check(
        placement.admit(tree_near, settings, None, set()) is None,
        "and admits the same record when nothing was clipped",
    )

    # off means the lane layer is never even read
    ids, stats = placement.road_clipped("/nonexistent-bundle", {"foliage_road_clearance": 0})
    check(
        ids == set() and stats["laneStations"] == 0,
        "clearance 0 short-circuits before touching the bundle",
    )


def test_road_clearance_on_the_city():
    """The deviation's size on the real city — it must stay a small share of the trees."""
    from cs2beamng.beamng import placement

    bundle = real_bundle()
    if not bundle:
        print("road clearance on the city: real bundle absent, skipped")
        return
    ids, stats = placement.road_clipped(bundle, {"foliage_road_clearance": 1.5})
    check(stats["clipped"] == len(ids), f"{stats['clipped']:,} clipped ids, all distinct")
    share = stats["clipped"] / max(stats["trees"], 1)
    check(
        0 < share < 0.05,
        f"{stats['clipped']:,} of {stats['trees']:,} trees = {share:.2%} "
        f"(a deviation, so it must stay small)",
    )


def _declared_gaps(root):
    """A synthetic bundle: one ABSENT prefab (which also carries a mesh warning), two that
    merely have no geometry, and six records whose ownership chains exercise all three
    outcomes."""
    warnings = [
        {
            "kind": "prefab",
            "assetId": "unresolved_7",
            "reason": "managed prefab did not resolve; instances kept with placement only",
        },
        # the same asset ALSO carries a mesh warning - `read_declared_gaps` collapses these
        # to the LAST one and loses the fact that the prefab is absent
        {
            "kind": "mesh",
            "assetId": "unresolved_7",
            "reason": "none of 1 sub-mesh(es) yielded geometry",
        },
        {
            "kind": "mesh",
            "assetId": "ParkingLot02",
            "reason": "none of 1 sub-mesh(es) yielded geometry",
        },
        {
            "kind": "prefab",
            "assetId": "CampfireSite01",
            "reason": "prefab has no SubMesh buffer, so no mesh is produced",
        },
    ]
    with open(os.path.join(root, "warnings.jsonl"), "w", encoding="utf-8") as fh:
        for row in warnings:
            print(json.dumps(row), file=fh)


def test_orphans_of_absent_owners():
    """A6.6 - a record whose owner the producer declared ABSENT is not placed either.

    The discriminator is the trap: `InstanceRipSystem` emits `kind: "prefab"` for BOTH "managed
    prefab did not resolve" (absent) and "prefab has no SubMesh buffer" (exists, no geometry), so
    keying on `kind` alone would delete real park furniture.
    """
    print("orphans of absent owners (A6.6)")
    from cs2beamng.beamng import placement
    from csiibundle import instances as bundle_instances

    root = tempfile.mkdtemp()
    try:
        _declared_gaps(root)
        absent = bundle_instances.absent_prefabs(root)
        check(
            absent == {"unresolved_7"},
            f"only the unresolved prefab is ABSENT (got {sorted(absent)})",
        )

        records = [
            {"id": 1, "kind": "building", "assetId": "unresolved_7", "pos": [0, 0, 0]},
            {"id": 2, "kind": "prop", "assetId": "Lamp", "parent": 1, "pos": [0, 90, 0]},
            {"id": 3, "kind": "prop", "assetId": "Bench", "parent": 2, "pos": [0, 90, 1]},
            {"id": 4, "kind": "building", "assetId": "ParkingLot02", "pos": [9, 0, 0]},
            {"id": 5, "kind": "prop", "assetId": "Lamp", "parent": 4, "pos": [9, 0, 1]},
            {"id": 6, "kind": "prop", "assetId": "Lamp", "pos": [20, 0, 0]},
        ]
        with open(os.path.join(root, "instances.jsonl"), "w", encoding="utf-8") as fh:
            for row in records:
                print(json.dumps(row), file=fh)

        ids, stats = bundle_instances.orphans_of_absent_owners(root)
        check(ids == {2, 3}, f"the absent owner's children go, TRANSITIVELY (got {sorted(ids)})")
        check(5 not in ids, "a car park that merely has no geometry KEEPS its lamp")
        check(6 not in ids, "a free-standing prop is untouched")
        check(stats["owners"] == 1 and stats["orphans"] == 2, f"counted: {stats}")

        settings = {"trees": True, "props": True, "buildings": True}
        rec2 = {"id": 2, "kind": "prop", "attrs": {}}
        check(
            placement.admit(rec2, settings, None, None, ids) == "orphan",
            "placement.admit names the orphan reason",
        )
        check(
            placement.admit(rec2, settings, None, None, set()) is None,
            "and admits it when nothing was orphaned",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_orphans_on_the_city():
    """The real city's orphan share — this only ever removes what cannot stand up."""
    from csiibundle import instances as bundle_instances

    bundle = real_bundle()
    if not bundle:
        print("orphans on the city: real bundle absent, skipped")
        return
    ids, stats = bundle_instances.orphans_of_absent_owners(bundle)
    check(
        stats["absentPrefabs"] > 0 and stats["orphans"] == len(ids),
        "{} orphans over {} absent owners ({} absent prefabs declared)".format(
            format(stats["orphans"], ","), stats["owners"], stats["absentPrefabs"]
        ),
    )
    check(
        stats["orphans"] < stats["withParent"] * 0.05,
        "{} is a small share of the {} records with a parent - this only ever removes "
        "what cannot stand up".format(
            format(stats["orphans"], ","), format(stats["withParent"], ",")
        ),
    )


def main():
    start()
    test_real_bundle_lod()
    test_variant_index_matches_the_bake()
    test_tree_vs_plant()
    test_tree_vs_plant_on_the_city()
    test_road_clearance()
    test_road_clearance_on_the_city()
    test_orphans_of_absent_owners()
    test_orphans_on_the_city()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_city.py`."""
    assert main() in (None, 0)
