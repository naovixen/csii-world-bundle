"""Bake product `materials` — the resolved PBR table, split maps, rebuilt normals, paint variants.

What a renderer needs from `materials.json` is decided once here (contract §13):

* **The emissive colour slots are joined here** (A6.9/D5). `materials.json` ships no
  `emissiveFactor` on any material because CS2 does not drive emission from a static surface
  property — it drives it from the render prefab's `ProceduralLight` buffer, which is
  `lights.json`'s 1,896 colour slots. `_emissive_slots` matches each mesh's slots to its emissive
  materials in `.glb` primitive order (639 of 781 meshes, 81.8 %) and writes CS2's own lit
  colour and intensity into `emissiveFactor`/`emissiveNits`.
* `derived/materials.json` — for every material its `materials.resolve` dict plus the paths of
  the maps this product wrote for it; an **alias map** collapsing materials whose resolved
  texture set is identical (`materials.texture_set_key`) — 4,641 → 1,409 on the reference city,
  and BeamNG's first performance cost is unique materials per view; the normal maps that were
  rebuilt; and the paint-variant textures.
* `derived/textures/<sha>_metallic.png` / `_roughness.png` — the §7.1 `mask` channels a renderer
  may actually wire, as single-channel PNGs (R, and 255 − A). Every engine wants them separate;
  the `.glb` ships none of them and every glTF consumer rendered the city at one flat roughness
  until it read this.
  **`_ao.png` is gone (product version 2, audit §54).** It was the mask's G channel, and G is not
  ambient occlusion: measured over all 890 masks of the reference city its mean is 9/255 with 91 %
  of texels exactly zero, and of the 18 that clear an "is it light enough to be AO" gate nine are
  uniformly white (a no-op) and nine are bimodal 0/255 selectors — only 2 of 890 (0.22 %) read as
  AO at all, and both are near-white. Binding it darkened every tunnel interior. The channel is not lost — `materials.json`'s `mask` still ships it — but nothing may
  wire it as occlusion, so the bake stops writing 890 PNGs no consumer is allowed to read.
* `derived/textures/<sha>_normal_rgb.png` — a two-channel (AG/RG) normal map rebuilt to RGB, for
  the rare packed one that still reaches disk (`materials.detect_normal_packing`).
* `derived/paint_variants.json` + `derived/textures/<sha>_v<k>.png` — per prefab, K
  representative `colorSet`s (k-means over the instances that carry one; trees carry hundreds
  of distinct sets per prefab, a continuum) and, for the kinds a consumer instances without
  per-instance colour (forest: trees by default), the tinted albedo per (material, variant) via
  `tints.apply_reference` in linear light. Buildings and props are expected to take a
  per-instance palette path in the engine; `paint_kinds` widens the bake when they cannot.

Options read from the bake context: `paint_kinds` (default `["tree"]`), `paint_variants`
(default 4 for trees, 2 otherwise), `variant_max_size` (tinted albedo edge cap, default 1024).
"""

import hashlib
import json
import os
import time
from collections import Counter, defaultdict

import numpy

from .. import colourspace, glb, images, instances, materials, parallel, png, reader, tints, water
from .. import lights as bundle_lights
from . import Product, register

PRODUCT_NAME = "materials"
INPUTS = ("materials", "textures", "colors", "instances", "meshes", "lights")

DEFAULT_PAINT_KINDS = ("tree",)
DEFAULT_PAINT_VARIANTS = {"tree": 4, "prop": 2, "building": 2, "fence": 2, "poi": 2, "decal": 1}

# Worst 8-bit sRGB channel error, over the texels the albedo actually shows, at which a paint
# variant stops being worth a texture of its own and ships a `baseColorFactor` instead. MEASURED,
# not chosen — `tools/measure_paint_factor.py` over all 7,592 (material, variant) pairs of the
# reference city, 2026-08-18:
#
#     maxErr <= 0    31.4 %      <= 8    34.5 %      <= 32   54.5 %
#     maxErr <= 4    32.9 %      <= 16   39.3 %      <= 64   91.9 %
#
# **Nearly a third are EXACTLY zero**: where the colour mask is uniform the tint IS one constant,
# and the texture was pure waste. The curve is flat from 0 to 8 and then climbs, so 4 takes the
# free third at a worst case of 1.6 % of one channel on one texel; 16 and 32 buy 6 and 22 more
# points at errors a viewer can see (the worst pairs are flowerbeds and urns, whose mask touches
# 4 % of the texture and changes those texels completely). 0 keeps every texture — the A3
# behaviour — and the setting is `paint_factor_max_error`.
DEFAULT_FACTOR_MAX_ERROR = 4
QUANT_STEPS = 31  # colour quantisation for counting distinct sets (1/31 ≈ 8 bits/32)


# -- helpers ------------------------------------------------------------------------------------


# Jobs per worker hand-off for this product's four parallel passes. They are all "one small file
# per job" — a mask to split, a normal to rebuild, a mesh to scan, an albedo to tint — so the IPC
# per job dominates unless several travel together. Also the threshold: below this many jobs a
# pool costs more to start than it saves. TUNED.
_CHUNK = 8


def _sha_stem(path):
    """`textures/<sha>.png` → `<sha>`; anything else → a sha of the path."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return hashlib.sha256((path or "").encode("utf-8")).hexdigest()


def _resample_mask(img, h, w):
    """The colour mask at the albedo's size — box-filtered when it is being shrunk.

    The mask is a per-texel BLEND WEIGHT, so averaging it over the texels that collapse into one
    is what the collapse means; taking one of them (`images.resize_nearest`) makes a hard edge out of
    a boundary the source resolves smoothly, and the texel then ships fully tinted or fully
    untinted rather than the mixture. Measured over 40 real (albedo, mask) pairs against tinting
    at 1024 and averaging afterwards: nearest is 1.441/255 RMS with 0.53 % of channel samples off
    by more than 4/255, the box filter 0.532/255 and 0.15 %.

    Only for an exact integer shrink on both axes, which is the only case the cap produces;
    anything else (a mask smaller than its albedo, a non-multiple size) is still nearest, because
    a box filter has nothing to average there.
    """
    if (
        img.shape[0] > h
        and img.shape[1] > w
        and img.shape[0] % h == 0
        and img.shape[1] % w == 0
        and img.shape[0] // h == img.shape[1] // w
    ):
        return png.downsample_box(img, img.shape[0] // h)
    return images.resize_nearest(img, h, w)


def _kmeans(points, weights, k, iterations=12):
    """Weighted Lloyd's k-means on (n, d) points; init = the k heaviest points. Returns centres."""
    n = len(points)
    if n == 0:
        return numpy.zeros((0, points.shape[1] if points.ndim == 2 else 0))
    k = max(1, min(k, n))
    order = numpy.argsort(-weights)
    centres = points[order[:k]].astype(numpy.float64)
    for _ in range(iterations):
        d = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        assign = d.argmin(axis=1)
        new = centres.copy()
        for j in range(k):
            sel = assign == j
            if sel.any():
                w = weights[sel][:, None]
                new[j] = (points[sel] * w).sum(axis=0) / w.sum()
        if numpy.allclose(new, centres):
            break
        centres = new
    return centres


# -- the parallel phases ---------------------------------------------------------------------------
#
# This product is the largest single item in the bake (11m23s of it on the reference city) and
# was entirely single-threaded on an 8-core machine. Three of its phases are embarrassingly
# parallel — 841 mask splits, 847 normal checks, the tinted albedos — and every one of them is
# "read a PNG, do arithmetic on it, write a PNG". The 1.2M-instance colour-set pass stays serial:
# it is a stream, and a pool over it would only move the file reads around.
#
# The jobs are module-level functions taking one tuple, which is what `Pool` (spawn on Windows)
# needs; the same functions run in-process when `jobs <= 1`, so the serial path is the same code.


def _split_mask_job(args):
    """One §7.1 mask -> its metallic (R) and roughness (255 - A) greys. `(rel, outs, error)`."""
    bundle, derived, rel = args
    stem = _sha_stem(rel)
    outs = {
        "metallic": f"textures/{stem}_metallic.png",
        "roughness": f"textures/{stem}_roughness.png",
    }
    src = os.path.join(bundle, rel)
    if not os.path.isfile(src):
        return rel, None, None
    try:
        rgba = png.rgba_array(src)
        png.write_gray(os.path.join(derived, outs["metallic"]), rgba[:, :, 0])
        png.write_gray(os.path.join(derived, outs["roughness"]), 255 - rgba[:, :, 3])
    except Exception as exc:
        return rel, None, repr(exc)
    return rel, outs, None


def _normal_job(args):
    """One normal map: detect its packing and rebuild it to RGB when it is two-channel.
    `(rel, entry_or_None, checked, error)`."""
    bundle, derived, rel = args
    src = os.path.join(bundle, rel)
    if not os.path.isfile(src):
        return rel, None, False, None
    try:
        rgba = png.rgba_array(src)
        stride = max(1, rgba.shape[0] // 64)
        sample = rgba[::stride, ::stride].reshape(-1, 4).astype(numpy.float64) / 255.0
        packing = materials.detect_normal_packing(sample)
        if packing == materials.NORMAL_RGB:
            return rel, None, True, None
        f = rgba.astype(numpy.float64) / 255.0
        x = 2.0 * (f[:, :, 3] if packing == materials.NORMAL_AG else f[:, :, 0]) - 1.0
        y = 2.0 * f[:, :, 1] - 1.0
        z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
        out = numpy.stack([(x + 1) * 0.5, (y + 1) * 0.5, (z + 1) * 0.5, numpy.ones_like(z)], axis=2)
        out8 = numpy.clip(numpy.rint(out * 255.0), 0, 255).astype(numpy.uint8)
        dst = f"textures/{_sha_stem(rel)}_normal_rgb.png"
        png.write_rgba(os.path.join(derived, dst), out8)
    except Exception as exc:
        return rel, None, False, repr(exc)
    return rel, {"packing": packing, "path": dst}, True, None


# The mesh-state suffixes a prefab may ship (§6.1). Every state file of one prefab binds the same
# materials, so any of them names them; all are read because the base file is not guaranteed.
STATE_SUFFIXES = (
    "",
    "__teen",
    "__elderly",
    "__dead",
    "__stump",
    "__partial1",
    "__partial2",
    "__full",
)


def _asset_materials_job(args):
    """Every material name this prefab's mesh states bind. `(asset_id, sorted names)`."""
    meshes_dir, asset_id = args
    names = set()
    for suffix in STATE_SUFFIXES:
        path = os.path.join(meshes_dir, asset_id + suffix + ".glb")
        if not os.path.isfile(path):
            continue
        try:
            nodes = glb.parse_glb(path)
        except Exception:
            continue
        for node in nodes or []:
            for name, _count in node.get("material_runs", []):
                names.add(name)
    return asset_id, sorted(names)


def _tint_job(args):
    """One source albedo tinted into every wanted variant.
    `(base_rel, written, error, {vkey: {"factor", "maxErr", "path"}})`.

    `tints.apply_reference` is the game's §7.2 per-texel `lerp(base, base*c, mask)` in LINEAR
    light, which is why the albedo is de-gamma'd first and re-encoded after.

    **The tint is a pure per-texel MULTIPLIER.** Each channel's step is
    `base*(1-w) + base*c*w = base*((1-w) + w*c)`, so the whole chain is `base * M(texel)` with
    `M` a function of the colour mask and the centre alone — never of the albedo. That is what
    makes a single `baseColorFactor` per variant a candidate: it costs ZERO textures where a
    tinted albedo costs a megabyte, and the error it makes is directly measurable against the
    exact image this function already computes. So both are produced: the constant that minimises
    the (alpha-weighted) squared error, the worst 8-bit sRGB channel error it makes, and the
    texture ONLY where that error exceeds `paint_factor_max_error`. A threshold of 0 keeps every
    texture, which is the pre-2026-08-18 behaviour.

    **Where `M` is constant the error is exactly 0 and nothing needs measuring.** A colour mask
    that is uniform over the whole texture — 31.4 % of the 7,592 (albedo, variant) pairs on the
    reference city — makes `M` a single RGB triple, so the "approximation" IS the exact image and
    the two sRGB encodes that measured the difference between them were 23 minutes of proving a
    tautology. That case now takes the constant straight from the mask value and never decodes
    the albedo at all. Where `M` does vary, the approximate image still comes from a 256-entry
    lookup table per channel rather than a full `power()` over the texture: `base` is uint8 and
    the constant is constant, so `f(g(v)·c)` has 256 distinct answers, and the LUT is the same
    arithmetic on the same values — bit-identical output, one encode per pair instead of two.
    """
    bundle, derived, base_rel, mask_rel, variants, variant_max, max_error = args
    try:
        mask = png.rgba_array(os.path.join(bundle, mask_rel))
    except Exception as exc:
        return base_rel, 0, repr(exc), {}
    # Uniformity is decided on the FULL-RESOLUTION mask: nearest-resampling a uniform channel
    # keeps it uniform, so this can only under-claim, never over-claim.
    if mask.size and bool(
        numpy.all(mask[:, :, :3].min(axis=(0, 1)) == mask[:, :, :3].max(axis=(0, 1)))
    ):
        mask_lo = mask[:, :, :3].min(axis=(0, 1)).astype(numpy.float64) / 255.0
        out = {}
        for vkey, centre in variants:
            constant = tints.multiplier(mask_lo, [(*tuple(centre[i]), 1.0) for i in range(3)])
            out[vkey] = {
                "factor": [round(float(v), 5) for v in constant],
                "maxErr": 0,
                "path": None,
            }
        return base_rel, 0, None, out

    try:
        base = png.rgba_array(os.path.join(bundle, base_rel))
    except Exception as exc:
        return base_rel, 0, repr(exc), {}
    h, w = base.shape[:2]
    factor = 1
    while max(h, w) // factor > variant_max:
        factor *= 2
    if factor > 1:
        base = png.downsample_box(base, factor)
        h, w = base.shape[:2]
    mask_r = _resample_mask(mask, h, w).astype(numpy.float64) / 255.0
    base_lin = colourspace.srgb8_to_linear(base[:, :, :3])
    alpha = base[:, :, 3].astype(numpy.float64) / 255.0
    visible = alpha > 0.5
    any_visible = bool(visible.any())
    written = 0
    out = {}
    for vkey, centre in variants:
        colours = [(*tuple(centre[i]), 1.0) for i in range(3)]
        multiplier = tints.multiplier(mask_r[:, :, :3], colours)
        lo = multiplier.min(axis=(0, 1))
        if numpy.array_equal(lo, multiplier.max(axis=(0, 1))):
            # this variant's centre flattened a mask that is not itself flat (every channel it
            # touches is 1.0): same tautology, same answer
            out[vkey] = {"factor": [round(float(v), 5) for v in lo], "maxErr": 0, "path": None}
            continue
        constant = tints.best_constant(multiplier, alpha)
        exact8 = colourspace.linear_to_srgb8(base_lin * multiplier)
        approx8 = _apply_lut(_srgb8_lut(constant), base[:, :, :3])
        delta = numpy.abs(exact8.astype(numpy.int16) - approx8.astype(numpy.int16))
        if any_visible:
            delta = delta[visible]
        max_err = int(delta.max()) if delta.size else 0
        rel = f"textures/{_sha_stem(base_rel)}_v{vkey}.png"
        path = os.path.join(derived, rel)
        if max_err > int(max_error):
            # A file already there is reused — the encode is deterministic — UNLESS it is the
            # wrong size. `variant_max_size` is a shaping option, so changing it re-runs this
            # product, and a name that does not carry the size would otherwise keep serving the
            # old cap's pixels for ever.
            if _png_size(path) != (h, w):
                png.write_rgba(path, numpy.concatenate([exact8, base[:, :, 3:4]], axis=2))
                written += 1
            out[vkey] = {
                "factor": [round(float(v), 5) for v in constant],
                "maxErr": max_err,
                "path": rel,
            }
        else:
            out[vkey] = {
                "factor": [round(float(v), 5) for v in constant],
                "maxErr": max_err,
                "path": None,
            }
    return base_rel, written, None, out


def _png_size(path):
    """`(height, width)` from a PNG's IHDR, or `None` when there is no readable file there.

    Row-major, the way numpy reports a shape — the opposite order to `png.read_header`, which is
    why this swaps rather than simply forwarding.
    """
    header = png.read_header(path)
    return None if header is None else (header[1], header[0])


def _srgb8_lut(constant):
    """`(3, 256)` uint8: `colourspace.linear_to_srgb8(colourspace.srgb8_to_linear(v) * constant[j])` for every byte `v`.

    The albedo is uint8, so a constant multiplier has only 256 possible answers per channel and
    the whole `power()` over the texture is a gather. Same function, same inputs, same bits.
    """
    lin = colourspace.srgb8_to_linear(numpy.arange(256, dtype=numpy.uint8))
    return numpy.stack([colourspace.linear_to_srgb8(lin * float(constant[j])) for j in range(3)])


def _apply_lut(lut, rgb8):
    """`(h, w, 3)` uint8 through a per-channel `(3, 256)` table."""
    return numpy.stack([lut[j][rgb8[:, :, j]] for j in range(3)], axis=2)


# -- the product --------------------------------------------------------------------------------

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


def _emissive_slots(bundle, resolved, progress):
    """Join each mesh's colour slots to its emissive materials and write CS2's own emission into
    the resolved records. Returns the stats the product reports.

    **What this closes.** `materials.json` carries no `emissiveFactor` on any material — CS2 does
    not drive emission from a static surface property, it drives it per-instance from the render
    prefab's `ProceduralLight` buffer, which is `lights.json`'s 1,896 colour slots. Without the
    join every emissive material in the city is a map with no colour and no strength behind it.

    **The join, and why one half of it is an ordinal.** The game's own key is
    `EmissiveProperties.SingleLightMapping.materialId` — a slot names one material SLOT of the
    mesh, and a slot is the position of the primitive that uses it. `AppearanceRipSystem` ships
    that id as of 2026-08-19, so on a bundle ripped since then the join is exact and this
    function's ordinal path never runs.

    On an older bundle the id is absent (it wrote `slot: i` and nothing else), and the slots of a
    mesh are matched to that mesh's emissive materials **in `.glb` primitive order** — the same
    order — but only where the two counts agree. Measured on the 2026-08-17 reference rip, of the
    781 slot-carrying meshes whose `.glb` is in the bundle:

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
            pairs = [
                (primitives[i], e) for i, e in by_id if 0 <= i < len(primitives) and primitives[i]
            ]
        else:
            names = []
            for name in primitives:
                if name and name not in names:
                    names.append(name)
            emissive = [n for n in names if (resolved.get(n) or {}).get("emissive")]
            if not emissive or len(emissive) != len(slots):
                stats["slotMeshesAmbiguous"] += 1
                continue
            stats["slotMeshesJoined"] += 1
            pairs = list(zip(emissive, slots))

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


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    paint_kinds = tuple(options.get("paint_kinds") or DEFAULT_PAINT_KINDS)
    variant_counts = dict(DEFAULT_PAINT_VARIANTS, **(options.get("paint_variants") or {}))
    variant_max = int(options.get("variant_max_size") or 1024)
    jobs = max(1, int(options.get("jobs") or 1))
    factor_max_error = int(options.get("paint_factor_max_error", DEFAULT_FACTOR_MAX_ERROR) or 0)
    tex_out = os.path.join(derived, "textures")
    os.makedirs(tex_out, exist_ok=True)
    stats = {}

    # 1. resolve + dedup ------------------------------------------------------------------------
    progress.begin("materials: resolve", total=None, unit="materials")
    catalogue = reader.load_materials(bundle) or {}
    appearance = water.load(bundle)
    resolved = materials.resolve_all(catalogue, appearance)
    progress.end(f"{len(resolved)} materials resolved")
    # BEFORE the dedup, deliberately: `texture_set_key` includes the emissive factor and nits, so
    # two materials that share a texture set but take different slot colours must not collapse
    # into one.
    stats["emissiveSlots"] = _emissive_slots(bundle, resolved, progress)
    progress.begin("materials: dedup", total=None, unit="materials")
    canonical_of = {}
    first_by_key = {}
    for key in sorted(resolved):
        tkey = materials.texture_set_key(resolved[key])
        canonical_of[key] = first_by_key.setdefault(tkey, key)
    aliases = {k: v for k, v in canonical_of.items() if k != v}
    stats["materials"] = len(resolved)
    stats["distinctTextureSets"] = len(first_by_key)
    progress.end(f"{len(resolved)} materials, {len(first_by_key)} distinct texture sets")

    # 2. split masks ----------------------------------------------------------------------------
    mask_paths = sorted({r["mask"] for r in resolved.values() if r.get("mask")})
    progress.begin("materials: split masks", total=len(mask_paths), unit="masks")
    split_of = {}
    written = 0
    for rel, outs, error in parallel.each(
        _split_mask_job,
        [(bundle, derived, rel) for rel in mask_paths],
        jobs,
        progress,
        chunksize=_CHUNK,
    ):
        if error:
            progress.log(f"mask {rel}: unreadable ({error}), skipped")
        elif outs:
            split_of[rel] = outs
            written += 1
    progress.end(f"{written} masks split into metallic/roughness ({jobs} worker(s))")
    stats["masksSplit"] = written

    # 3. rebuild packed normals -----------------------------------------------------------------
    normal_paths = sorted({r["normal"] for r in resolved.values() if r.get("normal")})
    progress.begin("materials: normals", total=len(normal_paths), unit="maps")
    rebuilt = {}
    checked = 0
    for rel, entry, ok, error in parallel.each(
        _normal_job,
        [(bundle, derived, rel) for rel in normal_paths],
        jobs,
        progress,
        chunksize=_CHUNK,
    ):
        if error:
            progress.log(f"normal {rel}: unreadable ({error}), skipped")
            continue
        checked += 1 if ok else 0
        if entry:
            rebuilt[rel] = entry
    progress.end(
        f"{checked} normal maps checked, {len(rebuilt)} rebuilt from a two-channel packing "
        f"({jobs} worker(s))"
    )
    stats["normalsRebuilt"] = len(rebuilt)

    # 4. paint variants -------------------------------------------------------------------------
    manifest = context.manifest or {}
    total_instances = int(((manifest.get("counts") or {}).get("instances")) or 0) or None
    progress.begin(
        "materials: paint variants (instances pass)", total=total_instances, unit="instances"
    )
    sets_by_asset = defaultdict(Counter)
    kind_of_asset = {}
    seen = 0
    for record in instances.read_instances(bundle):
        seen += 1
        if seen % 20000 == 0:
            progress.advance(20000)
            progress.check()
        cs = (record.get("attrs") or {}).get("colorSet")
        if not cs:
            continue
        key = tuple(int(round(float(c) * QUANT_STEPS)) for col in cs[:3] for c in col[:3])
        if len(key) != 9:
            key = key + (QUANT_STEPS,) * (9 - len(key))
        sets_by_asset[record["assetId"]][key] += 1
        kind_of_asset[record["assetId"]] = record.get("kind")
    progress.end(f"{len(sets_by_asset)} prefabs carry colour sets")

    variants = {}
    for asset_id, counter in sets_by_asset.items():
        kind = kind_of_asset.get(asset_id) or "prop"
        k = int(variant_counts.get(kind, 2))
        keys = numpy.asarray(list(counter.keys()), dtype=numpy.float64) / QUANT_STEPS
        weights = numpy.asarray(list(counter.values()), dtype=numpy.float64)
        if len(keys) <= k:
            centres = keys
        else:
            centres = _kmeans(keys, weights, k)
        # assign counts to nearest centre for the record
        d = ((keys[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        assign = d.argmin(axis=1)
        counts = [float(weights[assign == j].sum()) for j in range(len(centres))]
        variants[asset_id] = {
            "kind": kind,
            "instances": int(weights.sum()),
            "distinct": len(keys),
            "variants": [
                [[round(float(v), 4) for v in centre[3 * i : 3 * i + 3]] for i in range(3)]
                for centre in centres
            ],
            "counts": [int(c) for c in counts],
        }
    stats["paintPrefabs"] = len(variants)

    # tinted albedos for the paint kinds: which materials belong to which prefab? the .glb says.
    #
    # Three passes rather than one nested loop, because the work parallelises at two different
    # grains and the middle step has to see everything at once. (1) per prefab, parse its `.glb`s
    # for material names - independent, so a pool; (2) serially invert that into "per SOURCE
    # ALBEDO, which variant centres are wanted", which is also what keeps two materials sharing a
    # base texture from racing on the same output file; (3) per source albedo, tint - independent
    # again, and the phase that reads and writes the megabytes.
    paint_assets = [a for a, v in variants.items() if v["kind"] in paint_kinds]
    lay = reader.resolve_layers(bundle)
    progress.begin("materials: prefab materials", total=len(paint_assets), unit="prefabs")
    materials_of = {}
    for asset_id, names in parallel.each(
        _asset_materials_job,
        [(lay.meshes, a) for a in paint_assets],
        jobs,
        progress,
        chunksize=_CHUNK,
    ):
        materials_of[asset_id] = names
    progress.end(f"{len(paint_assets)} prefabs of kind {list(paint_kinds)} named their materials")

    # (2) group by source albedo. `out_rel` is a function of the base texture and the variant key
    # alone, so grouping here is what makes the parallel phase race-free: exactly one job owns
    # each output file. Where two materials share a base but declare different colour masks the
    # lexicographically first mask wins for all of them - the serial code had the same collision
    # and resolved it by iteration order, which was not reproducible.
    by_base = {}  # base_rel -> {"mask": rel, "variants": {vkey: centre}}
    tinted = {}  # material -> {variantKey: path}
    for asset_id in paint_assets:
        for name in materials_of.get(asset_id) or ():
            rec = resolved.get(name)
            if not rec or not rec.get("tintable") or not rec.get("baseColor"):
                continue
            base_rel, mask_rel = rec["baseColor"], rec["colorMask"]
            entry = by_base.setdefault(base_rel, {"mask": mask_rel, "variants": {}})
            if mask_rel and (not entry["mask"] or mask_rel < entry["mask"]):
                entry["mask"] = mask_rel
            for centre in variants[asset_id]["variants"]:
                vkey = hashlib.sha256(json.dumps(centre).encode("utf-8")).hexdigest()[:12]
                entry["variants"].setdefault(vkey, centre)
                tinted.setdefault(name, {})[vkey] = f"textures/{_sha_stem(base_rel)}_v{vkey}.png"

    progress.begin("materials: tinted albedos", total=len(by_base), unit="albedos")
    args = [
        (
            bundle,
            derived,
            base_rel,
            e["mask"],
            sorted(e["variants"].items()),
            variant_max,
            factor_max_error,
        )
        for base_rel, e in sorted(by_base.items())
    ]
    textures_written = 0
    failed_bases = set()
    per_base = {}  # base_rel -> {vkey: {"factor", "maxErr", "path"}}
    for base_rel, count, error, table in parallel.each(
        _tint_job, args, jobs, progress, chunksize=_CHUNK
    ):
        if error:
            progress.log(f"tint {base_rel}: {error}, skipped")
            failed_bases.add(base_rel)
            continue
        textures_written += count
        per_base[base_rel] = table
    # A variant whose constant is close enough ships NO texture: the consumer multiplies the
    # canonical albedo by `factor` instead. Both halves are recorded either way, so the split is
    # a measured number in the report rather than a claim.
    factors = {}  # material -> {vkey: {"factor", "maxErr"}}
    as_factor = as_texture = 0
    for name, table in list(tinted.items()):
        rec = resolved.get(name) or {}
        entries = per_base.get(rec.get("baseColor")) or {}
        keep = {}
        for vkey in list(table):
            entry = entries.get(vkey)
            if entry is None:
                continue
            factors.setdefault(name, {})[vkey] = {
                "factor": entry["factor"],
                "maxErr": entry["maxErr"],
            }
            if entry["path"]:
                keep[vkey] = entry["path"]
                as_texture += 1
            else:
                as_factor += 1
        if keep:
            tinted[name] = keep
        else:
            tinted.pop(name)
    for name in [n for n in tinted if (resolved.get(n) or {}).get("baseColor") in failed_bases]:
        tinted.pop(name)
    progress.end(
        f"{textures_written} tinted albedo textures over {len(by_base)} source albedos for "
        f"{len(paint_assets)} prefabs of kind {list(paint_kinds)}: {as_factor} (material, variant) "
        f"pairs ship a baseColorFactor and {as_texture} need the texture "
        f"(threshold {factor_max_error}/255, {jobs} worker(s))"
    )
    stats["tintedTextures"] = textures_written
    stats["tintedSourceAlbedos"] = len(by_base)
    stats["variantsAsFactor"] = as_factor
    stats["variantsAsTexture"] = as_texture
    stats["paintFactorMaxError"] = int(factor_max_error)

    # 5. write the tables -----------------------------------------------------------------------
    table = {}
    for key, rec in resolved.items():
        entry = dict(rec)
        entry["canonical"] = canonical_of[key]
        derived_maps = {}
        if rec.get("mask") and rec["mask"] in split_of:
            derived_maps.update(split_of[rec["mask"]])
        if rec.get("normal") and rec["normal"] in rebuilt:
            derived_maps["normalRgb"] = rebuilt[rec["normal"]]["path"]
        if key in tinted:
            derived_maps["variants"] = tinted[key]
        if key in factors:
            derived_maps["variantFactors"] = factors[key]
        entry["derived"] = derived_maps
        # tuples are not JSON
        for k2, v2 in list(entry.items()):
            if isinstance(v2, tuple):
                entry[k2] = list(v2)
        table[key] = entry
    document = {
        "schema": "csiibundle.bake.materials/1",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "materials": table,
        "aliases": aliases,
        "normalsRebuilt": rebuilt,
        "paintKinds": list(paint_kinds),
    }
    with open(
        os.path.join(derived, "materials.json"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
    with open(
        os.path.join(derived, "paint_variants.json"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(
            {"schema": "csiibundle.bake.paint_variants/1", "prefabs": variants},
            handle,
            indent=1,
            sort_keys=True,
        )
    return stats


# version 3: every paint variant records the constant `baseColorFactor` that approximates its
# per-texel tint and the error that makes, and ships a texture only where the error is too big
# version 4: the colour mask is box-filtered rather than nearest-sampled when it is shrunk to
# the albedo's capped size, which moves every tinted texel at a mask boundary (`_resample_mask`)
# version 5: `lights.json`'s colour slots are joined to each mesh's emissive materials, so a
# material carries CS2's own emissive COLOUR and STRENGTH instead of nothing at all
# (`_emissive_slots`); the join runs before the dedup because the key includes both
# version 6: the join takes the game's own `materialId` when the rip ships it (2026-08-19 and
# after), which retires the ordinal guess entirely - 765 of 778 meshes exact, 0 ambiguous, and
# the 13 that are left name a material LAYER and are counted as such rather than as a failure
register(
    Product(
        PRODUCT_NAME,
        INPUTS,
        "materials.json",
        run,
        version=6,
        options=("paint_kinds", "paint_variants", "variant_max_size", "paint_factor_max_error"),
    )
)
