"""Stage `groundcover` — billboard grass over the painted ground layers.

**This stage has no counterpart in CS2 and is not a fidelity feature.** The game's ground is a
textured heightfield and nothing else: there is no grass system, no detail mesh, no billboard
layer anywhere in `Game.dll` (searched for `GroundCover` / `DetailMesh` / a grass prefab; the
only ground renderer is `TerrainMaterialSystem`). So every number here is BeamNG dressing, the
blades are synthesised rather than ripped, and the setting is **off by default** — audit §62
records it as a deliberate deviation, not an approximation of anything.

It exists because BeamNG's ground reads flat at driving height where CS2's does not (CS2's
terrain shader carries its own triplanar detail tier and the camera is never at 1.5 m), and a
`GroundCover` is the engine's own answer to that. What it draws is tinted to the layer's own
albedo so it at least agrees with the ground it stands on.

One `GroundCover` per painted layer, `Types[].layer` naming the `TerrainMaterial`'s
`internalName` — the same names the terrain stage wrote. Field set, radii and clump counts
follow the vanilla levels (229 `GroundCover` objects read across the shipped levels).

The billboard material goes in `art/groundcover/main.materials.json`, NOT beside the terrain's:
no vanilla level mixes plain `Material` entries into a file with `TerrainMaterial` entries, and
`stages/validate.py` fails a build that does.
"""

import os
import time

import numpy

from csiibundle import png

from . import Stage, level_of

STAGE_KEY = "groundcover"

# Vanilla medians over the 229 shipped GroundCover objects: radius 50 / shapeCullRadius 45 /
# dissolveRadius 30 / gridSize 8 / maxBillboardTiltAngle 40, shapes never cast shadows.
VANILLA = {"radius": 50, "shapeCullRadius": 45, "dissolveRadius": 30, "gridSize": 8,
           "maxBillboardTiltAngle": 40, "shapesCastShadows": False, "maxElements": 200000,
           "windGustLength": 1.7, "windGustFrequency": 0.1, "windGustStrength": 0.2,
           "windTurbulenceFrequency": 0.3, "windTurbulenceStrength": 0.1}

# The 2x2 atlas the synthesised texture holds, and the size band of each card in metres.
CARDS = (((0.0, 0.0, 0.5, 0.5), 0.20, 0.35, 6),
         ((0.5, 0.0, 0.5, 0.5), 0.12, 0.22, 4),
         ((0.0, 0.5, 0.5, 0.5), 0.08, 0.15, 4),
         ((0.5, 0.5, 0.5, 0.5), 0.15, 0.28, 6))

# Which painted layers get grass at all. A rock or sand layer with blades on it looks worse than
# a bare one. OURS — there is nothing in the game to read.
GRASSY_LAYERS = ("grass", "extra1", "extra2", "extra3", "extra4")

ATLAS_PX = 256


def enabled(settings):
    return bool(settings.get("groundcover", False))


def units(census, settings):
    return 8


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    shared = ctx.shared if ctx.shared is not None else {}
    terrain = shared.get("terrain") or {}
    layers = [(ground, layer) for ground, layer in zip(terrain.get("grounds") or (), terrain.get("layers") or ())
              if ground in GRASSY_LAYERS]
    if not layers:
        progress.log("groundcover: no grassy layer is painted on this map (or the terrain stage "
                     "did not run), so nothing is placed")
        return {"covers": 0, "seconds": round(time.monotonic() - started, 1)}

    progress.begin("groundcover: billboards", total=len(layers), unit="layers")
    materials = {}
    covers = 0
    for name, layer in layers:
        tint = _layer_tint(ctx.bundle, name)
        atlas = _atlas(tint)
        rel = f"{level.name}_groundcover_{name}.png"
        level.write_bytes_if_changed(png.encode_rgba(atlas), "art", "groundcover", rel)
        material_name = f"{level.name}_groundcover_{name}"
        materials[material_name] = {
            "name": material_name, "class": "Material", "version": 1.5,
            "persistentId": level.pid("groundcover:material:" + name),
            "mapTo": material_name, "alphaTest": True, "alphaRef": 100,
            "doubleSided": True, "castShadows": False,
            "materialTag0": "beamng", "materialTag1": "cs2",
            "Stages": [{"baseColorMap": level.game_path("art", "groundcover", rel),
                        "roughnessFactor": 1.0, "metallicFactor": 0.0}, {}, {}, {}],
        }
        obj = {"name": f"groundcover_{name}", "class": "GroundCover",
               "position": [0, 0, 0], "material": material_name,
               "Types": [{"billboardUVs": list(uv), "layer": layer, "sizeMin": lo, "sizeMax": hi,
                          "maxClumpCount": clump, "minClumpCount": max(1, clump // 2),
                          "probability": 1, "windScale": 0.05}
                         for uv, lo, hi, clump in CARDS]}
        obj.update(VANILLA)
        level.add(["Vegetation"], obj, key="groundcover:" + name)
        covers += 1
        progress.advance(1)
    progress.end(f"{covers} ground covers")
    level.write_materials("groundcover", materials)
    level.write_objects()
    return {
        "covers": covers,
        "layers": [layer for _g, layer in layers],
        "seconds": round(time.monotonic() - started, 1),
        "deliberateDeviations": {
            "groundcover": {
                "what": f"{covers} GroundCover objects with synthesised billboard grass",
                "why": "CS2 has no grass, detail-mesh or groundcover system at all - the ground is "
                       "a textured heightfield. This is BeamNG dressing for a camera at driving "
                       "height, off by default, and nothing here approximates a game rule.",
                "count": covers,
            },
        },
    }


def _layer_tint(bundle, name):
    """The mean colour of this layer's own ground albedo, so the blades agree with the ground."""
    path = os.path.join(bundle, "terrain", "surfaces", f"{name}.png")
    if not os.path.isfile(path):
        return (0.36, 0.45, 0.22)
    try:
        image = png.rgba_array(path)
    except Exception:                                                     # noqa: BLE001
        return (0.36, 0.45, 0.22)
    mean = image[:, :, :3].reshape(-1, 3).mean(axis=0) / 255.0
    return tuple(float(v) for v in mean)


def _atlas(tint):
    """A 2x2 atlas of blade cards, RGBA uint8. Synthesised: the bundle carries no billboard art
    and the game has none to rip. Deterministic, so a rebuild does not re-cook the texture."""
    size = ATLAS_PX
    out = numpy.zeros((size, size, 4), dtype=numpy.uint8)
    rng = numpy.random.default_rng(20260818)
    half = size // 2
    for cell in range(4):
        oy, ox = (cell // 2) * half, (cell % 2) * half
        tile = numpy.zeros((half, half, 4), dtype=numpy.float64)
        blades = 7 + cell
        for _ in range(blades):
            x0 = rng.uniform(0.15, 0.85) * half
            lean = rng.uniform(-0.25, 0.25) * half
            width = rng.uniform(0.02, 0.05) * half
            top = rng.uniform(0.55, 0.95)
            for row in range(half):
                t = 1.0 - row / (half - 1.0)                  # 0 at the top of the image
                if t > top:
                    continue
                centre = x0 + lean * (t / max(top, 1e-6))
                w = max(1.0, width * (1.0 - 0.7 * t / max(top, 1e-6)))
                lo, hi = int(max(0, centre - w)), int(min(half, centre + w + 1))
                shade = 0.55 + 0.45 * (1.0 - t / max(top, 1e-6))
                tile[row, lo:hi, :3] = numpy.asarray(tint) * shade
                tile[row, lo:hi, 3] = 1.0
        out[oy:oy + half, ox:ox + half] = numpy.clip(tile * 255.0, 0, 255).astype(numpy.uint8)
    return out


INPUTS = ("layer:surfaces",)
SETTINGS = ("groundcover",)

STAGE = Stage(STAGE_KEY, "Billboard grass", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS, after=("terrain",))
