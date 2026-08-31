"""A merged world tile's LOD ladder, built from the game's own per-primitive cull distances.

A tile is thousands of separate things — road segments, lane markings, fences, painted surfaces —
merged into one `.dae` because 7,289 TSStatics is three times a vanilla level's whole object
count. CS2 culls every one of those things *individually* (`csiibundle.lod`: the composition rule
for a road, the lane rule for a marking, the per-triangle area rule for paint), and a merged tile
threw all of that away: A3 shipped every tile at `TILE_DETAIL_PX = 2`, which a 256 m tile only
falls below at about 85 km, so all 9.27M network and 1.59M marking triangles were live inside a
4 km view distance.

## What this builds, and why it cannot crack

Each LOD level of a tile is **the set of primitives CS2 still draws at that distance**. Nothing
is decimated, welded or moved: a triangle is either in a level or culled, exactly as the game
culls it. That is why the LOD switch cannot produce the cracks or popping a decimated road tile
would — the geometry that survives is bit-identical to LOD 0's, and the geometry that goes is
geometry the game had already stopped drawing.

## The two numbers, and which is which

* **`nulldetail`** is the game's: the tile disappears where its LAST primitive would have been
  culled by CS2 (the most generous of its contents — a tile is drawn while anything in it is).
  Divided by the one declared `lod_detail_scale` multiplier the object shapes already use.
* **The ladder spacing** (`RATIOS`) is BeamNG's, not the game's: CS2 has no LOD chain here at all,
  so where LOD 1 and LOD 2 take over is a consumer choice. They are multiples of the tile's own
  null threshold, so they scale with it rather than being absolute metres, and the *content* of
  each level is still the game's answer for the distance that multiple corresponds to.

A tile whose primitives all have the same cull distance gets one level and no ladder — which is
the honest outcome for a tile of uniform wide roads, and the report says so rather than implying
a saving that is not there.
"""

import math

import numpy
from csiibundle import lod

# A tile nothing culls (every primitive draws past the horizon) keeps the old always-on
# threshold: detail sizes must be > 0 or BeamNG refuses the shape ("invalid detail levels").
ALWAYS_PX = 2


# Where LOD 1 and LOD 2 take over, as multiples of the tile's null threshold, read off the
# vanilla BeamNG shapes in A2. OURS — coarsest first, so a level's size is `ratio * null_px` and
# its content is what the game draws at `nullDistance / ratio`.
#
# The OBJECT shapes no longer use ratios (`stages/shapes` derives each level's switch point from
# its measured displacement, A6.7) and a tile does not need to: a tile's levels are exact SUBSETS
# — the geometry that survives is bit-identical to LOD 0's and what goes is what the game had
# already stopped drawing — so a tile level's displacement is zero by construction and there is
# no error to budget.
RATIOS = (1.5, 6.0, 24.0)


# Beyond this the answer is "the game never stops drawing it" — a 16 m road's own limit works out
# at 41 km, past the far edge of any map, and treating that as a real distance produces a
# nulldetail of 0 px. Anything at or over this counts as unculled and is reported as such.
UNCULLED_METRES = 100000.0


# How wide a band around a tile's edge is held in place while the rest is clustered. A triangle
# belongs to the tile its CENTROID falls in (`bake/tiles.add`), so a tile's geometry can reach
# about one triangle past its nominal edge; this has to cover that reach on both sides or two
# neighbours will not pin the same vertices. 2 m covers every world-space product's triangles
# (the largest are area pieces, floored at 0.10 m, and network cross-sections). TUNED.
PIN_MARGIN_M = 2.0


def boundary_pins(verts, tile, margin=PIN_MARGIN_M):
    """Which vertices of a tile-local mesh sit in the band its neighbour also owns.

    Tile geometry is written relative to the tile CENTRE, so the tile spans ±`tile`/2 on both
    horizontal axes and a vertex shared with the tile to the east is at `+tile/2` here and
    `−tile/2` there. `|x| >= tile/2 − margin` is therefore the same test in both tiles and
    selects the same vertices — which is the whole point: `decimate.vertex_cluster` gives a
    pinned vertex a cluster of its own, so it comes out exactly where it went in and the two
    tiles still meet. Without this the two grids differ (the cell size is searched per mesh and
    the origin is the mesh's own corner) and the seam opens.
    """
    v = numpy.asarray(verts, dtype=numpy.float64)
    if len(v) == 0:
        return numpy.zeros(0, dtype=bool)
    edge = float(tile) * 0.5 - float(margin)
    return (numpy.abs(v[:, 0]) >= edge) | (numpy.abs(v[:, 1]) >= edge)


def cull_distances(min_lods, level_of_detail=lod.DEFAULT_LEVEL_OF_DETAIL):
    """`[metres]` for a list of the game's `m_MinLod` values; `None` (no limit known) → inf."""
    out = []
    for value in min_lods:
        if value is None:
            out.append(float("inf"))
        else:
            out.append(lod.cull_distance(int(value), level_of_detail))
    return out


def tile_levels(
    radius,
    distances_per_triangle,
    detail_scale=1.0,
    ratios=RATIOS,
    view_distance=None,
    distinct_only=True,
):
    """The whole ladder for one tile.

    `radius` is the tile's bounding-sphere radius — Torque measures a detail size against it —
    and `distances_per_triangle` is each triangle's own CS2 cull distance in metres (inf for a
    triangle nothing culls). Returns

        {"nullPx": int|None, "levels": [(px, boolean mask), ...], "gameNullMetres": float|None,
         "culledFraction": float}

    nearest level first. `nullPx` is None when nothing in the tile is ever culled inside a
    sensible distance; the caller then ships the single always-on level A3 shipped for
    everything, and the report says so rather than implying a saving that is not there.

    **`view_distance` is where the ladder is anchored, and `nullPx` is not.** A4 spaced the
    switches at `far/ratio` with `far` the game's own null distance, which for a road composition
    is 46 km: LOD 1 fired at 7.7 km, outside any view distance we ship, and a networks tile still
    carried 82.5 % of its triangles at its far level (measured, `lod.meanTrianglesKeptAtFarLevel`).
    The ladder read as a ladder and was not one. Anchoring at `min(far, view_distance)` puts the
    switches inside the world the player sees while `nullPx` keeps answering the game's question —
    the tile still disappears exactly where CS2 stops drawing it. FIDELITY on the null, TUNING on
    the spacing; §64 of the audit keeps the two apart.

    `distinct_only` drops a level whose visible set repeats the one before it, which is right
    when the levels differ ONLY in content: three copies of the same mesh are three times the
    file for nothing. It has to be off when the caller is going to DECIMATE the far levels,
    because then two levels with the same triangles are still two different meshes — and inside a
    view distance the game culls nothing (a road draws to 46 km), so every level's content is
    identical and de-duplication would collapse the whole ladder back to one.

    The **coarsest kept level takes the smallest ratio**, exactly as `stages/shapes._lod_levels`
    does and for the same reason: Torque uses the first detail whose size is at or below the
    current pixel size, so a shape whose only detail sat high would vanish in the gap above
    `nulldetail`.
    """
    d = numpy.asarray(distances_per_triangle, dtype=numpy.float64)
    if len(d) == 0 or radius <= 0.0:
        return {"nullPx": None, "levels": [], "gameNullMetres": None, "culledFraction": 0.0}
    far = float(d[numpy.isfinite(d)].max()) if numpy.isfinite(d).any() else float("inf")
    if not math.isfinite(far) or far >= UNCULLED_METRES:
        # The game never stops drawing this tile — skirts and the pre-clipped ground carry no
        # per-primitive limit at all — so `nullPx` stays None and it never disappears. That is a
        # statement about CONTENT, and it says nothing about how finely the content has to be
        # described at 3 km: the ladder's spacing was always ours (`RATIOS`), so an uncullable
        # tile still gets one when the caller is going to decimate. Without this, `skirts` was
        # 1.13 M triangles inside a 4 km view with no LOD chain at all — the second largest item
        # in the level, and the only product the A5 ladder did not touch.
        if not view_distance or distinct_only:
            return {
                "nullPx": None,
                "levels": [],
                "gameNullMetres": far if math.isfinite(far) else None,
                "culledFraction": 0.0,
            }
        anchor_px = lod.group_detail_px(radius, float(view_distance))
        base = min(float(r) for r in ratios)
        levels = []
        for ratio in sorted(float(r) for r in ratios):
            px = int(max(ALWAYS_PX, round(anchor_px * ratio / base)))
            levels.append((px, numpy.ones(len(d), dtype=bool)))
        levels.sort(key=lambda pair: -pair[0])
        fixed = []
        for px, mask in levels:
            if fixed and px >= fixed[-1][0]:
                px = fixed[-1][0] - 1
            if px >= ALWAYS_PX:
                fixed.append((px, mask))
        return {
            "nullPx": None,
            "levels": fixed,
            "gameNullMetres": None,
            "anchorMetres": float(view_distance),
            "culledFraction": 0.0,
        }
    null_px = lod.group_detail_px(radius, far) * float(detail_scale)
    if not math.isfinite(null_px) or null_px < ALWAYS_PX:
        return {"nullPx": None, "levels": [], "gameNullMetres": far, "culledFraction": 0.0}
    anchor = far if not view_distance else min(far, float(view_distance))
    # **`detail_scale` belongs to the null threshold, not to the ladder.** It is declared as "how
    # many times CLOSER than CS2 an object is culled", and Torque compares a detail's stored size
    # against the shape's REAL projected size — so multiplying the ladder's sizes by it pulls
    # every switch in by that factor too. On the A4 ladder that was invisible: `far` was 46 km, so
    # `far/detail_scale` still landed past the view distance. Anchored at the view distance it is
    # not invisible at all — with `detail_scale` 10 the coarsest level would have stopped drawing
    # at 267 m and the level would have had a hole beyond it. Caught by `tools/level_budget.py`
    # (35,097 triangles inside 4 km, which is not a level), not by anything that looks at the
    # build report.
    #
    # So the ladder is spaced on the RATIOS alone, normalised so the coarsest level's size is the
    # tile's projected size at the anchor — it draws out to exactly there — and each finer level
    # is `ratio/base` times bigger, i.e. takes over at `anchor * base / ratio`. With (1.5, 6, 24)
    # and a 4 km anchor that is: full detail inside 250 m, LOD 1 to 1 km, LOD 2 to 4 km.
    ordered = sorted(float(r) for r in ratios)
    base = ordered[0]
    scale_ladder = bool(view_distance)
    anchor_px = lod.group_detail_px(radius, anchor) * (1.0 if scale_ladder else float(detail_scale))
    masks = []
    for ratio in ordered:
        mask = d >= (anchor * base / ratio if scale_ladder else anchor / ratio)
        if not mask.any():
            continue
        if (
            distinct_only
            and masks
            and mask.shape == masks[-1][1].shape
            and bool((mask == masks[-1][1]).all())
        ):
            continue
        masks.append((ratio, mask))
    if not masks:
        return {"nullPx": None, "levels": [], "gameNullMetres": far, "culledFraction": 0.0}
    levels = []
    for _index, (ratio, mask) in enumerate(masks):
        # px scales exactly with 1/distance, so the size at `anchor * base / ratio` is
        # `anchor_px * ratio / base` — and at `anchor / ratio` (the unanchored case) `anchor_px *
        # ratio`, which is what A4 shipped.
        px = int(max(ALWAYS_PX, round(anchor_px * (ratio / base if scale_ladder else ratio))))
        levels.append((px, mask))
    levels.sort(key=lambda pair: -pair[0])  # nearest (largest px) first
    fixed = []
    for px, mask in levels:  # strictly decreasing, or Torque reads two as one
        if fixed and px >= fixed[-1][0]:
            px = fixed[-1][0] - 1
        if px < ALWAYS_PX:
            continue
        fixed.append((px, mask))
    kept = int(fixed[-1][1].sum()) if fixed else len(d)
    null = int(max(ALWAYS_PX, round(null_px)))
    null_lowered = None
    if fixed and null >= fixed[-1][0]:
        # THE NULL SITS UNDER THE COARSEST LEVEL, ALWAYS. The null is the game's cull scaled by
        # `detail_scale`; the coarsest level's size is the tile's projected size at the ANCHOR,
        # unscaled - so wherever the game's own cull is INSIDE the view distance (every
        # markings tile: a lane marking's cull is a few km) the anchor IS that cull and the null
        # lands `detail_scale` times ABOVE the coarsest level. Torque picks the largest detail
        # size at or under the projected size, so the whole band from the null's size up to
        # LOD 0 went to the null: the tile vanished in the mid-range and came back nearer
        # (validation pass 2026-08-31, A1; `tools/beamng/lod_audit.select` states the rule).
        # A Torque detail's size is where it STOPS, so the ladder keeps its sizes and the null
        # comes down under the coarsest one - the rule `stages/shapewrite._above_null` applies
        # to object shapes. DECLARED: such a tile draws to its ladder's anchor (the game's own
        # cull, or the view distance), not to the scaled cull - the scale is a residency lever
        # for object shapes, and pulling the anchor in with it would pull LOD 0 in too.
        null_lowered = {"from": null, "to": max(1, fixed[-1][0] - 1)}
        null = max(1, fixed[-1][0] - 1)
    return {
        "nullPx": null,
        "levels": fixed,
        "gameNullMetres": far,
        "anchorMetres": anchor,
        "culledFraction": round(1.0 - kept / max(1, len(d)), 4),
        "nullLowered": null_lowered,
    }
