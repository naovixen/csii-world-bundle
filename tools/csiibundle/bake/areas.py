"""Bake product `areas` — §8b painted surfaces conformed to the terrain, world-planar UVs, tiled.

`areas.build_pieces` is the whole algorithm (adaptive triangle split until each piece follows
the ground, `rendererPriority` lift order, region filter); this product feeds it the ground
(`ground.GroundSurface`, max-envelope so paint never sinks), converts its Z-up output back to
the bundle frame (keeping its face order — see the winding note in `run`), gives every vertex the prefab's world-planar UV (`uv = world_xy × uvScale`,
the game's own `colossal_UVScale`), and writes `derived/areas/areas_<tx>_<tz>.glb` with one
primitive per prefab (material name `area:<prefab>`). The material description each prefab
needs — texture, colour, normal/mask maps, metallic/roughness, uvScale, unresolved flag — is
written beside the tiles as `derived/areas/materials.json` from `areas.surface_material`.

**The conform runs on `jobs` worker processes.** Every area record is independent — it reads the
ground and writes only its own triangles — so the 24,457 records are cut into chunks and handed
to a pool, `imap` (ordered, so the merged result is the serial result bit for bit). Each worker
attaches the ONE decoded 8192² heightfield through `multiprocessing.shared_memory` rather than
decoding its own: two float64 copies per worker is 1 GiB each, and that is what made this loop
serial. Workers hand back numpy arrays, not lists of tuples — the parent's own copy of 16 million
Python 3-tuples was over a gigabyte on its own.
"""

import json
import os
import time
from collections import Counter
from multiprocessing import Pool, shared_memory

import numpy

from .. import areas as area_rules
from .. import reader
from . import Product, register
from .ground import GroundSurface
from .tiles import TileSink

PRODUCT_NAME = "areas"
INPUTS = ("areas", "areaPrefabs", "terrain", "terrainFine")

# Records per work unit. Small enough that a chunk holding one huge park polygon cannot become
# the tail of the whole product, large enough that pickling the chunk is noise beside conforming
# it. TUNED against 24,457 records on 7 workers.
CHUNK = 24

# Per-worker state, set once by `_worker_init` — the ground is far too big to ride on every task.
_WORKER = {}


def _worker_init(spec, cell, origin_x, origin_y, diagonal, mode, catalogue, center, radius,
                 bundle_dir, prefer_fine):
    """Attach the shared heightfield (or decode our own if the parent could not share one)."""
    if spec is not None:
        name, shape, dtype = spec
        block = shared_memory.SharedMemory(name=name)
        _WORKER["block"] = block                 # keep the mapping alive for the process's life
        heights = numpy.ndarray(tuple(shape), dtype=numpy.dtype(dtype), buffer=block.buf)
        ground = GroundSurface.from_heights(heights, cell, origin_x, origin_y, diagonal, mode)
    else:
        ground = GroundSurface(bundle_dir, diagonal, mode=mode, prefer_fine=prefer_fine)
    _WORKER["cell"] = float(ground.cell) if ground.present else 3.5
    _WORKER["sample"] = area_rules.memoised_sampler(ground) if ground.present else (lambda x, y: None)
    _WORKER["catalogue"] = catalogue
    _WORKER["center"] = center
    _WORKER["radius"] = radius


def _conform_chunk(records):
    pieces, stats = area_rules.build_pieces(records, _WORKER["cell"], _WORKER["sample"],
                                            center=_WORKER["center"], radius=_WORKER["radius"],
                                            catalogue=_WORKER["catalogue"])
    return _to_arrays(pieces), stats


def _to_arrays(pieces):
    """`build_pieces` output as numpy, dropping the faces — they are always `arange`.

    Every piece is emitted as three fresh vertices (`base, base+1, base+2` with
    `base = len(verts)`), so the face array carries no information the vertex count does not.
    Asserted rather than assumed, because reconstructing it wrong would silently reorder the
    whole layer.
    """
    out = {}
    for prefab, piece in pieces.items():
        faces = piece["faces"]
        if not faces:
            continue
        assert faces[0] == (0, 1, 2) and faces[-1][2] == len(piece["verts"]) - 1, \
            f"areas: {prefab} faces are not sequential triples; the arange reconstruction is invalid"
        out[prefab] = (numpy.asarray(piece["verts"], dtype=numpy.float64),
                       numpy.asarray(piece["minLod"], dtype=numpy.int64))
    return out


def _merge(into, chunk_pieces):
    for prefab, (verts, min_lods) in chunk_pieces.items():
        slot = into.setdefault(prefab, ([], []))
        slot[0].append(verts)
        slot[1].append(min_lods)


def _merge_stats(into, more):
    if into is None:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in more.items()}
    for key, value in more.items():
        if key == "counters":
            into["counters"]["unconformed"] += value["unconformed"]
            into["counters"]["unconformed_area"] += value["unconformed_area"]
            into["counters"]["worst"] = max(into["counters"]["worst"], value["worst"])
        else:
            into[key] += value
    return into


def _publish(heights):
    """Copy the decoded raster into a shared block, or `None` if the platform will not.

    512 MiB once for the whole pool instead of once per worker. A failure here is a memory
    regression, not a correctness one, so it degrades to per-worker decoding and says so.
    """
    try:
        block = shared_memory.SharedMemory(create=True, size=heights.nbytes)
    except Exception:      # noqa: BLE001 - no shared memory on this platform/container
        return None, None
    view = numpy.ndarray(heights.shape, dtype=heights.dtype, buffer=block.buf)
    view[:] = heights
    return block, (block.name, list(heights.shape), str(heights.dtype))


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    region = options.get("region")
    tile = float(options.get("tile", 256.0))
    jobs = max(1, int(options.get("jobs") or 1))
    diagonal = options.get("diagonal", "envelope")
    prefer_fine = options.get("ground", "fine") == "fine"
    out_dir = os.path.join(derived, PRODUCT_NAME)
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()

    catalogue = reader.load_area_prefabs(bundle) or {}
    areas_path = reader.layer_path(bundle, "areas")
    manifest = context.manifest or {}
    total = int(((manifest.get("counts") or {}).get("areas")) or 0) or None
    ground = GroundSurface(bundle, diagonal, mode="max", prefer_fine=prefer_fine)
    if not ground.present:
        progress.log("areas: no terrain layer — areas stay at their authored heights (flat)")
        cell = 3.5
    else:
        cell = float(ground.cell)

    progress.begin("areas: conform", total=total, unit="areas")
    center = radius = None
    if region:
        center = (float(region["centerX"]), float(region["centerZ"]))
        radius = float(region["radius"])
    records = list(reader.read_jsonl(areas_path)) if os.path.isfile(areas_path) else []
    chunks = [records[i:i + CHUNK] for i in range(0, len(records), CHUNK)]

    merged = {}
    stats = None
    if jobs > 1 and len(chunks) > 1:
        block, spec = _publish(ground.a.heights) if ground.present else (None, None)
        if ground.present and spec is None:
            progress.log(f"areas: no shared memory — each of {jobs} workers decodes its own raster")
        init = (spec, cell, ground.a.origin_x if ground.present else 0.0,
                ground.a.origin_y if ground.present else 0.0, diagonal, "max",
                catalogue, center, radius, bundle, prefer_fine)
        del ground      # the workers own the raster now; the parent samples nothing
        try:
            with Pool(processes=jobs, initializer=_worker_init, initargs=init) as pool:
                for chunk, (chunk_pieces, chunk_stats) in zip(chunks, pool.imap(_conform_chunk, chunks)):
                    progress.check()
                    _merge(merged, chunk_pieces)
                    stats = _merge_stats(stats, chunk_stats)
                    progress.advance(len(chunk))
        finally:
            if block is not None:
                block.close()
                block.unlink()
    else:
        sample = area_rules.memoised_sampler(ground) if ground.present else (lambda x, y: None)  # noqa: E731
        last = [0]

        def tick(placed):
            if placed - last[0] >= 200:
                progress.advance(placed - last[0])
                last[0] = placed
                progress.check()

        pieces, stats = area_rules.build_pieces(records, cell, sample, center=center, radius=radius,
                                                progress=tick, catalogue=catalogue)
        _merge(merged, _to_arrays(pieces))
        del pieces
    stats = stats or {"placed": 0, "skipped_region": 0, "dropped_bad": 0, "missed": 0,
                      "source_tris": 0, "painted_area": 0.0,
                      "counters": {"unconformed": 0, "unconformed_area": 0.0, "worst": 0.0}}
    pieces = {prefab: (numpy.concatenate(v) if len(v) > 1 else v[0],
                       numpy.concatenate(m) if len(m) > 1 else m[0])
              for prefab, (v, m) in merged.items()}
    del merged
    progress.end(f"{stats['placed']} areas -> {sum(len(m) for _, m in pieces.values()):,} pieces "
                 f"({stats['skipped_region']} outside region, {stats['missed']} node lookups missed, "
                 f"worst unconformed {stats['counters']['worst']:.3f} m)")

    # tiles: Z-up pieces back to the bundle frame (x, z_up, y_north), winding restored
    sink = TileSink(tile, PRODUCT_NAME, out_dir)
    lod_histogram = Counter()
    materials = {}
    for prefab, (v_zup, min_lods) in pieces.items():
        faces = numpy.arange(len(v_zup), dtype=numpy.int64).reshape(-1, 3)
        verts = v_zup[:, [0, 2, 1]]                        # bundle frame (x, y_up, z_north)
        # winding: build_pieces reversed the source order for Z-up; the axis swap back to the
        # bundle frame reverses orientation again, so the Z-up face order IS the bundle-frame
        # up-facing order — measured (bundle-frame normals +Y on the reference city), not argued
        entry = catalogue.get(prefab) or {}
        mat = area_rules.surface_material(entry)
        uv_scale = float(mat["uv_scale"] or area_rules.UV_SCALE_DEFAULT)
        uvs = numpy.stack([v_zup[:, 0] * uv_scale, v_zup[:, 1] * uv_scale], axis=1)   # world-planar (x, y_north)
        name = f"area:{prefab}"
        # one add per distinct limit, so the tile carries the game's own per-triangle cull
        for value in numpy.unique(min_lods):
            rows = numpy.nonzero(min_lods == value)[0]
            sink.add(verts, faces[rows], uvs, name, min_lod=int(value))
            lod_histogram[int(value)] += len(rows)
        materials[name] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in mat.items()}
        materials[name]["prefab"] = prefab
    piece_count = sum(len(m) for _, m in pieces.values())
    del pieces
    tiles = sink.tiles()
    progress.begin("areas: tiles", total=len(tiles), unit="tiles")
    written = sink.flush(progress, jobs=jobs)
    progress.end(f"{written['files']} tiles, {written['tris']:,} triangles")
    with open(os.path.join(out_dir, "materials.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"schema": "csiibundle.bake.areas.materials/1", "materials": materials}, handle, indent=1, sort_keys=True)
    return {"areas": stats["placed"], "pieces": piece_count,
            "sourceTris": stats["source_tris"], "paintedArea": round(stats["painted_area"], 1),
            "missedLookups": stats["missed"], "worstUnconformed": round(stats["counters"]["worst"], 4),
            "tiles": written["files"], "tris": written["tris"], "_tiles": written["tiles"],
            "minLodTriangles": {str(k): int(v) for k, v in sorted(lod_histogram.items())},
            "jobs": jobs, "seconds": round(time.monotonic() - started, 1)}


# version 2: tile primitives carry the game's per-triangle draw-distance limit (`#L<n>`)
register(Product(PRODUCT_NAME, INPUTS, "areas/", run, version=2, options=("ground", "region")))
