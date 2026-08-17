"""Bake product `materials` — the resolved PBR table, split maps, rebuilt normals, paint variants.

What a renderer needs from `materials.json` is decided once here (contract §13):

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

from .. import glb, instances, materials, png, reader, tints, water
from . import Product, register

PRODUCT_NAME = "materials"
INPUTS = ("materials", "textures", "colors", "instances", "meshes")

DEFAULT_PAINT_KINDS = ("tree",)
DEFAULT_PAINT_VARIANTS = {"tree": 4, "prop": 2, "building": 2, "fence": 2, "poi": 2, "decal": 1}
QUANT_STEPS = 31            # colour quantisation for counting distinct sets (1/31 ≈ 8 bits/32)


# -- helpers ------------------------------------------------------------------------------------

def _sha_stem(path):
    """`textures/<sha>.png` → `<sha>`; anything else → a sha of the path."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return hashlib.sha256((path or "").encode("utf-8")).hexdigest()


def _srgb_to_linear(arr):
    a = arr.astype(numpy.float64) / 255.0
    return numpy.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb8(arr):
    a = numpy.clip(arr, 0.0, 1.0)
    s = numpy.where(a <= 0.0031308, a * 12.92, 1.055 * numpy.power(a, 1 / 2.4) - 0.055)
    return numpy.clip(numpy.rint(s * 255.0), 0, 255).astype(numpy.uint8)


def _resample_nearest(img, h, w):
    if img.shape[0] == h and img.shape[1] == w:
        return img
    rows = numpy.clip((numpy.arange(h) * img.shape[0] / h).astype(numpy.int64), 0, img.shape[0] - 1)
    cols = numpy.clip((numpy.arange(w) * img.shape[1] / w).astype(numpy.int64), 0, img.shape[1] - 1)
    return img[rows][:, cols]


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


# -- the product --------------------------------------------------------------------------------

def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    paint_kinds = tuple(options.get("paint_kinds") or DEFAULT_PAINT_KINDS)
    variant_counts = dict(DEFAULT_PAINT_VARIANTS, **(options.get("paint_variants") or {}))
    variant_max = int(options.get("variant_max_size") or 1024)
    tex_out = os.path.join(derived, "textures")
    os.makedirs(tex_out, exist_ok=True)
    stats = {}

    # 1. resolve + dedup ------------------------------------------------------------------------
    progress.begin("materials: resolve", total=None, unit="materials")
    catalogue = reader.load_materials(bundle) or {}
    appearance = water.load(bundle)
    resolved = materials.resolve_all(catalogue, appearance)
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
    for i, rel in enumerate(mask_paths):
        progress.check()
        src = os.path.join(bundle, rel)
        stem = _sha_stem(rel)
        outs = {"metallic": f"textures/{stem}_metallic.png",
                "roughness": f"textures/{stem}_roughness.png"}
        if os.path.isfile(src):
            try:
                rgba = png.rgba_array(src)
                png.write_gray(os.path.join(derived, outs["metallic"]), rgba[:, :, 0])
                png.write_gray(os.path.join(derived, outs["roughness"]), 255 - rgba[:, :, 3])
                split_of[rel] = outs
                written += 1
            except Exception as exc:  # noqa: BLE001 - one bad texture must not stop the bake
                progress.log(f"mask {rel}: unreadable ({exc!r}), skipped")
        if i % 25 == 0:
            progress.advance(min(25, len(mask_paths) - i))
    progress.end(f"{written} masks split into metallic/roughness")
    stats["masksSplit"] = written

    # 3. rebuild packed normals -----------------------------------------------------------------
    normal_paths = sorted({r["normal"] for r in resolved.values() if r.get("normal")})
    progress.begin("materials: normals", total=len(normal_paths), unit="maps")
    rebuilt = {}
    checked = 0
    for i, rel in enumerate(normal_paths):
        progress.check()
        src = os.path.join(bundle, rel)
        if os.path.isfile(src):
            try:
                rgba = png.rgba_array(src)
                stride = max(1, rgba.shape[0] // 64)
                sample = rgba[::stride, ::stride].reshape(-1, 4).astype(numpy.float64) / 255.0
                packing = materials.detect_normal_packing(sample)
                checked += 1
                if packing != materials.NORMAL_RGB:
                    f = rgba.astype(numpy.float64) / 255.0
                    x = 2.0 * (f[:, :, 3] if packing == materials.NORMAL_AG else f[:, :, 0]) - 1.0
                    y = 2.0 * f[:, :, 1] - 1.0
                    z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
                    out = numpy.stack([(x + 1) * 0.5, (y + 1) * 0.5, (z + 1) * 0.5, numpy.ones_like(z)], axis=2)
                    out8 = numpy.clip(numpy.rint(out * 255.0), 0, 255).astype(numpy.uint8)
                    stem = _sha_stem(rel)
                    dst = f"textures/{stem}_normal_rgb.png"
                    png.write_rgba(os.path.join(derived, dst), out8)
                    rebuilt[rel] = {"packing": packing, "path": dst}
            except Exception as exc:  # noqa: BLE001
                progress.log(f"normal {rel}: unreadable ({exc!r}), skipped")
        if i % 25 == 0:
            progress.advance(min(25, len(normal_paths) - i))
    progress.end(f"{checked} normal maps checked, {len(rebuilt)} rebuilt from a two-channel packing")
    stats["normalsRebuilt"] = len(rebuilt)

    # 4. paint variants -------------------------------------------------------------------------
    manifest = context.manifest or {}
    total_instances = int(((manifest.get("counts") or {}).get("instances")) or 0) or None
    progress.begin("materials: paint variants (instances pass)", total=total_instances, unit="instances")
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
            "distinct": int(len(keys)),
            "variants": [[[round(float(v), 4) for v in centre[3 * i:3 * i + 3]] for i in range(3)] for centre in centres],
            "counts": [int(c) for c in counts],
        }
    stats["paintPrefabs"] = len(variants)

    # tinted albedos for the paint kinds: which materials belong to which prefab? the .glb says.
    paint_assets = [a for a, v in variants.items() if v["kind"] in paint_kinds]
    progress.begin("materials: tinted albedos", total=len(paint_assets), unit="prefabs")
    lay = reader.resolve_layers(bundle)
    tinted = {}                                     # material -> {variantKey: path}
    albedo_cache = {}
    mask_cache = {}
    textures_written = 0
    for asset_id in paint_assets:
        progress.check()
        progress.advance()
        # every state file of the prefab shares its materials; the base file is enough to name them
        candidates = [asset_id] + [asset_id + suffix for suffix in ("__teen", "__elderly", "__dead", "__stump",
                                                                    "__partial1", "__partial2", "__full")]
        material_names = set()
        for cand in candidates:
            path = os.path.join(lay.meshes, cand + ".glb")
            if not os.path.isfile(path):
                continue
            nodes = glb.parse_glb(path)
            for node in nodes or []:
                for name, _count in node.get("material_runs", []):
                    material_names.add(name)
        for name in sorted(material_names):
            rec = resolved.get(name)
            if not rec or not rec.get("tintable") or not rec.get("baseColor"):
                continue
            base_rel, mask_rel = rec["baseColor"], rec["colorMask"]
            if base_rel not in albedo_cache:
                try:
                    albedo_cache[base_rel] = png.rgba_array(os.path.join(bundle, base_rel))
                except Exception:  # noqa: BLE001
                    albedo_cache[base_rel] = None
            if mask_rel not in mask_cache:
                try:
                    mask_cache[mask_rel] = png.rgba_array(os.path.join(bundle, mask_rel))
                except Exception:  # noqa: BLE001
                    mask_cache[mask_rel] = None
            base = albedo_cache[base_rel]
            mask = mask_cache[mask_rel]
            if base is None or mask is None:
                continue
            h, w = base.shape[:2]
            factor = 1
            while max(h, w) // factor > variant_max:
                factor *= 2
            if factor > 1:
                base = png.downsample_box(base, factor)
                h, w = base.shape[:2]
            mask_r = _resample_nearest(mask, h, w).astype(numpy.float64) / 255.0
            base_lin = _srgb_to_linear(base[:, :, :3])
            for k, centre in enumerate(variants[asset_id]["variants"]):
                colours = [tuple(centre[i]) + (1.0,) for i in range(3)]
                vkey = hashlib.sha256(json.dumps(centre).encode("utf-8")).hexdigest()[:12]
                out_rel = f"textures/{_sha_stem(base_rel)}_v{vkey}.png"
                if out_rel in tinted.setdefault(name, {}).values():
                    continue
                out_path = os.path.join(derived, out_rel)
                if not os.path.isfile(out_path):
                    tinted_lin = tints.apply_reference(base_lin, mask_r[:, :, :3], colours)
                    out = numpy.concatenate([_linear_to_srgb8(tinted_lin), base[:, :, 3:4]], axis=2)
                    png.write_rgba(out_path, out)
                    textures_written += 1
                tinted[name][vkey] = out_rel
    progress.end(f"{textures_written} tinted albedo textures for {len(paint_assets)} prefabs of kind {list(paint_kinds)}")
    stats["tintedTextures"] = textures_written

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
        entry["derived"] = derived_maps
        # tuples are not JSON
        for k2, v2 in list(entry.items()):
            if isinstance(v2, tuple):
                entry[k2] = list(v2)
        table[key] = entry
    document = {"schema": "csiibundle.bake.materials/1", "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "materials": table, "aliases": aliases, "normalsRebuilt": rebuilt,
                "paintKinds": list(paint_kinds)}
    with open(os.path.join(derived, "materials.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
    with open(os.path.join(derived, "paint_variants.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"schema": "csiibundle.bake.paint_variants/1", "prefabs": variants}, handle, indent=1, sort_keys=True)
    return stats


register(Product(PRODUCT_NAME, INPUTS, "materials.json", run, version=2))
