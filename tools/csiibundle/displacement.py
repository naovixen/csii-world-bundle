"""How far a generated LOD level is from the surface it replaces, in the mesh's own metres.

`level_displacement` measures what a level COST: the distance from its surface back to LOD 0's.
A triangle ratio does not tell a consumer when a level may be shown - two levels at the same
ratio can be a centimetre and a metre out, and a switch point picked from the shape's size
rather than from its error is how a building's roof ends up moving 1.3 m at 56 m while the props
standing on it hold their layout to the centimetre (A6.7). The number is per level, and a
consumer turns it into a threshold with `lod.screen_px`.

`distance_to_surface` is the engine underneath: exact nearest point-to-SURFACE distance over a
uniform triangle grid, numpy only - this module is shared with the Blender adapter, which has
no scipy. The percentile and sample budget are TUNED, not contract; see each constant.
"""

import numpy

# -- how far a generated level is from the one it replaces ---------------------------------------

# A LOD level's error is measured as the distance from ITS surface to LOD 0's, area-weighted and
# read at a percentile rather than at the maximum: one spike on a chimney is not what a viewer
# reads as "the roof moved", and a Hausdorff maximum is entirely decided by that spike. p95 is
# the statistic the A6.7 measurement used. TUNED, and reported with the number so a reader can
# see which percentile the switch points were derived from.
DISPLACEMENT_PERCENTILE = 95.0

# Samples per node per level, after the area-weighted expansion below. MEASURED against a
# 4,000-sample answer on the reference bundle: 600 holds the p95 to within a few per cent on
# every shape but the very largest multi-node ones, and the switch point it feeds is inversely
# proportional to it - a few per cent on the displacement is a few per cent on a switch DISTANCE,
# which is not a difference anyone can see. The nearest-surface search is linear in this, and at
# 600 the whole bundle measures in about two minutes across the bake's workers.
DISPLACEMENT_SAMPLES = 600

# Triangles tested exactly per sample after the bounding-box ranking. Not an approximation: the
# ranking is verified per point and anything it cannot prove is redone exhaustively, so this only
# decides how often that happens.
DISPLACEMENT_CANDIDATES = 12

# Cap on the (points x triangles) pre-filter matrix, so an exhaustive pass over a 100k-triangle
# shape chunks instead of allocating gigabytes.
_DISTANCE_CHUNK_CELLS = 4_000_000

# The search grid, as a division of the mesh's own bounding-box diagonal, and the cell span past
# which a triangle is tested against everything rather than indexed into every cell it touches.
# TUNED. The grid only pays where the cell is bigger than the answer being measured - a LOD1 that
# is metres out on a forty-metre building falls through to the exhaustive path by design, and
# that is the honest outcome: it is the big meshes with SMALL errors where the index saves the
# time (9x on a 257k-triangle airport).
_GRID_DIVISIONS = 32
_MAX_TRIANGLE_CELLS = 1024


def _surface_samples(verts, tris, cap=DISPLACEMENT_SAMPLES, seed=1234):
    """`(points, weights)` on the surface of a mesh: the three vertices, the three edge midpoints
    and the centroid of every triangle, each carrying a seventh of that triangle's area.

    Vertices alone are not the surface. The error a decimated level actually shows is largest in
    the MIDDLE of a big triangle that bridges a notch LOD 0 has - a balcony recess, a dormer -
    where every vertex may still sit exactly on the original surface. Weighting by area is what
    makes the percentile a statement about the surface rather than about the vertex list.

    Deterministic: over the cap, a fixed-seed permutation subsamples, so two bakes of the same
    mesh give the same number.
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64).reshape(-1, 3)
    if len(tris) == 0 or len(verts) == 0:
        return numpy.zeros((0, 3)), numpy.zeros(0)
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    area = 0.5 * numpy.linalg.norm(numpy.cross(b - a, c - a), axis=1)
    points = numpy.concatenate(
        [a, b, c, (a + b) * 0.5, (b + c) * 0.5, (c + a) * 0.5, (a + b + c) / 3.0]
    )
    weights = numpy.tile(area / 7.0, 7)
    if len(points) > cap:
        pick = numpy.random.default_rng(seed).permutation(len(points))[:cap]
        points, weights = points[pick], weights[pick]
    return points, weights


def _point_triangle_distance_sq(points, a, b, c):
    """Squared distance from each point to its paired triangle - Ericson's closest-point-on-
    triangle, vectorised over arrays of matching shape `(..., 3)`."""
    ab, ac, ap = b - a, c - a, points - a
    d1 = (ab * ap).sum(-1)
    d2 = (ac * ap).sum(-1)
    bp = points - b
    d3 = (ab * bp).sum(-1)
    d4 = (ac * bp).sum(-1)
    cp = points - c
    d5 = (ab * cp).sum(-1)
    d6 = (ac * cp).sum(-1)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = va + vb + vc
    safe = numpy.where(numpy.abs(denom) > 1e-20, denom, 1.0)
    v = numpy.clip(vb / safe, 0.0, 1.0)
    w = numpy.clip(vc / safe, 0.0, 1.0)
    closest = a + ab * v[..., None] + ac * w[..., None]  # the face region
    # the three vertex regions
    closest = numpy.where(((d1 <= 0) & (d2 <= 0))[..., None], a, closest)
    closest = numpy.where(((d3 >= 0) & (d4 <= d3))[..., None], b, closest)
    closest = numpy.where(((d6 >= 0) & (d5 <= d6))[..., None], c, closest)
    # the three edge regions
    t = numpy.clip(d1 / numpy.where(numpy.abs(d1 - d3) > 1e-20, d1 - d3, 1.0), 0.0, 1.0)
    closest = numpy.where(
        ((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None], a + ab * t[..., None], closest
    )
    t = numpy.clip(d2 / numpy.where(numpy.abs(d2 - d6) > 1e-20, d2 - d6, 1.0), 0.0, 1.0)
    closest = numpy.where(
        ((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None], a + ac * t[..., None], closest
    )
    edge = (d4 - d3) + (d5 - d6)
    t = numpy.clip((d4 - d3) / numpy.where(numpy.abs(edge) > 1e-20, edge, 1.0), 0.0, 1.0)
    closest = numpy.where(
        ((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0))[..., None],
        b + (c - b) * t[..., None],
        closest,
    )
    delta = points - closest
    return (delta * delta).sum(-1)


def _cell_grid(a, b, c, cell):
    """Index every triangle into a uniform grid of `cell` metres: which cells each triangle's
    bounding box touches. Returns `(origin, dims, tri_index, keys, oversized)`, where `keys` is
    sorted and `tri_index` names the triangle each key belongs to, so a query is two
    `searchsorted` calls per cell.

    A triangle spanning more than `_MAX_TRIANGLE_CELLS` cells (a ten-metre wall against a
    decimetre grid) is NOT indexed - it goes in `oversized` and is tested against every query
    point. Indexing it would cost more cells than it saves tests.
    """
    lo = numpy.minimum(numpy.minimum(a, b), c)
    hi = numpy.maximum(numpy.maximum(a, b), c)
    origin = lo.min(axis=0)
    cell_lo = numpy.floor((lo - origin) / cell).astype(numpy.int64)
    cell_hi = numpy.floor((hi - origin) / cell).astype(numpy.int64)
    dims = cell_hi.max(axis=0) + 1
    span = cell_hi - cell_lo + 1
    counts = span.prod(axis=1)
    oversized = numpy.nonzero(counts > _MAX_TRIANGLE_CELLS)[0]
    keep = numpy.nonzero(counts <= _MAX_TRIANGLE_CELLS)[0]
    if len(keep) == 0:
        return origin, dims, numpy.zeros(0, numpy.int64), numpy.zeros(0, numpy.int64), oversized
    counts = counts[keep]
    index = numpy.repeat(keep, counts)
    ends = numpy.cumsum(counts)
    offset = numpy.arange(int(ends[-1])) - numpy.repeat(ends - counts, counts)
    nx = span[index, 0]
    ny = span[index, 1]
    cx = cell_lo[index, 0] + offset % nx
    cy = cell_lo[index, 1] + (offset // nx) % ny
    cz = cell_lo[index, 2] + offset // (nx * ny)
    keys = (cz * dims[1] + cy) * dims[0] + cx
    order = numpy.argsort(keys, kind="stable")
    return origin, dims, index[order], keys[order], oversized


def _ranges(starts, stops):
    """Concatenate `arange(start, stop)` over many ranges without a Python loop."""
    lengths = stops - starts
    total = int(lengths.sum())
    if total == 0:
        return numpy.zeros(0, numpy.int64)
    ends = numpy.cumsum(lengths)
    offset = numpy.arange(total) - numpy.repeat(ends - lengths, lengths)
    return numpy.repeat(starts, lengths) + offset


def _grid_pass(points, a, b, c, grid, cell, candidates, out):
    """One pass over the occupied query cells: every point in a cell reads the same 3x3x3 block
    of the triangle grid and takes its nearest distance over the triangles found there."""
    origin, dims, tri_index, keys, oversized = grid
    cells = numpy.clip(numpy.floor((points - origin) / cell).astype(numpy.int64), 0, dims - 1)
    point_key = (cells[:, 2] * dims[1] + cells[:, 1]) * dims[0] + cells[:, 0]
    order = numpy.argsort(point_key, kind="stable")
    unique, first = numpy.unique(point_key[order], return_index=True)
    bounds = numpy.append(first, len(order))
    neighbourhood = numpy.array(
        [
            (dz * dims[1] + dy) * dims[0] + dx
            for dz in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
        ],
        dtype=numpy.int64,
    )
    for slot in range(len(unique)):
        members = order[bounds[slot] : bounds[slot + 1]]
        block = unique[slot] + neighbourhood
        starts = numpy.searchsorted(keys, block, side="left")
        stops = numpy.searchsorted(keys, block, side="right")
        found = tri_index[_ranges(starts, stops)]
        if len(oversized):
            found = numpy.concatenate([found, oversized])
        if len(found) == 0:
            continue
        found = numpy.unique(found)
        out[members] = _nearest_triangle(points[members], a[found], b[found], c[found], candidates)


def distance_to_surface(points, verts, tris, candidates=DISPLACEMENT_CANDIDATES):
    """Nearest distance from each point to the SURFACE of `(verts, tris)`, not to its vertices.

    Nearest-vertex is the cheap answer and it is wrong on exactly the meshes this is for: a CS2
    wall is two triangles across ten metres, so a point in the middle of it is a centimetre from
    the surface and five metres from the nearest vertex.

    A uniform grid over the triangles' bounding boxes carries the search. A query reads the 3x3x3
    cell block around its own cell, which contains every triangle within one cell of the point by
    construction - so an answer at or under the cell size is EXACT, and anything larger (or a
    point whose block was empty) is redone against every triangle. On these meshes the exact
    answer is a fraction of the cell, so the exhaustive path is the exception rather than the
    rule; it is still there because a bounded search that quietly returns the wrong number is
    worse than a slow one.

    Numpy only - this module is shared with the Blender adapter, which has no scipy.
    """
    points = numpy.asarray(points, dtype=numpy.float64).reshape(-1, 3)
    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64).reshape(-1, 3)
    if len(points) == 0 or len(tris) == 0:
        return numpy.zeros(len(points))
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    diagonal = float(numpy.linalg.norm(verts.max(axis=0) - verts.min(axis=0))) or 1.0
    cell = max(diagonal / _GRID_DIVISIONS, 1e-6)
    grid = _cell_grid(a, b, c, cell)
    out = numpy.full(len(points), numpy.inf)
    if len(grid[2]):
        _grid_pass(points, a, b, c, grid, cell, candidates, out)
    # a point the grid could not answer exactly - an empty block, or a nearest surface further
    # away than the block is guaranteed to cover - is redone against the whole mesh
    redo = numpy.nonzero(~(out <= cell))[0]
    if len(redo):
        out[redo] = _nearest_triangle(points[redo], a, b, c, candidates)
    return out


def _nearest_triangle(points, a, b, c, candidates=DISPLACEMENT_CANDIDATES):
    """Exact nearest distance from each point to a set of triangles.

    Two passes: rank the triangles by the distance from the point to each triangle's bounding
    box - a lower bound on the true point-triangle distance - and run the exact point-triangle
    test on the best `candidates`. The box is used rather than `|p - centroid| - circumradius`
    because a CS2 wall is one ten-metre triangle: its circumradius bound is deeply negative
    everywhere, so a handful of such triangles crowd out the small one the point actually sits
    on, while its box is thin and bounds tightly.

    **The ranking is checked, not assumed.** Every triangle outside the chosen set has a bound of
    at least the k-th smallest, so when that is already >= the distance found, none of them can
    beat it and the answer is exact. Points that fail are redone against every triangle. (With
    the centroid bound and a fixed twelve candidates, unchecked, this was up to 16.9 m wrong on
    the reference bundle.)
    """
    if len(points) == 0:
        return numpy.zeros(0)
    if len(a) == 0:
        return numpy.full(len(points), numpy.inf)
    lo = numpy.minimum(numpy.minimum(a, b), c)
    hi = numpy.maximum(numpy.maximum(a, b), c)
    out = numpy.empty(len(points))
    k = int(min(max(1, candidates), len(a)))
    chunk = max(1, int(_DISTANCE_CHUNK_CELLS // max(len(a), 1)))
    unresolved = []
    for start in range(0, len(points), chunk):
        block = points[start : start + chunk]
        gap = numpy.maximum(lo[None, :, :] - block[:, None, :], 0.0) + numpy.maximum(
            block[:, None, :] - hi[None, :, :], 0.0
        )
        bound = numpy.sqrt((gap * gap).sum(2))
        if k < len(a):
            order = numpy.argpartition(bound, k - 1, axis=1)
            pick = order[:, :k]
            cutoff = numpy.take_along_axis(bound, order[:, k - 1 : k], axis=1).ravel()
        else:
            pick = numpy.broadcast_to(numpy.arange(len(a)), (len(block), len(a)))
            cutoff = numpy.full(len(block), numpy.inf)
        d2 = _point_triangle_distance_sq(block[:, None, :], a[pick], b[pick], c[pick])
        best = numpy.sqrt(d2.min(axis=1))
        out[start : start + chunk] = best
        if k < len(a):
            unresolved.append(numpy.nonzero(best > cutoff)[0] + start)
    rows = numpy.concatenate(unresolved) if unresolved else numpy.zeros(0, numpy.int64)
    if len(rows):
        # the exhaustive answer for the few points the ranking could not prove - no chunking
        # needed, there are never many
        for start in range(0, len(rows), max(1, chunk)):
            block_rows = rows[start : start + max(1, chunk)]
            block = points[block_rows]
            d2 = _point_triangle_distance_sq(
                block[:, None, :], a[None, :, :], b[None, :, :], c[None, :, :]
            )
            out[block_rows] = numpy.sqrt(d2.min(axis=1))
    return out


def weighted_percentile(values, weights, percentile):
    """The `percentile`-th percentile of `values` weighted by `weights` (0 for an empty set)."""
    values = numpy.asarray(values, dtype=numpy.float64).ravel()
    weights = numpy.asarray(weights, dtype=numpy.float64).ravel()
    if values.size == 0 or weights.sum() <= 0:
        return 0.0
    order = numpy.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = numpy.cumsum(weights) - 0.5 * weights
    cumulative /= weights.sum()
    return float(numpy.interp(percentile / 100.0, cumulative, values))


def level_displacement(
    level, source, percentile=DISPLACEMENT_PERCENTILE, samples=DISPLACEMENT_SAMPLES
):
    """How far a generated level's surface is from the surface it replaces, in the mesh's own
    metres: the `percentile` of the distance from points sampled over `level` to `source`.

    This is the number a switch point should be derived from, and a triangle count is not. Two
    levels at the same ratio can be a centimetre and a metre out - a prop's LOD1 is 0.02 m from
    its LOD0 and a building's is over a metre - and a ladder built from ratios of the shape's own
    CULL threshold cannot tell them apart, because that threshold is a statement about the
    shape's SIZE (A6.7).

    **Two-sided.** Samples over the level are measured to the source (where has this level put
    its surface — the roof moving) AND samples over the source are measured to the level (what
    has this level dropped). The second half is the one a one-sided measurement cannot see: a
    railing, a window frame or a crane boom that the decimator collapses onto the wall behind it
    leaves the level's surface exactly on that wall, at zero distance from the source — and the
    feature is gone. Measured from the source's side the railing's samples are metres from the
    nearest level surface, which is what a viewer sees as pop-in when the level takes over.
    (Refactor Phase 10, 2026-08-29: the quadric backend reaches 0.0 m one-sided on most
    buildings, and it is this half that keeps a 0.0 honest.)

    Returns `(displacement, distances, weights)` so a caller pooling several nodes of one file
    can take the percentile over all of them at once rather than averaging percentiles.
    """
    points, weights = _surface_samples(level.get("verts"), level.get("tris"), cap=samples)
    if len(points) == 0:
        return 0.0, numpy.zeros(0), numpy.zeros(0)
    back, back_weights = _surface_samples(source.get("verts"), source.get("tris"), cap=samples)
    distances = numpy.concatenate(
        [
            distance_to_surface(points, source["verts"], source["tris"]),
            distance_to_surface(back, level["verts"], level["tris"]),
        ]
    )
    weights = numpy.concatenate([weights, back_weights])
    return weighted_percentile(distances, weights, percentile), distances, weights
