"""The latent guards of the 2026-08-31 validation pass, each pinned where the defect lived.

* B4 — a building takes a generated level only where THAT level's bake backend is MeshLab
  (`stages/shapes._lod_backend` per level, `shapelods.lod_levels`).
* B7 — `stages/poi` reads the navgraph's DecalRoads from the build's own object tree, never
  from disk, and says which source it used.
* B8 — a building the game does not draw gets no skirt (`bake/skirts.admit`).
* B9 — a segment record with no mesh file is counted (`stages/networks._load_parts`).
* B10 — a corrupt `derived/materials.json` raises instead of silently changing every tile's
  materials (`bake/tiles.material_specs`).
* B13 — an archive member cannot escape the mods folder (`modinstall.member_dest`).

Run: `python tools/test_consumer_guards.py`  (system Python + numpy; no bundle needed).
"""

import json
import os
import sys
import tempfile

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


def test_meshlab_gate_per_level():
    from cs2beamng.stages import shapes

    index = {
        "a": {"backends": ["cluster", "meshlab"]},
        "b": {"backends": ["meshlab", "meshlab"]},
        "c": {"backends": ["cluster", "cluster"]},
        "d": {"backend": "meshlab"},  # an older bake: one string for every level
    }
    check(shapes._lod_backend(index, "a", "") == (2,), "only the level MeshLab shaped is admitted")
    check(shapes._lod_backend(index, "b", "") == (1, 2), "both when it shaped both")
    check(shapes._lod_backend(index, "c", "") == (), "none when it shaped neither")
    check(shapes._lod_backend(index, "d", "") == (1, 2), "an older bake's one string covers both")
    check(shapes._lod_backend(index, "missing", "") == (), "an asset the bake never saw: none")


def test_poi_roads_come_from_the_tree():
    from cs2beamng.stages import BuildContext, poi

    with tempfile.TemporaryDirectory() as tmp:
        shared = {}
        ctx = BuildContext(
            settings={"level_name": "lvl", "level_title": "L", "pois": True},
            bundle=os.path.join(tmp, "nobundle"),
            level_dir=os.path.join(tmp, "lvl"),
            derived_dir=os.path.join(tmp, "derived"),
            census={},
            region=None,
            report={},
            jobs=1,
            shared=shared,
        )
        from cs2beamng.stages import level_of

        level = level_of(ctx)
        level.set_stage("navgraph")
        level.add(
            ["AIWaypointsGroup"],
            {
                "name": "seg_1",
                "class": "DecalRoad",
                "nodes": [[0, 0, 10, 4], [50, 0, 10, 4], [100, 0, 10, 4]],
                "cs2Street": "Main Street",
            },
        )
        level.set_stage("poi")
        network = poi._road_network(ctx)  # nothing on disk: the tree is the only source
        check(
            len(network.points) == 2,
            f"the roads come from the build's tree ({len(network.points)} points)",
        )
        check(getattr(network, "source", None) == "navgraph", "and the source is recorded")
        check(network.labels[0] == "Main Street", "named by the CS2 street the navgraph rode along")


def test_skirt_admission():
    from csiibundle.bake import skirts

    ground = {
        "kind": "building",
        "id": 1,
        "attrs": {"elevationFlags": ["OnGround"], "state": "normal"},
    }
    site = {
        "kind": "building",
        "id": 2,
        "attrs": {"elevationFlags": ["OnGround"], "state": "construction"},
    }
    overridden = {
        "kind": "building",
        "id": 3,
        "attrs": {"elevationFlags": ["OnGround"], "overridden": True},
    }
    check(skirts.admit(ground, {}), "a drawn building on the ground gets a skirt")
    check(
        not skirts.admit(site, {}),
        "a building site (construction) gets none - the game draws no building there",
    )
    check(not skirts.admit(overridden, {}), "an overridden building gets none")


def test_missing_network_mesh_is_counted():
    from cs2beamng.stages import networks

    with tempfile.TemporaryDirectory() as tmp:
        parts = networks._load_parts(tmp, [{"assetId": "seg_nope", "pos": [0, 0, 0]}])
        check(
            parts["missing"] == 1 and not parts["pieces"],
            "a record with no mesh file is counted as missing",
        )


def test_corrupt_materials_table_raises():
    from csiibundle.bake import tiles

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "materials.json"), "w", encoding="utf-8") as handle:
            handle.write('{"materials": {')
        try:
            tiles.material_specs(tmp, os.path.join(tmp, "nobundle"))
            check(False, "a corrupt derived/materials.json raises rather than falling through")
        except RuntimeError:
            check(True, "a corrupt derived/materials.json raises rather than falling through")
        with open(os.path.join(tmp, "materials.json"), "w", encoding="utf-8") as handle:
            json.dump({"materials": {"m": {"baseColor": "a.png"}}}, handle)
        specs = tiles.material_specs(tmp, os.path.join(tmp, "nobundle"))
        check(specs.get("m", {}).get("baseColor") == "a.png", "a good one is read")


def test_mod_archive_member_stays_inside():
    from cs2beamng import modinstall

    mods = os.path.join(tempfile.gettempdir(), "cs2mods_guard")
    inside = modinstall.member_dest(mods, "Carto/Carto.dll")
    check(
        inside == os.path.join(os.path.abspath(mods), "Carto", "Carto.dll"),
        "a normal member lands in the mods folder",
        inside,
    )
    try:
        modinstall.member_dest(mods, "Carto/../../evil.dll")
        check(False, "`Carto/../../x` is refused")
    except ValueError:
        check(True, "`Carto/../../x` is refused")


def _cooked_fixture(level_dir):
    """A shapes state with one good row, one null over its coarsest level, one unbound plant."""
    os.makedirs(os.path.join(level_dir, "cs2beamng_state"))
    rows = [
        (
            "good",
            {
                "file": "Good.dae",
                "nullPx": 3,
                "collider": "mesh",
                "materials": ["m1"],
                "lods": {"detailSizes": [50, 10]},
            },
        ),
        (
            "null",
            {
                "file": "NullOver.dae",
                "nullPx": 60,
                "collider": "box",
                "materials": ["m1"],
                "lods": {"detailSizes": [50, 10]},
            },
        ),
        (
            "unbound",
            {
                "file": "Unbound.dae",
                "nullPx": 3,
                "collider": "plant",
                "materials": ["nope"],
                "lods": {"detailSizes": [50]},
            },
        ),
    ]
    pairs = [[{"$tuple": [k, "", None]}, v] for k, v in rows]
    state = {"shared": {"shapes": {"results": {"$pairs": pairs}}}}
    with open(
        os.path.join(level_dir, "cs2beamng_state", "shapes.state.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(state, handle)
    statics = [("Good.dae", "a"), ("Unbound.dae", "b")]
    return [
        (
            "f",
            i,
            {
                "class": "TSStatic",
                "name": n,
                "shapeName": f"/levels/lvl/art/shapes/cs2/{f}",
                "collisionType": "Collision Mesh",
            },
        )
        for i, (f, n) in enumerate(statics)
    ]


def test_cooked_shape_checks_read_the_stage_record():
    """With `keep_dae: false` every object shape's `.dae` is gone and the text checks pass on
    nothing; `levelcheck.shapes.check_cooked_shapes` asks the shapes stage's own record the
    same questions (E2)."""
    from cs2beamng.beamng.levelcheck import shapes as shape_checks
    from cs2beamng.beamng.levelcheck.context import LevelContext

    with tempfile.TemporaryDirectory() as tmp:
        level_dir = os.path.join(tmp, "lvl")
        objects = _cooked_fixture(level_dir)
        ctx = LevelContext(level_dir, os.path.join(tmp, "nobundle"))
        ctx.mapto = {"lvl_m1"}
        ctx.checks["collisionTypeMismatch"] = {}
        ctx.objects = objects
        shape_checks.check_cooked_shapes(ctx)
        check(
            ctx.checks["cookedShapesChecked"] == 3,
            "every shape without a source is checked from the record",
        )
        check(
            ctx.checks["cookedNullAboveCoarsest"] == 1, "a null over the coarsest level is caught"
        )
        check(ctx.checks["cookedUnboundMaterials"] == 1, "a material no file maps is caught")
        check(
            ctx.checks["collisionTypeMismatch"]["cookedMismatch"] == 1,
            "a Collision Mesh on a shape with no collider is caught",
        )
        check(
            ctx.checks["cookedShapesChecked"] and not ctx.fatal_problems,
            "none of these is fatal - they are notes",
        )


def test_ground_surface_fallback():
    """A texture-less Terrain-decal surface takes the bundle's ground albedo BY NAME (a modded
    moss/grass/terrain paint has no texture to rip - CS2 draws the terrain through it); an
    unknown name keeps the game's own grey (the Missing Area colour)."""
    import types

    from cs2beamng.stages import materials

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "terrain", "surfaces"))
        for ground in ("grass", "dirt"):
            open(os.path.join(tmp, "terrain", "surfaces", ground + ".png"), "wb").write(b"x")
        ctx = types.SimpleNamespace(bundle=tmp)
        pick = materials._ground_surface_fallback
        check(
            pick(ctx, {"name": "TTP - Moss 1 S Surface", "decal_layers": ["Terrain"]})
            == "terrain/surfaces/grass.png",
            "a moss surface takes the grass albedo",
        )
        check(
            pick(ctx, {"name": "Terrain01", "decal_layers": ["Terrain"]})
            == "terrain/surfaces/dirt.png",
            "a terrain surface takes the dirt albedo",
        )
        check(
            pick(ctx, {"name": "Missing Area", "decal_layers": ["Terrain"]}) is None,
            "an unknown name keeps the grey",
        )
        check(
            pick(ctx, {"name": "Moss thing", "decal_layers": ["Roads"]}) is None,
            "a surface not on the Terrain decal layer is left alone",
        )


def main():
    test_ground_surface_fallback()
    test_cooked_shape_checks_read_the_stage_record()
    test_meshlab_gate_per_level()
    test_poi_roads_come_from_the_tree()
    test_skirt_admission()
    test_missing_network_mesh_is_counted()
    test_corrupt_materials_table_raises()
    test_mod_archive_member_stays_inside()
    print("test_consumer_guards: " + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py)."""
    assert main() == 0
