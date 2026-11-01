"""The game's own LOD chain as a shape's ladder, in the strictly decreasing form Torque reads.

Where an authored chain exists, its switch points are CS2's (`lodchain.authored_lod_limits`), and
the residency levers that drop a level from it are declared and counted on the chain.
"""

from csiibundle import lod, lodchain

from ..beamng import daemesh
from .shapelevels import mesh_digest

# `authored_lod0_max_tris` keeps LOD 0 where the authored LOD 1 is under this fraction of it -
# a stand-in that coarse is not the same building (Oracle_Park: 0.6 %).
DEGENERATE_LOD1_RATIO = 0.05


def authored_chain(
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
    """The game's own LOD chain as a ladder, or None when the shape ships none.

    `authored` is `[(level, parts, level_size), ...]` from `shapeparts.split_lod_nodes`. The
    switch points are the game's (`lod.authored_lod_limits`, the `RequiredBatchesSystem` port),
    scaled by `lod_detail_scale` exactly as the null threshold always was — so a chain shape
    switches where CS2 switches it, `detail_scale` times nearer, and stops drawing where CS2
    stops. No displacement rule, no promotion, no generated level: the artist's levels have no
    holes and the game's rule is the authority (refactor Phase 10, 2026-08-29 — every decimator
    turned a CS2 facade into a see-through lattice, and no distance metric could see it).
    """
    if not authored or not options.get("authored_lods", True):
        return None
    meshes, sized, identical, kept_levels = _chain_levels(shape_name, authored, base_mesh)
    if not meshes:
        return None
    limits, px = _chain_limits(size, area, lod_bias, sized, radius, detail_scale, options)
    # A Torque detail's size is where that detail STARTS: LOD 0 at the size its own limit
    # stops it, level j at the size limit j stops it, and the null a hair under the coarsest
    # (the game draws the coarsest level right up to its cull; Torque wants the two sizes
    # distinct, so the last 10 % before the cull is a rounding, not a rule).
    chain = {
        "meshes": meshes,
        "thresholds": px,
        "nullPx": px[-1] * 0.9,
        "limits": limits,
        # the authored level number behind each kept mesh, so the ladder can ask for that
        # level's refined copy (`refined_level`); stays aligned through every drop below
        "levels": list(kept_levels),
        "colliderMesh": meshes[0],
        "lod1Dropped": False,
        "lod0Dropped": False,
        "identicalLevels": identical,
    }
    if _drop_lod0(chain, meshes, px, base_mesh, chain_displacement, options):
        return chain
    _drop_lod1(chain, meshes, kept_levels, px, options)
    return chain


def _chain_levels(shape_name, authored, base_mesh):
    """`(meshes, sized, identical, kept_levels)`: every authored level that is a LOD of its own."""
    meshes, sized, identical, kept_levels = [], [], [], []
    base_digest = mesh_digest(base_mesh) if base_mesh is not None else None
    for level, parts, level_size in authored:
        mesh = daemesh.group_by_material(daemesh.merge_meshes(f"{shape_name}_LOD{level}", parts))
        if len(mesh.tris) == 0:
            continue
        # An authored level that is BYTE-IDENTICAL to LOD 0 is not a LOD: a workshop asset
        # shipped with its LOD slots filled by copies of the full mesh (Kinsman_Plaza: 176,587
        # triangles twice, 37 MiB; 12 such levels on the reference city). The game draws the
        # copy where its limit says; drawing LOD 0 there instead is the same pixels for none of
        # the bytes, so the level is left out and LOD 0 keeps its band. Counted per shape.
        if base_digest is not None and mesh_digest(mesh) == base_digest:
            identical.append(level)
            continue
        meshes.append(mesh)
        kept_levels.append(level)
        sized.append((level_size, len(mesh.tris) * 3))
    return meshes, sized, identical, kept_levels


def _chain_limits(size, area, lod_bias, sized, radius, detail_scale, options):
    """`(limits, px)`: the game's own LOD limits, and each as a detail size at this level's scale."""
    bias = lod.lod_bias(size, area, lod_bias)
    limits = lodchain.authored_lod_limits(
        size, sized, bias, min_lod=lod.mesh_min_lod(size, area, lod_bias)
    )
    # The switch points are the game's limits times ONE uniform scale. Unscaled (v21-v29) this
    # city is 23.5 M building triangles inside 4 km - the custom packs carry LOD biases that keep
    # CS2 drawing their 10k-triangle LOD 0 for kilometres, and CS2 can, BeamNG cannot. What made
    # v20 "way too close and inconsistent" (Naomi) was three scales - buildings 10, props 4,
    # generated ladders their own - not the scaling; one scale for every chain is consistent.
    px = [
        max(1.0, lodchain.limit_px(radius, limit, options["level_of_detail"]) * detail_scale)
        for limit in limits
    ]
    return limits, px


def _drop_lod0(chain, meshes, px, base_mesh, chain_displacement, options):
    """Apply `authored_lod0_max_tris` to `chain`; True when LOD 0 was left out."""
    # `authored_lod0_max_tris`: the residency lever that reaches the bytes `authored_lod1_drop`
    # cannot. With LOD 1 gone, LOD 0 is ~90 % of an object shape's bytes and the reference
    # city's 20.2 M top-level triangles at ~100 B each are what an 8 GB card pages on
    # (record §7-§8). For a shape whose LOD 0 is at or over this many triangles, the game's own
    # LOD 1 becomes the NEAREST level shipped and LOD 0 is left out: LOD 1 is a median 41 % of
    # LOD 0 (measured: >= 20,000 tris reaches 189 shapes for -599 MiB of cooked files; >= 5,000
    # reaches 591 for -854 MiB). What it costs is declared: those shapes draw the game's LOD 1 at
    # every distance, including up close — nothing the game does not draw somewhere, but nearer.
    # The collider still comes off LOD 1, and `authored_lod1_drop` yields to this: LOD 1 is now
    # the top and LOD 2 keeps its own band. 0 = off.
    max_tris = int(options.get("authored_lod0_max_tris") or 0)
    if max_tris > 0 and base_mesh is not None and len(base_mesh.tris) >= max_tris:
        # The drop banks on LOD 1 being a usable near stand-in (the median 41 %). Where the
        # authored LOD 1 is under DEGENERATE_LOD1_RATIO of LOD 0 that premise fails on its
        # face - Oracle_Park_by_Dmscopio ships 927 of 147,867 triangles, a smooth hull at the
        # kerb (Naomi's look, 2026-08-31) - so LOD 0 stays for those. Declared and counted per
        # shape; on the reference city at 5 % this keeps 7 shapes (~19 MiB), and the next
        # ratio above the cut-off is 6.5 %.
        ratio = len(meshes[0].tris) / float(len(base_mesh.tris))
        # ...and the same premise fails, less visibly, where LOD 1 is a MEASURABLY different
        # building. The ratio only sees BADLY reduced; it cannot see moderately reduced and
        # geometrically far, which is what "some meshes look awful up close" was (Naomi,
        # 2026-09-01). Measured over the 644 capped shapes, LOD 1's p95 distance from LOD 0 is
        # p50 0.005 m / p75 0.025 m / p90 0.072 m - so the cap is right for almost all of them -
        # and then `OracleParkSculpture` at 1.088 m (5,974 -> 784 triangles),
        # `Planetario_Galileo_Galilei` 0.892 m, `Alvear_Tower_Buenos_Aires` 0.423 m, `Prison01`
        # 0.316 m, every one of which the ratio guard passes. Refusing the drop past 0.25 m costs
        # 8 shapes and ~26 MiB, and those shapes keep a REFINED LOD 0 rather than the full one.
        far = float(options.get("authored_lod0_max_error") or 0.0)
        measured = chain_displacement
        if ratio < DEGENERATE_LOD1_RATIO:
            chain["lod0DropSkippedDegenerate"] = round(ratio, 4)
        elif far > 0.0 and measured is not None and float(measured) > far:
            chain["lod0DropSkippedDistance"] = round(float(measured), 4)
        else:
            chain["thresholds"] = px[1:]
            chain["lod0Dropped"] = True
            return True
    return False


def _drop_lod1(chain, meshes, kept_levels, px, options):
    """Apply `authored_lod1_drop` to `chain`."""
    # `authored_lod1_drop`: the game's LOD 1 is a median 41 % of LOD 0's triangles and, mounted
    # whole by BeamNG for every shape whether or not an instance is near, ~1 GB of the reference
    # level's 2.9 GB of object meshes — for a band between where LOD 0 stops and where LOD 2
    # would have started. Dropping it hands that band to LOD 0: LOD 0 runs to where the game's
    # LOD 1 ENDED, so nothing is ever coarser than the game shows at that distance, and the cost
    # is drawn triangles mid-range, not bytes. (The first cut handed the band to LOD 2 instead,
    # and at `lod_detail_scale 10` that switched a house at ten metres from the road — Naomi's
    # look, 2026-08-30.) Taken by the presets that exist for a card the level does not fit on
    # (5,071 MiB mounted on an 8 GB card read 7.8 GB dedicated and 8 fps wherever the camera
    # pointed). The collider still comes off LOD 1, and a chain with only one level keeps it.
    if options.get("authored_lod1_drop") and len(meshes) >= 2:
        chain["meshes"] = meshes[1:]
        chain["levels"] = kept_levels[1:]
        chain["thresholds"] = [px[1], *px[2:]]
        chain["lod1Dropped"] = True


def authored_ladder(base_mesh, chain, stats, refine=None):
    """The game's chain as the shape's ladder: LOD 0 then every authored level, each starting at
    the pixel size the game's own limit puts it.

    `refine` is `(mesh, level) -> mesh`, swapping in the bake's refined copy of that level. It
    matters as much here as on LOD 0: with `authored_lod0_max_tris` on, the level whose bytes are
    resident on a heavy shape is the game's own LOD 1.
    """
    meshes = list(chain["meshes"]) if chain.get("lod0Dropped") else [base_mesh, *chain["meshes"]]
    if refine is not None:
        levels = list(chain.get("levels") or ())
        if not chain.get("lod0Dropped"):
            levels = [0, *levels]
        meshes = [
            refine(mesh, levels[i]) if i < len(levels) else mesh for i, mesh in enumerate(meshes)
        ]
    thresholds = list(chain["thresholds"])
    # strictly decreasing, as Torque needs; the limits are monotonic so this only guards rounding
    for index in range(1, len(thresholds)):
        thresholds[index] = min(thresholds[index], thresholds[index - 1] - 1.0)
    thresholds = [max(1.0, px) for px in thresholds]
    fixed = fixed_ladder(thresholds, meshes)
    stats.update(
        {
            "authored": len(chain["meshes"]),
            "levels": len(meshes),
            "triangles": [len(m.tris) for m in meshes],
            "detailSizes": [px for px, _ms in fixed],
            "splitGeometries": sum(len(ms) for _px, ms in fixed) - len(fixed),
            "gameLodLimits": list(chain["limits"]),
            "lod1Dropped": bool(chain.get("lod1Dropped")),
            "lod0Dropped": bool(chain.get("lod0Dropped")),
            "lod0DropSkippedDistance": chain.get("lod0DropSkippedDistance"),
            "lod0Triangles": len(base_mesh.tris),
            "identicalLevels": list(chain.get("identicalLevels") or ()),
            "switches": [],
        }
    )
    if "lod0DropSkippedDegenerate" in chain:
        stats["lod0DropSkippedDegenerate"] = chain["lod0DropSkippedDegenerate"]
    return fixed, stats, meshes


def fixed_ladder(thresholds, meshes):
    """`[(px, [geometry, ...])]` — the ladder as Torque needs it: strictly decreasing, split.

    `split_mesh` caps every `<geometry>` at `MAX_GEOMETRY_VERTS`: BeamNG's per-mesh index buffers
    wrap past 65,535 and the shape shards in-game (load #10). A hospital reached 68,856.
    """
    fixed = []
    for raw, mesh in zip(thresholds, meshes):
        px = int(round(raw))
        # the ladder must strictly decrease or Torque reads two details as one
        if fixed and px >= fixed[-1][0]:
            px = fixed[-1][0] - 1
        if px < 2:
            continue
        fixed.append((px, daemesh.split_mesh(mesh)))
    return fixed
