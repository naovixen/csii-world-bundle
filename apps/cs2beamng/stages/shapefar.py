"""The far level: a building's voxel silhouette under its ladder, out to the game's own cull.

A ring member's silhouette goes to `stages/farring` as a small `.npz` instead of a level of its
own; every other building gets one more detail, drawn with BeamNG's untextured `lod_vertcol`.
"""

import json
import math
import os

import numpy
from csiibundle import lod, png, silhouette

from ..beamng import dae

# The untextured vertex-colour material BeamNG ships for a shape's last level (the level_creation
# docs' `lod_vertcol`): one material, no texture bind, the colour is the vertex's own.
FAR_MATERIAL = "lod_vertcol"
# A far level is written only where it would be seen: its start (the shape's own null) has to
# sit at least this many pixels above the far null, or the band is a rounding.
FAR_MIN_BAND_PX = 2

# The map is 14 km across and CS2 draws a house to ~9 km and a tower to the far edge
# (`levelOfDetail 0.7`: until the rendering size is 1.43 px tall); `lod_detail_scale` culls
# every shape that many times nearer, which is the pop-in at the horizon. This puts one more
# level under the ladder — a closed voxel silhouette of the coarsest level (`csiibundle.silhouette`),
# coloured per face by the material that put the voxel there and drawn with BeamNG's untextured
# `lod_vertcol` — starting where the shape's own null was and stopping at CS2's own cull times
# `far_detail_scale` (1 = the game's distance). Buildings only: CS2 culls a prop at ~1 km itself
# and a tree's far look belongs to the forest. A box is 12 triangles; the shape's `nulldetail`
# moves out to the game's.

_CATALOGUE = {}
_ALBEDO = {}


def _ring_payload(job, shape_name, verts, normals, rgba, tris, radius, game_px):
    """A ring member's silhouette for `stages/farring` - an `.npz` beside the state, and the
    scalars inline. The arrays used to ride the result dict as lists (D1); the rounding is the
    old JSON one so the merged cells are the same bytes."""
    cull = round(float(lod.distance_at_px(radius, max(float(game_px), 1e-6))), 1)
    arrays = {
        "verts": numpy.asarray(verts, dtype=numpy.float32).round(3),
        "normals": numpy.asarray(normals, dtype=numpy.float32).round(4),
        "colors": numpy.asarray(rgba, dtype=numpy.float32).round(4),
        "tris": numpy.asarray(tris, dtype=numpy.int64),
    }
    if not job.ring_dir:
        return {**{k: v.tolist() for k, v in arrays.items()}, "cullMetres": cull}
    os.makedirs(job.ring_dir, exist_ok=True)
    path = os.path.join(job.ring_dir, shape_name + ".npz")
    with open(path, "wb") as handle:
        numpy.savez_compressed(handle, **arrays)
    return {
        "path": os.path.basename(path),
        "cullMetres": cull,
        "triangles": int(len(arrays["tris"])),
    }


def far_level(
    job, shape_name, coarsest, levels, final_null, game_px, bound_raw, lod_stats, radius=None
):
    """`(levels, far_null)` with the silhouette appended, or None when the shape gets none."""
    scale = float(job.options.get("far_detail_scale") or 0.0)
    if job.kind != "building" or scale <= 0.0 or not levels:
        return None
    # rounded UP: the far level may stop nearer than the game, never further (the same
    # one-direction rule as the generated ladder)
    far_null = float(max(1, math.ceil(float(game_px) * scale)))
    start = min(int(round(final_null)), levels[-1][0] - 1)
    # The far ring (`stages/farring`): below the hand-over distance D the per-building level
    # runs only up to D and the cell's merged ring takes over; a shape whose detailed levels
    # already reach past D keeps its own level to the game's cull and stays out of the ring.
    # With the ring on EVERY building hands over at D - towers included: at the game's own
    # switch points a house's LOD 2 reaches 16 km, so "detail reaching past D" was true of all
    # of them and nothing joined the ring (budget: 9.1 M building triangles at 8 km, no ring).
    # A tower's 1 m voxel hull at 2.5 km is what the ring is for.
    ring_m = float(job.options.get("far_ring_metres") or 0.0)
    in_ring = False
    if ring_m > 0.0 and radius:
        in_ring = True
        # rounded DOWN: the building must reach the hand-over (an overlap with the ring is a
        # hull inside a building at 2.5 km; a gap is a house that is not there). One pixel at
        # that distance is 600 m for a 6 m house (`lod_audit` R2, 2026-08-30).
        far_null = float(max(1, math.floor(lod.screen_px(radius, ring_m))))
    if not in_ring and (start < int(round(far_null)) + FAR_MIN_BAND_PX or start < 2):
        return None  # the shape already draws to the game's cull, or the band would be a rounding
    colours, ids = _run_colours(job, coarsest, bound_raw)
    verts, normals, rgba, tris, stats = silhouette.silhouette(
        coarsest.verts, coarsest.tris, ids, colours
    )
    if len(tris) == 0:
        return None
    if in_ring:
        return _ring_levels(
            job,
            shape_name,
            levels,
            far_null,
            verts,
            normals,
            rgba,
            tris,
            stats,
            lod_stats,
            radius,
            game_px,
        )
    return _far_mesh_levels(
        shape_name, levels, start, far_null, verts, normals, rgba, tris, stats, lod_stats
    )


def _ring_levels(
    job, shape_name, levels, far_null, verts, normals, rgba, tris, stats, lod_stats, radius, game_px
):
    """`(levels, null)` for a ring member: its levels run to the hand-over, the ring takes over."""
    # A ring member gets NO per-building box: its coarsest level runs out to the hand-over
    # (the shape's null moves from CS2's cull / `lod_detail_scale` to D) and the cell's
    # merged silhouette takes over there. Naomi's look, 2026-08-30: boxes "up close" on the
    # small houses were this band - a house's detailed levels stop a few hundred metres out
    # at scale 10, and a box from there is not a skyline, it is the house.
    lod_stats["farRing"] = _ring_payload(
        job, shape_name, verts, normals, rgba, tris, radius, game_px
    )
    lod_stats["far"] = {"px": None, "nullPx": int(round(far_null)), "ring": True, **stats}
    # Torque picks the detail by SIZE, so a level whose size sits under the new null (a
    # house's LOD 2 starts at the game's 16 km, 2 px, and the null at 2.5 km is 4 px) would
    # still be drawn beyond the hand-over. Levels under the null go; the finer level above
    # them covers the band to D. A shape whose LOD 0 itself sits under the null keeps it at
    # one pixel over (drawn to D, then the ring).
    # ...and the coarsest surviving level's SIZE becomes the hand-over's pixel size: Torque
    # draws a level for p >= its size, so a level left at 44 px with a null at 3 px vanishes
    # at 181 m and the null is chosen from there to 2.5 km (`lod_audit` R2: "not drawn at
    # 2,480 m"). The null sits one pixel under it.
    # 1 px is allowed here: a shed under 4 m projects to less than 2 px at 2.5 km, and a
    # 2 px floor stopped it 600 m short of the ring; at 1 px it has no null and is drawn
    # until it is under a pixel, which costs nothing
    hand_px = int(max(1, far_null))
    above = [(px, ms) for px, ms in levels if int(px) > hand_px]
    under = [(px, ms) for px, ms in levels if int(px) <= hand_px]
    if under:
        # the level the game shows AT the hand-over - the nearest of those under it (the
        # largest size not above p at D) - covers the band up to it. Dropping every level
        # under the hand-over ran LOD 0 to 2.5 km: 32 M building triangles (budget, v28)
        kept = [*above, (hand_px, under[0][1])]
    else:
        kept = [*above[:-1], (hand_px, above[-1][1])]
    if len(kept) != len(levels):
        lod_stats["levelsUnderHandOver"] = len(levels) - len(kept)
    return kept, float(hand_px - 1)


def _far_mesh_levels(
    shape_name, levels, start, far_null, verts, normals, rgba, tris, stats, lod_stats
):
    """`(levels, far_null)` with the silhouette appended as the shape's last detail."""
    far_mesh = dae.Mesh(
        f"{shape_name}_FAR",
        verts,
        tris,
        [(FAR_MATERIAL, len(tris))],
        normals=normals,
        uvs=numpy.zeros((len(verts), 2), dtype=numpy.float32),
        colors=rgba,
    )
    lod_stats["far"] = {"px": start, "nullPx": int(round(far_null)), **stats}
    lod_stats["detailSizes"] = [*lod_stats.get("detailSizes", []), start]
    lod_stats["triangles"] = [*lod_stats.get("triangles", []), int(len(tris))]
    return [*levels, (start, [far_mesh])], far_null


def _run_colours(job, mesh, bound_raw):
    """`(palette (K,3), per-triangle ids)` — each material run's mean albedo, in the run order of
    `mesh.runs`. A variant shape keeps its canonical material's colour (DECLARED: the silhouette is
    seen at 20 px; the tint is a hue on a box)."""
    palette, ids = [], []
    for bound, count in mesh.runs:
        raw = bound_raw.get(bound, bound)
        canonical = job.aliases.get(raw, raw)
        palette.append(_mean_albedo(job.bundle, job.derived_dir, canonical))
        ids.extend([len(palette) - 1] * int(count))
    if not palette:
        palette.append([0.6, 0.6, 0.6])
    return numpy.asarray(palette, dtype=numpy.float64), numpy.asarray(ids, dtype=numpy.int64)


def _mean_albedo(bundle_dir, derived_dir, canonical):
    """The mean of a material's base-colour texels (alpha-weighted) times its factor, 0..1 sRGB;
    mid grey when the material has no texture. Cached per worker."""
    catalogue = _CATALOGUE.get(derived_dir)
    if catalogue is None:
        path = os.path.join(derived_dir, "materials.json")
        try:
            with open(path, encoding="utf-8") as handle:
                catalogue = json.load(handle).get("materials") or {}
        except (OSError, ValueError):
            catalogue = {}
        _CATALOGUE[derived_dir] = catalogue
    entry = catalogue.get(canonical) or {}
    factor = entry.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
    rel = entry.get("baseColor")
    mean = numpy.array([0.6, 0.6, 0.6])
    if rel:
        key = (bundle_dir, rel)
        if key not in _ALBEDO:
            try:
                img = png.rgba_array(os.path.join(bundle_dir, rel)).astype(numpy.float64)
                alpha = img[..., 3:4] / 255.0
                weight = float(alpha.sum())
                _ALBEDO[key] = (
                    (img[..., :3] * alpha).sum(axis=(0, 1)) / (255.0 * weight)
                    if weight > 0.0
                    else mean
                )
            except Exception:  # an unreadable texture is mid grey
                _ALBEDO[key] = mean
        mean = _ALBEDO[key]
    return [float(min(1.0, max(0.0, mean[i] * float(factor[i])))) for i in range(3)]
