"""The bottom of a shape's ladder: the runtime billboard, and the null detail under it.

A billboard is for vegetation only, where the game's own far level for a tree is a card; every
shape's details end strictly above the null, with the billboard between them.
"""

from csiibundle import lod

from .shapelods import IMPOSTER_REACH


def imposter_thresholds(null_px, detail_scale, size, area, options, chain=None, kind=None):
    """`(billboard_px, null_px)` — where a billboard takes over, and where drawing stops.

    VEGETATION ONLY. Vanilla ships `bb_autobillboard` on 82 of west_coast_usa's 2,608 shapes
    and every one is vegetation; the engine bakes the atlas at play time and never frees it
    (the 14-minute imposter bake of 2026-08-18, one atlas per shape). Nothing here gated on the
    kind - a large no-chain building passed `imposter_min_metres` first of all, and only the
    far level happened to unset it; on the lowest preset (`far_detail_scale` 0) every landmark
    would have shipped an atlas (validation pass 2026-08-31, C3; audit R9).

    `gameCullMetres` divided by the declared `lod_detail_scale` is how far THIS level draws the
    mesh; an imposter only earns its atlas past `imposter_min_metres`.

    A shape that ships the game's own LOD chain gets none: CS2 draws its coarsest authored level
    right up to the cull and has no impostor for it (`RequiredBatchesSystem`), that level is
    already a few per cent of LOD 0, and a runtime `bb_autobillboard` atlas is the one thing
    BeamNG builds per shape at play time and never frees. The imposter stays a declared
    consumer addition for the generated ladder only.
    """
    if chain is not None or kind != "tree":
        return None, null_px
    reach_m = lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]) / max(
        detail_scale, 1e-6
    )
    wants_imposter = options["imposters"] and reach_m >= options["imposter_min_metres"]
    if not wants_imposter:
        return None, null_px
    # A Torque detail's size is its own FAR STOP, and the billboard is a detail like any other:
    # vanilla's pine_radiata_forest.dae ships meshes a800/a350/a150 and `bb_autobillboard25` -
    # the card's size sits at the far end of its band, one step over where drawing ends. Parking
    # the billboard at the mesh's null instead left [null / IMPOSTER_REACH, null) selecting the
    # null: the declared 3x reach never drew (found on the trees, 2026-08-31 - the same shape).
    far_px = null_px / IMPOSTER_REACH
    return far_px + 1.0, far_px


# -- vegetation: the game's own far level for a tree is a CARD ---------------------------------
#
# Every vanilla tree's authored LOD 1 is `MeshFlags.Impostor` — a POSITION-only card the game's
# shader textures from a baked atlas (the producer counts and skips them; the parser refuses a
# node without normals). So past its LOD 0 the game draws a tree as a billboard, and that is the
# rule to port: BeamNG's `bb_autobillboard` is the same mechanism (an atlas rendered once per
# shape, batched per forest cell — `TSForestCellBatch` exists only for shapes with a billboard
# detail). It takes over where the generated ladder's coarsest level did, which on the reference
# city is where 9.2 M of the 13.7 M triangles inside an 8 km view were: 892,000 forest items at
# their 6 % level. Vanilla ships it on 82 of west_coast_usa's 2,608 shapes and every one of them
# is vegetation; ours goes on the 292 vegetation shapes, not on buildings (§1 of the record).


def above_null(levels, billboard_px, final_null, lod_stats):
    """Every detail strictly above the null, the billboard between the meshes and the null.

    Torque orders details by size and a mesh at the null's own size is a coin toss between
    "drawn" and "gone" (`tools/beamng/lod_audit.py` R1: 1,028 props at the game's own distances
    had their coarsest level and their null both at 2 px). The coarsest level is lifted to one
    pixel over the null (or over the billboard), and every nearer level lifted as little as
    keeps the ladder strictly decreasing. Counted in `lod_stats["liftedAboveNull"]`.
    """
    # The coarsest mesh's own size IS where the shape stops (Torque draws it for p >= size and
    # nothing sits below it); the null only has to be smaller. Lifting the mesh instead moved
    # the stop and left a band [null, null+1) where the null was chosen - 1,678-2,510 m of no
    # warehouse (`lod_audit` R2, 2026-08-30). So: the mesh keeps its size, the null comes down.
    null = int(max(0, round(final_null)))  # 0 = no null detail at all
    bb = None if not billboard_px else int(max(1, round(billboard_px)))
    lod_stats["nullPx"] = null
    if not levels:
        return levels, billboard_px
    coarsest = int(levels[-1][0])
    if bb is not None and bb >= coarsest:
        bb = coarsest - 1 if coarsest > 2 else None
        lod_stats["billboardLowered"] = True
    floor = bb if bb is not None else coarsest
    if null >= floor:
        lod_stats["nullLowered"] = {"from": null, "to": max(0, floor - 1)}
        null = max(0, floor - 1)
        if bb is not None and bb <= null:
            bb = None
            lod_stats["billboardLowered"] = "dropped"
    lod_stats["nullPx"] = null
    return list(levels), (float(bb) if bb is not None else None)


def vegetation_imposter(job, levels, billboard_px, final_null, lod_stats):
    """`(levels, billboard_px)`: a vegetation shape keeps LOD 0 and hands straight to the engine
    billboard where its first generated level would have started; anything else unchanged.

    The game's own vanilla trees do exactly this - their authored LOD 1 IS an impostor card
    (`MeshFlags.Impostor`, skipped by the producer) - so LOD 0 -> billboard is the game's rule,
    and the clustered canopy that used to sit between them was ours (record §8.12)."""
    if job.kind != "tree" or not job.options.get("imposters", True) or len(levels) < 2:
        return levels, billboard_px
    # The card takes over where LOD 0 STOPS - LOD 0's own size, `levels[0][0]` - so that is the
    # size to test for a band above the null. This used to test the first generated level's
    # size instead, which is where THAT level stops; with the coarsest generated level now one
    # pixel over the null (A5) that size IS null + 1 for a tree with a single generated level,
    # and 28 trees lost their billboard and kept a bare mid-level (audit R3, Batch 1 rebuild).
    px = int(levels[0][0])
    if px <= int(round(final_null)) + 1:
        return levels, billboard_px
    # The card takes over where LOD 0 stops (LOD 0's own size is that boundary) and must run to
    # the cull, so the billboard detail's SIZE is one pixel over the null - a Torque detail's
    # size is its far stop, not its takeover (vanilla: pine_radiata_forest.dae, meshes
    # a800/a350/a150 and `bb_autobillboard25`). Written at the takeover size (v30-v33) the band
    # [null, takeover) selected the null: SpruceTree01 vanished from 99 m to 601 m - the bare
    # mid-range of Naomi's 2026-08-30/31 looks - while audit R1 passed (R8 pins this now).
    bb_px = float(int(round(final_null)) + 1)
    lod_stats["imposter"] = {
        "px": int(bb_px),
        "handoverPx": px,
        "replacedTriangles": int(sum(len(m.tris) for _px, ms in levels[1:] for m in ms)),
    }
    lod_stats["levels"] = 1
    lod_stats["detailSizes"] = [size for size, _ms in levels[:1]]
    lod_stats["triangles"] = lod_stats.get("triangles", [])[:1]
    return levels[:1], bb_px
