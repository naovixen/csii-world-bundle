"""Writing one shape: the mesh states, the generated levels, the collider and the materials.

`shape_job` is what the worker pool runs, one call per shape, so it takes plain data and returns
plain data — no context, no progress, nothing that cannot be pickled.
"""

import os
from dataclasses import dataclass
from typing import Any

import numpy
from csiibundle import glb, lod

from ..beamng import dae, daemesh, placement
from . import shapebillboard, shapechain, shapecollider, shapefar, shapelods
from .shapeparts import _bound_material, collect_parts, split_lod_nodes, with_wind_colours

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
    lod_backend: Any = None
    # How far the game's own LOD 1 is from LOD 0 for this shape, in metres (`bake/lods`
    # `_chain_displacement`), or None. What `authored_lod0_max_tris` never asked before dropping
    # LOD 0 on a triangle count alone.
    chain_displacement: Any = None
    # where a ring member's silhouette payload is written as a small `.npz` (D1): the verts /
    # normals / colours / tris used to ride the result dict as Python lists - through the pool
    # pipe and into `shapes.state.json`, 15.4 of whose 19.7 MiB they were, parsed whole by the
    # audit for a handful of scalars
    ring_dir: Any = None


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
    detail_scale = shapelods.detail_scale(kind, options, radius)
    shape_name = placement.shape_key(job.asset, job.suffix, job.variant)
    base_mesh = daemesh.group_by_material(daemesh.merge_meshes(shape_name, parts))
    game_px, null_px, chain, levels, lod_stats, ladder = _ladder(
        job, source, shape_name, base_mesh, authored, size, area, lod_bias, radius, detail_scale
    )
    colliders, collider_kind = _colliders(
        job, shape_name, base_mesh, ladder, lod_stats, chain=chain
    )
    trunk_ratio = _trunk_ratio(collider_kind, colliders, base_mesh)
    billboard_px, final_null = shapebillboard.imposter_thresholds(
        null_px, detail_scale, size, area, options, chain, kind=kind
    )
    levels, billboard_px, final_null = _bottom(
        job,
        shape_name,
        ladder,
        levels,
        billboard_px,
        final_null,
        game_px,
        bound_raw,
        lod_stats,
        radius,
    )
    _record_ladder(levels, billboard_px, final_null, lod_stats)
    written = _write(job, shape_name, levels, colliders, billboard_px, final_null)
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
        lod_bias=lod_bias,
    )


def _ladder(
    job, source, shape_name, base_mesh, authored, size, area, lod_bias, radius, detail_scale
):
    """`(game_px, null_px, chain, levels, lod_stats, ladder)`: the thresholds (`_thresholds`) and
    the LOD ladder (`shapelods.lod_levels`) for one job."""
    game_px, null_px, chain = _thresholds(
        shape_name,
        authored,
        size,
        area,
        lod_bias,
        radius,
        detail_scale,
        job.options,
        base_mesh,
        chain_displacement=job.chain_displacement,
    )
    levels, lod_stats, ladder = shapelods.lod_levels(
        job.derived_dir,
        source,
        shape_name,
        base_mesh,
        null_px,
        radius,
        job.displacement,
        job.kind,
        job.aliases,
        job.tinted,
        job.level_name,
        job.options,
        chain=chain,
        meshlab_levels=tuple(job.lod_backend or ()),
    )
    return game_px, null_px, chain, levels, lod_stats, ladder


def _bottom(
    job, shape_name, ladder, levels, billboard_px, final_null, game_px, bound_raw, lod_stats, radius
):
    """`(levels, billboard_px, final_null)` after the far level, the billboard and the null."""
    far = shapefar.far_level(
        job, shape_name, ladder[-1], levels, final_null, game_px, bound_raw, lod_stats, radius
    )
    if far is not None:
        levels, final_null = far
        if (lod_stats.get("far") or {}).get("ring"):
            billboard_px = None  # the ring is the far look; a runtime billboard would sit under it
    levels, billboard_px = shapebillboard.vegetation_imposter(
        job, levels, billboard_px, final_null, lod_stats
    )
    levels, billboard_px = shapebillboard.above_null(levels, billboard_px, final_null, lod_stats)
    final_null = float(lod_stats["nullPx"])
    return levels, billboard_px, final_null


def _record_ladder(levels, billboard_px, final_null, lod_stats):
    # (1) the record of the ladder is the ladder that ships - one source, after every step
    lod_stats["detailSizes"] = [int(px) for px, _ms in levels]
    lod_stats["triangles"] = [int(sum(len(m.tris) for m in ms)) for _px, ms in levels]
    lod_stats["levels"] = len(levels)
    # Where the shape STOPS: the smallest detail Torque can still pick - the coarsest mesh, or
    # the billboard under it. The null is only the size that stop has to stay over, and the
    # report's draw distance is read off the stop (`shapereport._cull_summary`), not the null.
    stops = [int(px) for px, _ms in levels]
    if billboard_px:
        stops.append(int(max(1, round(billboard_px))))
    lod_stats["stopPx"] = min(stops) if stops else int(round(final_null))


def _write(job, shape_name, levels, colliders, billboard_px, final_null):
    """Write the `.dae`; returns `dae.write_shape`'s answer."""
    # The billboard is the dae `bb_autobillboard` node at the engine's default cell (64 px).
    # Sizing the cell was tried three ways in Phase 10 (record §8.14) - FCOLLADA user
    # properties (ignored by the importer), a TSShapeConstructor `.cs` and the engine's own
    # `<shape>.dae.asset.json` (both bake the atlas without the shape's materials: flat orange
    # trees, twice confirmed in-game, vanilla's parameters verbatim). The one delta left against
    # vanilla is where the materials live (theirs beside the shape); untested.
    shape = dae.Shape(
        shape_name,
        levels,
        collision=colliders,
        billboard_px=int(max(1, round(billboard_px))) if billboard_px else None,
        null_px=int(round(final_null)) or None,
    )
    path = os.path.join(job.out_dir, shape_name + ".dae")
    return dae.write_shape(path, shape)


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
    lod_bias=0.0,
):
    """What the report and the instances stage need from one written shape."""
    lo, hi, size, area, radius = bounds
    game_px, final_null, billboard_px = thresholds
    return {
        "file": shape_name + ".dae",
        "kind": kind,
        "changed": bool(written.get("changed")),
        # the game's tree/plant split (`csiibundle.instances.is_tree`), carried to the instances
        # stage so a forest type made from a bush does not ship collidable
        "tree": bool(is_tree) if kind == "tree" else None,
        "variantKey": variant_key,
        "radius": round(radius, 4),
        "renderingSize": round(lod.rendering_size(size), 4),
        "lodBias": round(lod.lod_bias(size, area), 4),
        "gamePx": round(float(game_px), 4),
        # with the prefab's authored `LodProperties.m_Bias`, as `gamePx` is (`_thresholds`):
        # the two are divided by each other in `shapereport._cull_summary` and read as one
        # sweep limit by `lod_audit` R2, and they described different meshes (E4)
        "gameCullMetres": round(
            lod.cull_distance(
                lod.mesh_min_lod(size, area, authored_bias=lod_bias), options["level_of_detail"]
            ),
            1,
        ),
        "nullPx": int(max(0, round(final_null))),
        "stopPx": lod_stats.get("stopPx"),
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


def _colliders(job, shape_name, base_mesh, ladder, lod_stats, chain=None):
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
    if chain is not None and chain.get("colliderMesh") is not None:
        lod1_mesh = chain["colliderMesh"]  # the authored LOD 1, even when the ladder dropped it
    if job.asset in job.options.get("no_collide", ()):
        return (), "lightNone"
    return shapecollider.collider(
        shape_name, base_mesh, lod1_mesh, job.kind, job.options, job.is_tree
    )


def _thresholds(
    shape_name,
    authored,
    size,
    area,
    lod_bias,
    radius,
    detail_scale,
    options,
    base_mesh=None,
    chain_displacement=None,
):
    """`(game_px, null_px, chain)`: CS2's cull for the mesh, this level's null, and the game's
    own LOD chain as a ladder when the shape ships one (its null replaces the single-mesh one)."""
    game_px = lod.game_detail_px(
        radius, size, area, authored_bias=lod_bias, level_of_detail=options["level_of_detail"]
    )
    null_px = max(1.0, game_px * detail_scale)
    chain = shapechain.authored_chain(
        shape_name,
        authored,
        size,
        area,
        lod_bias,
        radius,
        detail_scale,
        options,
        base_mesh,
        chain_displacement=chain_displacement,
    )
    if chain is not None:
        null_px = chain["nullPx"]
    return game_px, null_px, chain


_PARSE_MEMO = {}


def _parse_glb_memo(path):
    """`glb.parse_glb` with a one-entry memo per worker: a prefab's paint variants are adjacent
    in the job list (sorted), and each used to re-parse the same source (110 extra parses on the
    reference city, D2). One entry, because two shapes' node lists are big."""
    hit = _PARSE_MEMO.get(path)
    if hit is None:
        _PARSE_MEMO.clear()
        hit = glb.parse_glb(path)
        _PARSE_MEMO[path] = hit
    return hit


def _geometry(job, source):
    """`((parts, lo, hi, area, materials), None)` or `(None, "why this shape cannot be written")`."""
    try:
        nodes = _parse_glb_memo(os.path.join(job.bundle, "meshes", source + ".glb"))
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
