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
import math
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import EXAMPLE, NS, check, finish, start  # noqa: E402

# -- the frame ------------------------------------------------------------------------------------


def test_frame():
    print("the instance transform is the contract's own composition")
    import random

    from cs2beamng.beamng import frame as frame_mod
    from csiibundle import frames

    random.seed(20260818)
    worst = 0.0
    for _ in range(300):
        quat = [random.uniform(-1, 1) for _ in range(4)]
        norm = sum(v * v for v in quat) ** 0.5
        record = {
            "pos": [random.uniform(-4000, 4000) for _ in range(3)],
            "rot": [v / norm for v in quat],
            "scale": [random.uniform(0.3, 3.0) for _ in range(3)],
        }
        # the .dae bakes `P . diag(-1,1,1)` into its vertices, so that basis divides out
        basis = frames.P_ZUP @ frames.MIRROR_X
        reference = frames.zup_instance_matrix(record, mesh_basis=basis)
        position, rotation, scale = frame_mod.record_transform(record)
        mine = numpy.eye(4)
        mine[:3, :3] = numpy.array(rotation) @ numpy.diag(scale)
        mine[:3, 3] = position
        worst = max(worst, float(numpy.abs(mine - reference).max()))
    check(worst < 1e-9, f"P.T.R.S.P^-1 reproduced to {worst:.2e}")

    # a rotation must stay a rotation: no second mirror anywhere
    _p, rot, _s = frame_mod.record_transform(
        {"pos": [0, 0, 0], "rot": [0.0, 0.3826834, 0.0, 0.9238795]}
    )
    det = float(numpy.linalg.det(numpy.array(rot)))
    check(abs(det - 1.0) < 1e-9, f"the rotation has determinant +1 ({det:.6f})")

    # a tree's growthScale is uniform and multiplies the linear part only
    _p, _r, scaled = frame_mod.record_transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(
        all(abs(v - 0.5) < 1e-9 for v in scaled), "growthScale reaches the scale, not the position"
    )
    pos, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(pos == [1.0, 3.0, 2.0], "the position is the Y/Z swap and nothing else")


def test_decal_lift_and_the_quaternion():
    print("the transform folds in the §4.2a lift, and lets a non-unit quaternion through")
    from cs2beamng.beamng import frame as frame_mod

    # §4.2a: a decal instance is coplanar paint and MUST be lifted clear of the surface it
    # paints over, or it z-fights with it. No decal reaches `record_transform` today —
    # `placement.BAKED_KINDS` sends them to `bake/markings`, which applies the lift as the
    # drape's epsilon — so this pins a LATENT trap: the day a decal does reach here, it arrives
    # lifted, because the transform is the shared one rather than a copy that forgot.
    from csiibundle import decals

    lift = decals.LIFT["instance_decal"]
    lifted, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "kind": "decal"})
    check(
        abs(lifted[2] - (2.0 + lift)) < 1e-12 and lifted[:2] == [1.0, 3.0],
        f"a decal is lifted by exactly {lift} m, on the up axis only",
    )
    plain, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "kind": "prop"})
    check(plain == [1.0, 3.0, 2.0], "and nothing else is lifted at all")
    check(
        lift > decals.LIFT["area"] > decals.LIFT["lane_marking"],
        "the ladder still puts decal instances above the areas they stand on",
    )

    # A stored non-unit quaternion is a signal, not noise: `frames.quat_matrix` deliberately
    # does not renormalise, and the app's old `_quat_rows` did, which discarded it. It should
    # arrive as a scale off 1.0.
    k = 1.001
    half = 0.7071067811865476
    _p, _r, off = frame_mod.record_transform(
        {"pos": [0, 0, 0], "rot": [0.0, half * k, 0.0, half * k]}
    )
    # The deviation lands on the two axes the turn acts on, at k^2 - 1, and not on the third.
    check(
        abs(max(off) - k**2) < 1e-5 and abs(min(off) - 1.0) < 1e-12,
        f"a non-unit quaternion reaches the scale rather than being renormalised away ({off})",
    )


# -- the placement rule ---------------------------------------------------------------------------


def test_placement():
    print("forest vs TSStatic")
    from cs2beamng.beamng import placement

    settings = {
        "trees": True,
        "props": True,
        "buildings": True,
        "foliage_density": 100,
        "prop_density": 100,
    }
    decal = {"id": 1, "assetId": "a", "kind": "decal", "pos": [0, 0, 0]}
    tree = {"id": 2, "assetId": "a", "kind": "tree", "pos": [0, 0, 0]}
    building = {"id": 3, "assetId": "a", "kind": "building", "pos": [0, 0, 0]}
    scaled = {"id": 4, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [1.0, 2.0, 1.0]}
    uniform = {"id": 5, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [2.0, 2.0, 2.0]}
    site = {
        "id": 6,
        "assetId": "a",
        "kind": "building",
        "pos": [0, 0, 0],
        "attrs": {"state": "construction"},
    }

    check(not placement.keep(decal, settings), "a decal is placed by the markings bake, not here")
    check(
        not placement.keep(site, settings),
        "a building under construction is not drawn (should_draw)",
    )
    check(placement.wants_forest(tree), "a tree is a forest item")
    check(not placement.wants_forest(building), "a building is a TSStatic")
    check(not placement.wants_forest(scaled), "a non-uniformly scaled prop is a TSStatic")
    check(placement.wants_forest(uniform), "a uniformly scaled prop stays a forest item")
    check(
        abs(placement.uniform_scale(uniform) - 2.0) < 1e-9,
        "the forest scalar is the record's scale",
    )
    check(
        not placement.wants_forest(tree, forest_allowed=False),
        "the forest can be switched off wholesale",
    )

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


def _detail_nodes(path):
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


def test_shapes_and_instances():
    print("the shapes and instances stages, end to end on the example bundle")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    level_dir = tempfile.mkdtemp(prefix="a3-level-")
    try:
        shared = {}
        ctx = _stage_ctx(EXAMPLE, level_dir, shared=shared)
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
        details, null_px, billboard, names = _detail_nodes(os.path.join(shape_dir, files[0]))
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
            lamp_ctx = _stage_ctx(EXAMPLE, lamp_dir, {"collision_lights": False})
            lamp_report = shapes_stage.run(lamp_ctx, Progress())
            lamp_names = _detail_nodes(os.path.join(lamp_dir, "art", "shapes", "cs2", files[0]))[3]
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
            1, round(scaled / shapes_stage.IMPOSTER_REACH if row.get("billboardPx") else scaled)
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

        objects = _level_objects(level_dir)
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


def _forest_bundle(root):
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


def test_forest_path():
    print("the forest path, on the example bundle plus 100 synthetic placements")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    root = tempfile.mkdtemp(prefix="a3-forest-")
    try:
        bundle = _forest_bundle(root)
        level_dir = os.path.join(root, "level")
        shared = {}
        ctx = _stage_ctx(bundle, level_dir, shared=shared)
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
        data = json.load(
            open(os.path.join(level_dir, "art", "forest", "managedItemData.json"), encoding="utf-8")
        )
        check(len(data) == report["forestTypes"], "every placed type has a TSForestItemData")
        entry = data[items[0]["type"]]
        check(
            entry["class"] == "TSForestItemData" and entry["shapeFile"].startswith("/levels/"),
            "the item data names its shape by game path",
        )
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
        check(
            again == len(items),
            f"a rebuild rewrites the forest ({again} items, not {2 * len(items)})",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _level_objects(level_dir):
    from cs2beamng.beamng.level import read_objects

    return [obj for _f, _n, obj in read_objects(level_dir) if obj]


def test_region_and_toggles():
    print("the two passes agree, and the toggles reach both")
    from cs2beamng.stages import instances as instances_stage
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle.bake.progress import Progress

    level_dir = tempfile.mkdtemp(prefix="a3-region-")
    try:
        shared = {}
        ctx = _stage_ctx(EXAMPLE, level_dir, settings={"trees": False}, shared=shared)
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
        ctx = _stage_ctx(EXAMPLE, level_dir)
        report = lights_stage.run(ctx, Progress())
        check("lights" in report, "the stage runs on the example bundle")
        check(report.get("lights", 0) <= ctx.settings["lights_max"], "the cap holds")
        if report.get("lights"):
            objects = [
                o
                for o in _level_objects(level_dir)
                if o.get("class") in ("PointLight", "SpotLight")
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
    test_frame()
    test_decal_lift_and_the_quaternion()
    test_placement()
    test_shapes_and_instances()
    test_forest_path()
    test_region_and_toggles()
    test_lights()
    test_groundcover_default()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_objects.py`."""
    assert main() in (None, 0)
