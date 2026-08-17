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
"""

import math
import os
import time
from collections import defaultdict

import numpy

from csiibundle import glb, reader

from ..beamng import dae, frame
from . import Stage, level_of
from .materials import mapto_name

STAGE_KEY = "networks"
TILE_DETAIL_PX = 2      # detail sizes must be > 0 (BeamNG: "invalid detail levels"); 2 px = always drawn
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
    lay = reader.resolve_layers(ctx.bundle)
    if not os.path.isfile(lay.networks) or not os.path.isdir(lay.net_meshes):
        progress.log("networks: no network meshes in the bundle")
        level.write_objects()
        return {"segments": 0, "tiles": 0}
    records = [r for r in reader.read_jsonl(lay.networks) if r.get("kind") == "segment" and r.get("assetId") and r.get("pos")]
    if ctx.region:
        cx, cz, rr = float(ctx.region["centerX"]), float(ctx.region["centerZ"]), float(ctx.region["radius"]) + REACH
        records = [r for r in records if (r["pos"][0] - cx) ** 2 + (r["pos"][2] - cz) ** 2 <= rr * rr]

    # -- bucket by tile of the segment origin (bundle frame x, z) -------------------------------
    buckets = defaultdict(list)
    for rec in records:
        tx = math.floor(rec["pos"][0] / tile)
        tz = math.floor(rec["pos"][2] / tile)
        buckets[(tx, tz)].append(rec)
    progress.log(f"networks: {len(records)} segments in {len(buckets)} tiles of {tile:.0f} m")

    progress.begin("networks: tiles", total=len(records), unit="segments")
    shapes_dir = ("art", "shapes", "networks")
    written = 0
    tris_total = 0
    verts_total = 0
    refused = 0
    split_geoms = 0
    for (tx, tz), recs in sorted(buckets.items()):
        parts = _load_parts(ctx, lay, recs, tris_total)
        refused += parts.pop("refused")
        pieces = parts["pieces"]
        if not pieces:
            progress.advance(len(recs))
            continue
        for sub_index, sub in enumerate(_split(pieces, SPLIT_VERTS)):
            origin_bundle = ((tx + 0.5) * tile, (tz + 0.5) * tile)   # tile centre, bundle x/z
            ox, oy = origin_bundle[0], origin_bundle[1]              # BeamNG x = bundle x, BeamNG y = bundle z
            merged = _merge_local(sub, ox, oy, level, ctx)
            name = f"net_{tx}_{tz}" + (f"_{sub_index}" if sub_index else "")
            mesh = dae.group_by_material(dae.Mesh(name, merged["verts"], merged["tris"], merged["runs"],
                                                  normals=merged["normals"], uvs=merged["uvs"]))
            meshes = dae.split_mesh(mesh)                # <= MAX_GEOMETRY_VERTS per geometry (load #10 shards)
            split_geoms += len(meshes) - 1
            shape = dae.Shape(name, [(TILE_DETAIL_PX, meshes)], collision=[], billboard_px=None, null_px=None)
            path = level.path(*shapes_dir, f"{name}.dae")
            stats = dae.write_shape(path, shape)
            level.add(GROUP, {
                "name": name, "class": "TSStatic", "shapeName": level.game_path(*shapes_dir, f"{name}.dae"),
                "position": [round(ox, 3), round(oy, 3), 0.0], "rotationMatrix": frame.identity_matrix(),
                "scale": [1, 1, 1], "collisionType": "Visible Mesh Final", "decalType": "None",
                "useInstanceRenderData": True, "canSave": True,
            }, key=f"networks:{name}")
            written += 1
            tris_total += stats["triangles"]
            verts_total += stats["vertices"]
        progress.advance(len(recs))
        progress.check()
    level.write_objects()
    progress.end(f"{written} tiles, {tris_total:,} triangles, {verts_total:,} vertices ({refused} meshes refused, "
                 f"{split_geoms} extra geometries from the {dae.MAX_GEOMETRY_VERTS:,}-vertex cap)")
    return {"segments": len(records), "tiles": written, "triangles": tris_total, "vertices": verts_total,
            "refused": refused, "splitGeometries": split_geoms, "seconds": round(time.monotonic() - started, 1)}


def _load_parts(ctx, lay, recs, _acc):
    """`{"pieces": [(verts_bundle_world, normals_glb, uvs, tris, runs, origin_xz)], "refused": n}`
    with verts still in glTF axes but placed in bundle WORLD (mirror undone, `pos` added)."""
    pieces = []
    refused = 0
    for rec in recs:
        path = os.path.join(lay.net_meshes, rec["assetId"] + ".glb")
        if not os.path.isfile(path):
            continue
        nodes = glb.parse_glb(path)
        if nodes is None:
            refused += 1
            continue
        pos = numpy.asarray(rec["pos"], dtype=numpy.float64)
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
            v[:, 0] -= pos[0]                        # bundle +x is glTF -x
            v[:, 1] += pos[1]
            v[:, 2] += pos[2]
            pieces.append((v, n, node["uvs"], node["tris"], node["material_runs"], (rec["pos"][0], rec["pos"][2])))
    return {"pieces": pieces, "refused": refused}


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


def _merge_local(pieces, ox, oy, level, ctx):
    """Convert to BeamNG local (tile origin at (ox, oy)) and merge."""
    parts = []
    for v, n, uv, tris, runs, _origin in pieces:
        local, nn = dae.glb_to_local(v, n)
        local[:, 0] -= ox
        local[:, 1] -= oy
        runs2 = [(mapto_name(ctx.shared, level, name), count) for name, count in runs]
        parts.append((local, nn, dae.glb_uvs(uv), None, tris, runs2))
    merged = dae.merge_meshes("tile", parts)
    return {"verts": merged.verts, "normals": merged.normals, "uvs": merged.uvs, "tris": merged.tris, "runs": merged.runs}


STAGE = Stage(STAGE_KEY, "Roads and networks", enabled, units, run)
