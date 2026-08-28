"""The game's own draw-distance limit, per lane prefab and per decal instance (A5).

CS2 culls a marking by its PREFAB's `NetLaneGeometryData.m_MinLod` (`csiibundle.lod`), which is
the min over the prefab's meshes of the flat-thing rule on `Size(bounds).xy` plus that mesh's
triangle-area bias. A tile merges thousands of markings, so the limit rides into the tile with
each primitive (`TileSink.add(min_lod=...)`) and a consumer rebuilds the visible set per
distance. Without this a lane line drawn to 406 m in CS2 is drawn to the whole view distance.
"""

import os

import numpy

from .. import frames, glb, lanes, lod


def lane_prefab_min_lod(prefab, entry, sources, lay, cache):
    """`NetLaneGeometryData.m_MinLod` for one lane prefab — the min over ALL its parts, decal and
    non-decal alike, because that is the set the game takes the min over."""
    if prefab in cache:
        return cache[prefab]
    meshes = []
    for part in entry.get("parts", []) or []:
        asset_id = part.get("assetId")
        if not asset_id:
            continue
        if asset_id not in sources:
            path = os.path.join(lay.lane_meshes, asset_id + ".glb")
            sources[asset_id] = (
                lanes.part_source(glb.parse_glb(path)) if os.path.isfile(path) else None
            )
        source = sources[asset_id]
        if source is None or len(source["tris"]) == 0:
            continue
        verts = numpy.asarray(source["verts"], dtype=numpy.float64)
        size = verts.max(axis=0) - verts.min(axis=0)
        meshes.append((size, lod.surface_area(verts, source["tris"])))
    answer = lod.lane_min_lod(meshes) if meshes else None
    cache[prefab] = answer
    return answer


def decal_min_lod(asset_id, quads, cache):
    """A placed decal is an OBJECT, so it takes the object rule (`lod.mesh_min_lod`): its own
    bounds, clamped axis by axis, with the surface-area bias."""
    if asset_id in cache:
        return cache[asset_id]
    lo = hi = None
    area = 0.0
    for corners, _uvs, _material, node_matrix in quads:
        pts = frames.transform_points(numpy.asarray(node_matrix, dtype=numpy.float64), corners)
        lo = pts.min(axis=0) if lo is None else numpy.minimum(lo, pts.min(axis=0))
        hi = pts.max(axis=0) if hi is None else numpy.maximum(hi, pts.max(axis=0))
        area += (
            lod.surface_area(pts, numpy.array([[0, 1, 2], [0, 2, 3]], dtype=numpy.int64))
            if len(pts) >= 4
            else 0.0
        )
    answer = None if lo is None else lod.mesh_min_lod(hi - lo, area)
    cache[asset_id] = answer
    return answer
