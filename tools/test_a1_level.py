"""cs2beamng A1 gate — the BeamNG format layer and the terrain/water/scene stages.

Run: `python tools/test_a1_level.py` (needs `pip install -e .`; ~10 s; example bundle only,
plus the real bundle's ground check when `%USERPROFILE%\\...\\ModsData\\Carto` is present).

Checks: `.ter` write/read is the identity and its orientation matches the settled contract
(row 0 south, `h = stored·maxHeight/65536 + position.z`, `sample_height` on a synthetic
ramp); `heading_matrix` reproduces vanilla small_island's spawn matrix; `Level` writes the
BeamNG tree with unique deterministic ids and re-reads it; the level validator catches an
injected duplicate id and a missing file; a headless build of the example bundle produces
TerrainBlock/LevelInfo/TimeOfDay/ScatterSky/SpawnSphere with 0 problems; the water band split
turns a synthetic descending river into several blocks and a sea into a plane; on the real
bundle the `.ter` height under buildings agrees to centimetres.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")

FAILS = []
CHECKS = 0


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def test_ter(tmp):
    from cs2beamng.beamng import ter

    print("ter")
    n = 64
    scale, offset = 0.05, 100.0
    # a ramp rising toward the NORTH (row 0 in bundle order is the highest) and toward the east
    rows = numpy.arange(n)[:, None]
    cols = numpy.arange(n)[None, :]
    heights_m = offset + scale * (1000 + (n - 1 - rows) * 10 + cols * 2)
    layers = numpy.zeros((n, n), dtype=numpy.uint8)
    layers[3, 5] = 1
    h16, l8, max_height, pos_z = ter.from_bundle_arrays(heights_m, layers, scale, offset)
    check(max_height == 65536 * scale and pos_z == offset, "maxHeight/position.z encode the bundle scale/offset")
    path = os.path.join(tmp, "t.ter")
    ter.write_ter(path, h16, l8, ["grass", "dirt"])
    size, h_back, l_back, names = ter.read_ter(path)
    check(size == n and numpy.array_equal(h_back, h16) and numpy.array_equal(l_back, l8) and names == ["grass", "dirt"],
          "write/read is the identity")
    # bundle row 0 (north) must be the LAST .ter row; bundle (3,5) -> ter (n-1-3, 5)
    check(l_back[n - 1 - 3, 5] == 1 and l_back[3, 5] == 0, "layers flip vertically (row 0 south)")
    cell = 3.5
    origin_x, origin_z = -100.0, 120.0                # bundle: top-left texel centre, row 0 north
    position = [origin_x, origin_z - (n - 1) * cell, pos_z]
    # bundle sample (r, c) sits at x = origin_x + c*cell, z = origin_z - r*cell; BeamNG y = z
    for r, c in ((0, 0), (n - 1, n - 1), (10, 20), (n - 1, 0)):
        x = origin_x + c * cell
        y = origin_z - r * cell
        h = ter.sample_height(h_back, max_height, position, cell, x, y)
        check(abs(h - heights_m[r, c]) < 1e-6, f"sample_height at bundle ({r},{c}) reproduces the height ({h:.3f})")
    # lossless: every stored value equals the bundle's own quantisation
    check(numpy.array_equal(h16[::-1], numpy.rint((heights_m - offset) / scale).astype("<u2")), "heights are lossless")


def test_frame():
    from cs2beamng.beamng import frame

    print("frame")
    check(frame.to_beamng(1.0, 2.0, 3.0) == (1.0, 3.0, 2.0), "bundle (x, up, north) -> beamng (x, north, up)")
    check(frame.to_bundle(*frame.to_beamng(1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0), "round trip")
    # vanilla small_island spawn_south: rotationMatrix [0.0825,0.9966,0,-0.9966,0.0825,0,0,0,1]
    m = frame.heading_matrix(0.996590674, 0.0825054049)
    want = [0.0825054049, 0.996590674, 0, -0.996590674, 0.0825054049, 0, 0, 0, 1]
    check(all(abs(a - b) < 1e-6 for a, b in zip(m, want)), "heading_matrix reproduces the vanilla spawn matrix")
    m = frame.heading_matrix(0.0, 1.0)
    check(m == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "facing north is the identity")


def test_level(tmp):
    from cs2beamng.beamng import level as level_mod

    print("level")
    d = os.path.join(tmp, "lvl")
    lv = level_mod.Level(d, "lvl", "Level")
    lv.add(["Level_objects"], {"name": "theLevelInfo", "class": "LevelInfo"})
    lv.add(["Level_objects", "water"], {"name": "w0", "class": "WaterBlock"}, key="water:0")
    lv.add(["PlayerDropPoints"], {"name": "spawn", "class": "SpawnSphere"})
    try:
        lv.add(["PlayerDropPoints"], {"name": "spawn", "class": "SpawnSphere"})
        check(False, "duplicate key rejected")
    except ValueError:
        check(True, "duplicate key rejected")
    n = lv.write_objects()
    objs = [o for _f, _n, o in level_mod.read_objects(d)]
    check(n == 3 and len(objs) == 3 + 1 + 3, f"tree written: {len(objs)} objects incl. MissionGroup + 3 groups")
    parents = {o["name"]: o.get("__parent") for o in objs}
    check(parents["water"] == "Level_objects" and parents["w0"] == "water" and parents["Level_objects"] == "MissionGroup",
          "__parent chain")
    ids = [o["persistentId"] for o in objs]
    check(len(set(ids)) == len(ids), "persistentIds unique")
    lv2 = level_mod.Level(os.path.join(tmp, "lvl2"), "lvl", "Level")
    check(lv2.pid("spawn") == lv.pid("spawn"), "persistentIds deterministic per level name")
    check(os.path.isfile(os.path.join(d, "main", "MissionGroup", "Level_objects", "water", "items.level.json")), "nested group directory")


def test_headless_example(tmp):
    print("headless example build (terrain/water/scene/validate)")
    out = os.path.join(tmp, "levels", "ex1")
    env = dict(os.environ, CS2BEAMNG_HOME=os.path.join(tmp, "home"))
    proc = subprocess.run([sys.executable, "-m", "cs2beamng", "build", EXAMPLE, "--out", out, "--set", "jobs=1"],
                          capture_output=True, text=True, env=env, cwd=REPO)
    check(proc.returncode == 0, f"exit 0 ({proc.returncode})")
    if proc.returncode != 0:
        print(proc.stdout[-2000:], proc.stderr[-2000:])
        return None
    report = json.load(open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8"))
    classes = report["validation"]["checks"].get("classes", {})
    for cls in ("TerrainBlock", "LevelInfo", "TimeOfDay", "ScatterSky", "SpawnSphere"):
        check(classes.get(cls, 0) >= 1, f"{cls} present")
    check(report["validation"]["problems"] == [], f"validator: {report['validation']['problems'][:3]}")
    check(os.path.isfile(os.path.join(out, "ex1.ter")) and os.path.isfile(os.path.join(out, "ex1.terrain.json")), ".ter + sidecar")
    info = json.load(open(os.path.join(out, "info.json"), encoding="utf-8"))
    check(info.get("defaultSpawnPointName") == "spawn_city", "info.json names the spawn")
    return out


def test_validator_catches(level_dir):
    from cs2beamng.beamng import validate_level

    print("validator: injected faults")
    if not level_dir:
        return
    path = os.path.join(level_dir, "main", "MissionGroup", "PlayerDropPoints", "items.level.json")
    original = open(path, encoding="utf-8").read()
    try:
        # duplicate persistentId + a TSStatic pointing at a missing shape
        first = json.loads(original.splitlines()[0])
        dup = dict(first, name="spawn_dup")
        missing = {"name": "ghost", "class": "TSStatic", "persistentId": "00000000-0000-0000-0000-000000000001",
                   "__parent": "PlayerDropPoints", "shapeName": "/levels/ex1/art/shapes/nope.dae"}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dup) + "\n" + json.dumps(missing) + "\n")
        _checks, problems = validate_level.check(level_dir, EXAMPLE)
        check(any("duplicates" in p for p in problems), "duplicate persistentId reported")
        check(any("missing file" in p and "nope.dae" in p for p in problems), "missing shape file reported")
    finally:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original)


def test_water_bands(tmp):
    """A synthetic raster: a sea at 10 m along the west edge and a river descending from 40 m to
    12 m eastwards; the split must give one plane and several level-band blocks."""
    from cs2beamng.stages import water as water_stage, BuildContext
    from csiibundle.bake.progress import Progress
    from cs2beamng.beamng import level as level_mod

    print("water: band split on a synthetic river")
    n = 512
    cell = 3.5
    heights = numpy.full((n, n), 50.0)
    heights[:, :64] = 0.0                                   # sea floor on the west
    for c in range(64, n):                                  # a river channel across the middle, descending toward the west
        heights[250:262, c] = 5.0 + (c - 64) * (30.0 / (n - 64))
    levels = numpy.zeros((n, n))
    levels[:, :64] = 10.0
    for c in range(64, n):
        levels[250:262, c] = 12.0 + (c - 64) * (30.0 / (n - 64))
    scale, offset = 0.05, 0.0
    bdir = os.path.join(tmp, "wbundle")
    os.makedirs(os.path.join(bdir, "terrain"), exist_ok=True)
    from csiibundle import png
    with open(os.path.join(bdir, "terrain", "heightmap.png"), "wb") as f:
        f.write(png.encode_gray(numpy.rint(heights / scale).astype(numpy.uint16), bit_depth=16))
    with open(os.path.join(bdir, "terrain", "water_level.png"), "wb") as f:
        f.write(png.encode_gray(numpy.rint(levels / scale).astype(numpy.uint16), bit_depth=16))
    manifest = {"schema": 2, "layers": {"terrain": "terrain/heightmap.png", "water": "terrain/water_level.png"},
                "rasters": {"resolution": cell, "size": [n, n], "originWorld": [0.0, 0.0, n * cell], "elevationScale": scale,
                            "elevationOffset": offset, "water": {"waterColor": [0.05, 0.2, 0.3, 1.0], "absorptionDistance": 16}}}
    json.dump(manifest, open(os.path.join(bdir, "bundle.json"), "w"))
    ldir = os.path.join(tmp, "wlevel")
    ctx = BuildContext(settings={"level_name": "w", "level_title": "w", "water": True}, bundle=bdir, level_dir=ldir,
                       derived_dir=os.path.join(bdir, "derived"), census={}, region=None, report={}, jobs=1, shared={})
    result = water_stage.run(ctx, Progress())
    objs = [o for _f, _n, o in level_mod.read_objects(ldir)]
    planes = [o for o in objs if o["class"] == "WaterPlane"]
    blocks = [o for o in objs if o["class"] == "WaterBlock"]
    check(len(planes) == 1 and abs(planes[0]["position"][2] - 10.0) < 0.6, f"the sea is one WaterPlane at ~10 m ({planes and planes[0]['position']})")
    check(len(blocks) >= 8, f"the descending river split into level-band blocks ({len(blocks)})")
    tops = sorted(b["position"][2] for b in blocks)
    check(tops[0] < 16.0 and tops[-1] > 36.0, f"block levels span the river's descent ({tops[0]:.1f}..{tops[-1]:.1f})")
    check(all(b["scale"][2] >= 1.0 for b in blocks), "blocks extend below their surface")
    check(result["plane"] and result["blocks"] == len(blocks), "result reports the counts")


def test_real_ground():
    from cs2beamng.beamng import validate_level

    if not os.path.isdir(BUNDLE):
        print("real bundle: absent, ground check skipped")
        return
    print("real bundle: terrain stage + ground check")
    tmp = tempfile.mkdtemp(prefix="cs2beamng_a1_real_")
    try:
        from cs2beamng.stages import terrain as terrain_stage, BuildContext
        from csiibundle.bake.progress import Progress
        ldir = os.path.join(tmp, "real")
        derived = os.path.join(BUNDLE, "derived")
        # the terrain stage reads the terrainmask bake (clip cells, shallow/deep lane rasters):
        # bake that one product if the derived cache is absent or old (~50 s on a full city)
        from csiibundle.bake import runner as bake_runner
        bake_runner.run(BUNDLE, derived_dir=derived, only=["terrainmask"], progress=Progress())
        ctx = BuildContext(settings={"level_name": "real", "level_title": "real", "terrain_holes": "touch", "terrain_upsample": 1},
                           bundle=BUNDLE, level_dir=ldir, derived_dir=derived, census={}, region=None, report={}, jobs=1, shared={})
        result = terrain_stage.run(ctx, Progress())
        check(result["size"] == 4096 and result["materials"] >= 4, f"real terrain written: {result}")
        check(result["underRoad"] > 5_000 and result["ring"] > 10_000, f"ground under roads + ring applied: {result}")
        check(result["holes"] > 0 and result["wallQuads"] > 0 and result["wallTiles"] > 0, f"holes punched and rim walls written: {result}")
        wall_daes = [f for f in os.listdir(os.path.join(ldir, "art", "shapes", "holewalls")) if f.endswith(".dae")]
        check(len(wall_daes) == result["wallTiles"], "one holewalls .dae per wall tile")
        checks, problems = validate_level.check(ldir, BUNDLE)
        g = checks.get("groundCheck", {})
        check(g.get("sampled", 0) >= 500 and g.get("fraction", 0) >= 0.97 and g.get("medianAbs", 9) < 0.1,
              f"terrain under buildings: {g}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp(prefix="cs2beamng_a1_")
    try:
        test_ter(tmp)
        test_frame()
        test_level(tmp)
        out = test_headless_example(tmp)
        test_validator_catches(out)
        test_water_bands(tmp)
        test_real_ground()
    finally:
        shutil.rmtree(os.path.join(EXAMPLE, "derived"), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
