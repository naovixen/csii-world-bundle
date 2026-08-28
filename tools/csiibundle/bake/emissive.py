"""The `materials` product's emissive join — CS2's colour slots onto the resolved table.

**What this closes.** `materials.json` carries no `emissiveFactor` on any material — CS2 does
not drive emission from a static surface property, it drives it per-instance from the render
prefab's `ProceduralLight` buffer, which is `lights.json`'s 1,896 colour slots. Without the
join every emissive material in the city is a map with no colour and no strength behind it.

**The join, and why one half of it is an ordinal.** The game's own key is
`EmissiveProperties.SingleLightMapping.materialId` — a slot names one material SLOT of the
mesh, and a slot is the position of the primitive that uses it. `AppearanceRipSystem` ships
that id as of 2026-08-19, so on a bundle ripped since then the join is exact and the ordinal
path never runs. On an older bundle the id is absent, and the slots of a mesh are matched to
that mesh's emissive materials **in `.glb` primitive order** — the same order — but only where
the two counts agree. Measured on the 2026-08-17 reference rip, of the 781 slot-carrying
meshes whose `.glb` is in the bundle:

    1 slot  : 1 emissive material     232    exact
    2 slots : 2 emissive materials    407    exact
    2 slots : 1 emissive material     129    ambiguous, skipped
    6 or 4  : 1 emissive material      13    ambiguous, skipped

**639 of 781 (81.8 %)**, covering 126,212 placed instances. The other 18.2 % and the 314
emissive materials no slot reaches keep the flat consumer default.

**12 materials are shared by meshes whose slots disagree** — five neon sign frames reuse one
texture set across three different sign colours. The first mesh in sorted order wins and the
rest are counted; there is no per-instance emissive colour in the level format to give them
each their own.
"""

import os

from .. import glb
from .. import lights as bundle_lights

# Nits per unit of CS2 emissive intensity — the one number in the colour-slot port that is not
# CS2's own. DECLARED TUNING, and here is exactly what is and is not known.
#
# CS2's side is exact: `EmissiveProperties.SingleLightMapping.intensity x kIntensityMultiplier`
# (= 100f) is the alpha of the `ProceduralLight` colour, and the shader is handed `(rgb, that)`.
# What is NOT known is the physical unit CS2's own shader reads it in — the shaders are not in
# the managed assemblies, and the bundle carries no nits. So the scale is calibrated against the
# CONSUMER's own band, which is measurable: over all 22 materials in BeamNG's 21 shipped levels
# that state `emissiveIntensityNits`, the range is 200 (`west_coast_usa`'s lit building windows
# and tunnel) to 10,000, median 5,000 (its `trafficLight`), and its `floodlight` is 4,000.
#
# At 10 nits/unit the reference city's slots land:
#
#     NeonSign          alpha 5 .. 75    ->    50 ..   750 nits
#     EffectSource      alpha 60         ->          600 nits
#     traffic signals   alpha 131 .. 381 ->  1,315 .. 3,811 nits   (vanilla's own is 5,000)
#     DecorativeLight   alpha 500        ->        5,000 nits      (vanilla's median exactly)
#
# — the whole placed spread inside vanilla's band with the medians on top of each other. It is
# one constant over 652 materials, so a later correction is one edit, and the ratios between
# CS2's own classes are preserved whatever it is set to.
EMISSIVE_NITS_PER_UNIT = 10.0

# The nits ceiling, so a vehicle-class slot that reaches a placed mesh cannot ship an emission an
# order of magnitude past anything the consumer engine has ever shipped. Vanilla's own maximum.
EMISSIVE_NITS_MAX = 10000.0


def _slot_pairs(slots, primitives, resolved, stats):
    """`(material name, slot)` pairs for one mesh — by the game's own materialId when the rip
    ships it, by primitive order only where the counts agree, or None when ambiguous."""
    # The game's own key, when the rip shipped it. `materialId` is the mesh's material SLOT,
    # which is the position of the primitive that uses it (`glb.primitive_materials`), so no
    # ordinal guess is needed and the count does not have to agree with anything. A `layerId`
    # slot drives a material LAYER instead — the multi lights, which come first in the buffer
    # — and the bundle has no layer concept, so it is skipped and counted.
    by_id = [(int(e["materialId"]), e) for e in slots if isinstance(e.get("materialId"), int)]
    layers = sum(1 for e in slots if "layerId" in e)
    if by_id or layers:
        # A `layerId` slot names a material LAYER, which the bundle has no counterpart for.
        # A mesh with only those is not ambiguous - it has no material join by design, and on
        # the reference city all 62 of them are vehicles.
        stats["slotsOnAMaterialLayer"] += layers
        if by_id:
            stats["slotMeshesJoinedById"] += 1
        else:
            stats["slotMeshesLayersOnly"] += 1
        return [(primitives[i], e) for i, e in by_id if 0 <= i < len(primitives) and primitives[i]]
    names = []
    for name in primitives:
        if name and name not in names:
            names.append(name)
    emissive = [n for n in names if (resolved.get(n) or {}).get("emissive")]
    if not emissive or len(emissive) != len(slots):
        stats["slotMeshesAmbiguous"] += 1
        return None
    stats["slotMeshesJoined"] += 1
    return list(zip(emissive, slots))


def _apply_emission(pairs, resolved, claimed, contested):
    """Write each pair's lit colour and nits onto the resolved record, first mesh wins."""
    for name, slot in pairs:
        if name not in resolved:
            continue
        emission = bundle_lights.slot_emission(slot)
        if emission is None:
            continue
        rgb, raw = emission
        if name in claimed:
            if claimed[name] != (rgb, raw):
                contested.add(name)
            continue
        claimed[name] = (rgb, raw)
        record = resolved[name]
        record["emissiveFactor"] = rgb
        record["emissiveNits"] = min(EMISSIVE_NITS_MAX, raw * EMISSIVE_NITS_PER_UNIT)


def emissive_slots(bundle, resolved, progress):
    """Join each mesh's colour slots to its emissive materials and write CS2's own emission into
    the resolved records — the join's story and its measured coverage are the module docstring.
    Returns the stats the product reports."""
    catalogue = bundle_lights.load(bundle)
    prefabs = (
        catalogue.get("prefabs")
        if isinstance(catalogue, dict) and "prefabs" in catalogue
        else catalogue
    )
    by_asset = bundle_lights.slots_by_asset(prefabs or {})
    stats = {
        "slotMeshes": len(by_asset),
        "slotMeshesRead": 0,
        "slotMeshesJoined": 0,
        "slotMeshesJoinedById": 0,
        "slotMeshesLayersOnly": 0,
        "slotMeshesAmbiguous": 0,
        "slotMeshesMissing": 0,
        "slotsOnAMaterialLayer": 0,
        "materialsFromSlots": 0,
        "materialsContested": 0,
    }
    if not by_asset:
        return stats
    progress.begin("materials: emissive colour slots", total=len(by_asset), unit="meshes")
    claimed = {}
    contested = set()
    for asset in sorted(by_asset):
        progress.advance(1)
        slots = by_asset[asset]
        primitives = glb.primitive_materials(os.path.join(bundle, "meshes", asset + ".glb"))
        if primitives is None:
            stats["slotMeshesMissing"] += 1
            continue
        stats["slotMeshesRead"] += 1
        pairs = _slot_pairs(slots, primitives, resolved, stats)
        if pairs is None:
            continue
        _apply_emission(pairs, resolved, claimed, contested)
    stats["materialsFromSlots"] = len(claimed)
    stats["materialsContested"] = len(contested)
    progress.end(
        f"{stats['slotMeshesJoinedById']} of {stats['slotMeshesRead']} slot-carrying "
        f"meshes joined by the game's own materialId and {stats['slotMeshesJoined']} by "
        f"ordinal ({stats['slotMeshesAmbiguous']} ambiguous, "
        f"{stats['slotMeshesLayersOnly']} on a material layer only, "
        f"{stats['slotMeshesMissing']} without a mesh), "
        f"{len(claimed)} materials take CS2's own emissive colour"
    )
    return stats
