"""decalLayers - keep terrain-only paint OFF the carriageway (A6.5).

**CS2's own mask, not a rule of ours.** `decalLayers` is what the game targets a projection
at (§4.2), and a surface whose layers are `["Terrain"]` is never painted onto roads. We
conformed every area across its whole polygon regardless and then LIFTED it above the
terrain, so on the reference city 472,957 m² of terrain-only paint lay over a carriageway and
stood proud of it over three quarters of the overlap — which is what buries roads at a
grazing angle.

`bake/markings.py` has picked its target from the same field since A2
(`decal_blocks_ground` vs `decal_blocks_road`); areas simply never called it.

Runs in the PARENT, after the conform, because the road soup is far too large to ship into
every conform worker and this is the only place both surfaces exist at once. Vectorised over
all of a prefab's pieces at once.

DECLARED APPROXIMATION: the cut follows the conform's own piece boundaries (sub-cell, so
<= 1.75 m on the fine raster), not the kerb, so the edge is ragged by up to one piece. And
the test is per-CENTROID, so a piece straddling a kerb goes whole or stays whole.
"""

import numpy

# How far above a piece a road surface may sit and still count as "on top of it". A bridge is
# metres up and the paint under it is real; a carriageway that a plaza runs into is centimetres.
ROAD_AT_GRADE_M = 1.0
# and how far BELOW, so a road that is a hair under the lifted paint still counts as covering it
# (the paint carries `AREA_LIFT` plus its priority rung — 2 cm and up — by construction).
ROAD_UNDER_TOLERANCE_M = 0.30


def _log_clip(stats, progress):
    """One line saying what the clip did — LOUD when no prefab matched at all, because a silent
    zero is indistinguishable from a working clip with nothing to do, which is exactly how the
    first cut of this shipped."""
    if stats["pieces"]:
        progress.log(
            f"areas: decalLayers - {stats['area']:,.0f} m2 of terrain-only paint clipped "
            f"off carriageways ({stats['pieces']:,} pieces over {stats['prefabs']} prefabs)"
        )
    elif not stats["terrainOnlyPrefabs"]:
        progress.log(
            "areas: decalLayers - NO prefab declared terrain-only layers; the catalogue "
            "may not carry `decalLayers` at all, so nothing was clipped"
        )
    else:
        progress.log(
            f"areas: decalLayers - {stats['terrainOnlyPrefabs']} terrain-only prefabs, "
            f"none of them over a carriageway"
        )


def clip_roads(pieces, catalogue, road, _ground, progress):
    """Drop the paint of `["Terrain"]`-only prefabs where a road surface is on top of it —
    CS2's own `decalLayers` mask, applied per conformed piece; the story, the measured overlap
    and the declared approximation are the module docstring."""
    stats = {
        "prefabs": 0,
        "pieces": 0,
        "area": 0.0,
        "kept": 0,
        "terrainOnlyPrefabs": 0,
        "roadTris": int(getattr(road, "count", 0) or 0),
    }
    if road is None or not getattr(road, "count", 0):
        progress.log("areas: decalLayers - no road surface, nothing to clip")
        return pieces, stats
    out = {}
    for prefab, (verts, min_lods) in pieces.items():
        entry = (catalogue or {}).get(prefab) or {}
        # `reader.load_area_prefabs` hands back the RAW §8c entries, so the key is camelCase
        # `decalLayers`. `areas.surface_entry` renames it to `decal_layers`, and reading only
        # that name is how the first cut of this silently clipped NOTHING and reported 0 m2 as
        # though nothing needed clipping. Accept both, and count how many prefabs matched so a
        # zero can never again be mistaken for "no work to do".
        layers = tuple(entry.get("decalLayers") or entry.get("decal_layers") or ())
        if not layers or "Roads" in layers:
            out[prefab] = (verts, min_lods)  # the game DOES paint these onto roads
            continue
        stats["terrainOnlyPrefabs"] += 1
        tri = verts.reshape(-1, 3, 3)
        centroid = tri.mean(axis=1)
        # `height` is the SCALAR api on every ProjectionSurface and returns None for arrays;
        # `height_many` is the vectorised one. Getting this wrong reads as "no road anywhere".
        road_z = road.height_many(centroid[:, 0], centroid[:, 1])
        delta = road_z - centroid[:, 2]
        covered = (
            (~numpy.isnan(road_z)) & (delta <= ROAD_AT_GRADE_M) & (delta >= -ROAD_UNDER_TOLERANCE_M)
        )
        if not covered.any():
            out[prefab] = (verts, min_lods)
            continue
        edge1 = tri[covered, 1, :2] - tri[covered, 0, :2]
        edge2 = tri[covered, 2, :2] - tri[covered, 0, :2]
        stats["area"] += (
            float(numpy.abs(edge1[:, 0] * edge2[:, 1] - edge1[:, 1] * edge2[:, 0]).sum()) * 0.5
        )
        stats["pieces"] += int(covered.sum())
        stats["prefabs"] += 1
        keep = ~covered
        stats["kept"] += int(keep.sum())
        if not keep.any():
            continue  # the whole surface was carriageway
        out[prefab] = (tri[keep].reshape(-1, 3), min_lods[keep])
    _log_clip(stats, progress)
    return out, stats
