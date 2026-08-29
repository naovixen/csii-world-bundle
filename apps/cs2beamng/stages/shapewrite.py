"""Writing one shape: the mesh states, the generated levels, the collider and the materials.

`shape_job` is what the worker pool runs, one call per shape, so it takes plain data and returns
plain data — no context, no progress, nothing that cannot be pickled.
"""

import os
from dataclasses import dataclass
from typing import Any

import json
import math

import numpy
from csiibundle import glb, lod, png, silhouette

from ..beamng import dae, daemesh, placement
from . import shapecollider, shapelods
from .shapelods import (
    IMPOSTER_REACH,
)
from .shapeparts import _bound_material, collect_parts, split_lod_nodes, with_wind_colours

# The untextured vertex-colour material BeamNG ships for a shape's last level (the level_creation
# docs' `lod_vertcol`): one material, no texture bind, the colour is the vertex's own.
FAR_MATERIAL = "lod_vertcol"
# A far level is written only where it would be seen: its start (the shape's own null) has to
# sit at least this many pixels above the far null, or the band is a rounding.
FAR_MIN_BAND_PX = 2

# -- writing one shape ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShapeJob:
    """One shape to write, as the worker pool receives it.

    A record rather than the 14-element tuple this was, because the pool pickles it and both of
    the two places that build one had to list all fourteen in the same order — the untinted shape
    and the per-variant one, side by side, with nothing checking they agreed.
    """

    bundle: str
    derived_dir: str
    out_dir: str
    level_name: str
    asset: str
    suffix: str
    variant: int | None
    variant_key: str | None
    kind: str
    options: dict
    aliases: dict
    tinted: dict
    is_tree: Any
    displacement: Any


def shape_job(job):
    """Worker: one `(asset, suffix, variant)` -> a written `.dae` plus the numbers the report and
    the instances stage need. Returns `(key, result_dict)`; `result_dict["error"]` on failure."""
    kind, options = job.kind, job.options
    key = (job.asset, job.suffix, job.variant)
    source = job.asset + job.suffix
    geometry, error = _geometry(job, source)
    if error is not None:
        return key, {"error": error}
    parts, lo, hi, area, materials_used, authored, lod_bias, material_levels, bound_raw = geometry
    size = hi - lo
    radius = 0.5 * float(numpy.linalg.norm(size))
    detail_scale = shapelods.detail_scale(kind, options)
    shape_name = placement.shape_key(job.asset, job.suffix, job.variant)
    base_mesh = daemesh.group_by_material(daemesh.merge_meshes(shape_name, parts))
    game_px, null_px, chain = _thresholds(
        shape_name, authored, size, area, lod_bias, radius, detail_scale, options
    )
    levels, lod_stats, ladder = shapelods.lod_levels(
        job.derived_dir,
        source,
        shape_name,
        base_mesh,
        null_px,
        radius,
        job.displacement,
        kind,
        job.aliases,
        job.tinted,
        job.level_name,
        options,
        chain=chain,
    )
    colliders, collider_kind = _colliders(job, shape_name, base_mesh, ladder, lod_stats)
    trunk_ratio = _trunk_ratio(collider_kind, colliders, base_mesh)
    billboard_px, final_null = _imposter_thresholds(
        null_px, detail_scale, size, area, options, chain
    )
    far = _far_level(job, shape_name, ladder[-1], levels, final_null, game_px, bound_raw, lod_stats)
    if far is not None:
        levels, final_null = far
    levels, billboard_px = _vegetation_imposter(job, levels, billboard_px, final_null, lod_stats)
    shape = dae.Shape(
        shape_name,
        levels,
        collision=colliders,
        billboard_px=int(max(1, round(billboard_px))) if billboard_px else None,
        null_px=int(max(1, round(final_null))),
    )
    path = os.path.join(job.out_dir, shape_name + ".dae")
    written = dae.write_shape(path, shape)
    return key, _result(
        shape_name,
        written,
        kind=kind,
        is_tree=job.is_tree,
        variant_key=job.variant_key,
        bounds=(lo, hi, size, area, radius),
        thresholds=(game_px, final_null, billboard_px),
        options=options,
        materials_used=materials_used,
        material_levels=material_levels,
        collider_kind=collider_kind,
        trunk_ratio=trunk_ratio,
        lod_stats=lod_stats,
    )


def _result(
    shape_name,
    written,
    *,
    kind,
    is_tree,
    variant_key,
    bounds,
    thresholds,
    options,
    materials_used,
    material_levels,
    collider_kind,
    trunk_ratio,
    lod_stats,
):
    """What the report and the instances stage need from one written shape."""
    lo, hi, size, area, radius = bounds
    game_px, final_null, billboard_px = thresholds
    return {
        "file": shape_name + ".dae",
        "changed": bool(written.get("changed")),
        # the game's tree/plant split (`csiibundle.instances.is_tree`), carried to the instances
        # stage so a forest type made from a bush does not ship collidable
        "tree": bool(is_tree) if kind == "tree" else None,
        "variantKey": variant_key,
        "radius": round(radius, 4),
        "renderingSize": round(lod.rendering_size(size), 4),
        "lodBias": round(lod.lod_bias(size, area), 4),
        "gamePx": round(float(game_px), 4),
        "gameCullMetres": round(
            lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]), 1
        ),
        "nullPx": int(max(1, round(final_null))),
        "billboardPx": int(max(1, round(billboard_px))) if billboard_px else None,
        "boundsMin": [round(float(v), 4) for v in lo],
        "boundsMax": [round(float(v), 4) for v in hi],
        "triangles": written["triangles"],
        "vertices": written["vertices"],
        "materials": sorted(materials_used),
        # the nearest authored level each material is seen at (0 = LOD 0); the materials stage
        # caps a texture bound only past LOD 0 by that level's on-screen size
        "materialLevels": {k: v for k, v in sorted(material_levels.items()) if v > 0},
        "collider": collider_kind,
        "trunkRatio": trunk_ratio,
        "lods": lod_stats,
    }


def _colliders(job, shape_name, base_mesh, ladder, lod_stats):
    """The collision meshes for one shape, and which rule produced them.

    The collider comes off LOD 1 where the bake generated one — `collision.colmesh_from_lod` is
    written for the ~25 % level, and a 20k-triangle building shell is not a collider. **WHICH
    ENTRY OF THE LADDER THAT IS MOVED IN A8**: with `lod0_max_error` promoting LOD 1 to the top
    detail, LOD 1 is `ladder[0]` and `ladder[1]` is LOD 2 — taking index 1 blindly would hand
    every promoted shape a collider two levels coarse than intended.
    """
    lod1_mesh = (
        ladder[0]
        if lod_stats.get("lod0Promoted")
        else (ladder[1] if len(ladder) > 1 else base_mesh)
    )
    if job.asset in job.options.get("no_collide", ()):
        return (), "lightNone"
    return shapecollider.collider(
        shape_name, base_mesh, lod1_mesh, job.kind, job.options, job.is_tree
    )


def _thresholds(shape_name, authored, size, area, lod_bias, radius, detail_scale, options):
    """`(game_px, null_px, chain)`: CS2's cull for the mesh, this level's null, and the game's
    own LOD chain as a ladder when the shape ships one (its null replaces the single-mesh one)."""
    game_px = lod.game_detail_px(
        radius, size, area, authored_bias=lod_bias, level_of_detail=options["level_of_detail"]
    )
    null_px = max(1.0, game_px * detail_scale)
    chain = shapelods.authored_chain(
        shape_name, authored, size, area, lod_bias, radius, detail_scale, options
    )
    if chain is not None:
        null_px = chain["nullPx"]
    return game_px, null_px, chain


def _geometry(job, source):
    """`((parts, lo, hi, area, materials), None)` or `(None, "why this shape cannot be written")`."""
    try:
        nodes = glb.parse_glb(os.path.join(job.bundle, "meshes", source + ".glb"))
    except Exception as exc:
        return None, f"{source}: {exc}"
    if not nodes:
        return None, f"{source}: no mesh-bearing node"
    base_nodes, lod_nodes, lod_bias = split_lod_nodes(nodes)
    parts, lo, hi, area, materials_used = collect_parts(
        base_nodes, job.aliases, job.tinted, job.level_name
    )
    if not parts:
        return None, f"{source}: every node was empty"
    if job.kind == "tree" and job.options["tree_wind"]:
        parts = with_wind_colours(parts)
    # the game's own coarser levels, in order, each as the parts a mesh is merged from; a
    # material bound ONLY by a level records the nearest level it is seen at, so the materials
    # stage can size its textures for that distance
    authored = []
    material_levels = dict.fromkeys(materials_used, 0)
    for level in sorted(lod_nodes):
        lod_parts, l_lo, l_hi, _area, used = collect_parts(
            lod_nodes[level], job.aliases, job.tinted, job.level_name
        )
        if not lod_parts:
            continue
        for name in used:
            material_levels[name] = min(material_levels.get(name, level), level)
        materials_used |= used
        authored.append((level, lod_parts, l_hi - l_lo))
    bound_raw = {}
    for node in [*base_nodes, *(n for level in lod_nodes.values() for n in level)]:
        for raw, _count in node["material_runs"]:
            bound_raw.setdefault(_bound_material(raw, job.aliases, job.tinted, job.level_name), raw)
    return (
        parts,
        lo,
        hi,
        area,
        materials_used,
        authored,
        lod_bias,
        material_levels,
        bound_raw,
    ), None


def _trunk_ratio(collider_kind, colliders, base_mesh):
    """How well the trunk search actually found a trunk, or None when it did not look.

    `collision.trunk_group` prefers a material run NAMED for one, and on a CS2 bundle that branch
    never fires — the game's material names are hashed asset ids
    (`lvl_OakTreeAdult01_c8092af6...`), so all 32 tree prefabs of the reference city fall through
    to the geometric fallback. That fallback is right for most of them (OakTree01: a 0.40 m
    cylinder under an 8.11 m canopy) and wrong where the lowest band is not the trunk — a spruce
    whose branches sweep the ground, or a wide shrub CS2 counts as a tree. MEASURED, not fixed:
    5 of 32 prefabs and 4.5 % of tree items, declared in the report.
    """
    if collider_kind != "trunk" or len(colliders) != 1:
        return None
    cyl = numpy.asarray(colliders[0].verts, dtype=numpy.float64)
    base_v = numpy.asarray(base_mesh.verts, dtype=numpy.float64)
    canopy = 0.5 * float(max(numpy.ptp(base_v[:, 0]), numpy.ptp(base_v[:, 1])))
    trunk = 0.5 * float(max(numpy.ptp(cyl[:, 0]), numpy.ptp(cyl[:, 1])))
    return round(trunk / canopy, 3) if canopy > 1e-6 else None


def _imposter_thresholds(null_px, detail_scale, size, area, options, chain=None):
    """`(billboard_px, null_px)` — where a billboard takes over, and where drawing stops.

    `gameCullMetres` divided by the declared `lod_detail_scale` is how far THIS level draws the
    mesh; an imposter only earns its atlas past `imposter_min_metres`.

    A shape that ships the game's own LOD chain gets none: CS2 draws its coarsest authored level
    right up to the cull and has no impostor for it (`RequiredBatchesSystem`), that level is
    already a few per cent of LOD 0, and a runtime `bb_autobillboard` atlas is the one thing
    BeamNG builds per shape at play time and never frees. The imposter stays a declared
    consumer addition for the generated ladder only.
    """
    if chain is not None:
        return None, null_px
    reach_m = lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]) / max(
        detail_scale, 1e-6
    )
    wants_imposter = options["imposters"] and reach_m >= options["imposter_min_metres"]
    if not wants_imposter:
        return None, null_px
    return null_px, null_px / IMPOSTER_REACH


# -- the far level: the building's silhouette out to the game's own cull ------------------------
#
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


def _far_level(job, shape_name, coarsest, levels, final_null, game_px, bound_raw, lod_stats):
    """`(levels, far_null)` with the silhouette appended, or None when the shape gets none."""
    scale = float(job.options.get("far_detail_scale") or 0.0)
    if job.kind != "building" or scale <= 0.0 or not levels:
        return None
    # rounded UP: the far level may stop nearer than the game, never further (the same
    # one-direction rule as the generated ladder)
    far_null = float(max(1, math.ceil(float(game_px) * scale)))
    start = min(int(round(final_null)), levels[-1][0] - 1)
    if start < int(round(far_null)) + FAR_MIN_BAND_PX or start < 2:
        return None  # the shape already draws to the game's cull, or the band would be a rounding
    colours, ids = _run_colours(job, coarsest, bound_raw)
    verts, normals, rgba, tris, stats = silhouette.silhouette(
        coarsest.verts, coarsest.tris, ids, colours
    )
    if len(tris) == 0:
        return None
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
            except Exception:  # noqa: BLE001 - an unreadable texture is mid grey
                _ALBEDO[key] = mean
        mean = _ALBEDO[key]
    return [float(min(1.0, max(0.0, mean[i] * float(factor[i])))) for i in range(3)]


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


def _vegetation_imposter(job, levels, billboard_px, final_null, lod_stats):
    """`(levels, billboard_px)`: a vegetation shape's coarsest generated level replaced by the
    engine billboard at the same switch size; anything else unchanged."""
    if job.kind != "tree" or not job.options.get("imposters", True) or len(levels) < 2:
        return levels, billboard_px
    px = int(levels[-1][0])
    if px <= int(round(final_null)) + 1:
        return levels, billboard_px
    lod_stats["imposter"] = {
        "px": px,
        "replacedTriangles": int(sum(len(m.tris) for m in levels[-1][1])),
    }
    lod_stats["levels"] = len(levels) - 1
    lod_stats["detailSizes"] = [size for size, _ms in levels[:-1]]
    lod_stats["triangles"] = lod_stats.get("triangles", [])[: len(levels) - 1]
    return levels[:-1], float(px)
