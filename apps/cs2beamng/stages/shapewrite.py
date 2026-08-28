"""Writing one shape: the mesh states, the generated levels, the collider and the materials.

`shape_job` is what the worker pool runs, one call per shape, so it takes plain data and returns
plain data — no context, no progress, nothing that cannot be pickled.
"""

import os
from dataclasses import dataclass
from typing import Any

import numpy
from csiibundle import glb, lod

from ..beamng import dae, placement
from . import shapecollider, shapelods
from .shapelods import (
    IMPOSTER_REACH,
)
from .shapeparts import collect_parts, with_wind_colours

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
    parts, lo, hi, area, materials_used = geometry
    size = hi - lo
    radius = 0.5 * float(numpy.linalg.norm(size))
    game_px = lod.game_detail_px(radius, size, area, level_of_detail=options["level_of_detail"])
    detail_scale = shapelods.detail_scale(kind, options)
    null_px = max(1.0, game_px * detail_scale)
    shape_name = placement.shape_key(job.asset, job.suffix, job.variant)
    base_mesh = dae.group_by_material(dae.merge_meshes(shape_name, parts))
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
    )
    colliders, collider_kind = _colliders(job, shape_name, base_mesh, ladder, lod_stats)
    trunk_ratio = _trunk_ratio(collider_kind, colliders, base_mesh)
    billboard_px, final_null = _imposter_thresholds(null_px, detail_scale, size, area, options)
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


def _geometry(job, source):
    """`((parts, lo, hi, area, materials), None)` or `(None, "why this shape cannot be written")`."""
    try:
        nodes = glb.parse_glb(os.path.join(job.bundle, "meshes", source + ".glb"))
    except Exception as exc:
        return None, f"{source}: {exc}"
    if not nodes:
        return None, f"{source}: no mesh-bearing node"
    parts, lo, hi, area, materials_used = collect_parts(
        nodes, job.aliases, job.tinted, job.level_name
    )
    if not parts:
        return None, f"{source}: every node was empty"
    if job.kind == "tree" and job.options["tree_wind"]:
        parts = with_wind_colours(parts)
    return (parts, lo, hi, area, materials_used), None


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


def _imposter_thresholds(null_px, detail_scale, size, area, options):
    """`(billboard_px, null_px)` — where a billboard takes over, and where drawing stops.

    `gameCullMetres` divided by the declared `lod_detail_scale` is how far THIS level draws the
    mesh; an imposter only earns its atlas past `imposter_min_metres`.
    """
    reach_m = lod.cull_distance(lod.mesh_min_lod(size, area), options["level_of_detail"]) / max(
        detail_scale, 1e-6
    )
    wants_imposter = options["imposters"] and reach_m >= options["imposter_min_metres"]
    if not wants_imposter:
        return None, null_px
    return null_px, null_px / IMPOSTER_REACH
