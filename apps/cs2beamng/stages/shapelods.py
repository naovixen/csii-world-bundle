"""The LOD ladder: which generated level takes over at what distance, and where drawing stops.

The null threshold — where the shape stops being drawn at all — is CS2's own answer, scaled once
by `lod_detail_scale`. Everything else in the ladder has no game counterpart, because CS2 ships
no LOD chain for these prefabs at all (`lodCap 0`), so the switch points are ours and every one
of them is either measured or a declared budget.
"""

import os

from csiibundle import glb, lod, lodchain

from ..beamng import daemesh
from .shapeparts import collect_parts

# The ladder, as multiples of the shape's null threshold, coarsest first — vanilla BeamNG's
# spacing read in A2. It is no longer what DECIDES a switch point (the measured displacement is,
# below) but it is still the CEILING on one, and the last rung is still the floor.
#
# **The displacement rule may only ever DEFER a level, never advance it**, and that is the whole
# lesson of the 13:24 build. Allowing it both ways looked principled — a level whose surface is
# a centimetre from LOD 0's has "earned" a nearer place — and it put 2,586 of 2,903 prop shapes
# (335,072 placements) and 615 of 1,057 building shapes onto their decimated level NEARER than
# before, a prop by up to 509 m and a building by 1,821 m. Naomi's load came back "even more
# buildings rotations are off, warehouses rotated into the ocean, cranes rotated and floating,
# highrises all diagonal": a vertex-clustered LOD of a crane lattice or a balcony run is a
# handful of skewed triangles, and SURFACE DISTANCE DOES NOT SEE THAT — a 0.1 m error on a 0.2 m
# boom is geometrically tiny and visually total. The ratio ladder was implicitly protecting thin
# and small geometry by tying the switch to the shape's SIZE, and only the deferring half of the
# new rule is an improvement on it.
LADDER_RATIOS = (1.5, 6.0, 24.0)

# How many pixels of surface error a generated level is allowed to show at the moment it takes
# over (`lod.displacement_switch_px`). DECLARED TUNING and the one number in the ladder that is a
# BUDGET rather than a measurement: fidelity would ask for about a pixel, and a pixel is not
# affordable, because a CS2 residential block is 3,295 triangles of balconies, dormers and window
# reveals and taking three quarters of them away moves the surface metres whatever the decimator
# (A6.7 measured the quadric backend against the clusterer and got the same error).
#
# MEASURED over 1.16 M placements, from the two densest FOREST neighbourhoods and the two densest
# BUILDING ones, against the ladder it defers from (vegetation exempt, see `_lod_levels`):
#
#   4 px  +1 %, +0 % forest   +51 %, +67 % downtown   1,568 of 4,229 shapes deferred
#   6 px  +0 %, +0 %          +30 %, +39 %            1,191
#   8 px  +0 %, +0 %          +16 %, +21 %            1,004
#
# Holding a building at LOD 0 further out is bought in drawn triangles and nothing else, and it
# is bought downtown, where the card is already the constraint (A6.0 is open and the level mounts
# 4,675 MiB). 8 px defers every gross case — the worst offenders go 16x further out either way —
# and costs half of what 6 does, so it is what ships by default. `settings` exposes it, the
# presets run 4 to 20, and the report states the pixel error every level actually ships with.
DEFAULT_SWITCH_ERROR_PX = 8.0

# An imposter takes over where the mesh would have been culled and runs for this many times more
# distance before nothing is drawn. OURS, and the reason it is worth having: it buys back part
# of CS2's draw distance at billboard cost.
IMPOSTER_REACH = 3.0

# ...but only for a shape that is still being drawn out there. **BeamNG builds an imposter atlas
# at RUNTIME**, rendering the shape from every angle the first time it comes into view, and keeps
# it: `beamng.log` on the A5 load shows `Creating imposters for:` 2,652 times between 64 s and
# 903 s, on things like `NA_MailboxWall01`, `NA_ChimneyResidentialSmall01` and `ClothesDryer01`.
# That is the stall when you pan and the memory that climbs as you explore. A4/A5 gave one to
# EVERY object shape — 4,245 of 4,245 — against the shipped levels' 3.1 % (west_coast_usa, 82 of
# 2,608), 7.2 % (italy, 102) and 4.4 % (johnson_valley, 22).
#
# (Counting these needs the WHOLE file: a COLLADA scene is the last section, so a grep over the
# first 200 KB reports only the small shapes and undercounts. That mistake read 4,245 as 2,747.)
#
# The gate is the mechanism's own question: a billboard replaces the MESH beyond the mesh's null
# distance, so it is worth an atlas exactly when the mesh is still drawn near the edge of the view
# distance. Three quarters of `view_distance` puts 153 of our 4,245 object shapes over the line —
# 3.6 %, inside the vanilla band — and it scales with the setting instead of being a constant that
# means something different at 2 km and at 8 km. OURS: CS2 has no imposter concept at all.
IMPOSTER_VIEW_FRACTION = 0.75


# Vegetation keeps the ladder, and the deferral rule does not apply to it. MEASURED: it is 262 of
# 268 tree shapes over 1,346,600 placements, +24 % drawn triangles in a forest for a change nobody
# asked for — and the reason to decline it is not only the cost. The rule's input is a SURFACE
# DISTANCE, and a canopy is a cloud of alpha-tested cards: moving its leaves 0.3 m measures the
# same as moving a wall 0.3 m and reads as nothing at all, where the wall reads as a fault. The
# metric does not know the difference, so vegetation is left where A6.1 and the ladder had it.
# OURS, declared, and reversible the moment someone measures foliage error properly.
DISPLACEMENT_RULE_KINDS = ("building", "prop", "decal")

# A8/P2. Metres of measured surface error at which LOD 1 REPLACES LOD 0 rather than following it.
# 0 disables. LOD 0 is ~66 % of a shape's bytes and a shape is mounted whole for one instance in
# view, so where the decimator's own p95 displacement says LOD 1 is nowhere further than this from
# the surface it replaces, those bytes buy nothing on screen. Measured on the reference city:
# 0.05 m reaches 2,947 shapes for 282 MiB, **0.10 m reaches 3,375 for 387 MiB**, 0.50 m reaches
# 3,683 for 585 MiB. 0.10 is four times tighter than the point where the error starts landing on
# landmarks (`InternationalAirport01` is 0.42 m), and the figure is a p95, so 5 % of a promoted
# surface is further out than it.
LOD0_MAX_ERROR_DEFAULT = 0.10

# ...and the shape is kept whole when the promotion would leave it SHIPPING fewer triangles than
# this, whatever its displacement says.
#
# A8/P2's promotion exists to save BYTES, and a shape with no triangles has no bytes to save. The
# same decimation ratio means two different things at the two ends of the range: `Five_Luxe_JBR_`
# goes 243,371 -> 59,389 triangles (24 % kept) and is still a tower; `NA_ParkingSign01` goes
# **22 -> 4** (18 % kept) and 4 triangles is one quad — the sign's panel, with its post gone, which
# is what "the sign floats and the post is not rendered, even up close" was.
#
# A p95 SURFACE distance cannot see that. Flattening a 5 cm post moves its surface 3 mm, so
# `NA_ParkingSign01` measured 0.0032 m against a 0.10 m threshold and was promoted with room to
# spare — the exact failure `lod_switch_error_px`'s own note warns about ("a surface distance is
# not a visual error for thin geometry"), reached by the other lever.
#
# THE COUNT THAT MATTERS IS THE ONE THAT SHIPS, not LOD 0's. The ratio says nothing: `Five_Luxe_JBR_`
# keeps 24 % of its triangles and `NA_CommercialLow01_L5_4x3` keeps 25 %, and one is still a tower
# while the other is a facade with 381 triangles — not enough to keep its window openings closed,
# which is "you can see into the building". 59,389 triangles is plenty; 381 is not, and 4 is a
# sign's panel with no post.
#
# MEASURED over the reference city's 3,338 promoted shapes: a floor of 1,000 SHIPPED triangles
# protects 2,941 of them for **26 % of the triangles the promotion saved**, and every one of the
# heavy shapes it exists for — Five_Luxe_JBR_ 243,371 -> 59,389, ParkingHall01 231,552 -> 58,210 —
# is still promoted. A floor on LOD 0's count instead let this building through at 1,514.
LOD0_MIN_TRIANGLES = 1000


def detail_scale(kind, options):
    """How many times closer than CS2 this level culls a shape of `kind`.

    One global multiplier over two things whose own distances differ by 12.7x is the A6.2 defect:
    it preserves the ratio and destroys the relationship. `lod_detail_scale_prop` gives props
    their own rate; 0 keeps the single global number, so this is inert unless asked for.

    THE GAME'S OWN RULE IS NOT THIS, and is worth stating where the deviation lives.
    `PreCullingSystem.RelativeCullingJob` culls a sub-object at the distance to its OWNER's bounds
    while keeping the sub-object's own `m_MinLod` — so CS2 buys a facade prop its building's own
    extent, tens of metres, not an order of magnitude (audit §70). That half is NOT ported, and
    the reason is structural rather than a shortcut: BeamNG carries detail thresholds in the
    SHAPE file, one per prefab, and ownership is per INSTANCE — the same prop stands both
    free-standing and bolted to a tower — so there is no per-instance threshold to put it in.
    """
    if kind == "prop" and options.get("detail_scale_prop"):
        return float(options["detail_scale_prop"])
    return float(options["detail_scale"])


def authored_chain(shape_name, authored, size, area, lod_bias, radius, detail_scale, options):
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
    meshes, sized = [], []
    for level, parts, level_size in authored:
        mesh = daemesh.group_by_material(daemesh.merge_meshes(f"{shape_name}_LOD{level}", parts))
        if len(mesh.tris) == 0:
            continue
        meshes.append(mesh)
        sized.append((level_size, len(mesh.tris) * 3))
    if not meshes:
        return None
    bias = lod.lod_bias(size, area, lod_bias)
    limits = lodchain.authored_lod_limits(
        size, sized, bias, min_lod=lod.mesh_min_lod(size, area, lod_bias)
    )
    px = [
        max(1.0, lodchain.limit_px(radius, limit, options["level_of_detail"]) * detail_scale)
        for limit in limits
    ]
    # A Torque detail's size is where that detail STARTS: LOD 0 at the size its own limit
    # stops it, level j at the size limit j stops it, and the null a hair under the coarsest
    # (the game draws the coarsest level right up to its cull; Torque wants the two sizes
    # distinct, so the last 10 % before the cull is a rounding, not a rule).
    chain = {
        "meshes": meshes,
        "thresholds": px,
        "nullPx": px[-1] * 0.9,
        "limits": limits,
        "colliderMesh": meshes[0],
        "lod1Dropped": False,
    }
    # `authored_lod1_drop`: the game's LOD 1 is a median 41 % of LOD 0's triangles and, mounted
    # whole by BeamNG for every shape whether or not an instance is near, ~1 GB of the reference
    # level's 2.9 GB of object meshes — for a band between where LOD 0 stops and where LOD 2
    # would have started. Dropping it hands that band to LOD 2 (4.5 %): a mid-range fidelity
    # trade, declared and counted, taken by the presets that exist for a card the level does
    # not fit on (2026-08-30: 5,071 MiB mounted on an 8 GB card read 7.8 GB dedicated and 8 fps
    # wherever the camera pointed). The collider still comes off LOD 1 — contact is not a
    # residency question — and a chain with only one level keeps it.
    if options.get("authored_lod1_drop") and len(meshes) >= 2:
        chain["meshes"] = meshes[1:]
        chain["thresholds"] = [px[0], *px[2:]]
        chain["lod1Dropped"] = True
    return chain


def lod_levels(
    derived_dir,
    source,
    shape_name,
    base_mesh,
    null_px,
    radius,
    displacement,
    kind,
    aliases,
    tinted,
    level_name,
    options,
    chain=None,
):
    """`[(px, [Mesh]), ...]` nearest-first: LOD 0 plus whatever the bake generated, each with the
    threshold its own measured displacement earns it.

    A detail's size in a Torque shape is the projected pixel size of the bounding-sphere radius
    at which that detail STARTS being used, so the size written on level *n* is where level *n+1*
    takes over — and that is the moment level *n+1*'s error appears on screen. `radius` and
    `displacement` are both in the mesh's own metres (the bundle's node matrices are rigid, so a
    node-local metre is a world metre), which is what makes the ratio between them a pure pixel
    count.
    """
    meshes = [base_mesh]
    generated = []
    stats = {"levels": 1, "triangles": [len(base_mesh.tris)], "lod0Promoted": False}
    if chain is not None:
        return _authored_ladder(base_mesh, chain, stats)
    if options["generate_lods"]:
        _load_generated(
            meshes, generated, stats, derived_dir, source, shape_name, aliases, tinted, level_name
        )

    meshes, generated = _promote_lod0(meshes, generated, stats, kind, displacement, options)

    thresholds, shipped = _switch_thresholds(
        generated, meshes, null_px, radius, displacement, kind, options
    )
    fixed = _fixed_ladder(thresholds, meshes)
    stats["detailSizes"] = [px for px, _ms in fixed]
    stats["splitGeometries"] = sum(len(ms) for _px, ms in fixed) - len(fixed)
    # what each generated level actually ships with: the threshold above it (where it takes over)
    # and the pixels its displacement covers there — the approximation, counted
    for index, entry in enumerate(shipped):
        entry["px"] = stats["detailSizes"][index] if index < len(stats["detailSizes"]) else None
        entry["errorPx"] = (
            round(lod.displacement_error_px(radius, entry["metres"], entry["px"]), 2)
            if entry["metres"] is not None and entry["px"]
            else None
        )
    stats["switches"] = shipped
    return fixed, stats, meshes


def _authored_ladder(base_mesh, chain, stats):
    """The game's chain as the shape's ladder: LOD 0 then every authored level, each starting at
    the pixel size the game's own limit puts it."""
    meshes = [base_mesh, *chain["meshes"]]
    thresholds = list(chain["thresholds"])
    # strictly decreasing, as Torque needs; the limits are monotonic so this only guards rounding
    for index in range(1, len(thresholds)):
        thresholds[index] = min(thresholds[index], thresholds[index - 1] - 1.0)
    thresholds = [max(1.0, px) for px in thresholds]
    fixed = _fixed_ladder(thresholds, meshes)
    stats.update(
        {
            "authored": len(chain["meshes"]),
            "levels": len(meshes),
            "triangles": [len(m.tris) for m in meshes],
            "detailSizes": [px for px, _ms in fixed],
            "splitGeometries": sum(len(ms) for _px, ms in fixed) - len(fixed),
            "gameLodLimits": list(chain["limits"]),
            "lod1Dropped": bool(chain.get("lod1Dropped")),
            "switches": [],
        }
    )
    return fixed, stats, meshes


def _load_generated(
    meshes, generated, stats, derived_dir, source, shape_name, aliases, tinted, level_name
):
    """Append the bake's LOD1/LOD2 meshes, skipping any it did not write or that came out empty."""
    for level in (1, 2):
        path = os.path.join(derived_dir, "lods", f"{source}_LOD{level}.glb")
        if not os.path.isfile(path):
            continue
        nodes = glb.parse_glb(path)
        if not nodes:
            continue
        parts, _lo, _hi, _area, _used = collect_parts(nodes, aliases, tinted, level_name)
        if not parts:
            continue
        mesh = daemesh.group_by_material(daemesh.merge_meshes(f"{shape_name}_LOD{level}", parts))
        if len(mesh.tris) == 0:
            continue
        meshes.append(mesh)
        generated.append(level)
        stats["levels"] += 1
        stats["triangles"].append(len(mesh.tris))


# A8/P2 — DROP LOD 0 where LOD 1 is measurably indistinguishable from it.
#
# A shape is mounted WHOLE for one instance in view, and LOD 0 is ~66 % of its bytes
# (measured over 200 shapes and again over the 60 heaviest). Where the decimator's own
# recorded displacement says LOD 1's surface is nowhere meaningfully away from LOD 0's, those
# bytes buy nothing: the ladder simply starts one rung coarser, LOD 2 becomes the only
# generated level, and every switch threshold below is computed exactly as before.
#
# `displacement[0]` is the p95 distance from LOD 1's surface back to LOD 0's, in the mesh's
# own metres. On the reference city at 0.10 m this reaches 3,375 of 4,165 shapes and saves
# ~387 MiB; `Five_Luxe_JBR_` goes 243,371 -> 59,389 triangles at 8 cm of error.
#
# `exempt` kinds keep LOD 0 unconditionally, for the same reason they are exempt from the
# displacement switch rule: a 24-triangle lamp-post LOD measures 0.06 m out and is visually
# total, so a distance is not a fair test of it (A6.7).
def _promote_lod0(meshes, generated, stats, kind, displacement, options):
    promote = float(options.get("lod0_max_error") or 0.0)
    keep_small = int(options.get("lod0_min_triangles") or 0)
    indistinguishable = (
        promote > 0.0
        and generated
        and generated[0] == 1
        and len(meshes) > 1
        and kind in DISPLACEMENT_RULE_KINDS
        and displacement
        and displacement[0] is not None
        and float(displacement[0]) <= promote
    )
    if not indistinguishable:
        return meshes, generated
    if len(meshes[1].tris) < keep_small:
        stats["lod0KeptSmall"] = len(meshes[1].tris)  # too few triangles to survive the drop
        return meshes, generated
    stats["lod0Promoted"] = True
    stats["lod0Triangles"] = len(meshes[0].tris)
    stats["levels"] -= 1
    stats["triangles"] = stats["triangles"][1:]
    # LOD 1 is now the shape's own top detail, so only LOD 2 is still a GENERATED level
    return meshes[1:], generated[1:]


def _switch_thresholds(generated, meshes, null_px, radius, displacement, kind, options):
    """`(thresholds, shipped)` — where each level takes over, nearest first.

    One threshold per mesh: each generated level's switch point, and the floor for the coarsest
    mesh, which has to hold the last band before `nulldetail`. The ladder's own value for a rung
    is the CEILING — the measured error may push a level further out, never nearer than the
    spacing that has always been there.
    """
    floor = max(2.0, null_px * LADDER_RATIOS[0])
    thresholds = []
    shipped = []
    exempt = kind not in DISPLACEMENT_RULE_KINDS
    for index, level in enumerate(generated):
        metres = None
        if displacement and len(displacement) >= level and not exempt:
            metres = displacement[level - 1]
        ceiling = max(
            2.0, null_px * LADDER_RATIOS[min(len(meshes) - 1 - index, len(LADDER_RATIOS) - 1)]
        )
        # `metres` is None for an exempt kind and for a level the bake did not measure; the
        # switch helper answers with its ceiling, which the clamp turns back into the ladder
        earned = lod.displacement_switch_px(radius, metres, options["switch_error_px"])
        px = max(floor, min(ceiling, earned))
        thresholds.append(px)
        shipped.append(
            {
                "level": level,
                "metres": None if metres is None else round(metres, 4),
                "ladderPx": int(round(ceiling)),
                "exempt": exempt,
                "deferred": bool(px < ceiling - 0.5),
            }
        )
    thresholds.append(floor)
    return thresholds, shipped


def _fixed_ladder(thresholds, meshes):
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
