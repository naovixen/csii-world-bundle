"""CS2's level-of-detail arithmetic, ported from the game — `Game.Rendering.RenderingUtils`,
`Game.Prefabs.MeshSystem` and `Game.Prefabs.ObjectInitializeSystem`.

The game decides how far an object is drawn from **the mesh's own bounds and triangle area**,
and nothing else: no authored distance, no per-asset tuning table. Every input is in the bundle
already (the `.glb`'s vertices), which is why this is a port rather than the producer change
audit §57.1 filed — see §62.

The chain, exactly as the game runs it:

1. `size = MathUtils.Size(renderPrefab.bounds)` — the mesh's bounding-box extents
   (`ObjectInitializeSystem`, the `float3 @float` at the top of the sub-mesh loop).
2. `rendering_size(size)` — each axis capped against the other two, then the mean.
3. `lod_bias(size, surface_area)` — `MeshSystem` subtracts `log2(sqrt(area / boxArea))` from the
   bias when the prefab knows its triangle area, so a **sparse** mesh (a fence, a tree canopy)
   is dropped sooner than its box suggests. `surface_area` is
   `Colossal.AssetPipeline.Geometry.CalcSurfaceArea` — the plain sum of triangle areas.
4. `min_lod(rendering_size, bias)` = `RenderingUtils.CalculateLodLimit`.
5. Visible while `CalculateLod(d^2, p) >= minLod`, which `cull_distance` inverts.

**`asint(x) >> 23` is the IEEE-754 exponent**, so both `CalculateLodLimit` and `CalculateLod`
reduce to `128 - floor(6 * log2(x))`. `calculate_lod_limit` still does it the game's way, bit
for bit, because the floor lands on exact powers of two and a `log2` would round the other way.

## What the number means on a screen

Substituting the game's own constants (`lodFactor = levelOfDetail * 540 / tan(fov/2)`), the
projected height in pixels of the rendering size at the cull distance is

    px = S * (H/2) / (d * tan(fov/2))  with  d = S * lodFactor
       = H / (1080 * levelOfDetail)

— **the field of view cancels exactly**. So CS2's rule is "draw it until its rendering size is
`H / (1080 * levelOfDetail)` pixels tall", i.e. **1.43 px at 1080p on the High preset**
(`levelOfDetail` 0.7; 0.5 Medium, 0.35 Low, 0.25 Very Low, slider 0.10-1.00). That is directly
comparable to a Torque/BeamNG detail size, which is the projected size in pixels of the shape's
bounding-sphere **radius** — hence `game_detail_px`, which converts between the two measures.
"""

import math

import numpy

# `RenderingSystem.levelOfDetail` per quality preset — `Game.Settings.LevelOfDetailQualitySettings`.
# The slider runs 0.10 to 1.00; the game's own High profile is 0.7. CONTRACT (the game's).
LEVEL_OF_DETAIL = {"veryLow": 0.25, "low": 0.35, "medium": 0.5, "high": 0.7, "max": 1.0}
DEFAULT_LEVEL_OF_DETAIL = LEVEL_OF_DETAIL["high"]

# The screen height the pixel figures are quoted at. Both engines' thresholds scale with the
# real viewport, so this is only the reference a *number* is written down against, and 1080 is
# what BeamNG's own levels are authored for (`nulldetail10` throughout the vanilla shapes).
REFERENCE_SCREEN_HEIGHT = 1080.0


def _as_int32(value):
    """`math.asint` — the float's bits as a signed 32-bit int, the game's own reinterpretation."""
    return int(numpy.float32(value).view(numpy.int32))


def calculate_lod_limit(meters_per_pixel, bias=0.0):
    """`RenderingUtils.CalculateLodLimit` — a metres-per-pixel figure as a LOD index (0..255).

    Bit for bit: `num = m*m`, then `(255 - (asint(num*num*num) >> 23)) & 0xFF`. The bias divides
    the input by `2^bias`, so a POSITIVE bias means "drop it sooner".
    """
    m = float(meters_per_pixel) * (2.0 ** -float(bias))
    num = numpy.float32(m) * numpy.float32(m)
    cube = numpy.float32(num) * numpy.float32(num) * numpy.float32(num)
    return (255 - (_as_int32(cube) >> 23)) & 0xFF


def calculate_lod(distance, factor):
    """`RenderingUtils.CalculateLod(distanceSq, lodParameters)` — the LOD index at a distance.

    `factor` is `lodParameters.x`; the game passes `y = 1/lodFactor^2` and squares inside, which
    is the same number.
    """
    d = float(distance) / max(float(factor), 1e-9)
    sq = numpy.float32(d) * numpy.float32(d)
    cube = numpy.float32(sq) * numpy.float32(sq) * numpy.float32(sq)
    return (255 - (_as_int32(cube) >> 23)) & 0xFF


def distance_factor(min_lod):
    """`RenderingUtils.CalculateDistanceFactor` — `2^((128 - lod) / 6)`."""
    return 2.0 ** ((128.0 - float(min_lod)) * (1.0 / 6.0))


def lod_factor(level_of_detail=DEFAULT_LEVEL_OF_DETAIL, fov_degrees=45.0):
    """`lodParameters.x` — `levelOfDetail * 540 / tan(fov/2)` (`CalculateLodParameters`).

    Only ever used together with a distance, and every *pixel* answer here cancels the fov out
    again, so the default is a placeholder for callers that want metres.
    """
    return float(level_of_detail) * 540.0 / math.tan(math.radians(float(fov_degrees) * 0.5))


def cull_distance(min_lod, level_of_detail=DEFAULT_LEVEL_OF_DETAIL, fov_degrees=45.0):
    """Metres at which the game stops drawing a mesh whose limit is `min_lod`.

    The game's own `RenderingUtils.CalculateDistance(lod, lodParameters)`; it is the inverse of
    `calculate_lod`, so `calculate_lod(cull_distance(L), lod_factor()) == L`. The index is
    quantised to 1/6 of an octave, so this is where the LAST visible band STARTS: the mesh is
    still drawn out to `2^(1/6)` (12.25 %) further, and disappears there.
    """
    return distance_factor(min_lod) * lod_factor(level_of_detail, fov_degrees)


def rendering_size(size):
    """`RenderingUtils.GetRenderingSize(float3)` with `ObjectInitializeSystem`'s clamp.

    `size` is the mesh's bounding-box extents. Each axis is first capped at
    `max(8 * min(other two), 4 * max(other two))` — which is what stops a long thin thing from
    claiming the draw distance of a large one — and the answer is the mean of the three.

    NOT applied here: the two special cases that gate on `surfaceArea <= 0` (a human object's
    x/3, a `Standing` object's leg lerp). Both need prefab flags the bundle does not carry, both
    only shrink the size, and neither has a counterpart in a `.glb`; they are counted as
    unavailable rather than guessed (audit §62).
    """
    s = numpy.asarray(size, dtype=numpy.float64).ravel()[:3]
    other1 = numpy.roll(s, -1)                      # size.yzx
    other2 = numpy.roll(s, -2)                      # size.zxy
    cap = numpy.maximum(numpy.minimum(other1, other2) * 8.0, numpy.maximum(other1, other2) * 4.0)
    s = numpy.minimum(s, cap)
    return float(s.sum() / 3.0)


def rendering_size_raw(size):
    """`RenderingUtils.GetRenderingSize(float3)` **verbatim** — `csum(size) / 3`, no clamp.

    The clamp `rendering_size` applies belongs to `ObjectInitializeSystem`, not to the helper,
    and the network / area / lane paths call the helper directly. Mixing them up is a silent
    factor on every road's draw distance, so the two are separate functions.
    """
    s = numpy.asarray(size, dtype=numpy.float64).ravel()[:3]
    return float(s.sum() / 3.0)


def rendering_size_2d(size):
    """`RenderingUtils.GetRenderingSize(float2 size)` — the overload every FLAT thing uses.

    `GetRenderingSize(float3(size, cmax(size * float2(8, 4))))`: the third component is a
    synthetic "depth" of eight times the width or four times the height, whichever is larger, so
    a 16 m road with a 1 m height range gets `(16 + 1 + 128)/3 = 48.3` m per pixel rather than
    the 5.7 its two real extents would give. That is the game saying a long flat thing is
    visible far past its cross-section.
    """
    s = numpy.asarray(size, dtype=numpy.float64).ravel()[:2]
    return rendering_size_raw((s[0], s[1], max(s[0] * 8.0, s[1] * 4.0)))


def composition_min_lod(width, height_range, bias=0.0):
    """`NetCompositionHelpers.CalculateMinLod` — a road/rail composition's `m_MinLod`.

    `CalculateLodLimit(GetRenderingSize(float2(m_Width, Size(m_HeightRange))), bias)`, where the
    bias is the **mean** of `MeshData.m_LodBias` over the composition's pieces. An EDGE is then
    culled on `min(startNode, edge, endNode)` of those three compositions (`PreCullingSystem`),
    which is the most generous of the three — the same "draw it while any part of it would be
    drawn" rule a merged tile needs.
    """
    return calculate_lod_limit(rendering_size_2d((width, height_range)), bias)


def lane_min_lod(meshes):
    """`NetLaneGeometryData.m_MinLod` — a lane prefab's (marking, fence, cable) own threshold.

    `NetInitializeSystem`: over every `NetLaneMeshInfo` of the prefab,
    `min(CalculateLodLimit(GetRenderingSize(Size(mesh.bounds).xy), meshData.m_LodBias))`. Note
    **`.xy`**: the mesh's width and height, never its length — a lane mesh is authored as one
    tile that the game repeats along the curve, so its length says nothing about the lane.

    `meshes` is `[(size3, area), ...]` in the meshes' own units; 255 (never drawn) for none.
    """
    best = 255
    for size, area in meshes:
        s = numpy.asarray(size, dtype=numpy.float64).ravel()[:3]
        best = min(best, calculate_lod_limit(rendering_size_2d(s[:2]), lod_bias(s, area)))
    return best


# `AreaUtils.GetMinNodeDistance(AreaType)` — the game's per-type constant, in metres. The painted
# surfaces this bundle ships (§8b) are `Surface`. CONTRACT (the game's).
AREA_NODE_DISTANCE = {"Lot": 8.0, "District": 32.0, "MapTile": 64.0, "Space": 1.0, "Surface": 0.75}
DEFAULT_AREA_NODE_DISTANCE = 1.0


def area_triangle_min_lod(nearest_two, node_distance=AREA_NODE_DISTANCE["Surface"], bias=0.0):
    """`Game.Areas.GeometrySystem:562` — one area triangle's `m_MinLod`.

    `CalculateLodLimit(GetRenderingSize(float3(4*d1, nodeDistance, 4*d2)), m_LodBias)`, where
    `d1 <= d2` are the two smallest distances from the triangle's chosen centre point to the
    area's own boundary edges. So an area is drawn as far as it is WIDE, not as far as it is
    long: a 2 m footpath vanishes at a 2 m thing's distance however far it runs. `AreaBatchSystem`
    then keeps the triangle while `CalculateLod(minDistance^2) >= m_MinLod`, per triangle.

    `nearest_two` is `(d1, d2)` in metres.
    """
    d1, d2 = (float(nearest_two[0]), float(nearest_two[1]))
    return calculate_lod_limit(rendering_size_raw((4.0 * d1, float(node_distance), 4.0 * d2)), bias)


def area_triangle_min_lods(nodes_xz, triangles, node_distance=AREA_NODE_DISTANCE["Surface"], bias=0.0):
    """Every triangle's `m_MinLod` for one area — `Game.Areas.GeometrySystem.CalculateGeometry`.

    The game measures how WIDE the paint is at each triangle, not how big the triangle is: it
    picks a centre point inside the triangle, finds the two smallest distances from it to the
    **area's own boundary polygon**, and feeds `4 * those` into the LOD limit. So a 2 m footpath
    disappears at a 2 m thing's distance however long it runs, while a car park stays.

    The centre is chosen from up to four candidates and the winner is the one furthest from the
    boundary. Which candidates exist depends on which of the triangle's edges are polygon edges
    (`|index difference| == 1 or N-1`): the midpoint of every DIAGONAL edge always, plus one
    weighted point when exactly one edge is a polygon edge, and the plain centroid otherwise.
    Ported verbatim, candidate order included, because the tie-break is "largest nearest
    distance wins" and a different candidate set moves the answer.

    `nodes_xz` is the polygon (N, 2) in order; `triangles` is (M, 3) node indices. Returns a list
    of M ints.
    """
    nodes = numpy.asarray(nodes_xz, dtype=numpy.float64).reshape(-1, 2)
    tris = numpy.asarray(triangles, dtype=numpy.int64).reshape(-1, 3)
    n = len(nodes)
    if n < 3 or len(tris) == 0:
        return [255] * len(tris)
    seg_a = nodes[numpy.arange(-1, n - 1)]                 # every boundary segment, wrapping
    seg_b = nodes
    out = []
    for tri in tris:
        a, b, c = nodes[tri[0]], nodes[tri[1]], nodes[tri[2]]
        diff = numpy.abs(numpy.array([tri[2] - tri[1], tri[0] - tri[2], tri[1] - tri[0]]))
        on_edge = (diff == 1) | (diff == n - 1)
        diagonal = ~on_edge
        candidates = []
        if diagonal[0]:
            candidates.append((b + c) * 0.5)
        if diagonal[1]:
            candidates.append((c + a) * 0.5)
        if diagonal[2]:
            candidates.append((a + b) * 0.5)
        if diagonal[0] and diagonal[1] and on_edge[2]:
            candidates.append(c * 0.5 + (a + b) * 0.25)
        elif diagonal[1] and diagonal[2] and on_edge[0]:
            candidates.append(a * 0.5 + (b + c) * 0.25)
        elif diagonal[2] and diagonal[0] and on_edge[1]:
            candidates.append(b * 0.5 + (c + a) * 0.25)
        else:
            candidates.append((a + b + c) / 3.0)
        best = (-1.0, -1.0)
        for point in candidates:
            d2 = _segment_distances_sq(seg_a, seg_b, point)
            if len(d2) < 2:
                continue
            two = numpy.partition(d2, 1)[:2]
            pair = (float(min(two)), float(max(two)))
            if pair[0] > best[0]:
                best = pair
        if best[0] < 0.0:
            out.append(255)
            continue
        d = numpy.sqrt(numpy.asarray(best)) * 4.0
        out.append(calculate_lod_limit(rendering_size_raw((d[0], float(node_distance), d[1])), bias))
    return out


def _segment_distances_sq(seg_a, seg_b, point):
    """Squared distance from `point` to every segment `a->b` — `MathUtils.DistanceSquared`."""
    ab = seg_b - seg_a
    ap = point[None, :] - seg_a
    denom = (ab * ab).sum(axis=1)
    t = numpy.where(denom > 1e-12, (ap * ab).sum(axis=1) / numpy.maximum(denom, 1e-12), 0.0)
    t = numpy.clip(t, 0.0, 1.0)
    closest = seg_a + ab * t[:, None]
    delta = point[None, :] - closest
    return (delta * delta).sum(axis=1)


def group_detail_px(radius, cull_metres, screen_height=REFERENCE_SCREEN_HEIGHT, fov_degrees=45.0):
    """A merged group's Torque detail size for a cull distance somebody else computed.

    `game_detail_px` answers this for one mesh from its own bounds; a TILE is many primitives
    merged, its Torque threshold is measured against the TILE's bounding-sphere radius, and the
    distance it has to disappear at comes from the primitives inside it. This converts between
    the two. The fov cancels the same way it does in `game_detail_px` as long as both sides use
    one value, which they do.
    """
    return screen_px(radius, cull_metres, screen_height, fov_degrees)


def surface_area(verts, tris):
    """`Geometry.CalcSurfaceArea` — the sum of triangle areas, in the mesh's own units."""
    v = numpy.asarray(verts, dtype=numpy.float64)
    t = numpy.asarray(tris, dtype=numpy.int64).reshape(-1, 3)
    if len(t) == 0:
        return 0.0
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return float(0.5 * numpy.linalg.norm(numpy.cross(b - a, c - a), axis=1).sum())


def lod_bias(size, area, authored_bias=0.0):
    """`MeshSystem`'s `m_LodBias` for a mesh whose box is `size` and whose triangles total `area`.

    `bias -= log2(sqrt(clamp(area / boxArea, 1e-6, 1)))`, where `boxArea` is the bounding box's
    own surface area. The term is never negative, so the bias only ever pulls the draw distance
    IN — a mesh that fills its box keeps the box's distance, a sparse one loses `2^bias` of it.

    `authored_bias` is `LodProperties.m_Bias`, which lives on a prefab that ships an authored LOD
    chain. The producer ships `lodCap 0` (no LOD meshes at all), so nothing in the bundle can
    carry it; it is an argument here so a future producer field has somewhere to land.
    """
    s = numpy.asarray(size, dtype=numpy.float64).ravel()[:3]
    box = 2.0 * float((s * numpy.roll(s, -1)).sum())
    if box <= 0.0 or area <= 0.0:
        return float(authored_bias)
    ratio = min(max(float(area) / box, 1e-6), 1.0)
    return float(authored_bias) - math.log2(math.sqrt(ratio))


def mesh_min_lod(size, area, authored_bias=0.0):
    """The whole chain for one mesh: bounds + triangle area -> the game's `MeshData.m_MinLod`."""
    return calculate_lod_limit(rendering_size(size), lod_bias(size, area, authored_bias))


def screen_px(world_length, distance, screen_height=REFERENCE_SCREEN_HEIGHT, fov_degrees=45.0):
    """The projected height in pixels of `world_length` at `distance`. Torque's
    `SceneRenderState::projectRadius` and Unity's projection are the same expression."""
    tan_half = math.tan(math.radians(float(fov_degrees) * 0.5))
    return float(world_length) * (screen_height * 0.5) / (max(float(distance), 1e-6) * tan_half)


def game_detail_px(radius, size, area, authored_bias=0.0,
                   level_of_detail=DEFAULT_LEVEL_OF_DETAIL, screen_height=REFERENCE_SCREEN_HEIGHT):
    """CS2's cull threshold for this mesh **in BeamNG's units** — the projected pixel size of the
    shape's bounding-sphere `radius` at the distance CS2 stops drawing it.

    Fov-free: the game's cull distance is `S_eff * levelOfDetail * 540 / tan(fov/2)` and a
    projection divides by the same `tan(fov/2)`, so the two cancel and what is left is

        px = (radius / S_eff) * screen_height / (1080 * levelOfDetail)

    with `S_eff = rendering_size(size) * 2^-lod_bias`. Feed it to a Torque `nulldetail<px>` node
    and the shape disappears where CS2 would have stopped drawing it.
    """
    effective = rendering_size(size) * (2.0 ** -lod_bias(size, area, authored_bias))
    if effective <= 0.0:
        return float("inf")
    return (float(radius) / effective) * float(screen_height) / (1080.0 * float(level_of_detail))
