"""Stage `networks` — the §5 segment meshes (roads, rails, bridges, tunnels, pipes...) merged
into world tiles, one `.dae` + `TSStatic` per tile with exact collision (`Visible Mesh Final`).

Why tiles: 7,289 segment meshes as 7,289 TSStatics is 3× vanilla's whole-level object count
before a single building; 256 m tiles are a few hundred objects and one draw per material per
tile. A tile whose vertex count passes `SPLIT_VERTS` is quartered by segment origin (dense
junction clusters) so no single shape is a memory spike, and within a shape no geometry holds
more than `daemesh.MAX_GEOMETRY_VERTS` vertices (`daemesh.split_mesh` — the load #10 junction shards
were the tiles that passed 65,535). Each tile's geometry is written
relative to the tile's origin (float32 precision stays sub-millimetre) and the TSStatic sits at
that origin.

Frame: segment glb (glTF, X-mirrored) → node matrix → undo mirror → + `pos` (bundle world) →
BeamNG `(x, z, y)`; that is `daemesh.glb_to_local` after adding the offset, so winding is kept.
Materials bind the level-prefixed canonical name (`beamng.materials.mapto_name`).

Region builds keep segments whose origin lies within the region (+`REACH` so a road entering
the region does not stop at its edge).

**One tile per worker (A5).** A tile reads its own segment meshes and writes `.dae` files no
other tile writes, so the loop runs on `jobs` processes; the parent keeps the scene tree, the
bound-material set and the ladder accounting. `imap` rather than `imap_unordered`, so the object
list does not depend on which worker finished first.
"""

import math
import os
import time
from collections import defaultdict

import numpy
from csiibundle import glb, lod, parallel, reader
from csiibundle import region as region_rule

from ..beamng import dae, daemesh, frame, tilemesh
from ..beamng import level as level_mod
from ..beamng.level import LevelPaths
from ..beamng.matbind import aliases_of
from . import Stage, level_of
from .networklod import merge_local, segment_min_lod, split_tile

STAGE_KEY = "networks"
TILE_DETAIL_PX = tilemesh.ALWAYS_PX  # what a tile nothing culls still ships (see beamng/tilelod)
REACH = 150.0
SPLIT_VERTS = 300_000
GROUP = ["Networks"]


def enabled(settings):
    return bool(settings.get("networks", True))


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("networkSegments") or 1)


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    tile = float(settings.get("tile_size") or 256)
    options = {
        "detail_scale": float(settings.get("lod_detail_scale", 6.0) or 6.0),
        "level_of_detail": lod.DEFAULT_LEVEL_OF_DETAIL,
        "view_distance": float(settings.get("view_distance") or 0)
        if settings.get("tile_lod", True)
        else None,
    }
    lay = reader.resolve_layers(ctx.bundle)
    if not os.path.isfile(lay.networks) or not os.path.isdir(lay.net_meshes):
        progress.log("networks: no network meshes in the bundle")
        level.write_objects()
        return {"segments": 0, "tiles": 0}
    records = [
        r
        for r in reader.read_jsonl(lay.networks)
        if r.get("kind") == "segment" and r.get("assetId") and r.get("pos")
    ]
    if ctx.region:
        # `csiibundle.region` holds the rule and the post-mortem of the day it was two rules.
        records = [
            r
            for r in records
            if region_rule.contains(r["pos"][0], r["pos"][2], ctx.region, reach=REACH)
        ]

    # -- bucket by tile of the segment origin (bundle frame x, z) -------------------------------
    buckets = defaultdict(list)
    for rec in records:
        tx = math.floor(rec["pos"][0] / tile)
        tz = math.floor(rec["pos"][2] / tile)
        buckets[(tx, tz)].append(rec)
    progress.log(f"networks: {len(records)} segments in {len(buckets)} tiles of {tile:.0f} m")

    progress.begin("networks: tiles", total=len(records), unit="segments")
    shapes_dir = ("art", "shapes", "networks")
    workers = ctx.jobs or 1
    aliases = aliases_of(ctx.shared, level)
    shared_used = (ctx.shared if ctx.shared is not None else {}).setdefault("materials_used", set())
    counts = {
        "written": 0,
        "tris": 0,
        "verts": 0,
        "refused": 0,
        "missing": 0,
        "split_geoms": 0,
        "rewritten": 0,
        "noShadow": 0,
    }
    names = set()
    ladder_stats = tilemesh.new_stats()
    # `aliases` goes through the pool initializer (`_init_worker`), not every tuple: a
    # multi-MB dict pickled once per chunk was the cost (validation pass 2026-08-31, D5)
    jobs = [
        (
            lay.net_meshes,
            recs,
            tx,
            tz,
            tile,
            level.dir,
            level.name,
            options["detail_scale"],
            options["view_distance"],
        )
        for (tx, tz), recs in sorted(buckets.items())
    ]

    def absorb(result):
        counts["refused"] += result["refused"]
        counts["missing"] += result.get("missing", 0)
        for shape in result["shapes"]:
            shared_used.update(shape["used"])
            tilemesh.account_compact(ladder_stats, shape["lod"])
            counts["split_geoms"] += shape["extraGeoms"]
            counts["rewritten"] += 1 if shape["changed"] else 0
            names.add(shape["name"])
            obj = level_mod.tsstatic(
                shape["name"],
                level.game_path(*shapes_dir, shape["name"] + ".dae"),
                (shape["ox"], shape["oy"], 0.0),
                frame.identity_matrix(),
                collision="Visible Mesh Final",
                canSave=True,
            )
            if not result.get("castShadows", True):
                obj["castShadows"] = False
                counts["noShadow"] += 1
            level.add(GROUP, obj, key="networks:" + shape["name"])
            counts["written"] += 1
            counts["tris"] += shape["triangles"]
            counts["verts"] += shape["vertices"]
        progress.advance(result["segments"])

    # `advance=False`: this stage's progress total is SEGMENTS, and `absorb` advances by however
    # many a tile carried. One per tile would make the bar mean nothing.
    for result in parallel.each(
        _tile_job,
        jobs,
        workers,
        progress,
        ordered=True,
        chunksize=2,
        min_parallel=4,
        spawn=True,
        advance=False,
        initializer=_init_worker,
        initargs=(aliases,),
    ):
        absorb(result)
    written, tris_total, verts_total = counts["written"], counts["tris"], counts["verts"]
    refused, split_geoms, rewritten = counts["refused"], counts["split_geoms"], counts["rewritten"]
    level.write_objects()
    # this stage owns art/shapes/networks/ (the folder is no longer cleared at build start)
    orphans = dae.sweep_orphans(level.path(*shapes_dir), names)
    progress.end(
        f"{written} tiles ({rewritten} rewritten), {tris_total:,} triangles, {verts_total:,} vertices "
        f"({refused} meshes refused, {orphans} orphans removed, "
        f"{split_geoms} extra geometries from the {daemesh.MAX_GEOMETRY_VERTS:,}-vertex cap)"
    )
    if counts["missing"]:
        progress.log(
            f"networks: {counts['missing']} segment record(s) have NO mesh file under "
            f"networks/ - those roads are absent from the level (an undeclared missing mesh)"
        )
    return {
        "segments": len(records),
        "tiles": written,
        "tilesRewritten": rewritten,
        "orphansRemoved": orphans,
        "triangles": tris_total,
        "vertices": verts_total,
        "refused": refused,
        "missingMesh": counts["missing"],
        # ground-form tiles out of the shadow pass; a tile with any elevated/tunnel segment keeps it
        "tilesNoShadow": counts["noShadow"],
        "splitGeometries": split_geoms,
        "jobs": workers,
        "lod": tilemesh.ladder_summary(ladder_stats, options["detail_scale"]),
        "approximations": {
            "compositionHeightRange": {
                "what": "a segment's composition height range is taken from the assembled "
                "mesh's own vertical extent, capped at its width",
                "why": "the bundle ships a segment's `width` but not its composition's "
                "`m_HeightRange`; the term is a third of the rendering size at most, "
                "and the cap keeps the error on the draw-FURTHER side",
                "count": len(records),
            },
            "compositionLodBias": {
                "what": "the mean of the pieces' `MeshData.m_LodBias` is taken as 0",
                "why": "the bundle ships assembled segment meshes, not the composition's "
                "piece list; a positive bias only ever pulls the distance IN, so 0 "
                "draws at least as far as the game",
                "count": len(records),
            },
        },
        "seconds": round(time.monotonic() - started, 1),
    }


_WORKER_ALIASES = {}


def _init_worker(aliases):
    """Pool initializer: the alias table every tile job binds materials against (D5)."""
    global _WORKER_ALIASES
    _WORKER_ALIASES = aliases


def _tile_job(args):
    """One tile of segments to its `.dae` file(s), in a worker process."""
    net_meshes, recs, tx, tz, tile, level_dir, level_name, detail_scale, view_distance = args
    aliases = _WORKER_ALIASES
    level = LevelPaths(level_dir, level_name)
    shared = {"materials_aliases": {"aliases": aliases}}
    parts = _load_parts(net_meshes, recs)
    # Whether this tile's roads can cast a shadow anyone sees: a bridge deck or a portal does,
    # flat carriageway at grade shadows only itself. Phase 10 declined shadows OFF for all
    # network tiles because bridges cast real ones; the refinement reads each segment's own
    # `form` - 6,707 of the reference city's 8,259 are `normal` - and keeps the pass for any
    # tile holding an `elevated` or `tunnel` segment (validation pass 2026-08-31, C5).
    casts = any((rec.get("attrs") or {}).get("form", "normal") != "normal" for rec in recs)
    out = {
        "refused": parts["refused"],
        "missing": parts["missing"],
        "segments": len(recs),
        "castShadows": casts,
        "shapes": [],
    }
    pieces = parts["pieces"]
    if not pieces:
        return out
    for sub_index, sub in enumerate(split_tile(pieces, SPLIT_VERTS)):
        ox, oy = (tx + 0.5) * tile, (tz + 0.5) * tile  # BeamNG x = bundle x, BeamNG y = bundle z
        merged = merge_local(sub, ox, oy, level, shared)
        name = f"net_{tx}_{tz}" + (f"_{sub_index}" if sub_index else "")
        mesh = daemesh.group_by_material(
            dae.Mesh(
                name,
                merged["verts"],
                merged["tris"],
                merged["runs"],
                normals=merged["normals"],
                uvs=merged["uvs"],
            )
        )
        # Same ladder as the derived tiles (`beamng/tilelod`): each level is the set of
        # segments CS2 still draws at that distance, carried in on the `#L<n>` marks the
        # merge kept on the material names.
        levels, ladder = tilemesh.build_levels(
            dae,
            name,
            mesh,
            detail_scale,
            lod.DEFAULT_LEVEL_OF_DETAIL,
            TILE_DETAIL_PX,
            view_distance=view_distance,
            targets=tilemesh.TILE_TARGETS if view_distance else None,
            tile=tile,
            # NO boundary pins: `boundary_pins` exists for products clipped at the tile edge
            # (a triangle belongs to the tile its centroid is in), where two neighbours share
            # vertices along the seam. A network tile is bucketed by SEGMENT ORIGIN and holds
            # every segment WHOLE, so its geometry reaches ~100 m past the edge and shares no
            # vertex with any neighbour - the 2 m band pinned a large share of every tile
            # (a pinned vertex is its own cluster) and the far level came out ~5x its target,
            # 3.8 M triangles against 0.8 M (validation pass 2026-08-31, C1).
            pins=False,
        )
        shape = dae.Shape(name, levels, collision=[], billboard_px=None, null_px=ladder["nullPx"])
        stats = dae.write_shape(level.path("art", "shapes", "networks", f"{name}.dae"), shape)
        out["shapes"].append(
            {
                "name": name,
                "ox": ox,
                "oy": oy,
                "triangles": stats["triangles"],
                "vertices": stats["vertices"],
                "changed": bool(stats.get("changed")),
                "lod": tilemesh.compact(levels, ladder),
                "extraGeoms": sum(len(ms) for _px, ms in levels) - len(levels),
            }
        )
    used = sorted(shared.get("materials_used") or ())
    for shape in out["shapes"]:
        shape["used"] = used
    return out


def _load_parts(net_meshes, recs):
    """`{"pieces": [(verts_bundle_world, normals_glb, uvs, tris, runs, origin_xz)], "refused": n}`
    with verts still in glTF axes but placed in bundle WORLD (mirror undone, `pos` added)."""
    pieces = []
    refused = 0
    missing = 0
    for rec in recs:
        path = os.path.join(net_meshes, rec["assetId"] + ".glb")
        if not os.path.isfile(path):
            # a segment record with no mesh file: the road is simply absent from the level, and
            # an undeclared missing mesh is a real error (`csiibundle.instances`), so it is
            # counted and logged rather than skipped in silence (B9)
            missing += 1
            continue
        nodes = glb.parse_glb(path)
        if nodes is None:
            refused += 1
            continue
        pos = numpy.asarray(rec["pos"], dtype=numpy.float64)
        min_lod = segment_min_lod(rec, nodes)
        for node in nodes:
            if len(node["tris"]) == 0:
                continue
            m = numpy.asarray(node["matrix"], dtype=numpy.float64)
            v = numpy.asarray(node["verts"], dtype=numpy.float64) @ m[:3, :3].T + m[:3, 3]
            n = node["normals"]
            if n is not None:
                n = numpy.asarray(n, dtype=numpy.float64) @ numpy.linalg.inv(m[:3, :3])
            # glTF axes with the mirror still applied: undoing the mirror is x -> -x, and the
            # bundle offset applies in bundle axes; keep everything in "mirrored glTF" form and
            # add the offset mirrored too, so daemesh.glb_to_local does the whole conversion once
            v[:, 0] -= pos[0]  # bundle +x is glTF -x
            v[:, 1] += pos[1]
            v[:, 2] += pos[2]
            pieces.append(
                (
                    v,
                    n,
                    node["uvs"],
                    node["tris"],
                    node["material_runs"],
                    (rec["pos"][0], rec["pos"][2]),
                    min_lod,
                )
            )
    return {"pieces": pieces, "refused": refused, "missing": missing}


INPUTS = ("layer:networks", "layer:networkMeshes", "derived:materials.json")
SETTINGS = (
    "networks",
    "tile_size",
    "region",
    "lod_detail_scale",
    "view_distance",
    "tile_lod",
)
OUTPUTS = ("art/shapes/networks",)

# version 13: a tile of flat carriageway (every segment `form: normal`) stops casting shadows;
# one with an elevated or tunnel segment keeps the pass - the Phase 10 decline, refined (C5)
# version 12: the tile ladder is decimated WITHOUT boundary pins - a network tile shares no
# vertex with its neighbours, and the pins held its far level at ~5x the target (C1)
# version 11: a segment record with no mesh file is counted (`missingMesh`) and logged (B9)
# version 10: a tile's `nulldetail` is kept UNDER its coarsest detail (`tilelod.tile_levels`);
# it sat `detail_scale` times above it wherever the game's own cull was inside the view
# distance, and Torque drew nothing there (validation pass 2026-08-31, A1)
STAGE = Stage(
    STAGE_KEY,
    "Roads and networks",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    shared_keys=("materials_used",),
    version=13,
)
