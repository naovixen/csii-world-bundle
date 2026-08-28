"""The three face families of the §8 ground lattice, and their assembly onto one vertex set.

Coarse block quads away from anything interesting, native cells inside the refine corridors,
and `transition_fans` joining the two — all indexing the ONE shared native lattice
(`csiibundle.lattice`), so the mesh is welded by construction. `families` builds the three;
`assemble` concatenates them and emits only the lattice points some face references.
"""

from collections import namedtuple

import numpy

from . import terrainclip

# Ground steeper than this (rise over run) is textured rock instead of grass — the gorge
# embankments. The game classifies grass/dirt/rock per pixel on the GPU by slope, height and
# noise, and §8 deliberately does not reproduce that; a plain slope threshold at ~40 degrees is
# the presentation-side stand-in. TUNED, and purely presentational.
ROCK_SLOPE = 0.84


def transition_fans(block_refined, block_transition, stride, lattice_w):
    """Triangle fans that join coarse blocks to refined ones without cracks.

    A transition block is a coarse block 4-adjacent to at least one refined block. Its face
    is emitted as a fan from the block's centre lattice point to an ordered perimeter loop:
    on a side shared with a REFINED block the perimeter includes every native lattice point
    of that edge (so the refined neighbour's true edge is part of this face — no T-junction,
    no crack, no pinning); on a side shared with a coarse/transition block only the two
    corners (that neighbour draws a straight segment there, and adding interior points would
    tear it). All indices are into the shared native lattice, flat (`row * lattice_w + col`).

    Returns `(loops, block_ids)`: `loops` a flat int array of fan-triangle loop indices
    (3 per triangle, CCW from above — same up-normal as the quad winding), `block_ids` one
    entry per triangle naming the (row-major) transition-block it belongs to, for per-face
    class lookup.
    """
    nfr, nfc = block_refined.shape
    half = stride // 2
    loops = []
    block_ids = []
    tr, tc = numpy.nonzero(block_transition)
    for bR, bC in zip(tr.tolist(), tc.tolist()):
        r0, c0 = bR * stride, bC * stride

        def edge_points(refined, points):
            # Each side contributes its starting corner plus, when the neighbour is refined,
            # the interior lattice points; the closing corner belongs to the next side.
            return points if refined else points[:1]

        west = 0 <= bC - 1 and block_refined[bR, bC - 1]
        south = bR + 1 < nfr and block_refined[bR + 1, bC]
        east = bC + 1 < nfc and block_refined[bR, bC + 1]
        north = 0 <= bR - 1 and block_refined[bR - 1, bC]

        perimeter = (
            edge_points(west, [(r0 + k, c0) for k in range(stride)])
            + edge_points(south, [(r0 + stride, c0 + k) for k in range(stride)])
            + edge_points(east, [(r0 + stride - k, c0 + stride) for k in range(stride)])
            + edge_points(north, [(r0, c0 + stride - k) for k in range(stride)])
        )
        centre = (r0 + half) * lattice_w + (c0 + half)
        count = len(perimeter)
        for i in range(count):
            pr, pc = perimeter[i]
            qr, qc = perimeter[(i + 1) % count]
            loops.extend((centre, pr * lattice_w + pc, qr * lattice_w + qc))
            block_ids.append(bR * nfc + bC)

    return (numpy.asarray(loops, dtype=numpy.int64), numpy.asarray(block_ids, dtype=numpy.int64))


# The three families before assembly. `sub_face` marks the refined faces a prism reaches —
# withheld from the quad set and cut exactly by `latticecut` instead.
Families = namedtuple(
    "Families",
    "coarse_loops coarse_class cbr cbc "
    "cell_rr cell_cc refined_loops_all by_face sub_face refined_class_all "
    "fan_loops fan_class",
)


def block_classes(native_y, stride, cell):
    """Block-level ground class (0 grass / 2 rock), from the coarse corners.

    Sampled (unclamped) heights, so the road clamp cannot flip a boundary face. Coarse and
    transition blocks can never be roadbed: a block containing any roadbed cell is refined
    by construction.
    """
    corner_ys = native_y[::stride, ::stride]  # (nfr+1, nfc+1), exact by lattice construction
    run = float(stride) * cell
    gx = (
        numpy.maximum(
            numpy.abs(corner_ys[:-1, 1:] - corner_ys[:-1, :-1]),
            numpy.abs(corner_ys[1:, 1:] - corner_ys[1:, :-1]),
        )
        / run
    )
    gz = (
        numpy.maximum(
            numpy.abs(corner_ys[1:, :-1] - corner_ys[:-1, :-1]),
            numpy.abs(corner_ys[1:, 1:] - corner_ys[:-1, 1:]),
        )
        / run
    )
    return numpy.where(numpy.hypot(gx, gz) > ROCK_SLOPE, 2, 0)


def _coarse_quads(block_coarse, block_class, lattice_flat, stride):
    """Quads for the coarse blocks. Winding (NW, SW, SE, NE) — south then east — keeps the
    normal up in Blender's frame, and faces stay QUADS so Blender tessellates them exactly as
    the scalar baseline did."""
    corner_flat = lattice_flat[::stride, ::stride]  # (nfr+1, nfc+1)
    cbr, cbc = numpy.nonzero(block_coarse)
    coarse_loops = numpy.stack(
        (
            corner_flat[cbr, cbc],
            corner_flat[cbr + 1, cbc],
            corner_flat[cbr + 1, cbc + 1],
            corner_flat[cbr, cbc + 1],
        ),
        axis=-1,
    )
    return cbr, cbc, coarse_loops, block_class[cbr, cbc]


def _refined_faces(win, block_refined, stride, grid, window, prisms):
    """Native cells inside refined blocks, and which of them a prism reaches.

    Every face a prism reaches is CUT (`latticecut`) rather than kept or deleted whole, so it
    leaves the quad set here and comes back as polygons. `terrainclip.index_prisms` answers
    "which prisms can reach this square" by plan bounding box, conservatively.

    Refined faces classify from their OWN corners at native resolution; roadbed reads the
    face's top-left cell sample, the rule the scalar face_roadbed always used.
    """
    cell = grid.cell
    rbr, rbc = numpy.nonzero(block_refined)
    span = numpy.arange(stride, dtype=numpy.int64)
    cell_rr = numpy.broadcast_to(
        rbr[:, None, None] * stride + span[None, :, None], (len(rbr), stride, stride)
    ).reshape(-1)
    cell_cc = numpy.broadcast_to(
        rbc[:, None, None] * stride + span[None, None, :], (len(rbr), stride, stride)
    ).reshape(-1)
    a = cell_rr * win.lat_w + cell_cc
    refined_loops_all = numpy.stack((a, a + win.lat_w, a + win.lat_w + 1, a + 1), axis=-1)
    by_face = (
        terrainclip.index_prisms(
            prisms,
            cell,
            grid.origin_x,
            grid.origin_z,
            win.lat_w,
            win.lat_h,
            row0=window[0],
            col0=window[1],
        )
        if prisms
        else {}
    )
    sub_face = numpy.fromiter(
        ((int(r), int(c)) in by_face for r, c in zip(cell_rr, cell_cc)),
        dtype=bool,
        count=len(cell_rr),
    )

    native_y = win.native_y
    y00 = native_y[cell_rr, cell_cc]
    y01 = native_y[cell_rr, cell_cc + 1]
    y10 = native_y[cell_rr + 1, cell_cc]
    y11 = native_y[cell_rr + 1, cell_cc + 1]
    ngx = numpy.maximum(numpy.abs(y01 - y00), numpy.abs(y11 - y10)) / cell
    ngz = numpy.maximum(numpy.abs(y10 - y00), numpy.abs(y11 - y01)) / cell
    refined_class = numpy.where(numpy.hypot(ngx, ngz) > ROCK_SLOPE, 2, 0)
    refined_class_all = numpy.where(win.roadbed[cell_rr, cell_cc], 1, refined_class)
    return cell_rr, cell_cc, refined_loops_all, by_face, sub_face, refined_class_all


def families(win, block_refined, block_transition, stride, grid, window, prisms):
    """All three face families for one window, as a `Families`."""
    block_class = block_classes(win.native_y, stride, grid.cell)
    lattice_flat = numpy.arange(win.lat_h * win.lat_w, dtype=numpy.int64).reshape(
        win.lat_h, win.lat_w
    )
    cbr, cbc, coarse_loops, coarse_class = _coarse_quads(
        ~block_refined & ~block_transition, block_class, lattice_flat, stride
    )
    cell_rr, cell_cc, refined_loops_all, by_face, sub_face, refined_class_all = _refined_faces(
        win, block_refined, stride, grid, window, prisms
    )
    fan_loops, fan_blocks = transition_fans(block_refined, block_transition, stride, win.lat_w)
    return Families(
        coarse_loops,
        coarse_class,
        cbr,
        cbc,
        cell_rr,
        cell_cc,
        refined_loops_all,
        by_face,
        sub_face,
        refined_class_all,
        fan_loops,
        block_class.reshape(-1)[fan_blocks],
    )


def assemble(win, f, ground_y, grid, window):
    """One shared vertex set over the lattice: concatenate the three families and emit only
    the lattice points some face references. Returns `(verts, loops, sizes, face_class,
    face_cell)`."""
    refined_loops = f.refined_loops_all[~f.sub_face]
    all_loops = numpy.concatenate(
        (f.coarse_loops.reshape(-1), refined_loops.reshape(-1), f.fan_loops)
    )
    sizes = numpy.concatenate(
        (
            numpy.full(len(f.coarse_loops), 4, dtype=numpy.int64),
            numpy.full(len(refined_loops), 4, dtype=numpy.int64),
            numpy.full(len(f.fan_loops) // 3, 3, dtype=numpy.int64),
        )
    )
    face_class = numpy.concatenate((f.coarse_class, f.refined_class_all[~f.sub_face], f.fan_class))
    face_cell = numpy.concatenate(
        (
            numpy.full((len(f.coarse_loops), 2), -1, dtype=numpy.int64),
            numpy.stack((f.cell_rr, f.cell_cc), axis=-1)[~f.sub_face],
            numpy.full((len(f.fan_loops) // 3, 2), -1, dtype=numpy.int64),
        )
    )

    used = numpy.zeros(win.lat_h * win.lat_w, dtype=bool)
    used[all_loops] = True
    # A sub-cut face draws no quad of its own, but the sub-patch is interpolated from its four
    # corners and welds to them, so all four must exist in the lattice vertex set — including
    # their post-snap positions. A corner whose own corner sub-face then falls inside the band
    # ends up referenced by nothing; the prune after the patch is built removes it.
    if f.sub_face.any():
        used[f.refined_loops_all[f.sub_face].reshape(-1)] = True
    used_idx = numpy.nonzero(used)[0]
    vid = numpy.full(win.lat_h * win.lat_w, -1, dtype=numpy.int64)
    vid[used_idx] = numpy.arange(len(used_idx))
    ur, uc = numpy.divmod(used_idx, win.lat_w)
    row0, col0 = window[0], window[1]
    verts = numpy.stack(
        (
            grid.origin_x + (col0 + uc).astype(numpy.float64) * grid.cell,
            grid.origin_z - (row0 + ur).astype(numpy.float64) * grid.cell,
            ground_y[ur, uc].astype(numpy.float64),
        ),
        axis=-1,
    )
    return verts, vid[all_loops], sizes, face_class, face_cell
