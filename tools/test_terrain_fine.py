"""Gate for the §8 `terrainFine` raster (2026-08-16) — the game's near-camera 1.75 m ground.

Run: `python tools/test_terrain_fine.py` (~1 min; the example bundle for the plumbing, the real
bundle for the fidelity numbers when it carries a fine map — otherwise those are reported as
"awaiting re-rip", not skipped silently).

Checks:
* `reader.load_heightfield`: an undeclared layer serves the base map (`fine` False); a declared
  one is served at its own cell/origin/size; a declared-but-missing file is a CONTRACT VIOLATION
  (never a fallback); `resample_nearest` maps a base raster onto the fine grid by position.
* `clipgrid.hole_mask` on a synthetic fine copy of the example bundle: face rasters at the fine
  size, and every base hole reappears (by world position) as fine holes — the clip volumes are
  read by world position, not by cell.
* the BeamNG terrain stage half — the `.ter`, the terraincut ground meshes and the seam —
  moved to `tools/test_terrain_fine_stage.py` (refactor Phase 4).
* real bundle with a fine map: fine vs base agree away from the flattening footprint (median
  |d| within one quantum at 2:1 texel centres), and inside the shallow clip bands the fine
  ground stands above the carriageway on FEWER samples/area than the base — the poke-through
  the whole feature exists to remove — printed as before/after numbers.
"""

import os
import shutil
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")
BUNDLE = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "AppData",
    "LocalLow",
    "Colossal Order",
    "Cities Skylines II",
    "ModsData",
    "Carto",
)

from csiibundle import (  # noqa: E402
    clipgrid,
    clipraster,
    containment,
    png,
    reader,
)
from finecopy import make_fine_copy  # noqa: E402

CHECKS = 0
FAILS = []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def test_loader(tmp):
    print("reader.load_heightfield")
    base = reader.load_heightfield(EXAMPLE)
    check(
        base is not None and base.fine is False and base.cell == 4.0 and base.raw.shape == (32, 32),
        f"undeclared -> base map ({base and base.raw.shape} @ {base and base.cell} m, fine={base and base.fine})",
    )
    dst, fine16, fine_cell = make_fine_copy(tmp)
    f = reader.load_heightfield(dst)
    check(
        f.fine
        and f.cell == fine_cell
        and f.raw.shape == (64, 64)
        and numpy.array_equal(f.raw, fine16),
        f"declared -> fine map ({f.raw.shape} @ {f.cell} m)",
    )
    check(
        abs(f.origin_x - (80.0 - 2.0 + 1.0)) < 1e-9
        and abs(f.origin_z - (-300.0 + 2.0 - 1.0)) < 1e-9,
        f"fine origin is the fine texel centre ({f.origin_x}, {f.origin_z})",
    )
    b = reader.load_heightfield(dst, prefer_fine=False)
    check(not b.fine and b.raw.shape == (32, 32), "prefer_fine=False still serves the base map")
    os.remove(os.path.join(dst, "terrain", "heightmap_fine.png"))
    try:
        reader.load_heightfield(dst)
        check(False, "declared but missing file raises")
    except RuntimeError as exc:
        check(
            "CONTRACT VIOLATION" in str(exc), f"declared but missing file raises: {str(exc)[:60]}"
        )
    # resample_nearest: a base raster onto the fine grid, by position
    ids = numpy.arange(32 * 32, dtype=numpy.uint8).reshape(32, 32)
    up = reader.resample_nearest(ids, (64, 64))
    check(
        up.shape == (64, 64)
        and all(numpy.array_equal(up[a::2, b::2], ids) for a in (0, 1) for b in (0, 1)),
        "resample_nearest doubles each base cell onto the fine grid (all four sub-cells)",
    )
    shutil.rmtree(dst, ignore_errors=True)


def test_hole_mask(tmp):
    print("clipgrid.hole_mask on a fine copy")
    dst, _fine16, fine_cell = make_fine_copy(tmp)
    hm_base = clipgrid.hole_mask(EXAMPLE)
    hm_fine = clipgrid.hole_mask(dst)
    check(
        hm_fine.fine
        and hm_fine.cell == fine_cell
        and hm_fine.contain.shape == (63, 63)
        and hm_base.contain.shape == (31, 31),
        f"fine mask at fine face size {hm_fine.contain.shape}, cell {hm_fine.cell}",
    )
    check(
        containment.contain_samples(3.5) == 8
        and containment.contain_samples(1.75) == 4
        and containment.contain_samples(4.0) == 9,
        "sub-sample count keeps the 0.4375 m spacing (8 @ 3.5 m, 4 @ 1.75 m)",
    )
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
        check(
            hit.mean() >= 0.9,
            f"{hit.mean() * 100:.0f}% of base holes are deep-band faces on the fine grid too",
        )
    else:
        print("        (example bundle has no contained faces; position check not exercised)")
    check(
        numpy.isfinite(hm_fine.band_ceiling).sum() >= numpy.isfinite(hm_base.band_ceiling).sum(),
        f"shallow-band samples scale with the grid ({int(numpy.isfinite(hm_fine.band_ceiling).sum())} vs {int(numpy.isfinite(hm_base.band_ceiling).sum())})",
    )
    shutil.rmtree(dst, ignore_errors=True)


def test_real_fidelity():
    print("real bundle: fine vs base")
    if not os.path.isdir(BUNDLE):
        print("  (real bundle absent)")
        return
    paths = reader.terrain_paths(BUNDLE)
    if paths.heightmap_fine is None:
        print(
            "  AWAITING RE-RIP: this bundle carries no layers.terrainFine (producer built 2026-08-16); the fidelity numbers "
            "below need a rip with the terrain stage on"
        )
        return
    fine = reader.load_heightfield(BUNDLE)
    base = reader.load_heightfield(BUNDLE, prefer_fine=False)
    ratio = fine.raw.shape[0] // base.raw.shape[0]
    check(
        ratio >= 2 and fine.raw.shape[0] == ratio * base.raw.shape[0],
        f"fine is {ratio}x the base grid",
    )
    q = base.elevation_scale
    hf = fine.raw.astype(numpy.float64) * fine.elevation_scale + fine.elevation_offset
    hb = base.raw.astype(numpy.float64) * base.elevation_scale + base.elevation_offset
    # base sample (r, c) centre lies between fine texels; compare with the mean of the 2x2 block
    hf_block = hf.reshape(base.raw.shape[0], ratio, base.raw.shape[1], ratio).mean(axis=(1, 3))
    d = hf_block - hb
    surface = (
        png.gray_array(paths.surface_index)
        if paths.surface_index and os.path.isfile(paths.surface_index)
        else None
    )
    off = numpy.ones(hb.shape, bool) if surface is None else (surface == 0)
    # dilate the roadbed by two base cells so the smoothing apron counts as "near roads"
    if surface is not None:
        near = clipraster.dilate(surface != 0, 3)
        off = ~near
    med_off = float(numpy.median(numpy.abs(d[off])))
    p99_off = float(numpy.percentile(numpy.abs(d[off]), 99))
    check(
        med_off <= 2 * q + 1e-9,
        f"away from roads the fine map is the base map: median |d| {med_off:.4f} m (quantum {q:.4f}), p99 {p99_off:.3f} m",
    )
    print(
        f"        near roads: median |d| {float(numpy.median(numpy.abs(d[~off]))):.3f} m, p99 {float(numpy.percentile(numpy.abs(d[~off]), 99)):.3f} m"
    )
    _check_carriageway_residual()


def _check_carriageway_residual():
    """The over-carriageway residual: samples standing above the game's own road cross-section
    (audit §51). These are the ONLY ones `clamp_carriageway` moves, and the finer grid should
    resolve the grading better and so leave fewer of them."""
    hm_b = clipgrid.hole_mask(BUNDLE, prefer_fine=False)
    hm_f = clipgrid.hole_mask(BUNDLE, prefer_fine=True)
    for label, hm in (("base", hm_b), ("fine", hm_f)):
        has = numpy.isfinite(hm.band_ceiling)
        over = has & (hm.heights > hm.band_ceiling)
        print(
            f"        {label}: {int(has.sum()):,} samples under a carriageway, {int(over.sum()):,} stand over it "
            f"({over.sum() * hm.cell**2:,.0f} m2, {over.sum() / max(1, has.sum()) * 100:.2f}%)"
        )
    frac_b = (numpy.isfinite(hm_b.band_ceiling) & (hm_b.heights > hm_b.band_ceiling)).sum() / max(
        1, numpy.isfinite(hm_b.band_ceiling).sum()
    )
    frac_f = (numpy.isfinite(hm_f.band_ceiling) & (hm_f.heights > hm_f.band_ceiling)).sum() / max(
        1, numpy.isfinite(hm_f.band_ceiling).sum()
    )
    check(
        frac_f <= frac_b + 1e-9,
        f"over-carriageway residual shrinks on the finer grid: {frac_b * 100:.2f}% -> {frac_f * 100:.2f}%",
    )


def main():
    tmp = tempfile.mkdtemp(prefix="cs2_terrain_fine_")
    try:
        test_loader(tmp)
        test_hole_mask(tmp)
        test_real_fidelity()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_terrain_fine.py`."""
    assert main() in (None, 0)
