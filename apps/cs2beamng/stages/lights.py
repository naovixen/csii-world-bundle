"""Stage `lights` — CS2's punctual lights as BeamNG `PointLight`/`SpotLight` objects.

`lights.json` is keyed by PREFAB, so one entry becomes as many lights as the city has copies of
that prefab: 214 prefabs carry a punctual light and 142,421 placed instances reference them, for
**176,799 lights** on the reference city. BeamNG cannot have those — the plan budgets 500, and
the setting is off by default — so this stage places a deterministic, weighted subset and says
in the report exactly what fraction that was.

What is ported and what is not:

* **Ported.** The entry's own photometry — `type`, `range`, `intensity` with its `intensityUnit`,
  `temperature` (through BeamNG's own `useColorTemperature`/`colorTemperatureKelvin`), `color`,
  and a spot's `spotAngle` / `innerSpotPercentage`. `csiibundle.lights.cone_angles` is what turns
  the percentage into BeamNG's `innerAngle`, because reading it as degrees hard-edges every cone.
* **Ported.** The frame: `position`/`rotation` are OBJECT-frame, so they compose with the same
  instance transform the mesh takes.
* **Skipped, counted.** Entries with a `boneIndex` — their position is in a procedural bone's
  bind-pose space and the bundle does not carry the bone (`lights.bone_space`); placing them in
  the object frame would put them somewhere the game never draws them.
* **Not in this stage, counted.** The 1,896 **colour slots** — neon signs, traffic signals,
  brake lamps. They are emissive material state, not light sources, and BeamNG's counterpart is
  a material's `nightEmissive`, not an object. Filed in the parity backlog.
"""

import hashlib
import time

from csiibundle import instances as bundle_instances, lights as bundle_lights

from ..beamng import placement
from . import Stage, level_of

STAGE_KEY = "lights"

# A light is expensive in BeamNG and the point of the cap is FPS, so none of ours casts a
# shadow. OURS — CS2's own lights do cast, and the divergence is declared in the report.
CAST_SHADOWS = False

# Which lights survive the cap first. A street light with a long reach earns its slot over a
# car's headlight; the weight multiplies a stable per-instance hash, so a smaller cap is always
# a SUBSET of a larger one. OURS, and the same shape as `instances.density_keep`.
RANGE_REFERENCE = 30.0


def enabled(settings):
    return bool(settings.get("lights", False))


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("instances") or 0) or None


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    catalogue = _load(ctx.bundle)
    by_prefab = {}
    skipped_bone = 0
    for prefab, entry in bundle_lights.photometric_lights(catalogue):
        if bundle_lights.bone_space(entry):
            skipped_bone += 1
            continue
        by_prefab.setdefault(prefab, []).append(entry)
    if not by_prefab:
        progress.log("lights: the bundle carries no punctual lights")
        return {"lights": 0, "seconds": round(time.monotonic() - started, 1)}

    cap = max(0, int(settings.get("lights_max", 500) or 0))
    candidates = []
    progress.begin("lights: finding lit instances", total=units(ctx.census, settings), unit="instances")
    scanned = 0
    for record in bundle_instances.read_instances(ctx.bundle):
        scanned += 1
        if scanned % 25000 == 0:
            progress.advance(25000)
            progress.check()
        entries = by_prefab.get(record["assetId"])
        if not entries:
            continue
        if not placement.keep(record, settings, ctx.region):
            continue
        for index, entry in enumerate(entries):
            candidates.append((_priority(record, entry, index), record, entry, index))
    progress.end(f"{len(candidates):,} lights in the city, cap {cap}")

    candidates.sort(key=lambda row: row[0])
    placed = 0
    slots = sum(1 for _p, _e in bundle_lights.colour_slots(catalogue))
    progress.begin("lights: placing", total=min(cap, len(candidates)), unit="lights")
    for _score, record, entry, index in candidates[:cap]:
        obj = _light_object(level, record, entry, index)
        if obj is None:
            continue
        level.add(["Lights"], obj, key=f"light:{record['id']}:{index}:{placed}")
        placed += 1
        progress.advance(1)
    progress.end(f"{placed:,} lights placed")
    level.write_objects()

    return {
        "lights": placed,
        "cityLights": len(candidates),
        "cap": cap,
        "skippedBoneSpace": skipped_bone,
        "seconds": round(time.monotonic() - started, 1),
        "approximations": {
            "lightCap": {
                "what": f"{placed:,} of the city's {len(candidates):,} punctual lights are placed",
                "why": "a dynamic light is BeamNG's most expensive per-frame object; the subset is "
                       "deterministic and weighted by reach, so a bigger cap is a superset",
                "count": max(0, len(candidates) - placed),
            },
            "lightShadows": {
                "what": "no placed light casts a shadow",
                "why": "CS2's do; a shadow-casting light costs a render pass and the cap exists "
                       "for FPS",
                "count": placed,
            },
            "emissiveColourSlots": {
                "what": "the bundle's colour slots (neon signs, traffic signals, brake lamps) are "
                        "not applied to any material",
                "why": "they are emissive material state, not light sources; BeamNG's counterpart "
                       "is a material nightEmissive and it is filed in the parity backlog",
                "count": slots,
            },
        },
    }


def _load(bundle_dir):
    catalogue = bundle_lights.load(bundle_dir)
    if isinstance(catalogue, dict) and "prefabs" in catalogue and isinstance(catalogue["prefabs"], dict):
        return catalogue["prefabs"]
    return catalogue or {}


def _priority(record, entry, index):
    """Sort key: smaller wins a slot. A stable hash divided by the light's reach, so a long
    street light survives a smaller cap and the subset is stable across builds."""
    digest = hashlib.blake2b(f"{record.get('id')}:{index}".encode("utf-8"), digest_size=8).digest()
    unit = int.from_bytes(digest, "little") / float(1 << 64)
    reach = max(1.0, float(entry.get("range") or 1.0))
    return unit * (RANGE_REFERENCE / reach)


def _light_object(level, record, entry, index):
    """One BeamNG light object for a light entry on a placed record."""
    position, rotation = _world(record, entry)
    colour = [float(c) for c in (entry.get("color") or (1.0, 1.0, 1.0, 1.0))]
    while len(colour) < 4:
        colour.append(1.0)
    common = {
        "name": f"cs2_light_{record['id']}_{index}",
        "position": [round(v, 3) for v in position],
        "color": [round(v, 6) for v in colour],
        "castShadows": CAST_SHADOWS,
        "intensity": round(float(entry.get("intensity") or 1.0), 4),
        "isEnabled": True,
        "nightLight": "1",
        "attenuationRatio": [1, 0, 0],
    }
    unit = entry.get("intensityUnit")
    if unit:
        common["intensityUnit"] = str(unit)
    temperature = entry.get("temperature")
    if temperature:
        common["useColorTemperature"] = True
        common["colorTemperatureKelvin"] = round(float(temperature), 1)
    cone = bundle_lights.cone_angles(entry)
    if entry.get("type") == "Spot" and cone:
        outer, inner = cone
        common.update({"class": "SpotLight", "outerAngle": round(outer, 3), "innerAngle": round(inner, 3),
                       "range": round(float(entry.get("range") or 10.0), 3),
                       "rotationMatrix": [round(v, 6) for v in rotation]})
        return common
    if entry.get("type") not in ("Spot", "Point"):
        return None
    common.update({"class": "PointLight", "radius": round(float(entry.get("range") or 10.0), 3)})
    return common


def _world(record, entry):
    """The light's world position and 3x3 row-major rotation in BeamNG axes.

    The entry's `position`/`rotation` are in the OBJECT frame, so they compose with the record's
    own transform; `stages.instances._transform` is the same conjugation, applied here to the
    composed matrix rather than to the record alone.
    """
    from .instances import _quat_rows, _swap_yz, _transform

    base_pos, base_rot, base_scale = _transform(record)
    local = entry.get("position") or (0.0, 0.0, 0.0)
    local_beamng = (float(local[0]), float(local[2]), float(local[1]))
    world = [base_pos[axis]
             + sum(base_rot[axis][k] * base_scale[k] * local_beamng[k] for k in range(3))
             for axis in range(3)]
    local_rot = _swap_yz(_quat_rows(entry.get("rotation") or (0.0, 0.0, 0.0, 1.0)))
    combined = [[sum(base_rot[r][k] * local_rot[k][c] for k in range(3)) for c in range(3)]
                for r in range(3)]
    return world, [combined[0][0], combined[0][1], combined[0][2],
                   combined[1][0], combined[1][1], combined[1][2],
                   combined[2][0], combined[2][1], combined[2][2]]


# `placement.keep` decides which instances are lit, so this stage inherits its whole admission
# surface - the kind toggles and the two density sliders included.
INPUTS = ("layer:lights", "layer:instances")
SETTINGS = ("lights", "lights_max", "region", "trees", "props", "buildings",
            "foliage_density", "prop_density")

STAGE = Stage(STAGE_KEY, "Street and building lights", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS)
