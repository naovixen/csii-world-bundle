"""The §8 ground lattice — one welded vertex grid, three face families, no T-junctions.

**One lattice, one vertex set, welded by construction.** The vertex grid IS the native texel
grid (§3 puts a vertex at each cell centre), and all three face families index it: coarse block
quads away from anything interesting, native cells inside the refine corridors, and the
transition fans joining the two (`csiibundle.latticefaces`). Nothing duplicates a position, no
edge is pinned to a subsample, and normals are shared across every seam.

**Never couple the cut resolution to the mesh density.** That coupling was a real bug — a
stride patchwork that produced spikes, chevrons and a serrated shoreline. The game keeps mesh
density and per-pixel decisions independent, and the refine CORRIDOR is how this grid does the
same: every decision boundary (clip volumes, roadbed, shoreline) gets native cells, the rest of
the map stays coarse, and the two are joined without T-junctions.

**The cut is exact.** Pass `prisms` (`terrainclip.load_prisms`) and every face one reaches is
cut against it by convex subtraction (`csiibundle.latticecut`), which is what the game's
stencil does per pixel (audit §46). Without them the lattice is built uncut — useful only for a
bundle with no clip layer.

**The heights are the game's own.** `carriageway.ground_heights` is applied to the lattice
ONCE, before any face family is built, so coarse, refined and transition faces all read
identical heights — and all it does now is lower the samples that stand over a carriageway
onto it.

Nothing here builds a mesh — it produces masks, lattices, index arrays and heights. Turning
those into geometry is the consumer's, and stays in the adapter.

**Two deletion rules that are recorded here because both are tempting and both are wrong**
(the shipped rule — containment sampled per face — is `csiibundle.containment`):

  * a face deleted on its own CENTRE. Unbiased, and half of an unbiased error deletes
    terrain no road covers — that half is sky. Measured 1,772-6,040 m² of it per 240 m
    window against the 0-16 m² the shipped rule leaves.
  * a face deleted when all four of its CORNER CELLS are cut. That tests the corners
    against the UNION of every prism, so four corners covered by four different prisms
    passed while the face's interior escaped them all — the geometry of a junction, and
    the mechanism behind the junction gaps this replaces (398 m² and 197 m² of sky at the
    two junctions reported on 2026-08-11).

The band used to be SPLIT at the carriageway — contained below the lane curve, face-centre
above it — to stop pure corner-containment walling off tunnels (§8c). That compensation is
gone with the rule that needed it: measured over the city's three densest tunnel-portal
clusters, containment sampled per face deletes 100%, 100% and 96.7% of the buried
centreline faces, which is the split band's own number to the digit.
"""

import math
from collections import namedtuple

import numpy

from . import carriageway, clipraster, latticecut, latticefaces

# What `build_lattice` hands back. Geometry first (`verts`/`loops`/`sizes`/`face_class` are the
# mesh), then the rasters a caller needs for UVs and materials, then counts a caller may report.
# `face_class` is 0 grass / 1 roadbed / 2 rock — presentation, not contract. `face_cell` is
# `(N, 2)` — the WINDOW cell `(r, c)` each face belongs to: its own cell for a native face, the
# parent's for a sub-cut patch, `-1` for a coarse block or a transition fan. A consumer that
# keeps part of the heightmap as its own grid (bake `terraincut`) needs the identity, because
# a face's centre is no longer inside its cell once the snap has moved a corner.
LatticeMesh = namedtuple(
    "LatticeMesh",
    (
        "lat_h lat_w nfr nfc "
        "verts loops sizes face_class face_cell "
        "native_y ground_y wet has_level level_value roadbed cut contain band_count "
        "block_refined coarse_loops cbr cbc cell_rr cell_cc refined_loops_all fan_loops "
        "fan_faces refined_faces cut_faces roadbed_faces rock_faces "
        "cut_parents cut_pieces cut_removed_m2 cut_slivers "
        "band_clamped band_clamp_drop stray_cells"
    ),
)

# The window's sliced rasters, all in the lattice frame: entry (r, c) is cell
# (row0 + r, col0 + c) of the full raster. `contain`/`band_count` are FACE rasters, one entry
# smaller on each axis (the window's last lattice row and column carry no face).
_Window = namedtuple(
    "_Window",
    "nfr nfc lat_h lat_w native_y roadbed cut contain band_count has_level level_value wet",
)


def _levels(level_raw, scale, offset, native_y):
    """The window's water level, from its already-sliced raw raster:
    `(has_level, level_value, wet)`."""
    if level_raw is None:
        has_level = numpy.zeros(native_y.shape, dtype=bool)
        return has_level, None, has_level
    has_level = level_raw != 0  # 0 = no data, same truthiness test as ever (§8)
    level_value = level_raw.astype(numpy.float32) * numpy.float32(scale) + numpy.float32(offset)
    return has_level, level_value, has_level & (level_value > native_y)


def _window(heights, surface_ids, levels, grid, window, stride, clip):
    """Slice every full-map raster down to one window's lattice frame, or `None` when the
    window is smaller than a single face.

    The window is nfr×nfc blocks of stride² native cells; the vertex lattice is the native
    texel grid itself (a vertex sits at a cell centre, contract §3).
    """
    width, height = grid.width, grid.height
    row0, col0, row1, col1 = window
    nfr = (row1 - row0) // stride
    nfc = (col1 - col0) // stride
    if nfr < 1 or nfc < 1:
        return None  # smaller than one face; the caller reports it
    lat_h = nfr * stride + 1
    lat_w = nfc * stride + 1

    heights_native = numpy.asarray(heights).reshape(height, width)
    native_y = heights_native[row0 : row0 + lat_h, col0 : col0 + lat_w].astype(
        numpy.float32
    ) * numpy.float32(grid.elevation_scale) + numpy.float32(grid.elevation_offset)

    roadbed = (
        (
            numpy.asarray(surface_ids).reshape(height, width)[
                row0 : row0 + lat_h, col0 : col0 + lat_w
            ]
            != 0
        )
        if surface_ids is not None
        else numpy.zeros((lat_h, lat_w), dtype=bool)
    )
    cut = clip.mask[row0 : row0 + lat_h, col0 : col0 + lat_w]
    contain = clip.contain[row0 : row0 + lat_h - 1, col0 : col0 + lat_w - 1]
    band_count = clip.band_count[row0 : row0 + lat_h - 1, col0 : col0 + lat_w - 1]
    level_raw = (
        numpy.asarray(levels).reshape(height, width)[row0 : row0 + lat_h, col0 : col0 + lat_w]
        if levels is not None
        else None
    )
    lvl = _levels(level_raw, grid.elevation_scale, grid.elevation_offset, native_y)
    return _Window(nfr, nfc, lat_h, lat_w, native_y, roadbed, cut, contain, band_count, *lvl)


def _corridors(win, stride, cell):
    """Where the mesh goes native: `(block_refined, block_transition, stray_cells)`.

    Every decision boundary — clip volumes, roadbed, shoreline — gets native-resolution
    cells; the rest of the map keeps the coarse grid. The 3-cell (~10.5 m at 3.5 m) dilation
    covers the game's 8 m terrain smoothing apron, which the roadbed mask deliberately
    excludes — the meld gradients down to a portal live inside it.
    """
    # A deleted face samples its own interior, so its four corner CELLS need not be cut (they
    # are not among the samples). Such a face must still land in a refined block, or the
    # deletion would have no native face to delete. Fold the face mask back onto the cells it
    # spans before dilating.
    cut_for_corridor = win.cut.copy()
    cut_for_corridor[:-1, :-1] |= win.contain
    cut_for_corridor[:-1, 1:] |= win.contain
    cut_for_corridor[1:, :-1] |= win.contain
    cut_for_corridor[1:, 1:] |= win.contain
    # 3 cells at 3.5 m (10.5 m) covers the game's 8 m terrain-smoothing apron; in cells of the
    # heightfield at hand, so the corridor is the same width in metres on the fine raster.
    refine_cells = clipraster.dilate(
        win.roadbed | cut_for_corridor | clipraster.mask_boundary(win.wet),
        max(1, int(math.ceil(10.5 / cell))),
    )

    if stride == 1:
        # A region import is already native everywhere; the whole window is one corridor and
        # the transition machinery has nothing to join.
        block_refined = numpy.ones((win.nfr, win.nfc), dtype=bool)
        block_transition = numpy.zeros((win.nfr, win.nfc), dtype=bool)
    else:
        block_refined, _ = clipraster.face_footprint_reduce(
            refine_cells, 0, 0, win.nfr, win.nfc, stride
        )
        block_transition = clipraster.dilate(block_refined, 1) & ~block_refined

    # Safety net: every cut cell must sit inside a refined block (the corridor contains the
    # cut mask by construction). A stray one would mean a coarse face silently kept over a
    # tunnel.
    stray_cells = 0
    if stride > 1 and cut_for_corridor.any():
        s_rr, s_cc = numpy.nonzero(cut_for_corridor)
        stray = ~block_refined[
            numpy.minimum(s_rr // stride, win.nfr - 1), numpy.minimum(s_cc // stride, win.nfc - 1)
        ]
        stray_cells = int(stray.sum())
    return block_refined, block_transition, stray_cells


def _mesh(win, f, cut, ground_y, block_refined, band_clamped, band_clamp_drop, stray_cells):
    """The `LatticeMesh` for one built window."""
    return LatticeMesh(
        lat_h=win.lat_h,
        lat_w=win.lat_w,
        nfr=win.nfr,
        nfc=win.nfc,
        verts=cut.verts,
        loops=cut.loops,
        sizes=cut.sizes,
        face_class=cut.face_class,
        face_cell=cut.face_cell,
        native_y=win.native_y,
        ground_y=ground_y,
        wet=win.wet,
        has_level=win.has_level,
        level_value=win.level_value,
        roadbed=win.roadbed,
        cut=win.cut,
        contain=win.contain,
        band_count=win.band_count,
        block_refined=block_refined,
        coarse_loops=f.coarse_loops,
        cbr=f.cbr,
        cbc=f.cbc,
        cell_rr=f.cell_rr,
        cell_cc=f.cell_cc,
        refined_loops_all=f.refined_loops_all,
        fan_loops=f.fan_loops,
        fan_faces=len(f.fan_class),
        refined_faces=int((~f.sub_face).sum()),
        cut_faces=int(f.sub_face.sum()) - cut.faces_kept,  # faces the cut removed entirely
        roadbed_faces=int((cut.face_class == 1).sum()),
        rock_faces=int((cut.face_class == 2).sum()),
        cut_parents=int(f.sub_face.sum()),
        cut_pieces=cut.pieces,
        cut_removed_m2=cut.removed_m2,
        cut_slivers=cut.slivers,
        band_clamped=band_clamped,
        band_clamp_drop=band_clamp_drop,
        stray_cells=stray_cells,
    )


def build_lattice(heights, surface_ids, levels, grid, window, stride, clip, prisms=None):
    """Build the §8 ground mesh for one window: vertices, faces, and what each face is.

    This is where every other function in this module meets — the design rules are the module
    docstring. `grid` is the heightmap's `clipgrid.Grid` frame, `window` its
    `(row0, col0, row1, col1)` cell window, and `clip` the `clipgrid.ClipGrid` for the whole
    map. Returns a `LatticeMesh`, or `None` when the window is smaller than a single face.
    """
    row0, col0 = window[0], window[1]
    win = _window(heights, surface_ids, levels, grid, window, stride, clip)
    if win is None:
        return None
    block_refined, block_transition, stray_cells = _corridors(win, stride, grid.cell)

    # The heightmap already carries the game's road grading, so the only edit is the residual:
    # samples the game's stencil would have discarded, standing above the carriageway's own
    # cross-section. That is `ground_heights` — the ONE definition a heightfield consumer
    # applies to its own grid too, so a mesh built here meets that heightfield sample for
    # sample (bake `terraincut`). The refine corridor is the roadbed dilated 3 cells, so a
    # clamped vertex only ever sits inside a refined block: it cannot move a coarse corner or
    # a fan edge.
    ground_y, band_clamped, band_clamp_drop = carriageway.ground_heights(
        win.native_y, clip.band_ceiling[row0 : row0 + win.lat_h, col0 : col0 + win.lat_w]
    )

    f = latticefaces.families(win, block_refined, block_transition, stride, grid, window, prisms)
    verts, loops, sizes, face_class, face_cell = latticefaces.assemble(
        win, f, ground_y, grid, window
    )
    cut = latticecut.cut_prism_faces(
        verts, loops, sizes, face_class, face_cell, f, ground_y, prisms, grid, window
    )
    return _mesh(win, f, cut, ground_y, block_refined, band_clamped, band_clamp_drop, stray_cells)
