"""Shared tally and fixtures for the A3 gate's five files.

The A3 gate is `test_a3_frame.py` (the transform), `test_a3_objects.py` (the shapes and
instances stages, end to end on the example bundle), `test_a3_scatter.py` (forest, lights and
groundcover), `test_a3_rules.py` (the BeamNG-writer rules) and `test_a3_city.py` (the game's
classification rules plus the real-bundle sweeps). Each file is its own pytest gate and its own
script; they share this tally so a run reads the same either way, and the four fixtures below
so that "build a level from the example bundle" is written once rather than five times.
"""

import json
import os
import shutil
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
NS = "{http://www.collada.org/2005/11/COLLADASchema}"

FAILS = []
COUNT = [0]


def check(cond, what, quiet=False):
    COUNT[0] += 1
    if not quiet or not cond:
        print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def start():
    """Reset the tally — each file's `main()` begins here, so one pytest process running all
    three gates does not leak one file's failures into the next file's summary."""
    FAILS.clear()
    COUNT[0] = 0


def finish():
    print()
    if FAILS:
        print(f"{len(FAILS)} of {COUNT[0]} FAILED:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"{COUNT[0]}/{COUNT[0]} checks passed")
    return 0


def real_bundle():
    from csiibundle.bake.locate import default_bundle_dir

    path = default_bundle_dir()
    return path if path and os.path.isfile(os.path.join(path, "instances.jsonl")) else None


def stage_ctx(bundle, level_dir, settings=None, region=None, shared=None):
    from cs2beamng.stages import BuildContext

    base = {
        "level_name": "t",
        "level_title": "t",
        "trees": True,
        "props": True,
        "buildings": True,
        "foliage_density": 100,
        "prop_density": 100,
        "generate_lods": False,
        "imposters": True,
        "lod_detail_scale": 6.0,
        "collision_preset": "simplified",
        "collision_buildings": True,
        "collision_props": True,
        "collision_trees": True,
        # the example bundle's one prefab OWNS a photometric light, so `collision_lights`
        # decides whether it ships a collider at all (audit 88); the off case is checked
        # explicitly below rather than left to this default
        "collision_lights": True,
        "forest_shadows": True,
        "tree_wind": False,
        "lights": True,
        "lights_max": 5,
        "groundcover": False,
    }
    base.update(settings or {})
    return BuildContext(
        settings=base,
        bundle=bundle,
        level_dir=level_dir,
        derived_dir=os.path.join(bundle, "derived"),
        census={},
        region=region,
        report={},
        jobs=1,
        shared=shared if shared is not None else {},
    )


def detail_nodes(path):
    root = ET.parse(path).getroot()
    names = [n.get("id") for n in root.iter(NS + "node")]
    details, null_px, billboard = [], None, None
    for name in names:
        if name.startswith("nulldetail"):
            null_px = int(name[len("nulldetail") :])
        elif name.startswith("bb_autobillboard"):
            billboard = int(name[len("bb_autobillboard") :])
        elif "_a" in name and name.rsplit("_a", 1)[1].isdigit():
            details.append(int(name.rsplit("_a", 1)[1]))
    return details, null_px, billboard, names


def forest_bundle(root):
    """The example bundle plus 60 trees and 40 props on its one mesh — the example city has
    three records and none of them reaches the forest path, which is most of this stage."""
    bundle = os.path.join(root, "bundle")
    shutil.copytree(EXAMPLE, bundle)
    records = []
    for i in range(60):
        records.append(
            {
                "id": 5000 + i,
                "assetId": "carto_example_cube",
                "kind": "tree",
                "pos": [100.0 + i, 12.5, -340.0],
                "rot": [0.0, 0.0, 0.0, 1.0],
                "attrs": {
                    "treeState": "Adult",
                    "growth": 1.0,
                    "growthScale": 0.5 + 0.01 * (i % 40),
                    "colorSet": [
                        [0.2 + 0.01 * (i % 30), 0.6, 0.3, 1.0],
                        [1.0, 1.0, 1.0, 1.0],
                        [1.0, 1.0, 1.0, 1.0],
                    ],
                },
            }
        )
    for i in range(40):
        scale = [1.0, 1.0, 1.0] if i % 4 else [1.0, 2.0, 1.0]
        records.append(
            {
                "id": 6000 + i,
                "assetId": "carto_example_cube",
                "kind": "prop",
                "pos": [100.0 + i, 12.0, -300.0],
                "rot": [0.0, 0.0, 0.0, 1.0],
                "scale": scale,
            }
        )
    with open(
        os.path.join(bundle, "instances.jsonl"), "a", encoding="utf-8", newline="\n"
    ) as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return bundle


def level_objects(level_dir):
    from cs2beamng.beamng.level import read_objects

    return [obj for _f, _n, obj in read_objects(level_dir) if obj]
