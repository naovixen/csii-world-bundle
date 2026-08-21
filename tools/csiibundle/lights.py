"""Lights (§4.3) — and the fact that `lights.json` holds TWO different kinds of record.

One file, keyed by prefab, whose values are lists of entries — but the entries are not all the
same thing, and a consumer that treats them uniformly will either create 2,532 lights where the
game has 654, or create none at all. On the reference city:

* **654 photometric lights** — a real light source to instantiate. Carries `type` (`Spot` or
  `Point`), `position`, `rotation`, `range`, `intensity` + `intensityUnit`, `temperature`,
  `color`, and for a spot `spotAngle` / `innerSpotPercentage` / `spotShape`.
* **1,878 colour slots** — NOT lights. These carry `slot`, `color`, `colorOff`, `purpose` and
  `responseSpeed`, and they describe **emissive material state**: a neon sign's lit colour, a
  traffic light's red/amber/green, a vehicle's brake and indicator lamps. The game switches a
  mesh's emission between `color` and `colorOff`; there is no light source to place.

`is_photometric` / `is_colour_slot` tell them apart, by which fields are present rather than by
a type tag the file does not carry.

**The colour slots are the ones you will notice.** `purpose` is the game's own enum —
`NeonSign` (1,045 of them), `TrafficLight_Red/Yellow/Green`, `Headlight_LowBeam`,
`BrakeLight`, `PedestrianLight_Walk`… A city rebuilt without them has dark signs and unlit
traffic signals, which reads as "the night looks wrong" rather than as a missing layer.

**Two colour fields, and they are not interchangeable.** `color` is the authored filter, in
0..1; `finalColor` (photometric entries only) is the game's resolved value with intensity folded
in and is NOT normalised — the sample below reaches 243.4. Use `color` × blackbody(`temperature`)
for the light's HUE and `candela()` for its STRENGTH.

***Do not build a light from `intensity`.*** *(Corrected 2026-08-19, A6.9/D3 — this said "use
`color` × `intensity` × blackbody(`temperature`)", and a consumer that did got every light in the
city at the same brightness, three orders of magnitude too dim.)* `intensity` is authored **lux at
an unshipped distance** and is 10 on 627 of the 638 entries; `finalColor.a` is the resolved
candela and is what the renderer is handed. `candela()` is the whole rule.

Positions and rotations are **in the object frame**, in the bundle frame's axes; place them
through the same instance matrix as the mesh (`csiibundle.frames`).

**`parentMesh` is not a parent** *(corrected 2026-08-14, H10.)* This module used to say it
"says which sub-mesh of the prefab it is attached to, so it moves with that part", and so did
the contract. Every reader of the game's `Effect.m_ParentMesh` was swept: `EffectTransformSystem`
uses it only to pick which sub-mesh's colour the light takes (the `colorChannel` case),
`SubObjectSystem` as sub-object elevation flags, `BuildingInitializeSystem` for fire SFX. None
places the light. It is also not a plain index — a value above the prefab's sub-mesh count is a
procedural-bone connection id, and negatives other than -1 are unresolvable authored values
(-1001 occurs in the reference city). `bone_space` is the field that really does change where a
light goes; see it.
"""

from . import reader

# The discriminator the producer actually writes: a punctual (real) light carries
# `kind: "punctual"` unconditionally, an emissive colour slot carries no `kind` at all.
# CONTRACT.
PUNCTUAL_KIND = "punctual"

# The old discriminators, kept as a fallback. `type` is written only when the managed
# LightEffect prefab resolves, so keying on it alone silently DROPPED any punctual entry whose
# photometry could not be read — neither a light nor a colour slot, and gone from both lists.
# It never fired on the reference city (654 punctual entries, 654 with `type`), which is
# exactly why it was worth fixing before it did.
PHOTOMETRIC_MARKER = "type"
COLOUR_SLOT_MARKER = "purpose"

# `intensityUnit` values the producer writes. A consumer converting to its own renderer's units
# must branch on this rather than assume: a Lux spot and a Lumen point are different quantities.
INTENSITY_UNITS = ("Lux", "Lumen", "Candela", "Ev100", "Nits")


def load(bundle_dir):
    """`lights.json` as `{prefabId: [entry, …]}`, or `{}` when the layer is absent."""
    return reader.load_lights(bundle_dir)


def is_photometric(entry):
    """Whether this entry is a real light source to instantiate."""
    if not isinstance(entry, dict):
        return False
    return entry.get("kind") == PUNCTUAL_KIND or PHOTOMETRIC_MARKER in entry


def is_colour_slot(entry):
    """Whether this entry is emissive material state rather than a light."""
    return isinstance(entry, dict) and COLOUR_SLOT_MARKER in entry and not is_photometric(entry)


def bone_space(entry):
    """`(boneIndex, subMesh)` when this light's `position`/`rotation` are NOT object-frame.

    The one thing that changes what a punctual light's position means. The game's
    `EffectInitializeSystem` resolves `parentMesh` through `FindBoneIndex`, and on a match that
    method rewrites the position and rotation into the matched procedural bone's BIND-POSE space
    and records the bone. The producer emits `boneIndex` exactly in that case, so:

    * `None` — ordinary light, `position`/`rotation` are in the object frame. Place it there.
    * a pair — the position is bone-relative and the bundle does not carry the bone. Either
      compose the bind pose yourself or skip the light; placing it in the object frame puts it
      somewhere the game never draws it.

    On the reference city at most 4 of 654 punctual lights can take one (three bicycles, one
    park asset), so
    skipping is a defensible consumer choice — believing the object frame is not.
    """
    pair = entry.get("boneIndex") if isinstance(entry, dict) else None
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    return int(pair[0]), int(pair[1])


def split(entries):
    """One prefab's entry list as `(photometric, colour_slots)`.

    Anything matching neither shape is dropped from both — a future entry kind should be handled
    deliberately, not swept into whichever list happens to come first.
    """
    photometric = [e for e in entries or () if is_photometric(e)]
    slots = [e for e in entries or () if is_colour_slot(e)]
    return photometric, slots


def photometric_lights(catalogue):
    """Yield `(prefabId, entry)` for every real light source across the whole catalogue."""
    for prefab, entries in (catalogue or {}).items():
        for entry in entries or ():
            if is_photometric(entry):
                yield prefab, entry


def colour_slots(catalogue):
    """Yield `(prefabId, entry)` for every emissive slot across the whole catalogue."""
    for prefab, entries in (catalogue or {}).items():
        for entry in entries or ():
            if is_colour_slot(entry):
                yield prefab, entry


def candela(entry):
    """A punctual light's resolved intensity in **candela**, or `None` if it cannot be had.

    **`intensity` is not it, and the bundle's own `intensityUnit` says so: every one of the 638
    entries on the reference city is `Lux`, and lux is an illuminance at a distance, not a light.
    627 of them carry the same authored `intensity` of 10.** What varies is
    `LightEffect.m_LuxAtDistance`, which the bundle does not ship — so reconstructing the light
    from `intensity` alone gives one number for the whole city.

    `finalColor.a` IS the resolved value. Decompiled (`Game.Prefabs.Effects.LightEffect`):

        ComputeLightIntensity()  => LightUtils.ConvertLuxToCandela(m_Intensity, m_LuxAtDistance)
        ComputeLightFinalColor() => m_Color.linear * ComputeLightIntensity()
                                    * CorrelatedColorTemperatureToRGB(m_ColorTemperature)
                                    * m_LightDimmer

    Unity multiplies all FOUR channels of a `Color`, and the temperature term's alpha is 1, so
    the alpha that survives is `intensity_cd * m_Color.a * m_LightDimmer` — the candela the
    renderer is actually handed. Verified against the file: `intensity 10, range 5` gives
    `finalColor.a 250` = 10 x 5^2, CS2's own default `m_LuxAtDistance = 5`.

    `intensityScale` (the per-placement `Effect.m_Intensity`) multiplies it, exactly as
    `LightCullingSystem` does: `m_Color = m_Scale * m_Intensity`, where `m_Scale` is
    `finalColor.rgb`.

    Percentiles over the reference city's 176,799 placed lights: p1 36, p50 250, p90 1,562,
    p99 2,890 cd — a wall lamp 48, a garden bollard 90, a street light 1,562, a stadium 16,000.

    The authored `intensity` is the fallback and only the fallback: it is the right number when
    the unit really is `Candela`, and a wrong-by-orders-of-magnitude one otherwise, which is why
    it is used only when `finalColor` is absent.
    """
    if not isinstance(entry, dict):
        return None
    scale = float(entry.get("intensityScale") or 1.0)
    final = entry.get("finalColor")
    if isinstance(final, (list, tuple)) and len(final) >= 4:
        return max(0.0, float(final[3])) * scale
    intensity = entry.get("intensity")
    if intensity is None:
        return None
    return max(0.0, float(intensity)) * scale


# CS2 keys a colour slot by its RENDER prefab (`<object>_Mesh`); every other layer of the bundle
# — `meshes/`, `instances.jsonl`'s `assetId`, `materials.json` — keys by the OBJECT prefab.
# 959 of the reference city's 960 slot-carrying keys carry the suffix and 781 of them resolve to
# a `.glb` on disk.
_MESH_SUFFIX = "_Mesh"


def slot_asset_id(prefab_key):
    """The `assetId` (mesh/instance key) a colour slot's `lights.json` key refers to."""
    if prefab_key and prefab_key.endswith(_MESH_SUFFIX):
        return prefab_key[: -len(_MESH_SUFFIX)]
    return prefab_key


def slots_by_asset(catalogue):
    """`{assetId: [slot, ...]}` — every colour slot, keyed the way the rest of the bundle is
    keyed and ordered by `slot`, which is the `ProceduralLight` buffer index.

    **The buffer's order is not arbitrary and a consumer joining on it needs to know why.**
    `Game.Prefabs.MeshSystem` fills it MULTI lights first (`EmissiveProperties.m_MultiLights`,
    which name a `layerId`) and SINGLE lights after (`m_SingleLights`, which name a
    `materialId`), so `slot` is a position in that concatenation. The bundle ships neither id
    today — `AppearanceRipSystem.ReadLights` writes `slot: i` and nothing else — so a consumer
    joins by ordinal against the mesh's emissive materials and declares the residue.
    """
    out = {}
    for prefab, entries in (catalogue or {}).items():
        slots = [e for e in entries or () if is_colour_slot(e)]
        if slots:
            out.setdefault(slot_asset_id(prefab), []).extend(slots)
    for _key, slots in out.items():
        slots.sort(key=lambda e: int(e.get("slot") or 0))
    return out


def slot_emission(entry):
    """`(rgb, nits_unscaled)` for one colour slot — its LIT colour and CS2's own intensity.

    `color` is the lit state and `colorOff` the unlit one; **`colorOff` is black on all 1,896
    slots of the reference city**, so "off" really is off and a consumer needs only the lit half.
    Both share an alpha, and the alpha is the intensity: `MeshSystem` writes
    `m_Color = float4(color.linear.rgb, mapping.intensity * kIntensityMultiplier)` with
    `kIntensityMultiplier = 100f`, and `ProceduralEmissiveSystem` resolves the pair as
    `lerp(m_Color, m_Color2, state); value.w *= state.m_Intensity`.

    The returned intensity is CS2's raw `intensity x 100` in its own units — 5 or 20 for a
    NeonSign, 500 for a DecorativeLight, 60 for an EffectSource, 0 on the 111 slots that are
    authored to emit nothing. Converting it to a physical unit is the consumer's job and is
    declared where it happens.
    """
    if not isinstance(entry, dict):
        return None
    colour = entry.get("color")
    if not isinstance(colour, (list, tuple)) or len(colour) < 4:
        return None
    return tuple(max(0.0, float(c)) for c in colour[:3]), max(0.0, float(colour[3]))


def cone_angles(entry):
    """`(outer, inner)` cone angles in degrees for a spot light, or `None` for anything else.

    `innerSpotPercentage` is a PERCENTAGE of the outer angle, not an angle — reading it as
    degrees gives a hard-edged cone on every light in the city.
    """
    if entry.get("type") != "Spot" or "spotAngle" not in entry:
        return None
    outer = float(entry["spotAngle"])
    inner = outer * float(entry.get("innerSpotPercentage", 100.0)) / 100.0
    return outer, inner
