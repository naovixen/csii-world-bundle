"""Bake product `lods` — a generated LOD chain for every prefab mesh state.

The producer ships LOD 0 only (`lodCap` 0), and an engine drawing 1.2 million placed things
needs less than that beyond a hundred metres. For every `meshes/<assetId>.glb` (every state
file, skirts excluded), this writes `derived/lods/<assetId>_LOD1.glb` and `_LOD2.glb` at
`decimate.lod_ladder`'s ratios (25 % and 6 % of the triangles by default), each with the same
node names as the source so a consumer routes them by `glb.lod_level(node) → base name`, and
with each node's own transform ALREADY APPLIED to its vertices (`lodworker._flatten`) because the writer
emits identity nodes. The
last level also carries `COLOR_0` — each vertex's albedo sampled from the material's base
colour texture — so an engine can bind an untextured vertex-colour material to it (BeamNG's
`lod_vertcol` convention) and stop paying texture binds at distance.

A source file that already contains `_LOD<n>` nodes (a producer that ripped LODs) is skipped:
authored LODs beat generated ones. Options: `lod_targets` (default `[0.25, 0.06]`),
`lod_backend` (`auto`|`cluster`|`quadric`|`meshlab`), `jobs` (worker processes; each file is
independent). `auto` also contests the texture-aware MeshLab collapse on meshes of 20,000
triangles and up when `pymeshlab` is installed (`decimatemeshlab`, the no-chain landmarks).

## What each level COST, and which decimator ran

Beside the meshes this writes `lods.json`: per asset, each level's triangle count **and its
displacement** - the p95 distance from the generated surface back to LOD 0's, in metres
(`displacement.level_displacement`). A consumer cannot pick a switch point from a triangle ratio: at
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

from .. import displacement, parallel, reader
from .. import region as region_rule
from . import Product, lodchoice, lodworker, register
from .lodrefine import REFINE_MAX_ERROR, REFINE_MAX_PEAK, REFINE_MIN_TRIS_OUT
from .lodreuse import REUSE_RULE_VERSION, refine_key, reusable

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


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "lods")
    os.makedirs(out_dir, exist_ok=True)
    targets = tuple(float(t) for t in (context.options.get("lod_targets") or DEFAULT_TARGETS))
    backend = context.options.get("lod_backend") or "auto"
    refine = _refine_options(context.options)
    jobs = max(1, int(context.options.get("jobs") or 1))
    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_materials(bundle) or {}
    names = _mesh_names(bundle, lay.meshes, context.options.get("region"))
    previous = {} if getattr(context, "force", False) else load_index(context.derived)
    reused = {
        name: previous[name]
        for name in names
        if reusable(previous.get(name), out_dir, targets, refine)
    }
    todo = [n for n in names if n not in reused]
    progress.begin("lods", total=len(todo), unit="meshes")
    args = [(bundle, lay.meshes, out_dir, n, targets, backend, refine) for n in todo]
    stats = _new_stats(len(names), targets, backend, refine)
    index = dict(reused)
    stats["reused"] = len(reused)
    for entry in reused.values():
        _absorb_reused(stats, entry)
    started = time.monotonic()

    # `parallel.each` advances and checks; `_account` did the advancing before it existed.
    for result in parallel.each(
        lodworker.process_file,
        args,
        jobs,
        progress,
        chunksize=4,
        initializer=lodworker.init_worker,
        initargs=(catalogue,),
    ):
        _account(stats, index, result, targets, refine, progress)
    stats["seconds"] = round(time.monotonic() - started, 1)
    ratio = [round(t / stats["trisIn"], 3) if stats["trisIn"] else None for t in stats["trisOut"]]
    stats["displacement"] = _displacement_summary(index, len(targets))
    stats["displacementPercentile"] = displacement.DISPLACEMENT_PERCENTILE
    _write_index(out_dir, stats["backend"], targets, index)
    progress.end(_end_message(stats, ratio))
    stats["ratios"] = ratio
    return stats


def _refine_options(options):
    """The refined-shipped-level settings the worker reads, from the product's options."""
    refine_error = options.get("lod0_refine_error")
    refine_peak = options.get("lod0_refine_peak")
    return {
        "enabled": bool(options.get("lod0_refine", True)),
        "maxError": REFINE_MAX_ERROR if refine_error is None else float(refine_error),
        "maxPeak": REFINE_MAX_PEAK if refine_peak is None else float(refine_peak),
        "minTrisOut": REFINE_MIN_TRIS_OUT,
    }


def _mesh_names(bundle, meshes_dir, region):
    """Every mesh state file's asset id (skirts excluded), sorted, cut to a region's prefabs."""
    names = sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(meshes_dir)
        if f.endswith(".glb") and not f.endswith("__base.glb")
    )
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
    return names


def _new_stats(meshes, targets, backend, refine):
    """The product stats before any asset is counted."""
    return {
        "meshes": meshes,
        "identicalLevels": 0,  # generated levels left unwritten as copies of the one above
        "baked": 0,
        "authored": 0,
        "refused": 0,
        "empty": 0,
        "failed": 0,
        "trisIn": 0,
        "trisOut": [0] * len(targets),
        "backendRequested": backend,
        "backend": (
            "best of cluster and quadric per level, by two-sided displacement"
            if backend == "auto" and lodchoice.quadric_available()
            else ("cluster" if backend == "auto" else backend)
        ),
        "backendTris": {},
        "reused": 0,  # assets whose previous bake stood (see `lodreuse.reusable`)
        # the refined shipped level: how many shapes took one and what it removed
        "refine": {
            "enabled": refine["enabled"],
            "maxError": refine["maxError"],
            "levels": 0,
            "trisIn": 0,
            "trisOut": 0,
            "worstDisplacement": 0.0,
        },
    }


def _absorb_refine(stats, per_level):
    agg = stats["refine"]
    for _level, got in sorted((per_level or {}).items()):
        agg["levels"] += 1
        agg["trisIn"] += got["trisIn"]
        agg["trisOut"] += got["tris"]
        agg["worstDisplacement"] = max(agg["worstDisplacement"], got["displacement"])


def _absorb_reused(stats, entry):
    """Count one asset whose previous bake stood."""
    _absorb_refine(stats, entry.get("refine"))
    if entry.get("authored"):
        stats["authored"] += 1
    else:
        stats["baked"] += 1
        stats["trisIn"] += entry.get("tris") or 0
        for i, t in enumerate(entry.get("levels") or []):
            if i < len(stats["trisOut"]):
                stats["trisOut"][i] += t
        stats["identicalLevels"] += len(entry.get("identical") or ())


def _account(stats, index, result, targets, refine, progress):
    """Count one worker result and record its index entry."""
    asset_id, status, st = result
    key = status.split(":")[0]
    stats[key] = stats.get(key, 0) + 1
    if status == "authored":
        # An authored shape generates no ladder, but it still needs an index entry so the
        # next bake reuses it rather than re-parsing every landmark in the city to conclude
        # the same thing.
        index[asset_id] = _authored_entry(st, targets, refine)
        _absorb_refine(stats, st.get("refine"))
        return
    if status == "baked":
        stats["identicalLevels"] += len(st.get("identical") or ())
        stats["trisIn"] += st["tris"]
        for i, t in enumerate(st["levels"]):
            stats["trisOut"][i] += t
        for tris_by_backend in st.get("backendTris") or []:
            for name, count in tris_by_backend.items():
                stats["backendTris"][name] = stats["backendTris"].get(name, 0) + count
        index[asset_id] = _baked_entry(st, refine)
        _absorb_refine(stats, st.get("refine"))
    elif status.startswith("failed"):
        progress.log(f"{asset_id}: {status}")


def _authored_entry(st, targets, refine):
    """The index entry of an authored shape: its refined levels and its chain's displacement."""
    return {
        "authored": True,
        "files": st.get("files") or [],
        "targets": list(targets),
        "reuseV": REUSE_RULE_VERSION,
        "refineKey": refine_key(refine),
        "refine": st.get("refine"),
        # how far the game's own LOD 1 is from LOD 0 - the consumer's cap guard
        "chainDisplacement": st.get("chainDisplacement"),
    }


def _baked_entry(st, refine):
    """The index entry of a generated ladder."""
    return {
        "tris": st["tris"],
        "levels": st["levels"],
        "displacement": st["displacement"],
        "backends": st.get("backends"),
        "identical": st.get("identical") or [],
        # what `reusable` reads on the next bake
        "files": st.get("files") or [],
        "targets": st.get("targets"),
        "reuseV": st.get("reuseV"),
        "refineKey": refine_key(refine),
        # persisted so a reused entry still reports what its refinement removed
        "refine": st.get("refine"),
    }


def _write_index(out_dir, backend, targets, index):
    """`lods.json`: the resolved backend, the targets, the percentile and every asset's entry."""
    with open(os.path.join(out_dir, INDEX_NAME), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "backend": backend,
                "targets": list(targets),
                "percentile": displacement.DISPLACEMENT_PERCENTILE,
                "assets": index,
            },
            handle,
            separators=(",", ":"),
            sort_keys=True,
        )


def _end_message(stats, ratio):
    """The one line the product ends its progress with."""
    spread = " ".join(
        f"LOD{i + 1} p50 {d['p50']:.3f} m" for i, d in enumerate(stats["displacement"]) if d
    )
    reuse_note = f", {stats['reused']:,} reused" if stats["reused"] else ""
    agg = stats["refine"]
    if agg["levels"]:
        pct = 100.0 * (1.0 - agg["trisOut"] / agg["trisIn"]) if agg["trisIn"] else 0.0
        reuse_note += (
            f", refined {agg['levels']:,} shipped levels "
            f"({agg['trisIn']:,} -> {agg['trisOut']:,} tris, {pct:.1f} % out, "
            f"worst p95 {agg['worstDisplacement']:.3f} m)"
        )
    return (
        f"{stats['baked']} baked ({stats['authored']} authored, {stats['refused']} refused)"
        f"{reuse_note}, "
        f"tri ratios {ratio}, backend {stats['backend']}, displacement {spread}"
    )


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


# NOT A VERSION BUMP: the product is per-asset incremental (`lodreuse.reusable`), and reuse produces the
# same bytes it would have recomputed - a version means "the outputs changed shape", and these do
# not. Bumping it would have forced the very full re-decimation of all 4,356 meshes the reuse
# exists to avoid. The first genuine re-bake after this writes `reuseV` into every entry; every
# bake after that carries the unchanged assets forward.
# v3: each node's own transform is applied to the level's vertices before it is written - the
# writer emits identity nodes, so 670 shapes' LODs were placed at the origin and TELEPORTED up to
# 75.9 m the moment they switched (A6.7).
# v2: every level carries its measured displacement from LOD 0, and the index records the
# RESOLVED decimator - a consumer picks its switch points from those (A6.7).
# v7: meshes of 20,000 triangles and up take the MeshLab backend outright - the p95 contest cannot
# see a cluster lattice, and Five_Luxe_JBR_ lost to one by 12 cm
# v6: LOD slots filled with byte-copies of LOD 0 (Kinsman_Plaza and its kin) no longer count as an
# authored chain - real levels are generated for them (the consumer already drops the copies)
# v5: `auto` contests the texture-aware MeshLab collapse (`decimatemeshlab`) on meshes of 20,000
# triangles and up - the no-chain landmark buildings' levels, judged like every candidate by
# measured displacement; the index's backend then names the winner per level
# v4: `glb.parse_glb` no longer refuses a file for its POSITION-only impostor level, so every
# tree shape the v3 index silently skipped (165 on the reference city) now gets its levels.
register(
    Product(
        PRODUCT_NAME,
        INPUTS,
        "lods/",
        run,
        version=7,
        options=(
            "lod_targets",
            "lod_backend",
            "region",
            "lod0_refine",
            "lod0_refine_error",
            "lod0_refine_peak",
        ),
    )
)
