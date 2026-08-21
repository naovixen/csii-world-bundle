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
from collections import Counter

import numpy

from .. import bezier, decals, drape, frames, glb, instances, lanes, lod, reader
from .. import region as region_rule
from . import Product, register
from .roads import load_road_surface
from .tiles import TileSink

PRODUCT_NAME = "markings"
INPUTS = (
    "laneGeometry",
    "lanePrefabs",
    "laneMeshes",
    "networkMeshes",
    "networks",
    "instances",
    "meshes",
    "terrain",
    "terrainFine",
)
# metres: instance-decal quads are split so no edge exceeds this before draping. TUNED.
DECAL_STEP = 2.0


def _in_region(x, z, region):
    """The region box. Kept as a name here because `skirts.py` and `lanegeometry.py` import it;
    refactor Phase 2 points those at `csiibundle.region` directly and this goes away."""
    return region_rule.contains(x, z, region)


def _subdivide_quad(corners, uvs, step):
    """Split a planar quad (4 corners in order, with 4 uvs) into an n x m lattice.
    Returns `(verts (N,3), tris (M,3), uvs (N,2))`."""
    p0, p1, p2, p3 = (numpy.asarray(c, dtype=numpy.float64) for c in corners)
    u0, u1, u2, u3 = (numpy.asarray(c, dtype=numpy.float64) for c in uvs)
    n = max(1, int(math.ceil(numpy.linalg.norm(p1 - p0) / step)))
    m = max(1, int(math.ceil(numpy.linalg.norm(p3 - p0) / step)))
    a = numpy.linspace(0.0, 1.0, n + 1)
    b = numpy.linspace(0.0, 1.0, m + 1)
    A, B = numpy.meshgrid(a, b, indexing="ij")  # (n+1, m+1)
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
    a = next(i for i in tris[0].tolist() if i not in shared)
    c = next(i for i in tris[1].tolist() if i not in shared)
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
    ground = drape.HeightfieldSurface.from_bundle(
        bundle, prefer_fine=options.get("ground", "fine") == "fine"
    )
    if ground is None:
        progress.log("no terrain layer: markings with no road beneath keep their bent height")

    lay = reader.resolve_layers(bundle)
    catalogue = reader.load_lane_prefabs(bundle) or {}
    manifest = context.manifest or {}
    total_records = int(((manifest.get("counts") or {}).get("laneGeometry")) or 0) or None

    # -- 2. bend the decal parts ------------------------------------------------------------
    progress.begin("markings: bend", total=total_records, unit="lanes")
    sources = {}
    prefab_lods = {}
    blocks = []
    sink = {
        "blocks": blocks,
        "instances": [],
        "tiles_instanced": 0,
        "tiles_baked": 0,
        "decal_dropped": 0,
    }
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
            decal_indices = [
                i for i in record.get("parts", []) if 0 <= i < len(parts) and parts[i].get("decal")
            ]
            if not decal_indices:
                continue
            curve_length = bezier.length(curve)
            spans = record.get("spans") or [[0.0, 1.0]]
            width_scale = record.get("widthScale", [1.0, 1.0])
            hang = lanes.hang_curve(record, entry, curve_length)
            if record.get("colorSet"):
                with_colorset += 1
            prefab_lod = _lane_prefab_min_lod(
                record.get("prefab"), entry, sources, lay, prefab_lods
            )
            for index in decal_indices:
                asset_id = parts[index]["assetId"]
                if asset_id not in sources:
                    path = os.path.join(lay.lane_meshes, asset_id + ".glb")
                    sources[asset_id] = (
                        lanes.part_source(glb.parse_glb(path)) if os.path.isfile(path) else None
                    )
                source = sources[asset_id]
                if source is None:
                    missing_mesh += 1
                    continue
                # The game's limit is a property of the PREFAB (the min over all its meshes,
                # `NetLaneGeometryData.m_MinLod`), not of the part being bent, so it is stamped on
                # whatever blocks this bend produced rather than passed down into `bend_part`.
                first = len(blocks)
                lanes.bend_part(
                    parts[index], source, curve, spans, curve_length, width_scale, hang, sink
                )
                for block in blocks[first:]:
                    block["min_lod"] = prefab_lod
            bent += 1
    progress.end(
        f"{bent} lanes bent into {len(blocks)} marking blocks "
        f"({skipped_region} outside region, {missing_prefab} no prefab, {missing_mesh} no mesh, "
        f"{with_colorset} carry a colorSet, {sink['decal_dropped']} dropped by clip)"
    )
    stats.update(
        {
            "lanesBent": bent,
            "markingBlocks": len(blocks),
            "decalDropped": sink["decal_dropped"],
            "lanesWithColorSet": with_colorset,
        }
    )

    # -- 3. instance decals -------------------------------------------------------------------
    int(((manifest.get("counts") or {}).get("instances")) or 0) or None
    # prefiltered to decal records, so the manifest's instance count is not the total
    progress.begin("markings: instance decals", total=None, unit="decals")
    decal_meshes = {}
    decal_lods = {}
    decal_blocks_road, decal_blocks_ground = [], []
    n_decals = n_decal_missing = n_decal_ranked = 0
    # Collected as we stream (the instance file is read ONCE — 1.2 M records), and turned into
    # rank lifts after the loop, because a rank needs the whole set and the blocks are not draped
    # until later anyway.
    decal_priorities_seen = set()
    seen = 0
    for record in instances.read_instances(bundle, kind="decal"):
        seen += 1
        if seen % 2000 == 0:
            progress.advance(2000)
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
                    quads.append(
                        (
                            q[0],
                            q[1],
                            node["material_runs"][0][0] if node["material_runs"] else "decal",
                            node["matrix"],
                        )
                    )
            decal_meshes[asset_id] = quads
        quads = decal_meshes[asset_id]
        if not quads:
            n_decal_missing += 1
            continue
        matrix = frames.instance_matrix(record)  # bundle frame, no lift: the drape sets the height
        half = (instances.decal_height(record) or 1.0) * 0.5
        layers = instances.decal_layers(record)
        target = decal_blocks_ground if (layers and "Roads" not in layers) else decal_blocks_road
        priority = instances.decal_priority(record)
        if priority is not None:
            decal_priorities_seen.add(priority)
        decal_lod = _decal_min_lod(asset_id, quads, decal_lods)
        for corners, uvs, material, node_matrix in quads:
            world = frames.transform_points(matrix @ node_matrix, corners)
            v, t, uv = _subdivide_quad(world, uvs, DECAL_STEP)
            target.append(
                {
                    "verts": v,
                    "tris": t,
                    "uvs": uv[t],
                    "material": material,
                    "drape_eps": decals.LIFT["instance_decal"],
                    "half_height": half,
                    "decal_priority": priority,
                    "min_lod": decal_lod,
                }
            )
        n_decals += 1
    progress.end(
        f"{n_decals} decal instances as {len(decal_blocks_road) + len(decal_blocks_ground)} blocks "
        f"({n_decal_missing} without a quad mesh)"
    )
    # The game's own draw order for two decals projecting onto one surface, applied as a rank lift
    # within the top rung of the ladder (audit §57.2). `areas.py` has done this for surface areas
    # since §8b; this is the same rule for decal instances, so both halves now come from the game.
    #
    # **Expect a no-op, and say so.** Measured on the 2026-08-17 rip (§58.2), `decalPriority` is 0
    # on 17,721 of 17,722 decals — the city states essentially no ordering, so `ordered` has one
    # value and every lift is zero. It is counted rather than assumed because a modded or DLC
    # decal may use the range, and because a silent no-op is indistinguishable from a bug.
    ordered = sorted(decal_priorities_seen)
    for block in decal_blocks_road + decal_blocks_ground:
        bump = decals.instance_priority_lift(block.pop("decal_priority", None), ordered)
        if bump:
            block["drape_eps"] += bump
            n_decal_ranked += 1
    stats.update(
        {
            "decalInstances": n_decals,
            "decalInstancesNoMesh": n_decal_missing,
            "decalPriorities": len(ordered),
            "decalBlocksRanked": n_decal_ranked,
        }
    )

    # -- 4. drape -----------------------------------------------------------------------------
    progress.begin("markings: drape", total=None, unit="vertices")
    counters = {"queries": 0, "draped": 0, "grounded": 0, "missed": 0}
    lane_blocks = [b for b in blocks if b.get("drape_eps") is not None]
    drape.drape_blocks(lane_blocks + decal_blocks_road, road, ground, counters)
    if decal_blocks_ground:
        drape.drape_blocks(
            decal_blocks_ground, ground if ground is not None else road, None, counters
        )
    total_v = sum(len(b["verts"]) for b in lane_blocks + decal_blocks_road + decal_blocks_ground)
    progress.set_total(total_v)
    progress.advance(total_v)
    miss_frac = counters["missed"] / max(1, total_v)
    progress.end(
        f"{counters['queries']} queries for {total_v} vertices: {counters['draped']} draped, "
        f"{counters['grounded']} grounded, {counters['missed']} missed ({miss_frac * 100:.2f}%)"
    )
    if miss_frac > drape.MISS_WARN_FRACTION:
        progress.log(
            f"WARNING: {miss_frac * 100:.1f}% of marking vertices found no surface (limit "
            f"{drape.MISS_WARN_FRACTION * 100:.0f}%) — region edge, or a missing network layer"
        )
    stats["drape"] = dict(counters, vertices=total_v)

    # -- 5. tiles -------------------------------------------------------------------------------
    resolved_specs = _material_specs(derived, bundle)
    tile_sink = TileSink(tile, PRODUCT_NAME, out_dir, resolved_specs)
    lod_histogram = Counter()
    # Winding. A `lanes.part_source` block keeps the part .glb's triangle order in the bundle
    # frame (the X un-mirror flips orientation), so the bundle-frame face order is the REVERSE
    # of that — the same reversal the producer applies when it writes a .glb (§2.2). Draped
    # markings are flat paint seen from above, so on top of the rule every face is forced to
    # face +Y: measured, marking tiles came out 90 % face-down before this.
    for b in lane_blocks + decal_blocks_road + decal_blocks_ground:
        if len(b["tris"]) == 0:
            continue
        material = b["material"] if isinstance(b["material"], str) else str(b["material"])
        tris, uvs = _face_up(b["verts"], b["tris"], b["uvs"])
        min_lod = b.get("min_lod")
        tile_sink.add(b["verts"], tris, uvs, material, min_lod=min_lod)
        lod_histogram[255 if min_lod is None else int(min_lod)] += len(tris)
    stats["minLodTriangles"] = {str(k): int(v) for k, v in sorted(lod_histogram.items())}
    stats["minLodUnknownTriangles"] = int(lod_histogram.get(255, 0))
    tiles = tile_sink.tiles()
    progress.begin("markings: tiles", total=len(tiles), unit="tiles")
    uri_base = _texture_uri_base(out_dir, bundle)
    written = tile_sink.flush(
        progress, texture_uri_base=uri_base, jobs=max(1, int(options.get("jobs") or 1))
    )
    progress.end(
        f"{written['files']} tiles, {written['verts']:,} vertices, {written['tris']:,} triangles"
    )
    stats.update(
        {
            "tiles": written["files"],
            "verts": written["verts"],
            "tris": written["tris"],
            "_tiles": written["tiles"],
            "seconds": round(time.monotonic() - started, 1),
        }
    )
    return stats


def _face_up(verts, tris, uvs):
    """Reverse any triangle whose bundle-frame normal points down; keeps per-corner uvs aligned."""
    v = numpy.asarray(verts, dtype=numpy.float64)
    t = numpy.asarray(tris, dtype=numpy.int64)
    n = numpy.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
    down = n[:, 1] < 0
    if not down.any():
        return t, uvs
    t2 = t.copy()
    t2[down] = t[down][:, [0, 2, 1]]
    u2 = None
    if uvs is not None:
        u = numpy.asarray(uvs, dtype=numpy.float64)
        if u.ndim == 3:
            u2 = u.copy()
            u2[down] = u[down][:, [0, 2, 1]]
        else:
            u2 = u
    return t2, u2


def _material_specs(derived, bundle):
    """Base-colour paths + alpha modes for the tile writer, from the bake's own materials
    product when present, else straight from `materials.json`."""
    path = os.path.join(derived, "materials.json")
    specs = {}
    try:
        if os.path.isfile(path):
            doc = json.load(open(path, encoding="utf-8"))
            for key, entry in doc.get("materials", {}).items():
                specs[key] = {
                    "baseColor": entry.get("baseColor"),
                    "normal": entry.get("normal"),
                    "alphaMode": entry.get("alphaMode"),
                    "alphaCutoff": entry.get("alphaCutoff"),
                    "doubleSided": entry.get("doubleSided"),
                    "baseColorFactor": entry.get("baseColorFactor"),
                }
            return specs
    except Exception:
        pass
    for key, entry in (reader.load_materials(bundle) or {}).items():
        specs[key] = {
            "baseColor": entry.get("baseColor"),
            "normal": entry.get("normal"),
            "alphaMode": entry.get("alphaMode"),
            "alphaCutoff": entry.get("alphaCutoff"),
            "doubleSided": entry.get("doubleSided"),
        }
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


# version 2: decal instances take the game's own `decalPriority` draw order (audit §57.2).
# version 3: every block carries the game's own draw-distance limit - `m_MinLod` per lane prefab
# and per decal instance - so a tile can be culled the way CS2 culls it (A5, `610998d`; the
# literal was bumped and this line was not, which is why the version gate could not see it).
# -- the game's own draw-distance limit, per lane prefab and per decal instance -------------------
#
# CS2 culls a marking by its PREFAB's `NetLaneGeometryData.m_MinLod` (`csiibundle.lod`), which is
# the min over the prefab's meshes of the flat-thing rule on `Size(bounds).xy` plus that mesh's
# triangle-area bias. A tile merges thousands of markings, so the limit rides into the tile with
# each primitive (`TileSink.add(min_lod=...)`) and a consumer rebuilds the visible set per
# distance. Without this a lane line drawn to 406 m in CS2 is drawn to the whole view distance.


def _lane_prefab_min_lod(prefab, entry, sources, lay, cache):
    """`NetLaneGeometryData.m_MinLod` for one lane prefab — the min over ALL its parts, decal and
    non-decal alike, because that is the set the game takes the min over."""
    if prefab in cache:
        return cache[prefab]
    meshes = []
    for part in entry.get("parts", []) or []:
        asset_id = part.get("assetId")
        if not asset_id:
            continue
        if asset_id not in sources:
            path = os.path.join(lay.lane_meshes, asset_id + ".glb")
            sources[asset_id] = (
                lanes.part_source(glb.parse_glb(path)) if os.path.isfile(path) else None
            )
        source = sources[asset_id]
        if source is None or len(source["tris"]) == 0:
            continue
        verts = numpy.asarray(source["verts"], dtype=numpy.float64)
        size = verts.max(axis=0) - verts.min(axis=0)
        meshes.append((size, lod.surface_area(verts, source["tris"])))
    answer = lod.lane_min_lod(meshes) if meshes else None
    cache[prefab] = answer
    return answer


def _decal_min_lod(asset_id, quads, cache):
    """A placed decal is an OBJECT, so it takes the object rule (`lod.mesh_min_lod`): its own
    bounds, clamped axis by axis, with the surface-area bias."""
    if asset_id in cache:
        return cache[asset_id]
    lo = hi = None
    area = 0.0
    for corners, _uvs, _material, node_matrix in quads:
        pts = frames.transform_points(numpy.asarray(node_matrix, dtype=numpy.float64), corners)
        lo = pts.min(axis=0) if lo is None else numpy.minimum(lo, pts.min(axis=0))
        hi = pts.max(axis=0) if hi is None else numpy.maximum(hi, pts.max(axis=0))
        area += (
            lod.surface_area(pts, numpy.array([[0, 1, 2], [0, 2, 3]], dtype=numpy.int64))
            if len(pts) >= 4
            else 0.0
        )
    answer = None if lo is None else lod.mesh_min_lod(hi - lo, area)
    cache[asset_id] = answer
    return answer


register(Product(PRODUCT_NAME, INPUTS, "markings/", run, version=3, options=("ground", "region")))
