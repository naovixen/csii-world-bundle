"""Bake product `lods` — a generated LOD chain for every prefab mesh state.

The producer ships LOD 0 only (`lodCap` 0), and an engine drawing 1.2 million placed things
needs less than that beyond a hundred metres. For every `meshes/<assetId>.glb` (every state
file, skirts excluded), this writes `derived/lods/<assetId>_LOD1.glb` and `_LOD2.glb` at
`decimate.lod_ladder`'s ratios (25 % and 6 % of the triangles by default), each with the same
node names as the source so a consumer routes them by `glb.lod_level(node) → base name`, and
with each node's own transform ALREADY APPLIED to its vertices (`_flatten`) because the writer
emits identity nodes. The
last level also carries `COLOR_0` — each vertex's albedo sampled from the material's base
colour texture — so an engine can bind an untextured vertex-colour material to it (BeamNG's
`lod_vertcol` convention) and stop paying texture binds at distance.

A source file that already contains `_LOD<n>` nodes (a producer that ripped LODs) is skipped:
authored LODs beat generated ones. Options: `lod_targets` (default `[0.25, 0.06]`),
`lod_backend` (`auto`|`cluster`|`quadric`), `jobs` (worker processes; each file is independent).

## What each level COST, and which decimator ran

Beside the meshes this writes `lods.json`: per asset, each level's triangle count **and its
displacement** - the p95 distance from the generated surface back to LOD 0's, in metres
(`decimate.level_displacement`). A consumer cannot pick a switch point from a triangle ratio: at
the same 25 % ratio, 603 of the reference bundle's 4,261 LOD1s are geometrically IDENTICAL to
LOD 0 and the worst is 37 m out (p50 0.018 m, p99 6.167 m). A ladder built from the shape's size
cannot tell those apart, and the coarse end of that spread is what reads in-game as a roof moving
under props that have not moved (A6.7).

`lods.json` also records the **resolved** backend rather than the requested one. `auto` silently
becomes `cluster` wherever `pyfqmr` is not installed, and a build that reports `"auto"` cannot
tell you which of two algorithms shaped every LOD in the level.
"""

import json
import os
import time

import numpy

from .. import decimate, glb, glbwrite, parallel, png, reader
from .. import region as region_rule
from . import Product, register

PRODUCT_NAME = "lods"
INPUTS = ("meshes", "materials", "textures")
DEFAULT_TARGETS = (0.25, 0.06)

# The per-asset index written beside the level files: triangle counts, displacements and the
# resolved backend, keyed by asset id (the source `.glb`'s base name). It lives here rather than
# in `derived.json` because it is one row per prefab mesh state - 4,262 of them on the reference
# city - and the manifest is a staleness document, not a data file. The aggregate distribution
# does go into the manifest's product stats.
INDEX_NAME = "lods.json"


def load_index(derived_dir):
    """`{assetId: {"tris", "levels", "displacement", "backend"}}` for a baked derived cache.

    `{}` when the product has not been baked, so a caller can tell "no LODs" from "LODs with no
    measurements" - the second is a stale cache from before this was measured, and a consumer
    that needs the displacement must re-bake rather than guess it.
    """
    path = os.path.join(derived_dir, "lods", INDEX_NAME)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("assets") or {}


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
                except Exception:
                    cache[rel] = None
            return cache[rel]
        tint = entry.get("tint")
        if tint:
            return tuple(float(v) for v in tint[:3])
        return None

    return sampler


def _flatten(node):
    """The node's geometry with its own transform applied, and the transform dropped.

    **`glbwrite` writes every node with an IDENTITY transform** (it is the one shape of glTF the
    bake proves it round-trips), so a level written from node-LOCAL vertices is placed at the
    origin by any reader while LOD 0 is placed by the source file's node matrix. On the reference
    bundle 791 of 4,453 mesh-bearing nodes carry a translation over a centimetre and 166 carry a
    rotation — up to 75.9 m — so 670 shapes, 39,565 placements, TELEPORTED the moment they
    dropped a level. Read out of the shipped `.dae` files, `Mangr0be_Tree__elderly`'s LOD1 sat
    **9.0 m** above its LOD 0 and switched at 17.6 m from the camera (10,886 of them);
    `Amazon_Warehouse`'s sat 75.9 m across. **And the colmesh is cut from LOD 1**, so 628 shapes
    carried an invisible collider that far off the building you can see. That is the "still
    floating or misaligned" of A6.7, and it is a defect rather than a tuning question.

    Flattening here rather than teaching the writer about transforms keeps the derived file
    self-contained: every consumer reads it exactly as it reads a producer mesh, and gets the
    same world geometry whichever level it takes. The matrix is rigid on every bundle mesh
    (measured: no node carries a scale), so normals take the same rotation and stay unit.
    """
    if node.get("matrix") is None:
        return node
    matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
    if matrix.shape != (4, 4) or numpy.allclose(matrix, numpy.identity(4)):
        return node
    rotation = matrix[:3, :3]
    out = dict(node)
    out["verts"] = numpy.asarray(node["verts"], dtype=numpy.float64) @ rotation.T + matrix[:3, 3]
    if node.get("normals") is not None:
        normals = numpy.asarray(node["normals"], dtype=numpy.float64) @ rotation.T
        length = numpy.linalg.norm(normals, axis=1, keepdims=True)
        out["normals"] = normals / numpy.where(length > 1e-12, length, 1.0)
    out["matrix"] = numpy.identity(4)
    return out


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
    nodes = [_flatten(node) for node in nodes]
    per_level_nodes = [[] for _ in targets]
    tri_in = 0
    tri_out = [0] * len(targets)
    resolved_backend = backend
    # every node's samples pool into one distribution per level, so a file's displacement is the
    # percentile over its whole surface rather than the mean of its nodes' percentiles
    spread = [([], []) for _ in targets]
    for node in nodes:
        tri_in += len(node["tris"])
        try:
            levels, ladder_stats = decimate.lod_ladder(node, targets=targets, backend=backend)
        except Exception as exc:
            return asset_id, f"failed: {exc!r}", {}
        resolved_backend = ladder_stats.get("backend", backend)
        for li, level in enumerate(levels):
            if len(level["tris"]) == 0:
                continue
            tri_out[li] += len(level["tris"])
            _, distances, weights = decimate.level_displacement(level, node)
            if len(distances):
                spread[li][0].append(distances)
                spread[li][1].append(weights)
            prims = []
            cursor = 0
            colours = None
            if li == len(levels) - 1:
                colours = decimate.vertex_albedo(level, sampler)
            # one primitive per material run, but glbwrite wants per-primitive vertex arrays:
            # split the level back into runs (compact vertices per run)
            for name, count in level["material_runs"]:
                tris = level["tris"][cursor : cursor + count]
                cursor += count
                used, inverse = numpy.unique(tris.ravel(), return_inverse=True)
                prim = {
                    "verts": level["verts"][used],
                    "normals": None if level["normals"] is None else level["normals"][used],
                    "uvs": None if level["uvs"] is None else level["uvs"][used],
                    "tris": inverse.reshape(-1, 3),
                    "material": name,
                }
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
    displacement = []
    for distances, weights in spread:
        if not distances:
            displacement.append(None)
            continue
        displacement.append(
            round(
                decimate.weighted_percentile(
                    numpy.concatenate(distances),
                    numpy.concatenate(weights),
                    decimate.DISPLACEMENT_PERCENTILE,
                ),
                4,
            )
        )
    return (
        asset_id,
        "baked",
        {
            "tris": tri_in,
            "levels": tri_out,
            "files": written,
            "displacement": displacement,
            "backend": resolved_backend,
        },
    )


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "lods")
    os.makedirs(out_dir, exist_ok=True)
    targets = tuple(float(t) for t in (context.options.get("lod_targets") or DEFAULT_TARGETS))
    backend = context.options.get("lod_backend") or "auto"
    jobs = max(1, int(context.options.get("jobs") or 1))
    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_materials(bundle) or {}
    names = sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(lay.meshes)
        if f.endswith(".glb") and not f.endswith("__base.glb")
    )
    region = context.options.get("region")
    if region:
        # LODs are per prefab, not per place; a region bake still bakes every prefab the region
        # references. Cheap way to know which: scan instances inside the region.
        from .. import instances as _inst

        wanted = set()
        for rec in _inst.read_instances(bundle):
            p = rec.get("pos") or (0, 0, 0)
            if region_rule.contains(p[0], p[2], region):
                for cand in _inst.mesh_candidates(rec):
                    wanted.add(cand)
        names = [n for n in names if n in wanted]
    progress.begin("lods", total=len(names), unit="meshes")
    args = [(bundle, lay.meshes, out_dir, n, targets, backend, catalogue) for n in names]
    stats = {
        "meshes": len(names),
        "baked": 0,
        "authored": 0,
        "refused": 0,
        "empty": 0,
        "failed": 0,
        "trisIn": 0,
        "trisOut": [0] * len(targets),
        "backendRequested": backend,
        "backend": None,
    }
    index = {}
    started = time.monotonic()

    def account(result):
        asset_id, status, st = result
        key = status.split(":")[0]
        stats[key] = stats.get(key, 0) + 1
        if status == "baked":
            stats["trisIn"] += st["tris"]
            for i, t in enumerate(st["levels"]):
                stats["trisOut"][i] += t
            stats["backend"] = st.get("backend") or stats["backend"]
            index[asset_id] = {
                "tris": st["tris"],
                "levels": st["levels"],
                "displacement": st["displacement"],
            }
        elif status.startswith("failed"):
            progress.log(f"{asset_id}: {status}")

    # `parallel.each` advances and checks; `account` did the advancing before it existed.
    for result in parallel.each(_process_file, args, jobs, progress, chunksize=4):
        account(result)
    stats["seconds"] = round(time.monotonic() - started, 1)
    ratio = [round(t / stats["trisIn"], 3) if stats["trisIn"] else None for t in stats["trisOut"]]
    stats["backend"] = stats["backend"] or backend
    stats["displacement"] = _displacement_summary(index, len(targets))
    stats["displacementPercentile"] = decimate.DISPLACEMENT_PERCENTILE
    with open(os.path.join(out_dir, INDEX_NAME), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "backend": stats["backend"],
                "targets": list(targets),
                "percentile": decimate.DISPLACEMENT_PERCENTILE,
                "assets": index,
            },
            handle,
            separators=(",", ":"),
            sort_keys=True,
        )
    spread = " ".join(
        f"LOD{i + 1} p50 {d['p50']:.3f} m" for i, d in enumerate(stats["displacement"]) if d
    )
    progress.end(
        f"{stats['baked']} baked ({stats['authored']} authored, {stats['refused']} refused), "
        f"tri ratios {ratio}, backend {stats['backend']}, displacement {spread}"
    )
    stats["ratios"] = ratio
    return stats


def _displacement_summary(index, levels):
    """The distribution of every level's displacement over the whole bundle - what the switch
    points a consumer derives from it are actually made of."""
    out = []
    for li in range(levels):
        values = sorted(
            entry["displacement"][li]
            for entry in index.values()
            if entry.get("displacement") and entry["displacement"][li] is not None
        )
        if not values:
            out.append(None)
            continue

        def at(fraction, values=values):
            return round(values[min(len(values) - 1, int(fraction * len(values)))], 4)

        out.append(
            {
                "shapes": len(values),
                "p50": at(0.5),
                "p90": at(0.9),
                "p99": at(0.99),
                "max": round(values[-1], 4),
                "zero": sum(1 for v in values if v <= 1e-6),
            }
        )
    return out


# v3: each node's own transform is applied to the level's vertices before it is written - the
# writer emits identity nodes, so 670 shapes' LODs were placed at the origin and TELEPORTED up to
# 75.9 m the moment they switched (A6.7).
# v2: every level carries its measured displacement from LOD 0, and the index records the
# RESOLVED decimator - a consumer picks its switch points from those (A6.7).
register(
    Product(
        PRODUCT_NAME,
        INPUTS,
        "lods/",
        run,
        version=3,
        options=("lod_targets", "lod_backend", "region"),
    )
)
