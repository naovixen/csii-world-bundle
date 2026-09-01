"""The forest files: the item data BeamNG loads, and folding the rarest kinds together.

A `TSForestItemData` is loaded whole whether it has one member or ten thousand, so a kind with
too few placements costs more as a kind than it saves as a variant."""

import os

# The two `collider` values that mean the shape carries NO `Colmesh_` node: the policy said no
# (`none`), or `collision_lights` took a light-bearing prefab's collider off (`lightNone`, audit
# §88). Naming `Collision Mesh` on either is a TSStatic that collides with nothing, which the
# level validator now calls out - so both paths that decide collision read this one tuple.
# ...or the shape is thin AND wider than a padded box may be (`oversizedThin`,
# `shapecollider.collider`): a field plane or a roundabout's paint lies on terrain that collides.
# ...or the shape is a PLANT (`plant`: a bush, a hedge - no collider is cut for it, so a
# TSStatic of one asked for a `Collision Mesh` it does not have and collided with nothing;
# the cooked-shape validator check is what surfaced it, E2)
NO_COLLIDER = ("none", "lightNone", "oversizedThin", "plant")

# BeamNG's semantic-segmentation tag per record kind, from the vanilla levels' own usage
# (`NATURE` on trees and bushes, `OBSTACLES` on street furniture, `BUILDINGS` on buildings).
ANNOTATION = {"tree": "NATURE", "prop": "OBSTACLES", "building": "BUILDINGS"}

# `TSForestItemData` wind/physics defaults, the median of the vanilla levels' own values
# (1,678 item datas read across every shipped level). OURS in the sense that CS2 states nothing
# about wind; vanilla-derived rather than invented.
FOREST_DEFAULTS = {
    "tree": {
        "windScale": 0.4,
        "detailAmp": 0.1,
        "detailFreq": 1.0,
        "branchAmp": 0.05,
        "trunkBendScale": 0.004,
        "rigidity": 20,
        "mass": 10,
    },
    "prop": {"windScale": 0.0, "rigidity": 50, "mass": 50},
}

# When forest shadows are off. `TSForestItemData` has no `castShadows`; `shadowNullDetailSize`
# is the only shadow control the vanilla data uses (40 px on 138 items), so a very large value
# drops the shadow immediately. INFERRED from vanilla usage, not from documentation.
NO_SHADOW_DETAIL_SIZE = 100000

# ...and when they are ON, the size at which each type's shadow STOPS — vanilla's own number.
#
# `shadowNullDetailSize` is a detail size like any other, so it is the pixel size below which the
# item stops casting: a NEAR shadow is kept and the far tail leaves the pass. Until now this was
# on/off, and "on" meant 576,148 forest items in the shadow pass with no far stop at all, against
# vanilla shipping 40 px on the 138 items that set it. Shadow-caster submission roughly doubles
# geometry work for the objects in the pass, and it is BeamNG's own #3 slow-scene cause.
#
# INFERRED from vanilla data, not documentation (the field is undocumented) — so it is a setting,
# and 0 restores the old behaviour of no far stop.
FOREST_SHADOW_NULL_PX = 40

# The suffix `stages.instances` puts on a forest type carrying the ELEVATED items of another type
# (a prop on a roof or a deck). The twin names the same `shapeFile` — Torque mounts a shape once
# per path, so it costs one `TSForestItemData` and no mesh residency — and ships `collidable:
# false` with its shadow off, which is the only way a per-INSTANCE decision can be expressed in
# `.forest4.json` at all (a forest item has no per-item collision or shadow flag).
ELEVATED_SUFFIX = "__elevated"

# Below this SHAPE height in metres a forest type ships `collidable: false`. BeamNG's own
# guidance is "disable collision for small decorative objects that vehicles should not hit", and
# 1.16M collidable items is 1.16M entries in the physics broadphase for flower beds and kerb
# planters. **THE NUMBER IS OURS** - CS2 has no collision concept for a placed object at all - so
# it is declared here, counted in the report, and chosen to sit under a passenger wheel radius
# (0.30-0.35 m): anything this short is driven OVER, not into. The collision settings still win
# in the other direction; this can only ever remove collision, never add it.
FOREST_COLLIDABLE_MIN_HEIGHT = 0.5

# And below this horizontal FOOTPRINT in metres, whatever its height. Height alone was the wrong
# instrument and it let through exactly what Naomi asked to have removed: a fire hydrant is 0.97 m
# tall and a road sign 2.2 m, so both cleared the height rule and both stopped a car dead.
#
# Measured over all 2,928 collidable types of the reference city, `max(x_extent, z_extent)` from
# the shape's own bounds: < 0.40 m takes 50 types, < 0.60 m takes 165, **< 0.80 m takes 442**, and
# < 1.00 m takes 659. At 0.80 the list is what it should be — NA_FireHydrant01 (0.46),
# NA_OnewaySign01 (0.49), NA_ParkingSign01 (0.54), NA_TrafficLightCar01 (0.56), Trashbin01-04
# (0.49-0.73), NA_MailboxStand01-04 (0.30-0.66), SignSidewaySmall01/02 (0.78), GardenLight01 (0.13)
# — and nothing survives below it that a car should stop against.
#
# **One exception this rule gets wrong, declared rather than special-cased: `Bollard01`/`02` are
# 0.24 m and a bollard exists precisely to stop a vehicle.** Exempting them means matching a name,
# which is not a property of the object and which this repo has a standing rule against; if
# driving through a bollard turns out to matter, the fix is a property the bundle carries, not a
# string. THE NUMBER IS OURS — CS2 has no collision concept for a placed object at all — so it is
# declared here, counted in the report as `tooThin`, and exposed as a setting.
FOREST_COLLIDABLE_MIN_FOOTPRINT = 0.8


# -- the forest files --------------------------------------------------------------------------


def drop_rare_types(
    forest_dir, types, type_source, item_counts, min_items, results=None, keep_radius=0.0
):
    """Drop every forest type placed fewer than `min_items` times — unless its shape's bounding
    radius is at least `keep_radius` metres. In place; returns `{"types", "items", "keptLarge"}`.

    A `TSForestItemData` mounts its shape whole for one item or ten thousand. The v17 removal
    took the floor out entirely because 10 dropped the rarer members of prop FAMILIES (a
    half-built scene); Naomi asked the middle back at 3: only the one-and-two-item tail goes,
    and anything of `keep_radius` (a crane, a floodlight tower) stays whatever its count.
    0 keeps every type. DECLARED APPROXIMATION, counted in the report.
    """
    dropped_types = dropped_items = 0
    kept = {"types": 0, "items": 0}
    if min_items <= 0:
        return {"types": 0, "items": 0, "keptLarge": kept}
    for name in sorted(types):
        count = int(item_counts.get(name, 0))
        if count >= min_items:
            continue
        if name.endswith(ELEVATED_SUFFIX):
            # The elevated split makes a twin per prefab, so it manufactures exactly the thin
            # types this floor removes — and here that would DELETE a roof's props rather than
            # coarsen them. The rare-type floor exists to stop a type mounting a shape for one
            # item; a twin mounts nothing its parent has not already mounted.
            continue
        source = type_source.get(name)
        row = (results or {}).get(source) if source else None
        if keep_radius > 0.0 and row is not None and float(row.get("radius") or 0.0) >= keep_radius:
            kept["types"] += 1
            kept["items"] += count
            continue
        try:
            os.remove(os.path.join(forest_dir, name + ".forest4.json"))
        except OSError:
            pass
        del types[name]
        type_source.pop(name, None)
        item_counts.pop(name, None)
        dropped_types += 1
        dropped_items += count
    return {"types": dropped_types, "items": dropped_items, "keptLarge": kept}


def merge_thin_types(forest_dir, types, type_source, item_counts, results, min_items):
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
        if name.endswith(ELEVATED_SUFFIX):
            # An elevated twin is a type BECAUSE of what it does not ship (collision, shadow), so
            # folding it into the untinted type would hand both back — the merge would silently
            # undo the rule on exactly the thin twins it was most likely to catch.
            continue
        asset, suffix, variant = type_source.get(name, (None, None, None))
        if variant is None:
            continue  # already the untinted type
        base_row = results.get((asset, suffix, None))
        if base_row is None:
            continue  # no untinted shape to fold into
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
        with open(
            os.path.join(forest_dir, base + ".forest4.json"), "a", encoding="utf-8", newline="\n"
        ) as handle:
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


def clear_forest(forest_dir):
    """A rebuild replaces the forest wholesale: a type that is no longer placed must not keep
    its items from the previous build (BeamNG mounts every `.forest4.json` it finds)."""
    if not os.path.isdir(forest_dir):
        os.makedirs(forest_dir, exist_ok=True)
        return
    for name in os.listdir(forest_dir):
        if name.endswith(".forest4.json"):
            os.remove(os.path.join(forest_dir, name))


def flush(forest_dir, lines):
    os.makedirs(forest_dir, exist_ok=True)
    for type_name, rows in lines.items():
        if not rows:
            continue
        with open(
            os.path.join(forest_dir, type_name + ".forest4.json"),
            "a",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write("\n".join(rows))
            handle.write("\n")
        rows.clear()


def _is_collidable(row, kind, collide, min_footprint, stats):
    """Whether this forest type ships collidable, counting into `stats` why not.

    The order matters and every rung has its own reason, so a single `and` of five conditions
    would lose which one fired — and which one fired is what the report declares.
    """
    if not collide.get(kind):
        stats["policyOff"] += 1
        return False
    if row.get("tree") is False:
        # `kind == "tree"` is ALL vegetation. CS2 builds a bush, a flower pot and a patch of
        # long grass from `PlantObject`, which never gets the `Tree` component
        # (`csiibundle.instances.is_tree`) — so this is the GAME's split, not a height guess,
        # and it holds regardless of how tall the shape happens to be.
        stats["plant"] += 1
        return False
    if row.get("collider") in NO_COLLIDER:
        # The shape carries no collision mesh at all - `collision_lights` off takes every
        # light-bearing prefab out - so a collidable forest type would name nothing.
        stats["noCollider"] = stats.get("noCollider", 0) + 1
        return False
    if _shape_height(row) < FOREST_COLLIDABLE_MIN_HEIGHT:
        stats["tooShort"] += 1
        return False
    if _shape_footprint(row) < min_footprint:
        stats["tooThin"] += 1
        return False
    return True


def write_item_data(level, types, settings, collide):
    """`art/forest/managedItemData.json` — one `TSForestItemData` per placed type.

    Returns `{"types", "collidable", "tooShort", "policyOff", "plant", "itemsNotCollidable"}` —
    the collision hygiene split the report declares.
    """
    shadows = bool(settings.get("forest_shadows", True))
    shadow_null_px = int(settings.get("forest_shadow_null_px", FOREST_SHADOW_NULL_PX) or 0)
    document = {}
    stats = {
        "types": 0,
        "collidable": 0,
        "tooShort": 0,
        "tooThin": 0,
        "policyOff": 0,
        "noCollider": 0,
        "plant": 0,
        "itemsNotCollidable": 0,
        "shadowNullPx": shadow_null_px if shadows else None,
        "elevatedTypes": 0,
    }
    min_footprint = float(
        settings.get("collision_min_footprint", FOREST_COLLIDABLE_MIN_FOOTPRINT) or 0.0
    )
    for name, (row, kind) in sorted(types.items()):
        entry = {
            "name": name,
            "internalName": name,
            "class": "TSForestItemData",
            "persistentId": level.pid("forestItem:" + name),
            "shapeFile": level.game_path("art", "shapes", "cs2", row["file"]),
            "annotation": ANNOTATION.get(kind, "OBSTACLES"),
            "radius": round(float(row["radius"]), 3),
        }
        entry.update(FOREST_DEFAULTS.get(kind, FOREST_DEFAULTS["prop"]))
        stats["types"] += 1
        elevated = name.endswith(ELEVATED_SUFFIX)
        if elevated:
            # the twin's whole reason for existing: neither cost, whatever its shape says
            entry["collidable"] = False
            entry["shadowNullDetailSize"] = NO_SHADOW_DETAIL_SIZE
            stats["elevatedTypes"] = stats.get("elevatedTypes", 0) + 1
            stats["itemsNotCollidable"] += int(row.get("_items") or 0)
            document[name] = entry
            continue
        if not _is_collidable(row, kind, collide, min_footprint, stats):
            entry["collidable"] = False
            stats["itemsNotCollidable"] += int(row.get("_items") or 0)
        else:
            stats["collidable"] += 1
        if not shadows:
            entry["shadowNullDetailSize"] = NO_SHADOW_DETAIL_SIZE
        elif shadow_null_px > 0:
            entry["shadowNullDetailSize"] = shadow_null_px
        document[name] = entry
    level.write_json(document, "art", "forest", "managedItemData.json")
    return stats


def forest_bytes(forest_dir):
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


def _shape_footprint(row):
    """The shape's larger HORIZONTAL extent in metres — glTF X and Z, since §2.2 keeps Y up.

    Not `TSForestItemData.radius`, which is a bounding SPHERE and therefore dominated by height on
    exactly the thin, tall things this is meant to catch: a palm tree reads 6.42 m and a pier
    16.76, while a 2.2 m road sign reads over 1 m on a 0.5 m post.
    """
    lo, hi = row.get("boundsMin"), row.get("boundsMax")
    if not lo or not hi or len(lo) < 3 or len(hi) < 3:
        return float("inf")  # unknown footprint never loses its collider
    return max(float(hi[0]) - float(lo[0]), float(hi[2]) - float(lo[2]))


def _shape_height(row):
    """The shape's own height in metres — glTF Y, which §2.2 keeps as up. Bounds come from the
    shapes stage, which measured them in the mesh's own space."""
    lo, hi = row.get("boundsMin"), row.get("boundsMax")
    if not lo or not hi or len(lo) < 2 or len(hi) < 2:
        return float("inf")  # unknown height never loses its collider
    return float(hi[1]) - float(lo[1])
