"""Surface areas (§8b) — driveways, parking-lot pavement, park paths.

An area is the painted ground of a meshless prefab, and the single most important thing about
one is that **its triangles are a FOOTPRINT, not a plane** (audit §30). The game renders each
triangle as a prism spanning `Triangle.m_HeightRange` — the min/max of the heightmap over that
triangle's own footprint, measured relative to its plane — and paints the material onto the
terrain inside it, as a decal (`RenderedArea.m_DecalLayerMask = DecalLayers.Terrain`). Nothing
grades the ground flat underneath. The one thing that does, `TerrainArea`/`CullAreasJob`,
belongs to LotPrefab and is already baked into §8's heightmap.

So a consumer that draws the triangle flat is drawing a plane the game never draws. Measured on
a full-city bundle, that put the ground through the pavement on 26.6% of 175,783 area triangles
and 61.4% of 27,422 areas (p95 11 cm, p99 55 cm). `conform_triangle` splits each one until it
follows the ground.

**Sample the ground the way you will draw it.** Bilinearly interpolating the heightmap cannot
substitute for asking the surface: the terrain is meshed as QUADS and the mesher picks their
diagonal, a choice worth a p95 of 9.4 cm at 3.5 m cells and 41 cm at the 14 m stride of a
full-city import. `conform_triangle` takes a `sample` callable so the caller supplies whatever
its own ground actually is; `csiibundle.drape` is one such surface.

**The tolerance is half the lift, and the ratio is the point** — the same rule the lane decals'
subdivision uses. How far the ground may rise through a piece cannot be chosen independently of
how far that piece is lifted clear of it. The lift itself is a rung of `decals.LIFT_LADDER`,
which is where every layer of ground paint is ordered against every other.
"""

import math

import numpy

from . import decals


# Lift above the terrain grid, below everything that stands on the area. The rung is
# `decals.LIFT_LADDER`'s, where every layer of ground paint is ordered against every other; the
# game draws these as terrain-layer decals, here they are thin geometry riding the ground.
AREA_LIFT = decals.LIFT["area"]

# Half the area's own lift. The RATIO is the constant, not either number — see the module
# docstring, and `csiibundle.decals` for the same rule on the layer above this one. TUNED.
CONFORM_TOLERANCE = AREA_LIFT * 0.5
# A big triangle at the full-city stride needs ~10 halvings to reach the floor, so the depth
# cap has to clear that or the cap silently becomes the stopping rule (it did: 810k pieces
# stopped with up to 4 m of ground above them). The runaway guard is the piece BUDGET below,
# not the depth — a budget bounds the work whatever the triangle's size, and reports itself.
MAX_DEPTH = 12
# Pieces one source triangle may spend, scaled by the ground it actually covers: a driveway
# triangle inside one terrain cell has nothing to resolve, a 25-hectare grass sheet over a
# gorge has plenty. A flat budget gets this backwards at both ends. The constant is pieces
# per terrain cell covered, so the whole layer costs ~this many pieces per cell of ground it
# paints, whatever the triangulation happens to look like.
PIECES_PER_CELL = 256
PIECE_BUDGET = (64, 65536)  # (floor, ceiling) on that scaling
DEDUP_MM = 1.0              # quantise world (x, y) to this before reusing a ray-cast

# `RenderedArea.m_UVScale`'s own field default, used only when the bundle predates §8b
# carrying the per-prefab value. World position times this is the UV.
UV_SCALE_FALLBACK = 0.2


def conform_triangle(tri, cell, sample, counters):
    """Split one area triangle until every piece follows the ground, and emit the pieces.

    `tri` is three (x, y, z) Blender-frame corners already dropped onto the ground; `sample`
    is the memoised ground lookup; `cell` the terrain grid's own spacing. Returns the list of
    (a, b, c) conformed corner triples.

    The floor is deliberately far below the grid cell. A first cut stopped at a quarter cell,
    reasoning that a smaller piece cannot straddle a kink — wrong, and measurably so: the
    kinks are the quads' own diagonals (Blender's choice, anywhere inside a cell) and the
    15 cm roadbed sink boundary that every driveway crosses, so 14.6% of pieces stopped with
    up to 2.1 cm of ground still above them. A plane kink's error shrinks linearly with piece
    size, so the floor only has to be small; what it really guards is a true discontinuity
    (a cut-face rim, a gorge wall), where no amount of splitting converges.
    """
    floor = max(cell * 0.02, 0.10)
    (ax, ay, _), (bx, by, _), (cx, cy, _) = tri
    plan = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) * 0.5
    budget = min(PIECE_BUDGET[1],
                 max(PIECE_BUDGET[0],
                     int(PIECES_PER_CELL * plan / (cell * cell))))
    work = [(tri, 0)]
    out = []
    while work:
        (a, b, c), depth = work.pop()
        mids = []
        worst = 0.0
        for p, q in ((a, b), (b, c), (c, a)):
            mx, my = (p[0] + q[0]) * 0.5, (p[1] + q[1]) * 0.5
            flat = (p[2] + q[2]) * 0.5
            got = sample(mx, my)
            mids.append((mx, my, flat if got is None else got))
            if got is not None:
                worst = max(worst, got - flat)
        # The centroid too: three edge midpoints can all sit on the ground while a rise in
        # the middle of the piece — a knoll inside one cell, the peak of a quad's diagonal —
        # goes straight through it unseen.
        gx, gy = (a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0
        centre = sample(gx, gy)
        if centre is not None:
            worst = max(worst, centre - (a[2] + b[2] + c[2]) / 3.0)
        longest = max(math.dist(p[:2], q[:2]) for p, q in ((a, b), (b, c), (c, a)))
        spent = len(out) + len(work)
        if (worst <= CONFORM_TOLERANCE or depth >= MAX_DEPTH
                or longest <= floor or spent >= budget):
            if worst > CONFORM_TOLERANCE:
                # Report the GROUND left unconformed, not the piece count: a piece is an
                # arbitrary unit (a budget stop leaves few big ones, a floor stop many small
                # ones) and only the square metres say how much of the city still shows it.
                counters["unconformed"] += 1
                counters["unconformed_area"] += abs(
                    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
                counters["worst"] = max(counters["worst"], worst)
            out.append((a, b, c))
            continue
        ab, bc, ca = mids
        work.append(((a, ab, ca), depth + 1))
        work.append(((ab, b, bc), depth + 1))
        work.append(((ca, bc, c), depth + 1))
        work.append(((ab, bc, ca), depth + 1))
    return out
