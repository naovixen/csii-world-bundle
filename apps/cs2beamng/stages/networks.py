"""Stage `networks` — the §5 segment meshes (roads, rails, bridges, tunnels, pipes...) merged
into world tiles, one `.dae` + `TSStatic` per tile with exact collision (`Visible Mesh Final`).

Why tiles: 7,289 segment meshes as 7,289 TSStatics is 3× vanilla's whole-level object count
before a single building; 256 m tiles are a few hundred objects and one draw per material per
tile. A tile whose vertex count passes `SPLIT_VERTS` is quartered by segment origin (dense
junction clusters) so no single shape is a memory spike, and within a shape no geometry holds
more than `dae.MAX_GEOMETRY_VERTS` vertices (`dae.split_mesh` — the load #10 junction shards
were the tiles that passed 65,535). Each tile's geometry is written
relative to the tile's origin (float32 precision stays sub-millimetre) and the TSStatic sits at
that origin.

Frame: segment glb (glTF, X-mirrored) → node matrix → undo mirror → + `pos` (bundle world) →
BeamNG `(x, z, y)`; that is `dae.glb_to_local` after adding the offset, so winding is kept.
Materials bind the level-prefixed canonical name (`materials.mapto_name`).

Region builds keep segments whose origin lies within the region (+`REACH` so a road entering
the region does not stop at its edge).

**One tile per worker (A5).** A tile reads its own segment meshes and writes `.dae` files no
other tile writes, so the loop runs on `jobs` processes; the parent keeps the scene tree, the
bound-material set and the ladder accounting. `imap` rather than `imap_unordered`, so the object
list does not depend on which worker finished first.
"""

import math
import multiprocessing
import os
import time
from collections import defaultdict

import numpy
from csiibundle import glb, lod, reader
from csiibundle.bake.tiles import MIN_LOD_MARK

from ..beamng import dae, frame, tilelod
from . import Stage, level_of
from .derived import LevelPaths
from .materials import aliases_of, mapto_name

STAGE_KEY = "networks"
TILE_DETAIL_PX = tilelod.ALWAYS_PX  # what a tile nothing culls still ships (see beamng/tilelod)
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
        # A SQUARE, like every other stage that reads a region (`beamng.placement.in_region`,
        # `stages/derived._tile_in_region`, `stages/water._intersects`). This was a circle until
        # 2026-08-20, so a region build kept a square of buildings and a disc of roads and the
        # corners of the area had houses and no streets. Nothing shrinks: a circle is inside its
        # square, so this only ever adds the segments that were missing. Found when the settings
        # screen's map picker had to draw the region and there were two shapes to draw.
        cx, cz = float(ctx.region["centerX"]), float(ctx.region["centerZ"])
        rr = float(ctx.region["radius"]) + REACH
        records = [
            r for r in records if abs(r["pos"][0] - cx) <= rr and abs(r["pos"][2] - cz) <= rr
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
    counts = {"written": 0, "tris": 0, "verts": 0, "refused": 0, "split_geoms": 0, "rewritten": 0}
    names = set()
    ladder_stats = tilelod.new_stats()
    jobs = [
        (
            lay.net_meshes,
            recs,
            tx,
            tz,
            tile,
            level.dir,
            level.name,
            aliases,
            options["detail_scale"],
            options["view_distance"],
        )
        for (tx, tz), recs in sorted(buckets.items())
    ]

    def absorb(result):
        counts["refused"] += result["refused"]
        for shape in result["shapes"]:
            shared_used.update(shape["used"])
            tilelod.account_compact(ladder_stats, shape["lod"])
            counts["split_geoms"] += shape["extraGeoms"]
            counts["rewritten"] += 1 if shape["changed"] else 0
            names.add(shape["name"])
            level.add(
                GROUP,
                {
                    "name": shape["name"],
                    "class": "TSStatic",
                    "shapeName": level.game_path(*shapes_dir, shape["name"] + ".dae"),
                    "position": [round(shape["ox"], 3), round(shape["oy"], 3), 0.0],
                    "rotationMatrix": frame.identity_matrix(),
                    "scale": [1, 1, 1],
                    "collisionType": "Visible Mesh Final",
                    "decalType": "None",
                    "useInstanceRenderData": True,
                    "canSave": True,
                },
                key="networks:" + shape["name"],
            )
            counts["written"] += 1
            counts["tris"] += shape["triangles"]
            counts["verts"] += shape["vertices"]
        progress.advance(result["segments"])

    if workers > 1 and len(jobs) > 4:
        mp = multiprocessing.get_context("spawn")
        with mp.Pool(min(workers, len(jobs))) as pool:
            for index, result in enumerate(pool.imap(_tile_job, jobs, chunksize=2)):
                absorb(result)
                if index % 25 == 0:
                    progress.check()
    else:
        for index, job in enumerate(jobs):
            absorb(_tile_job(job))
            if index % 25 == 0:
                progress.check()
    written, tris_total, verts_total = counts["written"], counts["tris"], counts["verts"]
    refused, split_geoms, rewritten = counts["refused"], counts["split_geoms"], counts["rewritten"]
    level.write_objects()
    # this stage owns art/shapes/networks/ (the folder is no longer cleared at build start)
    orphans = dae.sweep_orphans(level.path(*shapes_dir), names)
    progress.end(
        f"{written} tiles ({rewritten} rewritten), {tris_total:,} triangles, {verts_total:,} vertices "
        f"({refused} meshes refused, {orphans} orphans removed, "
        f"{split_geoms} extra geometries from the {dae.MAX_GEOMETRY_VERTS:,}-vertex cap)"
    )
    return {
        "segments": len(records),
        "tiles": written,
        "tilesRewritten": rewritten,
        "orphansRemoved": orphans,
        "triangles": tris_total,
        "vertices": verts_total,
        "refused": refused,
        "splitGeometries": split_geoms,
        "jobs": workers,
        "lod": tilelod.ladder_summary(ladder_stats, options["detail_scale"]),
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


def _tile_job(args):
    """One tile of segments to its `.dae` file(s), in a worker process."""
    net_meshes, recs, tx, tz, tile, level_dir, level_name, aliases, detail_scale, view_distance = (
        args
    )
    level = LevelPaths(level_dir, level_name)
    shared = {"materials_aliases": {"aliases": aliases}}
    parts = _load_parts(net_meshes, recs)
    out = {"refused": parts["refused"], "segments": len(recs), "shapes": []}
    pieces = parts["pieces"]
    if not pieces:
        return out
    for sub_index, sub in enumerate(_split(pieces, SPLIT_VERTS)):
        ox, oy = (tx + 0.5) * tile, (tz + 0.5) * tile  # BeamNG x = bundle x, BeamNG y = bundle z
        merged = _merge_local(sub, ox, oy, level, shared)
        name = f"net_{tx}_{tz}" + (f"_{sub_index}" if sub_index else "")
        mesh = dae.group_by_material(
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
        levels, ladder = tilelod.build_levels(
            dae,
            name,
            mesh,
            detail_scale,
            lod.DEFAULT_LEVEL_OF_DETAIL,
            TILE_DETAIL_PX,
            view_distance=view_distance,
            targets=tilelod.TILE_TARGETS if view_distance else None,
            tile=tile,
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
                "lod": tilelod.compact(levels, ladder),
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
    for rec in recs:
        path = os.path.join(net_meshes, rec["assetId"] + ".glb")
        if not os.path.isfile(path):
            continue
        nodes = glb.parse_glb(path)
        if nodes is None:
            refused += 1
            continue
        pos = numpy.asarray(rec["pos"], dtype=numpy.float64)
        min_lod = _segment_min_lod(rec, nodes)
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
            # add the offset mirrored too, so dae.glb_to_local does the whole conversion once
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
    return {"pieces": pieces, "refused": refused}


# -- the game's own draw-distance limit, per segment -------------------------------------------------


def _segment_min_lod(record, nodes):
    """`NetCompositionData.m_MinLod` for one segment — `NetCompositionHelpers.CalculateMinLod`.

    The game's input is the COMPOSITION's cross-section: `GetRenderingSize(float2(m_Width,
    Size(m_HeightRange)))` with the mean of the pieces' mesh biases. The bundle ships `width` on
    the segment record (§5) but not the height range and not the piece list, so:

    * width comes from the record, falling back to the mesh's smaller horizontal extent;
    * the height range is the assembled mesh's vertical extent CAPPED AT THE WIDTH — a bounded
      over-estimate, because a composition's cross-section is never taller than it is wide and a
      sloped bridge's mesh is. Over-estimating draws further, never shorter;
    * the bias is 0, which is the same direction: a positive bias only pulls the distance in.

    Both are declared in the stage's report. An edge is culled on the MINIMUM of its three
    compositions (`PreCullingSystem`), i.e. the most generous — which is what a per-segment
    number already is here, since we have one assembled mesh per segment rather than three.
    """
    width = (record.get("attrs") or {}).get("width")
    lo = hi = None
    for node in nodes or []:
        verts = numpy.asarray(node["verts"], dtype=numpy.float64)
        if len(verts) == 0:
            continue
        m = numpy.asarray(node["matrix"], dtype=numpy.float64)
        world = verts @ m[:3, :3].T + m[:3, 3]
        lo = world.min(axis=0) if lo is None else numpy.minimum(lo, world.min(axis=0))
        hi = world.max(axis=0) if hi is None else numpy.maximum(hi, world.max(axis=0))
    if lo is None:
        return None
    size = hi - lo
    if width is None:
        width = float(min(size[0], size[2]))
    width = float(width)
    if width <= 0.0:
        return None
    height_range = float(min(max(size[1], 0.0), width))
    return lod.composition_min_lod(width, height_range, 0.0)


def _split(pieces, limit):
    """Quarter a tile's pieces by their origin until every part is under `limit` vertices."""
    total = sum(len(p[0]) for p in pieces)
    if total <= limit or len(pieces) <= 1:
        return [pieces]
    xs = numpy.array([p[5][0] for p in pieces])
    zs = numpy.array([p[5][1] for p in pieces])
    mx, mz = numpy.median(xs), numpy.median(zs)
    quads = defaultdict(list)
    for p in pieces:
        quads[(p[5][0] >= mx, p[5][1] >= mz)].append(p)
    if len(quads) <= 1:
        return [pieces]
    out = []
    for q in quads.values():
        out.extend(_split(q, limit))
    return out


def _merge_local(pieces, ox, oy, level, shared):
    """Convert to BeamNG local (tile origin at (ox, oy)) and merge."""
    parts = []
    for v, n, uv, tris, runs, _origin, min_lod in pieces:
        local, nn = dae.glb_to_local(v, n)
        local[:, 0] -= ox
        local[:, 1] -= oy
        mark = "" if min_lod is None else f"{MIN_LOD_MARK}{int(min_lod)}"
        runs2 = [(mapto_name(shared, level, name) + mark, count) for name, count in runs]
        parts.append((local, nn, dae.glb_uvs(uv), None, tris, runs2))
    merged = dae.merge_meshes("tile", parts)
    return {
        "verts": merged.verts,
        "normals": merged.normals,
        "uvs": merged.uvs,
        "tris": merged.tris,
        "runs": merged.runs,
    }


INPUTS = ("layer:networks", "layer:networkMeshes", "derived:materials.json")
SETTINGS = ("networks", "tile_size", "region", "lod_detail_scale", "view_distance", "tile_lod")
OUTPUTS = ("art/shapes/networks",)

# version 5: `lod_detail_scale` no longer multiplies the LADDER, only the null threshold
# version 4: the decimator's UV bin scales with the mesh's own UV range
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
    version=5,
)
