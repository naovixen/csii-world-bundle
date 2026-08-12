"""Lights (§4.3) — and the fact that `lights.json` holds TWO different kinds of record.

One file, keyed by prefab, whose values are lists of entries — but the entries are not all the
same thing, and a consumer that treats them uniformly will either create 2,532 lights where the
game has 654, or create none at all. On the reference city:

* **654 photometric lights** — a real light source to instantiate. Carries `type` (`Spot` or
  `Point`), `position`, `rotation`, `range`, `intensity` + `intensityUnit`, `temperature`,
  `color`, and for a spot `spotAngle` / `innerSpotPercentage` / `spotShape`. `parentMesh` says
  which sub-mesh of the prefab it is attached to, so it moves with that part.
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
in and is NOT normalised — the sample below reaches 243.4. Use `color` × `intensity` ×
blackbody(`temperature`) to build a light, and treat `finalColor` as the producer's own
cross-check.

Positions and rotations are prefab-local, in the bundle frame; place them through the same
instance matrix as the mesh (`csiibundle.frames`).
"""

from . import reader

# Fields that only a real light source carries, and only an emissive slot carries. Presence is
# the discriminator — the file has no type tag. CONTRACT.
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
    return isinstance(entry, dict) and PHOTOMETRIC_MARKER in entry


def is_colour_slot(entry):
    """Whether this entry is emissive material state rather than a light."""
    return isinstance(entry, dict) and COLOUR_SLOT_MARKER in entry and not is_photometric(entry)


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
