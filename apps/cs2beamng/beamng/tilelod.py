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

from csiibundle import decimate, lod
from csiibundle.bake.tiles import split_material_name

# Where LOD 1 and LOD 2 take over, as multiples of the tile's null threshold. The same spacing
# the object shapes use (`stages/shapes.LADDER_RATIOS`), read off the vanilla BeamNG shapes in
# A2. OURS — coarsest first, so a level's size is `ratio * null_px` and its content is what the
# game draws at `nullDistance / ratio`.
RATIOS = (1.5, 6.0, 24.0)

# A tile nothing culls (every primitive draws past the horizon) keeps the old always-on
# threshold: detail sizes must be > 0 or BeamNG refuses the shape ("invalid detail levels").
ALWAYS_PX = 2

# Beyond this the answer is "the game never stops drawing it" — a 16 m road's own limit works out
# at 41 km, past the far edge of any map, and treating that as a real distance produces a
# nulldetail of 0 px. Anything at or over this counts as unculled and is reported as such.
UNCULLED_METRES = 100000.0

# How much of each level survives, nearest level first after LOD 0 (which is never touched: it is
# what you stand on, and it is what a `Visible Mesh*` collider rasterises — the engine's own enum
# calls "Visible Mesh Final" *"Rendered mesh polygons: wont fix"*, i.e. an intent tag beside
# "Visible Mesh"'s *"to be fixed"*, NOT a final detail level; BeamNG's resource checker measures
# visible-mesh collision at `setCurrentDetail(0)`).
#
# The same ratios `stages/shapes` has shipped since A2, deliberately. Keeping the tile's
# triangles-per-PIXEL constant instead would allow far less — the far level switches at 16x the
# near one's distance, so 1/256 would hold the density — and that is the number to revisit if
# these do not buy enough. Starting at a ladder already validated in game is the cheaper
# experiment. OURS (BeamNG's side of the fence: CS2 has no LOD chain here at all).
TILE_TARGETS = (0.25, 0.06)

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


def tile_levels(radius, distances_per_triangle, detail_scale=1.0, ratios=RATIOS,
                view_distance=None, distinct_only=True):
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
            return {"nullPx": None, "levels": [], "gameNullMetres": far if math.isfinite(far) else None,
                    "culledFraction": 0.0}
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
        return {"nullPx": None, "levels": fixed, "gameNullMetres": None,
                "anchorMetres": float(view_distance), "culledFraction": 0.0}
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
        if distinct_only and masks and mask.shape == masks[-1][1].shape \
                and bool((mask == masks[-1][1]).all()):
            continue
        masks.append((ratio, mask))
    if not masks:
        return {"nullPx": None, "levels": [], "gameNullMetres": far, "culledFraction": 0.0}
    levels = []
    for (index, (ratio, mask)) in enumerate(masks):
        # px scales exactly with 1/distance, so the size at `anchor * base / ratio` is
        # `anchor_px * ratio / base` — and at `anchor / ratio` (the unanchored case) `anchor_px *
        # ratio`, which is what A4 shipped.
        px = int(max(ALWAYS_PX, round(anchor_px * (ratio / base if scale_ladder else ratio))))
        levels.append((px, mask))
    levels.sort(key=lambda pair: -pair[0])              # nearest (largest px) first
    fixed = []
    for px, mask in levels:                            # strictly decreasing, or Torque reads two as one
        if fixed and px >= fixed[-1][0]:
            px = fixed[-1][0] - 1
        if px < ALWAYS_PX:
            continue
        fixed.append((px, mask))
    kept = int(fixed[-1][1].sum()) if fixed else len(d)
    return {"nullPx": int(max(ALWAYS_PX, round(null_px))), "levels": fixed, "gameNullMetres": far,
            "anchorMetres": anchor,
            "culledFraction": round(1.0 - kept / max(1, len(d)), 4)}


# -- applying the ladder to a merged tile mesh -------------------------------------------------------
#
# Both tile stages (`stages/derived`, `stages/networks`) do the same three things: read each
# triangle's limit off the material-name mark the bake wrote, strip the mark so the `.dae` binds a
# real material name, and cut the mesh down to each level's visible set. They live here rather
# than in one of the two stages because a rule that only one consumer gets is in the wrong place.

def tri_distances(mesh, level_of_detail=lod.DEFAULT_LEVEL_OF_DETAIL):
    """Per-triangle CS2 cull distance, in the mesh's own triangle order.

    A limit is constant within a material run by construction — the bake puts triangles with
    different limits into different primitives, and the `#L<n>` mark travels on the material name
    through `dae.group_by_material` — so this is one value per run, expanded.
    """
    values = []
    for name, count in mesh.runs:
        _base, _variant, min_lod = split_material_name(name)
        values.extend([min_lod] * int(count))
    return cull_distances(values, level_of_detail)


def strip_marks(dae, mesh):
    """The same mesh with `#L<n>` off its material names, adjacent runs of one material rejoined."""
    runs = [(split_material_name(name)[0], count) for name, count in mesh.runs]
    if runs == mesh.runs:
        return mesh
    merged = []
    for name, count in runs:
        if merged and merged[-1][0] == name:
            merged[-1] = (name, merged[-1][1] + count)
        else:
            merged.append((name, count))
    return dae.Mesh(mesh.name, mesh.verts, mesh.tris, merged, normals=mesh.normals,
                    uvs=mesh.uvs, colors=mesh.colors)


def subset(dae, shape_name, mesh, mask):
    """A copy of `mesh` holding only the masked triangles, material runs recomputed."""
    keep = numpy.asarray(mask, dtype=bool)
    if bool(keep.all()):
        return mesh
    if not bool(keep.any()):
        return None
    runs = []
    cursor = 0
    for name, count in mesh.runs:
        kept = int(keep[cursor:cursor + count].sum())
        if kept:
            runs.append((name, kept))
        cursor += count
    tris = numpy.asarray(mesh.tris)[keep]
    used = numpy.unique(tris.ravel())
    remap = numpy.zeros(int(used.max()) + 1, dtype=numpy.int64)
    remap[used] = numpy.arange(len(used))
    return dae.Mesh(shape_name, mesh.verts[used], remap[tris], runs,
                    normals=None if mesh.normals is None else mesh.normals[used],
                    uvs=None if mesh.uvs is None else mesh.uvs[used],
                    colors=None if mesh.colors is None else mesh.colors[used])


def radius_of(mesh):
    """The tile's bounding-SPHERE radius, which is what a Torque detail size is measured against."""
    if len(mesh.verts) == 0:
        return 0.0
    lo = numpy.asarray(mesh.verts.min(axis=0), dtype=numpy.float64)
    hi = numpy.asarray(mesh.verts.max(axis=0), dtype=numpy.float64)
    return float(numpy.linalg.norm(hi - lo) * 0.5)


def build_levels(dae, shape_name, mesh, detail_scale, level_of_detail=lod.DEFAULT_LEVEL_OF_DETAIL,
                 always_px=ALWAYS_PX, view_distance=None, targets=TILE_TARGETS, tile=None):
    """`([(px, [Mesh]), ...], ladder)` for one merged tile, ready for `dae.Shape`.

    `dae` is passed in rather than imported so this module stays a pure function of its inputs
    and the `.dae` writer keeps its one home.

    Two things shape a level. **Which triangles it may contain** is the game's answer — those CS2
    still draws at that level's distance — and never changes. **How finely those triangles are
    then described** is the consumer's, and `targets` decimates every level past the nearest one
    through `csiibundle.decimate`, holding the tile's boundary band in place so two neighbours
    still meet (`boundary_pins`). `targets=None` or `tile=None` keeps A4's behaviour: the levels
    are exact subsets and nothing is simplified.

    The nearest level is never decimated. It is what you stand on, and it is the detail a
    `Visible Mesh` collider rasterises.
    """
    distances = tri_distances(mesh, level_of_detail)
    mesh = strip_marks(dae, mesh)
    decimating = bool(targets and tile)
    ladder = tile_levels(radius_of(mesh), distances, detail_scale, view_distance=view_distance,
                         distinct_only=not decimating)
    out = []
    decimated = []
    for index, (px, mask) in enumerate(ladder["levels"]):
        part = subset(dae, shape_name, mesh, mask)
        if part is None:
            continue
        ratio = None
        if targets and tile and index > 0 and index - 1 < len(targets):
            ratio = float(targets[index - 1])
        if ratio and len(part.tris) > 16:
            before = len(part.tris)
            part = _decimated(dae, shape_name, part, ratio, tile)
            decimated.append((before, len(part.tris)))
        out.append((px, dae.split_mesh(part)))
    ladder["decimated"] = decimated
    if not out:
        ladder["nullPx"] = None
        return [(always_px, dae.split_mesh(mesh))], ladder
    return out, ladder


def _decimated(dae, shape_name, part, ratio, tile):
    """One level clustered to `ratio` of its triangles, boundary band pinned."""
    source = {"verts": numpy.asarray(part.verts, dtype=numpy.float64),
              "normals": None if part.normals is None else numpy.asarray(part.normals, dtype=numpy.float64),
              "uvs": None if part.uvs is None else numpy.asarray(part.uvs, dtype=numpy.float64),
              "tris": numpy.asarray(part.tris, dtype=numpy.int64),
              "material_runs": list(part.runs)}
    result = decimate.cluster_mesh(source, ratio, pinned=boundary_pins(source["verts"], tile))
    if len(result["tris"]) == 0:
        return part
    return dae.Mesh(shape_name, result["verts"], result["tris"], result["material_runs"],
                    normals=result["normals"], uvs=result["uvs"], colors=None)


def ladder_summary(stats, detail_scale):
    """What the ladder actually bought — including the tiles it bought nothing on, which is the
    honest outcome for anything the game never culls."""
    tiles = max(1, stats["tiles"])
    metres = sorted(stats["nullMetres"])
    anchors = sorted(stats.get("anchorMetres") or [])
    return {
        "tiles": stats["tiles"],
        "levelsPerTile": round(stats["levels"] / tiles, 2),
        "meanTrianglesKeptAtFarLevel": round(1.0 - stats["culled"] / tiles, 4),
        "tilesNeverCulled": stats["unculled"],
        "gameNullMetresMedian": round(metres[len(metres) // 2], 1) if metres else None,
        "anchorMetresMedian": round(anchors[len(anchors) // 2], 1) if anchors else None,
        "decimatedTriangles": [stats.get("decimatedFrom", 0), stats.get("decimatedTo", 0)],
        "detailScale": detail_scale,
    }


def new_stats():
    return {"tiles": 0, "levels": 0, "culled": 0.0, "unculled": 0, "nullMetres": [],
            "decimatedFrom": 0, "decimatedTo": 0, "anchorMetres": []}


def compact(levels, ladder):
    # `decimated` is `[(before, after), ...]` per decimated level; summed here so the per-tile
    # detail does not cross a process boundary.
    """One tile's ladder as the handful of numbers `account` needs.

    `ladder["levels"]` carries a boolean mask per level — one bit per triangle of the tile — so a
    worker process must not hand the whole thing back. This is what crosses the pipe instead.
    """
    pairs = ladder.get("decimated") or []
    return {"levels": len(levels), "culledFraction": ladder.get("culledFraction") or 0.0,
            "gameNullMetres": ladder.get("gameNullMetres"),
            "anchorMetres": ladder.get("anchorMetres"),
            "decimatedFrom": sum(a for a, _b in pairs), "decimatedTo": sum(b for _a, b in pairs)}


def account(stats, levels, ladder):
    account_compact(stats, compact(levels, ladder))


def account_compact(stats, item):
    stats["tiles"] += 1
    stats["levels"] += item["levels"]
    stats["culled"] += item["culledFraction"] or 0.0
    stats["decimatedFrom"] = stats.get("decimatedFrom", 0) + (item.get("decimatedFrom") or 0)
    stats["decimatedTo"] = stats.get("decimatedTo", 0) + (item.get("decimatedTo") or 0)
    if item.get("anchorMetres") is not None:
        stats.setdefault("anchorMetres", []).append(item["anchorMetres"])
    if item.get("gameNullMetres") is not None:
        stats["nullMetres"].append(item["gameNullMetres"])
    else:
        stats["unculled"] += 1
