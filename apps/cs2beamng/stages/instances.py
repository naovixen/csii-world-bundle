"""Stage `instances` — the 1.2 million placed objects: forest items and `TSStatic`s.

The `shapes` stage already decided what each `(assetId, state, paint variant)` looks like; this
one streams `instances.jsonl` a second time and puts each record where it belongs. The two
passes reach the same admission decision because both ask `beamng.placement.keep`, which is
pure — no list of a million decisions is carried between them.

## Frames

A record's world matrix is the contract's `T(pos) . R(rot) . S(scale) . diag(-1,1,1)` acting on
glTF vertices (§2.2). The `.dae` already holds `P . diag(-1,1,1)` baked into its vertices
(`dae.glb_to_local`), so what the object needs is the same transform conjugated into BeamNG
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

from csiibundle import instances as bundle_instances, tints

from ..beamng import frame, placement
from . import Stage, level_of

STAGE_KEY = "instances"

# The `collisionType` an object with a `Colmesh_` node takes. BeamNG's TSStatic documentation
# ranks the options and this is the one it recommends; the derived/network tiles deliberately keep
# `Visible Mesh Final` because they have no colmesh and the visible road surface IS the collision
# surface there.
COLLISION_TYPE = "Collision Mesh"

# SimGroup granularity for TSStatics, in metres. Vanilla levels group by district; a kilometre
# keeps each `items.level.json` a few hundred lines on a city map. OURS.
GROUP_METRES = 1000.0

# BeamNG's semantic-segmentation tag per record kind, from the vanilla levels' own usage
# (`NATURE` on trees and bushes, `OBSTACLES` on street furniture, `BUILDINGS` on buildings).
ANNOTATION = {"tree": "NATURE", "prop": "OBSTACLES", "building": "BUILDINGS"}

# `TSForestItemData` wind/physics defaults, the median of the vanilla levels' own values
# (1,678 item datas read across every shipped level). OURS in the sense that CS2 states nothing
# about wind; vanilla-derived rather than invented.
FOREST_DEFAULTS = {"tree": {"windScale": 0.4, "detailAmp": 0.1, "detailFreq": 1.0,
                            "branchAmp": 0.05, "trunkBendScale": 0.004, "rigidity": 20, "mass": 10},
                   "prop": {"windScale": 0.0, "rigidity": 50, "mass": 50}}

# When forest shadows are off. `TSForestItemData` has no `castShadows`; `shadowNullDetailSize`
# is the only shadow control the vanilla data uses (40 px on 138 items), so a very large value
# drops the shadow immediately. INFERRED from vanilla usage, not from documentation.
NO_SHADOW_DETAIL_SIZE = 100000

# Below this SHAPE height in metres a forest type ships `collidable: false`. BeamNG's own
# guidance is "disable collision for small decorative objects that vehicles should not hit", and
# 1.16M collidable items is 1.16M entries in the physics broadphase for flower beds and kerb
# planters. **THE NUMBER IS OURS** - CS2 has no collision concept for a placed object at all - so
# it is declared here, counted in the report, and chosen to sit under a passenger wheel radius
# (0.30-0.35 m): anything this short is driven OVER, not into. The collision settings still win
# in the other direction; this can only ever remove collision, never add it.
FOREST_COLLIDABLE_MIN_HEIGHT = 0.5

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
    _clear_forest(forest_dir)
    lines = {}                                              # type -> [json line, ...]
    pending = 0
    types = {}                                              # type -> the shape row behind it
    type_source = {}                                        # type -> (asset, suffix, variant)
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
        position, rotation, scale = _transform(record)
        if forest:
            types.setdefault(name, (row, kind))
            type_source.setdefault(name, (asset, suffix, variant))
            item_counts[name] = item_counts.get(name, 0) + 1
            lines.setdefault(name, []).append(_forest_line(name, position, rotation, scale))
            pending += 1
            counts["forest"] += 1
            if pending >= FLUSH_EVERY:
                _flush(forest_dir, lines)
                pending = 0
        else:
            key = _add_static(level, record, name, row, kind, position, rotation, scale, collide, statics)
            collision_types[key] = collision_types.get(key, 0) + 1
            statics += 1
            counts["tsstatic"] += 1
    _flush(forest_dir, lines)
    merged = _merge_thin_types(forest_dir, types, type_source, item_counts, results,
                               int(settings.get("forest_min_items") or 0))
    progress.end(f"{counts['forest']:,} forest items in {len(types):,} types "
                 f"({merged['types']} thin types holding {merged['items']} items merged into their "
                 f"untinted twin), {counts['tsstatic']:,} TSStatics")

    forest_stats = {}
    if types:
        level.add(["Vegetation"], {"name": "theForest", "class": "Forest"}, key="forest:theForest")
        for name, (row, _kind) in types.items():
            row["_items"] = item_counts.get(name, 0)   # for the collision-hygiene count below
        forest_stats = _write_item_data(level, types, settings, collide)
    level.write_objects()

    report = {
        "forestItems": counts["forest"], "forestTypes": len(types), "tsStatics": counts["tsstatic"],
        "forestTypesMerged": merged["types"], "forestItemsMerged": merged["items"],
        "forestMinItems": int(settings.get("forest_min_items") or 0),
        "collisionTypes": collision_types,
        "forestCollision": forest_stats,
        "forestBytes": _forest_bytes(forest_dir),
        "placed": counts["forest"] + counts["tsstatic"],
        "scanned": counts["records"], "kept": counts["kept"], "noShape": counts["noShape"],
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
    progress.log(f"instances: {report['placed']:,} objects placed "
                 f"({report['forestItems']:,} forest / {report['tsStatics']:,} TSStatic)")
    if untinted_forest:
        progress.log(f"instances: {untinted_forest:,} forest items drew an untinted albedo because "
                     f"their kind is outside the bake's paint_kinds")
    return report


# -- one record ------------------------------------------------------------------------------------

def _state_suffix(record, asset, mesh_dir, have, cache):
    key = (asset, (record.get("attrs") or {}).get("treeState"), (record.get("attrs") or {}).get("fullness"))
    if key in cache:
        return cache[key]
    answer = None
    for candidate in bundle_instances.mesh_candidates(record):
        if candidate not in have:
            have[candidate] = os.path.isfile(os.path.join(mesh_dir, candidate + ".glb"))
        if have[candidate]:
            answer = candidate[len(asset):]
            break
    cache[key] = answer
    return answer


def _transform(record):
    """`(position, rotation3x3_rowmajor, scale3)` in BeamNG world axes.

    `P . T . R . S . P^-1` written out: `P` swaps Y and Z, so the position and the scale swap
    their last two components and the rotation matrix swaps both its rows and its columns. The
    tree growth scale is uniform and multiplies straight in.
    """
    pos = record["pos"]
    position = [float(pos[0]), float(pos[2]), float(pos[1])]
    rotation = _swap_yz(_quat_rows(record.get("rot") or (0.0, 0.0, 0.0, 1.0)))
    raw = record.get("scale") or (1.0, 1.0, 1.0)
    growth = bundle_instances.growth_scale(record)
    scale = [float(raw[0]) * growth, float(raw[2]) * growth, float(raw[1]) * growth]
    return position, rotation, scale


def _quat_rows(quat):
    """A bundle quaternion `[x, y, z, w]` as a 3x3 row-major rotation in the BUNDLE frame."""
    x, y, z, w = (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))
    n = x * x + y * y + z * z + w * w
    s = 0.0 if n <= 0.0 else 2.0 / n
    xx, yy, zz = x * x * s, y * y * s, z * z * s
    xy, xz, yz = x * y * s, x * z * s, y * z * s
    wx, wy, wz = w * x * s, w * y * s, w * z * s
    return [[1.0 - (yy + zz), xy - wz, xz + wy],
            [xy + wz, 1.0 - (xx + zz), yz - wx],
            [xz - wy, yz + wx, 1.0 - (xx + yy)]]


def _swap_yz(matrix):
    """`P . M . P^-1` for the Y/Z swap: exchange rows 1 and 2, then columns 1 and 2."""
    m = [matrix[0][:], matrix[2][:], matrix[1][:]]
    for row in m:
        row[1], row[2] = row[2], row[1]
    return m


def _forest_line(type_name, position, rotation, scale):
    """One `.forest4.json` line. A forest item carries ONE scalar scale, so a record that needed
    three would not have reached here (`placement.wants_forest`)."""
    flat = frame.basis_flat(rotation)
    item = {"ctxid": 0,
            "pos": [round(v, 4) for v in position],
            "rotationMatrix": [round(v, ROTATION_DP) for v in flat],
            "scale": round(float(sum(scale) / 3.0), 5),
            "type": type_name}
    return json.dumps(item, separators=(",", ":"))


def _add_static(level, record, name, row, kind, position, rotation, scale, collide, ordinal):
    """One `TSStatic`, with the record's own three `colorSet` channels as `instanceColor*`.

    `ordinal` disambiguates the persistentId: a record `id` is unique per LOGICAL object and
    section records of one stacked instance deliberately share it (§4.1), so `id` alone would
    collide on a stack.
    """
    flat = frame.basis_flat(rotation)
    obj = {"name": f"{name}_{record['id']}_{ordinal}",
           "class": "TSStatic",
           "shapeName": level.game_path("art", "shapes", "cs2", row["file"]),
           "position": [round(v, 3) for v in position],
           "rotationMatrix": [round(v, 6) for v in flat],
           "scale": [round(v, 5) for v in scale],
           # `Collision Mesh` uses the `Colmesh_<name>` node the shapes stage already writes into
           # every collidable shape (a trunk cylinder, the decimated LOD1 hull, or a padded box).
           # BeamNG's own docs: "Collision Mesh - uses dedicated collision meshes from the shape.
           # **Recommended**"; "Visible Mesh Final - uses rendered mesh polygons intentionally.
           # Use only when required", and "expensive". A3 shipped 14,658 statics on the expensive
           # one while carrying the cheap one in the file unused.
           "collisionType": COLLISION_TYPE if row.get("collider", "none") != "none" else "None",
           "decalType": "None",
           "useInstanceRenderData": True,
           "annotation": ANNOTATION.get(kind, "OBSTACLES")}
    colours = tints.color_set(record.get("attrs") or {})
    for index, colour in enumerate(colours[:3]):
        key = "instanceColor" if index == 0 else f"instanceColor{index}"
        obj[key] = [round(float(c), 6) for c in colour]
    group = _group_of(position)
    level.add(["Objects", group], obj, key=f"instance:{record['id']}:{name}:{ordinal}")
    return obj["collisionType"]


def _group_of(position):
    return f"km_{int(position[0] // GROUP_METRES)}_{int(position[1] // GROUP_METRES)}"


def _collision_policy(settings):
    if str(settings.get("collision_preset") or "simplified") == "off":
        return {}
    return {"building": bool(settings.get("collision_buildings", True)),
            "prop": bool(settings.get("collision_props", True)),
            "tree": bool(settings.get("collision_trees", True))}


# -- the forest files --------------------------------------------------------------------------

def _merge_thin_types(forest_dir, types, type_source, item_counts, results, min_items):
    """Fold every forest type under `min_items` into its prefab's UNTINTED type. In place.

    BeamNG loads a `TSForestItemData` whole — its shape, its LOD chain, its material set — whether
    the type holds one item or ten thousand, and the paint-variant system makes a type per colour
    centre per prefab. On the reference city 2,864 of 4,951 types hold 10 items or fewer and 7,641
    items between them: 58 % of the types for 0.7 % of the forest.

    Nothing is deleted. Each thin type's items are rewritten onto the untinted type of the same
    prefab and state (`results[(asset, suffix, None)]`, the shape a prefab with no baked variants
    gets anyway), so the item stays where it was and loses only its tint — the same approximation
    the forest already declares for every item, one step coarser. A type whose prefab has no
    untinted shape, and a type that IS the untinted one, are both left alone.
    """
    out = {"types": 0, "items": 0}
    if min_items <= 0:
        return out
    for name in sorted(types):
        if item_counts.get(name, 0) >= min_items:
            continue
        asset, suffix, variant = type_source.get(name, (None, None, None))
        if variant is None:
            continue                                    # already the untinted type
        base_row = results.get((asset, suffix, None))
        if base_row is None:
            continue                                    # no untinted shape to fold into
        base = os.path.splitext(base_row["file"])[0]
        if base == name or base not in types:
            # The untinted shape EXISTS (the shapes stage writes one for every prefab) but this
            # build may never have placed it, and a type with items and no `TSForestItemData` is
            # dropped silently by BeamNG — which is exactly what `validate_level` caught on the
            # first A5 build. Merge only into a type that is already being declared.
            continue
        thin = os.path.join(forest_dir, name + ".forest4.json")
        if not os.path.isfile(thin):
            continue
        with open(thin, encoding="utf-8") as handle:
            rows = [line for line in handle if line.strip()]
        with open(os.path.join(forest_dir, base + ".forest4.json"), "a",
                  encoding="utf-8", newline="\n") as handle:
            for line in rows:
                handle.write(line.replace(f'"type":"{name}"', f'"type":"{base}"'))
        os.remove(thin)
        out["types"] += 1
        out["items"] += len(rows)
        types.pop(name, None)
        moved = item_counts.pop(name, 0)
        if base in types:
            item_counts[base] = item_counts.get(base, 0) + moved
    return out


def _clear_forest(forest_dir):
    """A rebuild replaces the forest wholesale: a type that is no longer placed must not keep
    its items from the previous build (BeamNG mounts every `.forest4.json` it finds)."""
    if not os.path.isdir(forest_dir):
        os.makedirs(forest_dir, exist_ok=True)
        return
    for name in os.listdir(forest_dir):
        if name.endswith(".forest4.json"):
            os.remove(os.path.join(forest_dir, name))


def _flush(forest_dir, lines):
    os.makedirs(forest_dir, exist_ok=True)
    for type_name, rows in lines.items():
        if not rows:
            continue
        with open(os.path.join(forest_dir, type_name + ".forest4.json"), "a",
                  encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(rows))
            handle.write("\n")
        rows.clear()


def _write_item_data(level, types, settings, collide):
    """`art/forest/managedItemData.json` — one `TSForestItemData` per placed type.

    Returns `{"types", "collidable", "tooShort", "policyOff", "plant", "itemsNotCollidable"}` —
    the collision hygiene split the report declares.
    """
    shadows = bool(settings.get("forest_shadows", True))
    document = {}
    stats = {"types": 0, "collidable": 0, "tooShort": 0, "policyOff": 0, "plant": 0,
             "itemsNotCollidable": 0}
    for name, (row, kind) in sorted(types.items()):
        entry = {"name": name, "internalName": name, "class": "TSForestItemData",
                 "persistentId": level.pid("forestItem:" + name),
                 "shapeFile": level.game_path("art", "shapes", "cs2", row["file"]),
                 "annotation": ANNOTATION.get(kind, "OBSTACLES"),
                 "radius": round(float(row["radius"]), 3)}
        entry.update(FOREST_DEFAULTS.get(kind, FOREST_DEFAULTS["prop"]))
        stats["types"] += 1
        collidable = bool(collide.get(kind))
        if not collidable:
            stats["policyOff"] += 1
        elif row.get("tree") is False:
            # `kind == "tree"` is ALL vegetation. CS2 builds a bush, a flower pot and a patch of
            # long grass from `PlantObject`, which never gets the `Tree` component
            # (`csiibundle.instances.is_tree`) — so this is the GAME's split, not a height guess,
            # and it holds regardless of how tall the shape happens to be.
            collidable = False
            stats["plant"] += 1
        elif _shape_height(row) < FOREST_COLLIDABLE_MIN_HEIGHT:
            collidable = False
            stats["tooShort"] += 1
        if collidable:
            stats["collidable"] += 1
        else:
            entry["collidable"] = False
            stats["itemsNotCollidable"] += int(row.get("_items") or 0)
        if not shadows:
            entry["shadowNullDetailSize"] = NO_SHADOW_DETAIL_SIZE
        document[name] = entry
    level.write_json(document, "art", "forest", "managedItemData.json")
    return stats


def _forest_bytes(forest_dir):
    total = files = 0
    if os.path.isdir(forest_dir):
        for name in os.listdir(forest_dir):
            if name.endswith(".forest4.json"):
                files += 1
                try:
                    total += os.path.getsize(os.path.join(forest_dir, name))
                except OSError:
                    pass
    return {"files": files, "bytes": total, "mib": round(total / (1 << 20), 1)}


def _shape_height(row):
    """The shape's own height in metres — glTF Y, which §2.2 keeps as up. Bounds come from the
    shapes stage, which measured them in the mesh's own space."""
    lo, hi = row.get("boundsMin"), row.get("boundsMax")
    if not lo or not hi or len(lo) < 2 or len(hi) < 2:
        return float("inf")                            # unknown height never loses its collider
    return float(hi[1]) - float(lo[1])


INPUTS = ("layer:instances", "layer:meshes", "derived:paint_variants.json")
SETTINGS = ("trees", "props", "buildings", "foliage_density", "prop_density", "region",
            "collision_preset", "collision_buildings", "collision_props", "collision_trees",
            "forest_shadows", "forest_min_items", "foliage_road_clearance")
OUTPUTS = ("forest",)

# version 4: `forest_min_items` folds a thin colour-variant type into its untinted twin —
# only into a type this build is already declaring, or the items land on a type BeamNG drops
# version 8: `rotationMatrix` is written as the object's own axes in groups of three - it
# was flattened row-major, i.e. every TSStatic and forest item carried the INVERSE of its
# rotation, which is why buildings stood diagonally on their own plots (A6.8)
STAGE = Stage(STAGE_KEY, "Trees, props and buildings", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS, outputs=OUTPUTS, after=("shapes",), version=8)
