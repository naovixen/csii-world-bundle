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
        # trees: where the billboard takes over rides on this (the generated ladder's sizes are
        # multiples of the null); the game's own tree cull is ~1 km and a full canopy to 580 m
        # is 16 M triangles in view - 10 puts the billboard at the 100-200 m Naomi saw
        "detail_scale_tree": float(settings.get("lod_detail_scale_tree") or 0.0),
        # big buildings' own scale (see `shapelods.detail_scale`); 0 = the buildings' scale
        "detail_scale_large": float(settings.get("lod_detail_scale_large") or 0.0),
        "large_radius_m": float(settings.get("lod_large_radius_m") or 30.0),
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
        # residency: ship the bake's refined copy of a level where it wrote one
        # (`shapelods.refined_level`). The bake made the decision and the measurement.
        "lod0_refine": bool(settings.get("lod0_refine", True)),
        # ...and refuse the `authored_lod0_max_tris` drop where the game's own LOD 1 is measurably
        # this far from LOD 0 - the shapes that "look awful up close" (`authored_chain`)
        "authored_lod0_max_error": float(settings.get("authored_lod0_max_error", 0.25) or 0.0),
        # the far silhouette level: CS2's own cull times this; 0 = none (`shapewrite._far_level`)
        "far_detail_scale": float(settings.get("far_detail_scale", 1.0) or 0.0),
        # residency: hand the authored LOD 1's band to LOD 2 and do not ship LOD 1
        "authored_lod1_drop": bool(settings.get("authored_lod1_drop", False)),
        # residency: a shape whose LOD 0 is at or over this ships the game's LOD 1 as its nearest
        # level; 0 = off (`shapelods.authored_chain`)
        "authored_lod0_max_tris": int(settings.get("authored_lod0_max_tris") or 0),
        # the far ring's hand-over distance (`stages/farring`); 0 = per-building silhouettes only
        "far_ring_metres": float(settings.get("far_ring_metres") or 0.0),
        "collide": _collision_policy(settings),
        # A prefab CS2 gives a photometric light to is a LAMP, and `collision_lights` says
        # whether one is solid. The set is the game's own answer to "is this a lamp" - see the
        # setting's help - and it is empty when lamps are allowed to collide.
        "no_collide": _light_bearing(ctx.bundle)
        if not settings.get("collision_lights")
        else frozenset(),
    }

    ring_dir = os.path.join(ctx.level_dir, "cs2beamng_state", "farring")
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
                    chain_displacement=(lod_index.get(asset + suffix) or {}).get(
                        "chainDisplacement"
                    ),
                    lod_backend=_lod_backend(lod_index, asset, suffix),
                    ring_dir=ring_dir,
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
                    chain_displacement=(lod_index.get(asset + suffix) or {}).get(
                        "chainDisplacement"
                    ),
                    lod_backend=_lod_backend(lod_index, asset, suffix),
                    ring_dir=ring_dir,
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
    _sweep_ring_dir(ring_dir, results)
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


def _sweep_ring_dir(ring_dir, results):
    """Remove silhouette `.npz` files no shape of this build wrote (D1)."""
    if not os.path.isdir(ring_dir):
        return
    keep = {((v.get("lods") or {}).get("farRing") or {}).get("path") for v in results.values()}
    for name in os.listdir(ring_dir):
        if name not in keep:
            try:
                os.remove(os.path.join(ring_dir, name))
            except OSError:
                pass


def _lod_backend(lod_index, asset, suffix):
    """The generated levels (1-based) the MeshLab backend shaped for this mesh state - the
    only generated levels a BUILDING may take (`shapelods.lod_levels`, §8.15).

    PER LEVEL, from the index's `backends` list (one winner per level). This used to test
    `"meshlab" in str(backends)`, so one MeshLab level admitted BOTH levels - a cluster lattice
    on the other one - and a multi-node building whose railings went through `cluster` still
    read as MeshLab by triangle plurality (validation pass 2026-08-31, B4). `lod_audit` R4
    reads the same list the same way."""
    entry = lod_index.get(asset + suffix) or {}
    backends = entry.get("backends")
    if backends is None:  # older bakes: one `backend` string for every level
        backends = [entry.get("backend")] * 2
    if isinstance(backends, str):
        backends = [backends] * 2
    return tuple(i + 1 for i, b in enumerate(backends) if b == "meshlab")


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
    "lod_detail_scale_tree",
    "lod_detail_scale_large",
    "lod_large_radius_m",
    "lod_switch_error_px",
    "lod0_max_error",
    "lod0_min_triangles",
    "lod0_refine",
    "authored_lod0_max_error",
    "far_detail_scale",
    "authored_lod1_drop",
    "authored_lod0_max_tris",
    "far_ring_metres",
    "tree_wind",
    "collision_preset",
    "collision_buildings",
    "collision_props",
    "collision_trees",
    "collision_lights",
)
OUTPUTS = ("art/shapes/cs2",)

# version 44: `lod0_refine` - the bake's refined copy of a level (`_LOD<n>R.glb`) ships in place
# of that level on buildings and props wherever the bake MEASURED that the collapse left the
# surface inside the tolerance (19-30 % of the triangles at a p95 of 0.000-0.005 m). And
# `authored_lod0_max_error`: the `authored_lod0_max_tris` drop is REFUSED where the game's own
# LOD 1 is measurably far from LOD 0 - the ratio guard only saw badly reduced, never moderately
# reduced and geometrically far, which is what looked awful up close.
# version 43: a runtime billboard is for trees only (`_imposter_thresholds(kind=)`, C3); a
# generated level ships no vertex-colour stream (C4)
# version 42: a building takes a generated level only where THAT level's bake backend is
# MeshLab (`_lod_backend` per level), not both levels when either was (B4)
# version 41: the coarsest generated level sits ONE pixel over the null, not 1.5x it - the
# shape's stop is the coarsest mesh's size and the report reads `stopPx` (A5); a shape that is
# thin AND wider than `BOX_COLLIDER_MAX_METRES` ships no collider instead of a padded slab (A4)
# version 40: a generated level identical to the one above it is left out (the authored
# chain's identical-level rule, on the bake's output - 182 shapes); `tree_billboard_px`
# removed (inert since v34: every route to a larger imposter cell failed in-game, §8.14)
# version 39: the billboard's size is its FAR STOP - `bb = null + 1` (trees drew nothing from
# 99 m to 601 m with the card at the takeover size, §8.16, audit R8); `authored_lod0_max_tris`
# keeps LOD 0 where the authored LOD 1 is under `DEGENERATE_LOD1_RATIO` of it (the stadium)
# version 38: ...reading the index's `backends` (per-level winners), which v37 misread as `backend`
# version 37: a building whose baked levels the MESHLAB backend won takes them - the texture-aware
# collapse the no-chain landmarks needed; every other building still refuses generated levels
# version 36: asset.json imposters OFF for good - orange with vanilla's own parameters too; the
# dae bb node (engine-default bake) is the billboard
# version 35: the asset.json imposter with VANILLA'S tree parameters (equatorSteps 16, no poles) -
# 611 vanilla shapes ship this route; orange came with our 8-views-plus-poles variant
# version 34: the asset.json imposter REVERTED - it sizes the atlas but the bake renders orange
# (no material found in the imposter pass); the dae bb node is the billboard again
# version 33: the imposter config is written as `<shape>.dae.asset.json` directly - the format the
# engine converts the .cs into anyway (watched live: 'Removed obsolete file ... replaced by')
# version 32: the tree billboard's atlas cell rides a TSShapeConstructor .cs (`addImposter`),
# the mechanism vanilla GridMap uses; the dae's bb node is dropped for those trees
# version 31: `lod_detail_scale_large` / `lod_large_radius_m` (big buildings' own scale) and
# `tree_billboard_px` (the billboard atlas cell, was Torque's 64)
# version 30: chain switch points scaled by ONE uniform `lod_detail_scale` again (unscaled: 23.5 M
# building triangles in 4 km); the level shown at the hand-over is the nearest one under it
# version 29: the coarsest level under the hand-over covers the band to it (not dropped) - LOD 0
# to 2.5 km was 32 M triangles in view
# version 28: a 1 px coarsest level with no null is allowed (sheds under 4 m reach the ring)
# version 27: a ring member's coarsest level takes the hand-over's SIZE (a level draws for p >= its
# size; the null under it), so it reaches the ring instead of vanishing at its own limit
# version 26: the coarsest mesh keeps its size and the null comes down under it (lifting the mesh
# left a band where the null was chosen)
# version 25: a ring member's null rounds DOWN (no gap before the ring) and it ships no billboard;
# the ladder stats are taken from the final levels
# version 24: every detail strictly above the null and the billboard between them (`_above_null`)
# version 23: a ring member's levels under the hand-over null are dropped (Torque picks by size);
# `lod_detail_scale_tree`
# version 22: every building is a ring member (towers too); a tree's generated levels are loaded
# for their switch sizes only, the billboard replaces them all
# version 21: generated levels for PROPS only (buildings: lattices; trees: bare canopies); a tree
# is LOD 0 then the billboard, as the game's own trees are; chain switch points UNSCALED
# version 20: a far-ring member keeps its coarsest level out to the hand-over and ships NO box of
# its own (the box up close on small houses was the band from its detail's end to the ring)
# version 19: `far_ring_metres` - a building's far level stops at the hand-over and its silhouette
# goes to the ring (`stages/farring`); towers reaching past it keep their own level
# version 18: the identical-level digest is order-independent (a copied level binds its own
# material names, so `group_by_material` reorders it; v17 caught none of the 12)
# version 17: `authored_lod0_max_tris` - a shape whose LOD 0 is at or over it ships the game's LOD 1
# as its nearest level (residency: LOD 0 is ~90 % of object bytes once LOD 1 is dropped); and an
# authored level byte-identical to LOD 0 is left out (Kinsman_Plaza carried its 176,587 triangles twice)
# version 16: `authored_lod1_drop` hands the authored LOD 1's band to LOD 0, not LOD 2
# version 15: `authored_lod1_drop` hands the authored LOD 1's band to LOD 2 (residency)
# version 14: the far silhouette level (`far_detail_scale`), and no billboard on a chain shape
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
    version=44,
)
