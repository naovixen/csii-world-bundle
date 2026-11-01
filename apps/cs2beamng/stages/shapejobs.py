"""The shapes stage's work: one `ShapeJob` per mesh state and paint variant the city places, run
through the worker pool.

Each job carries the bake's own measurements for its mesh state (`derived/lods`): the per-level
displacement and which levels the MeshLab backend shaped.
"""

import os

from csiibundle import parallel, tints

from . import shapewrite


def shape_jobs(
    ctx, level, out_dir, demand, variants, tinted_by_variant, aliases, options, lod_index, ring_dir
):
    """Every `ShapeJob` the demand asks for, deduplicated by `(asset, suffix, variant)`."""
    jobs = {}
    for (asset, suffix), entry in sorted(demand.items()):
        common = {
            "bundle": ctx.bundle,
            "derived_dir": ctx.derived_dir,
            "out_dir": out_dir,
            "level_name": level.name,
            "asset": asset,
            "suffix": suffix,
            "kind": entry["kind"],
            "options": options,
            "aliases": aliases,
            "is_tree": entry["tree"],
            "displacement": _displacement(lod_index, asset, suffix),
            "chain_displacement": (lod_index.get(asset + suffix) or {}).get("chainDisplacement"),
            "lod_backend": _lod_backend(lod_index, asset, suffix),
            "ring_dir": ring_dir,
        }
        wanted = sorted(entry["variants"]) if entry["forest"] and entry["variants"] else []
        prefab_variants = (variants.get(asset) or {}).get("variants") or []
        for index in wanted:
            centre = prefab_variants[index] if index < len(prefab_variants) else None
            key = tints.variant_key(centre) if centre is not None else None
            tinted = (tinted_by_variant.get(key) or {}) if key else {}
            # a variant the bake wrote no tinted albedo for is the UNTINTED shape (`variant=None`),
            # not variant 0 - see `placement.shape_key`
            index_out = index if tinted else None
            jobs.setdefault(
                (asset, suffix, index_out),
                shapewrite.ShapeJob(
                    **common, variant=index_out, variant_key=key if tinted else None, tinted=tinted
                ),
            )
        # the UNTINTED shape: what a TSStatic binds (it keeps `colorPaletteMap` and carries its
        # own `instanceColor*`), and what a prefab with no baked variants gets
        if entry["static"] or not wanted:
            jobs.setdefault(
                (asset, suffix, None),
                shapewrite.ShapeJob(**common, variant=None, variant_key=None, tinted={}),
            )
    return list(jobs.values())


def write_shapes(ctx, jobs, progress):
    """`(results, errors)`: every job written by the worker pool, results keyed by shape."""
    progress.begin("shapes: writing .dae", total=len(jobs), unit="shapes")
    results, errors = {}, []
    workers = ctx.jobs or 1
    # Unordered: `_absorb` keys results by shape, so nothing depends on the order they arrive.
    for key, value in parallel.each(
        shapewrite.shape_job,
        jobs,
        workers,
        progress,
        chunksize=8,
        min_parallel=8,
        spawn=True,
        advance=False,
    ):
        _absorb(key, value, results, errors, progress)
    for error in errors[:20]:
        progress.log("shapes: " + error)
    return results, errors


def sweep_ring_dir(ring_dir, results):
    """Remove silhouette `.npz` files no shape of this build wrote (D1)."""
    if not os.path.isdir(ring_dir):
        return
    keep = {((v.get("lods") or {}).get("farRing") or {}).get("path") for v in results.values()}
    for name in os.listdir(ring_dir):
        if name not in keep:
            try:
                os.remove(os.path.join(ring_dir, name))
            except OSError:
                pass


def _lod_backend(lod_index, asset, suffix):
    """The generated levels (1-based) the MeshLab backend shaped for this mesh state - the
    only generated levels a BUILDING may take (`shapelods.lod_levels`, §8.15).

    PER LEVEL, from the index's `backends` list (one winner per level). This used to test
    `"meshlab" in str(backends)`, so one MeshLab level admitted BOTH levels - a cluster lattice
    on the other one - and a multi-node building whose railings went through `cluster` still
    read as MeshLab by triangle plurality (validation pass 2026-08-31, B4). `lod_audit` R4
    reads the same list the same way."""
    entry = lod_index.get(asset + suffix) or {}
    backends = entry.get("backends")
    if backends is None:  # older bakes: one `backend` string for every level
        backends = [entry.get("backend")] * 2
    if isinstance(backends, str):
        backends = [backends] * 2
    return tuple(i + 1 for i, b in enumerate(backends) if b == "meshlab")


def _displacement(lod_index, asset, suffix):
    """The bake's per-level displacement for one mesh state, in metres, or `None` per level when
    the bake did not generate that level (a mesh too small to decimate keeps only LOD 0)."""
    entry = lod_index.get(asset + suffix) or {}
    return entry.get("displacement")


def _absorb(key, value, results, errors, progress):
    progress.advance(1)
    if "error" in value:
        errors.append(value["error"])
    else:
        results[key] = value
    if (len(results) + len(errors)) % 200 == 0:
        progress.check()
