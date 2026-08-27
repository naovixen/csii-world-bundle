"""Colliders for an engine that wants simple ones — and the thickness rule that makes them work.

Every physics engine's guidance: collide with something SIMPLER than the visible mesh, keep it
thick enough that a wheel at speed cannot tunnel through or snag on it, and never build a
collider out of paint. This module turns a mesh (or its LOD) into that:

* `thickness` — the object's smallest extent along its principal axes; a fence panel or a sign
  is thin, a building or a tree trunk is not.
* `colmesh_from_lod` — a decimated LOD as the collider when the shape is thick enough, else None
  (the caller falls back to `colbox`).
* `colbox` — the bounding box as 12 triangles; the collider for thin or tiny things.
* `trunk_group` — the vertices that make up a tree's trunk, by material name or by footprint.
* `trunk_cylinder` — a tree's collider: an 8-sided cylinder around the trunk sub-mesh, so a car
  stops at the trunk and drives through the canopy the way it does in every driving game.

**How thick is thick enough is the ENGINE's number, and it is not here.** `colmesh_from_lod` and
`colbox` require `min_thickness` from the caller. It used to default to BeamNG's ~0.3 m rule of
thumb, which put one engine's tuning in the package both consumers share and let a caller take it
without knowing it had; `cs2beamng` states it as `beamng.dae.MIN_COLLIDER_THICKNESS`.
`wind_vertex_colours` left for the same reason — the R/G/B channel meanings are BeamNG's forest
shader's, and it now lives in `beamng/dae.py` and calls `trunk_group` here.

Everything takes and returns arrays in whatever frame the caller holds (positions only matter
relative to each other), so it works on glTF-space `parse_glb` output directly.
"""

import numpy

CYLINDER_SIDES = 8


def bounds(verts):
    verts = numpy.asarray(verts, dtype=numpy.float64)
    if len(verts) == 0:
        return numpy.zeros(3), numpy.zeros(3)
    return verts.min(axis=0), verts.max(axis=0)


def thickness(verts, tris=None):
    """The smallest extent of the vertex cloud along its principal axes (metres).

    PCA on the positions (SVD of the centred cloud) so a wall rotated 30° in plan is still
    measured across its thin direction. `tris` is accepted for API symmetry and unused: the
    hull's thickness is a property of the points.
    """
    v = numpy.asarray(verts, dtype=numpy.float64)
    if len(v) < 3:
        return 0.0
    centred = v - v.mean(axis=0)
    # principal axes; project and take the smallest range
    try:
        _, _, vt = numpy.linalg.svd(centred, full_matrices=False)
    except numpy.linalg.LinAlgError:
        lo, hi = bounds(v)
        return float((hi - lo).min())
    projected = centred @ vt.T
    extents = projected.max(axis=0) - projected.min(axis=0)
    return float(extents.min())


def colmesh_from_lod(mesh, min_thickness):
    """The mesh itself (dict with `verts`/`tris`) when it is thick enough to be a collider, else
    None. Pass a decimated LOD (`decimate.lod_ladder`'s ~25 % level), not LOD 0."""
    if mesh is None or len(mesh.get("tris", ())) == 0:
        return None
    if thickness(mesh["verts"]) < min_thickness:
        return None
    return {
        "verts": numpy.asarray(mesh["verts"], dtype=numpy.float32),
        "tris": numpy.asarray(mesh["tris"], dtype=numpy.int64),
    }


def colbox(bounds_min, bounds_max, min_thickness):
    """An axis-aligned box as `(verts (8,3), tris (12,3))`, each side at least `min_thickness`
    (thin extents are padded symmetrically so a sign or a fence panel still holds a wheel)."""
    lo = numpy.asarray(bounds_min, dtype=numpy.float64).copy()
    hi = numpy.asarray(bounds_max, dtype=numpy.float64).copy()
    for k in range(3):
        if hi[k] - lo[k] < min_thickness:
            mid = 0.5 * (hi[k] + lo[k])
            lo[k], hi[k] = mid - 0.5 * min_thickness, mid + 0.5 * min_thickness
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    verts = numpy.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ],
        dtype=numpy.float32,
    )
    tris = numpy.array(
        [
            [0, 2, 1],
            [0, 3, 2],  # bottom (z0), facing -z
            [4, 5, 6],
            [4, 6, 7],  # top
            [0, 1, 5],
            [0, 5, 4],  # y0 side
            [1, 2, 6],
            [1, 6, 5],  # x1 side
            [2, 3, 7],
            [2, 7, 6],  # y1 side
            [3, 0, 4],
            [3, 4, 7],
        ],
        dtype=numpy.int64,
    )  # x0 side
    return verts, tris


def trunk_group(mesh, up_axis=1):
    """Index array of the vertices most likely to be the trunk: the material run whose name
    says so, else the run with the smallest horizontal footprint that reaches the base."""
    tris = numpy.asarray(mesh["tris"], dtype=numpy.int64)
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    runs = mesh.get("material_runs") or [(None, len(tris))]
    groups = []
    cursor = 0
    for name, count in runs:
        idx = numpy.unique(tris[cursor : cursor + count].ravel())
        cursor += count
        if idx.size:
            groups.append((name or "", idx))
    if not groups:
        return numpy.arange(len(verts))
    for name, idx in groups:
        lname = name.lower()
        if "trunk" in lname or "bark" in lname or "stem" in lname or "wood" in lname:
            return idx
    base = verts[:, up_axis].min()
    horizontal = [k for k in range(3) if k != up_axis]

    def score(idx):
        pts = verts[idx]
        reaches_base = pts[:, up_axis].min() <= base + 0.5
        footprint = float((pts[:, horizontal].max(axis=0) - pts[:, horizontal].min(axis=0)).max())
        return (0 if reaches_base else 1, footprint)

    return min(groups, key=lambda g: score(g[1]))[1]


def trunk_cylinder(mesh, up_axis=1, min_radius=0.15, sides=CYLINDER_SIDES):
    """`(verts, tris)` of an `sides`-gon prism around the trunk sub-mesh of a tree mesh, from
    its base to its top; radius = the trunk group's horizontal half-extent at its base band,
    floored at `min_radius`."""
    verts = numpy.asarray(mesh["verts"], dtype=numpy.float64)
    idx = trunk_group(mesh, up_axis)
    pts = verts[idx]
    horizontal = [k for k in range(3) if k != up_axis]
    y0, y1 = float(pts[:, up_axis].min()), float(pts[:, up_axis].max())
    band = pts[pts[:, up_axis] <= y0 + max(0.5, 0.15 * (y1 - y0))]
    if len(band) < 3:
        band = pts
    centre = band[:, horizontal].mean(axis=0)
    radius = max(min_radius, float(numpy.linalg.norm(band[:, horizontal] - centre, axis=1).max()))
    angles = numpy.linspace(0, 2 * numpy.pi, sides, endpoint=False)
    ring = numpy.stack([numpy.cos(angles), numpy.sin(angles)], axis=1) * radius + centre
    out = numpy.zeros((2 * sides, 3), dtype=numpy.float32)
    out[:sides, horizontal[0]] = ring[:, 0]
    out[:sides, horizontal[1]] = ring[:, 1]
    out[:sides, up_axis] = y0
    out[sides:, horizontal[0]] = ring[:, 0]
    out[sides:, horizontal[1]] = ring[:, 1]
    out[sides:, up_axis] = y1
    tris = []
    for i in range(sides):
        j = (i + 1) % sides
        tris.append([i, j, sides + j])
        tris.append([i, sides + j, sides + i])
    # caps
    for i in range(1, sides - 1):
        tris.append([0, i + 1, i])
        tris.append([sides, sides + i, sides + i + 1])
    return out, numpy.asarray(tris, dtype=numpy.int64)
