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

Nothing here is CONTRACT: the ratios, the UV bin, the cluster search are all TUNED and a
consumer may replace the ladder with authored LODs the moment the producer ships them.
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
    except Exception:  # noqa: BLE001
        return False


def split_runs(mesh):
    """`[(material_name, verts, normals, uvs, tris_local)]` — one compact sub-mesh per material run."""
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    normals = None if mesh.get("normals") is None else numpy.asarray(mesh["normals"], dtype=numpy.float64)
    uvs = None if mesh.get("uvs") is None else numpy.asarray(mesh["uvs"], dtype=numpy.float64)
    tris = numpy.asarray(mesh["tris"], dtype=numpy.int64)
    out = []
    cursor = 0
    for name, count in mesh.get("material_runs") or [(None, len(tris))]:
        t = tris[cursor:cursor + count]
        cursor += count
        if len(t) == 0:
            continue
        used, inverse = numpy.unique(t.ravel(), return_inverse=True)
        out.append((name, verts[used], None if normals is None else normals[used],
                    None if uvs is None else uvs[used], inverse.reshape(-1, 3)))
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
        return {"verts": numpy.zeros((0, 3)), "normals": numpy.zeros((0, 3)) if has_n else None,
                "uvs": numpy.zeros((0, 2)) if has_uv else None, "tris": numpy.zeros((0, 3), numpy.int64),
                "material_runs": []}
    return {"verts": numpy.concatenate(verts).astype(numpy.float32),
            "normals": numpy.concatenate(normals).astype(numpy.float32) if has_n else None,
            "uvs": numpy.concatenate(uvs).astype(numpy.float32) if has_uv else None,
            "tris": numpy.concatenate(tris), "material_runs": runs}


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
        columns.append(numpy.where(pins, numpy.arange(len(verts), dtype=numpy.int64),
                                   numpy.int64(-1)))
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
    keep = (new_tris[:, 0] != new_tris[:, 1]) & (new_tris[:, 1] != new_tris[:, 2]) & (new_tris[:, 0] != new_tris[:, 2])
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
            lo = mid          # too many faces: bigger cells
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
        q = query[s:s + chunk]
        d = ((q[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)
        out[s:s + chunk] = d.argmin(axis=1)
    return out


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
    stats = {"backend": "quadric" if use_quadric else "cluster",
             "input_tris": int(len(mesh["tris"])),
             "levels": [int(len(level["tris"])) for level in levels],
             "dropped_runs": dropped}
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
        t = tris[cursor:cursor + count]
        cursor += count
        if len(t) == 0:
            continue
        used, inverse = numpy.unique(t.ravel(), return_inverse=True)
        sub_v = verts[used]
        sub_n = None if mesh.get("normals") is None else numpy.asarray(mesh["normals"])[used]
        sub_uv = None if mesh.get("uvs") is None else numpy.asarray(mesh["uvs"])[used]
        sub_pin = None if pins is None else pins[used]
        out_v, out_n, out_uv, out_t = _cluster_to_ratio(sub_v, inverse.reshape(-1, 3), sub_uv,
                                                        sub_n, ratio, pinned=sub_pin)
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
        t = tris[cursor:cursor + count]
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
                texel = numpy.concatenate([texel, numpy.ones((len(texel), 1), numpy.float32)], axis=1)
            colours[idx] = texel[:, :4]
        else:
            flat = numpy.asarray(sample, dtype=numpy.float32).ravel()[:4]
            if flat.size == 3:
                flat = numpy.concatenate([flat, [1.0]])
            colours[idx] = flat
    return colours
