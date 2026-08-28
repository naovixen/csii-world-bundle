"""The degenerate-face policy — what to do about the faces that are not faces.

**Why this is not left to the mesh library.** Every validator this repo has met drops bad
polygons POSITIONALLY and says nothing, so afterwards a caller has no way to tell which of
ITS polygons a material run or a UV block was meant for. Re-zipping runs onto whatever
survived silently mis-assigns materials rather than failing loudly — so the drop happens
here, before the mesh is built, with the runs and UV blocks trimmed in the same pass.
Concatenated per-part arrays (lane meshes assembled from bent segments) are exactly the
shape most likely to contain one at a seam.

**The two classes are not equally diagnostic and are counted separately:**

* `repeated_count` — a polygon with the SAME vertex index twice. In geometry a consumer just
  computed (a bend, a grid, a drape) this is the classic sign that the computation broke,
  and it deserves to be loud.
* `zero_area_count` — distinct indices that still collapse to a line or a point. This one has
  a well-understood non-defect source in computed lane geometry (a heavily tiled part's last
  tile can clip a whole cross-section to a single arc position), and another in geometry read
  verbatim off a `.glb`, where it is a faithful rip of what the game's own mesh contained.

Neither count is a verdict. What to do with them depends on where the arrays came from,
which only the caller knows — that is why this module reports and does not warn.
"""

import numpy


def _degenerate_masks(verts, loops_flat, sizes, offsets):
    """`(repeated, area2)` per polygon, computed group-wise by polygon size.

    Fan-triangulated from vertex 0 (v0, v[i], v[i+1]) so the area test covers any polygon
    size. Sizes are processed group-wise so a mixed-size mesh (the terrain grid: 14 m quads,
    3.5 m quads and transition-fan triangles in one buffer) shares the uniform path's
    arithmetic instead of needing its own.
    """
    poly_count = len(sizes)
    repeated = numpy.zeros(poly_count, dtype=bool)
    area2 = numpy.zeros(poly_count)
    for size in numpy.unique(sizes):
        group = numpy.nonzero(sizes == size)[0]
        rows = loops_flat[offsets[group][:, None] + numpy.arange(size)[None, :]]
        rep = numpy.zeros(len(group), dtype=bool)
        for i in range(size):
            for j in range(i + 1, size):
                rep |= rows[:, i] == rows[:, j]
        v0 = verts[rows[:, 0]]
        a2 = numpy.zeros(len(group))
        for i in range(1, size - 1):
            edge_a = verts[rows[:, i]] - v0
            edge_b = verts[rows[:, i + 1]] - v0
            a2 += numpy.linalg.norm(numpy.cross(edge_a, edge_b), axis=1)
        repeated[group] = rep
        area2[group] = a2
    return repeated, area2


def _trim_runs(material_runs, keep):
    """`material_runs` re-run-length-encoded over the kept polygons, in polygon order."""
    expanded = []
    for material, count in material_runs:
        expanded.extend([material] * count)
    trimmed = []
    for material, kept in zip(expanded, keep):
        if not kept:
            continue
        if trimmed and trimmed[-1][0] is material:
            trimmed[-1] = (material, trimmed[-1][1] + 1)
        else:
            trimmed.append((material, 1))
    return trimmed


def clean_degenerate_polygons(verts, loops_flat, sizes, uv_layers=None, material_runs=None):
    """Drop polygons that carry no area, keeping materials and UVs aligned to the survivors.

    `verts` is `(N, 3)`; `loops_flat` is a flat array of loop vertex indices; `sizes` is `(M,)`
    per-polygon loop counts summing to `len(loops_flat)`. `uv_layers` is
    `{name: (len(loops_flat), 2)}` in the same loop order; `material_runs` is
    `[(material, polygon_count)]` consumed in polygon order.

    Returns `(loops_flat, sizes, uv_layers, material_runs, repeated_count, zero_area_count)`.
    The policy — why the drop happens here and what the two counts mean — is the module
    docstring.
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    loops_flat = numpy.asarray(loops_flat, dtype=numpy.int64)
    sizes = numpy.asarray(sizes, dtype=numpy.int64)
    if not len(sizes):
        return loops_flat, sizes, uv_layers, material_runs, 0, 0

    offsets = numpy.concatenate(([0], numpy.cumsum(sizes)))
    repeated, area2 = _degenerate_masks(verts, loops_flat, sizes, offsets)
    zero_area = (~repeated) & (area2 < 1e-12)  # TUNED: below any real mesh's face area
    degenerate = repeated | zero_area
    repeated_count = int(repeated.sum())
    zero_area_count = int(zero_area.sum())
    if not degenerate.any():
        return loops_flat, sizes, uv_layers, material_runs, 0, 0

    keep = ~degenerate
    if material_runs:
        material_runs = _trim_runs(material_runs, keep)
    loop_keep = numpy.repeat(keep, sizes)
    if uv_layers:
        uv_layers = {
            layer_name: numpy.asarray(uvs)[loop_keep] for layer_name, uvs in uv_layers.items()
        }

    return (
        loops_flat[loop_keep],
        sizes[keep],
        uv_layers,
        material_runs,
        repeated_count,
        zero_area_count,
    )
