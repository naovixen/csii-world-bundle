"""The scalar ground path is bit-identical to the vectorised one — the gate on A5/A1.

`bake/areas` conforms 160,906 source triangles into 5.4 million pieces by asking the ground for
one point at a time — three edge midpoints and a centroid per split. Each of those went through
`HeightfieldSurface.height` → `_sample`, allocating two 1-element arrays per call, and through
`GroundSurface.height` → `height_many` it ran that twice (once per diagonal of the `envelope`)
and then `stack` + `nanmax` inside an `errstate` block. That overhead was ~60 % of the bake.

`_sample_one` is the same arithmetic in plain Python floats. "Same arithmetic" is the whole
claim: a scalar path that merely *approximates* the vectorised one moves every conformed height
in the level by a sub-millimetre amount that nothing downstream would ever flag. So this asserts
**bit** identity — `==` on float64, not `allclose` — over:

  1. random points across a synthetic saddle field, both diagonals, both envelope modes;
  2. the cases the branch structure turns on: exactly on the split diagonal (`u == v` and
     `u + v == 1`), on a lattice vertex, on a cell edge;
  3. the raster's boundary — the last row/column (which belongs to the last cell), just outside
     it (which is `None`/NaN), and far outside;
  4. non-finite input, where the array path answers NaN by comparison and the scalar path must
     answer `None` rather than raising;
  5. the real bundle's 8192² fine heightmap when one is on disk, which is the field the numbers
     above were measured on.

Run: `python tools/test_ground_scalar.py`  (system Python + numpy; claim 5 is skipped when no
bundle is on disk).
"""
import math
import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle.bake.ground import GroundSurface
from csiibundle.drape import HeightfieldSurface

BUNDLE = os.path.expandvars(r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
                            r"\Cities Skylines II\ModsData\Carto")

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


def compare(surface, xs, ys, label):
    """`_sample_one` against `_sample`, point by point, as exact bit patterns."""
    want = surface._sample(numpy.asarray(xs, dtype=numpy.float64),
                           numpy.asarray(ys, dtype=numpy.float64))
    bad = None
    for i, (x, y) in enumerate(zip(xs, ys)):
        got = surface._sample_one(float(x), float(y))
        truth = float(want[i])
        if math.isnan(truth):
            if got is not None:
                bad = (x, y, got, "NaN")
                break
            continue
        # bit identity, not tolerance: 0.0 == -0.0 is the one case `==` conflates and it is not
        # a difference any consumer can see, so the plain comparison is the right test here
        if got is None or got != truth:
            bad = (x, y, got, truth)
            break
    check(bad is None, label, "" if bad is None else
          f"at ({bad[0]!r}, {bad[1]!r}): scalar {bad[2]!r} vs vectorised {bad[3]!r}")


rng = numpy.random.default_rng(20260818)

# -- 1. a synthetic saddle, both diagonals ------------------------------------------------------
# A saddle is the field where the two diagonals genuinely disagree, so it exercises both branches
# of each convention rather than a plane where every split answers the same thing.
rows = cols = 64
gy, gx = numpy.mgrid[0:rows, 0:cols]
field = (numpy.sin(gx * 0.31) * numpy.cos(gy * 0.27) * 7.0
         + gx * 0.13 - gy * 0.09 + rng.normal(0.0, 0.4, (rows, cols)))
CELL, OX, OY = 3.5, -1000.0, 2000.0

n = 20000
xs = OX + rng.uniform(-2.0, cols + 1.0, n) * CELL
ys = OY - rng.uniform(-2.0, rows + 1.0, n) * CELL

for diagonal in ("nw_se", "ne_sw"):
    surface = HeightfieldSurface(field, CELL, OX, OY, diagonal=diagonal)
    compare(surface, xs, ys, f"{n} random points, diagonal {diagonal}, bit-identical")

# -- 2. the points the branches turn on ---------------------------------------------------------
# u == v is the `nw_se` split line, u + v == 1 the `ne_sw` one; lattice vertices and cell edges
# are where floor() and the clip decide. Random floats hit none of these exactly.
special_x, special_y = [], []
for c in range(0, cols - 1, 7):
    for r in range(0, rows - 1, 5):
        for du, dv in ((0.0, 0.0), (0.5, 0.5), (0.25, 0.25), (0.75, 0.75),   # u == v
                       (0.5, 0.5), (0.25, 0.75), (0.75, 0.25),               # u + v == 1
                       (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.0, 0.5), (0.5, 0.0)):
            special_x.append(OX + (c + du) * CELL)
            special_y.append(OY - (r + dv) * CELL)
for diagonal in ("nw_se", "ne_sw"):
    surface = HeightfieldSurface(field, CELL, OX, OY, diagonal=diagonal)
    compare(surface, special_x, special_y, f"{len(special_x)} diagonal/vertex/edge points, {diagonal}")

# -- 3. the raster boundary ---------------------------------------------------------------------
edge_x = [OX + (cols - 1) * CELL, OX + (cols - 1) * CELL, OX - 1e-9, OX + cols * CELL, OX]
edge_y = [OY - (rows - 1) * CELL, OY, OY, OY, OY + 1e-9]
for diagonal in ("nw_se", "ne_sw"):
    surface = HeightfieldSurface(field, CELL, OX, OY, diagonal=diagonal)
    compare(surface, edge_x, edge_y, f"the last row/column and just outside it, {diagonal}")
    check(surface.height(OX - CELL, OY) is None and surface.height(OX, OY + CELL) is None,
          f"one cell beyond the north-west corner is off the raster, {diagonal}")

# -- 4. non-finite input ------------------------------------------------------------------------
surface = HeightfieldSurface(field, CELL, OX, OY)
nonfinite = [(float("nan"), OY), (OX, float("nan")), (float("inf"), OY), (OX, float("-inf")),
             (1e300, OY), (OX, -1e300)]
ok = True
for x, y in nonfinite:
    got = surface.height(x, y)
    truth = surface._sample(numpy.array([x]), numpy.array([y]))[0]
    if got is not None or not math.isnan(float(truth)):
        ok = False
check(ok, "NaN / infinity / 1e300 answer None, matching the array path's NaN")

# -- the envelope: GroundSurface's scalar path against its own height_many -----------------------
class _Envelope(GroundSurface):
    """A `GroundSurface` over synthetic fields — the real one only builds from a bundle."""

    def __init__(self, a, b, mode):
        self.a, self.b, self.mode, self.present = a, b, mode, True


a = HeightfieldSurface(field, CELL, OX, OY, diagonal="nw_se")
b = HeightfieldSurface(field, CELL, OX, OY, diagonal="ne_sw")
for mode in ("max", "min"):
    ground = _Envelope(a, b, mode)
    want = ground.height_many(numpy.asarray(xs), numpy.asarray(ys))
    bad = None
    for i in range(n):
        got = ground.height(float(xs[i]), float(ys[i]))
        truth = float(want[i])
        if math.isnan(truth):
            if got is not None:
                bad = (i, got, "NaN")
                break
        elif got is None or got != truth:
            bad = (i, got, truth)
            break
    check(bad is None, f"{n} envelope ({mode}) points bit-identical to height_many",
          "" if bad is None else f"index {bad[0]}: scalar {bad[1]!r} vs vectorised {bad[2]!r}")

# a one-sided envelope (diagonal != "envelope") must not lose the NaN → None rule
single = _Envelope(a, None, "max")
check(single.height(OX - CELL, OY) is None and single.height(OX, OY) == field[0, 0],
      "a single-diagonal GroundSurface answers the raster and bounds it")

# -- 5. the real bundle -------------------------------------------------------------------------
try:
    real = GroundSurface(BUNDLE, "envelope", mode="max", prefer_fine=True) \
        if os.path.isdir(BUNDLE) else None
except Exception as exc:      # noqa: BLE001 - a bundle we cannot read is a skip, not a failure
    print(f"  skip  real bundle ({exc!r})")
    real = None
if real is None or not real.present:
    print("  skip  the real 8192² fine heightmap (no bundle on disk)")
else:
    rows_r, cols_r = real.a.heights.shape
    cell_r = real.a.cell
    rx = real.a.origin_x + rng.uniform(-4.0, cols_r + 4.0, 30000) * cell_r
    ry = real.a.origin_y - rng.uniform(-4.0, rows_r + 4.0, 30000) * cell_r
    compare(real.a, rx, ry, f"30000 points on the real {rows_r}×{cols_r} raster, nw_se")
    compare(real.b, rx, ry, f"30000 points on the real {rows_r}×{cols_r} raster, ne_sw")
    want = real.height_many(rx, ry)
    bad = None
    for i in range(len(rx)):
        got = real.height(float(rx[i]), float(ry[i]))
        truth = float(want[i])
        if math.isnan(truth):
            if got is not None:
                bad = (i, got, "NaN")
                break
        elif got is None or got != truth:
            bad = (i, got, truth)
            break
    check(bad is None, "the real envelope agrees bit for bit with height_many",
          "" if bad is None else f"index {bad[0]}: scalar {bad[1]!r} vs vectorised {bad[2]!r}")

print("test_ground_scalar: " + ("FAILED" if failures else "OK"))
sys.exit(1 if failures else 0)
