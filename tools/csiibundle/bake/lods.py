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
from collections import Counter

import numpy

from .. import decimate, decimatemeshlab, displacement, glb, glbwrite, parallel, png, reader
from .. import region as region_rule
from . import Product, lodchoice, register
from .lodchoice import best_levels

PRODUCT_NAME = "lods"
INPUTS = ("meshes", "materials", "textures")
DEFAULT_TARGETS = (0.25, 0.06)

# The per-asset index written beside the level files: triangle counts, displacements and the
# resolved backend, keyed by asset id (the source `.glb`'s base name). It lives here rather than
# in `derived.json` because it is one row per prefab mesh state - 4,262 of them on the reference
# city - and the manifest is a staleness document, not a data file. The aggregate distribution
# does go into the manifest's product stats.
INDEX_NAME = "lods.json"

# -- the refined shipped level (`_LOD<n>R.glb`) ----------------------------------------------------
#
# LOD 0 is ~66 % of a shape's bytes, a shape is mounted WHOLE for one instance in view, and the
# level's 2,396 MiB of resident mesh is what starves the texture streamer on a card already at
# 88-96 % of its driver budget ("the FPS is MEMORY", phase10 §8.14). So: thin the level each shape
# actually ships, using the MeshLab collapse §8.15 built - every collapse lands on an EXISTING
# vertex, so the simplified surface lies on the original by construction - and keep a reduction
# only where the measured p95 displacement says the surface did not move.
#
# HOW DEEP, MEASURED 2026-09-01. The first cut asked for 50 % and 25 % and concluded there was no
# slack at all, because 50 % fails on almost everything. The curve says otherwise:
#
#     shape                      in        90%           80%           70%           50%
#     InternationalAirport01  57,163  51,609/0.000  46,055/0.000  40,503/0.076  29,449/1.191
#     MedicalUniversity01    126,896 115,590/0.000 104,286/0.000  92,982/0.230  80,460/0.440
#     CargoHarbor01           20,506  18,464/0.000  16,424/0.005  14,382/0.051  10,972/0.220
#     ParkingHall01           54,270  48,998/0.000  43,726/0.163  38,454/0.219  27,910/0.341
#     OfficeHighSignature02   97,689  89,191/0.005  80,695/0.078  79,259/0.330  79,259/0.330
#
# A tenth of the triangles comes off for NOTHING MEASURABLE. Over the heaviest 60 shapes this rule
# reached 58 of them for 18.4 % of their triangles at a 3 cm bound - ~219 MiB extrapolated over the
# bundle's 12.5 M shipped triangles.
#
# TWO THINGS THE CURVE DISPROVES, both of which the first cut assumed:
#   * the error is NOT monotone in the reduction (MedicalUniversity01 is 5.317 m at 60 % and
#     0.440 m at 50 %), so a walk may NOT stop at the first failure - every candidate is judged on
#     its own, deepest first, and the first one that PASSES wins;
#   * MeshLab stalls (OfficeHighSignature02 sits at 79,259 for 70/60/50 %), so a candidate that
#     came back no smaller is not a reduction and is skipped.
REFINE_LEVELS = (0, 1)
# Deepest first: the first candidate that passes the bound wins, so a shape with real slack costs
# ONE MeshLab pass and only a stubborn one costs three. Nothing below 0.7 - the curve says that is
# where the surface starts moving in metres, and the game's own LOD is the better answer there.
# NOTHING BELOW 0.7 ON A BUILDING, and the reason is in `decimatemeshlab`'s own declared limit:
# "a perforated facade (rows of open balconies) still webs shut at these ratios". The 0.5/0.6
# rungs were added to reach a deeper reduction and they reached the artifact instead - Naomi's
# screenshots, 2026-09-01: window openings torn open so the sky shows through a tower, facades
# faceted and rippling, curtain-wall mullion grids shredded into triangular tents. `lod_ladder`
# decimates PER MATERIAL RUN, so a wall run and a glass run stop sharing vertices and the seam
# between them opens; the deeper the ratio, the wider it opens. 0.7/0.8/0.9 at 3 cm is the
# envelope that shipped and was looked at without a complaint.
REFINE_TARGETS = (0.7, 0.8, 0.9)
# Metres of measured p95 surface error allowed - the BULK bound.
REFINE_MAX_ERROR = 0.03

# ...and metres of WORST-CASE displacement allowed, which is the bound that actually protects a
# building. MEASURED 2026-09-01, after a build at p95 8 cm "ruined some buildings and created
# triangle artifacts" (Naomi):
#
#     Alvear_Tower_Buenos_Aires  at 0.7:  p95 0.022 m   p99 0.553 m   MAX 1.000 m
#     Prison01                   at 0.7:  p95 0.508 m   p99 0.846 m   MAX 3.415 m
#     MedicalUniversity01        at 0.5:  p95 0.440 m   p99 2.072 m   MAX 5.841 m
#
# A 95th percentile leaves the worst 5 % of the surface COMPLETELY UNBOUNDED. Alvear_Tower passes
# an 8 cm p95 gate with room to spare while a metre of its geometry has moved - and a metre of
# moved geometry on a facade is the artifact. The tolerance was never the defect; the missing
# bound was. A candidate must now pass BOTH, so a shape whose collapse is clean keeps its deep
# reduction and only a shape with a tail is refused or dropped to a shallower ratio.
#
# The cap costs about half the reduction on the heaviest 40 shapes (29.9 % out -> 16.6 %) and is
# worth every triangle of it.
REFINE_MAX_PEAK = 0.25
# Only a level at or over this many triangles is a candidate. 500 rather than 2,000: the tail is
# 3,026 shapes holding 137 MiB, which a 2,000 floor left entirely untouched.
REFINE_MIN_TRIS = 2_000
# The count that matters is the one that SHIPS (`stages.shapelods.LOD0_MIN_TRIANGLES`).
REFINE_MIN_TRIS_OUT = 1_000


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


def _corner_digest(pieces):
    """Order-independent geometry digest: the sorted, rounded corner positions of every triangle
    in `pieces` (each a dict with `verts` and `tris`). Two levels built from the same source in
    a different triangle or material order digest the same; anything else differs."""
    corners = []
    for piece in pieces:
        verts = numpy.asarray(piece["verts"], dtype=numpy.float64)
        tris = numpy.asarray(piece["tris"], dtype=numpy.int64)
        if len(tris):
            corners.append(verts[tris].reshape(-1, 9))
    if not corners:
        return b""
    stack = numpy.concatenate(corners).round(4)
    return stack[numpy.lexsort(stack.T[::-1])].tobytes()


def _nodes_digest(level_nodes):
    """`_corner_digest` over a written level's `{"name", "primitives": [...]}` nodes."""
    return _corner_digest([prim for node in level_nodes for prim in node["primitives"]])


def _authored_is_copy(nodes, lod_nodes):
    """True when every authored level's geometry is a copy of level 0's (order-independent:
    the sorted corner positions per level; the consumer's identical-level rule, §8.13)."""
    digest = _corner_digest
    base = digest([n for n in nodes if glb.lod_level(n["name"])[1] == 0])
    levels = {}
    for node in lod_nodes:
        levels.setdefault(glb.lod_level(node["name"])[1], []).append(node)
    return all(digest(group) == base for group in levels.values())


_WORKER_CATALOGUE = {}


def _init_worker(catalogue):
    """Pool initializer: the materials catalogue every mesh's albedo sampler reads - it used
    to ride every argument tuple, pickled once per chunk (validation pass 2026-08-31, D5)."""
    global _WORKER_CATALOGUE
    _WORKER_CATALOGUE = catalogue


def _refine_one(node, targets, max_error, min_tris_out, max_peak=REFINE_MAX_PEAK):
    """The deepest refined copy of `node` whose measured p95 surface error passes `max_error`.

    `(level, p95, ratio)` or `(None, None, None)`. Every candidate is judged ON ITS OWN and the
    first that passes wins: the error is NOT monotone in the reduction (MedicalUniversity01 is
    5.317 m at 60 % and 0.440 m at 50 %), so a walk that stopped at the first failure would throw
    away levels that pass further down. `targets` is deepest-first, so a shape with real slack
    costs one MeshLab pass.
    """
    if len(node["tris"]) < REFINE_MIN_TRIS:
        return None, None, None
    # MeshLab or nothing: this is the level the viewer stands in front of, and the other two
    # decimators are exactly what banned generated levels from buildings (§8.12, the see-through
    # facade lattice). Where it is not installed a shape simply keeps its mesh.
    if not decimatemeshlab.available():
        return None, None, None
    for ratio in targets:
        level = decimate.lod_ladder(node, targets=(ratio,), backend="meshlab")[0][0]
        tris = len(level["tris"])
        # MeshLab stalls (OfficeHighSignature02 sits at 79,259 for three targets running): a
        # candidate that came back no smaller is not a reduction.
        if tris == 0 or tris < min_tris_out or tris >= len(node["tris"]):
            continue
        p95, distances, _weights = displacement.level_displacement(level, node)
        peak = float(distances.max()) if len(distances) else 0.0
        if p95 <= max_error and (max_peak <= 0.0 or peak <= max_peak):
            out = dict(level)
            out["backend"] = "meshlab"
            return out, (round(float(p95), 4), round(peak, 4)), ratio
    return None, None, None


def _level_prims(level, node_name):
    """One written node's primitives from a decimated level: the material runs split back out,
    vertices compacted per run (what `glbwrite` wants). No `COLOR_0` - the vertex-albedo stream
    belongs to the far level, and a near level binds its own textured materials (C4)."""
    prims = []
    cursor = 0
    for name, count in level["material_runs"]:
        tris = level["tris"][cursor : cursor + count]
        cursor += count
        if len(tris) == 0:
            continue
        used, inverse = numpy.unique(tris.ravel(), return_inverse=True)
        prims.append(
            {
                "verts": level["verts"][used],
                "normals": None if level["normals"] is None else level["normals"][used],
                "uvs": None if level["uvs"] is None else level["uvs"][used],
                "tris": inverse.reshape(-1, 3),
                "material": name,
            }
        )
    return {"name": node_name, "primitives": prims}


def _refine_level(nodes, refine):
    """One LEVEL of an asset, refined, or None when it earned nothing.

    A level is swapped WHOLE (the consumer binds one file per detail), so every node comes along:
    the heavy ones refined, the small ones copied through. When no node was refined the file would
    be a byte-copy of the level - bytes on disk for nothing - so nothing is written.
    """
    if not refine.get("enabled"):
        return None
    max_error = float(refine.get("maxError") or 0.0)
    if max_error <= 0.0:
        return None
    min_out = int(refine.get("minTrisOut") or REFINE_MIN_TRIS_OUT)
    max_peak = float(refine.get("maxPeak", REFINE_MAX_PEAK) or 0.0)
    out_nodes, tris_in, tris_out, touched = [], 0, 0, 0
    worst = worst_peak = 0.0
    for node in nodes:
        tris_in += len(node["tris"])
        level, measured, _ratio = _refine_one(
            node, REFINE_TARGETS, max_error, min_out, max_peak=max_peak
        )
        if level is None:
            out_nodes.append(
                _level_prims(
                    {
                        "verts": numpy.asarray(node["verts"]),
                        "normals": None
                        if node.get("normals") is None
                        else numpy.asarray(node["normals"]),
                        "uvs": None if node.get("uvs") is None else numpy.asarray(node["uvs"]),
                        "tris": numpy.asarray(node["tris"]),
                        "material_runs": node["material_runs"],
                    },
                    node["name"],
                )
            )
            tris_out += len(node["tris"])
            continue
        out_nodes.append(_level_prims(level, node["name"]))
        tris_out += len(level["tris"])
        p95, peak = measured
        worst = max(worst, float(p95 or 0.0))
        worst_peak = max(worst_peak, float(peak or 0.0))
        touched += 1
    if not touched:
        return None
    return {
        "nodes": out_nodes,
        "tris": tris_out,
        "trisIn": tris_in,
        "displacement": round(worst, 4),
        "peak": round(worst_peak, 4),
    }


def _chain_displacement(by_level):
    """How far the game's own LOD 1 sits from LOD 0, in metres (p95), or None.

    THE NUMBER `authored_lod0_max_tris` NEVER ASKED FOR. That cap drops LOD 0 on a triangle COUNT,
    so it cannot tell a LOD 1 that is indistinguishable from one that is a different building.
    Measured over the reference city's 644 capped shapes the split is stark: p50 0.005 m, p75
    0.025 m, p90 0.072 m - and then `OracleParkSculpture` at 1.088 m (5,974 -> 784 triangles),
    `Planetario_Galileo_Galilei` 0.892 m, `Alvear_Tower_Buenos_Aires` 0.423 m, `Prison01` 0.316 m.
    Those are the shapes that "look awful up close" (Naomi, 2026-09-01), and the existing ratio
    guard (`DEGENERATE_LOD1_RATIO`) misses every one of them: they are moderately reduced and
    geometrically far, where the ratio only sees badly reduced.
    """
    a = by_level.get(0) or []
    b = by_level.get(1) or []
    if not a or not b:
        return None
    base = max(a, key=lambda n: len(n["tris"]))
    lod1 = max(b, key=lambda n: len(n["tris"]))
    if not len(base["tris"]) or not len(lod1["tris"]):
        return None
    try:
        return round(float(displacement.level_displacement(lod1, base)[0]), 4)
    except Exception:
        return None


# The reuse rule's own version: bump it when a change would make a PREVIOUS bake's per-asset
# output wrong, so `_reusable` stops standing on it. The product version re-bakes everything;
# this re-bakes nothing that has not changed.
REUSE_RULE_VERSION = 2


def _refine_key(refine):
    """The refinement inputs an output depends on - a change to any of them invalidates a refined
    level, and only a refined level."""
    if not refine.get("enabled"):
        return None
    return [
        float(refine["maxError"]),
        float(refine.get("maxPeak") or 0.0),
        list(REFINE_TARGETS),
        REFINE_MIN_TRIS,
    ]


def _reusable(entry, out_dir, targets, refine):
    """Whether this asset's previous bake can stand as-is, so the worker is not run for it.

    `lods` is the bake's expensive product and staleness is per PRODUCT, so ANY version bump here
    re-decimates every LOD 1 and LOD 2 in the bundle — 4,356 meshes, the step that makes a bake an
    afternoon rather than a coffee. An entry stands when it was baked at the same targets, under
    the same reuse rule, and the files it named are still on disk; anything else is re-run.
    """
    if not entry or entry.get("targets") != list(targets):
        return False
    if entry.get("reuseV") != REUSE_RULE_VERSION:
        return False
    if entry.get("refineKey") != _refine_key(refine):
        return False
    for name in entry.get("files") or ():
        if not os.path.isfile(os.path.join(out_dir, name)):
            return False
    return True


def _process_file(args):
    """Worker: one source glb → its LOD files. Returns (assetId, status, stats)."""
    bundle_dir, meshes_dir, out_dir, asset_id, targets, backend, refine = args
    catalogue = _WORKER_CATALOGUE
    src = os.path.join(meshes_dir, asset_id + ".glb")
    nodes = glb.parse_glb(src)
    if nodes is None:
        return asset_id, "refused", {}
    if not nodes:
        return asset_id, "empty", {}
    lod_nodes = [n for n in nodes if glb.lod_level(n["name"])[1] > 0]
    authored = bool(lod_nodes) and not _authored_is_copy(nodes, lod_nodes)

    # THE REFINEMENT RUNS FIRST, and it runs for an authored shape too. A generated LADDER is
    # refused for a shape whose artist shipped one (their levels have no holes and the game's rule
    # is the authority) - but a refined copy of a level the ARTIST authored is not a generated
    # level: it is that same level with triangles removed where the surface provably did not move.
    # The authored shapes are where the bytes are (196 of the 221 shapes over 20,000 triangles).
    by_level = {}
    for node in nodes:
        by_level.setdefault(glb.lod_level(node["name"])[1], []).append(_flatten(node))
    refined, refine_written = {}, []
    for level in REFINE_LEVELS:
        group = by_level.get(level)
        if not group:
            continue
        got = _refine_level(group, refine)
        if got is None:
            continue
        out = os.path.join(out_dir, f"{asset_id}_LOD{level}R.glb")
        glbwrite.write_glb(out, got["nodes"])
        refine_written.append(os.path.basename(out))
        refined[str(level)] = {
            "tris": got["tris"],
            "trisIn": got["trisIn"],
            "displacement": got["displacement"],
            "peak": got.get("peak"),
        }
    if authored:
        return (
            asset_id,
            "authored",
            {
                "refine": refined or None,
                "files": refine_written,
                # what the consumer's cap guard reads (`_chain_displacement`)
                "chainDisplacement": _chain_displacement(by_level),
            },
        )
    # Only LOD 0 generates a ladder: where every authored level is a byte-copy of LOD 0
    # (Kinsman_Plaza and its kin - the workshop filled the LOD slots with the full mesh) real
    # levels are generated instead, and `by_level[0]` is already the flattened level-0 nodes.
    sampler = _albedo_sampler_factory(bundle_dir, catalogue)
    nodes = by_level.get(0) or []
    per_level_nodes = [[] for _ in targets]
    tri_in = 0
    tri_out = [0] * len(targets)
    backends = [Counter() for _ in targets]
    # every node's samples pool into one distribution per level, so a file's displacement is the
    # percentile over its whole surface rather than the mean of its nodes' percentiles
    spread = [([], []) for _ in targets]
    for node in nodes:
        tri_in += len(node["tris"])
        try:
            levels, measured = best_levels(node, targets, backend)
        except Exception as exc:
            return asset_id, f"failed: {exc!r}", {}
        for li, level in enumerate(levels):
            if len(level["tris"]) == 0:
                continue
            tri_out[li] += len(level["tris"])
            backends[li][level["backend"]] += len(level["tris"])
            distances, weights = measured[li]
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
    identical = []
    previous = None
    for li in range(len(targets)):
        if not per_level_nodes[li]:
            continue
        # A level whose geometry is the level above it is not a LOD: MeshLab stalls at the same
        # count for two targets on a hard facade (LuxuryTowerResidence: 61,594 twice) and the
        # cluster backend can too. 182 of the reference bake's 2,625 LOD 2 files were byte-copies
        # of their LOD 1 (18.5 MiB). The switch to it would draw the same pixels for the bytes,
        # so it is left unwritten and counted; the consumer's identical-level rule catches the
        # same case in a bake made before this.
        digest = _nodes_digest(per_level_nodes[li])
        if previous is not None and digest == previous:
            identical.append(li + 1)
            continue
        previous = digest
        out = os.path.join(out_dir, f"{asset_id}_LOD{li + 1}.glb")
        glbwrite.write_glb(out, per_level_nodes[li])
        written.append(os.path.basename(out))
    written.extend(refine_written)
    displacements = []
    for distances, weights in spread:
        if not distances:
            displacements.append(None)
            continue
        displacements.append(
            round(
                displacement.weighted_percentile(
                    numpy.concatenate(distances),
                    numpy.concatenate(weights),
                    displacement.DISPLACEMENT_PERCENTILE,
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
            "identical": identical,
            "targets": list(targets),
            "reuseV": REUSE_RULE_VERSION,
            "refine": refined or None,
            "displacement": displacements,
            # the backend that shaped most of each level's triangles
            "backends": [c.most_common(1)[0][0] if c else None for c in backends],
            "backendTris": [dict(c) for c in backends],
        },
    )


def run(context, progress):
    bundle = context.bundle_dir
    out_dir = os.path.join(context.derived, "lods")
    os.makedirs(out_dir, exist_ok=True)
    targets = tuple(float(t) for t in (context.options.get("lod_targets") or DEFAULT_TARGETS))
    backend = context.options.get("lod_backend") or "auto"
    refine_error = context.options.get("lod0_refine_error")
    refine_peak = context.options.get("lod0_refine_peak")
    refine = {
        "enabled": bool(context.options.get("lod0_refine", True)),
        "maxError": REFINE_MAX_ERROR if refine_error is None else float(refine_error),
        "maxPeak": REFINE_MAX_PEAK if refine_peak is None else float(refine_peak),
        "minTrisOut": REFINE_MIN_TRIS_OUT,
    }
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
    previous = {} if getattr(context, "force", False) else load_index(context.derived)
    reused = {
        name: previous[name]
        for name in names
        if _reusable(previous.get(name), out_dir, targets, refine)
    }
    todo = [n for n in names if n not in reused]
    progress.begin("lods", total=len(todo), unit="meshes")
    args = [(bundle, lay.meshes, out_dir, n, targets, backend, refine) for n in todo]
    stats = {
        "meshes": len(names),
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
        "reused": 0,  # assets whose previous bake stood (see `_reusable`)
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
    index = dict(reused)
    stats["reused"] = len(reused)
    for entry in reused.values():
        _absorb_refine(entry.get("refine"))
        if entry.get("authored"):
            stats["authored"] += 1
        else:
            stats["baked"] += 1
            stats["trisIn"] += entry.get("tris") or 0
            for i, t in enumerate(entry.get("levels") or []):
                if i < len(stats["trisOut"]):
                    stats["trisOut"][i] += t
            stats["identicalLevels"] += len(entry.get("identical") or ())
    started = time.monotonic()

    def _absorb_refine(per_level):
        agg = stats["refine"]
        for _level, got in sorted((per_level or {}).items()):
            agg["levels"] += 1
            agg["trisIn"] += got["trisIn"]
            agg["trisOut"] += got["tris"]
            agg["worstDisplacement"] = max(agg["worstDisplacement"], got["displacement"])

    def account(result):
        asset_id, status, st = result
        key = status.split(":")[0]
        stats[key] = stats.get(key, 0) + 1
        if status == "authored":
            # An authored shape generates no ladder, but it still needs an index entry so the
            # next bake reuses it rather than re-parsing every landmark in the city to conclude
            # the same thing.
            index[asset_id] = {
                "authored": True,
                "files": st.get("files") or [],
                "targets": list(targets),
                "reuseV": REUSE_RULE_VERSION,
                "refineKey": _refine_key(refine),
                "refine": st.get("refine"),
                # how far the game's own LOD 1 is from LOD 0 - the consumer's cap guard
                "chainDisplacement": st.get("chainDisplacement"),
            }
            _absorb_refine(st.get("refine"))
            return
        if status == "baked":
            stats["identicalLevels"] += len(st.get("identical") or ())
            stats["trisIn"] += st["tris"]
            for i, t in enumerate(st["levels"]):
                stats["trisOut"][i] += t
            for tris_by_backend in st.get("backendTris") or []:
                for name, count in tris_by_backend.items():
                    stats["backendTris"][name] = stats["backendTris"].get(name, 0) + count
            index[asset_id] = {
                "tris": st["tris"],
                "levels": st["levels"],
                "displacement": st["displacement"],
                "backends": st.get("backends"),
                "identical": st.get("identical") or [],
                # what `_reusable` reads on the next bake
                "files": st.get("files") or [],
                "targets": st.get("targets"),
                "reuseV": st.get("reuseV"),
                "refineKey": _refine_key(refine),
                # persisted so a reused entry still reports what its refinement removed
                "refine": st.get("refine"),
            }
            _absorb_refine(st.get("refine"))
        elif status.startswith("failed"):
            progress.log(f"{asset_id}: {status}")

    # `parallel.each` advances and checks; `account` did the advancing before it existed.
    for result in parallel.each(
        _process_file,
        args,
        jobs,
        progress,
        chunksize=4,
        initializer=_init_worker,
        initargs=(catalogue,),
    ):
        account(result)
    stats["seconds"] = round(time.monotonic() - started, 1)
    ratio = [round(t / stats["trisIn"], 3) if stats["trisIn"] else None for t in stats["trisOut"]]
    stats["displacement"] = _displacement_summary(index, len(targets))
    stats["displacementPercentile"] = displacement.DISPLACEMENT_PERCENTILE
    with open(os.path.join(out_dir, INDEX_NAME), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "backend": stats["backend"],
                "targets": list(targets),
                "percentile": displacement.DISPLACEMENT_PERCENTILE,
                "assets": index,
            },
            handle,
            separators=(",", ":"),
            sort_keys=True,
        )
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
    progress.end(
        f"{stats['baked']} baked ({stats['authored']} authored, {stats['refused']} refused)"
        f"{reuse_note}, "
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


# NOT A VERSION BUMP: the product is per-asset incremental (`_reusable`), and reuse produces the
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
