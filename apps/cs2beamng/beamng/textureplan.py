"""What textures a build will write, named before any of them is written.

Shared by the build and the cost estimate, which is the point: the meter's figure is the plan's
own arithmetic over the same file list the build will produce, not a second guess at it."""

import json
import os
import re

import numpy
from csiibundle import glbjson, images, png
from csiibundle import materials as bundle_materials

from . import dds as dds_mod

TEXTURE_SUBDIR = ("art", "textures")

# The version of the CONVERSION this module applies between a source image and the texture it
# writes - part of the texture ledger's key (`stages/materialledger.ledger_key`). The ledger
# used to key on the source bytes, the cap and the format alone, so a fix to `_colormask_rgba`,
# `_encode_dds` or the mip chain reported every texture "unchanged... not re-encoded" and
# shipped nothing - the trap `stages/materials` v5 already fell into one layer up. Bump it with
# any change to what a given source becomes.
#   1: the ledger's original key
#   2: a two-channel (AG / RG) normal map is rebuilt to RGB before encoding (`_rebuilt_normal`)
TEXTURE_PIPELINE_VERSION = 2

# -- textures ----------------------------------------------------------------------------------


# The edge cap for a texture bound ONLY by an authored level past LOD 0, by the nearest level
# it is seen at. The game's chain switches to LOD 1 at a few hundred projected pixels of the
# whole shape and to LOD 2 under about eighty (`csiibundle.lodchain`), so a 256-texel atlas on
# a LOD 2 face is sixteen times the pixels it can ever cover. MEASURED on the reference city:
# the chain brought 2,882 textures bound only by LOD 2 (613 MiB raw) and 242 only by LOD 1 —
# per-building baked atlases — and the level went 4,280 → 5,125 MiB resident, a hundred short
# of where D3D12 was seen to fail; at these caps it is 4,998. OURS, a budget; a texture any
# LOD 0 material binds keeps its kind's cap whatever a chain does with it elsewhere.
LOD_TEXTURE_CAPS = {1: 128, 2: 64}


def lod_cap(level, kind_cap):
    """The cap a texture seen no nearer than `level` exports at: its kind's, tightened by
    `LOD_TEXTURE_CAPS` past LOD 0 (never loosened)."""
    if level <= 0:
        return int(kind_cap or 0)
    tight = LOD_TEXTURE_CAPS[min(int(level), max(LOD_TEXTURE_CAPS))]
    return tight if (not kind_cap or tight < int(kind_cap)) else int(kind_cap)


_LEVELS_CACHE = {}


def material_lod_levels(bundle, table):
    """`{canonical material: nearest level any mesh binds it at}` over every `meshes/*.glb`,
    from their JSON chunks. What the shapes stage measures from the geometry it actually wrote
    (`materials_lod_level`), computed here without writing any — for the cost meter, which sizes
    the textures before a shape exists. Cached on the mesh directory's own stamp."""
    directory = os.path.join(bundle, "meshes")
    try:
        with os.scandir(directory) as it:
            entries = [e for e in it if e.name.endswith(".glb")]
    except OSError:
        return {}
    stamp = (directory, len(entries), max((e.stat().st_mtime_ns for e in entries), default=0))
    hit = _LEVELS_CACHE.get(stamp)
    if hit is not None:
        return hit
    aliases = table.get("aliases") or {}
    levels = {}
    for entry in entries:
        for name, level in (glbjson.material_levels(entry.path) or {}).items():
            canonical = aliases.get(name, name)
            levels[canonical] = min(levels.get(canonical, level), level)
    _LEVELS_CACHE.clear()
    _LEVELS_CACHE[stamp] = levels
    return levels


def _cap(arr, max_size):
    if not max_size:
        return arr
    h, w = arr.shape[:2]
    factor = 1
    while max(h, w) // factor > int(max_size):
        factor *= 2
    return png.downsample_box(arr, factor) if factor > 1 else arr


def capped_size(width, height, max_size):
    """The dimensions `_cap` produces for a `width` x `height` source, without loading it.

    The same halving walk, and `png.downsample_box` floors, so this is what the exported file
    measures. Split out for the cost meter, which sizes every texture from its PNG header
    (`pipeline/estimate.py`) and must agree with the export to the pixel or its "computed" claim
    is a modelled one wearing a better word.
    """
    if not max_size:
        return width, height
    factor = 1
    while max(height, width) // factor > int(max_size):
        factor *= 2
    if factor <= 1:
        return width, height
    return max(1, width // factor), max(1, height // factor)


def dxgi_format(kind, keep_alpha=False, grey=False):
    """The DXGI format `export_texture` encodes this (kind, source) as.

    `export_texture`'s branches in one function, so the meter cannot pick a different block size
    from the exporter: opacity is always the albedo's alpha as BC4; a colour mask is always the
    converted RGBA as BC7; a GREY source takes its kind's format directly (BC4 for `data`); and
    an RGB `data` source goes out as `data_rgb`, which is BC7 rather than BC4 - the one case
    where the block size doubles and a naive table would be half the truth.
    """
    if kind == "opacity":
        return dds_mod.KIND_FORMAT["opacity"]
    if kind == "colormask":
        return dds_mod.KIND_FORMAT["colormask"]
    if grey or keep_alpha:
        return dds_mod.KIND_FORMAT[kind]
    return dds_mod.KIND_FORMAT[kind if kind != "data" else "data_rgb"]


# -- the texture plan, shared by the build and the estimate --------------------------------------
#
# `stages/materials` builds these to EXPORT them; `pipeline/estimate` builds the same ones to
# SIZE them. Two copies of the walk would be two answers to "which textures does this level
# ship", and the meter's whole value is that it cannot disagree with the build.


def plan_canonical(bundle, derived_dir, table, names):
    """`{canonical: {slot: (src, kind, keep_alpha)}}` for the named canonical materials.

    The slots a v1.5 material can carry, resolved to source paths: the albedo (plus its alpha as
    a separate opacity map where the material cuts or blends), the normal (the bake's rebuilt RGB
    one where it made one), the split roughness and metallic, the emissive, and CS2's colour mask
    where the material is tintable. No occlusion slot - the mask's G is not AO (audit 54).
    """
    materials = table.get("materials") or {}
    plans = {}
    for name in names:
        rec = materials.get(name) or {}
        mode = rec.get("alphaMode") or "opaque"
        plan = {}
        if rec.get("baseColor"):
            plan["baseColor"] = (os.path.join(bundle, rec["baseColor"]), "color", False)
            if mode != "opaque":
                plan["opacity"] = (os.path.join(bundle, rec["baseColor"]), "opacity", False)
        # The bake rebuilds a PACKED normal to §7.1 RGB and records the per-material path in
        # `derived.normalRgb` (`bake/materials._write_tables`). This used to read the table's
        # `normalsRebuilt`, which is keyed by normal-map PATH with dict values, by material NAME
        # - a lookup that could never hit, so every material took the raw bundle map
        # (validation pass 2026-08-31, B1). `export_texture` rebuilds a packed map it meets
        # anyway (B2); this is the bake's answer reaching the level, as intended.
        derived_maps = rec.get("derived") or {}
        normal_rel = derived_maps.get("normalRgb") or rec.get("normal")
        if normal_rel:
            base = derived_dir if derived_maps.get("normalRgb") == normal_rel else bundle
            plan["normal"] = (os.path.join(base, normal_rel), "normal", False)
        for slot in ("roughness", "metallic"):
            rel = (rec.get("derived") or {}).get(slot)
            if rel:
                plan[slot] = (os.path.join(derived_dir, rel), "data", False)
        if rec.get("emissive"):
            plan["emissive"] = (os.path.join(bundle, rec["emissive"]), "color", False)
        if rec.get("colorMask") and rec.get("tintable"):
            # "colormask", not "data": the export converts CS2's mask into BeamNG's palette
            # semantics and the ALPHA it writes is the tint coverage (`_colormask_rgba`)
            plan["colorMask"] = (os.path.join(bundle, rec["colorMask"]), "colormask", False)
        plans[name] = plan
    return plans


def variant_albedo(derived_dir, rec, vkey):
    """The bake's tinted albedo for one (material, variant), or None where it baked a factor.

    A variant is the canonical material with this file in place of the atlas albedo; where the
    bake measured one constant multiply as reproducing the tint inside `paint_factor_max_error`
    it wrote no texture at all, and that pair costs nothing.
    """
    rel = ((rec.get("derived") or {}).get("variants") or {}).get(vkey)
    if not rel:
        return None
    path = os.path.join(derived_dir, rel)
    return path if os.path.isfile(path) else None


def plan_areas(bundle, derived_dir, has_alpha=None):
    """`[(src, kind, keep_alpha), ...]` for the 8b area-surface materials.

    `has_alpha(path)` decides whether a surface's albedo also ships its alpha as an opacity map;
    the build passes its own real test, the estimate passes a cheaper one.
    """
    path = os.path.join(derived_dir, "areas", "materials.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return []
    jobs = []
    for _raw, rec in (doc.get("materials") or {}).items():
        tex = rec.get("texture")
        if tex and os.path.isfile(os.path.join(bundle, tex)):
            src = os.path.join(bundle, tex)
            jobs.append((src, "color", False))
            if has_alpha is not None and has_alpha(src):
                jobs.append((src, "opacity", False))
        normal = rec.get("normal_map")
        if normal and os.path.isfile(os.path.join(bundle, normal)):
            jobs.append((os.path.join(bundle, normal), "normal", False))
    return jobs


def texture_name(src_path, kind, keep_alpha=False, use_dds=True):
    """The file name `export_texture` writes for this source, without touching a pixel.

    Split out so a build can decide "this one is already exported" from the source's mtime and
    size alone (`stages/materials._reusable`): BC7-encoding 5,684 textures and throwing every
    byte away because the file already held them cost 2m17s of every rebuild.
    """
    ext = "dds" if use_dds else "png"
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", os.path.splitext(os.path.basename(src_path or ""))[0])
    if kind == "opacity":  # the albedo's alpha as a grey map: v1.5's opacityMap
        return f"{stem}_o.data.{ext}"
    return f"{stem}.{kind}.{ext}" if not keep_alpha else f"{stem}_a.{kind}.{ext}"


def export_texture(
    level, src_path, kind, max_size=0, keep_alpha=False, cache=None, dds=None, stats=None
):
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
    arr, form = images.load_any(src_path)
    arr = _cap(arr, max_size)
    if kind == "normal" and form == "rgba":
        # A two-channel normal read as RGB shades BLACK (AG: x = +1, flat along the surface;
        # RG: z = -1, into it) - `csiibundle.normalmaps`. The producer rebuilds the packed ones
        # it sees and the bake the rest (`derived.normalRgb`), so a packed map reaching this
        # point is a bundle from a rip that missed one; the level is not the place to find out.
        arr, packing = _rebuilt_normal(arr)
        if packing and stats is not None:
            stats["normalsUnpacked"] = stats.get("normalsUnpacked", 0) + 1
    if kind == "opacity":
        alpha = arr[:, :, 3] if form == "rgba" else (numpy.full(arr.shape[:2], 255, numpy.uint8))
        data = (
            _encode_dds(alpha.astype(numpy.uint8), "opacity")
            if use_dds
            else png.encode_gray(alpha.astype(numpy.uint8), bit_depth=8)
        )
    elif kind == "colormask":
        # BEFORE the `form == "gray"` branch: a single-channel mask is still a colour mask, and
        # the conversion has to run on it too. (This branch was written into `texture_name` by
        # mistake in `199f084`, where `arr`/`form` do not exist — so every `colormask` export
        # raised NameError, no mask reached the level at all, and A6.3's fix was in the build
        # report and nowhere else. 724 textures on the reference city.)
        rgba = _colormask_rgba(arr, form)
        data = _encode_dds(rgba, "colormask", keep_alpha=True) if use_dds else png.encode_rgba(rgba)
    elif form == "gray":
        g = arr
        if g.dtype != numpy.uint8:
            g = (
                (g.astype(numpy.uint32) >> 8).astype(numpy.uint8)
                if g.max() > 255
                else g.astype(numpy.uint8)
            )
        data = _encode_dds(g, kind) if use_dds else png.encode_gray(g, bit_depth=8)
    elif keep_alpha:
        data = _encode_dds(arr, kind, keep_alpha=True) if use_dds else png.encode_rgba(arr)
    else:
        data = (
            _encode_dds(arr, kind if kind != "data" else "data_rgb")
            if use_dds
            else png.encode_rgb(arr)
        )
    level.write_bytes_if_changed(data, *TEXTURE_SUBDIR, name)
    if cache is not None:
        cache[key] = game
    return game


def _rebuilt_normal(arr):
    """`(rgba, packing)`: an AG- or RG-packed normal rebuilt to §7.1 RGB (Z reconstructed as
    the game's shader does), or the array untouched with `packing` None for an RGB map. The
    per-texel rule is `csiibundle.normalmaps.unpack_normal`; this is the same arithmetic over
    the whole image."""
    sample = arr[::16, ::16].reshape(-1, arr.shape[2]).astype(numpy.float64) / 255.0
    packing = bundle_materials.detect_normal_packing(tuple(row[:4]) for row in sample)
    if packing == bundle_materials.NORMAL_RGB:
        return arr, None
    f = arr.astype(numpy.float64) / 255.0
    x = 2.0 * (f[..., 3] if packing == bundle_materials.NORMAL_AG else f[..., 0]) - 1.0
    y = 2.0 * f[..., 1] - 1.0
    z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, None))
    out = numpy.empty_like(arr)
    for channel, value in enumerate((x, y, z)):
        out[..., channel] = numpy.clip(numpy.rint((value + 1.0) * 127.5), 0, 255).astype(
            numpy.uint8
        )
    out[..., 3] = 255
    return out, packing


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
    rgba = numpy.zeros((*arr.shape[:2], 4), dtype=numpy.uint8)
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
