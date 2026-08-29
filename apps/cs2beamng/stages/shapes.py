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

import os
import time

from csiibundle import lod, parallel, tints
from csiibundle.bake import lods as bake_lods

from ..beamng import dae
from . import Stage, level_of, shapedemand, shapematerials, shapereport, shapewrite
from .shapedemand import units
from .shapelods import (
    DEFAULT_SWITCH_ERROR_PX,
    IMPOSTER_VIEW_FRACTION,
    LOD0_MAX_ERROR_DEFAULT,
    LOD0_MIN_TRIANGLES,
)

STAGE_KEY = "shapes"
SHAPES_SUBDIR = "cs2"


def enabled(settings):
    return any(settings.get(k, True) for k in ("trees", "props", "buildings"))


# -- the stage -----------------------------------------------------------------------------------


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    demand, scan_stats, dropped = shapedemand.scan_demand(ctx, progress)

    variants = shapedemand.paint_variants(ctx)
    aliases, tinted_by_variant, bound_key = shapematerials.material_tables(ctx, level)
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
        # A collider over this is clustered down to it (`shapecollider`). MEASURED on the
        # reference city's LOD 1 hulls: median 58, p90 2,257, max 59,389 — so the cap touches
        # about one shape in ten and leaves the small ones exact.
        "colmesh_max_tris": 2000,
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
                shapewrite.ShapeJob(
                    bundle=ctx.bundle,
                    derived_dir=ctx.derived_dir,
                    out_dir=out_dir,
                    level_name=level.name,
                    asset=asset,
                    suffix=suffix,
                    variant=index_out,
                    variant_key=key if tinted else None,
                    kind=entry["kind"],
                    options=options,
                    aliases=aliases,
                    tinted=tinted,
                    is_tree=entry["tree"],
                    displacement=_displacement(lod_index, asset, suffix),
                ),
            )
        # the UNTINTED shape: what a TSStatic binds (it keeps `colorPaletteMap` and carries its
        # own `instanceColor*`), and what a prefab with no baked variants gets
        if entry["static"] or not wanted:
            jobs.setdefault(
                (asset, suffix, None),
                shapewrite.ShapeJob(
                    bundle=ctx.bundle,
                    derived_dir=ctx.derived_dir,
                    out_dir=out_dir,
                    level_name=level.name,
                    asset=asset,
                    suffix=suffix,
                    variant=None,
                    variant_key=None,
                    kind=entry["kind"],
                    options=options,
                    aliases=aliases,
                    tinted={},
                    is_tree=entry["tree"],
                    displacement=_displacement(lod_index, asset, suffix),
                ),
            )
    jobs = list(jobs.values())

    progress.begin("shapes: writing .dae", total=len(jobs), unit="shapes")
    results, errors = {}, []
    workers = ctx.jobs or 1
    # Unordered: `_absorb` keys results by shape, so nothing depends on the order they arrive.
    for key, value in parallel.each(
        shapewrite.shape_job,
        jobs,
        workers,
        progress,
        chunksize=8,
        min_parallel=8,
        spawn=True,
        advance=False,
    ):
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
    material_level = shared.setdefault("materials_lod_level", {})
    for value in results.values():
        used.update(value["materials"])
        # a material's level is the NEAREST any shape shows it at: bound by one shape's LOD 0,
        # it is a LOD 0 material whatever another shape's chain does with it
        levels = value.get("materialLevels") or {}
        for name in value["materials"]:
            level = int(levels.get(name, 0))
            material_level[name] = min(material_level.get(name, level), level)
        key = value.get("variantKey")
        if key:
            # recorded under the key the shape BOUND (`_material_tables`), which for a variant
            # identical to an earlier one is that earlier key
            for canonical in value["materials"]:
                variant_used.setdefault(bound_key.get((canonical, key), key), set()).add(canonical)
    shared["shapes"] = {
        "dir": SHAPES_SUBDIR,
        "results": results,
        "demand": demand,
        "variants": variants,
        "droppedRecords": sorted(dropped),
        "options": {k: v for k, v in options.items() if k != "collide"},
    }
    return shapereport.report(
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
    shared_keys=("shapes", "materials_used", "materials_variants_used", "materials_lod_level"),
    version=13,
)
