"""Bundle glTF geometry as the meshes a `.dae` is written from, and the wind convention.

Merging, splitting at the per-mesh vertex cap, grouping by material, and the vertex colours a
forest item's wind animation reads."""

import numpy
from csiibundle import collision

from .dae import Mesh


def glb_to_local(verts, normals=None, matrix=None):
    """glTF (RH, Y-up, X mirrored per §2.2) → BeamNG local (Z-up): `(−gx, gz, gy)`. `matrix` is
    the node's world matrix in glTF space, applied first."""
    v = numpy.asarray(verts, dtype=numpy.float64)
    n = None if normals is None else numpy.asarray(normals, dtype=numpy.float64)
    if matrix is not None:
        m = numpy.asarray(matrix, dtype=numpy.float64)
        v = v @ m[:3, :3].T + m[:3, 3]
        if n is not None:
            n = n @ numpy.linalg.inv(m[:3, :3])  # normals by the inverse transpose
    out = numpy.empty_like(v)
    out[:, 0] = -v[:, 0]
    out[:, 1] = v[:, 2]
    out[:, 2] = v[:, 1]
    if n is None:
        return out, None
    nn = numpy.empty_like(n)
    nn[:, 0] = -n[:, 0]
    nn[:, 1] = n[:, 2]
    nn[:, 2] = n[:, 1]
    length = numpy.linalg.norm(nn, axis=1, keepdims=True)
    nn = numpy.where(length > 1e-12, nn / numpy.maximum(length, 1e-12), nn)
    return out, nn


def glb_uvs(uvs):
    """glTF UVs (origin top-left, V down) → COLLADA/BeamNG (V up): `v' = 1 − v`. Torque's DAE
    importer reads texcoords Blender-style; a bundle mesh written as-is renders upside-down
    textures (the standard glTF↔COLLADA flip)."""
    if uvs is None:
        return None
    out = numpy.asarray(uvs, dtype=numpy.float32).copy()
    out[:, 1] = 1.0 - out[:, 1]
    return out


def merge_meshes(name, parts):
    """Concatenate `[(verts, normals, uvs, colors, tris, runs), ...]` into one Mesh with the runs
    re-based; parts without normals/uvs/colors get zeros so the arrays stay rectangular."""
    verts, normals, uvs, colors, tris, runs = [], [], [], [], [], []
    base = 0
    has_n = any(p[1] is not None for p in parts)
    has_uv = any(p[2] is not None for p in parts)
    has_c = any(p[3] is not None for p in parts)
    for v, n, uv, c, t, r in parts:
        v = numpy.asarray(v, dtype=numpy.float32)
        verts.append(v)
        if has_n:
            normals.append(
                numpy.asarray(n, dtype=numpy.float32)
                if n is not None
                else numpy.tile([0, 0, 1], (len(v), 1)).astype(numpy.float32)
            )
        if has_uv:
            uvs.append(
                numpy.asarray(uv, dtype=numpy.float32)
                if uv is not None
                else numpy.zeros((len(v), 2), numpy.float32)
            )
        if has_c:
            colors.append(
                numpy.asarray(c, dtype=numpy.float32)
                if c is not None
                else numpy.ones((len(v), 4), numpy.float32)
            )
        tris.append(numpy.asarray(t, dtype=numpy.int64) + base)
        runs.extend(r)
        base += len(v)
    return Mesh(
        name,
        numpy.concatenate(verts) if verts else numpy.zeros((0, 3), numpy.float32),
        numpy.concatenate(tris) if tris else numpy.zeros((0, 3), numpy.int64),
        _coalesce(runs),
        normals=numpy.concatenate(normals) if normals else None,
        uvs=numpy.concatenate(uvs) if uvs else None,
        colors=numpy.concatenate(colors) if colors else None,
    )


def _coalesce(runs):
    """Adjacent runs of the same material merge; order is preserved (it is the triangle order)."""
    out = []
    for m, c in runs:
        if c <= 0:
            continue
        if out and out[-1][0] == m:
            out[-1] = (m, out[-1][1] + c)
        else:
            out.append((m, c))
    return out


# One `<geometry>` (one TSMesh) may not exceed this many vertices. Load #10 (2026-08-16): the
# full-city level's junctions tore into shards — kerbs across carriageways, long thin triangles
# spanning the tile — exactly in the tiles whose single geometry passed 65,535 vertices (59 tiles,
# up to 167k; the torn interchange at (2936, 2240) is `net_11_8`, 94,001), while the accepted
# region level's largest tile was 62,798 and clean. BeamNG's compiler keeps the whole mesh with
# 32-bit indices in the .cdae (tools/beamng/probe_cdae.py), so the wrap is downstream of it; the
# renderer's per-mesh index buffers are the Torque-lineage 16-bit kind. Cap under the limit with
# room for the compiler's own vertex unsharing (+6 seen on a 154k tile).
MAX_GEOMETRY_VERTS = 60_000


def split_mesh(mesh, max_verts=MAX_GEOMETRY_VERTS):
    """Partition `mesh` into meshes of at most `max_verts` distinct vertices each, walking the
    triangles in order (so a material-grouped mesh keeps its runs contiguous) and compacting each
    part's vertex set. Returns `[mesh]` unchanged when it already fits."""
    if len(mesh.verts) <= max_verts:
        return [mesh]
    tri_mat = numpy.empty(len(mesh.tris), dtype=object)
    start = 0
    for m, c in mesh.runs:
        tri_mat[start : start + c] = m
        start += c
    parts = []
    lo = 0
    n = len(mesh.tris)
    while lo < n:
        # grow the part in blocks; back off with a bisection when a block overshoots
        hi = min(n, lo + max_verts // 3)  # a triangle brings at most 3 new vertices: always fits
        while hi < n:
            nxt = min(n, hi + max_verts // 3)
            if len(numpy.unique(mesh.tris[lo:nxt])) > max_verts:
                a, b = hi, nxt  # fill up: largest end in [hi, nxt) that fits
                while b - a > 1:
                    mid = (a + b) // 2
                    if len(numpy.unique(mesh.tris[lo:mid])) <= max_verts:
                        a = mid
                    else:
                        b = mid
                hi = a
                break
            hi = nxt
        if hi <= lo:
            raise ValueError(f"{mesh.name}: cannot split under {max_verts} vertices")
        used, inverse = numpy.unique(mesh.tris[lo:hi], return_inverse=True)
        tris = inverse.reshape(-1, 3)
        runs = []
        for m in tri_mat[lo:hi]:
            if runs and runs[-1][0] == m:
                runs[-1] = (m, runs[-1][1] + 1)
            else:
                runs.append((m, 1))
        parts.append(
            Mesh(
                f"{mesh.name}_p{len(parts)}",
                mesh.verts[used],
                tris,
                runs,
                normals=None if mesh.normals is None else mesh.normals[used],
                uvs=None if mesh.uvs is None else mesh.uvs[used],
                colors=None if mesh.colors is None else mesh.colors[used],
            )
        )
        lo = hi
    parts[0].name = mesh.name
    return parts


def group_by_material(mesh):
    """Reorder triangles so each material is one contiguous run (fewer `<triangles>` blocks)."""
    if len(mesh.runs) <= len(mesh.materials):
        return mesh
    order = []
    start = 0
    spans = []
    for m, c in mesh.runs:
        spans.append((m, start, start + c))
        start += c
    new_runs = []
    for mat in mesh.materials:
        total = 0
        for m, a, b in spans:
            if m == mat:
                order.append(numpy.arange(a, b))
                total += b - a
        new_runs.append((mat, total))
    idx = numpy.concatenate(order)
    return Mesh(
        mesh.name,
        mesh.verts,
        mesh.tris[idx],
        new_runs,
        normals=mesh.normals,
        uvs=mesh.uvs,
        colors=mesh.colors,
    )


# -- the forest wind convention ----------------------------------------------------------------

# Minimum extent, in metres, for a mesh to work as a collider that holds at speed — BeamNG's own
# rule of thumb. It lived in `csiibundle.collision` as a default argument, which put a BeamNG
# number in the engine-neutral package and let a caller get it without asking; `colmesh_from_lod`
# and `colbox` now REQUIRE it, so the engine supplying it is visible at the call site.
MIN_COLLIDER_THICKNESS = 0.3


def wind_vertex_colours(mesh, up_axis=1, seed=0):
    """`(N, 4)` float32 vertex colours in BeamNG's forest wind convention, from height and radius.

    R is up/down bend, G is per-instance variation, B is flutter, and black is rigid — which is a
    statement about BeamNG's forest shader and about nothing else, so it lives here rather than in
    `csiibundle.collision` where it used to. The trunk search it stands on is a plain mesh query
    with no engine in it and stayed there, as `collision.trunk_group`.

    TUNED.
    """
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    n = len(verts)
    if n == 0:
        return numpy.zeros((0, 4), numpy.float32)
    idx = collision.trunk_group(mesh, up_axis)
    horizontal = [k for k in range(3) if k != up_axis]
    trunk_axis = verts[idx][:, horizontal].mean(axis=0)
    h = verts[:, up_axis]
    h_norm = numpy.clip((h - h.min()) / max(h.max() - h.min(), 1e-6), 0.0, 1.0)
    r = numpy.linalg.norm(verts[:, horizontal] - trunk_axis, axis=1)
    r_norm = numpy.clip(r / max(float(r.max()), 1e-6), 0.0, 1.0)
    trunk_mask = numpy.zeros(n, dtype=bool)
    trunk_mask[idx] = True
    red = numpy.where(trunk_mask, 0.0, h_norm**2)  # bend grows with height, trunk rigid
    green = numpy.full(n, 0.5 + 0.5 * ((seed * 0.6180339887) % 1.0))  # variation phase, per mesh
    blue = numpy.where(trunk_mask, 0.0, r_norm * h_norm)  # flutter at the canopy edge
    return numpy.stack([red, green, blue, numpy.ones(n)], axis=1).astype(numpy.float32)
