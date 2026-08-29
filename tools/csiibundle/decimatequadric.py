"""The optional quadric decimation backend behind `decimate.lod_ladder` — `pyfqmr`
(Fast-Quadric-Mesh-Simplification, the `[lods]` extra) when it imports, nothing otherwise.

Split out of `decimate` so the numpy-only clustering that every consumer runs does not carry
an optional dependency's code. `available()` is the whole test; `to_ratio` has the same
signature as the cluster reducer and re-attaches UVs and normals by nearest original vertex,
which is fine at the distances a LOD is seen.
"""

import numpy


def available():
    try:
        import pyfqmr  # noqa: F401

        return True
    except Exception:
        return False


def to_ratio(verts, tris, uvs, normals, ratio):
    import pyfqmr

    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    target = max(1, int(round(len(tris) * ratio)))
    if len(tris) <= 4 or target >= len(tris):
        return verts, normals, uvs, tris
    simplifier = pyfqmr.Simplify()
    simplifier.setMesh(verts, tris)
    # `preserve_border=False` is deliberate. A CS2 mesh is split per material run and never
    # welded across one, so on the reference bundle nearly every edge is a border: with the
    # borders pinned the simplifier returned 17,809 of 18,017 triangles at a 25 % target, and
    # every "LOD" was LOD 0 again (refactor Phase 10, 2026-08-29). Unpinned, the same mesh
    # reached 4,504.
    simplifier.simplify_mesh(
        target_count=target, aggressiveness=7, preserve_border=False, verbose=0
    )
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


def _nearest_index(query, points):
    """Index into `points` of each `query` row's nearest neighbour.

    A k-d tree, not the chunked brute force this used to be: that built a `(2048, N, 3)` float64
    temporary per chunk, which on a 100k-vertex building is 5 GB of memory traffic per chunk and
    made the quadric backend SLOWER than the numpy clustering it was meant to replace — twelve
    sample meshes took the cluster backend 24 s and the quadric backend more than 10 minutes
    (refactor Phase 10, 2026-08-29).
    """
    from scipy.spatial import cKDTree

    _, index = cKDTree(points).query(query, k=1, workers=1)
    return numpy.asarray(index, dtype=numpy.int64)
