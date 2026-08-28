"""The `materials` product's §7.2 paint pipeline — variant selection, grouping, factors.

Which colour sets a prefab is painted with (weighted k-means over its instances), which
materials those prefabs bind (their mesh states name them), which SOURCE ALBEDO each tint job
owns (grouping is what makes the parallel phase race-free), and whether a variant ships a
texture or a single `baseColorFactor` (measured per pair by `materialmaps.tint_job`).
"""

import hashlib
import json
import os
from collections import Counter, defaultdict

import numpy

from .. import glb, instances, parallel, reader
from . import materialmaps
from .materialmaps import sha_stem, tint_job

QUANT_STEPS = 31  # colour quantisation for counting distinct sets (1/31 ≈ 8 bits/32)


def kmeans(points, weights, k, iterations=12):
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


def asset_materials_job(args):
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


def collect_variants(bundle, context, variant_counts, progress, stats):
    """K representative colorSets per prefab: a stream over the instance layer, then a weighted
    k-means per prefab (trees carry hundreds of distinct sets — a continuum)."""
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
        centres = keys if len(keys) <= k else kmeans(keys, weights, k)
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
    return variants


def prefab_materials(bundle, paint_assets, paint_kinds, jobs, progress):
    """Which materials each paint-kind prefab binds — its mesh states name them."""
    lay = reader.resolve_layers(bundle)
    progress.begin("materials: prefab materials", total=len(paint_assets), unit="prefabs")
    materials_of = {}
    for asset_id, names in parallel.each(
        asset_materials_job,
        [(lay.meshes, a) for a in paint_assets],
        jobs,
        progress,
        chunksize=materialmaps.CHUNK,
    ):
        materials_of[asset_id] = names
    progress.end(f"{len(paint_assets)} prefabs of kind {list(paint_kinds)} named their materials")
    return materials_of


def group_by_base(paint_assets, materials_of, resolved, variants):
    """Group wanted variants by SOURCE ALBEDO. `out_rel` is a function of the base texture and
    the variant key alone, so grouping here is what makes the parallel phase race-free: exactly
    one job owns each output file. Where two materials share a base but declare different colour
    masks the lexicographically first mask wins for all of them - the serial code had the same
    collision and resolved it by iteration order, which was not reproducible."""
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
                path = f"textures/{sha_stem(base_rel)}_v{vkey}.png"
                tinted.setdefault(name, {})[vkey] = path
    return by_base, tinted


def apply_factor_split(resolved, tinted, per_base, failed_bases, stats):
    """A variant whose constant is close enough ships NO texture: the consumer multiplies the
    canonical albedo by `factor` instead. Both halves are recorded either way, so the split is
    a measured number in the report rather than a claim."""
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
    stats["variantsAsFactor"] = as_factor
    stats["variantsAsTexture"] = as_texture
    return factors, as_factor, as_texture


def tint_albedos(bundle, derived, resolved, variants, options, jobs, progress, stats):
    """The tinted-albedo pipeline: three passes rather than one nested loop, because the work
    parallelises at two different grains and the middle step has to see everything at once.
    (1) per prefab, parse its `.glb`s for material names - independent, so a pool; (2) serially
    invert that into "per SOURCE ALBEDO, which variant centres are wanted"; (3) per source
    albedo, tint - independent again, and the phase that reads and writes the megabytes."""
    paint_kinds, variant_max, factor_max_error = options
    paint_assets = [a for a, v in variants.items() if v["kind"] in paint_kinds]
    materials_of = prefab_materials(bundle, paint_assets, paint_kinds, jobs, progress)
    by_base, tinted = group_by_base(paint_assets, materials_of, resolved, variants)

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
        tint_job, args, jobs, progress, chunksize=materialmaps.CHUNK
    ):
        if error:
            progress.log(f"tint {base_rel}: {error}, skipped")
            failed_bases.add(base_rel)
            continue
        textures_written += count
        per_base[base_rel] = table
    factors, as_factor, as_texture = apply_factor_split(
        resolved, tinted, per_base, failed_bases, stats
    )
    progress.end(
        f"{textures_written} tinted albedo textures over {len(by_base)} source albedos for "
        f"{len(paint_assets)} prefabs of kind {list(paint_kinds)}: {as_factor} (material, variant) "
        f"pairs ship a baseColorFactor and {as_texture} need the texture "
        f"(threshold {factor_max_error}/255, {jobs} worker(s))"
    )
    stats["tintedTextures"] = textures_written
    stats["tintedSourceAlbedos"] = len(by_base)
    stats["paintFactorMaxError"] = int(factor_max_error)
    return tinted, factors
