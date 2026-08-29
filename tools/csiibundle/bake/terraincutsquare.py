"""One heightfield square cut against the deep prisms that reach it — the `terraincut`
product's per-square work, in the shape a worker process runs it.

Everything a square reads from the rasters — its four corner heights, gradients and painted
layers — arrives inline with the square (`cut_batch`), so a worker holds no 4096² array; what
comes back is the square's kept mesh with its triplanar UVs, heightfield normals and layer per
face, which the product hands to its tile sink in square order.
"""

import numpy

from .. import terrainclip

# metres per texture repeat on the ground mesh: the terrain material's MACRO tier period
GROUND_UV_M = 40.0
GROUND_FALLBACK = "ground:grass"
# a kept piece smaller than this is a rounding sliver of the cut, not ground anybody sees
MIN_PIECE_M2 = 1e-4
# A square that loses less ground than this is treated as untouched and left to the heightfield.
# The prism only grazed it — a ribbon a few millimetres wide, which the cut drops anyway
# (`terrainclip.MIN_WIDTH_M`) — and punching it would trade a terrain-shaded square for a
# mesh-shaded one over 0.3 % of its area. On the reference city the threshold is the difference
# between 46,550 punched squares and 43,043.
MIN_REMOVED_M2 = 0.01

# Which plane a face's ground texture is projected in. PORTED, with a declared collapse.
#
# CS2's terrain samples its grounds TRIPLANAR. `Game.Rendering.TerrainMaterialSystem` sets two
# shader globals from the theme's `TerrainRenderSettingsPrefab`, beside `_SplatMultiplier` and
# the `_TerrainExtra*` set the rest of this bake already ports:
#
#     _TriplanarHeightmapBlending      <- m_TriplanarHeightMapBlending  (class default 0.5)
#     _TerrainTriplanarBlendStrengthY  <- m_TriplanarBlendStrengthY     (class default 0.2)
#
# and the producer already ships both, per map, as `rasters.terrainTiling.triplanar*` — on the
# reference city's `DefaultTerrainRenderSettings` they are **0.25 and 0.75**, not the class
# defaults. So a steep face in the game is textured from its SIDE projection, not smeared from
# above.
# A pure `(x, z)` projection stretches a face by `1/cos(slope)`: measured on the built level,
# 11.6 % of the cut mesh's area sits on faces at 85 deg or more (real 10-13 m steps between
# adjacent 1.75 m samples, where CS2 graded a corridor into a hillside), and 11.0 % of the area
# samples the ground at 4-10x the intended period. A 4-10x magnified 68 m rock tiling is one
# texel over a whole wall, which renders as a flat single-colour slab — the cream and near-black
# patches of load #26. Predicted colour for the worst of them (198, 186, 172) against (200, 186,
# 173) measured off the screenshot; audit §61.
#
# APPROXIMATED, declared and counted: the game blends the three projections PER PIXEL, weighted
# by the two globals above and by the grounds' own height maps. Those weights live in an HLSL
# shader that is not in the managed assemblies, and a mesh carries ONE baked UV set, so this
# collapses the blend to the single dominant plane PER FACE — `argmax(|n|)`, the limit every
# triplanar formulation agrees on, and the choice with no density error at the 45 deg crossover
# (both planes stretch by 1.41 there). The cost is a texture-pattern discontinuity across an edge
# whose two faces pick different planes; the run report counts the faces and area per plane.
# The shipped `triplanarBlendStrengthY` is deliberately NOT used as a weight: whether it scales
# the Y projection up, scales it down, or is an exponent is not determinable without the shader,
# and reading it as a Y weight would move the crossover to 36.9 deg (at 0.75) or 11.3 deg (at the
# class default 0.2) and send near-flat ground to a side projection. When the weighting curve is
# recovered, that is the upgrade path — and it needs the multi-stage `inLayer.opacity` mechanism,
# not a different UV.
# Faces flatter than 45 deg keep the `(x, z)` projection they had, so 83 % of the mesh is
# unchanged.
TRIPLANAR_PLANES = ((2, 1), (0, 2), (0, 1))  # per dominant normal axis x/y/z: the two axes kept


def cut_batch(squares, by_face, prisms, rasters, frame):
    """The worker's view of a batch of squares: every raster value a square reads, inline."""
    ground_y, gx, gz, layer_of, corner_names = rasters
    rows = numpy.asarray([r for r, _c in squares], dtype=numpy.int64)
    cols = numpy.asarray([c for _r, c in squares], dtype=numpy.int64)
    corner_r = numpy.stack([rows, rows, rows + 1, rows + 1], axis=1)  # nw ne sw se
    corner_c = numpy.stack([cols, cols + 1, cols, cols + 1], axis=1)
    heights = ground_y[corner_r, corner_c].astype(numpy.float64)
    grad_x = gx[corner_r, corner_c].astype(numpy.float64)
    grad_z = gz[corner_r, corner_c].astype(numpy.float64)
    # the four corners' layer ids, named per square — never the whole raster as names
    names = layer_of[corner_r, corner_c]
    items = []
    for i, (r, c) in enumerate(squares):
        items.append(
            (
                r,
                c,
                heights[i].tolist(),
                grad_x[i].tolist(),
                grad_z[i].tolist(),
                [corner_names[k] for k in names[i].tolist()],
                [prisms[j] for j in by_face[(r, c)]],
            )
        )
    return items, frame


def cut_chunk(args):
    """Worker: cut one batch of squares (`_cut_batch`) against the prisms that reach them."""
    items, (cell, origin_x, origin_z, width, height) = args
    out = {
        "seen": len(items),
        "stats": {},
        "plane_tris": [0, 0, 0],
        "plane_area": [0.0, 0.0, 0.0],
        "squares": [],
    }
    for r, c, heights, grad_x, grad_z, names, local in items:
        cut = _cut_square(r, c, cell, origin_x, origin_z, heights, local, out["stats"])
        if cut is None:
            continue
        removed, kept = cut
        if not kept:
            out["squares"].append((r, c, removed, 0, None, None, None, None, None))
            continue
        pieces, verts, tris, uvs = _square_mesh(kept, out)
        normals = _corner_normals(
            verts, r, c, cell, origin_x, origin_z, width, height, grad_x, grad_z
        )
        tri_mat = _corner_layers(verts[tris].mean(axis=1), r, c, cell, origin_x, origin_z, names)
        out["squares"].append((r, c, removed, pieces, verts, tris, uvs, normals, tri_mat))
    return out


def _cut_square(r, c, cell, origin_x, origin_z, heights, local, stats):
    """`(removed m², kept polygons)` for one square, or None when the prism only grazed it."""
    # the square's four corners, north-west first, at the consumer's own heights
    x0 = origin_x + c * cell
    x1 = x0 + cell
    z0 = origin_z - r * cell
    z1 = z0 - cell
    nw = (x0, heights[0], z0)
    ne = (x1, heights[1], z0)
    sw = (x0, heights[2], z1)
    se = (x1, heights[3], z1)
    whole = cell * cell
    kept = []
    for tri in ((nw, ne, se), (nw, se, sw)):  # the NW-SE diagonal, as `drape` assumes
        kept.extend(terrainclip.clip_triangle(tri, local, stats))
    areas = [abs(terrainclip.polygon_area([(q[0], q[2]) for q in p])) for p in kept]
    kept = [p for p, a in zip(kept, areas) if a > MIN_PIECE_M2]
    kept_area = sum(a for a in areas if a > MIN_PIECE_M2)
    if whole - kept_area < MIN_REMOVED_M2:
        return None
    return whole - kept_area, kept


def _square_mesh(kept, out):
    """Fan the kept polygons into an upward-facing mesh with triplanar UVs; counts the planes."""
    verts = []
    tris = []
    pieces = 0
    for poly in kept:
        base = len(verts)
        verts.extend(poly)
        for a, b, d in terrainclip.fan(poly):
            tris.append((base + a, base + b, base + d))
        pieces += 1
    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    # every ground face looks up
    a, b, d = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    nrm = numpy.cross(b - a, d - a)
    flip = nrm[:, 1] < 0
    tris[flip] = tris[flip][:, ::-1]
    # triplanar: project each face in the plane it is most parallel to (see TRIPLANAR_PLANES)
    corners = verts[tris]  # (M, 3, 3)
    dominant = numpy.abs(nrm).argmax(axis=1)
    uvs = numpy.empty((len(tris), 3, 2), dtype=numpy.float64)
    for axis, (u_ax, v_ax) in enumerate(TRIPLANAR_PLANES):
        on = dominant == axis
        if not on.any():
            continue
        uvs[on, :, 0] = corners[on, :, u_ax] / GROUND_UV_M
        uvs[on, :, 1] = corners[on, :, v_ax] / GROUND_UV_M
        out["plane_tris"][axis] += int(on.sum())
        out["plane_area"][axis] += float(0.5 * numpy.linalg.norm(nrm[on], axis=1).sum())
    return pieces, verts, tris, uvs


def _corner_normals(verts, r, c, cell, origin_x, origin_z, width, height, grad_x, grad_z):
    """The heightfield's normal at each vertex, bilinear over the square's corner gradients —
    the same clip-and-interpolate `raster_normals` did over the whole raster, so a vertex on the
    square's far edge reads the far corner exactly as before."""
    cf = numpy.clip((verts[:, 0] - origin_x) / cell, 0, width - 1.001)
    rf = numpy.clip((origin_z - verts[:, 2]) / cell, 0, height - 1.001)
    ci = cf.astype(numpy.int64) - c  # 0, or 1 on the square's east edge
    ri = rf.astype(numpy.int64) - r
    fu = numpy.where(ci == 0, cf - c, 0.0)
    fv = numpy.where(ri == 0, rf - r, 0.0)
    # corner order nw ne sw se: the value at (row offset, col offset)
    gxa = numpy.asarray(grad_x).reshape(2, 2)
    gza = numpy.asarray(grad_z).reshape(2, 2)

    def bil(g):
        r0 = numpy.where(ci == 0, g[0, 0] * (1 - fu) + g[0, 1] * fu, g[0, 1])
        r1 = numpy.where(ci == 0, g[1, 0] * (1 - fu) + g[1, 1] * fu, g[1, 1])
        return numpy.where(ri == 0, r0 * (1 - fv) + r1 * fv, r1)

    dx, dz = bil(gxa), bil(gza)
    n = numpy.stack((-dx, numpy.ones_like(dx), dz), axis=-1)
    return n / numpy.linalg.norm(n, axis=1)[:, None]


def _corner_layers(centres, r, c, cell, origin_x, origin_z, names):
    """The painted layer under each face centre — the nearest sample, which for a centre inside
    the square is one of its four corners."""
    rr = numpy.clip(numpy.rint((origin_z - centres[:, 2]) / cell).astype(numpy.int64) - r, 0, 1)
    cc = numpy.clip(numpy.rint((centres[:, 0] - origin_x) / cell).astype(numpy.int64) - c, 0, 1)
    table = numpy.asarray(names, dtype=object).reshape(2, 2)
    return table[rr, cc]
