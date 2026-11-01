"""Applying a LOD ladder to a merged tile mesh."""

import numpy
from csiibundle import decimate, decimatemeshlab, displacement
from csiibundle.bake.tiles import split_material_name

from . import daemesh
from .tilelod import ALWAYS_PX, boundary_pins, cull_distances, lod, tile_levels

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
    through `daemesh.group_by_material` — so this is one value per run, expanded.
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
    return dae.Mesh(
        mesh.name,
        mesh.verts,
        mesh.tris,
        merged,
        normals=mesh.normals,
        uvs=mesh.uvs,
        colors=mesh.colors,
    )


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
        kept = int(keep[cursor : cursor + count].sum())
        if kept:
            runs.append((name, kept))
        cursor += count
    tris = numpy.asarray(mesh.tris)[keep]
    used = numpy.unique(tris.ravel())
    remap = numpy.zeros(int(used.max()) + 1, dtype=numpy.int64)
    remap[used] = numpy.arange(len(used))
    return dae.Mesh(
        shape_name,
        mesh.verts[used],
        remap[tris],
        runs,
        normals=None if mesh.normals is None else mesh.normals[used],
        uvs=None if mesh.uvs is None else mesh.uvs[used],
        colors=None if mesh.colors is None else mesh.colors[used],
    )


def radius_of(mesh):
    """The tile's bounding-SPHERE radius, which is what a Torque detail size is measured against."""
    if len(mesh.verts) == 0:
        return 0.0
    lo = numpy.asarray(mesh.verts.min(axis=0), dtype=numpy.float64)
    hi = numpy.asarray(mesh.verts.max(axis=0), dtype=numpy.float64)
    return float(numpy.linalg.norm(hi - lo) * 0.5)


def build_levels(
    dae,
    shape_name,
    mesh,
    detail_scale,
    level_of_detail=lod.DEFAULT_LEVEL_OF_DETAIL,
    always_px=ALWAYS_PX,
    view_distance=None,
    targets=TILE_TARGETS,
    tile=None,
    pins=True,
    refine_error=0.0,
):
    """`([(px, [Mesh]), ...], ladder)` for one merged tile, ready for `dae.Shape`.

    `pins=False` decimates without holding the tile's boundary band: for a product whose
    geometry is not clipped at the tile edge there is no seam to protect, and the band
    only prevents the decimation (`boundary_pins`, `stages/networks`).

    `dae` is passed in rather than imported so this module stays a pure function of its inputs
    and the `.dae` writer keeps its one home.

    Two things shape a level. **Which triangles it may contain** is the game's answer — those CS2
    still draws at that level's distance — and never changes. **How finely those triangles are
    then described** is the consumer's, and `targets` decimates every level past the nearest one
    through `csiibundle.decimate`, holding the tile's boundary band in place so two neighbours
    still meet (`boundary_pins`). `targets=None` or `tile=None` keeps A4's behaviour: the levels
    are exact subsets and nothing is simplified.

    The nearest level is never CLUSTERED, for the reason above: it is what you stand on, and it
    is the detail a `Visible Mesh` collider rasterises. `refine_error` is the one thing allowed
    to touch it — a MeshLab collapse whose every edge lands on an EXISTING vertex and whose
    measured p95 displacement is held under that many metres, so the surface the car drives on
    provably does not move. It is a far tighter number than a shape's (1 cm against 3), because
    here a centimetre is a bump and not a silhouette. 0 keeps the level exactly as it was.
    """
    distances = tri_distances(mesh, level_of_detail)
    mesh = strip_marks(dae, mesh)
    decimating = bool(targets and tile)
    ladder = tile_levels(
        radius_of(mesh),
        distances,
        detail_scale,
        view_distance=view_distance,
        distinct_only=not decimating,
    )
    out = []
    decimated = []
    refined = []
    for index, (px, mask) in enumerate(ladder["levels"]):
        part = subset(dae, shape_name, mesh, mask)
        if part is None:
            continue
        ratio = None
        if targets and tile and index > 0 and index - 1 < len(targets):
            ratio = float(targets[index - 1])
        if ratio and len(part.tris) > 16:
            before = len(part.tris)
            part = _decimated(dae, shape_name, part, ratio, tile, pins)
            decimated.append((before, len(part.tris)))
        elif index == 0 and refine_error and len(part.tris) >= REFINE_MIN_TRIS:
            before = len(part.tris)
            part, moved = _refined(dae, shape_name, part, refine_error)
            if len(part.tris) != before:
                refined.append((before, len(part.tris), moved))
        out.append((px, daemesh.split_mesh(part)))
    ladder["decimated"] = decimated
    ladder["refined"] = refined
    if not out:
        ladder["nullPx"] = None
        return [(always_px, daemesh.split_mesh(mesh))], ladder
    return out, ladder


# The nearest level of a tile is refined only where at least this many triangles make the MeshLab
# round trip worth its seconds; a small tile has nothing to find.
REFINE_MIN_TRIS = 2_000
# Deepest first, first pass wins - the shape rule's ladder (`bake/lods.REFINE_TARGETS`).
REFINE_TARGETS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _refined(dae, shape_name, part, max_error):
    """`(part, moved)` - the deepest MeshLab collapse of the NEAREST level whose measured p95
    displacement stays under `max_error` metres, or the level untouched.

    This is the only simplification the nearest level of a tile is allowed, and the bound is what
    makes it allowed: a road tile ships `Visible Mesh Final`, so this mesh IS the surface the car
    drives on. MeshLab's collapse lands on existing vertices, so a flat run of coplanar road
    triangles merges at literally zero displacement while a crowned or kerbed one measures and is
    refused. Every candidate is judged on its own (the error is not monotone in the reduction).
    """
    if not decimatemeshlab.available():
        return part, None
    source = {
        "verts": numpy.asarray(part.verts, dtype=numpy.float64),
        "normals": None
        if part.normals is None
        else numpy.asarray(part.normals, dtype=numpy.float64),
        "uvs": None if part.uvs is None else numpy.asarray(part.uvs, dtype=numpy.float64),
        "tris": numpy.asarray(part.tris, dtype=numpy.int64),
        "material_runs": list(part.runs),
    }
    for ratio in REFINE_TARGETS:
        try:
            level = decimate.lod_ladder(source, targets=(ratio,), backend="meshlab")[0][0]
        except Exception:
            continue
        if len(level["tris"]) == 0 or len(level["tris"]) >= len(source["tris"]):
            continue
        try:
            p95 = float(displacement.level_displacement(level, source)[0])
        except Exception:
            continue
        if p95 > max_error:
            continue
        return (
            dae.Mesh(
                shape_name,
                level["verts"],
                level["tris"],
                level["material_runs"],
                normals=level["normals"],
                uvs=level["uvs"],
                colors=None,
            ),
            round(p95, 4),
        )
    return part, None


def _decimated(dae, shape_name, part, ratio, tile, pins=True):
    """One level clustered to `ratio` of its triangles, boundary band pinned unless `pins`
    is off."""
    source = {
        "verts": numpy.asarray(part.verts, dtype=numpy.float64),
        "normals": None
        if part.normals is None
        else numpy.asarray(part.normals, dtype=numpy.float64),
        "uvs": None if part.uvs is None else numpy.asarray(part.uvs, dtype=numpy.float64),
        "tris": numpy.asarray(part.tris, dtype=numpy.int64),
        "material_runs": list(part.runs),
    }
    pinned = boundary_pins(source["verts"], tile) if pins else None
    result = decimate.cluster_mesh(source, ratio, pinned=pinned)
    if len(result["tris"]) == 0:
        return part
    return dae.Mesh(
        shape_name,
        result["verts"],
        result["tris"],
        result["material_runs"],
        normals=result["normals"],
        uvs=result["uvs"],
        colors=None,
    )


def refined_summary(stats):
    """What refining the nearest levels bought, or None when it bought nothing."""
    if not stats.get("refinedTiles"):
        return None
    a, b = stats["refinedFrom"], stats["refinedTo"]
    return {
        "tiles": stats["refinedTiles"],
        "from": a,
        "to": b,
        "percentOut": round(100.0 * (1.0 - b / a), 1) if a else 0.0,
        "worstMetres": round(stats.get("refinedWorst") or 0.0, 4),
    }


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
        # tiles whose scaled cull sat inside their own ladder, so the null was lowered under the
        # coarsest level (`tilelod.tile_levels`, A1) - they draw to the anchor, not the scaled cull
        "nullLoweredTiles": stats.get("nullLowered", 0),
    }


def new_stats():
    return {
        "tiles": 0,
        "levels": 0,
        "culled": 0.0,
        "unculled": 0,
        "nullMetres": [],
        "decimatedFrom": 0,
        "decimatedTo": 0,
        "refinedTiles": 0,
        "refinedFrom": 0,
        "refinedTo": 0,
        "refinedWorst": 0.0,
        "anchorMetres": [],
        "nullLowered": 0,
    }


def compact(levels, ladder):
    # `decimated` is `[(before, after), ...]` per decimated level; summed here so the per-tile
    # detail does not cross a process boundary.
    """One tile's ladder as the handful of numbers `account` needs.

    `ladder["levels"]` carries a boolean mask per level — one bit per triangle of the tile — so a
    worker process must not hand the whole thing back. This is what crosses the pipe instead.
    """
    pairs = ladder.get("decimated") or []
    return {
        "levels": len(levels),
        "culledFraction": ladder.get("culledFraction") or 0.0,
        "gameNullMetres": ladder.get("gameNullMetres"),
        "anchorMetres": ladder.get("anchorMetres"),
        "decimatedFrom": sum(a for a, _b in pairs),
        "decimatedTo": sum(b for _a, b in pairs),
        "nullLowered": bool(ladder.get("nullLowered")),
        # the NEAREST level's measured refinement (`_refined`), which the ladder never touches
        "refined": [(a, b, m) for a, b, m in (ladder.get("refined") or []) if a is not None],
    }


def account(stats, levels, ladder):
    account_compact(stats, compact(levels, ladder))


def account_compact(stats, item):
    stats["tiles"] += 1
    stats["levels"] += item["levels"]
    stats["culled"] += item["culledFraction"] or 0.0
    stats["decimatedFrom"] = stats.get("decimatedFrom", 0) + (item.get("decimatedFrom") or 0)
    stats["decimatedTo"] = stats.get("decimatedTo", 0) + (item.get("decimatedTo") or 0)
    stats["nullLowered"] = stats.get("nullLowered", 0) + (1 if item.get("nullLowered") else 0)
    for before, after, moved in item.get("refined") or ():
        stats["refinedTiles"] = stats.get("refinedTiles", 0) + 1
        stats["refinedFrom"] = stats.get("refinedFrom", 0) + before
        stats["refinedTo"] = stats.get("refinedTo", 0) + after
        if moved is not None:
            stats["refinedWorst"] = max(stats.get("refinedWorst", 0.0), moved)
    if item.get("anchorMetres") is not None:
        stats.setdefault("anchorMetres", []).append(item["anchorMetres"])
    if item.get("gameNullMetres") is not None:
        stats["nullMetres"].append(item["gameNullMetres"])
    else:
        stats["unculled"] += 1
