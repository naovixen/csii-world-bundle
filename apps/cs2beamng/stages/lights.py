"""Stage `lights` — CS2's punctual lights as BeamNG `PointLight`/`SpotLight` objects.

`lights.json` is keyed by PREFAB, so one entry becomes as many lights as the city has copies of
that prefab: 214 prefabs carry a punctual light and 142,421 placed instances reference them, for
**176,799 lights** on the reference city.

**Neither engine caps at author time, and the cap here is a scene-graph budget only.** CS2 keeps
every one of its lights in the world and draws at most `RenderingSystem.maxLightCount` = 2,048
per frame, nearest-first by a range-relative distance with an adaptive reject distance
(`Game.Effects.LightCullingSystem`). BeamNG 0.39 does the same job in the engine — clustered
shading with frustum, size, occlusion and importance culling, and a flare sprite in place of what
it drops. So this stage places as many as the level format wants to carry (`lights_max`, default
`LIGHTS_MAX_DEFAULT`) in the order BeamNG's own importance cull would keep them, and says in the
report what it left out and how faint the faintest kept light was.

What is ported and what is not:

* **Ported.** The frame: `position`/`rotation` are OBJECT-frame, so they compose with the same
  instance transform the mesh takes; `rotationMatrix` goes through `frame.basis_flat` (A6.8).
* **Ported, exactly.** The photometry — `csiibundle.lights.candela` for the strength (CS2's own
  resolved `finalColor.a`, NOT the authored Lux, which is 10 on 627 of the 638 entries), written
  into the field of BeamNG's class-specific unit (`cd` for a spot, `4*pi*cd` lumens for a point)
  together with the `brightness` the renderer actually uses; `color` and `temperature` through
  BeamNG's own `useColorTemperature`/`colorTemperatureKelvin`; and a spot's `spotAngle` /
  `innerSpotPercentage` through `csiibundle.lights.cone_angles`, because reading the percentage
  as degrees hard-edges every cone.
* **Skipped, counted.** Entries with a `boneIndex` — their position is in a procedural bone's
  bind-pose space and the bundle does not carry the bone (`lights.bone_space`); placing them in
  the object frame would put them somewhere the game never draws them.
* **Not here, and no longer nowhere.** The 1,896 **colour slots** — neon signs, traffic signals,
  lit windows — are emissive material state, not light sources, and `csiibundle.bake.materials`
  now joins them to each mesh's emissive materials (A6.9/D5). What is still approximated is the
  day/night switch, and the report says why.

**The lights are night lights** (`nightLight`, BeamNG's own mechanism and vanilla's convention on
3,063 of its 5,033), so `environment.lua` disables them outside `(0.25, 0.75)` of the day — and
the level ships `play: false` like every vanilla level, so the built hour is the only hour they
are ever judged at. The stage logs that when the hour is a daytime one; `tod_hour` is the knob.
"""

import hashlib
import math
import time

from csiibundle import instances as bundle_instances
from csiibundle import lights as bundle_lights

from ..beamng import frame, placement
from . import Stage, level_of

STAGE_KEY = "lights"

# A light is expensive in BeamNG and the point of the cap is FPS, so none of ours casts a
# shadow. OURS — CS2's own lights do cast, and the divergence is declared in the report.
CAST_SHADOWS = False

# BeamNG's OWN sync constant between the two photometric fields a light carries, measured over
# all 5,033 lights in its 21 shipped levels and exact to the last digit on every one of the 4,047
# that carry both:
#
#     SpotLight   intensity / brightness = 5,000        (intensity in candela)
#     PointLight  intensity / brightness = 62,831.86    (= 4*pi * 5,000, intensity in lumens)
#
# The ratio is fixed by the CLASS, not by `intensityUnit` — a SpotLight labelled `lm` still
# stores candela, because the label is the editor's display unit and the stored number is the
# engine's. `brightness` is the value the renderer actually uses (`editor/inspector.lua`: "the
# actual render value"); a light with an `intensity` and no `brightness` has no brightness.
BRIGHTNESS_PER_CANDELA = 1.0 / 5000.0

# Vanilla's own medians, for the report to measure ours against: SpotLight 15,000 cd /
# brightness 3.0 / range 15 m; PointLight 5,000 lm / brightness 0.0796.
VANILLA_SPOT_CANDELA = 15000.0

# The `intensityUnit` strings BeamNG's editor accepts (`editor/lightClassConverter.lua`).
# We shipped "Lux" — CS2's unit, and not one of these — on all 500.
UNIT_CANDELA = "cd"
UNIT_LUMEN = "lm"

# How many of the city's punctual lights the level gets. NEITHER ENGINE CAPS AT AUTHOR TIME —
# CS2 keeps all 176,799 in the world and draws at most `RenderingSystem.maxLightCount` (2,048 at
# the default quality) per frame, nearest-first by a range-relative distance
# (`Game.Effects.LightCullingSystem`); BeamNG 0.39 does the same job in the engine, with frustum,
# size (`minLightScreenSize`), occlusion and importance culling plus flare substitution for what
# it drops (`core/settings/clusteredQuality.lua`). So the only thing an author-time cap buys is
# scene-graph cost, and it is measurable: the last load put 482 SpotLights up in 0.090 s, i.e.
# 0.19 ms each, against 8 ms for a TSStatic.
#
# 25,000 is the KNEE of the reference city's importance ranking, not a round number: it is the
# smallest cap that takes every one of the 17,847 street lights (`StreetlightSingle01/02/03` plus
# the commercial and industrial poles), and 50,000 is where the 43,282 palm uplights — 24 % of
# the city's lights and none of them lighting a road — start arriving. It costs ~4.7 s of load.
# A5's 500 placed no street light at all.
#
# The ladder, measured, for a machine that cannot take it: 500 (industrial poles only) /
# 5,000 (all commercial poles) / 25,000 (all street lighting) / 50,000 / 176,799 (everything).
LIGHTS_MAX_DEFAULT = 25000


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

    cap = max(0, int(settings.get("lights_max", LIGHTS_MAX_DEFAULT) or 0))
    candidates = []
    progress.begin(
        "lights: finding lit instances", total=units(ctx.census, settings), unit="instances"
    )
    scanned = 0
    # the road-clearance deviation, as the shapes stage computed it — a light parented to a tree
    # that was not placed must not be placed either, which is the whole point of one admission
    # test for every pass
    dropped = set((ctx.shared.get("shapes") or {}).get("droppedRecords") or ())
    for record in bundle_instances.read_instances(ctx.bundle):
        scanned += 1
        if scanned % 25000 == 0:
            progress.advance(25000)
            progress.check()
        entries = by_prefab.get(record["assetId"])
        if not entries:
            continue
        if not placement.keep(record, settings, ctx.region, dropped):
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

    faintest = min(
        (bundle_lights.candela(e) or 0.0 for _s, _r, e, _i in candidates[:cap]), default=0.0
    )
    # `nightLight` is BeamNG's own mechanism and vanilla uses it on 3,063 of its 5,033 lights, but
    # it means the level's CLOCK decides whether any of this is visible: `environment.lua` enables
    # the set only while `time` is inside (0.25, 0.75), and every vanilla level — ours included —
    # ships `play: false`, so the shipped hour is the only hour the level is ever seen at until
    # somebody moves the slider. Say so, with the hour, rather than leaving it to a screenshot.
    hour = float((ctx.shared.get("scene") or {}).get("todHour") or -1.0)
    night = 18.0 <= hour or hour < 6.0 if hour >= 0.0 else None
    if night is False:
        progress.log(
            f"lights: {placed:,} placed, and NONE of them will be lit — they are night "
            f"lights (BeamNG's `nightLight`, vanilla's own mechanism) and the level ships "
            f"at {int(hour):02d}:{int(round((hour % 1) * 60)):02d}. Move the time past "
            f"18:00 in game, or build with `tod_hour 21`, to see them."
        )

    return {
        "lights": placed,
        "cityLights": len(candidates),
        "cap": cap,
        "skippedBoneSpace": skipped_bone,
        "faintestPlacedCandela": round(faintest, 1),
        "nightLights": placed,
        "levelHour": round(hour, 2) if hour >= 0.0 else None,
        "litAtLevelHour": night,
        "seconds": round(time.monotonic() - started, 1),
        "approximations": {
            "lightCap": {
                "what": f"{placed:,} of the city's {len(candidates):,} punctual lights are placed, "
                f"the faintest at {faintest:,.0f} cd",
                "why": "NEITHER engine caps at author time - CS2 keeps all of them and draws up to "
                "RenderingSystem.maxLightCount (2,048) per frame, BeamNG culls by frustum, "
                "size, occlusion and importance in the engine - so this is a scene-graph "
                "budget, not a rendering one. The subset is ranked by BeamNG's own "
                "importance metric (luminance x brightness x range) and tie-broken by a "
                "stable hash, so a bigger cap is a superset; at a SMALL cap it is the "
                "brightest classes, which are not spread evenly over the map",
                "count": max(0, len(candidates) - placed),
            },
            "lightsAreNightOnly": {
                "what": "every placed light carries `nightLight` and is disabled by day",
                "why": "BeamNG's own mechanism and vanilla's convention (3,063 of its 5,033); CS2 "
                "decides per light instead. The level's clock does not move, so the built "
                "hour is the only hour these are judged at - `tod_hour` is the knob",
                "count": placed,
            },
            "lightShadows": {
                "what": "no placed light casts a shadow",
                "why": "CS2's do; a shadow-casting light costs a render pass and the cap exists "
                "for FPS",
                "count": placed,
            },
            "emissiveSlotsAlwaysOn": {
                "what": f"the {slots:,} colour slots are applied to their materials by "
                "`bake.materials` but never switch off - CS2 lerps each between `color` "
                "and `colorOff` (which is BLACK on all 1,896), we emit the lit half at "
                "every hour",
                "why": "BeamNG's counterpart is a TSStatic `nightEmissive`, which drives "
                "`instanceColor` slot 0 - and MEASURED, all 639 shapes the slot join "
                "reaches carry a `colorPaletteMap` material, so slot 0 is already the "
                "colour palette's own channel on every one of them and writing (0,0,0) "
                "into it by day would black out the building tint instead",
                "count": slots,
            },
        },
    }


def _load(bundle_dir):
    catalogue = bundle_lights.load(bundle_dir)
    if (
        isinstance(catalogue, dict)
        and "prefabs" in catalogue
        and isinstance(catalogue["prefabs"], dict)
    ):
        return catalogue["prefabs"]
    return catalogue or {}


def _importance(entry):
    """BeamNG's own importance-cull metric, with the two camera terms removed.

    The engine's tooltip states the metric exactly (`Bin64/BeamNG.drive.x64.exe`): *"drop lights
    whose **color times brightness times the camera exposure times the apparent screen size**
    falls below the floor. catches far away and daylight washed out lights that add nothing
    visible, e.g. lamps on a distant bridge, while keeping near or bright ones."*

    Exposure is one number for the whole frame and cancels. Distance is the only other unknown,
    and apparent screen size is `range / distance`, so at any fixed distance the ranking between
    two lights is `luminance(colour) x brightness x range` — and brightness is candela over a
    constant. That is this function, and it is the closest an author-time subset can get to the
    order the engine itself would drop them in.

    **It is also a correction.** A5 ranked by reach alone, which put the city's 43,282 palm
    uplights (range 30 m, 250 cd) ahead of its 17,847 street lights (range 18 m, 1,562 cd): at a
    cap of 500 not one street light was placed. Measured on the reference city, ranked this way:

        cap    500  ->  the industrial poles and a stadium, min 2,890 cd
        cap  5,000  ->  the commercial poles as well,       min 1,440 cd
        cap 25,000  ->  EVERY street light in the city,     min   900 cd
        cap 50,000  ->  the palm uplighting starts,         min   250 cd
    """
    cd = bundle_lights.candela(entry) or 0.0
    colour = entry.get("color") or (1.0, 1.0, 1.0, 1.0)
    luminance = min(
        1.0,
        max(0.0, 0.2126 * float(colour[0]) + 0.7152 * float(colour[1]) + 0.0722 * float(colour[2])),
    )
    return luminance * cd * BRIGHTNESS_PER_CANDELA * max(0.0, float(entry.get("range") or 0.0))


def _priority(record, entry, index):
    """Sort key: smaller wins a slot. `-importance` first, so the cap keeps the lights the engine
    itself would keep; a stable per-instance hash breaks the ties, and every light of one prefab
    ties, so a smaller cap is always a SUBSET of a larger one."""
    digest = hashlib.blake2b(f"{record.get('id')}:{index}".encode(), digest_size=8).digest()
    tie = int.from_bytes(digest, "little") / float(1 << 64)
    return (-_importance(entry), tie)


def _light_object(level, record, entry, index):
    """One BeamNG light object for a light entry on a placed record."""
    position, rotation = _world(record, entry)
    colour = [float(c) for c in (entry.get("color") or (1.0, 1.0, 1.0, 1.0))]
    while len(colour) < 4:
        colour.append(1.0)
    # THE PHOTOMETRY. `entry["intensity"]` is not it: it is CS2's authored LUX at a distance the
    # bundle does not ship, it is 10 on 627 of the 638 catalogue entries, and it is what A5 wrote
    # straight into BeamNG's field — 10 cd against vanilla's median 15,000. `bundle_lights.candela`
    # is the game's own resolved value (see it), and BeamNG stores it in the CLASS's unit.
    cd = bundle_lights.candela(entry)
    if cd is None:
        return None
    common = {
        "name": f"cs2_light_{record['id']}_{index}",
        "position": [round(v, 3) for v in position],
        "color": [round(v, 6) for v in colour],
        "castShadows": CAST_SHADOWS,
        "brightness": round(cd * BRIGHTNESS_PER_CANDELA, 6),
        # Vanilla writes `isEnabled: false` on 2,987 of its 3,063 night lights, and it is not
        # cosmetic: `environment.lua` only refreshes the set on a day/night TRANSITION, so a
        # light shipped enabled stays lit from load until the first one.
        "isEnabled": False,
        "nightLight": "1",
        "attenuationRatio": [1, 0, 0],
    }
    temperature = entry.get("temperature")
    if temperature:
        common["useColorTemperature"] = True
        common["colorTemperatureKelvin"] = round(float(temperature), 1)
    cone = bundle_lights.cone_angles(entry)
    if entry.get("type") == "Spot" and cone:
        outer, inner = cone
        common.update(
            {
                "class": "SpotLight",
                "outerAngle": round(outer, 3),
                "innerAngle": round(inner, 3),
                "range": round(float(entry.get("range") or 10.0), 3),
                # a SpotLight stores CANDELA
                "intensity": round(cd, 4),
                "intensityUnit": UNIT_CANDELA,
                "rotationMatrix": [round(v, 6) for v in rotation],
            }
        )
        return common
    if entry.get("type") not in ("Spot", "Point"):
        return None
    # a PointLight stores LUMENS: an isotropic source of `cd` candela emits `4*pi*cd` lumens,
    # which is why the vanilla ratio is 4*pi times the spot's and why `brightness` is the same
    # number for both.
    common.update(
        {
            "class": "PointLight",
            "radius": round(float(entry.get("range") or 10.0), 3),
            "intensity": round(cd * 4.0 * math.pi, 4),
            "intensityUnit": UNIT_LUMEN,
        }
    )
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
    world = [
        base_pos[axis] + sum(base_rot[axis][k] * base_scale[k] * local_beamng[k] for k in range(3))
        for axis in range(3)
    ]
    local_rot = _swap_yz(_quat_rows(entry.get("rotation") or (0.0, 0.0, 0.0, 1.0)))
    combined = [
        [sum(base_rot[r][k] * local_rot[k][c] for k in range(3)) for c in range(3)]
        for r in range(3)
    ]
    return world, frame.basis_flat(combined)


# `placement.keep` decides which instances are lit, so this stage inherits its whole admission
# surface - the kind toggles and the two density sliders included.
INPUTS = ("layer:lights", "layer:instances")
SETTINGS = (
    "lights",
    "lights_max",
    "region",
    "trees",
    "props",
    "buildings",
    "foliage_density",
    "prop_density",
    "foliage_road_clearance",
)

# version 2: `rotationMatrix` through `frame.basis_flat` - the field holds the object's own axes
# in groups of three, so flattening the matrix row-major aimed every spot light by the INVERSE of
# its rotation (A6.8)
# version 3: the photometry (A6.9/D3) - `intensity` in the CLASS's own unit from CS2's resolved
# candela plus the `brightness` BeamNG actually renders with, in place of the authored Lux; the
# cap ranks by BeamNG's own importance metric instead of reach alone and defaults to 25,000
# instead of 500; `isEnabled: false` beside `nightLight`, as vanilla writes it
STAGE = Stage(
    STAGE_KEY,
    "Street and building lights",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    after=("shapes", "scene"),
    version=3,
)
