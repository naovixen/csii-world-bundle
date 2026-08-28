"""Gate for the terrainFine raster, level half — the BeamNG terrain stage and the terraincut.

Split from `tools/test_terrain_fine.py` (refactor Phase 4): the reader/hole-mask/fidelity half
stays there; this file runs the cs2beamng terrain stage and the terraincut bake on copies of
the example bundle and checks the written `.ter`, the punched holes, the ground meshes in them
and the watertight seam. Run: `python tools/test_terrain_fine_stage.py`.
"""

import json
import os
import shutil
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")

sys.path.insert(0, HERE)
from csiibundle import clipgrid, png  # noqa: E402
from finecopy import make_fine_copy  # noqa: E402

CHECKS = 0
FAILS = []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def _ctx(settings, bundle, ldir, derived):
    from cs2beamng.stages import BuildContext

    return BuildContext(
        settings=settings,
        bundle=bundle,
        level_dir=ldir,
        derived_dir=derived,
        census={},
        region=None,
        report={},
        jobs=1,
        shared={},
    )


def test_beamng_stage(tmp):
    print("cs2beamng terrain stage on a fine copy")
    from cs2beamng.beamng import ter as ter_mod
    from cs2beamng.stages import terrain as terrain_stage
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Progress

    dst, _fine16, fine_cell = make_fine_copy(tmp)
    derived = os.path.join(tmp, "derived")
    bake_runner.run(dst, only=["terrainmask"], derived_dir=derived, progress=Progress())
    mask = json.load(open(os.path.join(derived, "terrain", "mask.json"), encoding="utf-8"))
    check(
        mask.get("fine") is True and mask["sampleSize"] == [64, 64],
        f"terrainmask bakes on the fine grid ({mask['sampleSize']}, fine={mask.get('fine')})",
    )
    ldir = os.path.join(tmp, "lvl")
    ctx = _ctx(
        {
            "level_name": "lvl",
            "level_title": "L",
            "terrain_holes": "contain",
            "terrain_upsample": 2,
        },
        dst,
        ldir,
        derived,
    )
    result = terrain_stage.run(ctx, Progress())
    check(
        result["size"] == 64
        and abs(result["squareSize"] - fine_cell) < 1e-9
        and result["fine"] is True,
        f"terrain stage wrote the fine grid, upsample skipped: {result}",
    )
    size, _heights, _layers, _names = ter_mod.read_ter(os.path.join(ldir, "lvl.ter"))
    check(size == 64, f".ter is {size}")
    (
        json.load(open(os.path.join(ldir, "lvl.terrain.json"), encoding="utf-8"))
        if os.path.isfile(os.path.join(ldir, "lvl.terrain.json"))
        else {}
    )
    shared = ctx.shared["terrain"]
    # position = south-west sample of the fine grid: origin_x, origin_z - (size-1)*cell
    check(
        abs(shared["position"][0] - (80.0 - 2.0 + 1.0)) < 1e-6
        and abs(shared["position"][1] - ((-300.0 + 2.0 - 1.0) - 63 * fine_cell)) < 1e-6,
        f"block position is the fine south-west sample {shared['position'][:2]}",
    )
    _check_base_grid_run(tmp, dst)
    shutil.rmtree(dst, ignore_errors=True)


def _check_base_grid_run(tmp, dst):
    """terrain_upsample 1 keeps the 4096-class base grid even when the bundle carries the fine
    map (8192 hung BeamNG on load #11), and the bake's ground option follows it."""
    from cs2beamng.stages import terrain as terrain_stage
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Progress

    derived_b = os.path.join(tmp, "derived_base")
    bake_runner.run(
        dst,
        only=["terrainmask"],
        derived_dir=derived_b,
        options={"ground": "base"},
        progress=Progress(),
    )
    mask_b = json.load(open(os.path.join(derived_b, "terrain", "mask.json"), encoding="utf-8"))
    ldir_b = os.path.join(tmp, "lvl_base")
    ctx_b = _ctx(
        {
            "level_name": "lvlb",
            "level_title": "L",
            "terrain_holes": "contain",
            "terrain_upsample": 1,
        },
        dst,
        ldir_b,
        derived_b,
    )
    result_b = terrain_stage.run(ctx_b, Progress())
    check(
        mask_b.get("fine") is False
        and mask_b["sampleSize"] == [32, 32]
        and result_b["size"] == 32
        and result_b["fine"] is False,
        f"terrain_upsample 1: base grid for both the bake ({mask_b['sampleSize']}) "
        f"and the .ter ({result_b['size']})",
    )


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
                    continue  # a sliver nobody sees
                cc = (tri[:, 0] - origin_x) / cell
                rr = (origin_z - tri[:, 1]) / cell
                for r in range(
                    max(0, int(numpy.floor(rr.min()))),
                    min(shape[0] - 1, int(numpy.floor(rr.max()))) + 1,
                ):
                    for c in range(
                        max(0, int(numpy.floor(cc.min()))),
                        min(shape[1] - 1, int(numpy.floor(cc.max()))) + 1,
                    ):
                        if covered[r, c]:
                            continue
                        x0 = origin_x + c * cell
                        z1 = origin_z - r * cell
                        if _tri_rect_overlap(tri, x0, x0 + cell, z1 - cell, z1):
                            covered[r, c] = True
    return covered, total


def _bake_terraincut(tmp):
    """Bake terrainmask + terraincut on the example bundle; check punch and mesh coverage."""
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Progress

    derived = os.path.join(tmp, "derived_tc")
    out = bake_runner.run(
        EXAMPLE,
        only=["terrainmask", "terraincut"],
        derived_dir=derived,
        options={"ground": "base"},
        progress=Progress(),
    )
    check(
        all(o.status == "baked" for o in out),
        f"terrainmask + terraincut baked ({[o.status for o in out]})",
    )
    corr = png.gray_array(os.path.join(derived, "terrain", "corridor_faces.png")) > 0
    hm = clipgrid.hole_mask(EXAMPLE, prefer_fine=False)
    check(
        corr.shape == hm.contain.shape and corr[hm.contain | hm.deep_touch].all(),
        "every face a deep band touches is punched",
    )
    tile_dir = os.path.join(derived, "terraincut")
    tiles = [n for n in os.listdir(tile_dir) if n.endswith(".glb")]
    covered, tri_count = _mesh_face_coverage(
        tile_dir, corr.shape, hm.cell, hm.origin_x, hm.origin_z
    )
    sky = corr & ~covered & ~hm.contain
    check(
        bool(tiles) and not sky.any(),
        f"{len(tiles)} tile(s), {tri_count:,} triangles: no punched face is left without ground "
        f"({int(hm.contain.sum())} contained faces are the harness's own holes, {int(sky.sum())} sky)",
    )
    return derived, corr, hm, tile_dir, tiles


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
    from cs2beamng.beamng import ter as ter_mod
    from cs2beamng.stages import terrain as terrain_stage
    from csiibundle.bake.progress import Progress

    derived, corr, hm, tile_dir, tiles = _bake_terraincut(tmp)
    ldir = os.path.join(tmp, "lvl_tc")
    ctx = _ctx(
        {
            "level_name": "tc",
            "level_title": "T",
            "terrain_holes": "contain",
            "terrain_upsample": 1,
            "terrain_cut": True,
            "markings": False,
            "areas": False,
            "skirts": False,
            "lane_furniture": False,
            "collision_preset": "simplified",
            "tile_size": 256,
        },
        EXAMPLE,
        ldir,
        derived,
    )
    result = terrain_stage.run(ctx, Progress())
    _size, _h, layers, _names = ter_mod.read_ter(os.path.join(ldir, "tc.ter"))
    holes = layers == ter_mod.HOLE
    # bundle face (r, c) -> .ter square at bundle sample (r+1, c) -> after the south-first flip row (H-1) - (r+1)
    H = layers.shape[0]
    rr, cc = numpy.nonzero(corr)
    check(
        result["holes"] >= int(corr.sum()) and holes[H - 1 - (rr + 1), cc].all(),
        "the .ter punches every corridor face",
    )
    _check_seam(tile_dir, tiles, corr, hm, _h, H)
    _check_derived_stage(ctx, ldir)


def _check_seam(tile_dir, tiles, corr, hm, _h, size):
    """The seam. A sample shared by a punched and a kept square is where the mesh has to meet
    the heightfield exactly; the mesh's boundary vertices are those samples by construction
    (`carriageway.ground_heights`, quantised to the elevation encoding), so equality is EXACT."""
    rr, cc = numpy.nonzero(corr)
    punched = numpy.zeros((size, size), dtype=bool)  # over samples, bundle order
    kept = numpy.zeros((size, size), dtype=bool)
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
            continue  # a hole meeting kept ground: no vertex is owed here
        met += 1
        # 1 mm, not zero: a glTF stores positions as float32, so a height around 600 m comes back
        # with ~3e-5 m of representation noise. The quantisation itself is exact (both sides round
        # the same float32 value in float64); anything at the elevation quantum (6 cm here) is not.
        if min(abs(g - ter_h[size - 1 - r, c]) for g in got) > 1e-3:
            wrong += 1
    check(
        len(shared[0]) and met and not wrong,
        f"watertight seam: every one of the {met} sample(s) where the mesh reaches kept ground sits at "
        f"the .ter's own height within a millimetre ({wrong} disagree; {len(shared[0]) - met} of "
        f"{len(shared[0])} shared samples are a hole's edge, where none is owed)",
    )


def _check_derived_stage(ctx, ldir):
    """The derived stage places the tiles with collision and the ground-mesh material binds."""
    from cs2beamng.stages import derived as derived_stage
    from csiibundle.bake.progress import Progress

    dres = derived_stage.run(ctx, Progress())
    tc = (dres.get("products") or {}).get("terraincut") or {}
    check(
        tc.get("tiles", 0) >= 1 and tc.get("collision") is True,
        f"derived stage placed the tiles with collision ({tc})",
    )
    dae_dir = os.path.join(ldir, "art", "shapes", "terraincut")
    text = "".join(
        open(os.path.join(dae_dir, n), encoding="utf-8").read(200000)
        for n in os.listdir(dae_dir)
        if n.endswith(".dae")
    )
    check("tc_ground_" in text, "tiles bind the level's ground mesh materials")
    # `art/ground/`, not `art/terrains/`: §49 moved the ground MESH materials out, because a plain
    # `Material` under `art/terrains/` is where no vanilla level puts one and BeamNG never bound it.
    mats = json.load(
        open(os.path.join(ldir, "art", "ground", "main.materials.json"), encoding="utf-8")
    )
    check(
        all(f"tc_ground_{g}" in mats for g in ("grass", "dirt", "rock")),
        "terrain stage writes the three ground mesh materials",
    )


def main():
    tmp = tempfile.mkdtemp(prefix="cs2_terrain_fine_stage_")
    try:
        test_beamng_stage(tmp)
        test_terraincut(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_terrain_fine_stage.py`."""
    assert main() in (None, 0)
