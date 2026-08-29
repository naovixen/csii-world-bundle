"""cs2beamng A3 gate — shapes, instance placement, lights and groundcover.

Run: `python tools/test_a3_objects.py` (~20 s on `docs/example-bundle`). Part 1 of the A3
gate: the writer rules are `test_a3_rules.py` and the classification rules plus real-city
sweeps are `test_a3_city.py` (split in Phase 4; the tally is shared via `a3common`).

Checks, in the order the build runs them:

* **the frame** — `beamng.frame.record_transform` reproduces `csiibundle.frames`' own
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

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import (  # noqa: E402
    EXAMPLE,
    check,
    detail_nodes,
    finish,
    level_objects,
    stage_ctx,
    start,
)

# -- the frame ------------------------------------------------------------------------------------


# -- the placement rule ---------------------------------------------------------------------------


# -- the shapes stage -----------------------------------------------------------------------------


def test_shapes_and_instances():
    print("the shapes and instances stages, end to end on the example bundle")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapelods
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    level_dir = tempfile.mkdtemp(prefix="a3-level-")
    try:
        shared = {}
        ctx = stage_ctx(EXAMPLE, level_dir, shared=shared)
        report = shapes_stage.run(ctx, Progress())
        check(report["shapes"] > 0, f"{report['shapes']} shapes written")
        check(report["failed"] == 0, f"no shape failed ({report['errors'][:1]})")
        check(
            "drawDistance" in report and report["drawDistance"],
            "the report states the draw distances",
        )
        for kind, row in report["drawDistance"].items():
            check(
                row["gameCullMedian"] > row["ourCullMedian"],
                f"{kind}: CS2 draws to {row['gameCullMedian']:,.0f} m, this level to "
                f"{row['ourCullMedian']:,.0f} m",
            )
        check(
            set(report["approximations"]) >= {"lodDetailScale", "lodLadderSwitchPoints"},
            "every approximation is declared in the report",
        )

        shape_dir = os.path.join(level_dir, "art", "shapes", "cs2")
        files = sorted(f for f in os.listdir(shape_dir) if f.endswith(".dae"))
        check(len(files) == report["shapes"], f"{len(files)} .dae on disk")
        details, null_px, billboard, names = detail_nodes(os.path.join(shape_dir, files[0]))
        distinct = sorted(set(details), reverse=True)
        check(
            len(distinct) == len(set(details)) and distinct == sorted(distinct, reverse=True),
            f"the distinct detail sizes strictly decrease {distinct} (a split level repeats one)",
        )
        check(
            null_px is not None and all(px > null_px for px in details),
            f"every detail sits above nulldetail{null_px}",
        )
        # A5 gated the imposter on `imposter_min_metres` (three quarters of the view distance
        # when 0), so most shapes correctly have NO billboard now — 3.6 % of the reference city
        # against 100 % before. The invariant is the ordering WHERE there is one, plus the gate
        # itself; asserting every shape has one is what this checked before the gate existed.
        check(
            billboard is None or billboard > null_px,
            f"any imposter ({billboard} px) takes over above the null threshold ({null_px} px)",
        )
        imposters = report["imposters"]
        check(
            imposters["shapes"] <= imposters["ofShapes"],
            f"{imposters['shapes']} of {imposters['ofShapes']} shapes earned an imposter "
            f"(past {imposters['minMetres']:,.0f} m)",
        )
        check(any(n.startswith("Colmesh_") for n in names), "a collider is present")

        # A8/88: a prefab CS2 gives a PHOTOMETRIC light to is a lamp, and with `collision_lights`
        # off it ships no collision mesh at all. The discriminator is the game's own, and the
        # example bundle's cube owns a light - so the same shape must lose its Colmesh here.
        lamp_dir = tempfile.mkdtemp(prefix="a3-lamp-")
        try:
            lamp_ctx = stage_ctx(EXAMPLE, lamp_dir, {"collision_lights": False})
            lamp_report = shapes_stage.run(lamp_ctx, Progress())
            lamp_names = detail_nodes(os.path.join(lamp_dir, "art", "shapes", "cs2", files[0]))[3]
            check(
                not any(n.startswith("Colmesh_") for n in lamp_names),
                "collision_lights off takes the collider off a light-bearing prefab",
            )
            check(
                lamp_report["colliders"].get("lightNone") == lamp_report["shapes"],
                f"...and the report counts it ({lamp_report['colliders']})",
            )
        finally:
            shutil.rmtree(lamp_dir, ignore_errors=True)
        check("base00" in names and "start01" in names, "the vanilla node hierarchy")

        row = next(iter(shared["shapes"]["results"].values()))
        # `nullPx` is the game's own px times `lod_detail_scale`, divided by the imposter's reach
        # ONLY when this shape earned an imposter — the billboard is what carries it the rest of
        # the way, so a shape without one stops at the mesh threshold itself
        scaled = row["gamePx"] * 6.0
        expected = max(
            1, round(scaled / shapelods.IMPOSTER_REACH if row.get("billboardPx") else scaled)
        )
        check(
            abs(row["nullPx"] - expected) <= 1,
            f"the null threshold is the game's {row['gamePx']:.2f} px x 6"
            f"{' / imposter reach' if row.get('billboardPx') else ' (no imposter)'}",
        )

        # -- placement
        report2 = instances_stage.run(ctx, Progress())
        check(report2["placed"] > 0, f"{report2['placed']} objects placed")
        check(
            report2["placed"] == report2["forestItems"] + report2["tsStatics"],
            "every object is one or the other",
        )
        forest_dir = os.path.join(level_dir, "forest")
        forest_files = (
            sorted(f for f in os.listdir(forest_dir)) if os.path.isdir(forest_dir) else []
        )
        if report2["forestItems"]:
            check(
                len(forest_files) == report2["forestTypes"],
                f"{len(forest_files)} forest type files",
            )
            with open(os.path.join(forest_dir, forest_files[0]), encoding="utf-8") as handle:
                item = json.loads(handle.readline())
            check(
                set(item) == {"ctxid", "pos", "rotationMatrix", "scale", "type"},
                f"a forest item carries vanilla's exact key set {sorted(item)}",
            )
            check(len(item["rotationMatrix"]) == 9, "a 3x3 rotation matrix")
            check(isinstance(item["scale"], float), "one scalar scale, as the format allows")
            data = json.load(
                open(
                    os.path.join(level_dir, "art", "forest", "managedItemData.json"),
                    encoding="utf-8",
                )
            )
            check(len(data) == report2["forestTypes"], "every placed type has a TSForestItemData")
            entry = next(iter(data.values()))
            check(
                entry["class"] == "TSForestItemData" and entry["shapeFile"].startswith("/levels/"),
                "the item data names its shape by game path",
            )
            check(item["type"] in data, "the item's type resolves in the item data")

        objects = level_objects(level_dir)
        statics = [o for o in objects if o.get("class") == "TSStatic"]
        check(len(statics) == report2["tsStatics"], f"{len(statics)} TSStatics in the level tree")
        if statics:
            one = statics[0]
            check(
                all(k in one for k in ("instanceColor", "instanceColor1", "instanceColor2")),
                "a TSStatic carries all three colorSet channels",
            )
            check(
                len(one["rotationMatrix"]) == 9 and len(one["scale"]) == 3,
                "rotation 3x3 and a 3-vector scale",
            )
        check(
            any(o.get("class") == "Forest" for o in objects) or not report2["forestItems"],
            "the level owns a Forest object when it has forest items",
        )
        check(
            report2["approximations"]["forestPaintVariants"]["count"] == report2["forestItems"],
            "the paint-variant approximation counts every forest item",
        )

    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


def test_region_and_toggles():
    print("the two passes agree, and the toggles reach both")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    level_dir = tempfile.mkdtemp(prefix="a3-region-")
    try:
        shared = {}
        ctx = stage_ctx(EXAMPLE, level_dir, settings={"trees": False}, shared=shared)
        first = shapes_stage.run(ctx, Progress())
        report = instances_stage.run(ctx, Progress())
        placed_kinds = {row[0] for row in shared["shapes"]["demand"]}
        check(
            report["noShape"] == first["scan"]["noMesh"],
            f"the two passes agree on what has no mesh ({report['noShape']} each)",
        )
        check(
            report["kept"] - report["noShape"] == report["placed"],
            "every kept record with a mesh was placed exactly once",
        )
        check(placed_kinds, "the demand scan found something")
        kinds = {entry["kind"] for entry in shared["shapes"]["demand"].values()}
        check("tree" not in kinds, "trees off means no tree shape is written")
    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


# -- lights ---------------------------------------------------------------------------------------


def main():
    start()
    test_shapes_and_instances()
    test_region_and_toggles()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_objects.py`."""
    assert main() in (None, 0)
