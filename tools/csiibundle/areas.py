"""Surface areas (§8b) — driveways, parking-lot pavement, park paths.

An area is the painted ground of a meshless prefab, and the single most important thing about
one is that **its triangles are a FOOTPRINT, not a plane** (audit §30). The game renders each
triangle as a prism spanning `Triangle.m_HeightRange` — the min/max of the heightmap over that
triangle's own footprint, measured relative to its plane — and paints the material onto the
terrain inside it, as a decal (`RenderedArea.m_DecalLayerMask = DecalLayers.Terrain`). Nothing
grades the ground flat underneath. The one thing that does, `TerrainArea`/`CullAreasJob`,
belongs to LotPrefab and is already baked into §8's heightmap.

So a consumer that draws the triangle flat is drawing a plane the game never draws. Measured on
a full-city bundle, that put the ground through the pavement on 26.6% of 175,783 area triangles
and 61.4% of 27,422 areas (p95 11 cm, p99 55 cm). `conform_triangle` splits each one until it
follows the ground.

**Sample the ground the way you will draw it.** Bilinearly interpolating the heightmap cannot
substitute for asking the surface: the terrain is meshed as QUADS and the mesher picks their
diagonal, a choice worth a p95 of 9.4 cm at 3.5 m cells and 41 cm at the 14 m stride of a
full-city import. `conform_triangle` takes a `sample` callable so the caller supplies whatever
its own ground actually is; `csiibundle.drape` is one such surface.

**The surface is a full PBR material, not a diffuse texture.** `areaprefabs.json` carries
`normalMap`, `maskMap`, `metallic` and `smoothness` alongside `texture`/`color`, and before it
did (audit #40 P11) pavement and plazas "rendered flat-lit with guessed specular". The maskMap
packs the same channels as a §7 material — see `csiibundle.materials` — so **`roughness =
1 - smoothness`** here too. `surface_material()` normalises an entry into one shape.

**`rendererPriority` is the overlap draw order, and without it a consumer has no rule for which
of two overlapping areas paints on top** — they z-fight instead. It is always present.
`draw_order()` sorts by it.

**The bundle ships the prism band; use it to bound the search.** `heightRanges`, when present,
is the game's own `Triangle.m_HeightRange` per triangle in the same order as `triangles`: the
min and max of the terrain over that triangle's footprint, **relative to the triangle's own
plane**, in metres. It states how far a drape has to reach, so a consumer can size its ray or
skip a triangle whose ground is already flat. It is a GUIDE, not a substitute for the heightmap
— the conformance still has to sample — and it is omitted when every range on an area is zero.
On a full-city rip 91.4% of areas carry one. `height_range()` reads it.

**The tolerance is half the lift, and the ratio is the point** — the same rule the lane decals'
subdivision uses. How far the ground may rise through a piece cannot be chosen independently of
how far that piece is lifted clear of it. The lift itself is a rung of `decals.LIFT_LADDER`,
which is where every layer of ground paint is ordered against every other.
"""

import math

from . import decals, lod
from . import region as region_rule

# Lift above the terrain grid, below everything that stands on the area. The rung is
# `decals.LIFT_LADDER`'s, where every layer of ground paint is ordered against every other; the
# game draws these as terrain-layer decals, here they are thin geometry riding the ground.
AREA_LIFT = decals.LIFT["area"]

# Half the area's own lift. The RATIO is the constant, not either number — see the module
# docstring, and `csiibundle.decals` for the same rule on the layer above this one. TUNED.
CONFORM_TOLERANCE = AREA_LIFT * 0.5
# A big triangle at the full-city stride needs ~10 halvings to reach the floor, so the depth
# cap has to clear that or the cap silently becomes the stopping rule (it did: 810k pieces
# stopped with up to 4 m of ground above them). The runaway guard is the piece BUDGET below,
# not the depth — a budget bounds the work whatever the triangle's size, and reports itself.
MAX_DEPTH = 12
# Pieces one source triangle may spend, scaled by the ground it actually covers: a driveway
# triangle inside one terrain cell has nothing to resolve, a 25-hectare grass sheet over a
# gorge has plenty. A flat budget gets this backwards at both ends. The constant is pieces
# per terrain cell covered, so the whole layer costs ~this many pieces per cell of ground it
# paints, whatever the triangulation happens to look like.
PIECES_PER_CELL = 256
PIECE_BUDGET = (64, 65536)  # (floor, ceiling) on that scaling

# `RenderedArea.m_UVScale`'s own field default, used only when the bundle predates §8b
# carrying the per-prefab value. World position times this is the UV.
UV_SCALE_FALLBACK = 0.2


# `RenderedArea.m_UVScale`'s own field default, for a bundle predating the per-prefab value.
# World position times this is the UV, so 0.2 means one repeat every 5 m. CONTRACT.
UV_SCALE_DEFAULT = 0.2

# The area-surface keys the game always pushes, so their absence means an old bundle rather than
# an unauthored slot. CONTRACT.
ALWAYS_PRESENT = ("roundness", "rendererPriority", "expandAmount", "decalLayers")


def surface_material(entry):
    """One `areaprefabs.json` entry as a normalised PBR description.

    Returns a dict with `texture`, `color`, `normal_map`, `mask_map`, `metallic`, `roughness`,
    `uv_scale`, `priority`, `decal_layers`, `unresolved` and `name`. Absent optional keys come
    back as `None` rather than as invented defaults, because **an unauthored map on an override
    surface is a CLEARED slot in the game too** — filling it in renders something CS2 does not.

    `roughness` is `1 - smoothness`, matching §7. `uv_scale` falls back to `UV_SCALE_DEFAULT`,
    which is the game's own field default rather than a guess.

    `unresolved` marks a surface whose asset was not loaded in the rip session: the game itself
    drew those with its grey Missing Area fallback, so the honest thing is to surface them BY
    NAME rather than silently render neutral. The cure is enabling the pack and re-ripping.
    """
    entry = entry or {}
    smoothness = entry.get("smoothness")
    return {
        "name": entry.get("name"),
        "texture": entry.get("texture"),
        "color": tuple(entry["color"][:4]) if entry.get("color") else None,
        "normal_map": entry.get("normalMap"),
        "mask_map": entry.get("maskMap"),
        "metallic": float(entry["metallic"]) if entry.get("metallic") is not None else None,
        "roughness": (1.0 - float(smoothness)) if smoothness is not None else None,
        "uv_scale": float(entry.get("uvScale") or UV_SCALE_DEFAULT),
        "priority": int(entry.get("rendererPriority") or 0),
        "expand_amount": entry.get("expandAmount"),
        "decal_layers": tuple(entry.get("decalLayers") or ()),
        "unresolved": bool(entry.get("unresolved")),
    }


# How far apart two overlapping areas are separated, per step of draw order, when the surfaces
# are rebuilt as GEOMETRY rather than drawn as decals. The game has no such offset — it composites
# decals by `rendererPriority` in one pass — so this is the geometric stand-in for that rule.
#
# It must stay well inside the gap between the `area` rung of `decals.LIFT_LADDER` (0.020) and
# the `instance_decal` rung above it (0.030): with the reference city's 13 distinct priorities
# this spends 0.0065 of the available 0.010, so an area can never climb through the decal
# instances standing on it. TUNED.
PRIORITY_LIFT_STEP = 0.0005

# The whole priority spread may never reach the next rung. 21 or more distinct priorities would
# put the top-ranked surface at 0.030 — the `instance_decal` rung exactly — and paint a parking
# lot's pavement over its own bay markings, which is the regression the ladder comment records.
# The catalogue has 13, so this has never bitten; a modded city with more surfaces would find it.
# The step is what compresses, not the order, so the ranking survives the clamp. CONTRACT (the
# ceiling) / TUNED (the step).
PRIORITY_LIFT_CEILING = (decals.LIFT["instance_decal"] - AREA_LIFT) * 0.75


def priority_lift(priority, ordered):
    """Extra metres to raise a surface of this `rendererPriority`, given all priorities present.

    `ordered` is the ascending list of distinct priorities in the catalogue. The lift is by
    RANK, not by the raw value: the game's priorities are sparse and signed (−122 … 42 on the
    reference city) and scaling by them directly would push the top surface metres into the air.

    The total is capped at `PRIORITY_LIFT_CEILING` — see it for why an uncapped rank is unsafe.
    """
    ordered = list(ordered)
    try:
        rank = ordered.index(priority)
    except ValueError:
        rank = 0
    if rank == 0:
        return 0.0

    step = PRIORITY_LIFT_STEP
    top = max(len(ordered) - 1, 1)
    if top * step > PRIORITY_LIFT_CEILING:
        step = PRIORITY_LIFT_CEILING / top
    return rank * step


def priorities(catalogue):
    """The distinct `rendererPriority` values in a catalogue, ascending — `priority_lift`'s arg."""
    return sorted({int((v or {}).get("rendererPriority") or 0) for v in (catalogue or {}).values()})


def draw_order(catalogue):
    """Prefab ids sorted by `rendererPriority`, lowest first — paint in this order.

    The game's own rule for which of two overlapping areas wins. A consumer that draws them in
    catalogue or record order gets whichever the iteration happened to reach last, which is
    z-fighting with extra steps.
    """
    return sorted(
        catalogue or {}, key=lambda k: int((catalogue[k] or {}).get("rendererPriority") or 0)
    )


def height_range(record, index):
    """The `[min, max]` prism band for triangle `index` of an area, or `None`.

    Relative to that triangle's OWN plane, in metres — not absolute heights. `None` means the
    area shipped no `heightRanges` (its ground is flat) or the layer predates the field, and in
    both cases the answer is "assume nothing and sample".
    """
    ranges = record.get("heightRanges")
    if not ranges or not 0 <= index < len(ranges):
        return None
    band = ranges[index]
    return (float(band[0]), float(band[1])) if band and len(band) >= 2 else None


def conform_triangle(tri, cell, sample, counters):
    """Split one area triangle until every piece follows the ground, and emit the pieces.

    `tri` is three (x, y, z) Blender-frame corners already dropped onto the ground; `sample`
    is the memoised ground lookup; `cell` the terrain grid's own spacing. Returns the list of
    (a, b, c) conformed corner triples.

    The floor is deliberately far below the grid cell. A first cut stopped at a quarter cell,
    reasoning that a smaller piece cannot straddle a kink — wrong, and measurably so: the
    kinks are the quads' own diagonals (Blender's choice, anywhere inside a cell) and the
    15 cm roadbed sink boundary that every driveway crosses, so 14.6% of pieces stopped with
    up to 2.1 cm of ground still above them. A plane kink's error shrinks linearly with piece
    size, so the floor only has to be small; what it really guards is a true discontinuity
    (a cut-face rim, a gorge wall), where no amount of splitting converges.
    """
    floor = max(cell * 0.02, 0.10)
    (ax, ay, _), (bx, by, _), (cx, cy, _) = tri
    plan = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) * 0.5
    budget = min(PIECE_BUDGET[1], max(PIECE_BUDGET[0], int(PIECES_PER_CELL * plan / (cell * cell))))
    work = [(tri, 0)]
    out = []
    while work:
        (a, b, c), depth = work.pop()
        mids = []
        worst = 0.0
        for p, q in ((a, b), (b, c), (c, a)):
            mx, my = (p[0] + q[0]) * 0.5, (p[1] + q[1]) * 0.5
            flat = (p[2] + q[2]) * 0.5
            got = sample(mx, my)
            mids.append((mx, my, flat if got is None else got))
            if got is not None:
                worst = max(worst, got - flat)
        # The centroid too: three edge midpoints can all sit on the ground while a rise in
        # the middle of the piece — a knoll inside one cell, the peak of a quad's diagonal —
        # goes straight through it unseen.
        gx, gy = (a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0
        centre = sample(gx, gy)
        if centre is not None:
            worst = max(worst, centre - (a[2] + b[2] + c[2]) / 3.0)
        longest = max(math.dist(p[:2], q[:2]) for p, q in ((a, b), (b, c), (c, a)))
        spent = len(out) + len(work)
        if worst <= CONFORM_TOLERANCE or depth >= MAX_DEPTH or longest <= floor or spent >= budget:
            if worst > CONFORM_TOLERANCE:
                # Report the GROUND left unconformed, not the piece count: a piece is an
                # arbitrary unit (a budget stop leaves few big ones, a floor stop many small
                # ones) and only the square metres say how much of the city still shows it.
                counters["unconformed"] += 1
                counters["unconformed_area"] += (
                    abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
                )
                counters["worst"] = max(counters["worst"], worst)
            out.append((a, b, c))
            continue
        ab, bc, ca = mids
        work.append(((a, ab, ca), depth + 1))
        work.append(((ab, b, bc), depth + 1))
        work.append(((ca, bc, c), depth + 1))
        work.append(((ab, bc, ca), depth + 1))
    return out


_MISS = object()


def memoised_sampler(surface):
    """A ground lookup over a `csiibundle.drape.ProjectionSurface`, memoised on the EXACT point.

    Conformance re-samples the same ground constantly — every split shares its edge midpoints
    with its sibling, every child re-samples what its parent already asked, and adjacent area
    records share their boundary nodes — so the memo is what keeps the layer affordable when a
    query is expensive (a consumer draping against a triangle soup, `drape.TriangleSoupSurface`).
    Returns a callable `(x, y) -> height or None`, Z-up like the surface itself.

    **The key is the point itself, not the point rounded to a millimetre.** The rounding was
    there when one heightfield query cost 160 µs and near-duplicates were worth collapsing; with
    the scalar path (`drape.HeightfieldSurface._sample_one`, 3 µs) it bought nothing and cost two
    real things. It was an undeclared approximation — two points 0.4 mm apart shared an answer
    and whichever arrived first decided it for both, worth a measured mean of 0.014 mm and up to
    8 mm of height on the map's steepest cliff. And it made the layer's output depend on the
    ORDER of the records file, which is what stopped the conform from being split across worker
    processes: a chunk-local cache cannot answer like a global one. Keyed on the point, the memo
    is a pure function of its argument — identical to no cache at all, so any partition of the
    work gives the same bytes, and the ground is sampled where the geometry actually is.

    The sharing that mattered survives: a shared edge midpoint is `(p + q) * 0.5` computed from
    the same two floats by both neighbours, and a shared boundary node is the same number read
    twice out of the same file. Those are exact repeats, and exact repeats are what this catches.
    """
    cache = {}
    get = cache.get

    def ground_at(x, y):
        key = (x, y)
        hit = get(key, _MISS)
        if hit is _MISS:
            hit = cache[key] = surface.height(x, y) if surface is not None else None
        return hit

    return ground_at


def build_pieces(
    records, cell, sample, center=None, radius=None, lift=AREA_LIFT, progress=None, catalogue=None
):
    """Conform every area record into Z-up triangle soup, grouped by prefab.

    `records` is an iterable of §8b area records; `sample` is a `(x, y) -> height or None`
    ground lookup (see `memoised_sampler`); `cell` is the terrain grid's own spacing.
    `center`/`radius` filter by the area's centroid in bundle-frame metres.

    Returns `(pieces, stats)`. `pieces` is `{prefab: {"verts", "faces", "minLod"}}` with
    vertices already in the Z-up frame and lifted by `lift` and `minLod` running parallel to
    `faces` (the game's own per-triangle draw-distance limit, inherited by every sub-triangle the
    conform produces); `stats` carries the counts a caller reports, plus `counters` from the
    conformance itself.

    **Two frame details, and both matter.** Bundle `(x, y, z)` becomes Z-up `(x, z, y)`, which
    flips handedness (§2.2), so every triangle's winding is REVERSED to keep it facing up — an
    area drawn face-down is invisible or black depending on the renderer. And a node whose ground
    lookup misses keeps its authored height rather than being dropped, so an area at the edge of
    an imported region degrades to flat instead of to nothing.
    """
    # Overlapping surfaces are separated by draw order, so the game's `rendererPriority` rule
    # survives being rebuilt as geometry instead of composited as decals. See `priority_lift`.
    ordered = priorities(catalogue) if catalogue else []
    pieces = {}
    stats = {
        "placed": 0,
        "skipped_region": 0,
        "dropped_bad": 0,
        "missed": 0,
        "source_tris": 0,
        "painted_area": 0.0,
        "counters": {"unconformed": 0, "unconformed_area": 0.0, "worst": 0.0},
    }
    counters = stats["counters"]

    for record in records:
        nodes = record.get("nodes") or []
        triangles = record.get("triangles") or []
        if len(nodes) < 3 or not triangles:
            continue

        if radius is not None and center is not None:
            cx = sum(n[0] for n in nodes) / len(nodes)
            cz = sum(n[2] for n in nodes) / len(nodes)
            box = {"centerX": center[0], "centerZ": center[1], "radius": radius}
            if not region_rule.contains(cx, cz, box):
                stats["skipped_region"] += 1
                continue

        prefab = record.get("prefab")
        sink = pieces.setdefault(prefab, {"verts": [], "faces": [], "minLod": []})
        # The game culls an area PER TRIANGLE, and by how WIDE the paint is there rather than by
        # how big the triangle is (`lod.area_triangle_min_lods`, ported from
        # `Game.Areas.GeometrySystem`). The limit is computed on the record's own polygon before
        # the conform subdivides it, and every conformed sub-triangle inherits its parent's — the
        # subdivision is a fidelity step for the terrain, not a change to what the game draws.
        tri_min_lod = lod.area_triangle_min_lods([(n[0], n[2]) for n in nodes], triangles)
        piece_lift = lift + (
            priority_lift(
                int(((catalogue or {}).get(prefab) or {}).get("rendererPriority") or 0), ordered
            )
            if ordered
            else 0.0
        )
        corners = []
        for n in nodes:
            hit = sample(n[0], n[2])
            if hit is None:
                stats["missed"] += 1
            corners.append((n[0], n[2], (n[1] if hit is None else hit)))
        for tri_index, tri in enumerate(triangles):
            if max(tri) >= len(nodes) or min(tri) < 0:
                stats["dropped_bad"] += 1
                continue
            stats["source_tris"] += 1
            a, b, c = (corners[i] for i in reversed(tri))
            stats["painted_area"] += (
                abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
            )
            for piece in conform_triangle((a, b, c), cell, sample, counters):
                base = len(sink["verts"])
                sink["verts"].extend((p[0], p[1], p[2] + piece_lift) for p in piece)
                sink["faces"].append((base, base + 1, base + 2))
                sink["minLod"].append(
                    tri_min_lod[tri_index] if tri_index < len(tri_min_lod) else 255
                )
        stats["placed"] += 1
        if progress is not None:
            progress(stats["placed"])

    return pieces, stats
