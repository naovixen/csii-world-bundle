"""Stage `instances` — the 1.2 million placed objects: forest items and `TSStatic`s.

The `shapes` stage already decided what each `(assetId, state, paint variant)` looks like; this
one streams `instances.jsonl` a second time and puts each record where it belongs. The two
passes reach the same admission decision because both ask `beamng.placement.keep`, which is
pure — no list of a million decisions is carried between them.

## Frames

A record's world matrix is the contract's `T(pos) . R(rot) . S(scale) . diag(-1,1,1)` acting on
glTF vertices (§2.2). The `.dae` already holds `P . diag(-1,1,1)` baked into its vertices
(`daemesh.glb_to_local`), so what the object needs is the same transform conjugated into BeamNG
axes and with the mesh basis divided out:

    M_beamng = P . T . R . S . P^-1

which is a swap of the Y and Z rows and columns — the rotation stays a rotation, the scale
becomes `(sx, sz, sy)`, and nothing is mirrored twice. `csiibundle.frames` states the identity;
`_transform` is the three-line form of it.

## Forest vs TSStatic

`beamng.placement` holds the rule and why it is ours rather than the game's. A forest item is
one line of `forest/<type>.forest4.json`; a `TSStatic` is one object in the level tree, grouped
by kilometre so BeamNG's editor and its culling both have something to work with. Only the
`TSStatic` path can carry `instanceColor`/`1`/`2` — CS2's three `colorSet` channels — which is
why the forest path takes the bake's quantised paint variants instead.
"""

import json
import os
import time

from csiibundle import instances as bundle_instances
from csiibundle import tints

from ..beamng import frame, placement
from ..beamng import level as level_mod
from . import Stage, level_of
from . import farring
from .instanceforest import (
    drop_rare_types,
    ANNOTATION,
    FOREST_COLLIDABLE_MIN_FOOTPRINT,
    FOREST_COLLIDABLE_MIN_HEIGHT,
    NO_COLLIDER,
    clear_forest,
    flush,
    forest_bytes,
    merge_thin_types,
    write_item_data,
)

STAGE_KEY = "instances"

# The `collisionType` an object with a `Colmesh_` node takes. BeamNG's TSStatic documentation
# ranks the options and this is the one it recommends; the derived/network tiles deliberately keep
# `Visible Mesh Final` because they have no colmesh and the visible road surface IS the collision
# surface there.
COLLISION_TYPE = "Collision Mesh"


# SimGroup granularity for TSStatics, in metres. Vanilla levels group by district; a kilometre
# keeps each `items.level.json` a few hundred lines on a city map. OURS.


# Decimal places on a forest item's `rotationMatrix`. 4 dp is 1e-4 of a unit vector = 0.6 mm of
# arc at 10 m, well under a texel of anything, and it is ~20 % off a 188 MiB file that BeamNG
# parses line by line at load (vanilla's largest forest is 151 MiB).
ROTATION_DP = 4

# Forest lines held in memory before a flush. 1.2M items at ~160 bytes is ~190 MB, so this only
# matters on a machine near its limit; the files are appended to, so a flush is not a boundary.
FLUSH_EVERY = 400000


def enabled(settings):
    return any(settings.get(k, True) for k in ("trees", "props", "buildings"))


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("instances") or 0) or None


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    shared = ctx.shared if ctx.shared is not None else {}
    shapes = shared.get("shapes") or {}
    results = shapes.get("results") or {}
    variants = shapes.get("variants") or {}
    if not results:
        progress.log("instances: the shapes stage wrote nothing, so there is nothing to place")
        return {"placed": 0, "seconds": round(time.monotonic() - started, 1)}

    mesh_dir = os.path.join(ctx.bundle, "meshes")
    have, suffix_cache = {}, {}
    forest_dir = level.path("forest")
    clear_forest(forest_dir)
    lines = {}  # type -> [json line, ...]
    pending = 0
    types = {}  # type -> the shape row behind it
    type_source = {}  # type -> (asset, suffix, variant)
    ring_m = float(settings.get("far_ring_metres") or 0.0)
    ring = (
        farring.Ring(ring_m, float(settings.get("far_detail_scale") or 1.0)) if ring_m > 0 else None
    )
    if ring is None:
        # a level built with a ring before and without one now keeps no cells
        farring.dae.sweep_orphans(level.path("art", "shapes", farring.PRODUCT), set())
    statics = 0
    counts = {"forest": 0, "tsstatic": 0, "noShape": 0, "kept": 0, "records": 0}
    untinted_forest = 0
    item_counts = {}
    collision_types = {}
    collide = _collision_policy(settings)
    # The road-clearance deviation, as the shapes stage computed it. Read from `shared` rather
    # than recomputed so the two passes cannot disagree — and a SKIPPED shapes stage replays its
    # shared delta, so this is present either way.
    dropped = set((ctx.shared.get("shapes") or {}).get("droppedRecords") or ())

    progress.begin("instances: placing", total=units(ctx.census, settings), unit="instances")
    for record in bundle_instances.read_instances(ctx.bundle):
        counts["records"] += 1
        if counts["records"] % 25000 == 0:
            progress.advance(25000)
            progress.check()
        if not placement.keep(record, settings, ctx.region, dropped):
            continue
        counts["kept"] += 1
        asset = record["assetId"]
        suffix = _state_suffix(record, asset, mesh_dir, have, suffix_cache)
        if suffix is None:
            counts["noShape"] += 1
            continue
        kind = record.get("kind")
        forest = placement.wants_forest(record)
        variant = None
        if forest:
            centres = (variants.get(asset) or {}).get("variants") or []
            variant = placement.variant_of(record, centres) if centres else None
        # `variant=None` is the untinted shape, which is also the fall-back when the bake wrote
        # no tinted albedo for this prefab's kind (`paint_kinds`)
        row = results.get((asset, suffix, variant))
        if row is None:
            row = results.get((asset, suffix, None))
            if forest and variant is not None and row is not None:
                untinted_forest += 1
        if row is None:
            counts["noShape"] += 1
            continue
        name = os.path.splitext(row["file"])[0]
        position, rotation, scale = frame.record_transform(record)
        if forest:
            types.setdefault(name, (row, kind))
            type_source.setdefault(name, (asset, suffix, variant))
            item_counts[name] = item_counts.get(name, 0) + 1
            lines.setdefault(name, []).append(_forest_line(name, position, rotation, scale))
            pending += 1
            counts["forest"] += 1
            if pending >= FLUSH_EVERY:
                flush(forest_dir, lines)
                pending = 0
        else:
            key = _add_static(
                level,
                record,
                name,
                row,
                kind,
                position,
                rotation,
                scale,
                collide,
                statics,
                shadow_min_radius=float(settings.get("shadow_min_radius") or 0.0),
            )
            if key.endswith(":noShadow"):
                key = key[: -len(":noShadow")]
                counts["noShadow"] = counts.get("noShadow", 0) + 1
            ring_data = ((row.get("lods") or {}).get("farRing")) if ring is not None else None
            if ring_data:
                ring.add(ring_data, position, rotation, scale)
            collision_types[key] = collision_types.get(key, 0) + 1
            statics += 1
            counts["tsstatic"] += 1
    flush(forest_dir, lines)
    if ring is not None:
        counts["farRing"] = ring.write(level, settings)
        progress.log(
            f"instances: far ring - {counts['farRing']['members']:,} silhouettes in "
            f"{len(counts['farRing']['cells'])} cells from {ring.d:.0f} m ({counts['farRing']['triangles']:,} triangles)"
        )
    merged = merge_thin_types(
        forest_dir,
        types,
        type_source,
        item_counts,
        results,
        int(settings.get("forest_min_items") or 0),
    )
    rare = drop_rare_types(
        forest_dir,
        types,
        type_source,
        item_counts,
        int(settings.get("forest_rare_type_min") or 0),
        results=results,
        keep_radius=float(settings.get("forest_rare_keep_radius") or 0.0),
    )
    counts["forest"] -= rare["items"]
    progress.end(
        f"{counts['forest']:,} forest items in {len(types):,} types "
        f"({merged['types']} thin types holding {merged['items']} items merged into their "
        f"untinted twin; {rare['types']} rare types holding {rare['items']} items dropped, "
        f"{rare['keptLarge']['types']} kept for size), {counts['tsstatic']:,} TSStatics"
    )

    forest_stats = {}
    if types:
        level.add(["Vegetation"], {"name": "theForest", "class": "Forest"}, key="forest:theForest")
        for name, (row, _kind) in types.items():
            row["_items"] = item_counts.get(name, 0)  # for the collision-hygiene count below
        forest_stats = write_item_data(level, types, settings, collide)
    level.write_objects()

    report = {
        "forestItems": counts["forest"],
        "forestTypes": len(types),
        "tsStatics": counts["tsstatic"],
        "tsStaticsNoShadow": counts.get("noShadow", 0),
        "farRing": counts.get("farRing"),
        "forestTypesMerged": merged["types"],
        "forestItemsMerged": merged["items"],
        "forestMinItems": int(settings.get("forest_min_items") or 0),
        "collisionTypes": collision_types,
        "forestCollision": forest_stats,
        "forestBytes": forest_bytes(forest_dir),
        "placed": counts["forest"] + counts["tsstatic"],
        "scanned": counts["records"],
        "kept": counts["kept"],
        "noShape": counts["noShape"],
        "seconds": round(time.monotonic() - started, 1),
        "approximations": {
            "forestPaintVariants": {
                "what": "a forest item is drawn in its prefab's nearest baked colorSet centre, "
                "not its own colour (a TSStatic gets instanceColor/1/2 exactly)",
                "why": "a BeamNG forest item carries no per-item colour; the bake clusters each "
                "prefab's sets and tints one albedo per centre",
                "count": counts["forest"],
            },
            "forestVariantWithoutTintedAlbedo": {
                "what": "forest items whose variant had no tinted albedo baked, so they fell "
                "back to the untinted material",
                "why": "the bake tints only `paint_kinds` (default trees); widen it to cover props",
                "count": untinted_forest,
            },
            "forestPlantsDoNotCollide": {
                "what": "a forest type built from a CS2 `PlantObject` (bush, hedge, flower pot, "
                "garden bed, long grass) ships collidable:false whatever its height",
                "why": "the `tree` kind is ALL vegetation - 376,187 of the reference city's "
                "686,641 planted items are plants, not trees. CS2's own archetypes split "
                "them: `TreeObject` adds `Tree`, `PlantObject` does not, and the producer "
                "writes `attrs.growth` inside exactly that component's lookup, so this is "
                "the GAME's rule (csiibundle.instances.is_tree), not a height guess. A "
                "plant is driven through, and it has no trunk to fit a cylinder to",
                "count": forest_stats.get("plant", 0),
            },
            "forestCollidableThreshold": {
                "what": f"a forest type whose shape is under {FOREST_COLLIDABLE_MIN_HEIGHT} m tall "
                f"ships collidable:false",
                "why": "CS2 states nothing about collision for a placed object, so there is no rule "
                "to port; BeamNG's own guidance is to disable it for small decorative items, "
                "and this height sits under a passenger wheel radius",
                "count": forest_stats.get("tooShort", 0),
                "value": FOREST_COLLIDABLE_MIN_HEIGHT,
                "items": forest_stats.get("itemsNotCollidable", 0),
            },
            "forestFootprintThreshold": {
                "what": f"a forest type whose shape is under "
                f"{settings.get('collision_min_footprint', FOREST_COLLIDABLE_MIN_FOOTPRINT)} m "
                f"ACROSS ships collidable:false whatever its height - road signs, "
                f"hydrants, bins, mailbox stands, bollards, traffic lights",
                "why": "height alone let through the very things it was meant to catch (a hydrant "
                "is 0.97 m tall, a road sign 2.2 m); what decides whether a car should stop "
                "is the FOOTPRINT. OURS - CS2 states nothing about collision for a placed "
                "object. KNOWN WRONG for bollards, which are 0.24 m across and exist to "
                "stop vehicles; exempting them would mean matching a name",
                "count": forest_stats.get("tooThin", 0),
                "value": float(
                    settings.get("collision_min_footprint", FOREST_COLLIDABLE_MIN_FOOTPRINT) or 0.0
                ),
            },
            "forestRotationPrecision": {
                "what": f"a forest item's rotationMatrix is written at {ROTATION_DP} decimal places",
                "why": "1e-4 of a unit vector is 0.6 mm of arc at 10 m; the file is parsed line by "
                "line at load and vanilla's largest forest is 151 MiB",
                "count": counts["forest"],
            },
            "forestWindConstants": {
                "what": "windScale / rigidity / mass on every TSForestItemData",
                "why": "CS2 states nothing about wind or physics for a placed object; the values "
                "are the vanilla levels' medians, not a port",
                "count": len(types),
            },
        },
    }
    progress.log(
        f"instances: {report['placed']:,} objects placed "
        f"({report['forestItems']:,} forest / {report['tsStatics']:,} TSStatic)"
    )
    if untinted_forest:
        progress.log(
            f"instances: {untinted_forest:,} forest items drew an untinted albedo because "
            f"their kind is outside the bake's paint_kinds"
        )
    return report


# -- one record ------------------------------------------------------------------------------------


def _state_suffix(record, asset, mesh_dir, have, cache):
    """The state suffix for this record, memoised on what actually varies it.

    Two caches, doing different jobs: `have` is one file probe per distinct candidate NAME (owned
    by `first_mesh_state`), and `cache` is one candidate WALK per distinct
    `(asset, treeState, fullness)` — the only three fields `mesh_candidates` reads, so records
    agreeing on them agree on the answer. A million records collapse to a few thousand walks.
    """
    key = (
        asset,
        (record.get("attrs") or {}).get("treeState"),
        (record.get("attrs") or {}).get("fullness"),
    )
    if key in cache:
        return cache[key]
    candidate = bundle_instances.first_mesh_state(record, mesh_dir, have)
    answer = None if candidate is None else candidate[len(asset) :]
    cache[key] = answer
    return answer


def _forest_line(type_name, position, rotation, scale):
    """One `.forest4.json` line. A forest item carries ONE scalar scale, so a record that needed
    three would not have reached here (`placement.wants_forest`).

    Formatted directly rather than through `json.dumps` — the same bytes (`repr` of a float IS
    the JSON float encoding, and the type name is JSON-quoted once), a million times per build.
    """
    flat = frame.basis_flat(rotation)
    pos = ",".join(repr(round(v, 4)) for v in position)
    rot = ",".join(repr(round(v, ROTATION_DP)) for v in flat)
    return (
        f'{{"ctxid":0,"pos":[{pos}],"rotationMatrix":[{rot}],'
        f'"scale":{round(float(sum(scale) / 3.0), 5)!r},"type":{json.dumps(type_name)}}}'
    )


def _add_static(
    level,
    record,
    name,
    row,
    kind,
    position,
    rotation,
    scale,
    collide,
    ordinal,
    shadow_min_radius=0.0,
):
    """One `TSStatic`, with the record's own three `colorSet` channels as `instanceColor*`.

    `ordinal` disambiguates the persistentId: a record `id` is unique per LOGICAL object and
    section records of one stacked instance deliberately share it (§4.1), so `id` alone would
    collide on a stack.
    """
    # `Collision Mesh` uses the `Colmesh_<name>` node the shapes stage already writes into every
    # collidable shape (a trunk cylinder, the decimated LOD1 hull, or a padded box). BeamNG's own
    # docs: "Collision Mesh - uses dedicated collision meshes from the shape. **Recommended**";
    # "Visible Mesh Final - uses rendered mesh polygons intentionally. Use only when required",
    # and "expensive". A3 shipped 14,658 statics on the expensive one while carrying the cheap
    # one in the file unused.
    colours = tints.color_set(record.get("attrs") or {})
    obj = level_mod.tsstatic(
        f"{name}_{record['id']}_{ordinal}",
        level.game_path("art", "shapes", "cs2", row["file"]),
        position,
        frame.basis_flat(rotation),
        scale=scale,
        collision="None" if row.get("collider", "none") in NO_COLLIDER else COLLISION_TYPE,
        instanced=bool(colours),  # per-instance render data only where there is a colour to carry
        annotation=ANNOTATION.get(kind, "OBSTACLES"),
    )
    for index, colour in enumerate(colours[:3]):
        key = "instanceColor" if index == 0 else f"instanceColor{index}"
        obj[key] = [round(float(c), 6) for c in colour]
    # a shape under `shadow_min_radius` casts no shadow — CS2's per-level shadow LOD stops small
    # things' shadows before their meshes, and BeamNG has only the switch
    no_shadow = bool(shadow_min_radius > 0.0 and float(row.get("radius") or 0.0) < shadow_min_radius)
    if no_shadow:
        obj["castShadows"] = False
    group = level_mod.cell_of(position)
    level.add(["Objects", group], obj, key=f"instance:{record['id']}:{name}:{ordinal}")
    return obj["collisionType"] + (":noShadow" if no_shadow else "")




def _collision_policy(settings):
    if str(settings.get("collision_preset") or "simplified") == "off":
        return {}
    return {
        "building": bool(settings.get("collision_buildings", True)),
        "prop": bool(settings.get("collision_props", True)),
        "tree": bool(settings.get("collision_trees", True)),
    }


# version 19: the rare-type floor is back as DEFAULT-OFF (the middle ground: at 3 only the one-
# and-two-item tail goes, landmarks kept by radius) - v17 removed it outright
# version 18: the ring cell's detail sizes the right way round (ring = its cull, near = the hand-over)
# version 17: the rare-type drop (`forest_rare_type_min`) is GONE - it left half-built scenes
# (a prop family's rarer members missing from every instance of it); every placed type ships
INPUTS = ("layer:instances", "layer:meshes", "derived:paint_variants.json")
SETTINGS = (
    "trees",
    "props",
    "buildings",
    "foliage_density",
    "prop_density",
    "region",
    "collision_preset",
    "collision_buildings",
    "collision_props",
    "collision_trees",
    "collision_min_footprint",
    "collision_lights",
    "forest_shadows",
    "forest_min_items",
    "forest_rare_type_min",
    "forest_rare_keep_radius",
    "shadow_min_radius",
    "foliage_road_clearance",
    "far_ring_metres",
    "far_detail_scale",
)
OUTPUTS = ("forest", "art/shapes/farring")

# version 16: the far ring - merged silhouette cells from `far_ring_metres` (`stages/farring`)
STAGE = Stage(
    STAGE_KEY,
    "Trees, props and buildings",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    after=("shapes",),
    version=19,
)
