"""A closed, blocky silhouette of a mesh — the far level a building is drawn as when it is a
handful of pixels tall and its artist LOD chain has stopped.

Why a voxel shell and not a decimation: every decimator turns a CS2 facade (unwelded window
quads over an open wall) into a see-through lattice, and no distance metric sees the hole
(refactor Phase 10, 2026-08-29). A voxel occupancy filled from the outside is closed by
construction: walls thinner than a cell still occupy it, windows and doors smaller than a cell
are solid, and the open bottom every building has is sealed by treating the ground plane as
occupied. What comes out is the outline the skyline reads at 20 px — a box for a box, an L for
an L, a stepped tower for a stepped tower — coloured per face by the material that put the
voxel there, so an untextured vertex-colour material (BeamNG's `lod_vertcol`) draws it.

Greedy meshing merges coplanar faces of one colour into rectangles, so a plain block is twelve
triangles however fine the grid. Everything here is numpy-only (the Blender adapter runs it too).

DECLARED APPROXIMATION: the shell is the mesh dilated to whole cells, so it stands up to one
cell proud of the true surface — at the distance it is shown that is under a pixel (`cells`
and the switch distance are chosen together by the caller).
"""

import numpy

# Cells along the mesh's longest axis; the cell is never smaller than `MIN_CELL` metres.
DEFAULT_CELLS = 8
MIN_CELL = 1.0

_AXES = ((1, 2), (0, 2), (0, 1))  # the two in-plane axes for a face normal to axis i


def voxelize(verts, tris, tri_ids, cell, origin, shape):
    """Occupancy `(nx, ny, nz)` bool and the per-voxel id `(nx, ny, nz)` int (−1 empty) from
    triangles sampled at about half a cell: every triangle marks the cells its samples land in,
    and the id a cell keeps is the LAST triangle that touched it — good enough for a colour."""
    occupied = numpy.zeros(shape, dtype=bool)
    ids = numpy.full(shape, -1, dtype=numpy.int64)
    v = numpy.asarray(verts, dtype=numpy.float64)
    t = numpy.asarray(tris, dtype=numpy.int64).reshape(-1, 3)
    if len(t) == 0:
        return occupied, ids
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    # samples per triangle from its longest edge, so a big wall gets many and a sliver one
    edge = numpy.maximum.reduce(
        [
            numpy.linalg.norm(b - a, axis=1),
            numpy.linalg.norm(c - b, axis=1),
            numpy.linalg.norm(a - c, axis=1),
        ]
    )
    steps = numpy.clip(numpy.ceil(edge / (0.5 * cell)).astype(int), 1, 64)
    for n in numpy.unique(steps):
        sel = numpy.nonzero(steps == n)[0]
        # barycentric lattice with n+1 points per edge
        i, j = numpy.meshgrid(numpy.arange(n + 1), numpy.arange(n + 1), indexing="ij")
        keep = (i + j) <= n
        u = (i[keep] / n)[None, :]
        w = (j[keep] / n)[None, :]
        pts = (
            a[sel][:, None, :] * (1.0 - u - w)[..., None]
            + b[sel][:, None, :] * u[..., None]
            + c[sel][:, None, :] * w[..., None]
        )
        idx = numpy.floor((pts - origin) / cell).astype(numpy.int64)
        idx = numpy.clip(idx, 0, numpy.asarray(shape) - 1)
        flat = numpy.ravel_multi_index(
            (idx[..., 0].ravel(), idx[..., 1].ravel(), idx[..., 2].ravel()), shape
        )
        occupied.ravel()[flat] = True
        owner = numpy.repeat(numpy.asarray(tri_ids)[sel], idx.shape[1])
        ids.ravel()[flat] = owner
    return occupied, ids


def fill_from_outside(occupied):
    """Occupancy with every cell the OUTSIDE cannot reach marked solid. The bottom slice is
    sealed (a building has no floor), so an open-bottomed shell fills like a closed one."""
    nx, ny, nz = occupied.shape
    padded = numpy.zeros((nx + 2, ny + 2, nz + 2), dtype=bool)
    padded[1:-1, 1:-1, 1:-1] = occupied
    padded[:, :, 0] = True  # the ground seals the bottom
    outside = numpy.zeros_like(padded)
    outside[0, :, :] = outside[-1, :, :] = True
    outside[:, 0, :] = outside[:, -1, :] = True
    outside[:, :, -1] = True
    outside &= ~padded
    while True:
        grown = outside.copy()
        grown[1:, :, :] |= outside[:-1, :, :]
        grown[:-1, :, :] |= outside[1:, :, :]
        grown[:, 1:, :] |= outside[:, :-1, :]
        grown[:, :-1, :] |= outside[:, 1:, :]
        grown[:, :, 1:] |= outside[:, :, :-1]
        grown[:, :, :-1] |= outside[:, :, 1:]
        grown &= ~padded
        if numpy.array_equal(grown, outside):
            break
        outside = grown
    solid = ~outside
    return solid[1:-1, 1:-1, 1:-1]


def _greedy_rectangles(mask, value):
    """Maximal same-value rectangles over a 2-D boolean mask: `[(r0, c0, h, w, value)]`."""
    mask = mask.copy()
    out = []
    rows, cols = mask.shape
    for r in range(rows):
        c = 0
        while c < cols:
            if not mask[r, c]:
                c += 1
                continue
            val = value[r, c]
            w = 1
            while c + w < cols and mask[r, c + w] and value[r, c + w] == val:
                w += 1
            h = 1
            while (
                r + h < rows
                and numpy.all(mask[r + h, c : c + w])
                and numpy.all(value[r + h, c : c + w] == val)
            ):
                h += 1
            mask[r : r + h, c : c + w] = False
            out.append((r, c, h, w, val))
            c += w
    return out


def shell_faces(solid, ids, cell, origin):
    """Every exposed face of `solid`, greedily merged: `(verts (N,3), normals (N,3), tris (M,3),
    face_ids (M,))` with `face_ids` the voxel id behind each triangle."""
    verts, normals, tris, face_ids = [], [], [], []
    base = 0
    for axis in range(3):
        p, q = _AXES[axis]
        n = solid.shape[axis]
        for direction in (1, -1):
            for k in range(n):
                here = numpy.take(solid, k, axis=axis)
                beyond_index = k + direction
                if 0 <= beyond_index < n:
                    beyond = numpy.take(solid, beyond_index, axis=axis)
                else:
                    beyond = numpy.zeros_like(here)
                exposed = here & ~beyond
                if not exposed.any():
                    continue
                value = numpy.take(ids, k, axis=axis)
                plane = (k + (1 if direction > 0 else 0)) * cell + origin[axis]
                for r0, c0, h, w, val in _greedy_rectangles(exposed, value):
                    lo_p, hi_p = origin[p] + r0 * cell, origin[p] + (r0 + h) * cell
                    lo_q, hi_q = origin[q] + c0 * cell, origin[q] + (c0 + w) * cell
                    corners = numpy.zeros((4, 3))
                    corners[:, axis] = plane
                    corners[:, p] = [lo_p, hi_p, hi_p, lo_p]
                    corners[:, q] = [lo_q, lo_q, hi_q, hi_q]
                    normal = numpy.zeros(3)
                    normal[axis] = float(direction)
                    # wind so the face normal (cross of the first two edges) points `direction`
                    order = [0, 1, 2, 3]
                    e1 = corners[1] - corners[0]
                    e2 = corners[2] - corners[0]
                    if numpy.dot(numpy.cross(e1, e2), normal) < 0:
                        order = [0, 3, 2, 1]
                    verts.append(corners[order])
                    normals.append(numpy.repeat(normal[None, :], 4, axis=0))
                    tris.append([[base, base + 1, base + 2], [base, base + 2, base + 3]])
                    face_ids.extend([int(val), int(val)])
                    base += 4
    if not verts:
        return (
            numpy.zeros((0, 3)),
            numpy.zeros((0, 3)),
            numpy.zeros((0, 3), dtype=numpy.int64),
            numpy.zeros(0, dtype=numpy.int64),
        )
    return (
        numpy.concatenate(verts),
        numpy.concatenate(normals),
        numpy.concatenate(tris).astype(numpy.int64),
        numpy.asarray(face_ids, dtype=numpy.int64),
    )


def silhouette(verts, tris, tri_colour_ids, colours, cells=DEFAULT_CELLS, min_cell=MIN_CELL):
    """The far level: `(verts, normals, colours_rgba (N,4), tris, stats)` in the input's frame
    (up is +Z — the ground seal is on the bottom Z slice).

    `tri_colour_ids` is one int per triangle indexing `colours` (K,3) in 0..1; a face takes the
    colour of the voxel behind it. `cells` cells along the longest axis, cell ≥ `min_cell`.
    An empty input returns empty arrays and `stats["cells"] == 0`.
    """
    v = numpy.asarray(verts, dtype=numpy.float64).reshape(-1, 3)
    t = numpy.asarray(tris, dtype=numpy.int64).reshape(-1, 3)
    if len(v) == 0 or len(t) == 0:
        empty = numpy.zeros((0, 3))
        return (
            empty,
            empty,
            numpy.zeros((0, 4)),
            numpy.zeros((0, 3), dtype=numpy.int64),
            {"cells": 0},
        )
    lo, hi = v.min(axis=0), v.max(axis=0)
    extent = hi - lo
    cell = max(float(min_cell), float(extent.max()) / max(int(cells), 1))
    shape = tuple(int(x) for x in numpy.maximum(numpy.ceil(extent / cell), 1).astype(int))
    origin = lo
    occupied, ids = voxelize(
        v, t, numpy.asarray(tri_colour_ids, dtype=numpy.int64), cell, origin, shape
    )
    solid = fill_from_outside(occupied)
    # a filled cell with no id (interior) takes the nearest marked one below/beside it: use the
    # colour of any occupied cell; fall back to id 0
    if (ids < 0).any():
        ids = numpy.where(ids < 0, _nearest_ids(ids, occupied), ids)
    face_verts, face_normals, face_tris, face_ids = shell_faces(solid, ids, cell, origin)
    # the grid rounds up to whole cells; the shell never stands outside the mesh's own box
    face_verts = numpy.clip(face_verts, lo, hi)
    palette = numpy.asarray(colours, dtype=numpy.float64).reshape(-1, 3)
    if len(palette) == 0:
        palette = numpy.array([[0.5, 0.5, 0.5]])
    per_tri = palette[numpy.clip(face_ids, 0, len(palette) - 1)]
    per_vertex = numpy.zeros((len(face_verts), 4))
    per_vertex[:, 3] = 1.0
    # every face's four vertices are its own (flat), so the triangle colour lands on them
    for tri, rgb in zip(face_tris, per_tri):
        per_vertex[tri, :3] = rgb
    stats = {
        "cells": int(solid.sum()),
        "cell": round(cell, 3),
        "grid": list(shape),
        "triangles": int(len(face_tris)),
    }
    return face_verts, face_normals, per_vertex, face_tris, stats


def _nearest_ids(ids, occupied):
    """For cells with no id, the id of the nearest occupied cell (Manhattan, by dilation)."""
    out = ids.copy()
    known = occupied & (ids >= 0)
    for _ in range(max(ids.shape)):
        missing = out < 0
        if not missing.any():
            break
        for shift_axis in range(3):
            for direction in (1, -1):
                rolled = numpy.roll(out, direction, axis=shift_axis)
                rolled_known = numpy.roll(known, direction, axis=shift_axis)
                take = missing & rolled_known & (rolled >= 0)
                out[take] = rolled[take]
                missing = out < 0
        known = out >= 0
    out[out < 0] = 0
    return out
