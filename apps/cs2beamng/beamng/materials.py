"""BeamNG materials v1.5 from the bake's resolved material table, and the texture export.

Conventions (from shipped `main.materials.json` files and the v1.5 docs):

* One object per material: `{"name", "mapTo", "class": "Material", "persistentId", "version":
  1.5, "Stages": [{...}, {}, {}, {}], flags...}`. `mapTo` is the name the `.dae` binds
  (`<material name="...">`), so it is what `dae.sanitize` produces for the same string.
* Stage 0 maps: `baseColorMap` (`.color.png`, sRGB, alpha kept when the material cuts or
  blends), `normalMap` (`.normal.png`), `roughnessMap` / `metallicMap` /
  `ambientOcclusionMap` (`.data.png`, linear, from the bake's split mask), `emissiveMap`,
  `colorPaletteMap` (the CS2 colour mask: RGB selects `instanceColor/1/2` — the palette tint
  path). Scalars: `roughnessFactor`, `metallicFactor`, `diffuseColor`, `emissiveFactor`.
* Alpha: `alphaMode == "mask"` → `alphaTest` + `alphaRef` (0..255); `"blend"` → `translucent`
  + `translucentBlendOp LerpAlpha` + `translucentZWrite`. `doubleSided` as given.
* Every material name carries the level name (materials are a global registry; see the
  terrain lesson) — `mapto(level_name, raw_name)`.

Textures: `export_texture` copies a bundle/derived PNG into `art/textures/` under the BeamNG
suffix for its colour space, box-downsampled to `max_size` (0 = full). Colour maps keep alpha
only when a material needs it; everything else is written RGB or grey (an unused alpha is
read as coverage by more than one BeamNG loader — the terrain packer already bit us).
"""

import os
import re

import numpy

from csiibundle import png

from . import dae

TEXTURE_SUBDIR = ("art", "textures")


def mapto(level_name, raw_name):
    """The material name a `.dae` binds and `main.materials.json` `mapTo`s: level-prefixed."""
    return dae.sanitize(f"{level_name}_{raw_name}")


# -- textures ----------------------------------------------------------------------------------

def _load_any(path):
    """`(array, kind)`: `(h,w,4) uint8` for RGB/RGBA PNGs (RGB gets alpha 255), `(h,w) uint8|uint16`
    for grey ones. csiibundle's decoders first (they never colour-manage), PIL for the rest."""
    try:
        return png.rgba_array(path), "rgba"
    except ValueError:
        pass
    try:
        g = png.gray_array(path)
        return g, "gray"
    except ValueError:
        pass
    from PIL import Image  # noqa: WPS433 - palette / RGB PNGs

    im = Image.open(path)
    if im.mode in ("L", "I;16", "I"):
        return numpy.asarray(im.convert("I")).astype(numpy.uint16), "gray"
    return numpy.asarray(im.convert("RGBA")), "rgba"


def _cap(arr, max_size):
    if not max_size:
        return arr
    h, w = arr.shape[:2]
    factor = 1
    while max(h, w) // factor > int(max_size):
        factor *= 2
    return png.downsample_box(arr, factor) if factor > 1 else arr


def export_texture(level, src_path, kind, max_size=0, keep_alpha=False, cache=None):
    """Write `src_path` into the level's `art/textures/` as `<stem>.<kind>.png` (`kind` in
    color / normal / data) capped at `max_size`. Returns the game path, or None when the source
    is missing. `cache` (a dict) short-circuits repeats within a build."""
    if not src_path or not os.path.isfile(src_path):
        return None
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", os.path.splitext(os.path.basename(src_path))[0])
    name = f"{stem}.{kind}.png" if not keep_alpha else f"{stem}_a.{kind}.png"
    key = (name, int(max_size or 0))
    if cache is not None and key in cache:
        return cache[key]
    dst = level.path(*TEXTURE_SUBDIR, name)
    game = level.game_path(*TEXTURE_SUBDIR, name)
    if not os.path.isfile(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        arr, form = _load_any(src_path)
        arr = _cap(arr, max_size)
        with open(dst, "wb") as handle:
            if form == "gray":
                g = arr
                if g.dtype != numpy.uint8:
                    g = (g.astype(numpy.uint32) >> 8).astype(numpy.uint8) if g.max() > 255 else g.astype(numpy.uint8)
                handle.write(png.encode_gray(g, bit_depth=8))
            elif keep_alpha:
                handle.write(png.encode_rgba(arr))
            else:
                handle.write(png.encode_rgb(arr))
    if cache is not None:
        cache[key] = game
    return game


# -- materials -----------------------------------------------------------------------------------

def material_v15(level, name, rec, maps, pid_key=None):
    """One v1.5 material object. `name` is the final (mapTo) name; `rec` the bake's resolved
    record; `maps` the exported game paths: {baseColor, normal, roughness, metallic, ao,
    emissive, colorMask} (any may be None)."""
    stage = {}
    if maps.get("baseColor"):
        stage["baseColorMap"] = maps["baseColor"]
    if maps.get("normal"):
        stage["normalMap"] = maps["normal"]
    if maps.get("roughness"):
        stage["roughnessMap"] = maps["roughness"]
    elif rec.get("roughness") is not None:
        stage["roughnessFactor"] = float(rec["roughness"])
    if maps.get("metallic"):
        stage["metallicMap"] = maps["metallic"]
    elif rec.get("metallic") is not None:
        stage["metallicFactor"] = float(rec["metallic"])
    if maps.get("ao"):
        stage["ambientOcclusionMap"] = maps["ao"]
    if maps.get("emissive"):
        stage["emissiveMap"] = maps["emissive"]
        factor = rec.get("emissiveFactor") or [1.0, 1.0, 1.0]
        stage["emissiveFactor"] = [float(v) for v in factor[:3]]
    if maps.get("colorMask") and rec.get("tintable"):
        stage["colorPaletteMap"] = maps["colorMask"]
    factor = rec.get("baseColorFactor")
    if factor and any(abs(float(v) - 1.0) > 1e-3 for v in factor[:4]):
        stage["diffuseColor"] = [float(v) for v in factor[:4]]
    if not stage.get("baseColorMap") and rec.get("color"):
        stage["diffuseColor"] = [float(v) for v in list(rec["color"])[:4]] + [1.0] * (4 - len(rec["color"][:4]))
    if rec.get("uv_scale") and rec.get("uv_scale") != 1.0:
        pass                                              # UV scale is baked into tile UVs by the areas product
    mat = {"name": name, "mapTo": name, "class": "Material", "persistentId": level.pid(pid_key or f"material:{name}"),
           "version": 1.5, "Stages": [stage, {}, {}, {}], "materialTag0": "beamng", "materialTag1": "cs2"}
    mode = rec.get("alphaMode") or "opaque"
    if mode == "mask":
        mat["alphaTest"] = True
        mat["alphaRef"] = int(round(255 * float(rec.get("alphaCutoff") or 0.5)))
    elif mode == "blend":
        mat["translucent"] = True
        mat["translucentBlendOp"] = "LerpAlpha"
        mat["translucentZWrite"] = True
    if rec.get("doubleSided"):
        mat["doubleSided"] = True
    if rec.get("castShadows") is False:
        mat["castShadows"] = False
    if rec.get("groundType"):
        mat["groundType"] = rec["groundType"]
    return mat


def placeholder_material(level, name, color=(0.5, 0.5, 0.5, 1.0)):
    """A flat material for a symbol nothing resolves (unresolved asset packs, `unmapped`)."""
    return {"name": name, "mapTo": name, "class": "Material", "persistentId": level.pid(f"material:{name}"),
            "version": 1.5, "Stages": [{"diffuseColor": [float(c) for c in color]}, {}, {}, {}],
            "materialTag0": "beamng", "materialTag1": "cs2"}
