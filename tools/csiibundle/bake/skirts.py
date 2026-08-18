"""Bake product `skirts` — every building's `__base` foundation band, placed and dropped to the
ground, tiled.

§4.1: the producer ships `<assetId>__base.glb` — a 1 m band around the building's silhouette
at its base level — and the CONSUMER "moves every `v = 0` vertex down to the terrain surface at
its world X/Z (never up above the top ring)". Nobody had done that half (the Blender adapter
placed the band as authored). Here it is done once: for every building instance that
`instances.wants_skirt_with_parents` accepts (an extension reads its parent's flags, §4.1) and
whose prefab has a skirt file, the band is placed by `frames.instance_matrix`, its bottom ring
found by geometry (`instances.skirt_rings`), dropped by `instances.drop_skirt` onto the ground
(`ground.GroundSurface`, MIN-envelope so the band never stops short of a triangulation), and
written to `derived/skirts/skirts_<tx>_<tz>.glb` with the skirt's own material.
"""

import os
import time

import numpy

from .. import frames, glb, instances, reader
from . import Product, register
from .ground import GroundSurface
from .markings import _in_region, _material_specs, _texture_uri_base
from .tiles import TileSink

PRODUCT_NAME = "skirts"
INPUTS = ("instances", "meshes", "terrain", "terrainFine")


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    region = options.get("region")
    tile = float(options.get("tile", 256.0))
    out_dir = os.path.join(derived, PRODUCT_NAME)
    started = time.monotonic()
    lay = reader.resolve_layers(bundle)
    manifest = context.manifest or {}
    total = int(((manifest.get("counts") or {}).get("instances")) or 0) or None

    ground = GroundSurface(bundle, options.get("diagonal", "envelope"), mode="min", prefer_fine=options.get("ground", "fine") == "fine")
    if not ground.present:
        progress.log("skirts: no terrain layer — bands stay as authored (1 m plinths)")

    # pass 1: parent flags
    # the stream is prefiltered to buildings, so the manifest's instance count is not the total
    progress.begin("skirts: parents", total=None, unit="buildings")
    parents = {}
    seen = 0
    for record in instances.read_instances(bundle, kind="building"):
        seen += 1
        if seen % 5000 == 0:
            progress.advance(5000)
            progress.check()
        if record.get("kind") == "building" and "id" in record:
            parents[record["id"]] = (record.get("attrs") or {}).get("elevationFlags")
    progress.end(f"{len(parents)} building records indexed")

    # pass 2: place + drop
    progress.begin("skirts: place", total=None, unit="buildings")
    specs = _material_specs(derived, bundle)
    sink = TileSink(tile, PRODUCT_NAME, out_dir, specs)
    skirt_cache = {}
    placed = no_file = not_ground = 0
    verts_dropped = 0
    seen = 0
    for record in instances.read_instances(bundle, kind="building"):
        seen += 1
        if seen % 5000 == 0:
            progress.advance(5000)
            progress.check()
        if record.get("kind") != "building":
            continue
        pos = record.get("pos") or (0, 0, 0)
        if not _in_region(pos[0], pos[2], region):
            continue
        if not instances.wants_skirt_with_parents(record, parents):
            not_ground += 1
            continue
        asset_id = record["assetId"] + instances.SKIRT_SUFFIX
        if asset_id not in skirt_cache:
            path = os.path.join(lay.meshes, asset_id + ".glb")
            nodes = glb.parse_glb(path) if os.path.isfile(path) else None
            prepared = []
            for node in nodes or []:
                v = numpy.asarray(node["verts"], dtype=numpy.float64)
                m = numpy.asarray(node["matrix"], dtype=numpy.float64)
                v = v @ m[:3, :3].T + m[:3, 3]
                prepared.append((v, numpy.asarray(node["tris"], dtype=numpy.int64),
                                 None if node.get("uvs") is None else numpy.asarray(node["uvs"], dtype=numpy.float64),
                                 node["material_runs"]))
            skirt_cache[asset_id] = prepared
        prepared = skirt_cache[asset_id]
        if not prepared:
            no_file += 1
            continue
        matrix = frames.instance_matrix(record)          # bundle world; includes the §2.2 mirror
        for v_local, tris, uvs, runs in prepared:
            world = frames.transform_points(matrix, v_local)
            bottom, _top = instances.skirt_rings(world, up_axis=1)
            if ground.present and bottom.any():
                # ground at each bottom vertex's own (x, z); Z-up query (x, y_north)
                g = ground.height_many(world[bottom, 0], world[bottom, 2])
                heights = numpy.full(len(world), numpy.nan)
                heights[bottom] = g
                world = instances.drop_skirt(world, bottom, heights, up_axis=1)
                verts_dropped += int((~numpy.isnan(g)).sum())
            # winding: `instance_matrix` carries the §2.2 mirror (det < 0), which flips the
            # orientation of raw arrays; the stored .glb order is the producer's already-reversed
            # one, so the bundle-frame face order is its reverse (same rule as `markings`)
            cursor = 0
            for name, count in runs:
                t = tris[cursor:cursor + count][:, [0, 2, 1]]
                cursor += count
                sink.add(world, t, uvs, name)
        placed += 1
    progress.end(f"{placed} skirts placed ({no_file} prefabs without a skirt file, {not_ground} not on the ground), "
                 f"{verts_dropped} bottom vertices dropped to the ground")
    tiles = sink.tiles()
    progress.begin("skirts: tiles", total=len(tiles), unit="tiles")
    written = sink.flush(progress, texture_uri_base=_texture_uri_base(out_dir, bundle),
                        jobs=max(1, int(options.get("jobs") or 1)))
    progress.end(f"{written['files']} tiles, {written['tris']:,} triangles")
    return {"placed": placed, "noSkirtFile": no_file, "notOnGround": not_ground, "verticesDropped": verts_dropped,
            "tiles": written["files"], "tris": written["tris"], "_tiles": written["tiles"],
            "seconds": round(time.monotonic() - started, 1)}


register(Product(PRODUCT_NAME, INPUTS, "skirts/", run, options=("ground", "region")))