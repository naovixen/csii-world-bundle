"""Stage `shapes` — every mesh state the city actually places, as a BeamNG `.dae`.

One file per `(assetId, mesh state, paint variant)`: the LOD-0 mesh from `meshes/`, the bake's
generated LOD1/LOD2 from `derived/lods/`, a collider per the collision policy, and the detail
thresholds **CS2 itself uses** (`csiibundle.lod`, audit §62).

## The detail thresholds are the game's, scaled once

CS2 draws an object until its *rendering size* covers `H / (1080 * levelOfDetail)` pixels — 1.43
px at 1080p on the game's High preset — and that is per-prefab, because the rendering size comes
from the mesh's own bounds and its bias from its own triangle area. `lod.game_detail_px` returns
that threshold in Torque's units (the projected pixel size of the bounding-sphere radius), so a
`nulldetail<px>` node reproduces the game's cull distance exactly.

Reproducing it *exactly* is not what a build wants: on the reference city the game's answer is a
median **1,024 m** for a prop and **7,301 m** for a building, over 1.2 million objects. So the
one and only deviation is a single global multiplier, `lod_detail_scale` (default 6.0 = "cull
six times closer than CS2"), and 1.0 restores the game. Every shape keeps the game's *relative*
answer — the bounds clamp and the surface-area bias that make a sparse fence disappear before a
solid block of the same size. The report states the multiplier, the median draw distance per
kind at it, and the game's own median beside it.

## Where the generated levels take over is derived from what they COST

The rest of the ladder — where LOD1 and LOD2 take over, and where an imposter does — has no game
counterpart at all: CS2 ships no LOD chain for these prefabs (`lodCap 0`), so the bake generates
one and the switch points are ours. They used to be geometric multiples of the shape's own null
threshold, and a null threshold is a statement about the shape's SIZE — while a generated level's
error has nothing to do with its size. Measured over the reference bundle, a LOD1's displacement
from LOD 0 runs from **0 m on 603 shapes to 37 m on the worst**, p50 0.018 m and p99 6.167 m; no
single multiple of a size can serve both ends of that, and the coarse half of it is what reads as
a roof moving under props that have not moved at all (A6.7).

So each level's threshold is the ladder's — **or further out**, when its own measured
displacement says it has no business being drawn that near
(`bake/lods` → `lod.displacement_switch_px`, budgeted by `lod_switch_error_px`).

**One direction only.** The first cut of this let the rule pull a level NEARER too, on the
reasoning that a level a centimetre from LOD 0 has earned it; that shipped 2,586 of 2,903 prop
shapes and 615 of 1,057 building shapes onto decimated geometry closer than the ladder had ever
put them, and the city came back "cranes rotated and floating, highrises all diagonal". Surface
distance is not visual error for thin or small geometry — a tenth of a metre across a crane boom
is geometrically nothing and visually the whole object — and the ratio ladder was protecting
exactly that by keying off the shape's SIZE. So the ladder stays as the ceiling and the
measurement is only allowed to say "not yet".
"""

import json
import math
import multiprocessing
import os
import time

import numpy
from csiibundle import collision, glb, lod, tints
from csiibundle import instances as bundle_instances
from csiibundle.bake import lods as bake_lods

from ..beamng import dae, matbind, placement
from . import Stage, level_of

STAGE_KEY = "shapes"
SHAPES_SUBDIR = "cs2"

# The ladder, as multiples of the shape's null threshold, coarsest first — vanilla BeamNG's
# spacing read in A2. It is no longer what DECIDES a switch point (the measured displacement is,
# below) but it is still the CEILING on one, and the last rung is still the floor.
#
# **The displacement rule may only ever DEFER a level, never advance it**, and that is the whole
# lesson of the 13:24 build. Allowing it both ways looked principled — a level whose surface is
# a centimetre from LOD 0's has "earned" a nearer place — and it put 2,586 of 2,903 prop shapes
# (335,072 placements) and 615 of 1,057 building shapes onto their decimated level NEARER than
# before, a prop by up to 509 m and a building by 1,821 m. Naomi's load came back "even more
# buildings rotations are off, warehouses rotated into the ocean, cranes rotated and floating,
# highrises all diagonal": a vertex-clustered LOD of a crane lattice or a balcony run is a
# handful of skewed triangles, and SURFACE DISTANCE DOES NOT SEE THAT — a 0.1 m error on a 0.2 m
# boom is geometrically tiny and visually total. The ratio ladder was implicitly protecting thin
# and small geometry by tying the switch to the shape's SIZE, and only the deferring half of the
# new rule is an improvement on it.
LADDER_RATIOS = (1.5, 6.0, 24.0)

# How many pixels of surface error a generated level is allowed to show at the moment it takes
# over (`lod.displacement_switch_px`). DECLARED TUNING and the one number in the ladder that is a
# BUDGET rather than a measurement: fidelity would ask for about a pixel, and a pixel is not
# affordable, because a CS2 residential block is 3,295 triangles of balconies, dormers and window
# reveals and taking three quarters of them away moves the surface metres whatever the decimator
# (A6.7 measured the quadric backend against the clusterer and got the same error).
#
# MEASURED over 1.16 M placements, from the two densest FOREST neighbourhoods and the two densest
# BUILDING ones, against the ladder it defers from (vegetation exempt, see `_lod_levels`):
#
#   4 px  +1 %, +0 % forest   +51 %, +67 % downtown   1,568 of 4,229 shapes deferred
#   6 px  +0 %, +0 %          +30 %, +39 %            1,191
#   8 px  +0 %, +0 %          +16 %, +21 %            1,004
#
# Holding a building at LOD 0 further out is bought in drawn triangles and nothing else, and it
# is bought downtown, where the card is already the constraint (A6.0 is open and the level mounts
# 4,675 MiB). 8 px defers every gross case — the worst offenders go 16x further out either way —
# and costs half of what 6 does, so it is what ships by default. `settings` exposes it, the
# presets run 4 to 20, and the report states the pixel error every level actually ships with.
DEFAULT_SWITCH_ERROR_PX = 8.0

# An imposter takes over where the mesh would have been culled and runs for this many times more
# distance before nothing is drawn. OURS, and the reason it is worth having: it buys back part
# of CS2's draw distance at billboard cost.
IMPOSTER_REACH = 3.0

# ...but only for a shape that is still being drawn out there. **BeamNG builds an imposter atlas
# at RUNTIME**, rendering the shape from every angle the first time it comes into view, and keeps
# it: `beamng.log` on the A5 load shows `Creating imposters for:` 2,652 times between 64 s and
# 903 s, on things like `NA_MailboxWall01`, `NA_ChimneyResidentialSmall01` and `ClothesDryer01`.
# That is the stall when you pan and the memory that climbs as you explore. A4/A5 gave one to
# EVERY object shape — 4,245 of 4,245 — against the shipped levels' 3.1 % (west_coast_usa, 82 of
# 2,608), 7.2 % (italy, 102) and 4.4 % (johnson_valley, 22).
#
# (Counting these needs the WHOLE file: a COLLADA scene is the last section, so a grep over the
# first 200 KB reports only the small shapes and undercounts. That mistake read 4,245 as 2,747.)
#
# The gate is the mechanism's own question: a billboard replaces the MESH beyond the mesh's null
# distance, so it is worth an atlas exactly when the mesh is still drawn near the edge of the view
# distance. Three quarters of `view_distance` puts 153 of our 4,245 object shapes over the line —
# 3.6 %, inside the vanilla band — and it scales with the setting instead of being a constant that
# means something different at 2 km and at 8 km. OURS: CS2 has no imposter concept at all.
IMPOSTER_VIEW_FRACTION = 0.75


def enabled(settings):
    return any(settings.get(k, True) for k in ("trees", "props", "buildings"))


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("instances") or 0) or None


# -- the demand scan ------------------------------------------------------------------------------


def scan_demand(ctx, progress):
    """One pass over `instances.jsonl`: which `(assetId, state)` the build places, how many of
    each, which paint variants are wanted, and whether the placement is forest or TSStatic.

    Returns `(demand, stats, dropped)` where `demand` is
    `{(asset_id, suffix): {"kind", "count", "forest", "variants": {index: count}}}` and `dropped`
    is the union of the record ids the road-clearance deviation removed and those orphaned by an
    absent owner — both are per-RECORD decisions the later passes must reach identically, and one
    set is enough because `admit` tests membership either way.
    """
    settings = ctx.settings
    region = ctx.region
    mesh_dir = os.path.join(ctx.bundle, "meshes")
    have = {}  # asset+suffix -> bool, cached file probes
    variants = _paint_variants(ctx)
    forest_allowed = True
    demand = {}
    stats = {
        "records": 0,
        "kept": 0,
        "noMesh": 0,
        "skippedBaked": 0,
        "skippedState": 0,
        "skippedKind": 0,
        "skippedDensity": 0,
        "skippedRegion": 0,
        "skippedRoadclear": 0,
        "skippedOrphan": 0,
    }  # `placement.admit`'s reasons
    # A6.6 — records whose owner the producer declared ABSENT (§10). Computed once here beside
    # the road clearance and handed to the other passes through `shared`.
    orphans, orphan_stats = bundle_instances.orphans_of_absent_owners(ctx.bundle)
    stats["orphans"] = orphan_stats
    # The road-clearance deviation, computed ONCE here and handed to the instances stage through
    # `shared` — one spatial pass over the lane layer instead of two, and the two passes cannot
    # reach different answers because they consult the same set.
    clipped, clip_stats = placement.road_clipped(ctx.bundle, settings, region)
    stats["roadclear"] = clip_stats
    progress.begin(
        "shapes: which meshes the city places", total=units(ctx.census, settings), unit="instances"
    )
    for record in bundle_instances.read_instances(ctx.bundle):
        stats["records"] += 1
        if stats["records"] % 25000 == 0:
            progress.advance(25000)
            progress.check()
        # ONE admission test for both passes: `placement.admit` names the reason so this stage
        # can break the count down without restating the rule (they drifted apart the moment
        # they were two functions)
        reason = placement.admit(record, settings, region, clipped, orphans)
        if reason is not None:
            stats["skipped" + reason.capitalize()] += 1
            continue
        kind = record.get("kind")
        asset = record["assetId"]
        suffix = _state_suffix(record, asset, mesh_dir, have)
        if suffix is None:
            stats["noMesh"] += 1
            continue
        forest = placement.wants_forest(record, forest_allowed)
        entry = demand.get((asset, suffix))
        if entry is None:
            entry = demand[(asset, suffix)] = {
                "kind": kind,
                "count": 0,
                "forest": False,
                "static": False,
                "variants": {},
                "tree": False,
            }
        # `kind == "tree"` is ALL vegetation; only `is_tree` is the game's own tree (see
        # `csiibundle.instances.is_tree`). Measured per prefab on the reference city, no asset
        # is ever classified both ways, so one flag per demand entry is exact rather than a
        # majority vote.
        if kind == "tree" and bundle_instances.is_tree(record):
            entry["tree"] = True
        entry["count"] += 1
        entry["forest"] = entry["forest"] or forest
        entry["static"] = entry["static"] or not forest
        if forest:
            centres = (variants.get(asset) or {}).get("variants") or []
            if centres:
                index = placement.variant_of(record, centres)
                entry["variants"][index] = entry["variants"].get(index, 0) + 1
        stats["kept"] += 1
    progress.end(
        f"{stats['kept']:,} of {stats['records']:,} records over {len(demand):,} mesh states"
    )
    return demand, stats, clipped | orphans


def _state_suffix(record, asset, mesh_dir, have):
    """The STATE SUFFIX of the first `mesh_candidates` entry on disk, or None if none is.

    The walk is `csiibundle.instances.first_mesh_state`; the slice is here because `asset` is
    what is being sliced off and it is in scope here, not there.
    """
    candidate = bundle_instances.first_mesh_state(record, mesh_dir, have)
    return None if candidate is None else candidate[len(asset) :]


def _paint_variants(ctx):
    path = os.path.join(ctx.derived_dir, "paint_variants.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("prefabs") or {}
    except (OSError, ValueError):
        return {}


# -- writing one shape ---------------------------------------------------------------------------


def _shape_job(args):
    """Worker: one `(asset, suffix, variant)` -> a written `.dae` plus the numbers the report and
    the instances stage need. Returns `(key, result_dict)`; `result_dict["error"]` on failure."""
    (
        bundle,
        derived_dir,
        out_dir,
        level_name,
        asset,
        suffix,
        variant,
        variant_key,
        kind,
        options,
        aliases,
        tinted,
        is_tree,
        displacement,
    ) = args
    source = asset + suffix
    try:
        nodes = glb.parse_glb(os.path.join(bundle, "meshes", source + ".glb"))
    except Exception as exc:
        return (asset, suffix, variant), {"error": f"{source}: {exc}"}
    if not nodes:
        return (asset, suffix, variant), {"error": f"{source}: no mesh-bearing node"}

    parts, lo, hi, area, materials_used = _collect(nodes, aliases, tinted, level_name)
    if not parts:
        return (asset, suffix, variant), {"error": f"{source}: every node was empty"}
    if kind == "tree" and options["tree_wind"]:
        parts = _with_wind_colours(parts)
    size = hi - lo
    radius = 0.5 * float(numpy.linalg.norm(size))
    game_px = lod.game_detail_px(radius, size, area, level_of_detail=options["level_of_detail"])
    detail_scale = _detail_scale(kind, options)
    null_px = max(1.0, game_px * detail_scale)

    shape_name = placement.shape_key(asset, suffix, variant)
    base_mesh = dae.group_by_material(dae.merge_meshes(shape_name, parts))
    levels, lod_stats, ladder = _lod_levels(
        derived_dir,
        source,
        shape_name,
        base_mesh,
        null_px,
        radius,
        displacement,
        kind,
        aliases,
        tinted,
        level_name,
        options,
    )
    # The collider comes off LOD 1 where the bake generated one — `collision.colmesh_from_lod` is
    # written for the ~25 % level, and a 20k-triangle building shell is not a collider. **WHICH
    # ENTRY OF THE LADDER THAT IS MOVED IN A8**: with `lod0_max_error` promoting LOD 1 to the top
    # detail, LOD 1 is `ladder[0]` and `ladder[1]` is LOD 2 — taking index 1 blindly would hand
    # every promoted shape a collider two levels coarse than intended.
    lod1_mesh = (
        ladder[0]
        if lod_stats.get("lod0Promoted")
        else (ladder[1] if len(ladder) > 1 else base_mesh)
    )
    if asset in options.get("no_collide", ()):
        colliders, collider_kind = (), "lightNone"
    else:
        colliders, collider_kind = _collider(
            shape_name, base_mesh, lod1_mesh, kind, options, is_tree
        )
    # How well the trunk search actually found a trunk. `collision.trunk_group` prefers a material run
    # NAMED for one, and on a CS2 bundle that branch never fires — the game's material names are
    # hashed asset ids (`lvl_OakTreeAdult01_c8092af6...`), so all 32 tree prefabs of the reference
    # city fall through to the geometric fallback. That fallback is right for most of them
    # (OakTree01: a 0.40 m cylinder under an 8.11 m canopy) and wrong where the lowest band is
    # not the trunk — a spruce whose branches sweep the ground, or a wide shrub CS2 counts as a
    # tree. MEASURED, not fixed: 5 of 32 prefabs and 4.5 % of tree items, declared in the report.
    trunk_ratio = None
    if collider_kind == "trunk" and len(colliders) == 1:
        cyl = numpy.asarray(colliders[0].verts, dtype=numpy.float64)
        base_v = numpy.asarray(base_mesh.verts, dtype=numpy.float64)
        canopy = 0.5 * float(max(numpy.ptp(base_v[:, 0]), numpy.ptp(base_v[:, 1])))
        trunk = 0.5 * float(max(numpy.ptp(cyl[:, 0]), numpy.ptp(cyl[:, 1])))
        trunk_ratio = round(trunk / canopy, 3) if canopy > 1e-6 else None

    # `gameCullMetres` divided by the declared `lod_detail_scale` is how far THIS level draws the
    # mesh; an imposter only earns its atlas past `imposter_min_metres`.
    reach_m = lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]) / max(
        detail_scale, 1e-6
    )
    wants_imposter = options["imposters"] and reach_m >= options["imposter_min_metres"]
    billboard_px = null_px if wants_imposter else None
    final_null = (null_px / IMPOSTER_REACH) if wants_imposter else null_px
    shape = dae.Shape(
        shape_name,
        levels,
        collision=colliders,
        billboard_px=int(max(1, round(billboard_px))) if billboard_px else None,
        null_px=int(max(1, round(final_null))),
    )
    path = os.path.join(out_dir, shape_name + ".dae")
    written = dae.write_shape(path, shape)
    return (asset, suffix, variant), {
        "file": shape_name + ".dae",
        "changed": bool(written.get("changed")),
        # the game's tree/plant split (`csiibundle.instances.is_tree`), carried to the instances
        # stage so a forest type made from a bush does not ship collidable
        "tree": bool(is_tree) if kind == "tree" else None,
        "variantKey": variant_key,
        "radius": round(radius, 4),
        "renderingSize": round(lod.rendering_size(size), 4),
        "lodBias": round(lod.lod_bias(size, area), 4),
        "gamePx": round(float(game_px), 4),
        "gameCullMetres": round(
            lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]), 1
        ),
        "nullPx": int(max(1, round(final_null))),
        "billboardPx": int(max(1, round(billboard_px))) if billboard_px else None,
        "boundsMin": [round(float(v), 4) for v in lo],
        "boundsMax": [round(float(v), 4) for v in hi],
        "triangles": written["triangles"],
        "vertices": written["vertices"],
        "materials": sorted(materials_used),
        "collider": collider_kind,
        "trunkRatio": trunk_ratio,
        "lods": lod_stats,
    }


def _collect(nodes, aliases, tinted, level_name):
    """Every node of one `.glb` as BeamNG-local mesh parts, plus the bounds and triangle area the
    game's LOD rule needs. Bounds and area are measured in the mesh's own (glTF) space, which is
    where `renderPrefab.bounds` and `CalcSurfaceArea` measure them too — the frame change is a
    rotation and a reflection, so neither number moves."""
    parts, materials_used = [], set()
    lo = numpy.array([math.inf] * 3)
    hi = numpy.array([-math.inf] * 3)
    area = 0.0
    for node in nodes:
        if len(node["tris"]) == 0:
            continue
        matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
        world = numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        lo = numpy.minimum(lo, world.min(axis=0))
        hi = numpy.maximum(hi, world.max(axis=0))
        area += lod.surface_area(world, node["tris"])
        # the node's rotation belongs to its normals as much as to its positions: 166 of the
        # reference bundle's 4,453 mesh-bearing nodes carry one (a boat lying on its side), and
        # lighting them by the unrotated normal is a shading bug with no visible geometry to
        # blame. The matrix is rigid on every bundle mesh, so a plain rotation is the whole
        # inverse transpose.
        normals_in = node["normals"]
        if normals_in is not None:
            normals_in = numpy.asarray(normals_in, dtype=numpy.float64) @ matrix[:3, :3].T
        local, normals = dae.glb_to_local(world, normals_in)
        runs = []
        for name, count in node["material_runs"]:
            bound = _bound_material(name, aliases, tinted, level_name)
            materials_used.add(aliases.get(name, name))
            runs.append((bound, count))
        parts.append(
            (local, normals, dae.glb_uvs(node["uvs"]), node.get("colors"), node["tris"], runs)
        )
    return parts, lo, hi, area, materials_used


def _with_wind_colours(parts):
    """Replace each part's vertex colours with BeamNG's forest wind convention
    (`dae.wind_vertex_colours`). Synthesised, cosmetic and off by default."""
    out = []
    for index, (verts, normals, uvs, _colors, tris, runs) in enumerate(parts):
        colours = dae.wind_vertex_colours(
            {"verts": verts, "tris": tris, "material_runs": runs}, up_axis=2, seed=index
        )
        out.append((verts, normals, uvs, colours, tris, runs))
    return out


def _bound_material(raw, aliases, tinted, level_name):
    """The material name this shape binds — the paint variant's when this shape is a variant and
    the bake wrote a tinted albedo for that material, else the canonical one."""
    canonical = aliases.get(raw, raw)
    variant_name = tinted.get(raw) or tinted.get(canonical)
    if variant_name:
        return variant_name

    return matbind.mapto(level_name, canonical)


# Vegetation keeps the ladder, and the deferral rule does not apply to it. MEASURED: it is 262 of
# 268 tree shapes over 1,346,600 placements, +24 % drawn triangles in a forest for a change nobody
# asked for — and the reason to decline it is not only the cost. The rule's input is a SURFACE
# DISTANCE, and a canopy is a cloud of alpha-tested cards: moving its leaves 0.3 m measures the
# same as moving a wall 0.3 m and reads as nothing at all, where the wall reads as a fault. The
# metric does not know the difference, so vegetation is left where A6.1 and the ladder had it.
# OURS, declared, and reversible the moment someone measures foliage error properly.
DISPLACEMENT_RULE_KINDS = ("building", "prop", "decal")

# A8/P2. Metres of measured surface error at which LOD 1 REPLACES LOD 0 rather than following it.
# 0 disables. LOD 0 is ~66 % of a shape's bytes and a shape is mounted whole for one instance in
# view, so where the decimator's own p95 displacement says LOD 1 is nowhere further than this from
# the surface it replaces, those bytes buy nothing on screen. Measured on the reference city:
# 0.05 m reaches 2,947 shapes for 282 MiB, **0.10 m reaches 3,375 for 387 MiB**, 0.50 m reaches
# 3,683 for 585 MiB. 0.10 is four times tighter than the point where the error starts landing on
# landmarks (`InternationalAirport01` is 0.42 m), and the figure is a p95, so 5 % of a promoted
# surface is further out than it.
LOD0_MAX_ERROR_DEFAULT = 0.10

# ...and the shape is kept whole when the promotion would leave it SHIPPING fewer triangles than
# this, whatever its displacement says.
#
# A8/P2's promotion exists to save BYTES, and a shape with no triangles has no bytes to save. The
# same decimation ratio means two different things at the two ends of the range: `Five_Luxe_JBR_`
# goes 243,371 -> 59,389 triangles (24 % kept) and is still a tower; `NA_ParkingSign01` goes
# **22 -> 4** (18 % kept) and 4 triangles is one quad — the sign's panel, with its post gone, which
# is what "the sign floats and the post is not rendered, even up close" was.
#
# A p95 SURFACE distance cannot see that. Flattening a 5 cm post moves its surface 3 mm, so
# `NA_ParkingSign01` measured 0.0032 m against a 0.10 m threshold and was promoted with room to
# spare — the exact failure `lod_switch_error_px`'s own note warns about ("a surface distance is
# not a visual error for thin geometry"), reached by the other lever.
#
# THE COUNT THAT MATTERS IS THE ONE THAT SHIPS, not LOD 0's. The ratio says nothing: `Five_Luxe_JBR_`
# keeps 24 % of its triangles and `NA_CommercialLow01_L5_4x3` keeps 25 %, and one is still a tower
# while the other is a facade with 381 triangles — not enough to keep its window openings closed,
# which is "you can see into the building". 59,389 triangles is plenty; 381 is not, and 4 is a
# sign's panel with no post.
#
# MEASURED over the reference city's 3,338 promoted shapes: a floor of 1,000 SHIPPED triangles
# protects 2,941 of them for **26 % of the triangles the promotion saved**, and every one of the
# heavy shapes it exists for — Five_Luxe_JBR_ 243,371 -> 59,389, ParkingHall01 231,552 -> 58,210 —
# is still promoted. A floor on LOD 0's count instead let this building through at 1,514.
LOD0_MIN_TRIANGLES = 1000


def _lod_levels(
    derived_dir,
    source,
    shape_name,
    base_mesh,
    null_px,
    radius,
    displacement,
    kind,
    aliases,
    tinted,
    level_name,
    options,
):
    """`[(px, [Mesh]), ...]` nearest-first: LOD 0 plus whatever the bake generated, each with the
    threshold its own measured displacement earns it.

    A detail's size in a Torque shape is the projected pixel size of the bounding-sphere radius
    at which that detail STARTS being used, so the size written on level *n* is where level *n+1*
    takes over — and that is the moment level *n+1*'s error appears on screen. `radius` and
    `displacement` are both in the mesh's own metres (the bundle's node matrices are rigid, so a
    node-local metre is a world metre), which is what makes the ratio between them a pure pixel
    count.
    """
    meshes = [base_mesh]
    generated = []
    stats = {"levels": 1, "triangles": [len(base_mesh.tris)], "lod0Promoted": False}
    if options["generate_lods"]:
        for level in (1, 2):
            path = os.path.join(derived_dir, "lods", f"{source}_LOD{level}.glb")
            if not os.path.isfile(path):
                continue
            nodes = glb.parse_glb(path)
            if not nodes:
                continue
            parts, _lo, _hi, _area, _used = _collect(nodes, aliases, tinted, level_name)
            if not parts:
                continue
            mesh = dae.group_by_material(dae.merge_meshes(f"{shape_name}_LOD{level}", parts))
            if len(mesh.tris) == 0:
                continue
            meshes.append(mesh)
            generated.append(level)
            stats["levels"] += 1
            stats["triangles"].append(len(mesh.tris))

    # A8/P2 — DROP LOD 0 where LOD 1 is measurably indistinguishable from it.
    #
    # A shape is mounted WHOLE for one instance in view, and LOD 0 is ~66 % of its bytes
    # (measured over 200 shapes and again over the 60 heaviest). Where the decimator's own
    # recorded displacement says LOD 1's surface is nowhere meaningfully away from LOD 0's, those
    # bytes buy nothing: the ladder simply starts one rung coarser, LOD 2 becomes the only
    # generated level, and every switch threshold below is computed exactly as before.
    #
    # `displacement[0]` is the p95 distance from LOD 1's surface back to LOD 0's, in the mesh's
    # own metres. On the reference city at 0.10 m this reaches 3,375 of 4,165 shapes and saves
    # ~387 MiB; `Five_Luxe_JBR_` goes 243,371 -> 59,389 triangles at 8 cm of error.
    #
    # `exempt` kinds keep LOD 0 unconditionally, for the same reason they are exempt from the
    # displacement switch rule: a 24-triangle lamp-post LOD measures 0.06 m out and is visually
    # total, so a distance is not a fair test of it (A6.7).
    promote = float(options.get("lod0_max_error") or 0.0)
    keep_small = int(options.get("lod0_min_triangles") or 0)
    if (
        promote > 0.0
        and generated
        and generated[0] == 1
        and len(meshes) > 1
        and kind in DISPLACEMENT_RULE_KINDS
        and displacement
        and displacement[0] is not None
        and float(displacement[0]) <= promote
        and len(meshes[1].tris) >= keep_small
    ):
        stats["lod0Promoted"] = True
        stats["lod0Triangles"] = len(base_mesh.tris)
        meshes = meshes[1:]  # LOD 1 is now the shape's own top detail
        generated = generated[1:]  # ...so only LOD 2 is still a GENERATED level
        stats["levels"] -= 1
        stats["triangles"] = stats["triangles"][1:]
    elif (
        promote > 0.0
        and generated
        and generated[0] == 1
        and len(meshes) > 1
        and kind in DISPLACEMENT_RULE_KINDS
        and displacement
        and displacement[0] is not None
        and float(displacement[0]) <= promote
    ):
        stats["lod0KeptSmall"] = len(meshes[1].tris)  # too few triangles to survive the drop

    floor = max(2.0, null_px * LADDER_RATIOS[0])
    # one threshold per mesh, nearest first: each generated level's switch point, and the floor
    # for the coarsest mesh, which has to hold the last band before `nulldetail`. The ladder's
    # own value for this rung is the CEILING — the measured error may push a level further out,
    # never nearer than the spacing that has always been there.
    thresholds = []
    shipped = []
    exempt = kind not in DISPLACEMENT_RULE_KINDS
    for index, level in enumerate(generated):
        metres = None
        if displacement and len(displacement) >= level and not exempt:
            metres = displacement[level - 1]
        ceiling = max(
            2.0, null_px * LADDER_RATIOS[min(len(meshes) - 1 - index, len(LADDER_RATIOS) - 1)]
        )
        # `metres` is None for an exempt kind and for a level the bake did not measure; the
        # switch helper answers with its ceiling, which the clamp turns back into the ladder
        earned = lod.displacement_switch_px(radius, metres, options["switch_error_px"])
        px = max(floor, min(ceiling, earned))
        thresholds.append(px)
        shipped.append(
            {
                "level": level,
                "metres": None if metres is None else round(metres, 4),
                "ladderPx": int(round(ceiling)),
                "exempt": exempt,
                "deferred": bool(px < ceiling - 0.5),
            }
        )
    thresholds.append(floor)
    # `split_mesh` caps every <geometry> at MAX_GEOMETRY_VERTS: BeamNG's per-mesh index buffers
    # wrap past 65,535 and the shape shards in-game (load #10). A hospital reached 68,856.
    fixed = []
    for px, mesh in zip(thresholds, meshes):
        px = int(round(px))
        # the ladder must strictly decrease or Torque reads two details as one
        if fixed and px >= fixed[-1][0]:
            px = fixed[-1][0] - 1
        if px < 2:
            continue
        fixed.append((px, dae.split_mesh(mesh)))
    stats["detailSizes"] = [px for px, _ms in fixed]
    stats["splitGeometries"] = sum(len(ms) for _px, ms in fixed) - len(fixed)
    # what each generated level actually ships with: the threshold above it (where it takes over)
    # and the pixels its displacement covers there — the approximation, counted
    for index, entry in enumerate(shipped):
        entry["px"] = stats["detailSizes"][index] if index < len(stats["detailSizes"]) else None
        entry["errorPx"] = (
            round(lod.displacement_error_px(radius, entry["metres"], entry["px"]), 2)
            if entry["metres"] is not None and entry["px"]
            else None
        )
    stats["switches"] = shipped
    return fixed, stats, meshes


def _detail_scale(kind, options):
    """How many times closer than CS2 this level culls a shape of `kind`.

    One global multiplier over two things whose own distances differ by 12.7x is the A6.2 defect:
    it preserves the ratio and destroys the relationship. `lod_detail_scale_prop` gives props
    their own rate; 0 keeps the single global number, so this is inert unless asked for.

    THE GAME'S OWN RULE IS NOT THIS, and is worth stating where the deviation lives.
    `PreCullingSystem.RelativeCullingJob` culls a sub-object at the distance to its OWNER's bounds
    while keeping the sub-object's own `m_MinLod` — so CS2 buys a facade prop its building's own
    extent, tens of metres, not an order of magnitude (audit §70). That half is NOT ported, and
    the reason is structural rather than a shortcut: BeamNG carries detail thresholds in the
    SHAPE file, one per prefab, and ownership is per INSTANCE — the same prop stands both
    free-standing and bolted to a tower — so there is no per-instance threshold to put it in.
    """
    if kind == "prop" and options.get("detail_scale_prop"):
        return float(options["detail_scale_prop"])
    return float(options["detail_scale"])


def _collider(shape_name, base_mesh, lod_mesh, kind, options, is_tree=True):
    """The collision policy, in the plan's order: trunk cylinder for a tree, the decimated LOD
    when it is thick enough, a padded box otherwise. `()` when this class does not collide.

    `base_mesh` is LOD 0 — the trunk search wants its material runs — and `lod_mesh` is what the
    colmesh is cut from (LOD 1 when the bake generated one, else LOD 0).

    `is_tree` splits the `tree` kind, which is ALL vegetation: 376,187 of the reference city's
    686,641 planted items are grass, bushes, flower pots and garden beds, and CS2 builds them
    from `PlantObject`, not `TreeObject` (`csiibundle.instances.is_tree`). A plant does not
    collide — you drive through a hedge and over long grass — and it certainly does not get a
    trunk, which is what fitting `trunk_cylinder` to a thing with no trunk was doing.
    """
    if not options["collide"].get(kind, False):
        return (), "none"
    if kind == "tree" and not is_tree:
        return (), "plant"
    verts = lod_mesh.verts
    tris = lod_mesh.tris
    if kind == "tree":
        # the MERGED mesh, in the same BeamNG-local frame the visible geometry is written in, so
        # the trunk group is found across every sub-mesh (a tree is trunk + leaves, and reading
        # only the first node picks whichever of the two the file happens to list first)
        cyl_v, cyl_t = collision.trunk_cylinder(
            {"verts": base_mesh.verts, "tris": base_mesh.tris, "material_runs": base_mesh.runs},
            up_axis=2,
        )
        return (
            dae.Mesh(f"Colmesh_{shape_name}", cyl_v, cyl_t, [(base_mesh.runs[0][0], len(cyl_t))]),
        ), "trunk"
    lo, hi = collision.bounds(base_mesh.verts)  # the box is LOD 0's, never a decimated hull's
    footprint = max(float(hi[0] - lo[0]), float(hi[1] - lo[1]))
    # A PADDED BOX IS ONLY A STAND-IN WHILE IT IS SMALL. Over the triangle cap the collider used
    # to fall back to the shape's bounding box unconditionally, and on the median shape that box
    # is 1.6 m across and perfectly reasonable. On `InternationalAirport01` it is
    # 1,344 x 1,245 x 82 m — an invisible solid block a kilometre wide, and driving onto the
    # airport puts the car inside it. That is the reported "the car gets stuck in the airport",
    # and 34 shapes carry a box over 20 m: the harbour, the power plant, a stadium, a ballpark.
    # Past that size a box stops approximating the building and becomes a wall round a district,
    # so the triangle cap gives way instead: a few thousand collision triangles cost far less
    # than a trap.
    oversized = footprint > BOX_COLLIDER_MAX_METRES
    if (
        options["collision_preset"] == "exact"
        or len(tris) <= options["colmesh_max_tris"]
        or oversized
    ):
        mesh = collision.colmesh_from_lod(
            {"verts": verts, "tris": tris}, dae.MIN_COLLIDER_THICKNESS
        )
        if mesh is not None:
            return (
                dae.Mesh(
                    f"Colmesh_{shape_name}",
                    mesh["verts"],
                    mesh["tris"],
                    [(base_mesh.runs[0][0], len(mesh["tris"]))],
                ),
            ), (
                "mesh_oversized"
                if oversized and len(tris) > options["colmesh_max_tris"]
                else "mesh"
            )
    box_v, box_t = collision.colbox(lo, hi, dae.MIN_COLLIDER_THICKNESS)
    return (
        dae.Mesh(f"Colmesh_{shape_name}", box_v, box_t, [(base_mesh.runs[0][0], len(box_t))]),
    ), "box"


# -- the stage -----------------------------------------------------------------------------------


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    demand, scan_stats, dropped = scan_demand(ctx, progress)

    variants = _paint_variants(ctx)
    aliases, tinted_by_variant = _material_tables(ctx, level)
    out_dir = level.path("art", "shapes", SHAPES_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)

    # What each generated level COST, measured by the bake (`bake/lods`): the p95 distance from
    # the level's surface back to LOD 0's, per asset. Its absence is not a fallback to the old
    # ratio ladder - it means the derived cache predates the measurement, and the switch points
    # would silently go back to being a function of the shape's size.
    lod_index = bake_lods.load_index(ctx.derived_dir) if settings.get("generate_lods", True) else {}
    if settings.get("generate_lods", True) and not lod_index:
        raise RuntimeError(
            f"{bake_lods.INDEX_NAME} is missing from {ctx.derived_dir}: the LOD switch points are "
            "derived from each level's measured displacement, so the `lods` bake product must be "
            "re-baked (it is version 2 or later that writes it)"
        )

    options = {
        "level_of_detail": lod.DEFAULT_LEVEL_OF_DETAIL,
        "detail_scale": float(settings.get("lod_detail_scale", 6.0) or 6.0),
        # A6.2: props are given up at their own rate. 0 = the global one, so nothing changes
        # unless it is asked for.
        "detail_scale_prop": float(settings.get("lod_detail_scale_prop") or 0.0),
        "generate_lods": bool(settings.get("generate_lods", True)),
        "imposters": bool(settings.get("imposters", True)),
        "imposter_min_metres": (
            float(settings.get("imposter_min_metres") or 0)
            or IMPOSTER_VIEW_FRACTION * float(settings.get("view_distance") or 4000)
        ),
        "collision_preset": str(settings.get("collision_preset") or "simplified"),
        "colmesh_max_tris": 20000,
        "tree_wind": bool(settings.get("tree_wind", False)),
        # A6.7: how many pixels of surface error a generated level may show where it takes over
        "switch_error_px": float(settings.get("lod_switch_error_px") or DEFAULT_SWITCH_ERROR_PX),
        # A8/P2: metres of measured surface error at which LOD 1 replaces LOD 0 outright
        "lod0_max_error": float(settings.get("lod0_max_error", LOD0_MAX_ERROR_DEFAULT) or 0.0),
        # ...and never on a shape this small, whatever the measured displacement says
        "lod0_min_triangles": int(
            settings.get("lod0_min_triangles", LOD0_MIN_TRIANGLES)
            if settings.get("lod0_min_triangles") is not None
            else LOD0_MIN_TRIANGLES
        ),
        "collide": _collision_policy(settings),
        # A prefab CS2 gives a photometric light to is a LAMP, and `collision_lights` says
        # whether one is solid. The set is the game's own answer to "is this a lamp" - see the
        # setting's help - and it is empty when lamps are allowed to collide.
        "no_collide": _light_bearing(ctx.bundle)
        if not settings.get("collision_lights")
        else frozenset(),
    }

    jobs = {}
    for (asset, suffix), entry in sorted(demand.items()):
        wanted = sorted(entry["variants"]) if entry["forest"] and entry["variants"] else []
        prefab_variants = (variants.get(asset) or {}).get("variants") or []
        for index in wanted:
            centre = prefab_variants[index] if index < len(prefab_variants) else None
            key = tints.variant_key(centre) if centre is not None else None
            tinted = (tinted_by_variant.get(key) or {}) if key else {}
            # a variant the bake wrote no tinted albedo for is the UNTINTED shape (`variant=None`),
            # not variant 0 - see `placement.shape_key`
            index_out = index if tinted else None
            jobs.setdefault(
                (asset, suffix, index_out),
                (
                    ctx.bundle,
                    ctx.derived_dir,
                    out_dir,
                    level.name,
                    asset,
                    suffix,
                    index_out,
                    key if tinted else None,
                    entry["kind"],
                    options,
                    aliases,
                    tinted,
                    entry["tree"],
                    _displacement(lod_index, asset, suffix),
                ),
            )
        # the UNTINTED shape: what a TSStatic binds (it keeps `colorPaletteMap` and carries its
        # own `instanceColor*`), and what a prefab with no baked variants gets
        if entry["static"] or not wanted:
            jobs.setdefault(
                (asset, suffix, None),
                (
                    ctx.bundle,
                    ctx.derived_dir,
                    out_dir,
                    level.name,
                    asset,
                    suffix,
                    None,
                    None,
                    entry["kind"],
                    options,
                    aliases,
                    {},
                    entry["tree"],
                    _displacement(lod_index, asset, suffix),
                ),
            )
    jobs = list(jobs.values())

    progress.begin("shapes: writing .dae", total=len(jobs), unit="shapes")
    results, errors = {}, []
    workers = ctx.jobs or 1
    if workers > 1 and len(jobs) > 8:
        mp = multiprocessing.get_context("spawn")
        with mp.Pool(min(workers, len(jobs))) as pool:
            for key, value in pool.imap_unordered(_shape_job, jobs, chunksize=8):
                _absorb(key, value, results, errors, progress)
    else:
        for job in jobs:
            key, value = _shape_job(job)
            _absorb(key, value, results, errors, progress)
    for error in errors[:20]:
        progress.log("shapes: " + error)
    # art/shapes/ is no longer cleared at build start (that threw away every .cdae and cost the
    # whole cook on a rebuild), so this stage owns its own directory: what it did not write this
    # build is an orphan and goes now, .cdae included.
    rewritten = sum(1 for v in results.values() if v.get("changed"))
    orphans = dae.sweep_orphans(out_dir, {os.path.splitext(v["file"])[0] for v in results.values()})
    progress.end(
        f"{len(results):,} shapes ({rewritten:,} rewritten, {len(results) - rewritten:,} unchanged), "
        f"{orphans} orphans removed, {len(errors)} failed"
    )

    shared = ctx.shared if ctx.shared is not None else {}
    used = shared.setdefault("materials_used", set())
    variant_used = shared.setdefault("materials_variants_used", {})
    for value in results.values():
        used.update(value["materials"])
        key = value.get("variantKey")
        if key:
            variant_used.setdefault(key, set()).update(value["materials"])
    shared["shapes"] = {
        "dir": SHAPES_SUBDIR,
        "results": results,
        "demand": demand,
        "variants": variants,
        "droppedRecords": sorted(dropped),
        "options": {k: v for k, v in options.items() if k != "collide"},
    }
    return _report(
        results, demand, scan_stats, options, errors, started, progress, rewritten, orphans
    )


def _displacement(lod_index, asset, suffix):
    """The bake's per-level displacement for one mesh state, in metres, or `None` per level when
    the bake did not generate that level (a mesh too small to decimate keeps only LOD 0)."""
    entry = lod_index.get(asset + suffix) or {}
    return entry.get("displacement")


def _absorb(key, value, results, errors, progress):
    progress.advance(1)
    if "error" in value:
        errors.append(value["error"])
    else:
        results[key] = value
    if (len(results) + len(errors)) % 200 == 0:
        progress.check()


def _light_bearing(bundle_dir):
    """Every prefab CS2 gives a photometric light to — the lamps, from the game's own data.

    `lights.json` is keyed by prefab and holds two kinds of entry: a photometric light (a real
    source) and a colour slot (emissive material state — a neon sign, a traffic signal's red).
    Only the first makes something a lamp, and `csiibundle.lights.photometric_lights` is the
    split. On the reference city that is **218 prefabs of the 1,186 in `lights.json`**: 44 lamps
    (every `Lightpole*`, `WallLight*`, `Streetlight*`, `GardenLight*`), 115 floodlit billboards,
    the vehicles, and none of the 12 traffic lights — which is why this is the discriminator
    rather than a name match (CLAUDE.md, and the tree/plant lesson behind it). The billboards are
    the part a name match would never have produced, and they are declared in audit §88 rather
    than exempted.
    """
    from csiibundle import lights as lights_mod

    try:
        catalogue = lights_mod.load(bundle_dir)
    except (OSError, ValueError):
        return frozenset()
    # `photometric_lights` takes the CATALOGUE and yields `(prefab, entry)`. Handing it one
    # prefab's entry LIST and testing the generator for truth is true for every prefab in
    # `lights.json` — 1,186 of them rather than the 218 that own a light — which is what this
    # line did until it was measured (audit §88).
    return frozenset(prefab for prefab, _entry in lights_mod.photometric_lights(catalogue))


def _collision_policy(settings):
    """Which record kinds get a collider — the plan's "everything physical collides by default"
    expressed against the settings the UI exposes."""
    if str(settings.get("collision_preset") or "simplified") == "off":
        return {}
    return {
        "building": bool(settings.get("collision_buildings", True)),
        "prop": bool(settings.get("collision_props", True)),
        "tree": bool(settings.get("collision_trees", True)),
    }


def _material_tables(ctx, level):
    """`(aliases, {variantKey: {rawMaterial: boundName}})` from the bake's `materials.json`.

    A variant's tinted albedo is a different TEXTURE for the same material, so it needs its own
    BeamNG material; the materials stage writes it under this name when it sees it bound.
    """

    path = os.path.join(ctx.derived_dir, "materials.json")
    aliases, by_variant = {}, {}
    if not os.path.isfile(path):
        return aliases, by_variant
    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)
    aliases = table.get("aliases") or {}
    for name, entry in (table.get("materials") or {}).items():
        derived = entry.get("derived") or {}
        # `variantFactors` covers EVERY variant the bake measured; `variants` only the ones whose
        # per-texel tint a single `baseColorFactor` could not stand in for (A4 §2.4). A shape binds
        # a variant material either way — the difference is whether that material carries its own
        # tinted albedo or just a multiplier on the canonical one.
        for key in derived.get("variantFactors") or derived.get("variants") or {}:
            canonical = entry.get("canonical") or name
            by_variant.setdefault(key, {})[name] = matbind.mapto(level.name, f"{canonical}__v{key}")
    return aliases, by_variant


# How wide a padded BOX collider may be before it is a trap rather than an approximation.
# OURS: CS2 has no collision for a placed object, so there is no rule to port. 20 m is about the
# footprint of a single building, which is the largest thing a box still resembles; measured over
# the reference city, 1,373 of 1,407 boxes are under it (median 1.6 m, p90 6.5 m) and the 34 above
# it are the airport, the harbour, the power plant and a handful of stadiums.
BOX_COLLIDER_MAX_METRES = 20.0

# A trunk collider wider than this share of the shape's own canopy is not a trunk. OURS — CS2
# has no collision for a planted object at all — and it is a REPORTING threshold only: nothing
# behaves differently either side of it, it decides when the build says so out loud.
TRUNK_WIDE_RATIO = 0.5


def _switch_summary(results):
    """What the displacement rule actually did, per generated level: the distribution of the
    measured error, the thresholds it produced, and how many shapes it FLOORED - a floored level
    is one too crude to earn a place at this pixel budget, so it is drawn only in the last band
    before the shape disappears and the shape effectively keeps LOD 0 instead. Counting them is
    the honest way to see what the budget bought."""
    per_level = {}
    exempted = 0
    for row in results.values():
        for entry in row["lods"].get("switches") or []:
            if entry.get("exempt"):
                exempted += 1
                continue
            if entry.get("metres") is None:
                continue
            bucket = per_level.setdefault(
                entry["level"], {"metres": [], "px": [], "error": [], "deferred": 0, "factor": []}
            )
            bucket["metres"].append(entry["metres"])
            bucket["px"].append(entry["px"] or 0)
            bucket["error"].append(entry["errorPx"] or 0.0)
            if entry.get("deferred"):
                bucket["deferred"] += 1
                if entry.get("px"):
                    bucket["factor"].append(entry["ladderPx"] / max(entry["px"], 1e-6))
    out = {"exemptLevels": exempted}
    for level, bucket in sorted(per_level.items()):
        metres = sorted(bucket["metres"])
        error = sorted(bucket["error"])

        def at(values, fraction):
            return round(values[min(len(values) - 1, int(fraction * len(values)))], 4)

        factor = sorted(bucket["factor"])
        out[f"LOD{level}"] = {
            "shapes": len(metres),
            "metresP50": at(metres, 0.5),
            "metresP90": at(metres, 0.9),
            "metresMax": at(metres, 1.0),
            "errorPxP50": at(error, 0.5),
            "errorPxP90": at(error, 0.9),
            "identical": sum(1 for m in metres if m <= 1e-6),
            # the only shapes this rule changes: the ones whose level was too far out of shape to
            # keep the ladder's place, and how many times further out it went
            "deferred": bucket["deferred"],
            "deferredMedianFactor": (round(factor[len(factor) // 2], 2) if factor else None),
            "deferredMaxFactor": (round(factor[-1], 2) if factor else None),
        }
    return out


def _report(
    results, demand, scan_stats, options, errors, started, progress, rewritten=0, orphans=0
):
    rows_by_pair = {}
    for (asset, suffix, _variant), row in results.items():
        rows_by_pair.setdefault((asset, suffix), []).append(row)
    by_kind = {}
    for pair, entry in demand.items():
        rows = rows_by_pair.get(pair)
        if not rows:
            continue
        bucket = by_kind.setdefault(
            entry["kind"], {"shapes": 0, "instances": 0, "gameCull": [], "ourCull": []}
        )
        bucket["shapes"] += len(rows)
        bucket["instances"] += entry["count"]
        for row in rows:
            bucket["gameCull"].append(row["gameCullMetres"])
            # distance scales inversely with the pixel threshold, so ours is the game's distance
            # times (game px / shipped px). `nullPx` is what the level actually stops drawing at,
            # imposter reach included.
            bucket["ourCull"].append(
                row["gameCullMetres"] * row["gamePx"] / max(row["nullPx"], 1e-6)
            )
    summary = {}
    for kind, bucket in by_kind.items():
        game = sorted(bucket["gameCull"])
        ours = sorted(bucket["ourCull"])
        summary[kind] = {
            "shapes": bucket["shapes"],
            "instances": bucket["instances"],
            "gameCullMedian": round(game[len(game) // 2], 1),
            "ourCullMedian": round(ours[len(ours) // 2], 1),
        }
        progress.log(
            f"shapes: {kind} - {bucket['shapes']} shapes, CS2 draws the median one to "
            f"{summary[kind]['gameCullMedian']:,.0f} m, this level to "
            f"{summary[kind]['ourCullMedian']:,.0f} m"
        )
    switch_summary = _switch_summary(results)
    colliders = {}
    for row in results.values():
        colliders[row["collider"]] = colliders.get(row["collider"], 0) + 1
    # trunk cylinders that are really canopy cylinders — see `trunkRatio` in `_shape_job`
    trunk_ratios = [r["trunkRatio"] for r in results.values() if r.get("trunkRatio") is not None]
    trunk_wide = sum(1 for r in trunk_ratios if r > TRUNK_WIDE_RATIO)
    for name, entry in switch_summary.items():
        if not isinstance(entry, dict) or "deferred" not in entry:
            continue
        progress.log(
            f"shapes: {name} is DEFERRED past the ladder on {entry['deferred']:,} of "
            f"{entry['shapes']:,} shapes (median {entry['deferredMedianFactor']}x further "
            f"out, worst {entry['deferredMaxFactor']}x); the rest keep the ladder's own "
            f"place, which no shape now takes NEARER than"
        )
    promoted = [r for r in results.values() if (r.get("lods") or {}).get("lod0Promoted")]
    kept_small = [r for r in results.values() if (r.get("lods") or {}).get("lod0KeptSmall")]
    if kept_small:
        smallest = min(int(r["lods"]["lod0KeptSmall"]) for r in kept_small)
        progress.log(
            f"shapes: LOD0 is KEPT on {len(kept_small):,} shapes that measured within "
            f"{options.get('lod0_max_error')} m of their LOD1 but hold fewer than "
            f"{options.get('lod0_min_triangles'):,} triangles once promoted (smallest "
            f"{smallest}) - a surface distance cannot see a decimation that opened the "
            f"object up, and 381 triangles is a facade you can see through. "
            f"{len(promoted):,} shapes were still promoted"
        )
    return {
        "shapes": len(results),
        "lod0Promoted": len(promoted),
        "lod0KeptSmall": len(kept_small),
        "shapesRewritten": rewritten,
        "orphansRemoved": orphans,
        "failed": len(errors),
        "errors": errors[:20],
        "scan": scan_stats,
        "triangles": sum(r["triangles"] for r in results.values()),
        "colliders": colliders,
        "trunkFit": {
            "shapes": len(trunk_ratios),
            "wide": trunk_wide,
            "medianRatio": (
                round(sorted(trunk_ratios)[len(trunk_ratios) // 2], 3) if trunk_ratios else None
            ),
            "ratio": TRUNK_WIDE_RATIO,
        },
        "imposters": {
            "shapes": sum(1 for r in results.values() if r.get("billboardPx")),
            "ofShapes": len(results),
            "minMetres": round(options["imposter_min_metres"], 1),
            "vanillaShare": "west_coast_usa 3.1 %, italy 7.2 %, johnson_valley 4.4 %",
        },
        "detailScale": options["detail_scale"],
        "detailScaleProp": options.get("detail_scale_prop") or options["detail_scale"],
        "levelOfDetail": options["level_of_detail"],
        "drawDistance": summary,
        "approximations": {
            "lodDetailScalePerKind": {
                "what": "a PROP's cull threshold uses `lod_detail_scale_prop` instead of the "
                "global multiplier",
                "why": "CS2 draws a median prop to 1,024 m and a median building to 13,008 m, a "
                "factor of 12.7, and one global divisor preserves that ratio while "
                "destroying the relationship - at 105 m a window frame is plainly visible "
                "where at CS2's 1,024 m it is sub-pixel, so facades read as construction "
                "sites (A6.2). DECLARED TUNING: the game has no per-kind divisor. The "
                "game's actual sub-object rule - cull at the OWNER's bounds, keep your own "
                "m_MinLod (audit §70) - is NOT ported, because BeamNG holds detail "
                "thresholds per SHAPE and ownership is per INSTANCE",
                "count": sum(
                    1
                    for pair, entry in demand.items()
                    if entry.get("kind") == "prop" and pair in rows_by_pair
                ),
                "value": options.get("detail_scale_prop") or options["detail_scale"],
            },
            "lodDetailScale": {
                "what": "every shape's cull threshold is CS2's own, multiplied by lod_detail_scale",
                "why": "CS2 draws a median prop to 1 km and a median building to 7.3 km over 1.2M "
                "objects; BeamNG cannot. 1.0 restores the game exactly.",
                "count": len(results),
                "value": options["detail_scale"],
            },
            "lodLadderSwitchPoints": {
                "what": "where LOD1/LOD2 take over: the distance at which that level's own "
                f"measured displacement from LOD 0 is worth "
                f"{options['switch_error_px']:.0f} px",
                "why": "CS2 ships no LOD chain for these prefabs (lodCap 0), so there is no game "
                "rule to port. The ladder used to be fixed multiples of the shape's CULL "
                "threshold, and that threshold is a statement about the shape's SIZE, not "
                "about its LOD's ERROR: a LOD1's displacement runs from 0 m on 603 of "
                "4,261 shapes to 37 m on the worst, so one multiple cannot serve both ends "
                "and the coarse end reads as a roof moving under props that have not moved "
                "(A6.7). Each level now takes its threshold from its own displacement, "
                "measured by the bake as the p95 distance from the generated surface back "
                "to LOD 0's. The PIXEL BUDGET is still ours: "
                "about a pixel is what fidelity would ask for and costs +384 % to +630 % "
                "drawn triangles (measured over 1.16 M placements from four viewpoints), "
                "where 6 px costs +0 % to +16 %, so `lod_switch_error_px` is a declared "
                "budget and not a measurement",
                "count": sum(1 for r in results.values() if r["lods"]["levels"] > 1),
                "value": options["switch_error_px"],
                "kinds": list(DISPLACEMENT_RULE_KINDS),
                "displacement": switch_summary,
            },
            "treeTrunkCylinderFit": {
                "what": "a tree whose trunk collider is wider than "
                f"{TRUNK_WIDE_RATIO:.0%} of its own canopy - the trunk search found the "
                "canopy instead",
                "why": "CS2 has no collision for a planted object, so there is no rule to port. "
                "`collision.trunk_group` prefers a material run NAMED trunk/bark/stem, "
                "and on a CS2 bundle that branch never fires - the game's material names "
                "are hashed asset ids - so every tree falls through to the geometric "
                "fallback (lowest band, smallest footprint). That is right for a clean "
                "bole and wrong where the lowest band is not the trunk: a spruce whose "
                "branches sweep the ground, or a wide shrub CS2 counts as a tree because "
                "it carries growth. MEASURED on the reference city: 5 of 32 tree prefabs, "
                "4.5 % of tree items",
                "count": trunk_wide,
            },
            "oversizedBoxColliders": {
                "what": f"a shape whose footprint is over {BOX_COLLIDER_MAX_METRES:.0f} m keeps a "
                f"MESH collider even past the triangle cap, instead of a padded box",
                "why": "a padded box is a fine stand-in at the median shape's 1.6 m and a trap at "
                "1,344 m: `InternationalAirport01`'s box was 1,344 x 1,245 x 82 m, an "
                "invisible solid block a kilometre wide, and driving onto the airport put "
                "the car inside it. 34 of 1,407 boxes were over 20 m - the harbour, the "
                "power plant, stadiums. CS2 has no collision for a placed object, so the "
                "20 m is OURS and is declared here",
                "count": sum(1 for r in results.values() if r.get("collider") == "mesh_oversized"),
                "value": BOX_COLLIDER_MAX_METRES,
            },
            "orphansOfAbsentOwners": {
                "what": "a record whose owner the producer declared ABSENT is not placed either",
                "why": "not a CS2 rule — the game had those prefabs and drew the buildings. It is "
                "a consumer's obligation against a DECLARED gap (§10): the prefab did not "
                "resolve at rip time (an unowned asset pack), so we drop the building and "
                "would otherwise place its props in mid-air — 6,714 records on the "
                "reference city, up to 292 m up, 695 of them hanging off ONE absent tower. "
                "Only the absent class is dropped: a prefab that exists WITHOUT GEOMETRY "
                "keeps its 5,806 children (the lamps and benches of car parks and sports "
                "pitches, whose own surface ships in the `areas` layer), and a construction "
                "site keeps its children too — measurement says they are the "
                "ConstructionCrane01 and the site's benches, which §4 states are real. "
                "THE REMEDY AT SOURCE IS CHEAPER: enable those asset packs in CS2 and "
                "re-rip, which brings the buildings back instead of hiding their props",
                "count": (scan_stats.get("orphans") or {}).get("orphans", 0),
                "owners": (scan_stats.get("orphans") or {}).get("owners", 0),
            },
            "foliageRoadClearance": {
                "what": f"a TREE standing within "
                f"{(scan_stats.get('roadclear') or {}).get('clearance', 0):.2f} m of a "
                f"drivable lane centre is not placed",
                "why": "A DECLARED DEVIATION, asked for, not a fidelity rule - CS2 clears objects "
                "when a road is BUILT and whatever survives in the save is what the city "
                "contains, so there is no game rule to port. Trees only: a bush or a "
                "flower pot is driven straight through once it stops colliding (A6.1), so "
                "removing it would cost fidelity and buy no drivability. The distance is "
                "the VEHICLE ENVELOPE, not an edge found in the data - the sweep showed "
                "trees per 0.25 m band are almost FLAT from 0.5 m out to 5 m, so there is "
                "no natural threshold to prefer. `foliage_road_clearance` 0 turns it off. "
                "Separately and always on, CS2's own `Overridden` rule removes what the "
                "game itself does not draw (instances.should_draw)",
                "count": (scan_stats.get("roadclear") or {}).get("clipped", 0),
                "value": (scan_stats.get("roadclear") or {}).get("clearance", 0),
                "ofTrees": (scan_stats.get("roadclear") or {}).get("trees", 0),
            },
            "renderingSizeSpecialCases": {
                "what": "the human-object and Standing-leg shrinks in GetRenderingSize",
                "why": "both need ObjectGeometryData flags the bundle does not ship; both only "
                "shrink the size, so the threshold errs towards drawing further",
                "count": None,
            },
        },
        "seconds": round(time.monotonic() - started, 1),
    }


# `placement.admit` is the reason trees/props/buildings and the two density sliders are here:
# they are read through a helper, so no source scan of THIS file would find them (the runtime
# recorder in pipeline/state.py does, and refuses to record the stage if one is missing).
INPUTS = (
    "layer:instances",
    "layer:meshes",
    "derived:lods",
    "derived:materials.json",
    "derived:paint_variants.json",
)
SETTINGS = (
    "trees",
    "props",
    "buildings",
    "foliage_density",
    "prop_density",
    "region",
    "foliage_road_clearance",
    "generate_lods",
    "imposters",
    "imposter_min_metres",
    "view_distance",
    "lod_detail_scale",
    "lod_detail_scale_prop",
    "lod_switch_error_px",
    "lod0_max_error",
    "lod0_min_triangles",
    "tree_wind",
    "collision_preset",
    "collision_buildings",
    "collision_props",
    "collision_trees",
    "collision_lights",
)
OUTPUTS = ("art/shapes/cs2",)

# version 13: a prefab CS2 gives a photometric light to ships NO collider while `collision_lights`
# is off - it renders and a car drives through it (audit §88)
# version 12: ...and that floor is on the count the shape SHIPS, not on LOD 0's. A ratio says
# nothing (a tower and a hole both keep ~25 %), and a floor on LOD 0's count left the
# 1,514-triangle facade promoted to 381
# version 11: `lod0_min_triangles` - version 10's promotion is REFUSED where it would leave the
# shape too small to stay closed, whatever its p95 displacement measured; flattening a 5 cm sign
# post moves its surface 3 mm, so a 22-triangle sign shipped as 4 and a facade as 381 (audit §87)
# version 9: vegetation keeps the ladder (the rule's metric says nothing useful about a canopy,
# and applying it held 1,346,600 tree placements at full geometry for +24 % triangles)
# version 10: A8/P2 - LOD 0 is DROPPED where the decimator's own measured displacement says LOD 1
# is within `lod0_max_error` of it, so the shape's own top detail becomes LOD 1 and the ladder
# starts one rung coarser; LOD 0 is ~66 % of a shape's bytes and a shape is mounted whole
# version 8: that rule may only DEFER a level, never advance it - allowing both directions put
# 2,586 of 2,903 prop shapes and 615 of 1,057 building shapes onto decimated geometry NEARER than
# the ladder ever had (a streetlight's 24-triangle level from 13 m), and the load came back worse
# version 7: a generated level's switch point comes from its measured displacement (A6.7)
# version 6: an oversized shape keeps a mesh collider instead of a box that traps the car
# version 5: props take their own detail scale (A6.2)
# version 4: orphans of absent owners are not placed (A6.6)
# version 3: the road-clearance deviation (A6.4)
# version 2: the tree/plant split (A6.1) - a plant gets no collider and no trunk cylinder, and
# every shape row carries `tree` and `trunkRatio`
STAGE = Stage(
    STAGE_KEY,
    "Object shapes",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    shared_keys=("shapes", "materials_used", "materials_variants_used"),
    version=13,
)
