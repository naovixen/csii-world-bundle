"""Bake product `lanegeometry` — the roadside furniture (§5B non-decal parts): fences, hedges,
cables, sleepers — as PLACEMENTS where a tile is rigid and level, and as baked, tiled geometry
everywhere else.

`lanes.bend_part` already splits the work: a full, un-hung tile whose curve window is straight
and whose width is constant collapses to one affine matrix (`rigid_tile_matrix`), everything
else is bent per vertex. Two things this product adds for an engine consumer:

* **The rigid matrix is a shear on any grade** — its columns are `right·width`, `ŷ`, and
  `chord/mesh_length`, and on a climb the chord is not perpendicular to `ŷ`. An instancing path
  that only knows rotation × scale (BeamNG forest: rotation × uniform scale; TSStatic: rotation
  × per-axis scale) cannot express it. So a rigid tile is emitted as a placement only when it
  is LEVEL within `LEVEL_GRADE` (the chord's rise over run); the rest are transformed by their
  own matrix into world geometry and baked with the curved tiles.
* **Volume.** A full city's furniture baked out is a lot of triangles; the product reports per
  category and honours `lane_parts` (record categories to keep, default all: `lane`, `fence`,
  `cable`) and `lane_max_tris` (a budget after which further baked geometry is dropped and
  counted, never silently).

Outputs: `derived/lanegeometry/instances.jsonl` — one line per placement:
`{"assetId", "category", "prefab", "matrix": [16 row-major bundle-frame], "scale": [sx, sy, sz],
"uniform": bool}` — and `derived/lanegeometry/lanegeometry_<tx>_<tz>.glb` tiles.
"""

import json
import os
import time
from collections import Counter

import numpy

from .. import bezier, glb, lanes, reader
from . import Product, register
from .markings import _in_region, _material_specs, _texture_uri_base
from .tiles import TileSink

PRODUCT_NAME = "lanegeometry"
INPUTS = ("laneGeometry", "lanePrefabs", "laneMeshes")
LEVEL_GRADE = 0.02      # rise/run below which a rigid tile is "level" and expressible as rotation x scale. TUNED.
UNIFORM_TOL = 0.02      # scale factors within 2 % of each other count as uniform (forest-able). TUNED.
DEFAULT_CATEGORIES = ("lane", "fence", "cable")


def _classify(matrix):
    """`(level, scale, uniform)` for a rigid tile matrix (bundle frame)."""
    m = numpy.asarray(matrix, dtype=numpy.float64)
    c0, c1, c2 = m[:3, 0], m[:3, 1], m[:3, 2]
    run = float(numpy.hypot(c2[0], c2[2]))
    grade = abs(float(c2[1])) / max(run, 1e-9)
    scale = [float(numpy.linalg.norm(c0)), float(numpy.linalg.norm(c1)), float(numpy.linalg.norm(c2))]
    smax, smin = max(scale), min(scale)
    uniform = (smax - smin) <= UNIFORM_TOL * max(smax, 1e-9)
    return grade <= LEVEL_GRADE, scale, uniform


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    region = options.get("region")
    tile = float(options.get("tile", 256.0))
    categories = tuple(options.get("lane_parts") or DEFAULT_CATEGORIES)
    max_tris = options.get("lane_max_tris")
    out_dir = os.path.join(derived, PRODUCT_NAME)
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()

    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_lane_prefabs(bundle) or {}
    manifest = context.manifest or {}
    total_records = int(((manifest.get("counts") or {}).get("laneGeometry")) or 0) or None

    progress.begin("lanegeometry: bend", total=total_records, unit="lanes")
    sources = {}
    per_category = Counter()
    placements = []                                # (assetId, category, prefab, matrix)
    baked_blocks = []                              # per-vertex bent tiles
    sheared = 0
    bent = skipped_region = skipped_category = missing_mesh = 0
    seen = 0
    if os.path.isfile(lay.lane_geometry):
        for record in reader.read_jsonl(lay.lane_geometry):
            seen += 1
            if seen % 2000 == 0:
                progress.advance(2000)
                progress.check()
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                continue
            category = record.get("category") or "lane"
            if category not in categories:
                skipped_category += 1
                continue
            curve = record["curve"]
            if not _in_region(curve[0][0], curve[0][2], region):
                skipped_region += 1
                continue
            parts = entry.get("parts", [])
            solid = [i for i in record.get("parts", []) if 0 <= i < len(parts) and not parts[i].get("decal")]
            if not solid:
                continue
            curve_length = bezier.length(curve)
            spans = record.get("spans") or [[0.0, 1.0]]
            width_scale = record.get("widthScale", [1.0, 1.0])
            hang = lanes.hang_curve(record, entry, curve_length)
            sink = {"blocks": [], "instances": [], "tiles_instanced": 0, "tiles_baked": 0, "decal_dropped": 0}
            for index in solid:
                asset_id = parts[index]["assetId"]
                if asset_id not in sources:
                    path = os.path.join(lay.lane_meshes, asset_id + ".glb")
                    sources[asset_id] = lanes.part_source(glb.parse_glb(path)) if os.path.isfile(path) else None
                source = sources[asset_id]
                if source is None:
                    missing_mesh += 1
                    continue
                lanes.bend_part(parts[index], source, curve, spans, curve_length, width_scale, hang, sink)
            for asset_id, matrix in sink["instances"]:
                level, scale, uniform = _classify(matrix)
                if level:
                    placements.append((asset_id, category, record.get("prefab"), matrix, scale, uniform))
                else:
                    # bake the sheared tile as world geometry from the same part source
                    src = sources.get(asset_id)
                    if src is None:
                        continue
                    world = src["verts"] @ numpy.asarray(matrix)[:3, :3].T + numpy.asarray(matrix)[:3, 3]
                    tri_mat = src["tri_material"]
                    for m_index in numpy.unique(tri_mat):
                        rows = numpy.nonzero(tri_mat == m_index)[0]
                        baked_blocks.append({"verts": world, "tris": src["tris"][rows], "uvs": src["uvs"][rows],
                                             "material": src["materials"][int(m_index)], "category": category})
                    sheared += 1
            for block in sink["blocks"]:
                block["category"] = category
                baked_blocks.append(block)
            per_category[category] += 1
            bent += 1
    progress.end(f"{bent} lanes: {len(placements)} level rigid tiles as placements, {sheared} sheared rigid tiles "
                 f"and {len(baked_blocks)} baked blocks; "
                 f"{skipped_region} outside region, {skipped_category} filtered by category, {missing_mesh} no mesh")

    # -- placements -----------------------------------------------------------------------------
    inst_path = os.path.join(out_dir, "instances.jsonl")
    uniform_count = 0
    with open(inst_path, "w", encoding="utf-8", newline="\n") as handle:
        for asset_id, category, prefab, matrix, scale, uniform in placements:
            uniform_count += int(uniform)
            row = {"assetId": asset_id, "category": category, "prefab": prefab,
                   "matrix": [round(float(v), 6) for v in numpy.asarray(matrix).reshape(-1)],
                   "scale": [round(s, 4) for s in scale], "uniform": bool(uniform)}
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    # -- baked tiles ----------------------------------------------------------------------------
    specs = _material_specs(derived, bundle)
    tile_sink = TileSink(tile, PRODUCT_NAME, out_dir, specs)
    tris_total = 0
    dropped_budget = 0
    tris_by_category = Counter()
    for block in baked_blocks:
        n = len(block["tris"])
        if n == 0:
            continue
        if max_tris is not None and tris_total + n > int(max_tris):
            dropped_budget += n
            continue
        material = block["material"] if isinstance(block["material"], str) else str(block["material"])
        tile_sink.add(block["verts"], block["tris"], block["uvs"], material)
        tris_total += n
        tris_by_category[block.get("category", "lane")] += n
    tiles = tile_sink.tiles()
    progress.begin("lanegeometry: tiles", total=len(tiles), unit="tiles")
    written = tile_sink.flush(progress, texture_uri_base=_texture_uri_base(out_dir, bundle))
    progress.end(f"{written['files']} tiles, {written['tris']:,} triangles baked "
                 f"({dropped_budget:,} dropped by lane_max_tris)" if max_tris else
                 f"{written['files']} tiles, {written['tris']:,} triangles baked")
    if dropped_budget:
        progress.log(f"lanegeometry: {dropped_budget:,} triangles dropped by lane_max_tris={max_tris}")
    return {"lanes": bent, "byCategory": dict(per_category), "placements": len(placements),
            "placementsUniform": uniform_count, "shearedTiles": sheared, "bakedTris": written["tris"],
            "bakedTrisByCategory": dict(tris_by_category), "droppedTris": dropped_budget,
            "tiles": written["files"], "_tiles": written["tiles"], "seconds": round(time.monotonic() - started, 1)}


register(Product(PRODUCT_NAME, INPUTS, "lanegeometry/", run))
