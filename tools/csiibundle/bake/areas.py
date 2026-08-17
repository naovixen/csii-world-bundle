"""Bake product `areas` — §8b painted surfaces conformed to the terrain, world-planar UVs, tiled.

`areas.build_pieces` is the whole algorithm (adaptive triangle split until each piece follows
the ground, `rendererPriority` lift order, region filter); this product feeds it the ground
(`ground.GroundSurface`, max-envelope so paint never sinks), converts its Z-up output back to
the bundle frame (keeping its face order — see the winding note in `run`), gives every vertex the prefab's world-planar UV (`uv = world_xy × uvScale`,
the game's own `colossal_UVScale`), and writes `derived/areas/areas_<tx>_<tz>.glb` with one
primitive per prefab (material name `area:<prefab>`). The material description each prefab
needs — texture, colour, normal/mask maps, metallic/roughness, uvScale, unresolved flag — is
written beside the tiles as `derived/areas/materials.json` from `areas.surface_material`.
"""

import json
import os
import time

import numpy

from .. import areas as area_rules
from .. import reader
from . import Product, register
from .ground import GroundSurface
from .tiles import TileSink

PRODUCT_NAME = "areas"
INPUTS = ("areas", "areaPrefabs", "terrain", "terrainFine")


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    region = options.get("region")
    tile = float(options.get("tile", 256.0))
    out_dir = os.path.join(derived, PRODUCT_NAME)
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()

    catalogue = reader.load_area_prefabs(bundle) or {}
    areas_path = reader.layer_path(bundle, "areas")
    manifest = context.manifest or {}
    total = int(((manifest.get("counts") or {}).get("areas")) or 0) or None
    ground = GroundSurface(bundle, options.get("diagonal", "envelope"), mode="max", prefer_fine=options.get("ground", "fine") == "fine")
    if not ground.present:
        progress.log("areas: no terrain layer — areas stay at their authored heights (flat)")
        sample = lambda x, y: None  # noqa: E731
        cell = 3.5
    else:
        sample = area_rules.memoised_sampler(ground)
        cell = float(ground.cell)

    progress.begin("areas: conform", total=total, unit="areas")
    center = radius = None
    if region:
        center = (float(region["centerX"]), float(region["centerZ"]))
        radius = float(region["radius"])
    records = reader.read_jsonl(areas_path) if os.path.isfile(areas_path) else []
    last = [0]

    def tick(placed):
        if placed - last[0] >= 200:
            progress.advance(placed - last[0])
            last[0] = placed
            progress.check()

    pieces, stats = area_rules.build_pieces(records, cell, sample, center=center, radius=radius,
                                            progress=tick, catalogue=catalogue)
    progress.end(f"{stats['placed']} areas -> {sum(len(p['faces']) for p in pieces.values()):,} pieces "
                 f"({stats['skipped_region']} outside region, {stats['missed']} node lookups missed, "
                 f"worst unconformed {stats['counters']['worst']:.3f} m)")

    # tiles: Z-up pieces back to the bundle frame (x, z_up, y_north), winding restored
    sink = TileSink(tile, PRODUCT_NAME, out_dir)
    materials = {}
    for prefab, piece in pieces.items():
        if not piece["faces"]:
            continue
        v_zup = numpy.asarray(piece["verts"], dtype=numpy.float64)
        faces = numpy.asarray(piece["faces"], dtype=numpy.int64)
        verts = v_zup[:, [0, 2, 1]]                        # bundle frame (x, y_up, z_north)
        # winding: build_pieces reversed the source order for Z-up; the axis swap back to the
        # bundle frame reverses orientation again, so the Z-up face order IS the bundle-frame
        # up-facing order — measured (bundle-frame normals +Y on the reference city), not argued
        entry = catalogue.get(prefab) or {}
        mat = area_rules.surface_material(entry)
        uv_scale = float(mat["uv_scale"] or area_rules.UV_SCALE_DEFAULT)
        uvs = numpy.stack([v_zup[:, 0] * uv_scale, v_zup[:, 1] * uv_scale], axis=1)   # world-planar (x, y_north)
        name = f"area:{prefab}"
        sink.add(verts, faces, uvs, name)
        materials[name] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in mat.items()}
        materials[name]["prefab"] = prefab
    tiles = sink.tiles()
    progress.begin("areas: tiles", total=len(tiles), unit="tiles")
    written = sink.flush(progress)
    progress.end(f"{written['files']} tiles, {written['tris']:,} triangles")
    with open(os.path.join(out_dir, "materials.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"schema": "csiibundle.bake.areas.materials/1", "materials": materials}, handle, indent=1, sort_keys=True)
    return {"areas": stats["placed"], "pieces": sum(len(p["faces"]) for p in pieces.values()),
            "sourceTris": stats["source_tris"], "paintedArea": round(stats["painted_area"], 1),
            "missedLookups": stats["missed"], "worstUnconformed": round(stats["counters"]["worst"], 4),
            "tiles": written["files"], "tris": written["tris"], "_tiles": written["tiles"],
            "seconds": round(time.monotonic() - started, 1)}


register(Product(PRODUCT_NAME, INPUTS, "areas/", run, options=("ground",)))