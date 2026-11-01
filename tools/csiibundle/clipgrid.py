"""Rasterising a bundle's `clipvolumes.jsonl` onto its heightmap grid — contract §8c.

CS2 does not carve tunnels out of its heightmap. The heightmap under a tunnel is intact ground,
and the game hides it at draw time by stencil-clipping against the §8c clip volumes: a prism
swept along each road's own left and right curves, spanning a band above and below the
carriageway. A consumer that meshes the heightmap and stops there gets a city whose tunnels are
filled with earth and whose trenches have no walls, and one that deletes everything a prism
touches gets holes in the open sky. `clip_grid` rasterises the layer into the decision rasters
both mesh and heightfield consumers read; the deletion rule itself is `csiibundle.containment`.

The cell raster and the deletion mask answer different questions and both are needed. "Which
cells does the clip volume cover?" decides the refine corridor and gives the boundary snap its
reference curve; "is this face WHOLLY inside the volume?" decides whether the face survives.
The strips are walked twice: once here at cell centres, and once per face-block inside
`contain_faces`, which re-evaluates the quads from `strip_geo` rather than holding a quarter of
a million of them in memory.

`hole_mask` is the same rasterisation packaged for a consumer that feeds the heightmap to its
engine's terrain and punches holes instead of building a lattice (bake `terrainmask`).
"""

import math
import os
from collections import namedtuple

import numpy

from . import bezier, clipblocks, clipraster, containment, reader

CLIP_REF_STATION_BITS = 11  # stations per strip cap at 1024 (< 2^11)

# The heightmap's geo frame, threaded whole through the §8 machinery (`clip_grid`,
# `lattice.build_lattice`, `watersurface.water_surface`) instead of as seven loose scalars.
# (width, height) is the full raster's shape; `cell` its metres per texel; `origin_x`/`origin_z`
# the top-left texel centre (row 0 north, §3); scale/offset the raw-to-metres mapping.
Grid = namedtuple("Grid", "width height cell origin_x origin_z elevation_scale elevation_offset")

# What `clip_grid` returns, in the tuple order it always had: the (h, w) bool cut mask over
# CELL centres; the (h−1, w−1) deletion mask from `contain_faces`; its per-face count of
# in-band sub-samples (the true clipped area in `(cell / CLIP_CONTAIN_SAMPLES)²` units); which
# faces a deep band reaches without containing; an int32 raster of packed
# (strip << CLIP_REF_STATION_BITS | station) references for every cut cell (−1 where uncut or
# cut only by an `area` record, which has no curves to snap to); `strip_geo[strip] =
# (left_pts, right_pts, lane, bottom, top, carves)` — the evaluated boundary polylines, and
# whether that strip is allowed to DELETE terrain (see `CLIP_DEEP_BAND_TOP`); and the
# carriageway ceiling raster `clamp_carriageway` reads. Every strip contributes to `mask` and
# `refs`, because those drive the refine corridor, not the deletion.
ClipGrid = namedtuple(
    "ClipGrid",
    "mask contain band_count deep_touch refs strip_geo band_ceiling area_shallow",
    defaults=(0,),
)


def _to_cell(points, cell, origin_x, origin_z):
    """World positions to cell space, cell (row, col) centred on (col + .5, row + .5)."""
    return numpy.stack(
        ((points[:, 0] - origin_x) / cell + 0.5, (origin_z - points[:, 2]) / cell + 0.5),
        axis=-1,
    )


def _read_strips(bundle_dir):
    """The §8c records. The layer is mandatory (contract §8c): a bundle with terrain but no
    clip volumes would silently wall off every tunnel, so its absence is an error, not a
    fallback."""
    path = reader.layer_path(bundle_dir, "clipVolumes")
    if not os.path.isfile(path):
        raise RuntimeError(
            "CONTRACT VIOLATION: terrain layer present but no clipvolumes.jsonl — "
            "the producer always writes the §8c clip layer; re-rip the bundle"
        )
    return list(reader.read_jsonl(path))


def _rasterise_area(strip, fill, to_cell, bottom, top, area_geo):
    """One `area` record — carries its own polygon since 2026-08-09 (§8c): the game never
    paints clip surfaces, so areas.jsonl stopped carrying them (§36) and the old join here
    read geometry that no longer exists."""
    nodes, triangles = strip.get("nodes") or [], strip.get("triangles") or []
    for tri in triangles:
        if any(not 0 <= i < len(nodes) for i in tri):
            continue
        pts = numpy.asarray([nodes[i] for i in tri], dtype=numpy.float64)
        cells = to_cell(pts)
        # A triangle reaches the scanline as a quad with one doubled corner, and its
        # lane reference is the corner mean — both as the producer has them. No ref:
        # an area cut has no boundary curves for the snap to project onto.
        fill(
            [tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
            float(pts[:, 1].mean()),
            bottom,
            top,
        )
        area_geo.append((cells, float(pts[:, 1].mean()), bottom, top))


def _rasterise_strip(strip, fill, to_cell, bottom, top, cell, strip_geo, band_ceiling):
    """One swept strip: evaluate its two curves, fill every station quad, and record the
    evaluated geometry for the containment pass and the hole-boundary snap."""
    left, right = strip.get("left"), strip.get("right")
    if not left or not right:
        return

    # One station per cell of arc keeps the fill within half a cell of the true curve. The
    # control polygon bounds the arc from above, so this errs toward more stations.
    polygon = numpy.asarray(left, dtype=numpy.float64)
    span = float(numpy.linalg.norm(numpy.diff(polygon, axis=0), axis=1).sum())
    steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))

    t = numpy.linspace(0.0, 1.0, steps + 1)
    left_pts = bezier.evaluate(left, t)
    right_pts = bezier.evaluate(right, t)
    left_cells = to_cell(left_pts)
    right_cells = to_cell(right_pts)
    left_y, right_y = left_pts[:, 1], right_pts[:, 1]
    lane = (left_y + right_y) * 0.5
    carves = top > containment.CLIP_DEEP_BAND_TOP

    strip_index = len(strip_geo)
    strip_geo.append((left_pts, right_pts, lane, bottom, top, carves))
    for i in range(1, steps + 1):
        # The band's two sides, not their mean: see `clipraster.lane_ramp`. A deep band writes
        # no ceiling — it is carved, and a carved cell has no ground left to clamp.
        fill(
            [
                tuple(left_cells[i - 1]),
                tuple(right_cells[i - 1]),
                tuple(right_cells[i]),
                tuple(left_cells[i]),
            ],
            (
                float((left_y[i - 1] + left_y[i]) * 0.5),
                float((right_y[i - 1] + right_y[i]) * 0.5),
            ),
            bottom,
            top,
            ref_value=(strip_index << CLIP_REF_STATION_BITS) | i,
            ceiling=(None if carves else band_ceiling),
        )


def _rasterise_volumes(strips, terrain_y, refs, to_cell, width, height, cell, floor_override):
    """Every §8c record onto the cell-centre rasters: `(mask, band_ceiling, strip_geo,
    area_geo)`."""
    mask = numpy.zeros((height, width), dtype=bool)

    # The top of the game's own carriageway cross-section under a SHALLOW band, per cell —
    # +inf where no shallow band covers it. `clamp_carriageway` reads this and nothing else; a
    # deep band writes nothing here because it is carved instead (`contain_faces`). float32
    # deliberately: this is one full-map raster (67 MiB at 4096², against 134 at float64) and it
    # holds a height around 500 m, where float32 still resolves better than a ten-thousandth of
    # a millimetre.
    band_ceiling = numpy.full((height, width), numpy.inf, dtype=numpy.float32)
    strip_geo = []
    area_geo = []

    def fill(quad, lane_y, bottom, top, ref_value=-1, ceiling=None):
        clipraster.fill_clip_quad(
            mask,
            terrain_y,
            width,
            height,
            quad,
            lane_y,
            bottom,
            top,
            refs=(refs if ref_value >= 0 else None),
            ref_value=ref_value,
            ceiling=ceiling,
        )

    unbounded = float("inf")
    for strip in strips:
        bottom = float(strip["bottom"]) if floor_override is None else float(floor_override)
        top = float(strip["top"]) if strip.get("top") is not None else unbounded
        if strip.get("kind") == "area":
            _rasterise_area(strip, fill, to_cell, bottom, top, area_geo)
        else:
            _rasterise_strip(strip, fill, to_cell, bottom, top, cell, strip_geo, band_ceiling)
    return mask, band_ceiling, strip_geo, area_geo


def clip_grid(bundle_dir, grid, heights, floor_override=None):
    """Rasterise `clipvolumes.jsonl` onto the heightmap grid — contract §8c.

    The volumes are what the producer intersects against the heightmap; a consumer meshing at
    its own resolution does the intersection itself. `grid` is the heightmap's `Grid` frame and
    `heights` its raw samples; what comes back is a `ClipGrid`, documented on the record.
    `floor_override`, when not None, replaces every strip's `bottom` — a diagnostic knob
    (`--clip-floor`) for previewing a producer band-floor change against an existing bundle.
    """
    width, height = grid.width, grid.height
    strips = _read_strips(bundle_dir)
    refs = numpy.full((height, width), -1, dtype=numpy.int32)
    if not strips:
        return ClipGrid(
            numpy.zeros((height, width), dtype=bool),
            numpy.zeros((height - 1, width - 1), dtype=bool),
            numpy.zeros((height - 1, width - 1), dtype=numpy.uint8),
            numpy.zeros((height - 1, width - 1), dtype=bool),
            refs,
            [],
            numpy.full((height, width), numpy.inf, dtype=numpy.float32),
        )

    terrain_y = (
        numpy.asarray(heights, dtype=numpy.float64).reshape(height, width) * grid.elevation_scale
        + grid.elevation_offset
    )

    def to_cell(points):
        return _to_cell(points, grid.cell, grid.origin_x, grid.origin_z)

    mask, band_ceiling, strip_geo, area_geo = _rasterise_volumes(
        strips, terrain_y, refs, to_cell, width, height, grid.cell, floor_override
    )
    block = containment.CLIP_CONTAIN_BLOCK
    strip_cells = clipblocks.station_bounds(strip_geo, to_cell)
    bins, shallow_areas = clipblocks.bin_deep(strip_cells, area_geo, width, height, block)
    contain, band_count, deep_touch = containment.contain_faces(
        width,
        height,
        terrain_y,
        mask,
        clipblocks.make_quad_source(strip_cells, area_geo, bins, block),
        containment.contain_samples(grid.cell),
        block,
    )
    return ClipGrid(
        mask, contain, band_count, deep_touch, refs, strip_geo, band_ceiling, shallow_areas
    )


# -- the hole mask for a heightmap-native consumer -----------------------------------------------

# What `hole_mask` hands a heightmap-native consumer:
#
# * `contain` — `(h−1, w−1)` bool: the face (terrain SQUARE between four heightmap samples)
#   lies wholly inside a DEEP clip band and must not draw. This is `contain_faces`' rule
#   unchanged: only a deep band may carve, and only when every sub-sample is inside it.
#   A heightmap-native engine marks these squares as holes.
# * `deep_touch` — `(h−1, w−1)` bool: a deep band reaches the face without containing it.
#   Informational (a "majority" hole rule can use `band_count` against
#   `CLIP_CONTAIN_SAMPLES²`); the contract's rule is `contain`.
# * `band_count` — `(h−1, w−1)` uint8 count of in-band sub-samples per face.
# * `band_ceiling` — `(h, w)` float32 per SAMPLE: the top of the game's own carriageway
#   cross-section under the SHALLOW band that covers the sample
#   (`lane + top − CLIP_BAND_EXPANSION`), `+inf` where none does. Feed it to
#   `clamp_carriageway` with the world heights to lower the samples that stand over the road
#   back onto it — the pixels the game's stencil discards and a heightfield cannot (audit
#   §51; 11,683 of 2,297,403 on the full city). The heights themselves are already graded by
#   the game and are not otherwise touched.
# * `heights` — `(h, w)` float64 world heights, `cell`, `origin_x/z` (top-left texel centre;
#   row 0 north), and the scale/offset, so the caller can write the clamped heights back at
#   the bundle's own quantisation; `fine` says whether that grid is the bundle's 1.75 m
#   `terrainFine` raster (`reader.load_heightfield`, preferred) or the 3.5 m base map.
#
# Face `(r, c)` spans samples `(r..r+1, c..c+1)`, i.e. world x from `origin_x + c·cell` to
# `+ (c+1)·cell` and z from `origin_z − r·cell` down to `− (r+1)·cell` — the same squares a
# BeamNG-style terrain triangulates between adjacent samples.
# * `area_shallow` — how many `area` clip records were shallow (`top <= CLIP_DEEP_BAND_TOP`) and
#   therefore never entered the deletion test at all; a counted fact, not an approximation.
HoleMask = namedtuple(
    "HoleMask",
    "contain deep_touch band_count band_ceiling heights cell "
    "origin_x origin_z elevation_scale elevation_offset fine area_shallow",
    defaults=(0,),
)


def hole_mask(bundle_dir, floor_override=None, prefer_fine=True):
    """The §8c decision rasters at the heightmap's own resolution, for a consumer that feeds
    the heightmap to its engine's terrain and punches holes instead of building a lattice
    (bake `terrainmask`). What each `HoleMask` field means is documented on the record above.
    """
    field = reader.load_heightfield(bundle_dir, prefer_fine=prefer_fine)
    if field is None:
        raise RuntimeError("hole_mask needs the terrain layer and its rasters block")
    raw = field.raw
    height, width = raw.shape
    grid = Grid(
        width,
        height,
        field.cell,
        float(field.origin_x),
        float(field.origin_z),
        field.elevation_scale,
        field.elevation_offset,
    )
    clip = clip_grid(bundle_dir, grid, raw.reshape(-1), floor_override=floor_override)
    heights = raw.astype(numpy.float64) * grid.elevation_scale + grid.elevation_offset
    return HoleMask(
        clip.contain,
        clip.deep_touch,
        clip.band_count,
        clip.band_ceiling,
        heights,
        grid.cell,
        grid.origin_x,
        grid.origin_z,
        grid.elevation_scale,
        grid.elevation_offset,
        field.fine,
        clip.area_shallow,
    )
