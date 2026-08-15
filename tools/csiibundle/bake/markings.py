"""Bake product `markings` — road markings, wear tint and instance decals, draped and tiled.

CS2 draws every marking as a projector: a box the vertex shader bends along the lane's curve
and the deferred decal pass projects onto whatever surface is beneath. Rebuilt as geometry it
has to be PUT on that surface, and doing it once here is what saves every consumer the
2.2-million-ray-cast bill (`docs/plans/beamng-consumer-plan.md`). The pipeline, all
`csiibundle`:

1. `roads.load_road_surface` — the network meshes as a `TriangleSoupSurface`; the §8 heightmap
   as the ground fallback (`HeightfieldSurface`).
2. `lanemeshes.jsonl` streamed; each record's DECAL parts (`part["decal"]`) bent by
   `lanes.bend_part` (the §5B algorithm: chord-to-chord box, texture repeat in V, stations from
   the marking's own lift), non-decal parts left to `lanegeometry`.
3. `instances.jsonl` `kind: "decal"` records — flattened quads at the projector's mid-height —
   subdivided to ≤ `DECAL_STEP` and added as blocks with the `instance_decal` rung and their own
   `decalHeight / 2` reach; `decalLayers` picks the surface (`Roads` → road soup, `Terrain` →
   ground; both → road then ground).
4. `drape.drape_blocks` — one bulk query, nearest-to-expected inside each part's projector
   window, ground fallback at the same window, a miss keeps its height.
5. `tiles.TileSink` — welded, one primitive per material, `derived/markings/markings_<tx>_<tz>.glb`.

Per-lane `colorSet` (the wear tint's colour) is NOT applied here: the marking materials of the
reference city are untinted atlases and the wear decal's colour variation is a few percent of
alpha; a lane's colorSet is recorded in stats and left for a follow-up if it proves visible.
"""

import json
import math
import os
import time

import numpy

from .. import bezier, decals, drape, frames, glb, instances, lanes, reader
from . import Product, register
from .roads import load_road_surface
from .tiles import TileSink

PRODUCT_NAME = "markings"
INPUTS = ("laneGeometry", "lanePrefabs", "laneMeshes", "networkMeshes", "networks", "instances", "meshes", "terrain")
DECAL_STEP = 2.0     # metres: instance-decal quads are split so no edge exceeds this before draping. TUNED.


def _in_region(x, z, region):
    if not region:
        return True
    return (x - region["centerX"]) ** 2 + (z - region["centerZ"]) ** 2 <= region["radius"] ** 2


def _subdivide_quad(corners, uvs, step):
    """Split a planar quad (4 corners in order, with 4 uvs) into an n x m lattice.
    Returns `(verts (N,3), tris (M,3), uvs (N,2))`."""
    p0, p1, p2, p3 = (numpy.asarray(c, dtype=numpy.float64) for c in corners)
    u0, u1, u2, u3 = (numpy.asarray(c, dtype=numpy.float64) for c in uvs)
    n = max(1, int(math.ceil(numpy.linalg.norm(p1 - p0) / step)))
    m = max(1, int(math.ceil(numpy.linalg.norm(p3 - p0) / step)))
    a = numpy.linspace(0.0, 1.0, n + 1)
    b = numpy.linspace(0.0, 1.0, m + 1)
    A, B = numpy.meshgrid(a, b, indexing="ij")            # (n+1, m+1)
    A = A.reshape(-1, 1)
    B = B.reshape(-1, 1)
    verts = (1 - A) * (1 - B) * p0 + A * (1 - B) * p1 + A * B * p2 + (1 - A) * B * p3
    uv = (1 - A) * (1 - B) * u0 + A * (1 - B) * u1 + A * B * u2 + (1 - A) * B * u3
    tris = []
    for i in range(n):
        for j in range(m):
            k = i * (m + 1) + j
            tris.append([k, k + m + 1, k + m + 2])
            tris.append([k, k + m + 2, k + 1])
    return verts, numpy.asarray(tris, dtype=numpy.int64), uv


def _decal_quad(node):
    """The 4 corners + uvs of a flattened decal mesh (2 triangles), in the node's own order, or
    None when the mesh is not a simple quad."""
    verts = numpy.asarray(node["verts"], dtype=numpy.float64)
    tris = numpy.asarray(node["tris"], dtype=numpy.int64)
    if len(verts) != 4 or len(tris) != 2 or node.get("uvs") is None:
        return None
    # order the four corners around the quad: start at tri 0, walk shared edges
    t0, t1 = set(tris[0].tolist()), set(tris[1].tolist())
    shared = list(t0 & t1)
    if len(shared) != 2:
        return None
    a = [i for i in tris[0].tolist() if i not in shared][0]
    c = [i for i in tris[1].tolist() if i not in shared][0]
    order = [a, shared[0], c, shared[1]]
    uvs = numpy.asarray(node["uvs"], dtype=numpy.float64)
    return verts[order], uvs[order]


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    region = options.get("region")
    tile = float(options.get("tile", 256.0))
    out_dir = os.path.join(derived, "markings")
    stats = {}
    started = time.monotonic()

    road, road_stats = load_road_surface(bundle, region, progress)
    stats["road"] = road_stats
    ground = drape.HeightfieldSurface.from_bundle(bundle)
    if ground is None:
        progress.log("no terrain layer: markings with no road beneath keep their bent height")

    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_lane_prefabs(bundle) or {}
    manifest = context.manifest or {}
    total_records = int(((manifest.get("counts") or {}).get("laneGeometry")) or 0) or None

    # -- 2. bend the decal parts ------------------------------------------------------------
    progress.begin("markings: bend", total=total_records, unit="lanes")
    sources = {}
    blocks = []
    sink = {"blocks": blocks, "instances": [], "tiles_instanced": 0, "tiles_baked": 0, "decal_dropped": 0}
    bent = skipped_region = missing_prefab = missing_mesh = with_colorset = 0
    seen = 0
    if os.path.isfile(lay.lane_geometry):
        for record in reader.read_jsonl(lay.lane_geometry):
            seen += 1
            if seen % 2000 == 0:
                progress.advance(2000)
                progress.check()
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                missing_prefab += 1
                continue
            curve = record["curve"]
            if not _in_region(curve[0][0], curve[0][2], region):
                skipped_region += 1
                continue
            parts = entry.get("parts", [])
            decal_indices = [i for i in record.get("parts", []) if 0 <= i < len(parts) and parts[i].get("decal")]
            if not decal_indices:
                continue
            curve_length = bezier.length(curve)
            spans = record.get("spans") or [[0.0, 1.0]]
            width_scale = record.get("widthScale", [1.0, 1.0])
            hang = lanes.hang_curve(record, entry, curve_length)
            if record.get("colorSet"):
                with_colorset += 1
            for index in decal_indices:
                asset_id = parts[index]["assetId"]
                if asset_id not in sources:
                    path = os.path.join(lay.lane_meshes, asset_id + ".glb")
                    sources[asset_id] = lanes.part_source(glb.parse_glb(path)) if os.path.isfile(path) else None
                source = sources[asset_id]
                if source is None:
                    missing_mesh += 1
                    continue
                lanes.bend_part(parts[index], source, curve, spans, curve_length, width_scale, hang, sink)
            bent += 1
    progress.end(f"{bent} lanes bent into {len(blocks)} marking blocks "
                 f"({skipped_region} outside region, {missing_prefab} no prefab, {missing_mesh} no mesh, "
                 f"{with_colorset} carry a colorSet, {sink['decal_dropped']} dropped by clip)")
    stats.update({"lanesBent": bent, "markingBlocks": len(blocks), "decalDropped": sink["decal_dropped"],
                  "lanesWithColorSet": with_colorset})

    # -- 3. instance decals -------------------------------------------------------------------
    total_instances = int(((manifest.get("counts") or {}).get("instances")) or 0) or None
    progress.begin("markings: instance decals", total=total_instances, unit="instances")
    decal_meshes = {}
    decal_blocks_road, decal_blocks_ground = [], []
    n_decals = n_decal_missing = 0
    seen = 0
    for record in instances.read_instances(bundle):
        seen += 1
        if seen % 20000 == 0:
            progress.advance(20000)
            progress.check()
        if record.get("kind") != "decal":
            continue
        pos = record.get("pos") or (0, 0, 0)
        if not _in_region(pos[0], pos[2], region):
            continue
        asset_id = record["assetId"]
        if asset_id not in decal_meshes:
            path = os.path.join(lay.meshes, asset_id + ".glb")
            nodes = glb.parse_glb(path) if os.path.isfile(path) else None
            quads = []
            for node in nodes or []:
                q = _decal_quad(node)
                if q is not None:
                    quads.append((q[0], q[1], node["material_runs"][0][0] if node["material_runs"] else "decal", node["matrix"]))
            decal_meshes[asset_id] = quads
        quads = decal_meshes[asset_id]
        if not quads:
            n_decal_missing += 1
            continue
        matrix = frames.instance_matrix(record)          # bundle frame, no lift: the drape sets the height
        half = (instances.decal_height(record) or 1.0) * 0.5
        layers = instances.decal_layers(record)
        target = decal_blocks_ground if (layers and "Roads" not in layers) else decal_blocks_road
        for corners, uvs, material, node_matrix in quads:
            world = frames.transform_points(matrix @ node_matrix, corners)
            v, t, uv = _subdivide_quad(world, uvs, DECAL_STEP)
            target.append({"verts": v, "tris": t, "uvs": uv[t], "material": material,
                           "drape_eps": decals.LIFT["instance_decal"], "half_height": half})
        n_decals += 1
    progress.end(f"{n_decals} decal instances as {len(decal_blocks_road) + len(decal_blocks_ground)} blocks "
                 f"({n_decal_missing} without a quad mesh)")
    stats.update({"decalInstances": n_decals, "decalInstancesNoMesh": n_decal_missing})

    # -- 4. drape -----------------------------------------------------------------------------
    progress.begin("markings: drape", total=None, unit="vertices")
    counters = {"queries": 0, "draped": 0, "grounded": 0, "missed": 0}
    lane_blocks = [b for b in blocks if b.get("drape_eps") is not None]
    drape.drape_blocks(lane_blocks + decal_blocks_road, road, ground, counters)
    if decal_blocks_ground:
        drape.drape_blocks(decal_blocks_ground, ground if ground is not None else road, None, counters)
    total_v = sum(len(b["verts"]) for b in lane_blocks + decal_blocks_road + decal_blocks_ground)
    progress.set_total(total_v)
    progress.advance(total_v)
    miss_frac = counters["missed"] / max(1, total_v)
    progress.end(f"{counters['queries']} queries for {total_v} vertices: {counters['draped']} draped, "
                 f"{counters['grounded']} grounded, {counters['missed']} missed ({miss_frac * 100:.2f}%)")
    if miss_frac > drape.MISS_WARN_FRACTION:
        progress.log(f"WARNING: {miss_frac * 100:.1f}% of marking vertices found no surface (limit "
                     f"{drape.MISS_WARN_FRACTION * 100:.0f}%) — region edge, or a missing network layer")
    stats["drape"] = dict(counters, vertices=total_v)

    # -- 5. tiles -------------------------------------------------------------------------------
    resolved_specs = _material_specs(derived, bundle)
    tile_sink = TileSink(tile, PRODUCT_NAME, out_dir, resolved_specs)
    for b in lane_blocks + decal_blocks_road + decal_blocks_ground:
        if len(b["tris"]) == 0:
            continue
        material = b["material"] if isinstance(b["material"], str) else str(b["material"])
        tile_sink.add(b["verts"], b["tris"], b["uvs"], material)
    tiles = tile_sink.tiles()
    progress.begin("markings: tiles", total=len(tiles), unit="tiles")
    uri_base = _texture_uri_base(out_dir, bundle)
    written = tile_sink.flush(progress, texture_uri_base=uri_base)
    progress.end(f"{written['files']} tiles, {written['verts']:,} vertices, {written['tris']:,} triangles")
    stats.update({"tiles": written["files"], "verts": written["verts"], "tris": written["tris"],
                  "_tiles": written["tiles"], "seconds": round(time.monotonic() - started, 1)})
    return stats


def _material_specs(derived, bundle):
    """Base-colour paths + alpha modes for the tile writer, from the bake's own materials
    product when present, else straight from `materials.json`."""
    path = os.path.join(derived, "materials.json")
    specs = {}
    try:
        if os.path.isfile(path):
            doc = json.load(open(path, encoding="utf-8"))
            for key, entry in doc.get("materials", {}).items():
                specs[key] = {"baseColor": entry.get("baseColor"), "normal": entry.get("normal"),
                              "alphaMode": entry.get("alphaMode"), "alphaCutoff": entry.get("alphaCutoff"),
                              "doubleSided": entry.get("doubleSided"), "baseColorFactor": entry.get("baseColorFactor")}
            return specs
    except Exception:  # noqa: BLE001
        pass
    for key, entry in (reader.load_materials(bundle) or {}).items():
        specs[key] = {"baseColor": entry.get("baseColor"), "normal": entry.get("normal"),
                      "alphaMode": entry.get("alphaMode"), "alphaCutoff": entry.get("alphaCutoff"),
                      "doubleSided": entry.get("doubleSided")}
    return specs


def _texture_uri_base(out_dir, bundle):
    """Relative URI prefix from a tile directory to `<bundle>/textures/`, or None when no
    relative path exists (different drive)."""
    try:
        textures = reader.layer_path(bundle, "textures")
        rel = os.path.relpath(textures, out_dir).replace(os.sep, "/")
        return rel.rstrip("/") + "/"
    except ValueError:
        return None


register(Product(PRODUCT_NAME, INPUTS, "markings/", run))
