"""Stage `scene` — `LevelInfo`, `TimeOfDay`, `ScatterSky`, the spawn point and `info.json`.

* `LevelInfo.visibleDistance` is the setting `view_distance` (the global far cull, the single
  biggest performance lever the engine gives us); fog density from `grading.fog.meanFreePath`
  (`1 / meanFreePath` — an exponential attenuation over the same distance), height from its
  `maximumHeight`, colour a daylight haze.
* `TimeOfDay` from `grading.lighting.time` (or the `tod_hour` setting): BeamNG's `time` is
  `(hour/24 − 0.5) % 1` — **0 is noon, 0.5 midnight** (`environment.lua`; the first load
  came up at 01:20 because of the naive fraction); the map's latitude/longitude; `play` off so
  the sky stays put.
* `ScatterSky` azimuth/elevation from the sun's own direction vector (`Lighting.sun.toward`,
  bundle frame → azimuth clockwise from north = `atan2(east, north)`), sun colour from the
  light's filter × blackbody when the game used a colour temperature. Everything else is
  BeamNG's `small_island` sky. If TimeOfDay overrides the sun the numbers still describe the
  same moment.
* Spawn: the master, drivable, car lane whose midpoint is nearest the region centre (or, for a
  whole city, the median of all such midpoints — the city's centre of mass, which is on a
  road by construction), 0.5 m above the lane, heading along it. BeamNG faces the vehicle
  along the sphere's +Y (`beamng.frame.heading_matrix`).
"""

import math
import os
import time

import numpy

from csiibundle import colour as colour_mod, lanegraph, reader

from ..beamng import frame
from . import Stage, level_of

STAGE_KEY = "scene"
SPAWN_NAME = "spawn_city"
SPAWN_LIFT = 0.5


def enabled(settings):
    return True


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("lanes") or 1)


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    manifest = reader.load_manifest(ctx.bundle) or {}
    grading = colour_mod.load(ctx.bundle)
    lighting = getattr(grading, "lighting", None) or colour_mod.Lighting(None)
    fog = (getattr(grading, "raw", None) or {}).get("fog") if grading is not None else None
    if fog is None and grading is not None:
        fog = getattr(grading, "fog", None)
    fog = fog if isinstance(fog, dict) else {}

    # -- LevelInfo -------------------------------------------------------------------------
    progress.begin("scene: level info", total=None, unit="")
    view = float(settings.get("view_distance") or 4000)
    mean_free = float(fog.get("meanFreePath") or 6900.0)
    fog_density = 1.0 / max(mean_free, 100.0)
    fog_height = float(fog.get("maximumHeight") or 1000.0)
    tint = fog.get("tint") or [1.0, 1.0, 1.0]
    haze = [0.66, 0.89, 1.0]
    fog_color = [round(min(1.0, haze[i] * float(tint[i]) if i < len(tint) else haze[i]), 4) for i in range(3)] + [1]
    level.add(["Level_objects"], {
        "name": "theLevelInfo", "class": "LevelInfo", "canvasClearColor": "black", "enabled": "1",
        "visibleDistance": view, "fogDensity": round(fog_density, 7), "fogAtmosphereHeight": round(min(fog_height, 3000.0), 1),
        "fogColor": fog_color, "gravity": -9.81, "nearClip": 0.1,
    })
    progress.end(f"visibleDistance {view:.0f} m, fog 1/{mean_free:.0f} m")

    # -- TimeOfDay + ScatterSky ------------------------------------------------------------
    progress.begin("scene: sky", total=None, unit="")
    clock = lighting.time or {}
    hour = float(clock.get("hour", 12) or 0) + float(clock.get("minute", 0) or 0) / 60.0
    override = settings.get("tod_hour")
    if override is not None and float(override) >= 0.0:
        hour = float(override) % 24.0
    # BeamNG's TimeOfDay: 0 = NOON, 0.5 = midnight (environment.lua: time = (s/86400 - 0.5) % 1)
    tod_time = (hour / 24.0 - 0.5) % 1.0
    lat = float(clock.get("latitude") or manifest.get("geo", {}).get("latitude") or 45.0)
    lon = float(clock.get("longitude") or manifest.get("geo", {}).get("longitude") or 0.0)
    day = int(clock.get("day") or 172)
    level.add(["Level_objects"], {
        "name": "tod", "class": "TimeOfDay", "position": [0, 0, 0], "animate": "0", "play": False,
        "axisTilt": 23.44, "day": day, "latitude": round(lat, 4), "longitude": round(lon, 4),
        "startTime": round(tod_time, 4), "time": round(tod_time, 4), "utcOffset": "0", "dstRule": "off", "version": 2,
    })
    key = lighting.key_light
    azimuth, elevation = 210.0, 45.0
    if key is not None and key.toward is not None:
        tx, ty, tz = key.toward                      # bundle frame: x east, y up, z north
        azimuth = math.degrees(math.atan2(tx, tz)) % 360.0
        elevation = math.degrees(math.asin(max(-1.0, min(1.0, ty))))
    sun_rgb = list(lighting.sun.color) if lighting.sun.enabled else [1.0, 1.0, 1.0]
    if lighting.sun.enabled and lighting.sun.use_color_temperature:
        bb = colour_mod.blackbody(lighting.sun.temperature) if hasattr(colour_mod, "blackbody") else (1.0, 1.0, 1.0)
        sun_rgb = [sun_rgb[i] * bb[i] for i in range(3)]
    peak = max(sun_rgb) or 1.0
    sun_scale = [round(c / peak, 4) for c in sun_rgb] + [1]
    level.add(["Level_objects"], {
        "name": "sunsky", "class": "ScatterSky", "position": [0, 0, 0],
        "azimuth": round(azimuth, 3), "elevation": round(max(1.0, elevation), 3),
        "sunScale": sun_scale, "sunScaleGradientFile": "art/sky_gradients/default/gradient_sunscale.png",
        "ambientScale": [1, 0.894, 0.78, 1], "ambientScaleGradientFile": "art/sky_gradients/default/gradient_ambient.png",
        "colorize": [0.2157, 0.349, 0.6039, 1], "colorizeGradientFile": "art/sky_gradients/default/gradient_colorize.png",
        "fogScale": [0.396, 0.667, 1, 1], "fogScaleGradientFile": "art/sky_gradients/default/gradient_fog.png",
        "nightColor": [0, 0, 0, 1], "nightCubemap": "nightCubemap", "nightFogColor": [0, 0, 0, 1],
        "nightFogGradientFile": "art/sky_gradients/default/gradient_fog.png",
        "nightGradientFile": "art/sky_gradients/default/gradient_ambient.png",
        "skyBrightness": 40, "mieScattering": 0.00047, "occlusionScale": 0.025, "shadowDarkenColor": [0, 0, 0, 0],
        "flareScale": 5, "flareType": "BNG_Sunflare_3", "moonScale": 0.05, "moonAngularSize": 0.67,
        "moonMat": "Moon_Glow_Mat", "moonLightColor": [1, 1, 1, 1], "moonAzimuth": (azimuth + 180.0) % 360.0,
        "moonElevation": 30.0, "starVisibility": 1, "enableFogFallBack": False,
    })
    progress.end(f"sun azimuth {azimuth:.1f} elevation {elevation:.1f}, clock {hour:.2f} h")

    # -- spawn -----------------------------------------------------------------------------
    progress.begin("scene: spawn", total=units(ctx.census, settings), unit="lanes")
    spawn = _pick_spawn(ctx, progress)
    spawns = []
    if spawn is not None:
        pos, heading = spawn
        spawns.append({"name": SPAWN_NAME, "class": "SpawnSphere", "position": [round(v, 3) for v in pos],
                       "rotationMatrix": [round(v, 6) for v in frame.heading_matrix(*heading)],
                       "autoplaceOnSpawn": "0", "dataBlock": "SpawnSphereMarker", "enabled": "1",
                       "homingCount": "0", "indoorWeight": "1", "isAIControlled": "0", "lockCount": "0",
                       "outdoorWeight": "1", "radius": 5, "sphereWeight": "1"})
    for obj in spawns:
        level.add(["PlayerDropPoints"], obj)
    level.write_objects()
    progress.end(f"{len(spawns)} spawn point(s)" + (f" at {spawns[0]['position']}" if spawns else " (no drivable lane found)"))

    # -- preview + info.json -----------------------------------------------------------------
    progress.begin("scene: preview", total=None, unit="")
    preview = _write_preview(ctx, level)
    progress.end("preview image" if preview else "no preview (no terrain)")
    map_size = float(manifest.get("mapSize") or 14336.0)
    city = manifest.get("city") or "CS2 city"
    info = {"title": level.title, "description": f"{city} - ripped from Cities: Skylines II with Carto and built by cs2beamng",
            "previews": [preview] if preview else [], "size": [map_size, map_size], "authors": "cs2beamng",
            "spawnPoints": [{"translationId": "City", "objectname": SPAWN_NAME, "preview": preview or ""}] if spawns else [],
            "defaultSpawnPointName": SPAWN_NAME if spawns else ""}
    level.write_info(info)
    return {"visibleDistance": view, "sunAzimuth": round(azimuth, 1), "sunElevation": round(elevation, 1),
            "spawn": spawns[0]["position"] if spawns else None, "seconds": round(time.monotonic() - started, 1)}


def _write_preview(ctx, level, size=512):
    """`<name>_preview.png`: a hillshaded heightmap with water tinted, from the bundle rasters.
    Cosmetic, but a level without a preview is a blank tile in BeamNG's menu."""
    from csiibundle import png

    paths = reader.terrain_paths(ctx.bundle)
    if paths.rasters is None or not paths.heightmap or not os.path.isfile(paths.heightmap):
        return None
    scale = float(paths.rasters.get("elevationScale", 1.0))
    raw = png.gray_array(paths.heightmap).astype(numpy.float32)
    step = max(1, raw.shape[0] // size)
    h = raw[::step, ::step] * scale
    water = None
    if paths.water and os.path.isfile(paths.water):
        lv = png.gray_array(paths.water)[::step, ::step].astype(numpy.float32) * scale
        water = (lv > 0) & (lv > h)
    # hillshade: light from the north-west, slope from finite differences (metres per pixel)
    metres = float(paths.rasters["resolution"]) * step
    dzdx = numpy.gradient(h, metres, axis=1)
    dzdy = -numpy.gradient(h, metres, axis=0)          # row 0 is north
    nx, ny, nz = -dzdx, -dzdy, numpy.ones_like(h)
    norm = numpy.sqrt(nx * nx + ny * ny + nz * nz)
    lx, ly, lz = -0.5, 0.5, 0.707
    shade = numpy.clip((nx * lx + ny * ly + nz * lz) / norm, 0.0, 1.0)
    lo, hi = numpy.percentile(h, 2), numpy.percentile(h, 98)
    tone = numpy.clip((h - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    r = 60 + 120 * tone + 60 * shade
    g = 90 + 90 * tone + 60 * shade
    b = 50 + 60 * tone + 50 * shade
    if water is not None:
        r = numpy.where(water, 40 + 30 * shade, r)
        g = numpy.where(water, 90 + 40 * shade, g)
        b = numpy.where(water, 160 + 60 * shade, b)
    rgba = numpy.stack([r, g, b, numpy.full_like(r, 255)], axis=-1)
    rgba = numpy.clip(rgba, 0, 255).astype(numpy.uint8)
    name = f"{level.name}_preview.png"
    with open(level.path(name), "wb") as handle:
        handle.write(png.encode_rgba(rgba))
    return name


def _pick_spawn(ctx, progress):
    """`(beamng_position, (dx, dy))` on the chosen lane, or None."""
    lanes_path = reader.layer_path(ctx.bundle, "lanes")
    if not lanes_path:
        return None
    mids = []
    heads = []
    heights = []
    n = 0
    for lane in reader.read_jsonl(lanes_path):
        n += 1
        if n % 20000 == 0:
            progress.advance(20000)
            progress.check()
        if lane.get("secondary") or lane.get("kind") != "car":
            continue
        path = lane.get("path") or []
        if len(path) < 2:
            continue
        i = (len(path) - 1) // 2
        a, b = path[i], path[i + 1]
        mid = [(a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5, (a[2] + b[2]) * 0.5]
        mids.append((mid[0], mid[2]))
        heights.append(mid[1])
        heads.append((b[0] - a[0], b[2] - a[2]))
    progress.advance(n % 20000)
    if not mids:
        return None
    pts = numpy.asarray(mids, dtype=numpy.float64)
    if ctx.region is not None:
        target = numpy.array([ctx.region["centerX"], ctx.region["centerZ"]])
    else:
        target = numpy.median(pts, axis=0)
    idx = int(numpy.argmin(((pts - target) ** 2).sum(axis=1)))
    x, z = mids[idx]
    y = heights[idx] + SPAWN_LIFT
    dx, dz = heads[idx]
    return frame.to_beamng(x, y, z), (dx, dz)


INPUTS = ("layer:grading", "layer:lanes", "layer:terrain", "layer:terrainFine")
SETTINGS = ("view_distance", "tod_hour", "ai_roads", "level_title", "terrain_upsample")
OUTPUTS = ("info.json",)

STAGE = Stage(STAGE_KEY, "Sky, spawn and level info", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS, outputs=OUTPUTS)
