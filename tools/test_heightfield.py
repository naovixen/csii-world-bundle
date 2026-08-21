"""`csiibundle.drape.HeightfieldSurface`: does the fallback ground query answer correctly?

This one needs its own test for a reason worth stating. Every other piece of `csiibundle` is
also on the Blender harness's path, so the A/B render gate exercises it whether a unit test does
or not. `HeightfieldSurface` is NOT — the harness keeps its BVH over the built mesh, and this
class exists for the consumer that has no ray-caster at all. Nothing else would ever run it.

Four claims, each scored against something that is not the implementation:

  1. **At a lattice vertex it returns that vertex's own height, exactly.** The raster is ground
     truth here and the answer is a lookup, so any interpolation error shows up as a mismatch.
  2. **On a synthetic tilted plane it is exact everywhere**, not just at vertices — a planar
     field is the one case where the true answer is known in closed form at every point, and any
     triangle that is wrong (wrong corner, wrong diagonal handling, swapped u/v) breaks it.
  3. **Both diagonal conventions agree on a plane and differ on a saddle.** A quad whose four
     corners are not coplanar has two genuinely different answers at its centre, and which one
     you get depends on how you split it. If the two conventions agreed there, the class would
     be silently bilinear — the very substitution `csiibundle.areas` says costs a p95 of 9.4 cm.
  4. **Outside the raster it says so** rather than clamping to an edge value, and `drop`
     respects the window it is given rather than always answering.

Run: `python tools/test_heightfield.py`  (system Python + numpy; claim 1 is skipped when no
bundle is on disk).
"""

import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle import png as png_reader
from csiibundle import reader
from csiibundle.drape import HeightfieldSurface

BUNDLE = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
    r"\Cities Skylines II\ModsData\Carto"
)

failures = 0


def check(ok, label, detail=""):
    global failures
    failures += not ok
    print(("ok   " if ok else "FAIL ") + label + (f": {detail}" if detail else ""))


# -- 2, 3, 4: synthetic fields, where the answer is known ---------------------------------------

CELL = 3.5
ORIGIN_X, ORIGIN_Y = -100.0, 250.0
ROWS = COLS = 9

# A plane z = 2x - 3y + 17, sampled on the grid. Row r sits at y = ORIGIN_Y - r*CELL.
cols = ORIGIN_X + numpy.arange(COLS) * CELL
rows = ORIGIN_Y - numpy.arange(ROWS) * CELL
gx, gy = numpy.meshgrid(cols, rows)
plane = 2.0 * gx - 3.0 * gy + 17.0

rng = numpy.random.default_rng(20260812)
qx = rng.uniform(ORIGIN_X, ORIGIN_X + (COLS - 1) * CELL, 5000)
qy = rng.uniform(ORIGIN_Y - (ROWS - 1) * CELL, ORIGIN_Y, 5000)
truth = 2.0 * qx - 3.0 * qy + 17.0

for diagonal in ("nw_se", "ne_sw"):
    surface = HeightfieldSurface(plane, CELL, ORIGIN_X, ORIGIN_Y, diagonal=diagonal)
    got = surface.height(qx, qy)
    worst = float(numpy.abs(got - truth).max())
    check(
        worst < 1e-9,
        f"planar field is exact everywhere ({diagonal})",
        f"worst {worst:.2e} m over {len(qx)} query points",
    )

# A saddle: the four corners of every quad are deliberately non-coplanar, so a quad's centre has
# three DIFFERENT defensible answers and which you get says which surface you built.
#
# At the centre (u = v = 0.5) the query sits exactly ON whichever diagonal was chosen, so the
# answer is that diagonal's own midpoint — (nw + se)/2 for "nw_se", (ne + sw)/2 for "ne_sw".
# Bilinear would give (nw + ne + sw + se)/4, a fourth value distinct from both. Each convention
# is therefore checked against ITS OWN closed-form value, not merely against the other one.
#
# Checking them only against each other is not enough, and this is not hypothetical: an earlier
# draft of this file did exactly that, and a probe that replaced ONE diagonal's triangle pick
# with a bilinear blend still passed it, because the two remained different from each other.
saddle = (gx - ORIGIN_X) * (ORIGIN_Y - gy) / 100.0
centres_x = cols[:-1] + CELL * 0.5
centres_y = rows[:-1] - CELL * 0.5
cxg, cyg = numpy.meshgrid(centres_x, centres_y)
nw, ne = saddle[:-1, :-1], saddle[:-1, 1:]
sw, se = saddle[1:, :-1], saddle[1:, 1:]
bilinear = (nw + ne + sw + se) * 0.25

a = HeightfieldSurface(saddle, CELL, ORIGIN_X, ORIGIN_Y, diagonal="nw_se")
b = HeightfieldSurface(saddle, CELL, ORIGIN_X, ORIGIN_Y, diagonal="ne_sw")
for name, surface, want in (("nw_se", a, (nw + se) * 0.5), ("ne_sw", b, (ne + sw) * 0.5)):
    got = surface.height(cxg.ravel(), cyg.ravel()).reshape(want.shape)
    worst = float(numpy.abs(got - want).max())
    apart = float(numpy.abs(got - bilinear).min())
    check(
        worst < 1e-9 and apart > 1e-6,
        f"quad centres take the {name} diagonal's own midpoint, not a bilinear blend",
        f"error {worst:.2e} m, nearest bilinear value {apart * 100:.2f} cm away",
    )

gap = float(
    numpy.abs(a.height(cxg.ravel(), cyg.ravel()) - b.height(cxg.ravel(), cyg.ravel())).max()
)
check(
    gap > 1e-6,
    "and the two conventions therefore disagree with each other",
    f"worst {gap * 100:.2f} cm at a quad centre",
)

# Vertices are shared by both triangles whichever diagonal is chosen, so both must reproduce
# them; this is claim 1's synthetic half and holds with or without a bundle.
for name, surface in (("nw_se", a), ("ne_sw", b)):
    got = surface.height(gx.ravel(), gy.ravel())
    worst = float(numpy.abs(got - saddle.ravel()).max())
    check(worst < 1e-9, f"lattice vertices are exact on a saddle ({name})", f"worst {worst:.2e} m")

flat = HeightfieldSurface(numpy.zeros((ROWS, COLS)), CELL, ORIGIN_X, ORIGIN_Y)
outside = [
    (ORIGIN_X - CELL, ORIGIN_Y),
    (ORIGIN_X, ORIGIN_Y + CELL),
    (ORIGIN_X + COLS * CELL, ORIGIN_Y),
    (ORIGIN_X, ORIGIN_Y - ROWS * CELL),
]
check(
    all(flat.height(x, y) is None for x, y in outside),
    "outside the raster returns None rather than clamping to an edge value",
)
check(flat.height(ORIGIN_X, ORIGIN_Y) == 0.0, "the exact corner vertex is inside, not outside")

inside_x, inside_y = ORIGIN_X + CELL, ORIGIN_Y - CELL
check(
    flat.drop(inside_x, inside_y, 0.4, 0.5, 0.5) == 0.0,
    "drop answers when the ground is inside the projector window",
)
check(
    flat.drop(inside_x, inside_y, 0.9, 0.5, 0.5) is None,
    "drop declines when the ground is outside the window (a marking on a deck above)",
)
check(
    flat.drop(inside_x, inside_y, -0.9, 0.5, 0.5) is None,
    "drop declines when the ground is above the window too",
)


# -- 1: the real heightmap ----------------------------------------------------------------------

if not os.path.isdir(BUNDLE):
    print(f"SKIP the real-raster check — no bundle at {BUNDLE}")
else:
    paths = reader.terrain_paths(BUNDLE)
    if paths.rasters is None or not os.path.isfile(paths.heightmap):
        print("SKIP the real-raster check — no §8 terrain layer")
    else:
        # `prefer_fine=False`: this check compares against `heightmap.png` at BASE-grid
        # coordinates, so it must build the surface from the same raster. `from_bundle` defaults
        # to the fine 1.75 m one (the ground a heightfield engine is actually given).
        surface = HeightfieldSurface.from_bundle(BUNDLE, prefer_fine=False)
        raw = png_reader.gray_array(paths.heightmap).astype(numpy.float64)
        scale = float(paths.rasters.get("elevationScale", 1.0))
        offset = float(paths.rasters.get("elevationOffset", 0.0))
        truth_grid = raw * scale + offset
        h, w = truth_grid.shape

        # Sample vertices all over the map, not one patch: the geo-reference is what is being
        # checked and a single neighbourhood would not catch a sign error in the row axis.
        rr = rng.integers(0, h, 20000)
        cc = rng.integers(0, w, 20000)
        ox, _, oz = paths.rasters["originWorld"]
        cell = float(paths.rasters["resolution"])
        vx = ox + cc * cell
        vy = oz - rr * cell
        got = surface.height(vx, vy)
        want = truth_grid[rr, cc]
        worst = float(numpy.nanmax(numpy.abs(got - want)))
        check(
            worst < 1e-9,
            f"{len(rr)} real lattice vertices reproduce the raster exactly",
            f"worst {worst:.2e} m, heights {want.min():.1f}..{want.max():.1f} m",
        )

        # And the map's own extent: one cell beyond the last vertex must be outside.
        check(
            surface.height(ox - cell, oz) is None and surface.height(ox, oz + cell) is None,
            "the real raster's north-west edge is bounded correctly",
        )

print("test_heightfield: " + ("FAILED" if failures else "OK"))
sys.exit(1 if failures else 0)
