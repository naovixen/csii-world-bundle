"""cs2beamng A3 gate — shapes, instance placement, lights and groundcover.

Run: `python tools/test_a3_objects.py` (~20 s on `docs/example-bundle`; the real-bundle checks
are skipped when the ModsData export is absent).

Checks, in the order the build runs them:

* **the frame** — `stages.instances._transform` reproduces `csiibundle.frames`' own
  `P . T . R . S . P^-1` to machine precision over random transforms, which is the one identity
  in this phase that is silently wrong if it is wrong;
* **the placement rule** — `beamng.placement` sends decals nowhere, buildings and non-uniformly
  scaled props to `TSStatic`, everything else to the forest, and `keep` agrees with itself
  across the two passes;
* **the shapes stage** — writes a `.dae` per demanded state, the detail ladder strictly
  decreases and sits above `nulldetail`, the null threshold is the game's answer times the
  configured scale, and a collider is present for the kinds that collide;
* **the instances stage** — a `.forest4.json` line has vanilla's exact key set, the item data
  names every placed type, a `TSStatic` carries all three `instanceColor*` channels;
* **the lights stage** — a spot's `innerAngle` is a percentage of the outer, bone-space entries
  are skipped, the cap holds;
* **`groundcover`** — off by default, and writes nothing without a grassy layer.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import xml.etree.ElementTree as ET

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
NS = "{http://www.collada.org/2005/11/COLLADASchema}"
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.join(REPO, "apps"))

FAILS = []
CHECKS = 0


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def real_bundle():
    from csiibundle.bake.locate import default_bundle_dir
    path = default_bundle_dir()
    return path if path and os.path.isfile(os.path.join(path, "instances.jsonl")) else None


# -- the frame ------------------------------------------------------------------------------------

def test_frame():
    print("the instance transform is the contract's own composition")
    import random

    from csiibundle import frames
    from cs2beamng.stages import instances as stage

    random.seed(20260818)
    worst = 0.0
    for _ in range(300):
        quat = [random.uniform(-1, 1) for _ in range(4)]
        norm = sum(v * v for v in quat) ** 0.5
        record = {"pos": [random.uniform(-4000, 4000) for _ in range(3)],
                  "rot": [v / norm for v in quat],
                  "scale": [random.uniform(0.3, 3.0) for _ in range(3)]}
        # the .dae bakes `P . diag(-1,1,1)` into its vertices, so that basis divides out
        basis = frames.P_ZUP @ frames.MIRROR_X
        reference = frames.zup_instance_matrix(record, mesh_basis=basis)
        position, rotation, scale = stage._transform(record)
        mine = numpy.eye(4)
        mine[:3, :3] = numpy.array(rotation) @ numpy.diag(scale)
        mine[:3, 3] = position
        worst = max(worst, float(numpy.abs(mine - reference).max()))
    check(worst < 1e-9, f"P.T.R.S.P^-1 reproduced to {worst:.2e}")

    # a rotation must stay a rotation: no second mirror anywhere
    _p, rot, _s = stage._transform({"pos": [0, 0, 0], "rot": [0.0, 0.3826834, 0.0, 0.9238795]})
    det = float(numpy.linalg.det(numpy.array(rot)))
    check(abs(det - 1.0) < 1e-9, f"the rotation has determinant +1 ({det:.6f})")

    # a tree's growthScale is uniform and multiplies the linear part only
    _p, _r, scaled = stage._transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(all(abs(v - 0.5) < 1e-9 for v in scaled), "growthScale reaches the scale, not the position")
    pos, _r, _s = stage._transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(pos == [1.0, 3.0, 2.0], "the position is the Y/Z swap and nothing else")


# -- the placement rule ---------------------------------------------------------------------------

def test_placement():
    print("forest vs TSStatic")
    from cs2beamng.beamng import placement

    settings = {"trees": True, "props": True, "buildings": True, "foliage_density": 100, "prop_density": 100}
    decal = {"id": 1, "assetId": "a", "kind": "decal", "pos": [0, 0, 0]}
    tree = {"id": 2, "assetId": "a", "kind": "tree", "pos": [0, 0, 0]}
    building = {"id": 3, "assetId": "a", "kind": "building", "pos": [0, 0, 0]}
    scaled = {"id": 4, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [1.0, 2.0, 1.0]}
    uniform = {"id": 5, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [2.0, 2.0, 2.0]}
    site = {"id": 6, "assetId": "a", "kind": "building", "pos": [0, 0, 0], "attrs": {"state": "construction"}}

    check(not placement.keep(decal, settings), "a decal is placed by the markings bake, not here")
    check(not placement.keep(site, settings), "a building under construction is not drawn (should_draw)")
    check(placement.wants_forest(tree), "a tree is a forest item")
    check(not placement.wants_forest(building), "a building is a TSStatic")
    check(not placement.wants_forest(scaled), "a non-uniformly scaled prop is a TSStatic")
    check(placement.wants_forest(uniform), "a uniformly scaled prop stays a forest item")
    check(abs(placement.uniform_scale(uniform) - 2.0) < 1e-9, "the forest scalar is the record's scale")
    check(not placement.wants_forest(tree, forest_allowed=False), "the forest can be switched off wholesale")

    settings_off = dict(settings, trees=False)
    check(not placement.keep(tree, settings_off), "the trees toggle is honoured")
    region = {"centerX": 500.0, "centerZ": 500.0, "radius": 100.0}
    check(not placement.keep(tree, settings, region), "a record outside the region is dropped")
    inside = dict(tree, pos=[520.0, 0.0, 480.0])
    check(placement.keep(inside, settings, region), "a record inside the region is kept")

    print("density thinning is monotone and stable")
    records = [{"id": i, "assetId": "a", "kind": "tree", "pos": [0, 0, 0]} for i in range(4000)]
    at_full = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=100))}
    at_half = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=50))}
    at_tenth = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=10))}
    check(at_tenth <= at_half <= at_full, "a lower density is a subset of a higher one")
    check(400 < len(at_half) - len(at_tenth), "the sliders actually differ")


# -- the shapes stage -----------------------------------------------------------------------------

def _stage_ctx(bundle, level_dir, settings=None, region=None, shared=None):
    from cs2beamng.stages import BuildContext
    base = {"level_name": "t", "level_title": "t", "trees": True, "props": True, "buildings": True,
            "foliage_density": 100, "prop_density": 100, "generate_lods": False, "imposters": True,
            "lod_detail_scale": 6.0, "collision_preset": "simplified",
            "collision_buildings": True, "collision_props": True, "collision_trees": True,
            "forest_shadows": True, "tree_wind": False, "lights": True, "lights_max": 5,
            "groundcover": False}
    base.update(settings or {})
    return BuildContext(settings=base, bundle=bundle, level_dir=level_dir,
                        derived_dir=os.path.join(bundle, "derived"), census={}, region=region,
                        report={}, jobs=1, shared=shared if shared is not None else {})


def _detail_nodes(path):
    root = ET.parse(path).getroot()
    names = [n.get("id") for n in root.iter(NS + "node")]
    details, null_px, billboard = [], None, None
    for name in names:
        if name.startswith("nulldetail"):
            null_px = int(name[len("nulldetail"):])
        elif name.startswith("bb_autobillboard"):
            billboard = int(name[len("bb_autobillboard"):])
        elif "_a" in name and name.rsplit("_a", 1)[1].isdigit():
            details.append(int(name.rsplit("_a", 1)[1]))
    return details, null_px, billboard, names


def test_shapes_and_instances():
    print("the shapes and instances stages, end to end on the example bundle")
    from csiibundle.bake.progress import Progress
    from cs2beamng.stages import instances as instances_stage, shapes as shapes_stage

    level_dir = tempfile.mkdtemp(prefix="a3-level-")
    try:
        shared = {}
        ctx = _stage_ctx(EXAMPLE, level_dir, shared=shared)
        report = shapes_stage.run(ctx, Progress())
        check(report["shapes"] > 0, f"{report['shapes']} shapes written")
        check(report["failed"] == 0, f"no shape failed ({report['errors'][:1]})")
        check("drawDistance" in report and report["drawDistance"], "the report states the draw distances")
        for kind, row in report["drawDistance"].items():
            check(row["gameCullMedian"] > row["ourCullMedian"],
                  f"{kind}: CS2 draws to {row['gameCullMedian']:,.0f} m, this level to "
                  f"{row['ourCullMedian']:,.0f} m")
        check(set(report["approximations"]) >= {"lodDetailScale", "lodLadderSwitchPoints"},
              "every approximation is declared in the report")

        shape_dir = os.path.join(level_dir, "art", "shapes", "cs2")
        files = sorted(f for f in os.listdir(shape_dir) if f.endswith(".dae"))
        check(len(files) == report["shapes"], f"{len(files)} .dae on disk")
        details, null_px, billboard, names = _detail_nodes(os.path.join(shape_dir, files[0]))
        distinct = sorted(set(details), reverse=True)
        check(len(distinct) == len(set(details)) and distinct == sorted(distinct, reverse=True),
              f"the distinct detail sizes strictly decrease {distinct} (a split level repeats one)")
        check(null_px is not None and all(px > null_px for px in details),
              f"every detail sits above nulldetail{null_px}")
        # A5 gated the imposter on `imposter_min_metres` (three quarters of the view distance
        # when 0), so most shapes correctly have NO billboard now — 3.6 % of the reference city
        # against 100 % before. The invariant is the ordering WHERE there is one, plus the gate
        # itself; asserting every shape has one is what this checked before the gate existed.
        check(billboard is None or billboard > null_px,
              f"any imposter ({billboard} px) takes over above the null threshold ({null_px} px)")
        imposters = report["imposters"]
        check(imposters["shapes"] <= imposters["ofShapes"],
              f"{imposters['shapes']} of {imposters['ofShapes']} shapes earned an imposter "
              f"(past {imposters['minMetres']:,.0f} m)")
        check(any(n.startswith("Colmesh_") for n in names), "a collider is present")
        check("base00" in names and "start01" in names, "the vanilla node hierarchy")

        row = next(iter(shared["shapes"]["results"].values()))
        # `nullPx` is the game's own px times `lod_detail_scale`, divided by the imposter's reach
        # ONLY when this shape earned an imposter — the billboard is what carries it the rest of
        # the way, so a shape without one stops at the mesh threshold itself
        scaled = row["gamePx"] * 6.0
        expected = max(1, round(scaled / shapes_stage.IMPOSTER_REACH if row.get("billboardPx")
                                else scaled))
        check(abs(row["nullPx"] - expected) <= 1,
              f"the null threshold is the game's {row['gamePx']:.2f} px x 6"
              f"{' / imposter reach' if row.get('billboardPx') else ' (no imposter)'}")

        # -- placement
        report2 = instances_stage.run(ctx, Progress())
        check(report2["placed"] > 0, f"{report2['placed']} objects placed")
        check(report2["placed"] == report2["forestItems"] + report2["tsStatics"], "every object is one or the other")
        forest_dir = os.path.join(level_dir, "forest")
        forest_files = sorted(f for f in os.listdir(forest_dir)) if os.path.isdir(forest_dir) else []
        if report2["forestItems"]:
            check(len(forest_files) == report2["forestTypes"], f"{len(forest_files)} forest type files")
            with open(os.path.join(forest_dir, forest_files[0]), encoding="utf-8") as handle:
                item = json.loads(handle.readline())
            check(set(item) == {"ctxid", "pos", "rotationMatrix", "scale", "type"},
                  f"a forest item carries vanilla's exact key set {sorted(item)}")
            check(len(item["rotationMatrix"]) == 9, "a 3x3 rotation matrix")
            check(isinstance(item["scale"], float), "one scalar scale, as the format allows")
            data = json.load(open(os.path.join(level_dir, "art", "forest", "managedItemData.json"), encoding="utf-8"))
            check(len(data) == report2["forestTypes"], "every placed type has a TSForestItemData")
            entry = next(iter(data.values()))
            check(entry["class"] == "TSForestItemData" and entry["shapeFile"].startswith("/levels/"),
                  "the item data names its shape by game path")
            check(item["type"] in data, "the item's type resolves in the item data")

        objects = _level_objects(level_dir)
        statics = [o for o in objects if o.get("class") == "TSStatic"]
        check(len(statics) == report2["tsStatics"], f"{len(statics)} TSStatics in the level tree")
        if statics:
            one = statics[0]
            check(all(k in one for k in ("instanceColor", "instanceColor1", "instanceColor2")),
                  "a TSStatic carries all three colorSet channels")
            check(len(one["rotationMatrix"]) == 9 and len(one["scale"]) == 3, "rotation 3x3 and a 3-vector scale")
        check(any(o.get("class") == "Forest" for o in objects) or not report2["forestItems"],
              "the level owns a Forest object when it has forest items")
        check(report2["approximations"]["forestPaintVariants"]["count"] == report2["forestItems"],
              "the paint-variant approximation counts every forest item")

    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


def _forest_bundle(root):
    """The example bundle plus 60 trees and 40 props on its one mesh — the example city has
    three records and none of them reaches the forest path, which is most of this stage."""
    bundle = os.path.join(root, "bundle")
    shutil.copytree(EXAMPLE, bundle)
    records = []
    for i in range(60):
        records.append({"id": 5000 + i, "assetId": "carto_example_cube", "kind": "tree",
                        "pos": [100.0 + i, 12.5, -340.0], "rot": [0.0, 0.0, 0.0, 1.0],
                        "attrs": {"treeState": "Adult", "growth": 1.0,
                                  "growthScale": 0.5 + 0.01 * (i % 40),
                                  "colorSet": [[0.2 + 0.01 * (i % 30), 0.6, 0.3, 1.0],
                                               [1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0]]}})
    for i in range(40):
        scale = [1.0, 1.0, 1.0] if i % 4 else [1.0, 2.0, 1.0]
        records.append({"id": 6000 + i, "assetId": "carto_example_cube", "kind": "prop",
                        "pos": [100.0 + i, 12.0, -300.0], "rot": [0.0, 0.0, 0.0, 1.0],
                        "scale": scale})
    with open(os.path.join(bundle, "instances.jsonl"), "a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return bundle


def test_forest_path():
    print("the forest path, on the example bundle plus 100 synthetic placements")
    from csiibundle.bake.progress import Progress
    from cs2beamng.stages import instances as instances_stage, shapes as shapes_stage

    root = tempfile.mkdtemp(prefix="a3-forest-")
    try:
        bundle = _forest_bundle(root)
        level_dir = os.path.join(root, "level")
        shared = {}
        ctx = _stage_ctx(bundle, level_dir, shared=shared)
        shapes_stage.run(ctx, Progress())
        report = instances_stage.run(ctx, Progress())
        check(report["forestItems"] == 90, f"{report['forestItems']} forest items (60 trees + 30 uniform props)")
        check(report["tsStatics"] == 12, f"{report['tsStatics']} TSStatics (2 buildings + 10 scaled props)")
        forest_dir = os.path.join(level_dir, "forest")
        files = sorted(os.listdir(forest_dir))
        check(len(files) == report["forestTypes"], f"{len(files)} forest type files")
        with open(os.path.join(forest_dir, files[0]), encoding="utf-8") as handle:
            items = [json.loads(line) for line in handle if line.strip()]
        check(set(items[0]) == {"ctxid", "pos", "rotationMatrix", "scale", "type"},
              f"a forest item carries vanilla's exact key set {sorted(items[0])}")
        check(len(items[0]["rotationMatrix"]) == 9, "a 3x3 rotation matrix")
        check(isinstance(items[0]["scale"], float), "one scalar scale, as the format allows")
        check(len({round(i["scale"], 3) for i in items}) > 5,
              "growthScale reaches the forest item's scale")
        data = json.load(open(os.path.join(level_dir, "art", "forest", "managedItemData.json"), encoding="utf-8"))
        check(len(data) == report["forestTypes"], "every placed type has a TSForestItemData")
        entry = data[items[0]["type"]]
        check(entry["class"] == "TSForestItemData" and entry["shapeFile"].startswith("/levels/"),
              "the item data names its shape by game path")
        check(entry.get("radius", 0) > 0, "the item data carries the shape's radius")
        objects = _level_objects(level_dir)
        check(any(o.get("class") == "Forest" for o in objects), "the level owns a Forest object")

        # a rebuild replaces the forest rather than appending to it
        shared2 = {}
        ctx2 = _stage_ctx(bundle, level_dir, shared=shared2)
        shapes_stage.run(ctx2, Progress())
        instances_stage.run(ctx2, Progress())
        with open(os.path.join(forest_dir, files[0]), encoding="utf-8") as handle:
            again = sum(1 for line in handle if line.strip())
        check(again == len(items), f"a rebuild rewrites the forest ({again} items, not {2 * len(items)})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _level_objects(level_dir):
    from cs2beamng.beamng.level import read_objects
    return [obj for _f, _n, obj in read_objects(level_dir) if obj]


def test_region_and_toggles():
    print("the two passes agree, and the toggles reach both")
    from csiibundle.bake.progress import Progress
    from cs2beamng.stages import instances as instances_stage, shapes as shapes_stage

    level_dir = tempfile.mkdtemp(prefix="a3-region-")
    try:
        shared = {}
        ctx = _stage_ctx(EXAMPLE, level_dir, settings={"trees": False}, shared=shared)
        first = shapes_stage.run(ctx, Progress())
        report = instances_stage.run(ctx, Progress())
        placed_kinds = {row[0] for row in shared["shapes"]["demand"]}
        check(report["noShape"] == first["scan"]["noMesh"],
              f"the two passes agree on what has no mesh ({report['noShape']} each)")
        check(report["kept"] - report["noShape"] == report["placed"],
              "every kept record with a mesh was placed exactly once")
        check(placed_kinds, "the demand scan found something")
        kinds = {entry["kind"] for entry in shared["shapes"]["demand"].values()}
        check("tree" not in kinds, "trees off means no tree shape is written")
    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


# -- lights ---------------------------------------------------------------------------------------

def test_lights():
    print("lights")
    from csiibundle import lights as bundle_lights
    from csiibundle.bake.progress import Progress
    from cs2beamng.stages import lights as lights_stage

    cone = bundle_lights.cone_angles({"type": "Spot", "spotAngle": 80.0, "innerSpotPercentage": 25.0})
    check(cone == (80.0, 20.0), "innerSpotPercentage is a percentage of the outer angle, not degrees")

    level_dir = tempfile.mkdtemp(prefix="a3-lights-")
    try:
        ctx = _stage_ctx(EXAMPLE, level_dir)
        report = lights_stage.run(ctx, Progress())
        check("lights" in report, "the stage runs on the example bundle")
        check(report.get("lights", 0) <= ctx.settings["lights_max"], "the cap holds")
        if report.get("lights"):
            objects = [o for o in _level_objects(level_dir) if o.get("class") in ("PointLight", "SpotLight")]
            check(len(objects) == report["lights"], "every reported light is in the level tree")
            check(all(o["castShadows"] is False for o in objects), "no placed light casts a shadow")
        check("emissiveColourSlots" in report.get("approximations", {}),
              "the unported colour slots are counted")
    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


def test_groundcover_default():
    print("groundcover")
    from cs2beamng import settings as settings_mod
    from cs2beamng.stages import groundcover as gc

    check(settings_mod.defaults()["groundcover"] is False,
          "groundcover is OFF by default - CS2 has no groundcover system to be faithful to")
    check(not gc.enabled(settings_mod.defaults()), "the stage does not run by default")
    check(all(preset.get("groundcover") is False for preset in settings_mod.PRESETS.values()),
          "no quality preset turns it on")
    atlas = gc._atlas((0.3, 0.5, 0.2))
    check(atlas.shape == (gc.ATLAS_PX, gc.ATLAS_PX, 4), "the synthesised atlas is RGBA")
    check(bool((atlas[:, :, 3] > 0).any()) and bool((atlas[:, :, 3] == 0).any()),
          "the blades have alpha and so does the gap between them")


# -- the real bundle ------------------------------------------------------------------------------

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
            world = numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
            lo = numpy.minimum(lo, world.min(axis=0))
            hi = numpy.maximum(hi, world.max(axis=0))
            area += lod.surface_area(world, node["tris"])
        size = hi - lo
        radius = 0.5 * float(numpy.linalg.norm(size))
        biases.append(lod.lod_bias(size, area))
        pixels.append(lod.game_detail_px(radius, size, area))
    check(len(pixels) > 50, f"{len(pixels)} real meshes measured")
    check(all(b >= -1e-9 for b in biases), "the surface-area bias never pulls the distance OUT")
    check(all(0.5 < px < 40 for px in pixels),
          f"every threshold is a plausible pixel size ({min(pixels):.2f}..{max(pixels):.2f})")
    median = sorted(pixels)[len(pixels) // 2]
    check(1.0 < median < 4.0, f"the median mesh is drawn to about {median:.2f} px - CS2's own 1.43 px scale")


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
    for asset, entry in list(multi.items())[:400]:
        for index, centre in enumerate(entry["variants"]):
            attrs = {"colorSet": [list(c) + [1.0] for c in centre]}
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
    from csiibundle import instances as bundle_instances
    from cs2beamng.stages import shapes as shapes_stage

    oak = {"kind": "tree", "attrs": {"growth": 0.796, "treeState": "Adult"}}
    sapling = {"kind": "tree", "attrs": {"growth": 0.0}}          # falsy, and still a tree
    bush = {"kind": "tree", "attrs": {}}
    grass = {"kind": "tree"}                                       # no attrs at all
    prop = {"kind": "prop", "attrs": {}}

    check(bundle_instances.is_tree(oak), "a grown tree is a tree")
    check(bundle_instances.is_tree(sapling),
          "growth 0.0 is a SAPLING, not a shrub (a truth test would misread it)")
    check(not bundle_instances.is_tree(bush), "a bush carries no growth")
    check(not bundle_instances.is_tree(grass), "a record with no attrs is not a tree")
    check(bundle_instances.vegetation_kind(oak) == "tree", "vegetation_kind: tree")
    check(bundle_instances.vegetation_kind(bush) == "plant", "vegetation_kind: plant")
    check(bundle_instances.vegetation_kind(prop) is None, "vegetation_kind: a prop is not planting")

    # the collider follows it: a plant gets nothing, a tree still gets its trunk
    options = {"collide": {"tree": True}, "collision_preset": "simplified",
               "colmesh_max_tris": 1000}
    mesh = types.SimpleNamespace(
        verts=numpy.array([[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [0., 0., 4.]]),
        tris=numpy.array([[0, 1, 2], [0, 1, 3]]), runs=[("bark", 2)])
    plant_cols, plant_kind = shapes_stage._collider("p", mesh, mesh, "tree", options, is_tree=False)
    tree_cols, tree_kind = shapes_stage._collider("t", mesh, mesh, "tree", options, is_tree=True)
    check(plant_cols == () and plant_kind == "plant", "a plant gets no collider at all")
    check(len(tree_cols) == 1 and tree_kind == "trunk", "a tree still gets its trunk cylinder")

    # and on the real city, the split is per PREFAB — no asset is ever classified both ways,
    # which is what lets one flag per shape be exact rather than a vote
    bundle = real_bundle()
    if bundle:
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
        check(trees and trees < len(by_asset),
              f"{trees} tree prefabs and {len(by_asset) - trees} plant prefabs")


def main():
    test_frame()
    test_placement()
    test_shapes_and_instances()
    test_forest_path()
    test_region_and_toggles()
    test_lights()
    test_groundcover_default()
    test_real_bundle_lod()
    test_variant_index_matches_the_bake()
    test_tree_vs_plant()
    print()
    if FAILS:
        print(f"{len(FAILS)} of {CHECKS} FAILED:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"{CHECKS}/{CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
