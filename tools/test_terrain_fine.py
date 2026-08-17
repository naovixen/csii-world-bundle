"""Gate for the §8 `terrainFine` raster (2026-08-16) — the game's near-camera 1.75 m ground.

Run: `python tools/test_terrain_fine.py` (~1 min; the example bundle for the plumbing, the real
bundle for the fidelity numbers when it carries a fine map — otherwise those are reported as
"awaiting re-rip", not skipped silently).

Checks:
* `reader.load_heightfield`: an undeclared layer serves the base map (`fine` False); a declared
  one is served at its own cell/origin/size; a declared-but-missing file is a CONTRACT VIOLATION
  (never a fallback); `resample_nearest` maps a base raster onto the fine grid by position.
* `terrain.hole_mask` on a synthetic fine copy of the example bundle: face rasters at the fine
  size, and every base hole reappears (by world position) as fine holes — the clip volumes are
  read by world position, not by cell.
* the BeamNG terrain stage on that copy writes a `.ter` at the fine size and `squareSize`, with
  the block's `position` still the south-west sample and holes at the same world places.
* real bundle with a fine map: fine vs base agree away from the flattening footprint (median
  |d| within one quantum at 2:1 texel centres), and inside the shallow clip bands the fine
  ground stands above the carriageway on FEWER samples/area than the base — the poke-through
  the whole feature exists to remove — printed as before/after numbers.
"""

import json
import os
import shutil
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")
BUNDLE = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "Colossal Order",
                      "Cities Skylines II", "ModsData", "Carto")

from csiibundle import png, reader, terrain  # noqa: E402

CHECKS = 0
FAILS = []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def make_fine_copy(tmp, factor=2, jitter=None):
    """A copy of the example bundle with `terrain/heightmap_fine.png` = the base map upsampled
    bilinearly by `factor` (texel centres at the quarter points, like the game's cascade) and the
    manifest declaring `layers.terrainFine` + `rasters.terrainFine`."""
    dst = os.path.join(tmp, "bundle")
    shutil.copytree(EXAMPLE, dst)
    manifest = json.load(open(os.path.join(dst, "bundle.json"), encoding="utf-8"))
    rasters = manifest["rasters"]
    base = png.gray_array(os.path.join(dst, "terrain", "heightmap.png")).astype(numpy.float64)
    h, w = base.shape
    cell = float(rasters["resolution"])
    fine_cell = cell / factor
    # fine texel (i, j) centre = base origin - cell/2 + (i + 0.5) * fine_cell -> bilinear sample of the base
    fy = (numpy.arange(h * factor) + 0.5) / factor - 0.5
    fx = (numpy.arange(w * factor) + 0.5) / factor - 0.5
    y0 = numpy.clip(numpy.floor(fy).astype(int), 0, h - 1); y1 = numpy.clip(y0 + 1, 0, h - 1); ty = numpy.clip(fy - y0, 0, 1)
    x0 = numpy.clip(numpy.floor(fx).astype(int), 0, w - 1); x1 = numpy.clip(x0 + 1, 0, w - 1); tx = numpy.clip(fx - x0, 0, 1)
    top = base[y0][:, x0] * (1 - tx)[None, :] + base[y0][:, x1] * tx[None, :]
    bot = base[y1][:, x0] * (1 - tx)[None, :] + base[y1][:, x1] * tx[None, :]
    fine = top * (1 - ty)[:, None] + bot * ty[:, None]
    if jitter is not None:
        fine = fine + jitter
    fine16 = numpy.ascontiguousarray(numpy.clip(numpy.rint(fine), 0, 65535).astype(numpy.uint16))
    png.write_gray(os.path.join(dst, "terrain", "heightmap_fine.png"), fine16, bit_depth=16)
    ox, _, oz = rasters["originWorld"]
    manifest["layers"]["terrainFine"] = "terrain/heightmap_fine.png"
    rasters["terrainFine"] = {"file": "terrain/heightmap_fine.png", "size": [w * factor, h * factor],
                              "resolution": [fine_cell, fine_cell],
                              "originWorld": [ox - cell / 2 + fine_cell / 2, 0.0, oz + cell / 2 - fine_cell / 2],
                              "semantics": "test: bilinear upsample of the base map"}
    json.dump(manifest, open(os.path.join(dst, "bundle.json"), "w", encoding="utf-8"), indent=1)
    return dst, fine16, fine_cell


def test_loader(tmp):
    print("reader.load_heightfield")
    base = reader.load_heightfield(EXAMPLE)
    check(base is not None and base.fine is False and base.cell == 4.0 and base.raw.shape == (32, 32),
          f"undeclared -> base map ({base and base.raw.shape} @ {base and base.cell} m, fine={base and base.fine})")
    dst, fine16, fine_cell = make_fine_copy(tmp)
    f = reader.load_heightfield(dst)
    check(f.fine and f.cell == fine_cell and f.raw.shape == (64, 64) and numpy.array_equal(f.raw, fine16),
          f"declared -> fine map ({f.raw.shape} @ {f.cell} m)")
    check(abs(f.origin_x - (80.0 - 2.0 + 1.0)) < 1e-9 and abs(f.origin_z - (-300.0 + 2.0 - 1.0)) < 1e-9,
          f"fine origin is the fine texel centre ({f.origin_x}, {f.origin_z})")
    b = reader.load_heightfield(dst, prefer_fine=False)
    check(not b.fine and b.raw.shape == (32, 32), "prefer_fine=False still serves the base map")
    os.remove(os.path.join(dst, "terrain", "heightmap_fine.png"))
    try:
        reader.load_heightfield(dst)
        check(False, "declared but missing file raises")
    except RuntimeError as exc:
        check("CONTRACT VIOLATION" in str(exc), f"declared but missing file raises: {str(exc)[:60]}")
    # resample_nearest: a base raster onto the fine grid, by position
    ids = numpy.arange(32 * 32, dtype=numpy.uint8).reshape(32, 32)
    up = reader.resample_nearest(ids, (64, 64))
    check(up.shape == (64, 64) and all(numpy.array_equal(up[a::2, b::2], ids) for a in (0, 1) for b in (0, 1)),
          "resample_nearest doubles each base cell onto the fine grid (all four sub-cells)")
    shutil.rmtree(dst, ignore_errors=True)


def test_hole_mask(tmp):
    print("terrain.hole_mask on a fine copy")
    dst, _fine16, fine_cell = make_fine_copy(tmp)
    hm_base = terrain.hole_mask(EXAMPLE)
    hm_fine = terrain.hole_mask(dst)
    check(hm_fine.fine and hm_fine.cell == fine_cell and hm_fine.contain.shape == (63, 63)
          and hm_base.contain.shape == (31, 31), f"fine mask at fine face size {hm_fine.contain.shape}, cell {hm_fine.cell}")
    check(terrain.contain_samples(3.5) == 8 and terrain.contain_samples(1.75) == 4 and terrain.contain_samples(4.0) == 9,
          "sub-sample count keeps the 0.4375 m spacing (8 @ 3.5 m, 4 @ 1.75 m)")
    # every base hole is covered by fine holes at the same place: base face (r, c) spans fine faces
    # (2r-? ...) — compare by world position of face centres instead
    if hm_base.contain.any():
        rr, cc = numpy.nonzero(hm_base.contain)
        cx = hm_base.origin_x + (cc + 0.5) * hm_base.cell
        cz = hm_base.origin_z - (rr + 0.5) * hm_base.cell
        fc = ((cx - hm_fine.origin_x) / hm_fine.cell).astype(int)
        fr = ((hm_fine.origin_z - cz) / hm_fine.cell).astype(int)
        fc = numpy.clip(fc, 0, hm_fine.contain.shape[1] - 1)
        fr = numpy.clip(fr, 0, hm_fine.contain.shape[0] - 1)
        hit = hm_fine.contain[fr, fc] | hm_fine.deep_touch[fr, fc]
        check(hit.mean() >= 0.9, f"{hit.mean() * 100:.0f}% of base holes are deep-band faces on the fine grid too")
    else:
        print("        (example bundle has no contained faces; position check not exercised)")
    check(numpy.isfinite(hm_fine.band_ceiling).sum() >= numpy.isfinite(hm_base.band_ceiling).sum(),
          f"shallow-band samples scale with the grid ({int(numpy.isfinite(hm_fine.band_ceiling).sum())} vs {int(numpy.isfinite(hm_base.band_ceiling).sum())})")
    shutil.rmtree(dst, ignore_errors=True)


def test_beamng_stage(tmp):
    print("cs2beamng terrain stage on a fine copy")
    from cs2beamng.stages import terrain as terrain_stage, BuildContext
    from cs2beamng.beamng import ter as ter_mod
    from csiibundle.bake.progress import Progress
    from csiibundle.bake import runner as bake_runner
    dst, _fine16, fine_cell = make_fine_copy(tmp)
    derived = os.path.join(tmp, "derived")
    bake_runner.run(dst, only=["terrainmask"], derived_dir=derived, progress=Progress())
    mask = json.load(open(os.path.join(derived, "terrain", "mask.json"), encoding="utf-8"))
    check(mask.get("fine") is True and mask["sampleSize"] == [64, 64], f"terrainmask bakes on the fine grid ({mask['sampleSize']}, fine={mask.get('fine')})")
    ldir = os.path.join(tmp, "lvl")
    ctx = BuildContext(settings={"level_name": "lvl", "level_title": "L", "terrain_holes": "contain", "terrain_upsample": 2},
                       bundle=dst, level_dir=ldir, derived_dir=derived, census={}, region=None, report={}, jobs=1, shared={})
    result = terrain_stage.run(ctx, Progress())
    check(result["size"] == 64 and abs(result["squareSize"] - fine_cell) < 1e-9 and result["fine"] is True,
          f"terrain stage wrote the fine grid, upsample skipped: {result}")
    size, heights, layers, names = ter_mod.read_ter(os.path.join(ldir, "lvl.ter"))
    check(size == 64, f".ter is {size}")
    block = json.load(open(os.path.join(ldir, "lvl.terrain.json"), encoding="utf-8")) if os.path.isfile(os.path.join(ldir, "lvl.terrain.json")) else {}
    shared = ctx.shared["terrain"]
    # position = south-west sample of the fine grid: origin_x, origin_z - (size-1)*cell
    check(abs(shared["position"][0] - (80.0 - 2.0 + 1.0)) < 1e-6 and abs(shared["position"][1] - ((-300.0 + 2.0 - 1.0) - 63 * fine_cell)) < 1e-6,
          f"block position is the fine south-west sample {shared['position'][:2]}")
    # terrain_upsample 1 keeps the 4096-class base grid even when the bundle carries the fine map
    # (8192 hung BeamNG on load #11), and the bake's ground option follows it
    derived_b = os.path.join(tmp, "derived_base")
    bake_runner.run(dst, only=["terrainmask"], derived_dir=derived_b, options={"ground": "base"}, progress=Progress())
    mask_b = json.load(open(os.path.join(derived_b, "terrain", "mask.json"), encoding="utf-8"))
    ldir_b = os.path.join(tmp, "lvl_base")
    ctx_b = BuildContext(settings={"level_name": "lvlb", "level_title": "L", "terrain_holes": "contain", "terrain_upsample": 1},
                         bundle=dst, level_dir=ldir_b, derived_dir=derived_b, census={}, region=None, report={}, jobs=1, shared={})
    result_b = terrain_stage.run(ctx_b, Progress())
    check(mask_b.get("fine") is False and mask_b["sampleSize"] == [32, 32] and result_b["size"] == 32 and result_b["fine"] is False,
          f"terrain_upsample 1: base grid for both the bake ({mask_b['sampleSize']}) and the .ter ({result_b['size']})")
    shutil.rmtree(dst, ignore_errors=True)


def _tri_rect_overlap(tri, x0, x1, z0, z1):
    """SAT: does triangle `tri` ((3, 2) in x/z) overlap the axis-aligned rectangle?"""
    axes = [(1.0, 0.0), (0.0, 1.0)]
    for k in range(3):
        ax, az = tri[(k + 1) % 3] - tri[k]
        axes.append((-az, ax))
    rect = numpy.array([(x0, z0), (x1, z0), (x1, z1), (x0, z1)])
    for ax, az in axes:
        n = float(numpy.hypot(ax, az))
        if n < 1e-12:
            continue
        a, b = ax / n, az / n
        t = tri[:, 0] * a + tri[:, 1] * b
        r = rect[:, 0] * a + rect[:, 1] * b
        if t.max() < r.min() - 1e-9 or r.max() < t.min() - 1e-9:
            return False
    return True


def _mesh_face_coverage(tile_dir, shape, cell, origin_x, origin_z):
    """`(covered (h-1, w-1) bool, triangles)` — which heightfield faces the terraincut tiles'
    triangles really overlap. Face centres are not enough: the exact cut leaves pieces whose
    centroid can sit outside the square they came from."""
    from csiibundle import glb
    covered = numpy.zeros(shape, dtype=bool)
    total = 0
    for name in sorted(n for n in os.listdir(tile_dir) if n.endswith(".glb")):
        for node in glb.parse_glb(os.path.join(tile_dir, name)):
            v = numpy.asarray(node["verts"], dtype=numpy.float64)
            m = numpy.asarray(node["matrix"])
            v = v @ m[:3, :3].T + m[:3, 3]
            # glTF frame -> bundle frame (the §2.2 mirror): bundle x = -gx, bundle z = gz
            p = numpy.stack((-v[:, 0], v[:, 2]), axis=-1)
            corners = p[numpy.asarray(node["tris"])]
            total += len(corners)
            for tri in corners:
                e1, e2 = tri[1] - tri[0], tri[2] - tri[0]
                if abs(e1[0] * e2[1] - e1[1] * e2[0]) * 0.5 < 1e-4:
                    continue                       # a sliver nobody sees
                cc = (tri[:, 0] - origin_x) / cell
                rr = (origin_z - tri[:, 1]) / cell
                for r in range(max(0, int(numpy.floor(rr.min()))), min(shape[0] - 1, int(numpy.floor(rr.max()))) + 1):
                    for c in range(max(0, int(numpy.floor(cc.min()))), min(shape[1] - 1, int(numpy.floor(cc.max()))) + 1):
                        if covered[r, c]:
                            continue
                        x0 = origin_x + c * cell
                        z1 = origin_z - r * cell
                        if _tri_rect_overlap(tri, x0, x0 + cell, z1 - cell, z1):
                            covered[r, c] = True
    return covered, total


def test_terraincut(tmp):
    """The harness's cut lattice as ground meshes in heightfield holes. Three invariants, all
    measured against the real triangles rather than face centres:

    * every face a DEEP band touches is punched (the heightfield may not draw a cut face);
    * every punched face is either meshed or CONTAINED — a contained face is deleted outright,
      which is the hole the harness leaves too; anything else punched and unmeshed is sky;
    * the seam is watertight: wherever the mesh reaches a sample that a KEPT square also uses,
      it is there at exactly the `.ter` height. A shared sample with no mesh vertex is not a
      crack — it is a hole meeting kept ground, which is what the harness draws too; a crack
      would need a kept square with a snapped corner, and a face with a snapped corner is
      always meshed and punched.
    """
    print("terraincut (the harness cut in heightfield holes)")
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Progress
    from csiibundle import glb
    from cs2beamng.stages import terrain as terrain_stage, derived as derived_stage, BuildContext
    from cs2beamng.beamng import ter as ter_mod
    derived = os.path.join(tmp, "derived_tc")
    out = bake_runner.run(EXAMPLE, only=["terrainmask", "terraincut"], derived_dir=derived, options={"ground": "base"}, progress=Progress())
    check(all(o.status == "baked" for o in out), f"terrainmask + terraincut baked ({[o.status for o in out]})")
    corr = png.gray_array(os.path.join(derived, "terrain", "corridor_faces.png")) > 0
    hm = terrain.hole_mask(EXAMPLE, prefer_fine=False)
    check(corr.shape == hm.contain.shape and corr[hm.contain | hm.deep_touch].all(),
          "every face a deep band touches is punched")
    tile_dir = os.path.join(derived, "terraincut")
    tiles = [n for n in os.listdir(tile_dir) if n.endswith(".glb")]
    covered, tri_count = _mesh_face_coverage(tile_dir, corr.shape, hm.cell, hm.origin_x, hm.origin_z)
    sky = corr & ~covered & ~hm.contain
    check(bool(tiles) and not sky.any(),
          f"{len(tiles)} tile(s), {tri_count:,} triangles: no punched face is left without ground "
          f"({int(hm.contain.sum())} contained faces are the harness's own holes, {int(sky.sum())} sky)")
    ldir = os.path.join(tmp, "lvl_tc")
    ctx = BuildContext(settings={"level_name": "tc", "level_title": "T", "terrain_holes": "contain", "terrain_upsample": 1, "terrain_cut": True,
                                 "markings": False, "areas": False, "skirts": False, "lane_furniture": False, "collision_preset": "simplified", "tile_size": 256},
                       bundle=EXAMPLE, level_dir=ldir, derived_dir=derived, census={}, region=None, report={}, jobs=1, shared={})
    result = terrain_stage.run(ctx, Progress())
    _size, _h, layers, _names = ter_mod.read_ter(os.path.join(ldir, "tc.ter"))
    holes = (layers == ter_mod.HOLE)
    # bundle face (r, c) -> .ter square at bundle sample (r+1, c) -> after the south-first flip row (H-1) - (r+1)
    H = layers.shape[0]
    rr, cc = numpy.nonzero(corr)
    check(result["holes"] >= int(corr.sum()) and holes[H - 1 - (rr + 1), cc].all(), "the .ter punches every corridor face")
    # -- the seam. A sample shared by a punched and a kept square is where the mesh has to meet
    # the heightfield exactly; the mesh's boundary vertices are those samples by construction
    # (`terrain.ground_heights`, quantised to the elevation encoding), so equality is EXACT.
    punched = numpy.zeros((H, H), dtype=bool)          # over samples, bundle order
    kept = numpy.zeros((H, H), dtype=bool)
    for dr, dc in ((0, 0), (1, 0), (0, 1), (1, 1)):
        punched[rr + dr, cc + dc] = True
    kr, kc = numpy.nonzero(~corr)
    for dr, dc in ((0, 0), (1, 0), (0, 1), (1, 1)):
        kept[kr + dr, kc + dc] = True
    shared = numpy.nonzero(punched & kept)
    ter_h = _h.astype(numpy.float64) * hm.elevation_scale + hm.elevation_offset
    verts = {}
    from csiibundle import glb as _glb
    for name in tiles:
        for node in _glb.parse_glb(os.path.join(tile_dir, name)):
            v = numpy.asarray(node["verts"], dtype=numpy.float64)
            m = numpy.asarray(node["matrix"])
            v = v @ m[:3, :3].T + m[:3, 3]
            for x, y, z in numpy.stack((-v[:, 0], v[:, 1], v[:, 2]), axis=-1):
                verts.setdefault((round(float(x), 3), round(float(z), 3)), []).append(float(y))
    met = wrong = 0
    for r, c in zip(*shared):
        x = hm.origin_x + c * hm.cell
        z = hm.origin_z - r * hm.cell
        got = verts.get((round(float(x), 3), round(float(z), 3)))
        if got is None:
            continue                    # a hole meeting kept ground: no vertex is owed here
        met += 1
        # 1 mm, not zero: a glTF stores positions as float32, so a height around 600 m comes back
        # with ~3e-5 m of representation noise. The quantisation itself is exact (both sides round
        # the same float32 value in float64); anything at the elevation quantum (6 cm here) is not.
        if min(abs(g - ter_h[H - 1 - r, c]) for g in got) > 1e-3:
            wrong += 1
    check(len(shared[0]) and met and not wrong,
          f"watertight seam: every one of the {met} sample(s) where the mesh reaches kept ground sits at "
          f"the .ter's own height within a millimetre ({wrong} disagree; {len(shared[0]) - met} of "
          f"{len(shared[0])} shared samples are a hole's edge, where none is owed)")
    dres = derived_stage.run(ctx, Progress())
    tc = (dres.get("products") or {}).get("terraincut") or {}
    check(tc.get("tiles", 0) >= 1 and tc.get("collision") is True, f"derived stage placed the tiles with collision ({tc})")
    dae_dir = os.path.join(ldir, "art", "shapes", "terraincut")
    text = "".join(open(os.path.join(dae_dir, n), encoding="utf-8").read(200000) for n in os.listdir(dae_dir) if n.endswith(".dae"))
    check("tc_ground_" in text, "tiles bind the level's ground mesh materials")
    # `art/ground/`, not `art/terrains/`: §49 moved the ground MESH materials out, because a plain
    # `Material` under `art/terrains/` is where no vanilla level puts one and BeamNG never bound it.
    mats = json.load(open(os.path.join(ldir, "art", "ground", "main.materials.json"), encoding="utf-8"))
    check(all(f"tc_ground_{g}" in mats for g in ("grass", "dirt", "rock")), "terrain stage writes the three ground mesh materials")


def test_real_fidelity():
    print("real bundle: fine vs base")
    if not os.path.isdir(BUNDLE):
        print("  (real bundle absent)")
        return
    paths = reader.terrain_paths(BUNDLE)
    if paths.heightmap_fine is None:
        print("  AWAITING RE-RIP: this bundle carries no layers.terrainFine (producer built 2026-08-16); the fidelity numbers "
              "below need a rip with the terrain stage on")
        return
    fine = reader.load_heightfield(BUNDLE)
    base = reader.load_heightfield(BUNDLE, prefer_fine=False)
    ratio = fine.raw.shape[0] // base.raw.shape[0]
    check(ratio >= 2 and fine.raw.shape[0] == ratio * base.raw.shape[0], f"fine is {ratio}x the base grid")
    q = base.elevation_scale
    hf = fine.raw.astype(numpy.float64) * fine.elevation_scale + fine.elevation_offset
    hb = base.raw.astype(numpy.float64) * base.elevation_scale + base.elevation_offset
    # base sample (r, c) centre lies between fine texels; compare with the mean of the 2x2 block
    hf_block = hf.reshape(base.raw.shape[0], ratio, base.raw.shape[1], ratio).mean(axis=(1, 3))
    d = hf_block - hb
    surface = png.gray_array(paths.surface_index) if paths.surface_index and os.path.isfile(paths.surface_index) else None
    off = numpy.ones(hb.shape, bool) if surface is None else (surface == 0)
    # dilate the roadbed by two base cells so the smoothing apron counts as "near roads"
    if surface is not None:
        near = terrain.dilate(surface != 0, 3)
        off = ~near
    med_off = float(numpy.median(numpy.abs(d[off])))
    p99_off = float(numpy.percentile(numpy.abs(d[off]), 99))
    check(med_off <= 2 * q + 1e-9, f"away from roads the fine map is the base map: median |d| {med_off:.4f} m (quantum {q:.4f}), p99 {p99_off:.3f} m")
    print(f"        near roads: median |d| {float(numpy.median(numpy.abs(d[~off]))):.3f} m, p99 {float(numpy.percentile(numpy.abs(d[~off]), 99)):.3f} m")
    # The over-carriageway residual: samples standing above the game's own road cross-section
    # (audit §51). These are the ONLY ones `clamp_carriageway` moves, and the finer grid should
    # resolve the grading better and so leave fewer of them.
    hm_b = terrain.hole_mask(BUNDLE, prefer_fine=False)
    hm_f = terrain.hole_mask(BUNDLE, prefer_fine=True)
    for label, hm in (("base", hm_b), ("fine", hm_f)):
        has = numpy.isfinite(hm.band_ceiling)
        over = has & (hm.heights > hm.band_ceiling)
        print(f"        {label}: {int(has.sum()):,} samples under a carriageway, {int(over.sum()):,} stand over it "
              f"({over.sum() * hm.cell ** 2:,.0f} m2, {over.sum() / max(1, has.sum()) * 100:.2f}%)")
    frac_b = (numpy.isfinite(hm_b.band_ceiling) & (hm_b.heights > hm_b.band_ceiling)).sum() / max(1, numpy.isfinite(hm_b.band_ceiling).sum())
    frac_f = (numpy.isfinite(hm_f.band_ceiling) & (hm_f.heights > hm_f.band_ceiling)).sum() / max(1, numpy.isfinite(hm_f.band_ceiling).sum())
    check(frac_f <= frac_b + 1e-9,
          f"over-carriageway residual shrinks on the finer grid: {frac_b * 100:.2f}% -> {frac_f * 100:.2f}%")


def main():
    tmp = tempfile.mkdtemp(prefix="cs2_terrain_fine_")
    try:
        test_loader(tmp)
        test_hole_mask(tmp)
        test_beamng_stage(tmp)
        test_terraincut(tmp)
        test_real_fidelity()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
