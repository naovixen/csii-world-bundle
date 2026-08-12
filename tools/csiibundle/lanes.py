"""The lane bend (§5B v2) — how a lane record plus a part catalogue become geometry.

`lanemeshes.jsonl` records are **bend parameters, not baked meshes**. The straight part meshes
live once under `lanemeshes/`, `laneprefabs.json` says how each lane prefab assembles them, and
the consumer bends each record's parts along its Bézier. That is a deliberate choice by the
producer — a full city's fences, hedges, cables, sleepers and markings baked out would be
hundreds of millions of vertices, and the game itself does not store them either: it bends in
the vertex shader, per tile, every frame.

`bend_part` below is the reference implementation of that bend, ported from the producer's
former baking code, which in turn reproduced the game's `BatchDataSystem.UpdateLaneData` /
`BatchInstanceSystem.UpdateLaneInstances`. The map, for one source vertex `v`:

    local = R_part · v + pos_part
    arc   = arc_start + z_fraction(local.z) · arc_per_z
    world = P(arc) + right(arc) · (local.x · widthScale) + ŷ · (local.y + lift − hang)

with `right = normalize(cross(ŷ, forward))` — HORIZONTAL, so a banked road does not roll its
fences. `P` and `forward` come from the curve by ARC LENGTH, never by parameter: see
`csiibundle.bezier` for why those are not the same thing.

**Tiling is the game's, not a guess.** `tile_count` is `BatchDataHelpers.GetTileCount` ported
term for term. Its `−0.0001` epsilon and its `min(1, tiles)` branch for non-geometry tiling both
matter: they are what decide whether a 6.4 m stub of fence gets one post or none, and a consumer
that rounds differently gets a different fence from the game on every short span. CONTRACT.

**Most tiles do not need baking at all.** On a straight span the bend IS a rigid transform, so
the tile can be one instance of a shared mesh rather than a copy of its vertices. Measured on a
full city, 85% of all lane vertices were fence/hedge tiles and most fence footage runs straight
along lot edges. `rigid_tile_matrix` returns that transform, or `None` when the window is not
straight enough or the width tapers — so instancing is exact where it applies and simply absent
where it is not. Curved tiles, tapered spans, hanging cables, the clipped last tile and every
decal bake as before.

Nothing here imports a renderer. `bend_part` appends plain dicts of numpy arrays to a `sink`,
and `material` values are passed through opaquely — hand it your own material objects and it
will hand them back.
"""

import math

import numpy

from . import bezier, decals

# Ceiling on tiles per span, matching the game's own. CONTRACT.
MAX_TILES = 256

# When a tile may be instanced instead of baked. Both are accuracy bounds on the claim "this
# window's bend is rigid", so tightening them costs speed and never fidelity. TUNED.
TILE_CHORD_TOL = 0.02   # m: max deviation of the tile's curve window from its 3D chord
TILE_WIDTH_TOL = 0.01   # fraction: max widthScale variation across the tile


def tile_count(span_length, mesh_length, tiling_count, geometry_tiling):
    """`BatchDataHelpers.GetTileCount`, with the SPAN's length in place of the whole curve's.

    Returns `(tiles, clip)`. `clip` is the game's quantised repeat count and is 0 when
    `tiling_count` is 0.

    **`clip == 0` with a non-zero `tiling_count` means the game draws ZERO instances** — a
    sub-half-period stub carries no part at all. A consumer that draws one anyway litters short
    road stubs with markings and posts the game leaves bare.

    The `− 0.0001` in the geometry-tiling branch is the game's, and it is load-bearing: without
    it a span whose length is an exact multiple of `mesh_length` gets one extra empty tile.
    CONTRACT, every term.
    """
    if tiling_count:
        clip = round(span_length / max(1.0, mesh_length) * tiling_count)
        tiles = (clip + tiling_count - 1) // tiling_count
        return (min(MAX_TILES, tiles) if geometry_tiling else min(1, tiles)), clip
    if geometry_tiling:
        return max(1, min(MAX_TILES,
                          math.ceil(span_length / max(1.0, mesh_length) - 0.0001))), 0
    return 1, 0


def hang_drop(control, t):
    """A lane's droop in metres at parameters `t` — the cubic in `hang`, a curve of scalars.

    Cables and wires sag; the bundle ships the sag as its own one-dimensional Bézier rather than
    baking it into the path, so the path stays the lane and the droop stays separable.
    """
    return bezier.evaluate_scalar(control, t)


def quat_apply(quat, vectors):
    """Rotate an `(N, 3)` array of bundle-frame vectors by a bundle quaternion `[x, y, z, w]`.

    The two-cross-product form rather than building a matrix: for a part's few dozen source
    vertices it is the cheaper path, and it avoids materialising a 3×3 per part.
    """
    x, y, z, w = quat
    v = numpy.asarray(vectors, dtype=numpy.float64)
    tx = 2 * (y * v[:, 2] - z * v[:, 1])
    ty = 2 * (z * v[:, 0] - x * v[:, 2])
    tz = 2 * (x * v[:, 1] - y * v[:, 0])
    return numpy.stack((v[:, 0] + w * tx + y * tz - z * ty,
                        v[:, 1] + w * ty + z * tx - x * tz,
                        v[:, 2] + w * tz + x * ty - y * tx), axis=1)


def quat_to_mat3(quat):
    """A bundle quaternion `[x, y, z, w]` as a 3×3 rotation matrix (columns = rotated axes)."""
    x, y, z, w = quat
    return numpy.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=numpy.float64)


def rigid_tile_matrix(curve, t_table, arc_table, arc_start, arc_per_z,
                      span, inverted, width_scale, pos, rot, bmin_z, mesh_length):
    """The BUNDLE-frame 4×4 placing one straight tile rigidly, or `None` if the bend is not
    rigid over this tile's window.

    When the curve window is straight (within `TILE_CHORD_TOL`, measured in 3D so grade
    curvature counts too) and the width is constant (within `TILE_WIDTH_TOL`), `P(arc)` is
    linear in `z` and the whole bend collapses to one affine transform: columns
    `right · width`, `ŷ`, and `chord / mesh_length`. The chord carries the grade, which is why a
    fence on a straight climb shears exactly as the bend sheared it rather than staying level.

    Composed with the part's own `(rot, pos)`, the result applies verbatim to the shared,
    un-bent part mesh — so an instanced tile and a baked tile are the same triangles in the same
    place. A consumer whose meshes are in another frame converts this transform the same way it
    converts any other (`frames.to_zup_transform` for the Z-up case).
    """
    arc_end = arc_start + arc_per_z
    arcs = numpy.array([arc_start, arc_start + 0.25 * arc_per_z, arc_start + 0.5 * arc_per_z,
                        arc_start + 0.75 * arc_per_z, arc_end])
    t = numpy.interp(arcs, arc_table, t_table)
    points = bezier.evaluate(curve, t)
    chord = points[-1] - points[0]
    chord_len = float(numpy.linalg.norm(chord))
    if chord_len < 1e-6:
        return None

    deviation = numpy.linalg.norm(numpy.cross(points[1:4] - points[0], chord), axis=1) / chord_len
    if float(deviation.max()) > TILE_CHORD_TOL:
        return None

    t_global = numpy.where(inverted, span[1] - (span[1] - span[0]) * t[[0, -1]],
                           span[0] + (span[1] - span[0]) * t[[0, -1]])
    w0, w1 = (width_scale[0] + (width_scale[1] - width_scale[0]) * t_global)
    if abs(w1 - w0) > TILE_WIDTH_TOL * max(abs(w0), abs(w1), 1e-6):
        return None
    width = (w0 + w1) * 0.5

    # right = normalize(cross(up, forward)), horizontal — exactly the bend's own frame.
    flat = math.hypot(chord[0], chord[2])
    if flat < 1e-6:
        return None
    right = numpy.array([chord[2] / flat, 0.0, -chord[0] / flat])

    outer = numpy.empty((3, 3), dtype=numpy.float64)
    outer[:, 0] = right * width
    outer[:, 1] = (0.0, 1.0, 0.0)
    outer[:, 2] = chord / mesh_length
    matrix = numpy.eye(4)
    matrix[:3, :3] = outer @ quat_to_mat3(rot)
    matrix[:3, 3] = (points[0] - outer[:, 2] * bmin_z
                     + outer @ numpy.asarray(pos, dtype=numpy.float64))
    return matrix


def bend_part(part, source, curve, spans, curve_length, width_scale, hang, sink):
    """Bend one catalogue part along one lane record's curve, appending results to `sink`.

    `sink` is a dict the caller owns, with at least `"blocks"` and `"instances"` lists.

    * `sink["blocks"]` gains one dict per (span, tile, material): `verts` `(N, 3)` bundle-frame
      WORLD positions, `tris` `(M, 3)` local indices, `uvs` `(M, 3, 2)`, `material` (whatever
      the caller put in the catalogue), and `drape_eps` — the lift a draped part must end up
      above, or `None` for a part that never drapes. A draped block also carries `half_height`,
      the part's OWN projector reach, which is what bounds the surface search: see
      `csiibundle.decals`. The drape itself is the caller's, and happens once every block
      exists.
    * `sink["instances"]` gains `(assetId, matrix)` for every tile the rigid gate accepted —
      bundle-frame 4×4s to be instanced against the un-bent part mesh.
    * `sink["tiles_instanced"]` / `["tiles_baked"]` / `["decal_dropped"]` count what happened.

    A `decal` part is wear tint and road markings — crosswalks, centre lines, the drive lanes'
    ~12%-alpha darkening — projected over the surface the arms' node meshes pave (§5B, audit
    §18). It is bent like any part and lifted a little so it wins the depth test the projection
    wins in-game; everything about how far, and how finely, is `csiibundle.decals`.
    """
    lift = decals.BEND_LIFT if part.get("decal") else 0.0
    pos = numpy.asarray(part["pos"], dtype=numpy.float64)
    rot = part["rot"]
    bmin, bmax = part["boundsMin"], part["boundsMax"]
    mesh_length = max(bmax[2] - bmin[2], 0.001)
    tiling_count = part.get("tilingCount", 0)
    geometry_tiling = part.get("geometryTiling", False)
    inverted = part.get("inverted", False)
    is_decal = bool(part.get("decal"))
    # The marking's OWN projector reach, not a tuned probe/depth constant.
    half_height = (bmax[1] - bmin[1]) * 0.5

    quad = source.get("quad") if (is_decal and source is not None) else None
    decal_role = part.get("decalRole") if is_decal else None

    for span in spans:
        span_length = curve_length * (span[1] - span[0])
        if span_length <= 0.1:
            continue

        curve_span = bezier.cut(curve, span[0], span[1])
        if inverted:
            curve_span = (curve_span[3], curve_span[2], curve_span[1], curve_span[0])

        t_table, arc_table = bezier.arc_table(curve_span)
        total_arc = max(float(arc_table[-1]), 0.001)

        # A decal is a projector box the game stretches chord-to-chord along the lane, but only
        # the GEOMETRY stretches — the texture repeats. See `csiibundle.decals` for both halves
        # of that, and for why a `clip == 0` span draws nothing at all.
        if is_decal:
            tiles, clip = tile_count(total_arc, mesh_length, tiling_count, False)
            tiles = min(tiles, 1)  # geometry stays the single chord-to-chord box
            n_total = (clip / tiling_count if tiling_count
                       else total_arc / max(1.0, mesh_length))
        else:
            tiles, clip = tile_count(span_length, mesh_length, tiling_count, geometry_tiling)
        if tiles < 1 or (tiling_count > 0 and clip < 1):
            if is_decal:
                sink["decal_dropped"] = sink.get("decal_dropped", 0) + 1
            continue

        drape_eps = decals.drape_lift(decal_role) if is_decal else None

        if is_decal and quad is not None:
            # Rows from THIS span's own curvature, reusing its arc-table samples; the lattice is
            # generated directly, with no static mesh to subdivide. The row count needs the
            # part's lift, so `drape_eps` is settled above it. Texture-period boundaries are
            # merged in as duplicated stations so the repeated V never smears across the seam.
            curve_points_full = bezier.evaluate(curve_span, t_table)
            rows = decals.station_count(curve_points_full, arc_table, drape_eps)
            row_stations, v_frac = decals.station_rows(rows, n_total)
            raw_verts, tris_local, uv_local = decals.lattice(quad, row_stations, v_frac)
            material_groups = [(quad["material"], numpy.arange(len(tris_local)))]
        else:
            # Everything else — including a decal whose source is not a simple rectangle, which
            # takes this general unsubdivided path rather than a guess at its shape.
            raw_verts, tris_local, uv_local = source["verts"], source["tris"], source["uvs"]
            tri_mat = source["tri_material"]
            material_groups = [(source["materials"][m], numpy.nonzero(tri_mat == m)[0])
                               for m in numpy.unique(tri_mat)]

        local = quat_apply(rot, raw_verts) + pos
        z_fraction = (local[:, 2] - bmin[2]) / mesh_length
        stations, station_of_vert = numpy.unique(z_fraction, return_inverse=True)
        station_of_vert = station_of_vert.reshape(-1)  # numpy>=2.0 may keep input's ndim
        local_x = local[:, 0]
        local_y = local[:, 1]
        # A part with >1 material (rare — every decal source is one material; only a general
        # fence/cable mesh can mix them) gets a full `world.copy()` per extra material group
        # below, i.e. (#materials - 1) x #verts wasted rather than indexing just the subset each
        # group's tris reference. Deliberately not fixed: it would need renumbering each group's
        # tri indices into its own compact range, real code for a case this bundle's lane
        # sources essentially never hit — every multi-material part measured is a small,
        # non-tiled mesh, not the million-vertex tiled parts the instancing path targets.
        share_verts = len(material_groups) == 1

        for tile in range(tiles):
            if is_decal:
                # Geometry is the one stretched box regardless of tilingCount — the repeat lives
                # in the lattice's V, never in clip windows.
                arc_start, arc_per_z, arc_limit = 0.0, total_arc, total_arc
                full_tile = False
            elif tiling_count > 0:
                clip0 = tile * clip // tiles
                clip1 = (tile + 1) * clip // tiles
                if clip1 <= clip0:
                    continue
                arc_start = total_arc * clip0 / clip
                arc_per_z = total_arc * tiling_count / clip
                arc_limit = total_arc * clip1 / clip
                full_tile = (clip0 + tiling_count) <= clip1
            else:
                arc_start = tile * mesh_length
                arc_per_z = mesh_length
                arc_limit = total_arc
                full_tile = arc_start + mesh_length <= total_arc + 1e-6

            # Rigid-tile gate: a full, un-hung tile whose curve window is straight and whose
            # width is constant bends into exactly one affine transform — record an instance and
            # skip the per-vertex bake. Everything failing the gate bakes as before.
            if full_tile and not is_decal and hang is None and lift == 0.0:
                matrix = rigid_tile_matrix(curve_span, t_table, arc_table, arc_start, arc_per_z,
                                           span, inverted, width_scale, pos, rot,
                                           bmin[2], mesh_length)
                if matrix is not None:
                    sink["instances"].append((part["assetId"], matrix))
                    sink["tiles_instanced"] = sink.get("tiles_instanced", 0) + 1
                    continue
            if not is_decal:
                sink["tiles_baked"] = sink.get("tiles_baked", 0) + 1

            arc_pos = numpy.clip(arc_start + stations * arc_per_z, 0.0, arc_limit)
            t = numpy.interp(arc_pos, arc_table, t_table)
            points = bezier.evaluate(curve_span, t)
            forward = bezier.tangent(curve_span, t)
            flen = numpy.linalg.norm(forward, axis=1, keepdims=True)
            forward = forward / numpy.where(flen == 0, 1.0, flen)
            # right = normalize(cross(up, forward)) with up = +y (bundle frame). Horizontal by
            # construction, so a banked or climbing lane does not roll what stands on it.
            right = numpy.stack((forward[:, 2], numpy.zeros(len(t)), -forward[:, 0]), axis=1)
            rlen = numpy.linalg.norm(right[:, (0, 2)], axis=1, keepdims=True)
            right = right / numpy.clip(rlen, 1e-9, None)

            t_global = numpy.where(inverted, span[1] - (span[1] - span[0]) * t,
                                   span[0] + (span[1] - span[0]) * t)
            width = width_scale[0] + (width_scale[1] - width_scale[0]) * t_global
            drop = hang_drop(hang, t_global) if hang is not None else numpy.zeros_like(t_global)

            world = (points[station_of_vert]
                     + right[station_of_vert] * (local_x * width[station_of_vert])[:, None])
            world[:, 1] += local_y + lift - drop[station_of_vert]

            for material, tri_rows in material_groups:
                if len(tri_rows) == 0:
                    continue
                block = {
                    "verts": world if share_verts else world.copy(),
                    "tris": tris_local[tri_rows],
                    "uvs": uv_local[tri_rows],
                    "material": material,
                    "drape_eps": drape_eps,
                }
                if drape_eps is not None:
                    # The drape samples the vertex's OWN (x, z) — including its lateral offset —
                    # not the curve's centerline, so no extra per-station bookkeeping is stored.
                    block["half_height"] = half_height
                sink["blocks"].append(block)
