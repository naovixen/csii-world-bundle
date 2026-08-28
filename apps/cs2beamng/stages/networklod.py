"""The game's own draw-distance limit per network segment, and cutting a tile to fit.

A network tile is one shape however many segments merged into it, so its threshold is the
nearest of theirs — the tile has to still be drawn when the closest thing in it is."""

from collections import defaultdict

import numpy
from csiibundle import lod
from csiibundle.bake.tiles import MIN_LOD_MARK

from ..beamng import dae
from ..beamng.matbind import mapto_name

# -- the game's own draw-distance limit, per segment -------------------------------------------------


def segment_min_lod(record, nodes):
    """`NetCompositionData.m_MinLod` for one segment — `NetCompositionHelpers.CalculateMinLod`.

    The game's input is the COMPOSITION's cross-section: `GetRenderingSize(float2(m_Width,
    Size(m_HeightRange)))` with the mean of the pieces' mesh biases. The bundle ships `width` on
    the segment record (§5) but not the height range and not the piece list, so:

    * width comes from the record, falling back to the mesh's smaller horizontal extent;
    * the height range is the assembled mesh's vertical extent CAPPED AT THE WIDTH — a bounded
      over-estimate, because a composition's cross-section is never taller than it is wide and a
      sloped bridge's mesh is. Over-estimating draws further, never shorter;
    * the bias is 0, which is the same direction: a positive bias only pulls the distance in.

    Both are declared in the stage's report. An edge is culled on the MINIMUM of its three
    compositions (`PreCullingSystem`), i.e. the most generous — which is what a per-segment
    number already is here, since we have one assembled mesh per segment rather than three.
    """
    width = (record.get("attrs") or {}).get("width")
    lo = hi = None
    for node in nodes or []:
        verts = numpy.asarray(node["verts"], dtype=numpy.float64)
        if len(verts) == 0:
            continue
        m = numpy.asarray(node["matrix"], dtype=numpy.float64)
        world = verts @ m[:3, :3].T + m[:3, 3]
        lo = world.min(axis=0) if lo is None else numpy.minimum(lo, world.min(axis=0))
        hi = world.max(axis=0) if hi is None else numpy.maximum(hi, world.max(axis=0))
    if lo is None:
        return None
    size = hi - lo
    if width is None:
        width = float(min(size[0], size[2]))
    width = float(width)
    if width <= 0.0:
        return None
    height_range = float(min(max(size[1], 0.0), width))
    return lod.composition_min_lod(width, height_range, 0.0)


def split_tile(pieces, limit):
    """Quarter a tile's pieces by their origin until every part is under `limit` vertices."""
    total = sum(len(p[0]) for p in pieces)
    if total <= limit or len(pieces) <= 1:
        return [pieces]
    xs = numpy.array([p[5][0] for p in pieces])
    zs = numpy.array([p[5][1] for p in pieces])
    mx, mz = numpy.median(xs), numpy.median(zs)
    quads = defaultdict(list)
    for p in pieces:
        quads[(p[5][0] >= mx, p[5][1] >= mz)].append(p)
    if len(quads) <= 1:
        return [pieces]
    out = []
    for q in quads.values():
        out.extend(split_tile(q, limit))
    return out


def merge_local(pieces, ox, oy, level, shared):
    """Convert to BeamNG local (tile origin at (ox, oy)) and merge."""
    parts = []
    for v, n, uv, tris, runs, _origin, min_lod in pieces:
        local, nn = dae.glb_to_local(v, n)
        local[:, 0] -= ox
        local[:, 1] -= oy
        mark = "" if min_lod is None else f"{MIN_LOD_MARK}{int(min_lod)}"
        runs2 = [(mapto_name(shared, level, name) + mark, count) for name, count in runs]
        parts.append((local, nn, dae.glb_uvs(uv), None, tris, runs2))
    merged = dae.merge_meshes("tile", parts)
    return {
        "verts": merged.verts,
        "normals": merged.normals,
        "uvs": merged.uvs,
        "tris": merged.tris,
        "runs": merged.runs,
    }
