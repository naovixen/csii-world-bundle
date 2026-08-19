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

The rest of the ladder — where LOD1 and LOD2 take over, and where an imposter does — has no game
counterpart at all: CS2 ships no LOD chain for these prefabs (`lodCap 0`), so the bake generates
one and the switch points are ours. They are geometric multiples of the shape's own null
threshold, so they scale with it rather than being absolute metres.
"""

import json
import math
import multiprocessing
import os
import time

import numpy

from csiibundle import collision, glb, instances as bundle_instances, lod, tints

from ..beamng import dae, placement
from . import Stage, level_of

STAGE_KEY = "shapes"
SHAPES_SUBDIR = "cs2"

# The LOD ladder above the null threshold, as multiples of it. Each level is used from its own
# pixel size upward, so LOD2 (coarsest) sits just above `nulldetail` and LOD0 furthest up.
# OURS — CS2 has no LOD chain for objects; the ratios are the vanilla BeamNG spacing (3-5x
# between consecutive details across the shipped shapes read in A2).
LADDER_RATIOS = (1.5, 6.0, 24.0)

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
    have = {}                                          # asset+suffix -> bool, cached file probes
    variants = _paint_variants(ctx)
    forest_allowed = True
    demand = {}
    stats = {"records": 0, "kept": 0, "noMesh": 0, "skippedBaked": 0, "skippedState": 0,
             "skippedKind": 0, "skippedDensity": 0, "skippedRegion": 0,
             "skippedRoadclear": 0, "skippedOrphan": 0}                 # `placement.admit`'s reasons
    # A6.6 — records whose owner the producer declared ABSENT (§10). Computed once here beside
    # the road clearance and handed to the other passes through `shared`.
    orphans, orphan_stats = bundle_instances.orphans_of_absent_owners(ctx.bundle)
    stats["orphans"] = orphan_stats
    # The road-clearance deviation, computed ONCE here and handed to the instances stage through
    # `shared` — one spatial pass over the lane layer instead of two, and the two passes cannot
    # reach different answers because they consult the same set.
    clipped, clip_stats = placement.road_clipped(ctx.bundle, settings, region)
    stats["roadclear"] = clip_stats
    progress.begin("shapes: which meshes the city places", total=units(ctx.census, settings), unit="instances")
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
            entry = demand[(asset, suffix)] = {"kind": kind, "count": 0, "forest": False,
                                               "static": False, "variants": {}, "tree": False}
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
    progress.end(f"{stats['kept']:,} of {stats['records']:,} records over {len(demand):,} mesh states")
    return demand, stats, clipped | orphans


def _state_suffix(record, asset, mesh_dir, have):
    """The suffix of the first `mesh_candidates` entry that is on disk, or None if none is.

    `mesh_candidates` is the contract's own walk (§6.1: a variant file exists only where the
    prefab flags that state, so absence means the game has no such variant either)."""
    for candidate in bundle_instances.mesh_candidates(record):
        if candidate not in have:
            have[candidate] = os.path.isfile(os.path.join(mesh_dir, candidate + ".glb"))
        if have[candidate]:
            return candidate[len(asset):]
    return None


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
    (bundle, derived_dir, out_dir, level_name, asset, suffix, variant, variant_key, kind,
     options, aliases, tinted, is_tree) = args
    source = asset + suffix
    try:
        nodes = glb.parse_glb(os.path.join(bundle, "meshes", source + ".glb"))
    except Exception as exc:                                              # noqa: BLE001
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
    null_px = max(1.0, game_px * float(options["detail_scale"]))

    shape_name = placement.shape_key(asset, suffix, variant)
    base_mesh = dae.group_by_material(dae.merge_meshes(shape_name, parts))
    levels, lod_stats, ladder = _lod_levels(derived_dir, source, shape_name, base_mesh, null_px,
                                            aliases, tinted, level_name, options)
    # the collider comes off LOD 1 where the bake generated one — `collision.colmesh_from_lod`
    # is written for the ~25 % level, and a 20k-triangle building shell is not a collider
    colliders, collider_kind = _collider(shape_name, base_mesh,
                                         ladder[1] if len(ladder) > 1 else base_mesh, kind, options,
                                         is_tree)
    # How well the trunk search actually found a trunk. `_trunk_group` prefers a material run
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
    reach_m = lod.cull_distance(lod.mesh_min_lod(size, area),
                                options["level_of_detail"]) / max(options["detail_scale"], 1e-6)
    wants_imposter = options["imposters"] and reach_m >= options["imposter_min_metres"]
    billboard_px = null_px if wants_imposter else None
    final_null = (null_px / IMPOSTER_REACH) if wants_imposter else null_px
    shape = dae.Shape(shape_name, levels, collision=colliders,
                      billboard_px=int(max(1, round(billboard_px))) if billboard_px else None,
                      null_px=int(max(1, round(final_null))))
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
        "gameCullMetres": round(lod.cull_distance(lod.mesh_min_lod(size, area),
                                                  options["level_of_detail"]), 1),
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
        local, normals = dae.glb_to_local(world, node["normals"])
        runs = []
        for name, count in node["material_runs"]:
            bound = _bound_material(name, aliases, tinted, level_name)
            materials_used.add(aliases.get(name, name))
            runs.append((bound, count))
        parts.append((local, normals, dae.glb_uvs(node["uvs"]), node.get("colors"), node["tris"], runs))
    return parts, lo, hi, area, materials_used


def _with_wind_colours(parts):
    """Replace each part's vertex colours with BeamNG's forest wind convention
    (`collision.wind_vertex_colours`). Synthesised, cosmetic and off by default."""
    out = []
    for index, (verts, normals, uvs, _colors, tris, runs) in enumerate(parts):
        colours = collision.wind_vertex_colours({"verts": verts, "tris": tris, "material_runs": runs},
                                                up_axis=2, seed=index)
        out.append((verts, normals, uvs, colours, tris, runs))
    return out


def _bound_material(raw, aliases, tinted, level_name):
    """The material name this shape binds — the paint variant's when this shape is a variant and
    the bake wrote a tinted albedo for that material, else the canonical one."""
    canonical = aliases.get(raw, raw)
    variant_name = tinted.get(raw) or tinted.get(canonical)
    if variant_name:
        return variant_name
    from ..beamng import materials as bm
    return bm.mapto(level_name, canonical)


def _lod_levels(derived_dir, source, shape_name, base_mesh, null_px, aliases, tinted, level_name, options):
    """`[(px, [Mesh]), ...]` nearest-first: LOD 0 plus whatever the bake generated."""
    ratios = list(LADDER_RATIOS)
    meshes = [base_mesh]
    stats = {"levels": 1, "triangles": [len(base_mesh.tris)]}
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
            stats["levels"] += 1
            stats["triangles"].append(len(mesh.tris))
    # The COARSEST mesh takes the smallest threshold, whatever the ladder's length: Torque picks
    # the first detail whose size is <= the current pixel size, so a shape whose only detail sat
    # high would vanish in the gap between `nulldetail` and that detail.
    # `split_mesh` caps every <geometry> at MAX_GEOMETRY_VERTS: BeamNG's per-mesh index buffers
    # wrap past 65,535 and the shape shards in-game (load #10). A hospital reached 68,856.
    levels = [(max(2, int(round(null_px * ratio))), dae.split_mesh(mesh))
              for ratio, mesh in zip(ratios, reversed(meshes))]
    levels.sort(key=lambda pair: -pair[0])
    # the ladder must strictly decrease or Torque reads two details as one
    fixed = []
    for px, ms in levels:
        if fixed and px >= fixed[-1][0]:
            px = fixed[-1][0] - 1
        if px < 2:
            continue
        fixed.append((px, ms))
    stats["detailSizes"] = [px for px, _ms in fixed]
    stats["splitGeometries"] = sum(len(ms) for _px, ms in fixed) - len(fixed)
    return fixed, stats, meshes


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
            up_axis=2)
        return (dae.Mesh(f"Colmesh_{shape_name}", cyl_v, cyl_t, [(base_mesh.runs[0][0], len(cyl_t))]),), "trunk"
    if options["collision_preset"] == "exact" or len(tris) <= options["colmesh_max_tris"]:
        mesh = collision.colmesh_from_lod({"verts": verts, "tris": tris})
        if mesh is not None:
            return (dae.Mesh(f"Colmesh_{shape_name}", mesh["verts"], mesh["tris"],
                             [(base_mesh.runs[0][0], len(mesh["tris"]))]),), "mesh"
    lo, hi = collision.bounds(base_mesh.verts)      # the box is LOD 0's, never a decimated hull's
    box_v, box_t = collision.colbox(lo, hi)
    return (dae.Mesh(f"Colmesh_{shape_name}", box_v, box_t, [(base_mesh.runs[0][0], len(box_t))]),), "box"


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

    options = {
        "level_of_detail": lod.DEFAULT_LEVEL_OF_DETAIL,
        "detail_scale": float(settings.get("lod_detail_scale", 6.0) or 6.0),
        "generate_lods": bool(settings.get("generate_lods", True)),
        "imposters": bool(settings.get("imposters", True)),
        "imposter_min_metres": (float(settings.get("imposter_min_metres") or 0)
                                or IMPOSTER_VIEW_FRACTION * float(settings.get("view_distance") or 4000)),
        "collision_preset": str(settings.get("collision_preset") or "simplified"),
        "colmesh_max_tris": 20000,
        "tree_wind": bool(settings.get("tree_wind", False)),
        "collide": _collision_policy(settings),
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
            jobs.setdefault((asset, suffix, index_out),
                            (ctx.bundle, ctx.derived_dir, out_dir, level.name, asset, suffix,
                             index_out, key if tinted else None, entry["kind"], options, aliases,
                             tinted, entry["tree"]))
        # the UNTINTED shape: what a TSStatic binds (it keeps `colorPaletteMap` and carries its
        # own `instanceColor*`), and what a prefab with no baked variants gets
        if entry["static"] or not wanted:
            jobs.setdefault((asset, suffix, None),
                            (ctx.bundle, ctx.derived_dir, out_dir, level.name, asset, suffix,
                             None, None, entry["kind"], options, aliases, {}, entry["tree"]))
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
    progress.end(f"{len(results):,} shapes ({rewritten:,} rewritten, {len(results) - rewritten:,} unchanged), "
                 f"{orphans} orphans removed, {len(errors)} failed")

    shared = ctx.shared if ctx.shared is not None else {}
    used = shared.setdefault("materials_used", set())
    variant_used = shared.setdefault("materials_variants_used", {})
    for value in results.values():
        used.update(value["materials"])
        key = value.get("variantKey")
        if key:
            variant_used.setdefault(key, set()).update(value["materials"])
    shared["shapes"] = {"dir": SHAPES_SUBDIR, "results": results, "demand": demand,
                        "variants": variants, "droppedRecords": sorted(dropped),
                        "options": {k: v for k, v in options.items() if k != "collide"}}
    return _report(results, demand, scan_stats, options, errors, started, progress, rewritten, orphans)


def _absorb(key, value, results, errors, progress):
    progress.advance(1)
    if "error" in value:
        errors.append(value["error"])
    else:
        results[key] = value
    if (len(results) + len(errors)) % 200 == 0:
        progress.check()


def _collision_policy(settings):
    """Which record kinds get a collider — the plan's "everything physical collides by default"
    expressed against the settings the UI exposes."""
    if str(settings.get("collision_preset") or "simplified") == "off":
        return {}
    return {"building": bool(settings.get("collision_buildings", True)),
            "prop": bool(settings.get("collision_props", True)),
            "tree": bool(settings.get("collision_trees", True))}


def _material_tables(ctx, level):
    """`(aliases, {variantKey: {rawMaterial: boundName}})` from the bake's `materials.json`.

    A variant's tinted albedo is a different TEXTURE for the same material, so it needs its own
    BeamNG material; the materials stage writes it under this name when it sees it bound.
    """
    from ..beamng import materials as bm
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
        for key in (derived.get("variantFactors") or derived.get("variants") or {}):
            canonical = entry.get("canonical") or name
            by_variant.setdefault(key, {})[name] = bm.mapto(level.name, f"{canonical}__v{key}")
    return aliases, by_variant


# A trunk collider wider than this share of the shape's own canopy is not a trunk. OURS — CS2
# has no collision for a planted object at all — and it is a REPORTING threshold only: nothing
# behaves differently either side of it, it decides when the build says so out loud.
TRUNK_WIDE_RATIO = 0.5


def _report(results, demand, scan_stats, options, errors, started, progress, rewritten=0, orphans=0):
    rows_by_pair = {}
    for (asset, suffix, _variant), row in results.items():
        rows_by_pair.setdefault((asset, suffix), []).append(row)
    by_kind = {}
    for pair, entry in demand.items():
        rows = rows_by_pair.get(pair)
        if not rows:
            continue
        bucket = by_kind.setdefault(entry["kind"], {"shapes": 0, "instances": 0, "gameCull": [], "ourCull": []})
        bucket["shapes"] += len(rows)
        bucket["instances"] += entry["count"]
        for row in rows:
            bucket["gameCull"].append(row["gameCullMetres"])
            # distance scales inversely with the pixel threshold, so ours is the game's distance
            # times (game px / shipped px). `nullPx` is what the level actually stops drawing at,
            # imposter reach included.
            bucket["ourCull"].append(row["gameCullMetres"] * row["gamePx"] / max(row["nullPx"], 1e-6))
    summary = {}
    for kind, bucket in by_kind.items():
        game = sorted(bucket["gameCull"])
        ours = sorted(bucket["ourCull"])
        summary[kind] = {
            "shapes": bucket["shapes"], "instances": bucket["instances"],
            "gameCullMedian": round(game[len(game) // 2], 1),
            "ourCullMedian": round(ours[len(ours) // 2], 1),
        }
        progress.log(f"shapes: {kind} - {bucket['shapes']} shapes, CS2 draws the median one to "
                     f"{summary[kind]['gameCullMedian']:,.0f} m, this level to "
                     f"{summary[kind]['ourCullMedian']:,.0f} m")
    colliders = {}
    for row in results.values():
        colliders[row["collider"]] = colliders.get(row["collider"], 0) + 1
    # trunk cylinders that are really canopy cylinders — see `trunkRatio` in `_shape_job`
    trunk_ratios = [r["trunkRatio"] for r in results.values() if r.get("trunkRatio") is not None]
    trunk_wide = sum(1 for r in trunk_ratios if r > TRUNK_WIDE_RATIO)
    return {
        "shapes": len(results),
        "shapesRewritten": rewritten,
        "orphansRemoved": orphans,
        "failed": len(errors),
        "errors": errors[:20],
        "scan": scan_stats,
        "triangles": sum(r["triangles"] for r in results.values()),
        "colliders": colliders,
        "trunkFit": {"shapes": len(trunk_ratios), "wide": trunk_wide,
                     "medianRatio": (round(sorted(trunk_ratios)[len(trunk_ratios) // 2], 3)
                                     if trunk_ratios else None),
                     "ratio": TRUNK_WIDE_RATIO},
        "imposters": {"shapes": sum(1 for r in results.values() if r.get("billboardPx")),
                      "ofShapes": len(results),
                      "minMetres": round(options["imposter_min_metres"], 1),
                      "vanillaShare": "west_coast_usa 3.1 %, italy 7.2 %, johnson_valley 4.4 %"},
        "detailScale": options["detail_scale"],
        "levelOfDetail": options["level_of_detail"],
        "drawDistance": summary,
        "approximations": {
            "lodDetailScale": {
                "what": "every shape's cull threshold is CS2's own, multiplied by lod_detail_scale",
                "why": "CS2 draws a median prop to 1 km and a median building to 7.3 km over 1.2M "
                       "objects; BeamNG cannot. 1.0 restores the game exactly.",
                "count": len(results), "value": options["detail_scale"],
            },
            "lodLadderSwitchPoints": {
                "what": "where LOD1/LOD2/the imposter take over",
                "why": "CS2 ships no LOD chain for these prefabs (lodCap 0), so there is no game "
                       "rule to port; the ratios are vanilla BeamNG's spacing",
                "count": sum(1 for r in results.values() if r["lods"]["levels"] > 1),
            },
            "treeTrunkCylinderFit": {
                "what": "a tree whose trunk collider is wider than "
                        f"{TRUNK_WIDE_RATIO:.0%} of its own canopy - the trunk search found the "
                        "canopy instead",
                "why": "CS2 has no collision for a planted object, so there is no rule to port. "
                       "`collision._trunk_group` prefers a material run NAMED trunk/bark/stem, "
                       "and on a CS2 bundle that branch never fires - the game's material names "
                       "are hashed asset ids - so every tree falls through to the geometric "
                       "fallback (lowest band, smallest footprint). That is right for a clean "
                       "bole and wrong where the lowest band is not the trunk: a spruce whose "
                       "branches sweep the ground, or a wide shrub CS2 counts as a tree because "
                       "it carries growth. MEASURED on the reference city: 5 of 32 tree prefabs, "
                       "4.5 % of tree items",
                "count": trunk_wide,
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
INPUTS = ("layer:instances", "layer:meshes", "derived:lods", "derived:materials.json",
          "derived:paint_variants.json")
SETTINGS = ("trees", "props", "buildings", "foliage_density", "prop_density", "region",
            "foliage_road_clearance",
            "generate_lods", "imposters", "imposter_min_metres", "view_distance",
            "lod_detail_scale", "tree_wind",
            "collision_preset", "collision_buildings", "collision_props", "collision_trees")
OUTPUTS = ("art/shapes/cs2",)

# version 4: orphans of absent owners are not placed (A6.6)
# version 3: the road-clearance deviation (A6.4)
# version 2: the tree/plant split (A6.1) - a plant gets no collider and no trunk cylinder, and
# every shape row carries `tree` and `trunkRatio`
STAGE = Stage(STAGE_KEY, "Object shapes", enabled, units, run, inputs=INPUTS, settings_keys=SETTINGS,
              outputs=OUTPUTS,
              shared_keys=("shapes", "materials_used", "materials_variants_used"), version=4)
