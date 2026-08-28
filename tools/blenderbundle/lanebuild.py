"""Bend the §5B v2 lane records along their curves, into one merged object per prefab.

`csiibundle.lanes` is the reference implementation of the bend and `csiibundle.drape` of the
snap onto the road surface; what is here is the Blender side — reading the catalogue, driving
the bend, draping the decal blocks onto whatever they are painted on, and turning the result
into objects. `lanepart` owns the part meshes themselves and the rigid-tile instancers.

The ground-paint ladder these lifts belong to is `csiibundle.decals.LIFT_LADDER` — one table,
because splitting it across files is how it got broken. Its docstring records why the ORDER
matters and the values do not, and why the bend's pre-drape offset is deliberately not a rung
of it:

    0.000  terrain / road surface           the thing being painted on
    0.008  lane_tint                        lane wear tint          ) on the ROAD, draped
    0.014  lane_marking                     markings, crosswalks    )  onto it per vertex
    0.020  area                             §8b surface areas — pavement, driveways, paths
    0.030  instance_decal                   §4.2a decal instances — bay markings, arrows
 Lane decal parts (§5B `decal`) cannot use a fixed lift at all: the game stores lane
curves at the base plane of the carriageway, and the rendered surface crowns ABOVE them —
measured on a full-city rip, the road top near centre lines is a median 7 mm above the lane
path and beats path + the bend lift on 42% of samples, which is why bent markings vanish into
the asphalt. The game never sees the problem because a decal is a projector volume, painted
onto whatever surface it meets. The harness equivalent is a drape: sample the road surface
under the marking and snap onto it (`drape.drape_block`).
"""

import json
import math
import os

import bpy
from csiibundle import bezier, drape, lanes
from csiibundle.reader import read_jsonl

from . import lanepart, lanereport, materiallayer, meshbuild, sceneprep
from .log import LOG


def _read_catalogue(lanes_path, catalogue_path):
    """laneprefabs.json, or None when the layer cannot be built — with the §5B role check.

    Every decal part carries `decalRole`, which is what settles its lift (see
    `lanes.bend_part`). Checked once here rather than per part, and reported rather than
    guessed around — a decal with no role would silently take the wear tint's lower lift and
    bury a marking, the exact class of failure the field was added to end.
    """
    if not os.path.isfile(lanes_path):
        LOG.warn(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")
        return None
    if not os.path.isfile(catalogue_path):
        LOG.warn(f"CONTRACT VIOLATION: lanemeshes.jsonl without {catalogue_path}")
        return None

    with open(catalogue_path, encoding="utf-8") as handle:
        catalogue = json.load(handle)

    roleless = sorted(
        {
            part.get("assetId", "?")
            for entry in catalogue.values()
            for part in entry.get("parts", ())
            if part.get("decal") and not part.get("decalRole")
        }
    )
    if roleless:
        LOG.warn(
            f"CONTRACT VIOLATION: {len(roleless)} decal part(s) carry no §5B decalRole "
            f"({', '.join(roleless[:4])}{' …' if len(roleless) > 4 else ''}) — they take "
            "the wear-tint lift; re-rip the bundle"
        )
    return catalogue


def _bend_parts(record, entry, sink, sources, lane_mesh_dir, markings_only, tally):
    """Bend every part this record names onto its curve, into `sink`."""
    curve = record["curve"]
    curve_length = bezier.length(curve)
    spans = record.get("spans") or [[0.0, 1.0]]
    width_scale = record.get("widthScale", [1.0, 1.0])
    hang = lanes.hang_curve(record, entry, curve_length)

    for index in record.get("parts", []):
        parts = entry.get("parts", [])
        if not 0 <= index < len(parts):
            continue
        if markings_only and not parts[index].get("decal"):
            continue
        source = lanepart.load_source(parts[index]["assetId"], lane_mesh_dir, sources)
        if source is None:
            tally["missing_mesh"] += 1
            continue
        lanes.bend_part(parts[index], source, curve, spans, curve_length, width_scale, hang, sink)


def _bend_records(lanes_path, catalogue, lane_mesh_dir, sources, region, limit, markings_only):
    """Walk lanemeshes.jsonl, bending each record's parts into a per-(prefab, paint) sink.

    Grouped by (prefab, paint): one object per prefab would lose the per-lane colorSet
    (§7.2), and the distinct paints per prefab are few — the variation table is small.
    """
    center, radius = region
    builders = {}
    counts = {}
    tally = dict.fromkeys(("bent", "missing_prefab", "missing_mesh", "region", "limit"), 0)

    for record in read_jsonl(lanes_path):
        entry = catalogue.get(record.get("prefab"))
        if entry is None:
            tally["missing_prefab"] += 1
            continue

        curve = record["curve"]
        if radius is not None and center is not None:
            if math.hypot(curve[0][0] - center[0], curve[0][2] - center[1]) > radius:
                tally["region"] += 1
                continue
        if limit is not None and tally["bent"] >= limit:
            tally["limit"] += 1
            continue

        color_set = record.get("colorSet")
        key = (record["prefab"], json.dumps(color_set, sort_keys=True) if color_set else "")
        sink = builders.setdefault(key, {"blocks": [], "instances": [], "colorSet": color_set})
        _bend_parts(record, entry, sink, sources, lane_mesh_dir, markings_only, tally)

        tally["bent"] += 1
        category = record.get("category", "lane")
        counts[category] = counts.get(category, 0) + 1
        LOG.progress("lanes bent", tally["bent"], None)

    return builders, counts, tally


# One ray-cast per unique WORLD (x, z), not per vertex (see drape.drape_block) — exact
# because it samples each vertex's own laterally-offset position (fix round 1: an
# earlier version deduped per curve STATION and broadcast one centerline sample to a
# whole row, which was wrong for any part with width). The remaining cut versus the old
# per-vertex drape is genuinely-coincident positions (tiling-clip stations, duplicate
# source verts) plus Tier 1c's fewer stations overall — NOT the ~2x L/R dedup a
# station-keyed version would get, since a wide part's left and right edge now almost
# always cast separately, as they must. Progress strides in markings (blocks), not
# vertices or queries: there are two orders of magnitude fewer of them, and the "draped"
# count on the summary line below is the one tools/ab_compare.py's COUNT_MARKERS is
# expected to move (Tier 1c changed how many decal vertices exist at all) — quote old vs
# new there.
def _drape_markings(builders, road_surface, ground_surface):
    """Snap every decal block onto the road (or the terrain under it). Returns the counters.

    The bend loop prints its own progress; everything here (drape ray-casts, lazy
    road-surface BVH builds) is the quiet expensive part of a full-city run — announce it so
    a long silence reads as work, not a hang.
    """
    blocks = [
        block
        for sink in builders.values()
        for block in sink["blocks"]
        if block["drape_eps"] is not None
    ]
    to_drape = sum(len(block["verts"]) for block in blocks)
    if road_surface is not None and to_drape:
        LOG.step(
            f"bending done; draping {len(blocks)} marking(s) "
            f"({to_drape} decal vertex(es)) onto the road surface and "
            f"building {len(builders)} object(s)"
        )

    counters = {"draped": 0, "grounded": 0, "missed": 0, "queries": 0}
    if road_surface is not None:
        for seen, block in enumerate(blocks, 1):
            drape.drape_block(block, road_surface, counters, ground_surface=ground_surface)
            if seen % 5000 == 0:
                LOG.step(f"{seen}/{len(blocks)} marking(s) draped")
    if counters["queries"]:
        LOG.step(f"{counters['queries']} drape ray-cast(s) issued for {to_drape} decal vertex(es)")
    return counters


# A heavily tiled non-decal part (train sleepers/rails, catenary cable — a repeated
# section stamped along the whole curve) can have its LAST tile's clip window fall short
# of a full section: every station past the clip boundary gets clamped to the same arc
# position (see the `numpy.clip` in the tile loop of `lanes.bend_part`), so the vertices
# between them collapse onto one point-and-width axis — geometrically zero area (distinct
# indices), but not a hint of bad data, just where a fixed-size repeating section doesn't
# divide the span evenly. This is NOT specific to the array rewrite: the previous
# `mesh.from_pydata` pipeline built the identical degenerate triangles from the identical
# bend math, it just never noticed (an invisible, zero-area face costs nothing to leave
# in a mesh, so nothing ever caught it) — proven by direct old-vs-new classification, see
# task-6-report.md §5c. `meshbuild.from_arrays` now does notice, and would otherwise print a
# `CONTRACT VIOLATION` per object for this ONE class — zero-area, distinct indices — which
# on a full city is ~2,000 lines about a phenomenon that is not a violation. One
# aggregated line at the end of the phase instead. This does NOT cover a repeated-vertex-
# index face: `meshbuild.from_arrays` reports that class with the loud wording unconditionally,
# from every caller including this one — `degenerate_report` is never consulted for it, so
# a genuine future regression in the bend (not this known, benign tiling-clip artifact)
# still surfaces loudly instead of being swallowed by this aggregate.
def _build_objects(builders, collection):
    """One merged mesh object per (prefab, paint). Returns (built, degenerate totals)."""
    totals = {"faces": 0, "objects": 0}

    def report_degenerate(_name, zero_area_count, repeated):
        # `repeated` is always False here: lanes are COMPUTED geometry (source_geometry
        # defaults to False on this call), so `meshbuild.from_arrays` never routes a
        # repeated-vertex-index count through this callback — only the zero-area class is
        # ever downgradable for a computed caller. Asserting it costs nothing and pins the
        # invariant in place if that ever changes.
        assert not repeated
        totals["faces"] += zero_area_count
        totals["objects"] += 1

    built = 0
    for (prefab_key, _), sink in builders.items():
        blocks = [block for block in sink["blocks"] if len(block["tris"])]
        if not blocks:
            continue

        verts, tris, uvs, material_runs = lanepart.object_arrays(blocks)
        verts = verts[:, [0, 2, 1]]  # bundle -> Blender: X stays, Y<->Z swap, no sign flip
        mesh = meshbuild.from_arrays(
            f"lane_{prefab_key}",
            verts,
            tris,
            uv_layers={"UVMap": uvs},
            material_runs=material_runs,
            poly_size=3,
            smooth=False,
            degenerate_report=report_degenerate,
        )
        obj = bpy.data.objects.new(f"lane_{prefab_key}", mesh)
        collection.objects.link(obj)
        if sink["colorSet"] is not None:
            materiallayer.set_paint(obj, {"colorSet": sink["colorSet"]})
        built += 1
    return built, totals


def _instance_tiles(builders, sources, collection):
    """One instancer object per (prefab, paint, part): a point cloud of tile matrices,
    Geometry Nodes instancing one shared part mesh. `set_paint` lands on the instancer, where
    the materials' per-instance attribute reads pick it up exactly as they do for the
    collection-instance empties the prop layer uses."""
    stats = {"tiles": 0, "instancers": 0, "parts": 0, "degenerate": 0}

    def report_degenerate(_name, count, _repeated):
        stats["degenerate"] += count

    part_cache = {}
    group_cache = {}
    parts_collection = None
    for (prefab_key, _), sink in builders.items():
        by_part = {}
        for asset_id, matrix in sink["instances"]:
            by_part.setdefault(asset_id, []).append(matrix)
        for asset_id, matrices in by_part.items():
            if parts_collection is None:
                parts_collection = bpy.data.collections.new("bundle_laneparts")
                bpy.context.scene.collection.children.link(parts_collection)
            part_obj = lanepart.part_object(
                asset_id, sources[asset_id], part_cache, parts_collection, report_degenerate
            )
            instancer = lanepart.instancer_object(
                f"lanetiles_{prefab_key}_{asset_id[:12]}", part_obj, matrices, group_cache
            )
            collection.objects.link(instancer)
            if sink["colorSet"] is not None:
                materiallayer.set_paint(instancer, {"colorSet": sink["colorSet"]})
            stats["tiles"] += len(matrices)
            stats["instancers"] += 1
    if parts_collection is not None:
        # The shared part meshes must never draw on their own — the instancers' Object Info
        # still evaluates them from an excluded collection.
        layer = sceneprep.find_layer_collection(parts_collection)
        if layer is not None:
            layer.exclude = True
    stats["parts"] = len(part_cache)
    return stats


def build_lanes(
    lanes_path,
    catalogue_path,
    lane_mesh_dir,
    collection,
    center=None,
    radius=None,
    limit=None,
    road_surface=None,
    markings_only=False,
    ground_surface=None,
):
    """Bend every lane record's parts along its curve, merged into one object per prefab.

    `markings_only` bends just the decal parts (road markings, wear tint) and skips the
    roadside furniture — the light import for looking at carriageways without the fence,
    hedge and sleeper geometry that dominates a full `--lanes` build.
    """
    catalogue = _read_catalogue(lanes_path, catalogue_path)
    if catalogue is None:
        return 0

    sources = {}
    builders, counts, tally = _bend_records(
        lanes_path, catalogue, lane_mesh_dir, sources, (center, radius), limit, markings_only
    )
    counters = _drape_markings(builders, road_surface, ground_surface)
    built, totals = _build_objects(builders, collection)
    tiles = _instance_tiles(builders, sources, collection)

    lanereport.geometry(builders, sources, tally, built, totals, tiles)
    lanereport.drape_markings(counters, road_surface, ground_surface)
    lanereport.skips(counts, tally, lane_mesh_dir)
    return tally["bent"]
