"""Generated LOD chains — the producer ships LOD 0 only, and an engine needs less far away.

`lod_ladder` takes one mesh (the `glb.parse_glb` node shape: `verts`, `normals`, `uvs`, `tris`,
`material_runs`) and returns simplified copies at the requested triangle ratios, per material
run so material boundaries survive. Two backends behind one API:

* **quadric** — `pyfqmr` (Fast-Quadric-Mesh-Simplification; `pip install pyfqmr`, the optional
  `[lods]` extra) when importable. Better shape preservation; UVs are re-attached by nearest
  original vertex, which is fine at the distances a LOD is seen.
* **vertex clustering** — numpy only, always available. Positions are snapped to a grid whose
  cell is searched to hit the target ratio; vertices in the same cell AND the same coarse UV
  bin merge (so a texture seam does not smear across a cluster); degenerate and duplicate faces
  drop. Crude, fast, and good enough for a building at 200 m — the engine's own imposter
  (`bb_autobillboard`) takes over beyond that.

Both keep every material run (a run may vanish if all its faces collapse, and that is reported
in the returned `stats`). `vertex_albedo` samples each vertex's albedo for the last level, which
BeamNG draws with the untextured `lod_vertcol` material.

`level_displacement` measures what a level COST: the distance from its surface back to LOD 0's.
A triangle ratio does not tell a consumer when a level may be shown - two levels at the same
ratio can be a centimetre and a metre out, and a switch point picked from the shape's size
rather than from its error is how a building's roof ends up moving 1.3 m at 56 m while the props
standing on it hold their layout to the centimetre (A6.7). The number is per level, in the
mesh's own metres, and a consumer turns it into a threshold with `lod.screen_px`.

Nothing here is CONTRACT: the ratios, the UV bin, the cluster search and the displacement
percentile are all TUNED and a consumer may replace the ladder with authored LODs the moment the
producer ships them.
"""

import numpy

# Coarse UV bin used to keep seams apart while clustering: vertices whose UVs differ by more
# than this fraction of the texture stay separate even in the same cell. TUNED.
UV_BIN = 0.125

# ...but a fixed bin is a fraction of ONE texture repeat, and a world-planar UV is not a texture
# coordinate in that sense: an area or road tile's UV is its world position times the prefab's
# `colossal_UVScale`, so a 256 m tile spans tens of repeats and the fixed bin separated almost
# every vertex from its neighbour — clustering could not merge anything. Measured on the first A5
# build: `areas` reached 39 % of its target ratio and `markings` 33 %, against `networks`' 22 %.
# The bin is therefore at least the mesh's own UV range divided by this, so the same amount of
# merging is allowed whatever the repeat count; an atlased mesh (range ~1) keeps `UV_BIN` exactly.
UV_BINS_PER_MESH = 8.0

# Cell search bounds as fractions of the mesh's bounding-box diagonal. TUNED.
CELL_MIN_FRACTION = 0.002
CELL_MAX_FRACTION = 0.5


def _quadric_available():
    try:
        import pyfqmr  # noqa: F401

        return True
    except Exception:
        return False


def split_runs(mesh):
    """`[(material_name, verts, normals, uvs, tris_local)]` — one compact sub-mesh per material run."""
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    normals = (
        None if mesh.get("normals") is None else numpy.asarray(mesh["normals"], dtype=numpy.float64)
    )
    uvs = None if mesh.get("uvs") is None else numpy.asarray(mesh["uvs"], dtype=numpy.float64)
    tris = numpy.asarray(mesh["tris"], dtype=numpy.int64)
    out = []
    cursor = 0
    for name, count in mesh.get("material_runs") or [(None, len(tris))]:
        t = tris[cursor : cursor + count]
        cursor += count
        if len(t) == 0:
            continue
        used, inverse = numpy.unique(t.ravel(), return_inverse=True)
        out.append(
            (
                name,
                verts[used],
                None if normals is None else normals[used],
                None if uvs is None else uvs[used],
                inverse.reshape(-1, 3),
            )
        )
    return out


def merge_runs(parts):
    """Inverse of `split_runs`: back to one `verts/normals/uvs/tris/material_runs` mesh."""
    verts, normals, uvs, tris, runs = [], [], [], [], []
    base = 0
    has_n = all(p[2] is not None for p in parts if p is not None)
    has_uv = all(p[3] is not None for p in parts if p is not None)
    for part in parts:
        if part is None:
            continue
        name, v, n, uv, t = part
        if len(t) == 0 or len(v) == 0:
            continue
        verts.append(v)
        if has_n:
            normals.append(n)
        if has_uv:
            uvs.append(uv)
        tris.append(t + base)
        runs.append((name, len(t)))
        base += len(v)
    if not verts:
        return {
            "verts": numpy.zeros((0, 3)),
            "normals": numpy.zeros((0, 3)) if has_n else None,
            "uvs": numpy.zeros((0, 2)) if has_uv else None,
            "tris": numpy.zeros((0, 3), numpy.int64),
            "material_runs": [],
        }
    return {
        "verts": numpy.concatenate(verts).astype(numpy.float32),
        "normals": numpy.concatenate(normals).astype(numpy.float32) if has_n else None,
        "uvs": numpy.concatenate(uvs).astype(numpy.float32) if has_uv else None,
        "tris": numpy.concatenate(tris),
        "material_runs": runs,
    }


# -- vertex clustering --------------------------------------------------------------------------


def vertex_cluster(verts, tris, uvs=None, normals=None, cell=1.0, uv_bin=UV_BIN, pinned=None):
    """Snap vertices to a grid of `cell` metres and merge those that share a cell (and a UV bin).

    Returns `(verts, normals, uvs, tris)` with degenerate and duplicate faces removed. Positions,
    normals and UVs of a cluster are its members' means (normals renormalised).

    **`pinned` is what makes this safe on a tiled world.** The grid's origin is the mesh's own
    minimum corner and its cell size is searched per mesh (`_cluster_to_ratio`), so two adjacent
    tiles cluster on two different grids: a vertex on their shared edge is averaged one way in
    one tile and another way in the other, and the seam opens. A `pinned` boolean per vertex puts
    each of those vertices in a cluster of its own, so its position comes out EXACTLY as it went
    in and both tiles still agree on it. The caller decides which vertices those are — for a
    world tile it is a band around the tile boundary, a rule both neighbours compute identically
    from world position (`beamng/tilelod.boundary_pins`).
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    if len(verts) == 0 or len(tris) == 0:
        return verts, normals, uvs, tris
    origin = verts.min(axis=0)
    keys = numpy.floor((verts - origin) / max(cell, 1e-9)).astype(numpy.int64)
    columns = [keys[:, 0], keys[:, 1], keys[:, 2]]
    if uvs is not None and uv_bin:
        uv = numpy.asarray(uvs, dtype=numpy.float64)
        span = float(numpy.nanmax(uv) - numpy.nanmin(uv)) if uv.size else 0.0
        bin_size = max(float(uv_bin), span / UV_BINS_PER_MESH) if span > 0 else float(uv_bin)
        columns.append(numpy.floor(uv[:, 0] / bin_size).astype(numpy.int64))
        columns.append(numpy.floor(uv[:, 1] / bin_size).astype(numpy.int64))
    if pinned is not None:
        pins = numpy.asarray(pinned, dtype=bool)
        # -1 for everything free, its own index for everything pinned: a column no two pinned
        # vertices share, and one that never separates two free ones.
        columns.append(
            numpy.where(pins, numpy.arange(len(verts), dtype=numpy.int64), numpy.int64(-1))
        )
    composite = numpy.stack(columns, axis=1)
    _, first, inverse = numpy.unique(composite, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    n_out = len(first)
    counts = numpy.bincount(inverse, minlength=n_out).astype(numpy.float64)
    new_verts = numpy.zeros((n_out, 3))
    for k in range(3):
        new_verts[:, k] = numpy.bincount(inverse, weights=verts[:, k], minlength=n_out) / counts
    new_normals = None
    if normals is not None:
        nrm = numpy.asarray(normals, dtype=numpy.float64)
        new_normals = numpy.zeros((n_out, 3))
        for k in range(3):
            new_normals[:, k] = numpy.bincount(inverse, weights=nrm[:, k], minlength=n_out)
        length = numpy.linalg.norm(new_normals, axis=1)
        length[length < 1e-12] = 1.0
        new_normals /= length[:, None]
    new_uvs = None
    if uvs is not None:
        uv = numpy.asarray(uvs, dtype=numpy.float64)
        new_uvs = numpy.zeros((n_out, 2))
        for k in range(2):
            new_uvs[:, k] = numpy.bincount(inverse, weights=uv[:, k], minlength=n_out) / counts
    new_tris = inverse[tris]
    keep = (
        (new_tris[:, 0] != new_tris[:, 1])
        & (new_tris[:, 1] != new_tris[:, 2])
        & (new_tris[:, 0] != new_tris[:, 2])
    )
    new_tris = new_tris[keep]
    if len(new_tris):
        # duplicates (same three vertices in any order) collapse to one; winding kept from the first
        sorted_keys = numpy.sort(new_tris, axis=1)
        _, first_tri = numpy.unique(sorted_keys, axis=0, return_index=True)
        new_tris = new_tris[numpy.sort(first_tri)]
    # drop vertices no face references
    used = numpy.zeros(n_out, dtype=bool)
    used[new_tris.ravel()] = True
    remap = numpy.cumsum(used) - 1
    new_tris = remap[new_tris]
    new_verts = new_verts[used]
    new_normals = None if new_normals is None else new_normals[used]
    new_uvs = None if new_uvs is None else new_uvs[used]
    return new_verts, new_normals, new_uvs, new_tris


def _cluster_to_ratio(verts, tris, uvs, normals, ratio, iterations=12, pinned=None):
    """Binary-search the cluster cell so the face count lands near `ratio` of the input."""
    verts = numpy.asarray(verts, dtype=numpy.float64)
    if len(tris) < 4:
        return verts, normals, uvs, numpy.asarray(tris)
    diag = float(numpy.linalg.norm(verts.max(axis=0) - verts.min(axis=0))) or 1.0
    lo, hi = diag * CELL_MIN_FRACTION, diag * CELL_MAX_FRACTION
    target = max(1, int(round(len(tris) * ratio)))
    best = None
    for _ in range(iterations):
        mid = (lo + hi) * 0.5
        result = vertex_cluster(verts, tris, uvs, normals, cell=mid, pinned=pinned)
        count = len(result[3])
        if best is None or abs(count - target) < abs(len(best[3]) - target):
            best = result
        if count > target:
            lo = mid  # too many faces: bigger cells
        else:
            hi = mid
    return best


# -- quadric (optional) --------------------------------------------------------------------------


def _quadric_to_ratio(verts, tris, uvs, normals, ratio):
    import pyfqmr

    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    target = max(1, int(round(len(tris) * ratio)))
    if len(tris) <= 4 or target >= len(tris):
        return verts, normals, uvs, tris
    simplifier = pyfqmr.Simplify()
    simplifier.setMesh(verts, tris)
    simplifier.simplify_mesh(target_count=target, aggressiveness=7, preserve_border=True, verbose=0)
    new_verts, new_tris, _new_normals = simplifier.getMesh()
    new_verts = numpy.asarray(new_verts, dtype=numpy.float64)
    new_tris = numpy.asarray(new_tris, dtype=numpy.int64)
    if len(new_tris) == 0:
        return verts, normals, uvs, tris
    # attributes by nearest original vertex (a k-d tree would be nicer; chunked brute force is
    # fine at LOD sizes)
    nearest = _nearest_index(new_verts, verts)
    new_uvs = None if uvs is None else numpy.asarray(uvs)[nearest]
    new_normals = None if normals is None else numpy.asarray(normals)[nearest]
    return new_verts, new_normals, new_uvs, new_tris


def _nearest_index(query, points, chunk=2048):
    out = numpy.empty(len(query), dtype=numpy.int64)
    for s in range(0, len(query), chunk):
        q = query[s : s + chunk]
        d = ((q[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)
        out[s : s + chunk] = d.argmin(axis=1)
    return out


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
    origin, dims, tri_index, keys, oversized = _cell_grid(a, b, c, cell)
    out = numpy.full(len(points), numpy.inf)

    if len(tri_index):
        cells = numpy.clip(numpy.floor((points - origin) / cell).astype(numpy.int64), 0, dims - 1)
        point_key = (cells[:, 2] * dims[1] + cells[:, 1]) * dims[0] + cells[:, 0]
        # one pass per occupied query cell: every point in it reads the same 27-cell block
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
            out[members] = _nearest_triangle(
                points[members], a[found], b[found], c[found], candidates
            )
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

    One-sided (level -> source) on purpose: it answers "where has this level put its surface",
    which is what a viewer sees as the roof moving. A source feature the level dropped shows up
    too, because the level's surface has to be somewhere else to have dropped it.

    Returns `(displacement, distances, weights)` so a caller pooling several nodes of one file
    can take the percentile over all of them at once rather than averaging percentiles.
    """
    points, weights = _surface_samples(level.get("verts"), level.get("tris"), cap=samples)
    if len(points) == 0:
        return 0.0, numpy.zeros(0), numpy.zeros(0)
    distances = distance_to_surface(points, source["verts"], source["tris"])
    return weighted_percentile(distances, weights, percentile), distances, weights


# -- the ladder ---------------------------------------------------------------------------------


def lod_ladder(mesh, targets=(0.25, 0.06), backend="auto"):
    """`(levels, stats)`: one simplified mesh per ratio in `targets` (each in the input's own
    frame and shape), and a stats dict `{"backend", "input_tris", "levels": [tris...]}`.

    `backend` is `"auto"` (quadric if `pyfqmr` imports, else cluster), `"cluster"` or
    `"quadric"`. Ratios are per material run; a run that collapses entirely is dropped from that
    level and counted in `stats["dropped_runs"]`.
    """
    use_quadric = (backend == "quadric") or (backend == "auto" and _quadric_available())
    reducer = _quadric_to_ratio if use_quadric else _cluster_to_ratio
    parts = split_runs(mesh)
    levels = []
    dropped = 0
    for ratio in targets:
        reduced = []
        for name, v, n, uv, t in parts:
            rv, rn, ruv, rt = reducer(v, t, uv, n, ratio)
            if len(rt) == 0:
                dropped += 1
                continue
            reduced.append((name, rv, rn, ruv, rt))
        levels.append(merge_runs(reduced))
    stats = {
        "backend": "quadric" if use_quadric else "cluster",
        "input_tris": len(mesh["tris"]),
        "levels": [len(level["tris"]) for level in levels],
        "dropped_runs": dropped,
    }
    return levels, stats


def cluster_mesh(mesh, ratio, pinned=None):
    """One `{verts, normals, uvs, tris, material_runs}` mesh clustered to about `ratio` of its
    triangles, per material run, with `pinned` (per vertex of the WHOLE mesh) held in place.

    The run split is what keeps a material boundary from smearing; `pinned` is indexed on the
    merged mesh and is re-indexed into each run here, so a caller computes it once from the
    mesh's own vertices.
    """
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    tris = numpy.asarray(mesh["tris"], dtype=numpy.int64)
    pins = None if pinned is None else numpy.asarray(pinned, dtype=bool)
    parts = []
    cursor = 0
    for name, count in mesh.get("material_runs") or [(None, len(tris))]:
        t = tris[cursor : cursor + count]
        cursor += count
        if len(t) == 0:
            continue
        used, inverse = numpy.unique(t.ravel(), return_inverse=True)
        sub_v = verts[used]
        sub_n = None if mesh.get("normals") is None else numpy.asarray(mesh["normals"])[used]
        sub_uv = None if mesh.get("uvs") is None else numpy.asarray(mesh["uvs"])[used]
        sub_pin = None if pins is None else pins[used]
        out_v, out_n, out_uv, out_t = _cluster_to_ratio(
            sub_v, inverse.reshape(-1, 3), sub_uv, sub_n, ratio, pinned=sub_pin
        )
        parts.append((name, out_v, out_n, out_uv, out_t))
    return merge_runs(parts)


def vertex_albedo(mesh, sampler):
    """`(N, 4)` float32 linear-ish RGBA per vertex: the material's albedo sampled at the vertex
    UV (nearest texel), or its flat colour, for the `lod_vertcol` last level.

    `sampler(material_name)` returns either an `(h, w, 4)` uint8 array (a decoded albedo PNG)
    or an RGBA tuple in 0..1 (a flat colour), or None (unknown → mid grey). UVs are glTF
    top-left origin, tiled by fract().
    """
    verts = numpy.asarray(mesh["verts"])
    n = len(verts)
    colours = numpy.full((n, 4), (0.5, 0.5, 0.5, 1.0), dtype=numpy.float32)
    uvs = None if mesh.get("uvs") is None else numpy.asarray(mesh["uvs"], dtype=numpy.float64)
    tris = numpy.asarray(mesh["tris"], dtype=numpy.int64)
    cursor = 0
    for name, count in mesh.get("material_runs") or [(None, len(tris))]:
        t = tris[cursor : cursor + count]
        cursor += count
        idx = numpy.unique(t.ravel())
        if idx.size == 0:
            continue
        sample = sampler(name)
        if sample is None:
            continue
        if isinstance(sample, numpy.ndarray) and sample.ndim == 3 and uvs is not None:
            h, w = sample.shape[:2]
            uv = uvs[idx]
            u = numpy.mod(uv[:, 0], 1.0)
            v = numpy.mod(uv[:, 1], 1.0)
            col = numpy.clip((u * w).astype(numpy.int64), 0, w - 1)
            row = numpy.clip((v * h).astype(numpy.int64), 0, h - 1)
            texel = sample[row, col].astype(numpy.float32) / 255.0
            if texel.shape[1] == 3:
                texel = numpy.concatenate(
                    [texel, numpy.ones((len(texel), 1), numpy.float32)], axis=1
                )
            colours[idx] = texel[:, :4]
        else:
            flat = numpy.asarray(sample, dtype=numpy.float32).ravel()[:4]
            if flat.size == 3:
                flat = numpy.concatenate([flat, [1.0]])
            colours[idx] = flat
    return colours
