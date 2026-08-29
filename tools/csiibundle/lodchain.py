"""The game's own LOD chain: where each authored level takes over — `RequiredBatchesSystem`'s
per-level limits, ported, on top of the single-mesh arithmetic in `csiibundle.lod`.

A prefab that ships `LodProperties.m_LodMeshes` is drawn by the game at one of several
artist-made levels, chosen by the lod index the camera distance maps to; the bundle carries
the levels as `<node>_LOD<n>` nodes and this module says which the game would draw where.
"""

import math

import numpy

from .lod import (
    DEFAULT_LEVEL_OF_DETAIL,
    REFERENCE_SCREEN_HEIGHT,
    calculate_lod_limit,
    cull_distance,
    mesh_min_lod,
)


def rendering_size_indexed(size, index_count):
    """`RenderingUtils.GetRenderingSize(float3 size, float indexCount)` —
    `csum(size) * 0.57735 * rsqrt(indexCount)`: the overload the renderer sizes an LOD level
    with. A coarser level (fewer indices) measures LARGER, and is therefore drawn further, which
    is the whole mechanism of the game's chain."""
    s = numpy.asarray(size, dtype=numpy.float64).ravel()[:3]
    return float(s.sum()) * 0.57735026 / math.sqrt(max(float(index_count), 1.0))


def authored_lod_limits(group_size, levels, bias, min_lod=None):
    """`RequiredBatchesSystem.InitializeBatchGroup` for an object with an authored LOD chain:
    the `m_MinLod` of every level, LOD 0 first — the game's own switch points.

    `group_size` is LOD 0's bounding-box extents (the group's), `levels` the coarser levels
    richest first as `[(size, index_count), ...]`, `bias` the group's `MeshData.m_LodBias`
    (`lod_bias`, with the prefab's `LodProperties.m_Bias` folded in), and `min_lod` LOD 0's own
    limit as a single mesh (`mesh_min_lod`; computed from `group_size` when omitted).

    The rule, verbatim from the game: start at LOD 0's own limit clamped to `255 - k`; walk the
    chain COARSEST first, and give each level the limit in hand, then raise the limit to
    `clamp(CalculateLodLimit(GetRenderingSize(groupSize, indexCount), bias) + 1, x + 1, 255 - j)`
    — so a level's start is decided by the size of the level COARSER than it, and LOD 0 takes
    whatever is left. Level `j` is drawn while `limits[j] <= lod < limits[j - 1]`, and the lod
    index falls with distance (`calculate_lod`), so `cull_distance(limits[j - 1])` is where
    level `j - 1` hands over to level `j`, and `cull_distance(limits[-1])` is where drawing stops.
    Stacked objects (`StackDirection`) use a different overload; not ported, no bundle object is
    stacked at the LOD chain — declared.
    """
    k = len(levels)
    x = int(mesh_min_lod(group_size, 0.0, bias) if min_lod is None else min_lod)
    x = min(x, 255 - k)
    limits = [0] * (k + 1)
    for j in range(k, 0, -1):  # coarsest first, as the game walks it
        limits[j] = x
        _level_size, index_count = levels[j - 1]  # the default overload sizes by the GROUP's box
        raw = calculate_lod_limit(rendering_size_indexed(group_size, index_count), bias) + 1
        x = min(max(raw, x + 1), 255 - (j - 1))
    limits[0] = x
    return limits


def limit_px(
    radius, limit, level_of_detail=DEFAULT_LEVEL_OF_DETAIL, screen_height=REFERENCE_SCREEN_HEIGHT
):
    """The Torque detail size — projected pixels of `radius` — at the distance where a batch of
    lod limit `limit` stops being drawn (`cull_distance`). Fov-free like `game_detail_px`."""
    distance = cull_distance(limit, level_of_detail)
    tan_half = math.tan(math.radians(45.0 * 0.5))
    return float(radius) * (screen_height * 0.5) / (max(distance, 1e-6) * tan_half)
