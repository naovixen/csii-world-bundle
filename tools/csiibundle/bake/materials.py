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

import json
import os
import time

from .. import materials, parallel, reader, water
from . import Product, emissive, materialmaps, paint, register

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


# Jobs per worker hand-off for this product's parallel passes: `materialmaps.CHUNK`.

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


def _resolve_and_dedup(bundle, progress, stats):
    """§13's resolve + the alias map. Emissive slots join BEFORE the dedup, deliberately:
    `texture_set_key` includes the emissive factor and nits, so two materials that share a
    texture set but take different slot colours must not collapse into one."""
    progress.begin("materials: resolve", total=None, unit="materials")
    catalogue = reader.load_materials(bundle) or {}
    appearance = water.load(bundle)
    resolved = materials.resolve_all(catalogue, appearance)
    progress.end(f"{len(resolved)} materials resolved")
    stats["emissiveSlots"] = emissive.emissive_slots(bundle, resolved, progress)
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
    return resolved, canonical_of, aliases


def _split_masks(bundle, derived, resolved, jobs, progress, stats):
    """§7.1 masks into metallic/roughness greys, one file per job."""
    mask_paths = sorted({r["mask"] for r in resolved.values() if r.get("mask")})
    progress.begin("materials: split masks", total=len(mask_paths), unit="masks")
    split_of = {}
    written = 0
    for rel, outs, error in parallel.each(
        materialmaps.split_mask_job,
        [(bundle, derived, rel) for rel in mask_paths],
        jobs,
        progress,
        chunksize=materialmaps.CHUNK,
    ):
        if error:
            progress.log(f"mask {rel}: unreadable ({error}), skipped")
        elif outs:
            split_of[rel] = outs
            written += 1
    progress.end(f"{written} masks split into metallic/roughness ({jobs} worker(s))")
    stats["masksSplit"] = written
    return split_of


def _rebuild_normals(bundle, derived, resolved, jobs, progress, stats):
    """Detect each normal map's packing; rebuild the two-channel ones to RGB."""
    normal_paths = sorted({r["normal"] for r in resolved.values() if r.get("normal")})
    progress.begin("materials: normals", total=len(normal_paths), unit="maps")
    rebuilt = {}
    checked = 0
    for rel, entry, ok, error in parallel.each(
        materialmaps.normal_job,
        [(bundle, derived, rel) for rel in normal_paths],
        jobs,
        progress,
        chunksize=materialmaps.CHUNK,
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
    return rebuilt


def _write_tables(
    derived,
    resolved,
    canonical_of,
    aliases,
    split_of,
    rebuilt,
    tinted,
    factors,
    variants,
    paint_kinds,
):
    """`derived/materials.json` + `derived/paint_variants.json`, the §13 tables."""
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


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    paint_kinds = tuple(options.get("paint_kinds") or DEFAULT_PAINT_KINDS)
    variant_counts = dict(DEFAULT_PAINT_VARIANTS, **(options.get("paint_variants") or {}))
    variant_max = int(options.get("variant_max_size") or 1024)
    jobs = max(1, int(options.get("jobs") or 1))
    factor_max_error = int(options.get("paint_factor_max_error", DEFAULT_FACTOR_MAX_ERROR) or 0)
    os.makedirs(os.path.join(derived, "textures"), exist_ok=True)
    stats = {}

    resolved, canonical_of, aliases = _resolve_and_dedup(bundle, progress, stats)
    split_of = _split_masks(bundle, derived, resolved, jobs, progress, stats)
    rebuilt = _rebuild_normals(bundle, derived, resolved, jobs, progress, stats)
    variants = paint.collect_variants(bundle, context, variant_counts, progress, stats)
    tinted, factors = paint.tint_albedos(
        bundle,
        derived,
        resolved,
        variants,
        (paint_kinds, variant_max, factor_max_error),
        jobs,
        progress,
        stats,
    )
    _write_tables(
        derived,
        resolved,
        canonical_of,
        aliases,
        split_of,
        rebuilt,
        tinted,
        factors,
        variants,
        paint_kinds,
    )
    return stats


# version 3: every paint variant records the constant `baseColorFactor` that approximates its
# per-texel tint and the error that makes, and ships a texture only where the error is too big
# version 4: the colour mask is box-filtered rather than nearest-sampled when it is shrunk to
# the albedo's capped size, which moves every tinted texel at a mask boundary (`resample_mask`)
# version 5: `lights.json`'s colour slots are joined to each mesh's emissive materials, so a
# material carries CS2's own emissive COLOUR and STRENGTH instead of nothing at all
# (`emissive.emissive_slots`); the join runs before the dedup because the key includes both
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
