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

import itertools
import json
import math
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

FAILS = []
CHECKS = 0


def check(cond, what, quiet=False):
    global CHECKS
    CHECKS += 1
    if not quiet or not cond:
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

    from cs2beamng.stages import instances as stage
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
    check(
        all(abs(v - 0.5) < 1e-9 for v in scaled), "growthScale reaches the scale, not the position"
    )
    pos, _r, _s = stage._transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(pos == [1.0, 3.0, 2.0], "the position is the Y/Z swap and nothing else")


def test_decal_lift_and_the_quaternion():
    print("the transform folds in the §4.2a lift, and lets a non-unit quaternion through")
    from cs2beamng.stages import instances as stage

    # §4.2a: a decal instance is coplanar paint and MUST be lifted clear of the surface it
    # paints over, or it z-fights with it. No decal reaches `_transform` today —
    # `placement.BAKED_KINDS` sends them to `bake/markings`, which applies the lift as the
    # drape's epsilon — so this pins a LATENT trap: the day a decal does reach here, it arrives
    # lifted, because the transform is the shared one rather than a copy that forgot.
    from csiibundle import decals

    lift = decals.LIFT["instance_decal"]
    lifted, _r, _s = stage._transform({"pos": [1, 2, 3], "kind": "decal"})
    check(
        abs(lifted[2] - (2.0 + lift)) < 1e-12 and lifted[:2] == [1.0, 3.0],
        f"a decal is lifted by exactly {lift} m, on the up axis only",
    )
    plain, _r, _s = stage._transform({"pos": [1, 2, 3], "kind": "prop"})
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
    _p, _r, off = stage._transform({"pos": [0, 0, 0], "rot": [0.0, half * k, 0.0, half * k]})
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


BEAMNG_INSTALL = r"G:\SteamLibrary\steamapps\common\BeamNG.drive"


def test_rotation_matrix_is_basis_vectors():
    """`rotationMatrix` holds the object's OWN AXES in groups of three, not a row-major matrix.

    The only non-circular gate in the rotation chain: it asks BeamNG's own shipped levels, not
    our writers. A tunnel's spot light points at the road, so reading the field as groups must
    put a vanilla SpotLight's +Y DOWN — and reading it row-major puts it up, which is what our
    level shipped for months (A6.8, audit §78)."""
    print("rotationMatrix is the object's basis vectors (vanilla-anchored)")
    from cs2beamng.beamng import frame

    # 1. the serialisation itself, on a rotation whose axes are unmistakable: a +90 deg yaw
    yaw = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]  # columns are the axes
    flat = frame.basis_flat(yaw)
    check(flat[0:3] == [0.0, 1.0, 0.0], f"group 0 is the object's X axis ({flat[0:3]})")
    check(flat[3:6] == [-1.0, 0.0, 0.0], f"group 1 is its Y axis ({flat[3:6]})")
    check(flat[6:9] == [0.0, 0.0, 1.0], f"group 2 is its Z axis ({flat[6:9]})")
    east = frame.heading_matrix(1.0, 0.0)
    check(east[3:6] == [1.0, 0.0, 0.0], f"an object facing east has +Y east ({east[3:6]})")

    # 2. the convention, against BeamNG's own levels
    levels = os.path.join(BEAMNG_INSTALL, "content", "levels")
    if not os.path.isdir(levels):
        print("  ..  vanilla levels absent, the anchor is skipped")
        return
    import zipfile

    groups_down = columns_down = total = 0
    for name in sorted(f for f in os.listdir(levels) if f.endswith(".zip"))[:6]:
        with zipfile.ZipFile(os.path.join(levels, name)) as z:
            for entry in [n for n in z.namelist() if n.endswith(".level.json")]:
                for line in z.read(entry).splitlines():
                    line = line.strip()
                    if not line.startswith(b"{") or b'"SpotLight"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    m = obj.get("rotationMatrix")
                    if obj.get("class") != "SpotLight" or not m or len(m) != 9:
                        continue
                    total += 1
                    groups_down += int(m[5] <= -0.9)  # group 1 (+Y) points down
                    columns_down += int(m[7] <= -0.9)  # column 1 (+Y) points down
    if not total:
        print("  ..  no vanilla SpotLights found, the anchor is skipped")
        return
    check(total > 100, f"{total:,} vanilla SpotLights read from the first levels on disk")
    check(
        groups_down > 10 * max(columns_down, 1),
        f"read as GROUPS {groups_down:,} of {total:,} point down; read row-major {columns_down:,} do",
    )


def test_switch_points_only_ever_defer():
    """The A6.7 regression, as a gate: a measured displacement may push a generated level FURTHER
    out than the ladder puts it, never nearer.

    Allowing both directions is what shipped 2,586 of 2,903 prop shapes onto their decimated
    geometry closer than the ladder ever had — a streetlight's 24-triangle level from 13 m — and
    the level came back "cranes rotated and floating, highrises all diagonal". Surface distance is
    not visual error for thin geometry, and the ladder's size-keyed spacing is what protects it,
    so the rule is one-directional by construction and this proves it stays that way."""
    print("LOD switch points defer, never advance")
    from cs2beamng.beamng import dae
    from cs2beamng.stages import shapes as shapes_stage
    from csiibundle import glbwrite

    box_v = numpy.array(
        [[0.0, 0, 0], [4, 0, 0], [4, 0, 6], [0, 0, 6], [0, 3, 0], [4, 3, 0], [4, 3, 6], [0, 3, 6]]
    )
    box_t = numpy.array(
        [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [2, 3, 7], [2, 7, 6]]
    )
    mesh = dae.Mesh("box", box_v, box_t, [("mat", len(box_t))])
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "lods"), exist_ok=True)
        for level in (1, 2):
            glbwrite.write_glb(
                os.path.join(tmp, "lods", f"src_LOD{level}.glb"),
                [
                    {
                        "name": "box",
                        "primitives": [
                            {
                                "verts": box_v,
                                "normals": None,
                                "uvs": None,
                                "tris": box_t[: 4 if level == 1 else 2],
                                "material": "mat",
                            }
                        ],
                    }
                ],
            )
        options = {"generate_lods": True, "switch_error_px": 6.0}
        moved = 0
        for null_px in (3.0, 9.0, 40.0):
            for metres in (None, 0.0, 0.001, 0.5, 5.0):
                _levels, stats, meshes = shapes_stage._lod_levels(
                    tmp,
                    "src",
                    "shape",
                    mesh,
                    null_px,
                    8.0,
                    [metres, metres],
                    "building",
                    {},
                    {},
                    "level",
                    options,
                )
                sizes = stats["detailSizes"]
                ladder = [
                    max(2, int(round(null_px * r)))
                    for r in reversed(shapes_stage.LADDER_RATIOS[: len(meshes)])
                ]
                check(len(meshes) == 3, f"nullPx {null_px}: the two generated levels loaded")
                check(
                    all(got <= want + 0.5 for got, want in zip(sizes, ladder)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} never above the ladder {ladder}",
                )
                check(
                    all(a > b for a, b in itertools.pairwise(sizes)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} strictly decreases",
                )
                if any(got < want - 0.5 for got, want in zip(sizes, ladder)):
                    moved += 1
        check(moved > 0, f"and a level that IS out of shape is deferred ({moved} of 15 cases)")
        # vegetation is exempt by kind, and exempt means exactly the ladder
        _levels, veg, _m = shapes_stage._lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "tree", {}, {}, "level", options
        )
        ladder = [
            max(2, int(round(9.0 * r)))
            for r in reversed(shapes_stage.LADDER_RATIOS[: len(veg["triangles"])])
        ]
        check(
            veg["detailSizes"] == ladder,
            f"a tree keeps the ladder exactly ({veg['detailSizes']} == {ladder})",
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

    bundle = real_bundle()
    if bundle:
        ids, stats = placement.road_clipped(bundle, {"foliage_road_clearance": 1.5})
        check(stats["clipped"] == len(ids), f"{stats['clipped']:,} clipped ids, all distinct")
        share = stats["clipped"] / max(stats["trees"], 1)
        check(
            0 < share < 0.05,
            f"{stats['clipped']:,} of {stats['trees']:,} trees = {share:.2%} "
            f"(a deviation, so it must stay small)",
        )


def test_color_palette():
    """A6.3 — CS2's colour mask converted into BeamNG's palette semantics.

    Both sides implemented literally from their own sources: CS2 from `docs/bundle-schema.md`
    §7.2, BeamNG from `shaders/common/material/shadergen/shadergen.h.hlsl::getColorPalette`.
    The old export forced the mask's alpha to 255, which is why an untinted texel rendered BLACK.
    """
    print("colour palette (A6.3)")
    from cs2beamng.beamng.materials import _colormask_rgba

    def cs2(base, mask, colours):
        t = numpy.asarray(base, dtype=numpy.float64)
        for i in range(3):
            t = t * (1.0 - mask[i]) + (t * numpy.asarray(colours[i])) * mask[i]
        return t

    def beamng(base, mask_rgba, colours):
        s = numpy.asarray(mask_rgba, dtype=numpy.float64).copy()
        s[:3] = s[:3] / (s[0] + s[1] + s[2] + 1e-10)
        a = s[3]
        s[:3] *= a
        palette = sum(s[i] * numpy.asarray(colours[i]) for i in range(3)) + (1.0 - a)
        return numpy.asarray(base, dtype=numpy.float64) * palette

    base = [0.55, 0.50, 0.47]
    colours = [[0.90, 0.30, 0.20], [0.20, 0.55, 0.85], [0.0, 0.0, 0.0]]

    worst = 0.0
    for m in (
        [0, 0, 0],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
        [0.3, 0, 0],
        [0, 0.75, 0],
        [0, 0, 0.5],
        [0.05, 0, 0],
    ):
        mask = numpy.asarray(m, dtype=numpy.float64)
        conv = _colormask_rgba((mask * 255).round().astype(numpy.uint8).reshape(1, 1, 3), "rgb")
        worst = max(
            worst,
            float(
                numpy.abs(
                    beamng(base, conv[0, 0] / 255.0, colours) - cs2(base, mask, colours)
                ).max()
            ),
        )
    check(
        worst < 0.002,
        f"a DISJOINT mask reproduces CS2's tint exactly (worst {worst:.5f}, the alpha's own step)",
    )

    # the bug this replaces: alpha forced to 255 makes an untinted texel BLACK
    black = beamng(base, [0.0, 0.0, 0.0, 1.0], colours)
    check(
        float(numpy.abs(black).max()) < 1e-9,
        "the OLD alpha-255 export renders an untinted texel pure black (the reported defect)",
    )
    conv = _colormask_rgba(numpy.zeros((1, 1, 3), numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 0, "an untinted texel now carries alpha 0 - no tint, not black")
    conv = _colormask_rgba(numpy.array([[[255, 0, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "a fully tinted texel carries alpha 255")
    conv = _colormask_rgba(numpy.array([[[200, 100, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "alpha saturates rather than wrapping")

    # and the material must actually switch the palette on
    from cs2beamng.beamng import materials as bm

    class _L:
        name = "lvl"

        def game_path(self, *p):
            return "/" + "/".join(p)

        def pid(self, k):
            return k

    stage = bm.material_v15(_L(), "m", {"tintable": True}, {"colorMask": "/t.dds"})
    stage0 = stage["Stages"][0] if "Stages" in stage else stage
    check(
        stage0.get("paletteBaseColor") is True,
        "a tintable material sets paletteBaseColor - without it the palette applies to nothing",
    )

    # A6.9/D5: the emissive nits come from the joined slot, INCLUDING when the slot says zero.
    # 115 of the reference city's slots do, and every one is the unlit face of a billboard; a
    # falsy test would light all of them at the flat fallback.
    lit = bm.material_v15(_L(), "m", {"emissiveNits": 5000.0}, {"emissive": "/e.dds"})
    lit0 = lit["Stages"][0] if "Stages" in lit else lit
    check(lit0.get("emissiveIntensityNits") == 5000.0, "a joined slot's nits are used")
    off = bm.material_v15(_L(), "m", {"emissiveNits": 0.0}, {"emissive": "/e.dds"})
    off0 = off["Stages"][0] if "Stages" in off else off
    check(
        off0.get("emissiveIntensityNits") == 0.0,
        "a slot authored to emit NOTHING stays at zero, not the flat fallback",
    )
    none = bm.material_v15(_L(), "m", {}, {"emissive": "/e.dds"})
    none0 = none["Stages"][0] if "Stages" in none else none
    check(
        none0.get("emissiveIntensityNits") == bm.EMISSIVE_NITS
        and none0.get("emissiveFactor") == [1.0, 1.0, 1.0],
        "a material no slot reaches takes the declared fallback, white at 200 nits",
    )

    # AND THE MASK MUST REACH THE LEVEL. `199f084` wrote the conversion into `texture_name`, the
    # NAMING function, where `arr` does not exist: every `colormask` export raised NameError, all
    # 724 masks were dropped, and the built level carried ZERO `colorPaletteMap` - so the whole of
    # A6.3 was in the build report and nowhere else. `material_v15` alone cannot see that.
    try:
        name = bm.texture_name("/src/abc.png", "colormask", use_dds=False)
        ok = name.endswith(".colormask.png")
    except Exception as exc:
        name, ok = repr(exc), False
    check(ok, f"texture_name('colormask') returns a name rather than raising ({name})")

    written = {}

    class _LW(_L):
        def write_bytes_if_changed(self, data, *parts):
            written[parts[-1]] = data

    src = os.path.join(tempfile.mkdtemp(), "mask.png")
    from csiibundle import png as _png

    with open(src, "wb") as h:
        h.write(_png.encode_rgb(numpy.array([[[200, 100, 0], [0, 0, 0]]], numpy.uint8)))
    out = bm.export_texture(_LW(), src, "colormask", dds=False)
    check(bool(out) and written, f"a colormask actually exports ({out})")
    if written:
        tmp = os.path.join(tempfile.mkdtemp(), "out.png")
        with open(tmp, "wb") as h:
            h.write(next(iter(written.values())))
        back = _png.rgba_array(tmp)
        check(
            back.shape[2] == 4 and int(back[0, 0, 3]) == 255 and int(back[0, 1, 3]) == 0,
            "the exported mask carries the tint coverage in ALPHA (255 tinted, 0 untinted)",
        )


def test_a8_performance_rules():
    """A8 — the two new predicates, both of which remove something a viewer can see."""
    print("A8 performance rules")
    from cs2beamng.beamng import materials as bm
    from cs2beamng.stages import instances as inst
    from cs2beamng.stages import shapes as shp

    # P1: EVERY exported kind must be capped. A kind missing from the table reads as 0 = uncapped,
    # which is how 767 colour masks shipped at 1024 beside 256-pixel albedos for 1,025 MiB.
    caps = bm.caps_from_settings(
        {"texture_max": 256, "texture_max_normal": 128, "texture_max_data": 64}
    )
    check(
        caps.get("colormask") == 256,
        f"the colour mask takes the colour cap ({caps.get('colormask')})",
    )
    missing = [
        k
        for k in ("color", "normal", "data", "opacity", "colormask")
        if k not in bm.KIND_CAP_SETTING
    ]
    check(not missing, f"every exported texture kind has a cap setting (missing: {missing})")

    # P4: the footprint predicate. Height alone passed a hydrant (0.46 m across, 0.97 m tall).
    hydrant = {"boundsMin": [-0.23, 0.0, -0.23], "boundsMax": [0.23, 0.97, 0.23]}
    sign = {"boundsMin": [-0.27, 0.0, -0.05], "boundsMax": [0.27, 2.13, 0.05]}
    kiosk = {"boundsMin": [-1.2, 0.0, -1.2], "boundsMax": [1.2, 2.4, 1.2]}
    check(inst._shape_footprint(hydrant) < 0.8, "a fire hydrant is under the footprint threshold")
    check(inst._shape_footprint(sign) < 0.8, "a road sign is under it, though it is 2.13 m TALL")
    check(
        inst._shape_height(sign) > inst.FOREST_COLLIDABLE_MIN_HEIGHT,
        "...and the HEIGHT rule alone would have kept its collider - the defect A8 fixes",
    )
    check(inst._shape_footprint(kiosk) >= 0.8, "a 2.4 m kiosk keeps its collider")
    check(
        inst._shape_footprint({}) == float("inf"), "an unmeasurable shape NEVER loses its collider"
    )

    # P2: promotion is gated on the MEASURED displacement, and vegetation is exempt from it for
    # the same reason it is exempt from the switch rule (a lamp-post LOD measures 0.06 m and is
    # visually total, A6.7).
    check(shp.LOD0_MAX_ERROR_DEFAULT > 0, "LOD0 promotion has a default threshold")
    check(
        "tree" not in shp.DISPLACEMENT_RULE_KINDS,
        "vegetation is exempt from the displacement rule, so it never loses LOD 0",
    )


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

    bundle = real_bundle()
    if bundle:
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
    test_frame()
    test_decal_lift_and_the_quaternion()
    test_placement()
    test_shapes_and_instances()
    test_forest_path()
    test_region_and_toggles()
    test_lights()
    test_groundcover_default()
    test_real_bundle_lod()
    test_rotation_matrix_is_basis_vectors()
    test_switch_points_only_ever_defer()
    test_variant_index_matches_the_bake()
    test_a8_performance_rules()
    test_orphans_of_absent_owners()
    test_tree_vs_plant()
    test_road_clearance()
    test_color_palette()
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


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_objects.py`."""
    assert main() in (None, 0)
