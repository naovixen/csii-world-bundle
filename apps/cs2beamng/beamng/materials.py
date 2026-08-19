"""BeamNG materials v1.5 from the bake's resolved material table, and the texture export.

Conventions (from shipped `main.materials.json` files and the v1.5 docs):

* One object per material: `{"name", "mapTo", "class": "Material", "persistentId", "version":
  1.5, "Stages": [{...}, {}, {}, {}], flags...}`. `mapTo` is the name the `.dae` binds
  (`<material name="...">`), so it is what `dae.sanitize` produces for the same string.
* Stage 0 maps: `baseColorMap` (`.color.png`, sRGB, alpha kept when the material cuts or
  blends), `normalMap` (`.normal.png`), `roughnessMap` / `metallicMap` (`.data.png`, linear,
  from the bake's split mask — there is **no** `ambientOcclusionMap`, because the mask's G
  channel is not occlusion; audit §54), `emissiveMap`,
  `colorPaletteMap` (the CS2 colour mask: RGB selects `instanceColor/1/2` — the palette tint
  path). Scalars: `roughnessFactor`, `metallicFactor`, `diffuseColor`, `emissiveFactor`.
* Alpha: v1.5 takes opacity from an **`opacityMap`** (464 of ~500 vanilla translucent materials
  and every alphaTest one carry it), NOT from the albedo's alpha — load #7 drew every marking
  decal as an opaque white box. So mask/blend materials get the albedo alpha exported as a grey
  `_o.data.png`, `opacityFactor` = the base-colour factor's alpha; `"mask"` → `alphaTest` +
  `alphaRef`; `"blend"` → `translucent` + `LerpAlpha` + `translucentZWrite`. `doubleSided` as given.
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
from . import dds as dds_mod

TEXTURE_SUBDIR = ("art", "textures")

# Per-kind edge caps, because one cap for every map is one cap too few. CALIBRATED on
# `west_coast_usa`'s own DDS headers — BeamNG's biggest shipped level, 0.58 GiB of textures
# against A3's 5.72 GiB:
#
#   colour  185 of 256 files are <= 512
#   data    391 of 470 files are <= 256   (roughness/metallic carry almost no high frequency)
#   normal  512-1024
#
# `opacity` is the albedo's OWN alpha channel re-exported as a grey map, so it takes the colour
# cap: it is a coverage mask cut at the albedo's scale, and dropping it to the data cap is what
# turns a tree's leaf cut-outs blocky. `emissive` is a colour map. The keys are settings names;
# `caps_from_settings` resolves them.
KIND_CAP_SETTING = {"color": "texture_max", "normal": "texture_max_normal",
                    "data": "texture_max_data", "opacity": "texture_max"}


def caps_from_settings(settings):
    """`{kind: edge cap}` from the settings, falling back to the colour cap for anything the
    settings do not name (an old settings file that only has `texture_max`)."""
    colour = int(settings.get("texture_max") or 0)
    out = {}
    for kind, key in KIND_CAP_SETTING.items():
        value = settings.get(key, colour)
        out[kind] = int(colour if value is None else value)
    return out


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


def texture_name(src_path, kind, keep_alpha=False, use_dds=True):
    """The file name `export_texture` writes for this source, without touching a pixel.

    Split out so a build can decide "this one is already exported" from the source's mtime and
    size alone (`stages/materials._reusable`): BC7-encoding 5,684 textures and throwing every
    byte away because the file already held them cost 2m17s of every rebuild.
    """
    ext = "dds" if use_dds else "png"
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", os.path.splitext(os.path.basename(src_path or ""))[0])
    if kind == "colormask":
        data = _encode_dds(_colormask_rgba(arr, form), "colormask", keep_alpha=True) if use_dds             else png.encode_rgba(_colormask_rgba(arr, form))
    elif kind == "opacity":                              # the albedo's alpha as a grey map: v1.5's opacityMap
        return f"{stem}_o.data.{ext}"
    return f"{stem}.{kind}.{ext}" if not keep_alpha else f"{stem}_a.{kind}.{ext}"


def export_texture(level, src_path, kind, max_size=0, keep_alpha=False, cache=None, dds=None):
    """Write `src_path` into the level's `art/textures/` as `<stem>.<kind>.dds` (`kind` in
    color / normal / data / opacity) capped at `max_size` — a pre-compressed, mipmapped DDS in
    the format BeamNG's cooker would have chosen (`dds.KIND_FORMAT`), so the game never cooks
    it. Falls back to `.png` when the encoder is not installed (`dds=None` = auto; False forces
    PNG). Returns the game path, or None when the source is missing. `cache` (a dict)
    short-circuits repeats within a build."""
    if not src_path or not os.path.isfile(src_path):
        return None
    use_dds = dds_mod.available() if dds is None else bool(dds and dds_mod.available())
    name = texture_name(src_path, kind, keep_alpha, use_dds)
    key = (name, int(max_size or 0))
    if cache is not None and key in cache:
        return cache[key]
    game = level.game_path(*TEXTURE_SUBDIR, name)
    # Encoded every build, WRITTEN only when the bytes differ from what is there: an unchanged
    # texture keeps its mtime and BeamNG keeps its cooked copy in temp/ (load #11: every first
    # load re-cooked all 260 textures because the folder was cleared and rewritten each build).
    # A same-name file with different bytes (another cap, another rip) is replaced, never reused.
    arr, form = _load_any(src_path)
    arr = _cap(arr, max_size)
    if kind == "opacity":
        alpha = arr[:, :, 3] if form == "rgba" else (numpy.full(arr.shape[:2], 255, numpy.uint8))
        data = _encode_dds(alpha.astype(numpy.uint8), "opacity") if use_dds else png.encode_gray(alpha.astype(numpy.uint8), bit_depth=8)
    elif form == "gray":
        g = arr
        if g.dtype != numpy.uint8:
            g = (g.astype(numpy.uint32) >> 8).astype(numpy.uint8) if g.max() > 255 else g.astype(numpy.uint8)
        data = _encode_dds(g, kind) if use_dds else png.encode_gray(g, bit_depth=8)
    elif keep_alpha:
        data = _encode_dds(arr, kind, keep_alpha=True) if use_dds else png.encode_rgba(arr)
    else:
        data = _encode_dds(arr, kind if kind != "data" else "data_rgb") if use_dds else png.encode_rgb(arr)
    level.write_bytes_if_changed(data, *TEXTURE_SUBDIR, name)
    if cache is not None:
        cache[key] = game
    return game


def _colormask_rgba(arr, form):
    """CS2's colour mask, converted into the semantics BeamNG's palette actually implements.

    THE TWO ARE NOT THE SAME MAP, and shipping CS2's straight through is why buildings render
    wrong. Read from `shaders/common/material/shadergen/shadergen.h.hlsl::getColorPalette`:

        rgb  = rgb / (r + g + b)                    # NORMALISED to a simplex - a selector
        cov  = a                                    # the ALPHA is how much tint applies at all
        rgb *= cov ;  rest = 1 - cov
        palette = SUM rgb[i] * linear(instanceColor[i]) + rest * float3(1,1,1)
        baseColor *= palette

    CS2's mask (bundle §7.2) is three INDEPENDENT lerp weights and its alpha means nothing:

        t = lerp(t, t * colorSet[i], mask[i])   for i in R, G, B

    So BeamNG's RGB is only a *chooser* and its ALPHA carries the weight. We exported the mask as
    `data_rgb`, which forces alpha to 255 — coverage 1 on every texel, so the white "untinted"
    remainder contributes nothing. Wherever CS2's mask says (0,0,0) — the MEDIAN texel on 234 of
    400 measured masks — the palette evaluates to (0,0,0) and the surface renders BLACK; wherever
    it says (0.3,0,0) the normalisation makes it a full-strength tint instead of a 30 % one.

    The conversion is EXACT while the channels are authored disjoint, which §7.2 states they are:
    with one channel `i` active, CS2 gives `lerp(1, c_i, m_i)`, and setting `alpha = r + g + b`
    makes BeamNG compute `m_i * c_i + (1 - m_i) * 1` — the same thing. Where two channels overlap
    BeamNG can only mix where CS2 multiplies, and no alpha can fix that; it is declared in the
    report as `colorMaskOverlap` with its count.
    """
    rgba = numpy.zeros(arr.shape[:2] + (4,), dtype=numpy.uint8)
    if form == "gray":
        rgba[..., 0] = arr if arr.dtype == numpy.uint8 else arr.astype(numpy.uint8)
    else:
        rgba[..., :3] = arr[..., :3]
    total = rgba[..., :3].astype(numpy.uint16).sum(axis=2)
    rgba[..., 3] = numpy.minimum(total, 255).astype(numpy.uint8)
    return rgba


def _encode_dds(arr, kind, keep_alpha=False):
    """The DDS bytes for an exported array: grey -> BC4, normal -> BC5, colour -> BC7 sRGB
    (alpha kept only when asked), RGB data -> BC7 linear."""
    rgba = dds_mod.to_rgba(arr, kind if not keep_alpha else "color_alpha")
    if kind == "color" and rgba.shape[2] == 4 and not keep_alpha:
        rgba[..., 3] = 255
    fmt = dds_mod.KIND_FORMAT.get(kind, dds_mod.BC7_UNORM_SRGB)
    return dds_mod.encode(rgba, fmt)


# -- materials -----------------------------------------------------------------------------------

def material_v15(level, name, rec, maps, pid_key=None):
    """One v1.5 material object. `name` is the final (mapTo) name; `rec` the bake's resolved
    record; `maps` the exported game paths: {baseColor, normal, roughness, metallic,
    emissive, colorMask} (any may be None). There is deliberately no occlusion map: the §7.1
    mask's G channel is not AO (audit §54)."""
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
    if maps.get("emissive"):
        stage["emissiveMap"] = maps["emissive"]
        factor = rec.get("emissiveFactor") or [1.0, 1.0, 1.0]
        stage["emissiveFactor"] = [float(v) for v in factor[:3]]
    if maps.get("colorMask") and rec.get("tintable"):
        stage["colorPaletteMap"] = maps["colorMask"]
        # Without this the shader takes `baseColor *= 1` and the palette is applied to NOTHING
        # (`getColorPalette`: `baseColor *= (flags & MATERIAL_LAYER_FLAG_paletteBaseColor) ?
        # colorPalette.rgb : 1`). Set explicitly rather than relying on whatever the material
        # compiler defaults it to when a `colorPaletteMap` is present.
        stage["paletteBaseColor"] = True
    if maps.get("opacity"):
        stage["opacityMap"] = maps["opacity"]           # v1.5 reads opacity from here, not from the albedo's alpha
    factor = rec.get("baseColorFactor")
    if factor:
        rgb = [float(v) for v in factor[:3]]
        alpha = float(factor[3]) if len(factor) > 3 else 1.0
        if any(abs(v - 1.0) > 1e-3 for v in rgb):
            stage["baseColorFactor"] = rgb + [1.0]
        if alpha < 0.999:
            stage["opacityFactor"] = alpha
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
