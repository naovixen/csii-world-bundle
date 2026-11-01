"""The refined shipped level (`_LOD<n>R.glb`) of the `lods` product: a level a shape actually
ships, thinned by the MeshLab collapse and kept only where the measured displacement says the
surface did not move.
"""

import os

import numpy

from .. import decimate, decimatemeshlab, displacement, glbwrite

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


def write_refined(by_level, refine, out_dir, asset_id):
    """Every `REFINE_LEVELS` level of one asset that earned a refined copy, written as
    `<assetId>_LOD<n>R.glb`. Returns `(refined, written)`: the per-level stats keyed by the level
    as a string, and the file names in the order they were written."""
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
    return refined, refine_written
