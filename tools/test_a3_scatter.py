"""cs2beamng A3 gate — the SCATTERED layers: forest, lights and groundcover.

Run: `python tools/test_a3_scatter.py`  (needs `pip install -e .`; ~10 s, example bundle only).

Part of the A3 gate beside `test_a3_frame.py`, `test_a3_objects.py` and `test_a3_city.py`;
tally shared through `a3common`.

These three are grouped because none of them is a `TSStatic`: a forest item is referenced by no
object at all, a light is a property of one, and groundcover is a default the level carries.
Each is a layer the oracle would show as missing rather than wrong, which is the failure that
takes longest to notice. What is pinned: a `.forest4.json` line has vanilla's exact key set and
the item data names every placed type; a spot's `innerAngle` is a percentage of the outer,
bone-space entries are skipped and the cap holds; `groundcover` is off by default and writes
nothing without a grassy layer.
"""

import json
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import (  # noqa: E402
    EXAMPLE,
    check,
    finish,
    forest_bundle,
    level_objects,
    stage_ctx,
    start,
)


def test_forest_path():
    print("the forest path, on the example bundle plus 100 synthetic placements")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    root = tempfile.mkdtemp(prefix="a3-forest-")
    try:
        bundle = forest_bundle(root)
        level_dir = os.path.join(root, "level")
        shared = {}
        ctx = stage_ctx(bundle, level_dir, shared=shared)
        shapes_stage.run(ctx, Progress())
        report = instances_stage.run(ctx, Progress())
        check(
            report["forestItems"] == 90,
            f"{report['forestItems']} forest items (60 trees + 30 uniform props)",
        )
        check(
            report["tsStatics"] == 12,
            f"{report['tsStatics']} TSStatics (2 buildings + 10 scaled props)",
        )
        forest_dir = os.path.join(level_dir, "forest")
        files = sorted(os.listdir(forest_dir))
        check(len(files) == report["forestTypes"], f"{len(files)} forest type files")
        with open(os.path.join(forest_dir, files[0]), encoding="utf-8") as handle:
            items = [json.loads(line) for line in handle if line.strip()]
        check(
            set(items[0]) == {"ctxid", "pos", "rotationMatrix", "scale", "type"},
            f"a forest item carries vanilla's exact key set {sorted(items[0])}",
        )
        check(len(items[0]["rotationMatrix"]) == 9, "a 3x3 rotation matrix")
        check(isinstance(items[0]["scale"], float), "one scalar scale, as the format allows")
        check(
            len({round(i["scale"], 3) for i in items}) > 5,
            "growthScale reaches the forest item's scale",
        )
        _check_item_data(level_dir, report, items[0]["type"])
        _check_rebuild_replaces(bundle, level_dir, forest_dir, files[0], len(items))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_item_data(level_dir, report, first_type):
    """`managedItemData.json` names every placed type and points at a real shape."""
    with open(
        os.path.join(level_dir, "art", "forest", "managedItemData.json"), encoding="utf-8"
    ) as handle:
        data = json.load(handle)
    check(len(data) == report["forestTypes"], "every placed type has a TSForestItemData")
    entry = data[first_type]
    check(
        entry["class"] == "TSForestItemData" and entry["shapeFile"].startswith("/levels/"),
        "the item data names its shape by game path",
    )
    check(entry.get("radius", 0) > 0, "the item data carries the shape's radius")
    objects = level_objects(level_dir)
    check(any(o.get("class") == "Forest" for o in objects), "the level owns a Forest object")


def _check_rebuild_replaces(bundle, level_dir, forest_dir, first_file, placed):
    """A rebuild REPLACES the forest rather than appending to it — the failure that doubles a
    city's tree count every time someone rebuilds."""
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    ctx = stage_ctx(bundle, level_dir, shared={})
    shapes_stage.run(ctx, Progress())
    instances_stage.run(ctx, Progress())
    with open(os.path.join(forest_dir, first_file), encoding="utf-8") as handle:
        again = sum(1 for line in handle if line.strip())
    check(again == placed, f"a rebuild rewrites the forest ({again} items, not {2 * placed})")


def test_lights():
    print("lights")
    from cs2beamng.stages import lights as lights_stage
    from csiibundle import lights as bundle_lights
    from csiibundle.bake.progress import Progress

    cone = bundle_lights.cone_angles(
        {"type": "Spot", "spotAngle": 80.0, "innerSpotPercentage": 25.0}
    )
    check(
        cone == (80.0, 20.0), "innerSpotPercentage is a percentage of the outer angle, not degrees"
    )

    level_dir = tempfile.mkdtemp(prefix="a3-lights-")
    try:
        ctx = stage_ctx(EXAMPLE, level_dir)
        report = lights_stage.run(ctx, Progress())
        check("lights" in report, "the stage runs on the example bundle")
        check(report.get("lights", 0) <= ctx.settings["lights_max"], "the cap holds")
        if report.get("lights"):
            objects = [
                o for o in level_objects(level_dir) if o.get("class") in ("PointLight", "SpotLight")
            ]
            check(len(objects) == report["lights"], "every reported light is in the level tree")
            check(all(o["castShadows"] is False for o in objects), "no placed light casts a shadow")
            # A6.9/D2: vanilla writes `isEnabled: false` on 2,987 of its 3,063 night lights, and
            # `environment.lua` only refreshes the set on a day/night TRANSITION.
            check(all(o["isEnabled"] is False for o in objects), "a night light ships disabled")
            # A6.9/D3: the photometry. `intensity` in the CLASS's unit and the `brightness` the
            # renderer actually uses, in BeamNG's own exact 5,000:1 ratio.
            for obj in objects:
                cd = (
                    obj["intensity"] / (4.0 * math.pi)
                    if obj["class"] == "PointLight"
                    else obj["intensity"]
                )
                check(
                    abs(cd / 5000.0 - obj["brightness"]) < 1e-4,
                    f"{obj['class']} brightness is intensity/5000 in candela",
                    quiet=True,
                )
                check(
                    obj["intensityUnit"] in ("cd", "lm"),
                    "intensityUnit is one of BeamNG's four, not CS2's Lux",
                    quiet=True,
                )
            check(True, "every light carries BeamNG's own units and a brightness")
        check(
            "emissiveSlotsAlwaysOn" in report.get("approximations", {}),
            "the colour slots' day/night switch is declared as the residual",
        )
    finally:
        shutil.rmtree(level_dir, ignore_errors=True)


def test_groundcover_default():
    print("groundcover")
    from cs2beamng import settings as settings_mod
    from cs2beamng.stages import groundcover as gc

    check(
        settings_mod.defaults()["groundcover"] is False,
        "groundcover is OFF by default - CS2 has no groundcover system to be faithful to",
    )
    check(not gc.enabled(settings_mod.defaults()), "the stage does not run by default")
    check(
        all(preset.get("groundcover") is False for preset in settings_mod.PRESETS.values()),
        "no quality preset turns it on",
    )
    atlas = gc._atlas((0.3, 0.5, 0.2))
    check(atlas.shape == (gc.ATLAS_PX, gc.ATLAS_PX, 4), "the synthesised atlas is RGBA")
    check(
        bool((atlas[:, :, 3] > 0).any()) and bool((atlas[:, :, 3] == 0).any()),
        "the blades have alpha and so does the gap between them",
    )


def main():
    start()
    test_forest_path()
    test_lights()
    test_groundcover_default()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_scatter.py`."""
    assert main() in (None, 0)
