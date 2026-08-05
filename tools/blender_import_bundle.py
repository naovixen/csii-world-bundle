#!/usr/bin/env python3
"""Reconstruct a CS2 World Bundle in Blender — the PLAN Phase 3, 4 and 5 acceptance harness.

This is a *test* tool, not a product. The repo's job is to produce a correct bundle; turning
one into a scene is a consumer's job and explicitly out of scope (see CLAUDE.md). This script
exists only to answer the acceptance questions by eye: does `instances.jsonl` + `meshes/`
actually reconstruct the city (Phase 3), do the buildings come out textured (Phase 4), and do
the roads reconstruct with elevated and tunnel runs told apart (Phase 5)? It reads nothing but
the documented contract, which is also what makes it a useful check — if it needs inside
knowledge of the mod, the contract is wrong.

Nothing here wires materials up by hand, and that is the point. Contract §6 has each `.glb`
reference its textures by relative path into `textures/`, so Blender's stock glTF importer
resolves them on its own; if the buildings arrive grey, the bundle is at fault, not the script.

    blender --python tools/blender_import_bundle.py -- <bundle_dir> [options]

Options (after the `--`):
    --kinds building,tree,prop   which instance kinds to place        (default: building)
                                 `--kinds none` places no instances at all
    --limit N                    stop after N instances               (default: 20000)
    --assets a,b,c               only place instances whose assetId contains one of these
                                 (case-insensitive substring), e.g. --assets pillar
    --everything R               every kind and every network category, within R metres of
                                 downtown — the whole bundle at neighbourhood scale. A base:
                                 any option named alongside it still wins.
    --center X Z --radius R      only place within R metres of a point, bundle-frame metres
    --center auto --radius R     centre on the densest part of the selected kinds instead
    --center downtown --radius R centre on the tallest 250 m cell of buildings, whatever kinds
                                 are being placed — the useful centre for a mixed look
    --networks                   also extrude networks.jsonl          (Phase 5)
    --net-limit N                stop after N network records — the fast way to look
    --net-categories a,b,c       which network categories to extrude, or `all`
                                 (default: road,highway,train,tram,subway,pathway,taxiway,runway)
    --keep-existing              don't wipe the scene first
    --save out.blend             write a .blend and exit, so the import is paid for once

Examples:
    # the 1:1 building check — start here, it is fast and the most diagnostic
    blender --python tools/blender_import_bundle.py -- "…/spike" --kinds building

    # a dense neighbourhood with its trees and props, wherever the city actually is
    blender --python tools/blender_import_bundle.py -- "…/spike" \\
        --kinds building,tree,prop --center auto --radius 300

    # the Phase 5 check: the road network alone, coloured by form
    blender --python tools/blender_import_bundle.py -- "…/spike" --kinds none --networks

    # everything at once — buildings, trees, props, every network category, all textured —
    # over 500 m of downtown, which is small enough to import repeatedly
    blender --python tools/blender_import_bundle.py -- "…/spike" --everything 500

The map origin is not the city. A save's buildings can sit kilometres off (0,0), so a radius
around a guessed point mostly finds empty ground — which looks exactly like a broken import.
Every run prints the bounds and centroid of the kinds it selected before placing anything, and
`--center auto` picks the densest 250 m cell, so neither has to be guessed at.

Placing all 1.3M instances of a full city is deliberately not the default: Blender handles the
geometry fine (every instance is a collection instance sharing one mesh) but not that many
objects in the depsgraph. Buildings alone are usually only thousands and need no filter at all;
trees and props are the ones that need one.
"""

import json
import math
import os
import sys
import time

import bpy
from mathutils import Matrix, Quaternion, Vector


# -- the frame conversion, contract §2 ------------------------------------------------------
#
# The bundle frame is CS2 world space verbatim: left-handed, Y up, X east, Z north (§2.1).
# Mesh payloads are the one thing converted: every .glb has its vertices mirrored on X so that
# it is right-handed and opens correctly on its own (§2.2). The contract gives the formula:
#
#     v_world = T(pos) · R(rot) · S(scale) · diag(-1,1,1) · v_glb
#
# Two further conversions are Blender's, not the bundle's:
#
#   G   Blender's glTF importer rewrites Y-up to Z-up on import: (x,y,z) → (x,-z,y). So the
#       mesh sitting in Blender is already G·v_glb, and undoing that is the price of letting
#       the importer do its job.
#   P   Blender is Z-up: bundle (x,y,z) → Blender (x,z,y). A transposition, determinant -1,
#       which is exactly what turns the bundle's left-handed frame into Blender's right-handed
#       one — no extra mirror needed anywhere.
#
# So the matrix applied to an imported object is  P · T · R · S · diag(-1,1,1) · G⁻¹, and since
# P is linear it can be split into a translation by P·pos and a constant linear tail:
#
#     P · diag(-1,1,1) · G⁻¹  =  diag(-1,-1,1)     — a 180° turn about Z, no reflection.
#
# It being a proper rotation is the sanity check: the mesh was already made right-handed, so
# reconstructing it in a right-handed application must not mirror anything again.

# P — bundle Y-up to Blender Z-up. A row swap, determinant -1, which is precisely what turns
# the bundle's left-handed frame into Blender's right-handed one.
_P = Matrix(((1, 0, 0, 0),
             (0, 0, 1, 0),
             (0, 1, 0, 0),
             (0, 0, 0, 1)))

# diag(-1,1,1) · G⁻¹ — undo the bundle's X mirror, then undo Blender's importer axis swap.
_UNMIRROR_UNIMPORT = Matrix(((-1, 0, 0, 0),
                             (0, 0, 1, 0),
                             (0, -1, 0, 0),
                             (0, 0, 0, 1)))


def bundle_to_blender_point(pos):
    """Bundle-frame position (X east, Y up, Z north) to Blender (X east, Y north, Z up)."""
    return Vector((pos[0], pos[2], pos[1]))


def instance_matrix(record):
    """The full world matrix for one instance record, ready to assign to matrix_world.

    Composed left to right exactly as contract §2.2 states it, with Blender's two conventions
    bracketing the outside:  P · T · R · S · diag(-1,1,1) · G⁻¹
    """
    pos = record["pos"]
    rot = record.get("rot", [0.0, 0.0, 0.0, 1.0])
    scale = record.get("scale", [1.0, 1.0, 1.0])

    # The bundle stores quaternions [x,y,z,w]; mathutils wants (w,x,y,z). The rotation is
    # applied in bundle coordinates and P converts the result, which is why no term flips sign
    # here — the same quaternion algebra holds in a left-handed basis.
    rotation = Quaternion((rot[3], rot[0], rot[1], rot[2])).to_matrix().to_4x4()
    scaling = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))

    return _P @ Matrix.Translation(Vector(pos)) @ rotation @ scaling @ _UNMIRROR_UNIMPORT


# -- bundle reading -------------------------------------------------------------------------

def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_declared_gaps(bundle_dir):
    """assetIds the producer declared it could not deliver (contract §10)."""
    path = os.path.join(bundle_dir, "warnings.jsonl")
    gaps = {}
    if not os.path.isfile(path):
        return gaps
    for record in read_jsonl(path):
        asset_id = record.get("assetId")
        if isinstance(asset_id, str):
            gaps[asset_id] = record.get("reason", "")
    return gaps


def skyline_weight(record):
    """How much a building counts towards "this cell is downtown".

    The square of its height, so that a cluster of towers outranks a larger number of houses:
    five 60 m towers score 18,000 against a hundred 4 m houses' 1,600, where a plain count puts
    the houses twenty to one. Downtown is what makes a useful test region — it is where the tall
    meshes, the dense props and every road category are — and a plain densest-cell search finds
    suburbia instead, because that is where the object count is.
    """
    height = record.get("attrs", {}).get("height")
    try:
        height = float(height)
    except (TypeError, ValueError):
        height = 0.0
    # A building with no height attribute still counts as one building rather than nothing.
    return max(height, 1.0) ** 2


def survey(instances_path, kinds, cell=250.0, weight=None):
    """Where the selected kinds actually are: bounds, centroid, and the densest cell.

    A pre-pass over instances.jsonl, which costs a few seconds on a full city and saves the far
    more expensive mistake of importing a patch of empty ground and concluding the bundle is
    broken. Returns None when the selection is empty.

    `weight` scores each record's contribution to the densest cell; the default counts them.
    """
    count = 0
    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    sum_x = sum_z = 0.0
    grid = {}

    for record in read_jsonl(instances_path):
        if record.get("kind") not in kinds:
            continue
        x, _, z = record["pos"]
        count += 1
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_z, max_z = min(min_z, z), max(max_z, z)
        sum_x += x
        sum_z += z
        cell_key = (math.floor(x / cell), math.floor(z / cell))
        grid[cell_key] = grid.get(cell_key, 0) + (weight(record) if weight else 1)

    if count == 0:
        return None

    densest = max(grid, key=grid.get)
    return {
        "count": count,
        "bounds": (min_x, max_x, min_z, max_z),
        "centroid": (sum_x / count, sum_z / count),
        "densest": (densest[0] * cell + cell / 2, densest[1] * cell + cell / 2),
        "densest_count": grid[densest],
    }


def report_materials(bundle_dir):
    """Say what the material layer offers, so a grey scene has a diagnosis rather than a shrug.

    Reads materials.json only to count and to check that its texture paths resolve. The meshes
    reference textures themselves (contract §6), so nothing here feeds the import.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        print("materials: no materials.json — meshes will import untextured")
        return

    with open(path, encoding="utf-8") as handle:
        materials = json.load(handle)

    slots = ("baseColor", "normal", "mask", "emissive")
    referenced, dangling, mapless = set(), [], 0
    for mat_id, material in materials.items():
        paths = [material[s] for s in slots if s in material]
        if not paths:
            mapless += 1
        for rel in paths:
            referenced.add(rel)
            if not os.path.isfile(os.path.join(bundle_dir, rel)):
                dangling.append(f"{mat_id}.{rel}")

    print(f"materials: {len(materials)} ({mapless} with no maps), {len(referenced)} distinct textures")
    if dangling:
        # A path in the table with no file behind it is a producer bug: §7 lets a map be absent,
        # but not present-and-broken.
        print(f"CONTRACT VIOLATION: {len(dangling)} texture reference(s) do not resolve: {dangling[:5]}")


def show_materials():
    """Put every 3D viewport into material preview, so 'is it textured?' is a question you can
    answer by looking. Solid shading is the default and would hide the entire Phase 4 result."""
    for area in getattr(bpy.context.screen, "areas", []):
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.shading.type = "MATERIAL"


def resolve_layers(bundle_dir):
    """Read layer paths from bundle.json, falling back to the conventional layout.

    A Phase 3 bundle has no manifest yet — that is Phase 7 — so the fallback is what actually
    runs today, and the manifest path is here so this keeps working once it exists.
    """
    manifest_path = os.path.join(bundle_dir, "bundle.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            layers = json.load(handle).get("layers", {})
        meshes = layers.get("meshes", "meshes/")
        instances = layers.get("instances", "instances.jsonl")
        networks = layers.get("networks", "networks.jsonl")
        net_meshes = layers.get("networkMeshes", "networks/")
    else:
        meshes, instances = "meshes/", "instances.jsonl"
        networks, net_meshes = "networks.jsonl", "networks/"

    return (os.path.join(bundle_dir, meshes),
            os.path.join(bundle_dir, instances),
            os.path.join(bundle_dir, networks),
            os.path.join(bundle_dir, net_meshes))


# -- networks, contract §5 ------------------------------------------------------------------
#
# A network record carries geometry in exactly one of two forms, and §5 is explicit that a
# consumer must implement both. Only the centerline form is implemented here, because that is
# the only one the Carto ripper emits (§5.1) — a mesh-form record is counted and reported
# rather than silently dropped, so the day a producer starts emitting them this says so.

# Colour by `form`, which is the whole Phase 5 acceptance question: an elevated run and a
# tunnel have to be distinguishable without opening the JSON.
_FORM_COLOURS = {
    "normal": (0.22, 0.22, 0.24, 1.0),
    "elevated": (0.20, 0.45, 0.85, 1.0),
    "tunnel": (0.85, 0.30, 0.20, 1.0),
    "bridge": (0.85, 0.70, 0.20, 1.0),
}

_DRIVABLE = ("road", "highway", "train", "tram", "subway", "pathway", "taxiway", "runway")

# Contract §4: the closed `kind` set. Listed rather than derived so that a v2 kind shows up here
# as a deliberate edit instead of silently joining `--everything`.
_ALL_KINDS = ("building", "prop", "tree", "fence", "decal", "poi")

_UNSET = object()


def _segment_normals(clean):
    """The left-hand horizontal unit normal of each segment of a polyline."""
    normals = []
    for a, b in zip(clean, clean[1:]):
        direction = Vector((b.x - a.x, b.y - a.y, 0.0))
        if direction.length < 1e-6:
            direction = Vector((1.0, 0.0, 0.0))
        direction.normalize()
        normals.append(Vector((-direction.y, direction.x, 0.0)))
    return normals


def ribbon(centerline, width, lift, mitre_limit=4.0):
    """Extrude a centerline into a flat ribbon, in Blender coordinates.

    Contract §5: `width` is the *total* width, extruded symmetrically about the line. The
    offset is taken in the horizontal plane only — a banked road is not a thing CS2 builds, and
    a horizontal offset keeps a steep ramp's surface the right width in plan.

    Corners are properly mitred: the offset at a bend is pushed out by 1/cos(θ/2) so the ribbon
    keeps its full width *across* the turn instead of pinching to width·cos(θ/2). Taking the
    average normal alone is the easy version and is visibly wrong exactly where roads matter —
    at the junction ends the producer's centerlines run into. The limit caps a hairpin, where
    the exact mitre goes to infinity.
    """
    points = [bundle_to_blender_point(p) for p in centerline]

    # Duplicate points would give a zero-length direction and a NaN normal.
    clean = [points[0]]
    for point in points[1:]:
        if (point - clean[-1]).length > 1e-4:
            clean.append(point)
    if len(clean) < 2:
        return None

    half = width / 2.0
    normals = _segment_normals(clean)

    verts, faces = [], []
    for i, point in enumerate(clean):
        before = normals[max(i - 1, 0)]
        after = normals[min(i, len(normals) - 1)]

        bisector = before + after
        if bisector.length < 1e-6:
            # A perfect reversal has no bisector; there is no sane mitre, so butt it.
            offset = after * half
        else:
            bisector.normalize()
            scale = min(1.0 / max(bisector.dot(after), 1e-3), mitre_limit)
            offset = bisector * (half * scale)

        lifted = Vector((point.x, point.y, point.z + lift))
        verts.append(lifted - offset)
        verts.append(lifted + offset)

    for i in range(len(clean) - 1):
        a = 2 * i
        faces.append((a, a + 1, a + 3, a + 2))

    return verts, faces


def place_network_mesh(record, mesh_dir, collection):
    """Import <assetId>.glb and place it by the record's pos/rot, contract §2.2.

    Unlike a prefab mesh, a network mesh is unique to its segment, so it is imported once and
    used once — no collection instancing to set up.
    """
    path = os.path.join(mesh_dir, record["assetId"] + ".glb")
    if not os.path.isfile(path):
        return False

    imported = import_glb(path)
    if not imported:
        return False

    matrix = instance_matrix(record)
    for obj in imported:
        collection.objects.link(obj)
        if obj.parent is None:
            obj.matrix_world = matrix @ obj.matrix_world

    return True


def build_networks(networks_path, categories, collection, net_mesh_dir=None, lift=0.05,
                   center=None, radius=None, limit=None):
    """Extrude every centerline record into one merged mesh per (category, form).

    Merged rather than one object per segment: a city has tens of thousands of edges, and
    Blender's depsgraph is the thing that struggles with object count, not with polygons.
    """
    if not os.path.isfile(networks_path):
        print(f"no networks.jsonl at {networks_path} — skipping networks")
        return 0

    groups = {}
    counts = {"form": {}, "category": {}}
    mesh_form = 0
    missing_mesh = 0
    skipped_category = 0
    skipped_region = 0
    skipped_limit = 0
    placed_records = 0
    degenerate = 0
    total = 0

    for record in read_jsonl(networks_path):
        total += 1
        attrs = record.get("attrs", {})
        category = attrs.get("category", "other")
        form = attrs.get("form", "normal")

        if categories is not None and category not in categories:
            skipped_category += 1
            continue

        # Importing a network mesh costs a fixed ~100 ms per file whatever its size, so a whole
        # city is a quarter of an hour of overhead. Both filters exist to make looking at one
        # neighbourhood cheap enough to do repeatedly.
        if radius is not None and center is not None:
            where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
            if math.hypot(where[0] - center[0], where[2] - center[1]) > radius:
                skipped_region += 1
                continue

        if limit is not None and placed_records >= limit:
            skipped_limit += 1
            continue

        placed_records += 1

        if "centerline" not in record:
            # Mesh form: the producer assembled real geometry for this segment. It is placed the
            # same way an instance is, and is unique per segment rather than deduped, so there is
            # no point caching the collection — link the imported objects straight in.
            mesh_form += 1
            if net_mesh_dir is not None:
                placed = place_network_mesh(record, net_mesh_dir, collection)
                if placed:
                    counts["form"][form] = counts["form"].get(form, 0) + 1
                    counts["category"][category] = counts["category"].get(category, 0) + 1
                else:
                    missing_mesh += 1
            continue

        built = ribbon(record["centerline"], float(record["width"]), lift)
        if built is None:
            degenerate += 1
            continue

        verts, faces = built
        key = (category, form)
        if key not in groups:
            groups[key] = ([], [])
        all_verts, all_faces = groups[key]
        base = len(all_verts)
        all_verts.extend(verts)
        all_faces.extend(tuple(index + base for index in face) for face in faces)

        counts["form"][form] = counts["form"].get(form, 0) + 1
        counts["category"][category] = counts["category"].get(category, 0) + 1

    # Blender suffixes a duplicate material name on every import, so 7,380 segments sharing
    # ~170 cross-section pieces produce ~91,000 datablocks that are all the same handful of
    # materials. Collapse them back onto the first of each name: same look, a fraction of the
    # memory, and a .blend that opens.
    if net_mesh_dir is not None:
        canonical = {}
        merged = 0
        for mat in list(bpy.data.materials):
            base = mat.name.rsplit(".", 1)[0] if mat.name.rsplit(".", 1)[-1].isdigit() else mat.name
            if base not in canonical:
                canonical[base] = mat
        for obj in collection.objects:
            if obj.type != "MESH":
                continue
            for slot in obj.material_slots:
                if slot.material is None:
                    continue
                name = slot.material.name
                base = name.rsplit(".", 1)[0] if name.rsplit(".", 1)[-1].isdigit() else name
                keep = canonical.get(base)
                if keep is not None and keep is not slot.material:
                    slot.material = keep
                    merged += 1
        for mat in list(bpy.data.materials):
            if mat.users == 0:
                bpy.data.materials.remove(mat)
        if merged:
            print(f"           merged {merged} duplicate material slot(s) onto "
                  f"{len(bpy.data.materials)} material(s)")

    placed = 0
    for (category, form), (verts, faces) in sorted(groups.items()):
        mesh = bpy.data.meshes.new(f"net_{category}_{form}")
        mesh.from_pydata([tuple(v) for v in verts], [], faces)
        mesh.update()

        material = bpy.data.materials.new(f"net_{form}")
        material.diffuse_color = _FORM_COLOURS.get(form, (0.5, 0.5, 0.5, 1.0))
        # New materials already carry a node tree; setting `use_nodes` is deprecated in 5.x.
        tree = getattr(material, "node_tree", None)
        principled = tree.nodes.get("Principled BSDF") if tree is not None else None
        if principled is not None:
            principled.inputs["Base Color"].default_value = material.diffuse_color
            principled.inputs["Roughness"].default_value = 0.9
        mesh.materials.append(material)

        obj = bpy.data.objects.new(f"net_{category}_{form}", mesh)
        collection.objects.link(obj)
        placed += len(faces)

    print()
    print(f"networks:  {total} record(s) read, {sum(counts['category'].values())} extruded "
          f"into {len(groups)} mesh(es), {placed} quad(s)")
    for form, count in sorted(counts["form"].items()):
        print(f"           form {form}: {count}")
    for category, count in sorted(counts["category"].items()):
        print(f"           category {category}: {count}")
    if skipped_category:
        print(f"           {skipped_category} skipped by --net-categories")
    if skipped_region:
        print(f"           {skipped_region} skipped outside the region")
    if skipped_limit:
        print(f"           {skipped_limit} skipped by --net-limit")
    if degenerate:
        print(f"           {degenerate} centerline(s) collapsed to a point")
    if mesh_form:
        print(f"           {mesh_form} record(s) use the mesh form")
    if missing_mesh:
        # A referenced mesh that is not on disk is a broken reference, not a style choice.
        print(f"           CONTRACT VIOLATION: {missing_mesh} mesh-form record(s) reference a "
              f"missing .glb under {net_mesh_dir}")

    return sum(counts["category"].values())


# -- scene building -------------------------------------------------------------------------

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


_SCRATCH_NAME = "_carto_import_scratch"


def find_layer_collection(collection, root=None):
    """The LayerCollection for `collection` in the active view layer, or None."""
    root = root or bpy.context.view_layer.layer_collection
    if root.collection is collection:
        return root
    for child in root.children:
        found = find_layer_collection(collection, child)
        if found is not None:
            return found
    return None


class parked_out_of_view_layer:                                 # noqa: N801 - used as a context
    """Keep a collection out of the view layer for the duration of a bulk import.

    The glTF importer ends every file with `bpy.ops.object.select_all(action='DESELECT')`, which
    walks *every object in the view layer* — so once a few thousand segments have landed, each
    further import pays for all of them. Profiled over 500 network meshes it was 70 s of 197 s,
    and it grows with the scene. Objects in an excluded collection are not in the view layer, so
    parking the destination there makes that deselect walk only the handful the current file just
    produced. Everything is linked back at the end and nothing is lost — exclusion hides a
    collection from the view layer, it does not unlink it.
    """

    def __init__(self, collection):
        self.collection = collection
        self.layer = None

    def __enter__(self):
        self.layer = find_layer_collection(self.collection)
        if self.layer is not None:
            self.was_excluded = self.layer.exclude
            self.layer.exclude = True
        return self

    def __exit__(self, *exc):
        if self.layer is not None:
            self.layer.exclude = self.was_excluded
        return False


def _scratch_layer_collection():
    """A collection the glTF importer drops its objects into, so they can be found in O(1).

    The importer links what it creates into the view layer's *active* collection. Pointing that at
    a small scratch collection does two things. It turns "what did this import produce" into
    reading one short list, where the obvious `set(bpy.data.objects)` before/after diff is two
    passes over the whole scene — quadratic across a city, though measurement put it well below
    the costs that actually dominate. More importantly it is what lets the destination collection
    be parked outside the view layer entirely (see `parked_out_of_view_layer`): the importer has
    to link its output *somewhere* in the view layer, and if that somewhere is the scene's master
    collection then the scene grows and the parking buys nothing.
    """
    scene_collection = bpy.context.scene.collection
    scratch = bpy.data.collections.get(_SCRATCH_NAME)
    if scratch is None:
        scratch = bpy.data.collections.new(_SCRATCH_NAME)
    if scratch.name not in scene_collection.children:
        scene_collection.children.link(scratch)
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.collection is scratch:
            return layer
    return None


def import_glb(path):
    """Import one .glb and return the objects it created, unlinked from every collection.

    The caller links them wherever they belong. Returns [] if the import failed, having already
    said why — one unreadable file should not end a city-sized import.
    """
    layer = _scratch_layer_collection()
    if layer is None:
        return []

    view_layer = bpy.context.view_layer
    previous = view_layer.active_layer_collection
    view_layer.active_layer_collection = layer
    try:
        bpy.ops.import_scene.gltf(filepath=path)
    except Exception as exc:                                    # noqa: BLE001 - report and go on
        print(f"  ! {os.path.basename(path)}: import failed — {exc}")
        return []
    finally:
        view_layer.active_layer_collection = previous

    imported = list(layer.collection.objects)
    for obj in imported:
        layer.collection.objects.unlink(obj)
    return imported


def load_prefab_collection(asset_id, mesh_dir, sources):
    """Import <asset_id>.glb once and return a Collection holding it, or None.

    Each prefab becomes a collection so that instances can be collection-instanced: one object
    per instance in the scene, one copy of the geometry in memory, and multi-part prefabs keep
    their node hierarchy and per-part offsets for free.
    """
    path = os.path.join(mesh_dir, asset_id + ".glb")
    if not os.path.isfile(path):
        return None

    imported = import_glb(path)
    if not imported:
        return None

    collection = bpy.data.collections.new(f"prefab_{asset_id}")
    sources.children.link(collection)
    for obj in imported:
        collection.objects.link(obj)

    return collection


def aim_viewports(collection):
    """Point every 3D viewport at what was just placed, and move the far clip plane past it.

    A city is not near Blender's origin and is much bigger than Blender expects. An instance
    at bundle X 1375 Z -1625 lands at Blender (1375, -1625, h), some 2.1 km out, while the
    viewport's far clip defaults to 1000 m — so the import is complete, correct, and entirely
    invisible. Frame All does not rescue it either: framing pushes the camera further back,
    which clips the content harder. Both have to change together, so both are set here rather
    than left as two manual steps to rediscover on every run.

    No-ops under `-b`, where there are no screens to aim.
    """
    objects = list(collection.objects)
    if not objects:
        return

    # Instances are empties, so their origin *is* their position. A network ribbon is one big
    # mesh whose vertices are already world-space, so its origin sits at (0,0,0) and aiming at
    # that would point the camera at the map corner. Use the bounding box where there is one.
    points = []
    for obj in objects:
        if obj.type == "MESH" and len(obj.data.vertices):
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        else:
            points.append(obj.matrix_world.translation)
    if not points:
        return
    centre = Vector((sum(p.x for p in points) / len(points),
                     sum(p.y for p in points) / len(points),
                     sum(p.z for p in points) / len(points)))
    span = max((p - centre).length for p in points)

    aimed = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                # Far enough for a whole city; near plane lifted off 0.1 m to keep some depth
                # precision at that range, but still well under building scale.
                space.clip_start = 0.5
                space.clip_end = 100000.0
                region = space.region_3d
                if region is not None:
                    region.view_location = centre
                    region.view_distance = max(span * 2.5, 100.0)
                aimed += 1

    if aimed:
        print(f"aimed {aimed} viewport(s) at X {centre.x:.0f} Y {centre.y:.0f} Z {centre.z:.0f}, "
              f"clip end 100000")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print(__doc__)
        return 1

    bundle_dir = argv[0]
    kinds = _UNSET
    limit = _UNSET
    center = None
    radius = None
    keep = False
    save_path = None
    do_networks = False
    net_categories = _UNSET
    net_limit = None
    assets = None
    everything = None

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--kinds":
            kinds = {k.strip() for k in argv[i + 1].split(",") if k.strip()}
            if kinds == {"none"}:
                kinds = set()
            i += 2
        elif arg == "--networks":
            do_networks = True
            i += 1
        elif arg == "--net-limit":
            do_networks = True
            net_limit = int(argv[i + 1])
            i += 2
        elif arg == "--net-categories":
            do_networks = True
            value = argv[i + 1].strip()
            net_categories = None if value == "all" else {c.strip() for c in value.split(",") if c.strip()}
            i += 2
        elif arg == "--limit":
            limit = int(argv[i + 1])
            i += 2
        elif arg == "--assets":
            assets = [a.strip().lower() for a in argv[i + 1].split(",") if a.strip()]
            i += 2
        elif arg == "--everything":
            everything = float(argv[i + 1])
            i += 2
        elif arg == "--center":
            if argv[i + 1] in ("auto", "downtown"):
                center = argv[i + 1]
                i += 2
            else:
                center = (float(argv[i + 1]), float(argv[i + 2]))
                i += 3
        elif arg == "--radius":
            radius = float(argv[i + 1])
            i += 2
        elif arg == "--keep-existing":
            keep = True
            i += 1
        elif arg == "--save":
            save_path = os.path.abspath(argv[i + 1])
            i += 2
        else:
            print(f"unknown option {arg!r}")
            return 2

    # `--everything R` is a base, not an override: anything named explicitly still wins, so
    # `--everything 400 --kinds building` is a legal way to say "that region, buildings only".
    if everything is not None:
        do_networks = True
        if kinds is _UNSET:
            kinds = set(_ALL_KINDS)
        if net_categories is _UNSET:
            net_categories = None                               # every category
        if limit is _UNSET:
            limit = None                                        # the radius is the limit
        if center is None:
            center = "downtown"
        if radius is None:
            radius = everything

    if kinds is _UNSET:
        kinds = {"building"}
    if net_categories is _UNSET:
        net_categories = set(_DRIVABLE)
    if limit is _UNSET:
        limit = 20000

    mesh_dir, instances_path, networks_path, net_mesh_dir = resolve_layers(bundle_dir)
    if kinds and not os.path.isfile(instances_path):
        print(f"no instances.jsonl at {instances_path}")
        return 2

    declared_gaps = read_declared_gaps(bundle_dir)
    print(f"bundle:    {bundle_dir}")
    print(f"meshes:    {mesh_dir}")
    print(f"kinds:     {sorted(kinds) or 'none'}   limit: {'none' if limit is None else limit}")
    if assets is not None:
        print(f"assets:    matching {assets}")
    if do_networks:
        print(f"networks:  {networks_path}")
        print(f"           categories: {'all' if net_categories is None else sorted(net_categories)}")

    # Always survey before placing. Knowing where the selection is turns "nothing appeared" from
    # a mystery into an arithmetic error the user can see.
    where = survey(instances_path, kinds) if kinds else None
    if kinds and where is None:
        print(f"no instances of kinds {sorted(kinds)} in this bundle")
        return 2

    if where is not None:
        min_x, max_x, min_z, max_z = where["bounds"]
        print(f"selected:  {where['count']} instance(s)")
        print(f"           X {min_x:.0f} .. {max_x:.0f}   Z {min_z:.0f} .. {max_z:.0f}")
        print(f"           centroid X {where['centroid'][0]:.0f} Z {where['centroid'][1]:.0f}"
              f"   densest 250 m cell X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f}"
              f" ({where['densest_count']} there)")

        if center == "auto":
            center = where["densest"]
            print(f"           --center auto resolved to X {center[0]:.0f} Z {center[1]:.0f}")
    elif center == "auto":
        print("--center auto needs instances to survey; drop it or add --kinds")
        return 2

    # Downtown is decided by buildings whatever is being placed. Centring on the densest cell of
    # a selection that includes trees finds a forest, and of one that includes props finds a car
    # park; and with `--kinds none` there is nothing to survey at all, though a networks-only run
    # still wants to look at the middle of the city.
    if center == "downtown":
        skyline = survey(instances_path, {"building"}, weight=skyline_weight)
        if skyline is None:
            print("--center downtown needs buildings in the bundle; "
                  "use --center auto, or give explicit X Z")
            return 2
        center = skyline["densest"]
        print(f"           --center downtown resolved to X {center[0]:.0f} Z {center[1]:.0f}"
              f"   (tallest 250 m cell of {skyline['count']} building(s))")

    if radius is not None:
        if center is None:
            print("--radius needs --center (or --center auto)")
            return 2
        print(f"region:    within {radius} m of X {center[0]:.0f} Z {center[1]:.0f}")

    print(f"declared gaps in warnings.jsonl: {len(declared_gaps)}")
    report_materials(bundle_dir)

    if not keep:
        clear_scene()

    # Sources live in their own collection, excluded from the view layer, so the templates do
    # not render on top of the instances placed from them.
    sources = bpy.data.collections.new("bundle_sources")
    bpy.context.scene.collection.children.link(sources)
    placed_collection = bpy.data.collections.new("bundle_instances")
    bpy.context.scene.collection.children.link(placed_collection)

    # Park both out of the view layer for the placement pass, for the reason given on
    # `parked_out_of_view_layer`: every prefab import deselects the whole view layer, and the
    # instances placed so far are in it. They are put back before anything has to look at them.
    instance_park = parked_out_of_view_layer(placed_collection)
    sources_park = parked_out_of_view_layer(sources)
    instance_park.__enter__()
    sources_park.__enter__()

    prefabs = {}
    placed = 0
    skipped_kind = 0
    skipped_region = 0
    skipped_asset = 0
    missing = {}
    started = time.time()

    for record in (read_jsonl(instances_path) if kinds else ()):
        if limit is not None and placed >= limit:
            break

        if record.get("kind") not in kinds:
            skipped_kind += 1
            continue

        # Substring rather than exact match: a family like the elevated-road supports spans
        # Vertical_Pillar, Pillar_Top_16, Horizontal_Pillar_12 …, and `--assets pillar` should
        # bring the whole family without having to enumerate it.
        if assets is not None:
            lowered = record["assetId"].lower()
            if not any(a in lowered for a in assets):
                skipped_asset += 1
                continue

        pos = record["pos"]
        if radius is not None and center is not None:
            if math.hypot(pos[0] - center[0], pos[2] - center[1]) > radius:
                skipped_region += 1
                continue

        asset_id = record["assetId"]
        if asset_id not in prefabs:
            prefabs[asset_id] = load_prefab_collection(asset_id, mesh_dir, sources)
            if prefabs[asset_id] is None:
                reason = declared_gaps.get(asset_id, "NOT DECLARED in warnings.jsonl")
                missing[asset_id] = reason

        collection = prefabs[asset_id]
        if collection is None:
            continue

        empty = bpy.data.objects.new(f"{asset_id}.{record.get('id', placed)}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = collection
        empty.empty_display_size = 0.01
        empty.matrix_world = instance_matrix(record)
        placed_collection.objects.link(empty)
        placed += 1

        if placed % 500 == 0:
            print(f"  … {placed} placed, {len(prefabs)} prefabs loaded")

    networks = 0
    if do_networks:
        network_collection = bpy.data.collections.new("bundle_networks")
        bpy.context.scene.collection.children.link(network_collection)
        with parked_out_of_view_layer(network_collection):
            networks = build_networks(networks_path, net_categories, network_collection,
                                      net_mesh_dir, center=center, radius=radius,
                                      limit=net_limit)

    # Everything is imported, so the placed instances can come back into the view layer. This is
    # deliberately after the networks: an instance parked here is an instance the segment imports
    # do not have to deselect, and there are two orders of magnitude more of them than there are
    # segments. Un-parking before the network pass put 40,500 empties back in front of every
    # segment import and took each one from 0.06 s to 1.5 s.
    instance_park.__exit__()
    sources_park.__exit__()

    # The scratch collection is empty by construction — every import unlinks what it produced —
    # so drop it rather than leave an empty entry in the outliner.
    scratch = bpy.data.collections.get(_SCRATCH_NAME)
    if scratch is not None:
        if scratch.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(scratch)
        bpy.data.collections.remove(scratch)

    # Hide the templates from the view layer without unlinking them; instances still resolve.
    layer_child = bpy.context.view_layer.layer_collection.children.get(sources.name)
    if layer_child is not None:
        layer_child.exclude = True

    show_materials()

    # Aim at whatever there is to look at. With `--kinds none --networks` the instance
    # collection is empty, and aiming at an empty collection leaves the viewport at the origin
    # — which is where a city almost never is.
    aim_viewports(placed_collection if placed else
                  (network_collection if networks else placed_collection))

    elapsed = time.time() - started
    print()
    print(f"placed {placed} instance(s) from {len(prefabs)} prefab(s) in {elapsed:.1f} s")
    print(f"skipped: {skipped_kind} by kind, {skipped_asset} by --assets, {skipped_region} outside the region")

    # An empty-looking scene is nearly always the region filter, not the bundle. Say so, with the
    # coordinates that would have worked. The absolute floor matters as much as the ratio: a
    # deliberate 500 m window on a 14 km city keeps well under a twentieth of it by design, and
    # tens of thousands of instances is not an empty scene — pointing that run at `--center auto`
    # would have moved it from downtown to whichever field has the most trees.
    if (radius is not None and where is not None
            and placed < where["count"] / 20 and placed < 500):
        print(f"NOTE: the region kept {placed} of {where['count']} selected instance(s). "
              f"The densest 250 m cell is at X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f} — "
              f"try --center auto, --center downtown, a larger --radius, or drop them.")

    if missing:
        undeclared = [a for a, r in missing.items() if r == "NOT DECLARED in warnings.jsonl"]
        print(f"{len(missing)} assetId(s) had no mesh ({len(undeclared)} of them undeclared):")
        for asset_id, reason in list(missing.items())[:15]:
            print(f"  - {asset_id}: {reason}")
        if undeclared:
            # A gap the producer did not declare is a contract violation, not a missing file:
            # the consumer had no way to know the asset was coming up short.
            print(f"CONTRACT VIOLATION: {len(undeclared)} undeclared: {undeclared[:10]}")

    if save_path:
        # relative_remap=False because the textures stay where the rip put them, under
        # ModsData on the system drive, and a .blend saved anywhere else cannot express that
        # as a relative path. Blender's default is to try anyway and warn once per image --
        # "cannot be made relative" for every one of a few thousand textures, which buries the
        # import's own output. The paths are absolute either way; only the noise differs.
        bpy.ops.wm.save_as_mainfile(filepath=save_path, relative_remap=False)
        print(f"saved {save_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
