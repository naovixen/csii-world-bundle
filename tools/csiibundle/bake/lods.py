"""Bake product `lods` — a generated LOD chain for every prefab mesh state.

The producer ships LOD 0 only (`lodCap` 0), and an engine drawing 1.2 million placed things
needs less than that beyond a hundred metres. For every `meshes/<assetId>.glb` (every state
file, skirts excluded), this writes `derived/lods/<assetId>_LOD1.glb` and `_LOD2.glb` at
`decimate.lod_ladder`'s ratios (25 % and 6 % of the triangles by default), each with the same
node names as the source so a consumer routes them by `glb.lod_level(node) → base name`. The
last level also carries `COLOR_0` — each vertex's albedo sampled from the material's base
colour texture — so an engine can bind an untextured vertex-colour material to it (BeamNG's
`lod_vertcol` convention) and stop paying texture binds at distance.

A source file that already contains `_LOD<n>` nodes (a producer that ripped LODs) is skipped:
authored LODs beat generated ones. Options: `lod_targets` (default `[0.25, 0.06]`),
`lod_backend` (`auto`|`cluster`|`quadric`), `jobs` (worker processes; each file is independent).
"""

import os
import time
from multiprocessing import Pool

import numpy

from .. import decimate, glb, glbwrite, png, reader
from . import Product, register

PRODUCT_NAME = "lods"
INPUTS = ("meshes", "materials", "textures")
DEFAULT_TARGETS = (0.25, 0.06)


def _albedo_sampler_factory(bundle_dir, catalogue):
    cache = {}

    def sampler(material_name):
        entry = catalogue.get(material_name) or {}
        rel = entry.get("baseColor")
        if rel:
            if rel not in cache:
                try:
                    img = png.rgba_array(os.path.join(bundle_dir, rel))
                    # a coarse copy is plenty for per-vertex sampling and keeps the cache small
                    step = max(1, img.shape[0] // 128)
                    cache[rel] = img[::step, ::step]
                except Exception:  # noqa: BLE001
                    cache[rel] = None
            return cache[rel]
        tint = entry.get("tint")
        if tint:
            return tuple(float(v) for v in tint[:3])
        return None

    return sampler


def _process_file(args):
    """Worker: one source glb → its LOD files. Returns (assetId, status, stats)."""
    bundle_dir, meshes_dir, out_dir, asset_id, targets, backend, catalogue = args
    src = os.path.join(meshes_dir, asset_id + ".glb")
    nodes = glb.parse_glb(src)
    if nodes is None:
        return asset_id, "refused", {}
    if not nodes:
        return asset_id, "empty", {}
    if any(glb.lod_level(n["name"])[1] > 0 for n in nodes):
        return asset_id, "authored", {}
    sampler = _albedo_sampler_factory(bundle_dir, catalogue)
    per_level_nodes = [[] for _ in targets]
    tri_in = 0
    tri_out = [0] * len(targets)
    for node in nodes:
        tri_in += len(node["tris"])
        try:
            levels, _stats = decimate.lod_ladder(node, targets=targets, backend=backend)
        except Exception as exc:  # noqa: BLE001
            return asset_id, f"failed: {exc!r}", {}
        for li, level in enumerate(levels):
            if len(level["tris"]) == 0:
                continue
            tri_out[li] += len(level["tris"])
            prims = []
            cursor = 0
            colours = None
            if li == len(levels) - 1:
                colours = decimate.vertex_albedo(level, sampler)
            # one primitive per material run, but glbwrite wants per-primitive vertex arrays:
            # split the level back into runs (compact vertices per run)
            for name, count in level["material_runs"]:
                tris = level["tris"][cursor:cursor + count]
                cursor += count
                used, inverse = numpy.unique(tris.ravel(), return_inverse=True)
                prim = {"verts": level["verts"][used], "normals": None if level["normals"] is None else level["normals"][used],
                        "uvs": None if level["uvs"] is None else level["uvs"][used],
                        "tris": inverse.reshape(-1, 3), "material": name}
                if colours is not None:
                    prim["colors"] = colours[used]
                prims.append(prim)
            per_level_nodes[li].append({"name": node["name"], "primitives": prims})
    written = []
    for li in range(len(targets)):
        if not per_level_nodes[li]:
            continue
        out = os.path.join(out_dir, f"{asset_id}_LOD{li + 1}.glb")
        glbwrite.write_glb(out, per_level_nodes[li])
        written.append(os.path.basename(out))
    return asset_id, "baked", {"tris": tri_in, "levels": tri_out, "files": written}


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "lods")
    os.makedirs(out_dir, exist_ok=True)
    targets = tuple(float(t) for t in (context.options.get("lod_targets") or DEFAULT_TARGETS))
    backend = context.options.get("lod_backend") or "auto"
    jobs = max(1, int(context.options.get("jobs") or 1))
    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_materials(bundle) or {}
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(lay.meshes)
                   if f.endswith(".glb") and not f.endswith("__base.glb"))
    region = context.options.get("region")
    if region:
        # LODs are per prefab, not per place; a region bake still bakes every prefab the region
        # references. Cheap way to know which: scan instances inside the disc.
        from .. import instances as _inst
        cx, cz, r = float(region["centerX"]), float(region["centerZ"]), float(region["radius"])
        wanted = set()
        for rec in _inst.read_instances(bundle):
            p = rec.get("pos") or (0, 0, 0)
            if (p[0] - cx) ** 2 + (p[2] - cz) ** 2 <= r * r:
                for cand in _inst.mesh_candidates(rec):
                    wanted.add(cand)
        names = [n for n in names if n in wanted]
    progress.begin("lods", total=len(names), unit="meshes")
    args = [(bundle, lay.meshes, out_dir, n, targets, backend, catalogue) for n in names]
    stats = {"meshes": len(names), "baked": 0, "authored": 0, "refused": 0, "empty": 0, "failed": 0,
             "trisIn": 0, "trisOut": [0] * len(targets), "backend": backend}
    started = time.monotonic()

    def account(result):
        asset_id, status, st = result
        key = status.split(":")[0]
        stats[key] = stats.get(key, 0) + 1
        if status == "baked":
            stats["trisIn"] += st["tris"]
            for i, t in enumerate(st["levels"]):
                stats["trisOut"][i] += t
        elif status.startswith("failed"):
            progress.log(f"{asset_id}: {status}")
        progress.advance()

    if jobs > 1 and len(args) > 4:
        with Pool(processes=jobs) as pool:
            for result in pool.imap_unordered(_process_file, args, chunksize=4):
                progress.check()
                account(result)
    else:
        for a in args:
            progress.check()
            account(_process_file(a))
    stats["seconds"] = round(time.monotonic() - started, 1)
    ratio = [round(t / stats["trisIn"], 3) if stats["trisIn"] else None for t in stats["trisOut"]]
    progress.end(f"{stats['baked']} baked ({stats['authored']} authored, {stats['refused']} refused), "
                 f"tri ratios {ratio}, backend {backend}")
    stats["ratios"] = ratio
    return stats


register(Product(PRODUCT_NAME, INPUTS, "lods/", run, options=("lod_targets", "lod_backend", "region")))
