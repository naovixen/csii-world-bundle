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
The one exception is per-instance paint (contract §7.2): CS2 authors tintable surfaces —
foliage above all — desaturated, and colours them per object at draw time, so the tint *cannot*
live inside a shared `.glb`. Applying `attrs.colorSet` through the material's `colorMask` is a
documented consumer rule, and the harness implements exactly that rule and nothing more; without
it every tree and bush renders the grey the atlas actually stores.

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
    --center interchange         centre on the tallest skyline that has a highway or elevated
                                 segment in it — tall buildings and motorway junctions together
    --networks                   also extrude networks.jsonl          (Phase 5)
    --net-limit N                stop after N network records — the fast way to look
    --net-categories a,b,c       which network categories to extrude, or `all`
                                 (default: road,highway,train,tram,subway,pathway,taxiway,runway)
    --lanes                      also bend lanemeshes.jsonl — cables, fences, hedges assembled
                                 from laneprefabs.json + lanemeshes/ (contract §5B v2; same
                                 region filter as networks)
    --skirts                     also place each building's <assetId>__base.glb foundation
                                 skirt, as authored (the 1 m band; the terrain drop of its
                                 v=0 ring is the consumer rule in contract §4.1)
    --terrain                    also place the §8 terrain under everything: the road-flattened
                                 heightmap as a ground grid (roadbed faces textured from
                                 surface_index.png and sunk slightly, steep faces textured
                                 rock) and the water surface where water_level.png rises above
                                 the ground (same region filter as everything else). The
                                 ground, not the junction floors — junction interiors are the
                                 arms' own node meshes (audit §18)
    --areas                      also place the §8b surface areas from areas.jsonl — house
                                 driveways, parking-lot pavement, park paths: the painted
                                 ground of every meshless prefab
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

# How far a road marking is lifted off the carriageway, in metres of Blender world Z. Enough to
# beat depth precision at city scale, small enough that nothing downstream can care: a 1.5 cm
# kerb-height error is already inside the tolerance of the road surface it sits on.
_DECAL_LIFT = 0.015

# How far terrain faces marked roadbed (§8 surface id 1) sink below their sampled height —
# the game's own clamp direction: its road job pulls ground under a carriageway to the
# MINIMUM of the road's surface band, below the asphalt top, while a consumer's linearly
# interpolated (and possibly stride-subsampled) grid crests through it.
_ROADBED_SINK = 0.15

# Linear-space gain on the ground and surface-area textures. The theme diffuse is authored
# dark (the ripped grass averages sRGB (66, 72, 53) — a faithful rip, verified) and reads as
# it does in-game only under the game's exposure and sky light, neither of which a bare
# Blender scene has. Presentation only: the ripped pixels are untouched on disk.
_GROUND_GAIN = 2.8

# Ground steeper than this (rise over run) is textured rock instead of grass — the gorge
# embankments. The game classifies grass/dirt/rock per pixel on the GPU by slope, height and
# noise (§8: the bundle deliberately does not reproduce that); a plain slope threshold at
# ~40 degrees is the presentation-side stand-in.
_ROCK_SLOPE = 0.84


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


def survey_interchange(instances_path, networks_path, cell=250.0):
    """The tallest place that also has a motorway junction in it.

    Downtown and the motorway are usually not the same place, and looking at either alone leaves
    half the interesting geometry out of frame: junction flares and elevated forms are on the
    motorway, tall meshes and dense props are downtown. This scores only those cells that hold
    highway or elevated network records, by the building skyline over their 3x3 neighbourhood —
    a junction cell often holds no buildings itself, but the one next to it does.

    Returns (x, z) or None if the bundle has no highway records.
    """
    if not os.path.isfile(networks_path):
        return None

    junction_cells = set()
    for record in read_jsonl(networks_path):
        attrs = record.get("attrs", {})
        if attrs.get("category") != "highway" and attrs.get("form") != "elevated":
            continue
        where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
        junction_cells.add((math.floor(where[0] / cell), math.floor(where[2] / cell)))

    if not junction_cells:
        return None

    skyline = {}
    for record in read_jsonl(instances_path):
        if record.get("kind") != "building":
            continue
        x, _, z = record["pos"]
        key = (math.floor(x / cell), math.floor(z / cell))
        skyline[key] = skyline.get(key, 0.0) + skyline_weight(record)

    def neighbourhood(key):
        cx, cz = key
        return sum(skyline.get((cx + dx, cz + dz), 0.0)
                   for dx in (-1, 0, 1) for dz in (-1, 0, 1))

    best = max(junction_cells, key=neighbourhood)
    if neighbourhood(best) <= 0:
        return None
    return (best[0] * cell + cell / 2, best[1] * cell + cell / 2)


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

    slots = ("baseColor", "normal", "mask", "emissive", "colorMask")
    referenced, dangling, mapless, masked = set(), [], 0, 0
    for mat_id, material in materials.items():
        paths = [material[s] for s in slots if s in material]
        if not paths:
            mapless += 1
        if "colorMask" in material:
            masked += 1
        for rel in paths:
            referenced.add(rel)
            if not os.path.isfile(os.path.join(bundle_dir, rel)):
                dangling.append(f"{mat_id}.{rel}")

    print(f"materials: {len(materials)} ({mapless} with no maps), {len(referenced)} distinct textures")

    # colors.json is a palette; colorMask is what says where each channel of it lands. Counting
    # them here is how "did the colour layer actually come out?" gets answered by the harness
    # rather than by opening a texture folder and squinting.
    print(f"materials: {masked} with a colorMask (the map colors.json needs to be applicable)")
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


_TINT_PROPS = ("cs2_color0", "cs2_color1", "cs2_color2")
_TINT_LABEL = "cs2_tint"


def set_paint(obj, attrs):
    """Store a record's resolved paint (contract §7.2) as custom properties on its object.

    The three colours land on the instancing empty (or the placed mesh object, for lane
    geometry), where the material's Attribute nodes read them per instance — one shared
    material, thousands of individually-painted copies, exactly the game's own split between
    prefab data and per-object state. White is the identity for the multiply, so objects
    without a colorSet — older bundles predate the field — render the atlas untouched.
    """
    color_set = (attrs or {}).get("colorSet")
    for i, prop in enumerate(_TINT_PROPS):
        channel = color_set[i] if color_set and i < len(color_set) else None
        if channel:
            obj[prop] = [float(channel[0]), float(channel[1]), float(channel[2]),
                         float(channel[3]) if len(channel) > 3 else 1.0]
        else:
            obj[prop] = [1.0, 1.0, 1.0, 1.0]


def _bundle_material_id(name):
    """Blender suffixes `.001` on every duplicate name; the bundle id is what is left."""
    stem, dot, suffix = name.rpartition(".")
    return stem if dot and suffix.isdigit() else name


def _mix_socket(node, name, outputs=False):
    """Pick a Mix node socket by name among the *enabled* ones.

    A ShaderNodeMix carries float, vector and colour variants of `A`/`B`/`Result` under the
    same names, and name lookup returns the first — the float one. Only the sockets of the
    active data_type are enabled, so enabled-ness is the reliable selector.
    """
    pool = node.outputs if outputs else node.inputs
    return next(s for s in pool if s.name == name and s.enabled)


def apply_color_tints(bundle_dir):
    """Wire contract §7.2 into every imported material that carries a colorMask.

    For each such material: base colour becomes
    `lerp(base, base × colorN, colorMask.{R,G,B})`, chained over the three channels — the
    per-texel selection the contract documents, with the per-instance colours read from the
    `cs2_color*` properties `set_paint` stored. `colorMask.A` is snow coverage and a harness
    city has no weather, so it is ignored. Returns how many materials were rigged.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)

    rigged = 0
    for mat in bpy.data.materials:
        entry = table.get(_bundle_material_id(mat.name))
        if not entry or "colorMask" not in entry:
            continue
        tree = getattr(mat, "node_tree", None)
        if tree is None:
            continue
        if any(node.label == _TINT_LABEL for node in tree.nodes):
            continue                                            # already rigged (--keep-existing rerun)
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is None:
            continue
        base_in = principled.inputs.get("Base Color")
        if base_in is None or not base_in.links:
            continue                                            # no base texture to tint

        mask_path = os.path.join(bundle_dir, entry["colorMask"])
        if not os.path.isfile(mask_path):
            continue                                            # report_materials already flagged it
        image = bpy.data.images.load(mask_path, check_existing=True)
        image.colorspace_settings.name = "Non-Color"

        anchor = principled.location
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image, tex.label = image, _TINT_LABEL
        tex.location = (anchor.x - 1000, anchor.y - 600)
        sep = tree.nodes.new("ShaderNodeSeparateColor")
        sep.label = _TINT_LABEL
        sep.location = (anchor.x - 700, anchor.y - 600)
        tree.links.new(tex.outputs["Color"], sep.inputs["Color"])

        upstream = base_in.links[0].from_socket
        for i, (prop, channel) in enumerate(zip(_TINT_PROPS, ("Red", "Green", "Blue"))):
            attr = tree.nodes.new("ShaderNodeAttribute")
            attr.attribute_type = "INSTANCER"                   # instancer empty first, object itself second
            attr.attribute_name, attr.label = prop, _TINT_LABEL
            attr.location = (anchor.x - 700 + i * 60, anchor.y - 900)
            mix = tree.nodes.new("ShaderNodeMix")
            mix.data_type, mix.blend_type, mix.label = "RGBA", "MULTIPLY", _TINT_LABEL
            mix.location = (anchor.x - 450 + i * 150, anchor.y - 500)
            tree.links.new(sep.outputs[channel], _mix_socket(mix, "Factor"))
            tree.links.new(upstream, _mix_socket(mix, "A"))
            tree.links.new(attr.outputs["Color"], _mix_socket(mix, "B"))
            upstream = _mix_socket(mix, "Result", outputs=True)

        tree.links.new(upstream, base_in)                       # replaces the existing link
        rigged += 1

    return rigged


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
        lanes = layers.get("laneMeshes", "lanemeshes.jsonl")
        lane_meshes = layers.get("laneMeshDir", "lanemeshes/")
    else:
        meshes, instances = "meshes/", "instances.jsonl"
        networks, net_meshes = "networks.jsonl", "networks/"
        lanes, lane_meshes = "lanemeshes.jsonl", "lanemeshes/"

    return (os.path.join(bundle_dir, meshes),
            os.path.join(bundle_dir, instances),
            os.path.join(bundle_dir, networks),
            os.path.join(bundle_dir, net_meshes),
            os.path.join(bundle_dir, lanes),
            os.path.join(bundle_dir, lane_meshes),
            os.path.join(bundle_dir, "laneprefabs.json"))


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
        # Lane records carry resolved paint too (contract §5B) — hedges are the visible case.
        set_paint(obj, record.get("attrs"))

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


# -- lane geometry, contract §5B v2 ---------------------------------------------------------
#
# lanemeshes.jsonl records are bend parameters, not baked meshes: the straight part meshes are
# stored once under lanemeshes/, laneprefabs.json says how each lane prefab assembles them, and
# the consumer bends each record's parts along its bézier. The functions below are the
# reference implementation of the contract's bend algorithm, ported from the producer's former
# baking code — which itself reproduced the game's BatchDataSystem.UpdateLaneData /
# BatchInstanceSystem.UpdateLaneInstances. The math is deliberately bpy-free so it can be
# exercised outside Blender.

_LANE_ARC_SAMPLES = 32
_LANE_MAX_TILES = 256


def _bezier_point(c, t):
    u = 1.0 - t
    return tuple(u * u * u * c[0][i] + 3 * u * u * t * c[1][i]
                 + 3 * u * t * t * c[2][i] + t * t * t * c[3][i] for i in range(3))


def _bezier_tangent(c, t):
    u = 1.0 - t
    return tuple(3 * u * u * (c[1][i] - c[0][i]) + 6 * u * t * (c[2][i] - c[1][i])
                 + 3 * t * t * (c[3][i] - c[2][i]) for i in range(3))


def _cut_bezier(c, t0, t1):
    """The sub-segment of a cubic bézier over [t0, t1], by two De Casteljau splits."""
    def split_after(points, t):
        p01 = tuple((1 - t) * points[0][i] + t * points[1][i] for i in range(3))
        p12 = tuple((1 - t) * points[1][i] + t * points[2][i] for i in range(3))
        p23 = tuple((1 - t) * points[2][i] + t * points[3][i] for i in range(3))
        p012 = tuple((1 - t) * p01[i] + t * p12[i] for i in range(3))
        p123 = tuple((1 - t) * p12[i] + t * p23[i] for i in range(3))
        p = tuple((1 - t) * p012[i] + t * p123[i] for i in range(3))
        return (p, p123, p23, points[3]), (points[0], p01, p012, p)

    tail, _ = split_after(c, t0)
    if t1 >= 1.0 - 1e-9:
        return tail
    local = (t1 - t0) / max(1.0 - t0, 1e-9)
    _, head = split_after(tail, local)
    return head


def _arc_table(c):
    """Cumulative arc length at _LANE_ARC_SAMPLES+1 evenly spaced parameters."""
    table = [0.0]
    previous = _bezier_point(c, 0.0)
    for i in range(1, _LANE_ARC_SAMPLES + 1):
        point = _bezier_point(c, i / _LANE_ARC_SAMPLES)
        table.append(table[-1] + math.dist(previous, point))
        previous = point
    return table


def _param_at_arc(table, position):
    """Invert the arc table: the curve parameter at a given arc position."""
    total = table[-1]
    if position <= 0.0:
        return 0.0
    if position >= total:
        return 1.0
    low, high = 0, len(table) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if table[mid] <= position:
            low = mid
        else:
            high = mid
    segment = table[high] - table[low]
    within = (position - table[low]) / segment if segment > 1e-6 else 0.0
    return (low + within) / (len(table) - 1)


def _lane_tile_count(span_length, mesh_length, tiling_count, geometry_tiling):
    """BatchDataHelpers.GetTileCount with the span's length in place of the whole curve's."""
    if tiling_count:
        clip = round(span_length / max(1.0, mesh_length) * tiling_count)
        tiles = (clip + tiling_count - 1) // tiling_count
        return (min(_LANE_MAX_TILES, tiles) if geometry_tiling else min(1, tiles)), clip
    if geometry_tiling:
        return max(1, min(_LANE_MAX_TILES,
                          math.ceil(span_length / max(1.0, mesh_length) - 0.0001))), 0
    return 1, 0


def _hang_drop(control, t):
    u = 1.0 - t
    return (u * u * u * control[0] + 3 * u * u * t * control[1]
            + 3 * u * t * t * control[2] + t * t * t * control[3])


def _quat_apply(q, v):
    """Rotate bundle-frame vector v by bundle quaternion [x, y, z, w]."""
    x, y, z, w = q
    tx = 2 * (y * v[2] - z * v[1])
    ty = 2 * (z * v[0] - x * v[2])
    tz = 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + y * tz - z * ty,
            v[1] + w * ty + z * tx - x * tz,
            v[2] + w * tz + x * ty - y * tx)


def _bend_lane_part(part, source, curve, spans, curve_length, width_scale, hang, sink):
    """Bend one catalogue part along one lane record's curve into `sink`.

    `sink` collects (verts, loops) in bundle frame: verts world positions, loops per triangle
    as (material key, vertex indices, uvs). Everything here mirrors the producer's former
    baking loop, which reproduced what the game's vertex shader does.

    A `decal` part is wear tint and road markings — crosswalks, centre lines, the drive lanes'
    ~12%-alpha darkening — projected over the junction surface the arms' node meshes pave
    (contract §5B, audit §18). Bent here like any part, it is lifted a little above the
    surface so it wins the depth test the projection wins in-game.
    """
    lift = _DECAL_LIFT if part.get("decal") else 0.0
    pos, rot = part["pos"], part["rot"]
    bmin, bmax = part["boundsMin"], part["boundsMax"]
    mesh_length = max(bmax[2] - bmin[2], 0.001)
    tiling_count = part.get("tilingCount", 0)
    geometry_tiling = part.get("geometryTiling", False)
    inverted = part.get("inverted", False)

    for span in spans:
        span_length = curve_length * (span[1] - span[0])
        if span_length <= 0.1:
            continue

        bezier = _cut_bezier(curve, span[0], span[1])
        if inverted:
            bezier = (bezier[3], bezier[2], bezier[1], bezier[0])

        tiles, clip = _lane_tile_count(span_length, mesh_length, tiling_count, geometry_tiling)
        if tiles < 1 or (tiling_count > 0 and clip < 1):
            continue

        arc = _arc_table(bezier)
        total_arc = max(arc[-1], 0.001)

        # A decal part is a projector box the game stretches chord-to-chord along the lane
        # (BuildTransformMatrix's isDecal path) — it never tiles. Mapped by its authored
        # length, a 64 m box left the tail of longer connector lanes unpainted.
        decal_stretch = bool(part.get("decal"))
        if decal_stretch:
            tiles, clip, tiling_count = 1, 0, 0

        for tile in range(tiles):
            if tiling_count > 0:
                clip0 = tile * clip // tiles
                clip1 = (tile + 1) * clip // tiles
                if clip1 <= clip0:
                    continue
                arc_start = total_arc * clip0 / clip
                arc_per_z = total_arc * tiling_count / clip
                arc_limit = total_arc * clip1 / clip
            elif decal_stretch:
                arc_start = 0.0
                arc_per_z = total_arc
                arc_limit = total_arc
            else:
                arc_start = tile * mesh_length
                arc_per_z = mesh_length
                arc_limit = total_arc

            base = len(sink["verts"])
            for v in source["verts"]:
                local = _quat_apply(rot, v)
                local = (local[0] + pos[0], local[1] + pos[1], local[2] + pos[2])
                z_fraction = (local[2] - bmin[2]) / mesh_length
                arc_position = min(max(arc_start + z_fraction * arc_per_z, 0.0), arc_limit)
                t = _param_at_arc(arc, arc_position)
                t_global = (span[1] - (span[1] - span[0]) * t) if inverted \
                    else (span[0] + (span[1] - span[0]) * t)

                point = _bezier_point(bezier, t)
                forward = _bezier_tangent(bezier, t)
                length = math.sqrt(sum(f * f for f in forward)) or 1.0
                forward = tuple(f / length for f in forward)
                # right = normalize(cross(up, forward)) with up = +y (bundle frame)
                right = (forward[2], 0.0, -forward[0])
                length = math.hypot(right[0], right[2]) or 1.0
                right = (right[0] / length, 0.0, right[2] / length)

                width = width_scale[0] + (width_scale[1] - width_scale[0]) * t_global
                world = (point[0] + right[0] * local[0] * width,
                         point[1] + local[1] + lift,
                         point[2] + right[2] * local[0] * width)
                if hang is not None:
                    world = (world[0], world[1] - _hang_drop(hang, t_global), world[2])
                sink["verts"].append(world)

            for material, tri, uvs in source["tris"]:
                sink["loops"].append((material, tuple(base + i for i in tri), uvs))


def _load_lane_source(asset_id, lane_mesh_dir, cache):
    """Import one part .glb once and reduce it to bundle-frame arrays plus its materials.

    The importer hands back Blender-local coordinates (glTF x, -z, y); the bundle frame is
    recovered as (-x, z, -y) of those — the writer's X mirror undone, axes restored.
    """
    if asset_id in cache:
        return cache[asset_id]

    path = os.path.join(lane_mesh_dir, asset_id + ".glb")
    source = None
    if os.path.isfile(path):
        imported = import_glb(path)
        for obj in imported:
            if obj.type != "MESH" or source is not None:
                continue
            mesh = obj.data
            uv_layer = mesh.uv_layers.active
            verts = [(-v.co.x, v.co.z, -v.co.y) for v in mesh.vertices]
            tris = []
            for poly in mesh.polygons:
                indices = list(poly.vertices)
                uvs = [tuple(uv_layer.data[i].uv) if uv_layer else (0.0, 0.0)
                       for i in poly.loop_indices]
                material = mesh.materials[poly.material_index] if mesh.materials else None
                # fan-triangulate the rare n-gon; glb import normally hands triangles
                for k in range(1, len(indices) - 1):
                    tris.append((material,
                                 (indices[0], indices[k], indices[k + 1]),
                                 (uvs[0], uvs[k], uvs[k + 1])))
            source = {"verts": verts, "tris": tris}
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)

    cache[asset_id] = source
    return source


def build_lanes(lanes_path, catalogue_path, lane_mesh_dir, collection,
                center=None, radius=None, limit=None):
    """Bend every lane record's parts along its curve, merged into one object per prefab."""
    if not os.path.isfile(lanes_path):
        print(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")
        return 0
    if not os.path.isfile(catalogue_path):
        print(f"CONTRACT VIOLATION: lanemeshes.jsonl without {catalogue_path}")
        return 0

    with open(catalogue_path, encoding="utf-8") as handle:
        catalogue = json.load(handle)

    sources = {}
    builders = {}
    counts = {}
    bent = missing_prefab = missing_mesh = skipped_region = skipped_limit = 0

    for record in read_jsonl(lanes_path):
        entry = catalogue.get(record.get("prefab"))
        if entry is None:
            missing_prefab += 1
            continue

        curve = record["curve"]
        if radius is not None and center is not None:
            if math.hypot(curve[0][0] - center[0], curve[0][2] - center[1]) > radius:
                skipped_region += 1
                continue
        if limit is not None and bent >= limit:
            skipped_limit += 1
            continue

        table = _arc_table(curve)
        curve_length = table[-1]
        spans = record.get("spans") or [[0.0, 1.0]]
        width_scale = record.get("widthScale", [1.0, 1.0])

        hang = None
        distances = record.get("hang")
        hanging = entry.get("hanging", 0.0)
        if distances is not None or hanging:
            d0, d1 = distances if distances is not None else (0.0, 0.0)
            # The game's quadratic-to-cubic lift of the hanging parabola, at calm air.
            hang = (d0, (d0 + hanging * curve_length) * 2 / 3,
                    (d1 + hanging * curve_length) * 2 / 3, d1)

        # Grouped by (prefab, paint): one object per prefab would lose the per-lane colorSet
        # (§7.2), and the distinct paints per prefab are few — the variation table is small.
        color_set = record.get("colorSet")
        key = (record["prefab"], json.dumps(color_set, sort_keys=True) if color_set else "")
        sink = builders.setdefault(key, {"verts": [], "loops": [], "colorSet": color_set})

        for index in record.get("parts", []):
            parts = entry.get("parts", [])
            if not 0 <= index < len(parts):
                continue
            source = _load_lane_source(parts[index]["assetId"], lane_mesh_dir, sources)
            if source is None:
                missing_mesh += 1
                continue
            _bend_lane_part(parts[index], source, curve, spans, curve_length,
                            width_scale, hang, sink)

        bent += 1
        category = record.get("category", "lane")
        counts[category] = counts.get(category, 0) + 1
        if bent % 5000 == 0:
            print(f"  … {bent} lane(s) bent")

    built = 0
    for (prefab_key, _), sink in builders.items():
        if not sink["loops"]:
            continue

        mesh = bpy.data.meshes.new(f"lane_{prefab_key}")
        # Bundle → Blender is P·(bend), det -1, so faces are emitted reversed to keep their
        # facing — the same compensation the writer applies for its X mirror.
        mesh.from_pydata([bundle_to_blender_point(v) for v in sink["verts"]], [],
                         [tuple(reversed(loop[1])) for loop in sink["loops"]])

        slots = {}
        for material, _, _ in sink["loops"]:
            if material is not None and material.name not in slots:
                slots[material.name] = len(mesh.materials)
                mesh.materials.append(material)

        uv_layer = mesh.uv_layers.new()
        loop_cursor = 0
        for polygon, (material, _, uvs) in zip(mesh.polygons, sink["loops"]):
            if material is not None:
                polygon.material_index = slots[material.name]
            for uv in reversed(uvs):
                uv_layer.data[loop_cursor].uv = uv
                loop_cursor += 1

        mesh.validate()
        obj = bpy.data.objects.new(f"lane_{prefab_key}", mesh)
        collection.objects.link(obj)
        if sink["colorSet"] is not None:
            set_paint(obj, {"colorSet": sink["colorSet"]})
        built += 1

    print()
    print(f"lanes:     {bent} record(s) bent into {built} object(s), "
          f"{len(sources)} part mesh(es) loaded")
    for category, count in sorted(counts.items()):
        print(f"           category {category}: {count}")
    if skipped_region:
        print(f"           {skipped_region} skipped outside the region")
    if skipped_limit:
        print(f"           {skipped_limit} skipped by --net-limit")
    if missing_prefab:
        print(f"           CONTRACT VIOLATION: {missing_prefab} record(s) name a prefab "
              f"missing from laneprefabs.json")
    if missing_mesh:
        print(f"           CONTRACT VIOLATION: {missing_mesh} part reference(s) missing "
              f"their .glb under {lane_mesh_dir}")
    return bent


# -- terrain, contract §8 -------------------------------------------------------------------
#
# The terrain is the Phase 6 acceptance layer: the ground everything else stands on, with the
# road-flattened cells recorded in surface_index.png. (It is NOT what closes junctions — the
# arms' node meshes pave those; audit §18 corrected an earlier claim here.) The rasters are
# decoded in plain Python rather than through Blender's image loader so that no
# colour-management transform can touch the samples — the decoder below is also deliberately a
# reference implementation of §8's file format for any consumer to lift.

def _decode_png_gray(path):
    """Decode a single-channel PNG (§8) to (width, height, samples).

    Samples come back as an array of ints at the file's own bit depth — 16-bit values 0..65535
    for elevation and depth, 8-bit ids for the surface index. All five standard scanline
    filters are handled; the producer writes filter 0 (None) rows, which is the fast path.
    """
    import zlib
    from array import array

    data = open(path, "rb").read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{path} is not a PNG")

    pos = 8
    width = height = depth = None
    colour_type = None
    idat = []
    while pos + 8 <= len(data):
        (length,) = __import__("struct").unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = __import__("struct").unpack(">II", payload[:8])
            depth, colour_type = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break

    if colour_type != 0 or depth not in (8, 16):
        raise ValueError(f"{path}: contract §8 rasters are 8- or 16-bit grayscale, "
                         f"got colour type {colour_type} at depth {depth}")

    raw = zlib.decompress(b"".join(idat))
    bpp = depth // 8
    stride = width * bpp
    out = bytearray(width * height * bpp)
    previous = bytearray(stride)

    for y in range(height):
        offset = y * (stride + 1)
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        if filter_type == 1:    # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                corner = previous[i - bpp] if i >= bpp else 0
                estimate = left + up - corner
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
                predictor = (left, up, corner)[distances.index(min(distances))]
                line[i] = (line[i] + predictor) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        previous = line

    if depth == 16:
        samples = array("H", bytes(out))
        if sys.byteorder == "little":
            samples.byteswap()
    else:
        samples = array("B", bytes(out))
    return width, height, samples


def _terrain_paths(bundle_dir):
    """The §8 layer paths and the `rasters` geo-reference block.

    Reads bundle.json when it exists; otherwise the conventional layout plus the producer's
    pre-manifest terrain/rasters.json sidecar (§8).
    """
    manifest_path = os.path.join(bundle_dir, "bundle.json")
    layers = {}
    rasters = None
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        layers = manifest.get("layers", {})
        rasters = manifest.get("rasters")

    heightmap = os.path.join(bundle_dir, layers.get("terrain", "terrain/heightmap.png"))
    surface = os.path.join(bundle_dir, layers.get("surfaceIndex", "terrain/surface_index.png"))
    water = os.path.join(bundle_dir, layers.get("water", "terrain/water_level.png"))
    surfaces_dir = os.path.join(bundle_dir, layers.get("surfaces", "terrain/surfaces/"))

    if rasters is None:
        sidecar = os.path.join(bundle_dir, "terrain", "rasters.json")
        if os.path.isfile(sidecar):
            with open(sidecar, encoding="utf-8") as handle:
                rasters = json.load(handle)

    return heightmap, surface, water, surfaces_dir, rasters


def _ground_material(name, texture_path, fallback_colour, gain=1.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    if texture_path and os.path.isfile(texture_path):
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = bpy.data.images.load(texture_path)
        upstream = image_node.outputs["Color"]
        if gain != 1.0:
            # Presentation gain (_GROUND_GAIN): the authored theme diffuse is dark and only
            # reads right under the game's own exposure. Multiply node, so the texture on
            # disk stays the faithful rip.
            mix = material.node_tree.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs["Fac"].default_value = 1.0
            mix.inputs["Color2"].default_value = (gain, gain, gain, 1.0)
            material.node_tree.links.new(upstream, mix.inputs["Color1"])
            upstream = mix.outputs["Color"]
        material.node_tree.links.new(upstream, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = fallback_colour
    return material


def build_terrain(bundle_dir, collection, center, radius):
    """Place the §8 terrain under everything: ground grid, roadbed split, water surface.

    Returns the number of objects placed (0 when the layer is absent). The region filter crops
    the grid to the same window as everything else; without one, the grid is subsampled to at
    most ~1024 cells per axis so a full 4096² map stays viewable.
    """
    heightmap_path, surface_path, water_path, surfaces_dir, rasters = _terrain_paths(bundle_dir)
    if not os.path.isfile(heightmap_path):
        print(f"no terrain layer at {heightmap_path} — skipping terrain")
        return 0
    if rasters is None:
        print("terrain rasters present but no geo-reference (bundle.json rasters or "
              "terrain/rasters.json) — skipping terrain")
        return 0

    width, height, heights = _decode_png_gray(heightmap_path)
    cell = float(rasters["resolution"])
    origin_x, _, origin_z = rasters["originWorld"]
    elevation_scale = float(rasters.get("elevationScale", 1.0))
    elevation_offset = float(rasters.get("elevationOffset", 0.0))

    surface_ids = None
    if os.path.isfile(surface_path):
        sw, sh, surface_ids = _decode_png_gray(surface_path)
        if (sw, sh) != (width, height):
            print(f"CONTRACT VIOLATION: surface_index is {sw}x{sh}, heightmap {width}x{height}")
            surface_ids = None

    levels = None
    if os.path.isfile(water_path):
        ww, wh, levels = _decode_png_gray(water_path)
        if (ww, wh) != (width, height):
            print(f"CONTRACT VIOLATION: water_level is {ww}x{wh}, heightmap {width}x{height}")
            levels = None

    # The crop window, in raster indices. Row 0 is the northernmost (§3: rows toward -Z).
    if center is not None and radius is not None:
        margin = 2.0 * cell
        col0 = max(0, int((center[0] - radius - margin - origin_x) / cell))
        col1 = min(width - 1, int(math.ceil((center[0] + radius + margin - origin_x) / cell)))
        row0 = max(0, int((origin_z - (center[1] + radius + margin)) / cell))
        row1 = min(height - 1, int(math.ceil((origin_z - (center[1] - radius - margin)) / cell)))
        stride = 1
    else:
        col0, col1, row0, row1 = 0, width - 1, 0, height - 1
        stride = max(1, (max(width, height) + 1023) // 1024)

    if col0 >= col1 or row0 >= row1:
        print("terrain: the region window misses the raster entirely — skipping terrain")
        return 0

    cols = list(range(col0, col1 + 1, stride))
    rows = list(range(row0, row1 + 1, stride))

    def world_y(row, col):
        return heights[row * width + col] * elevation_scale + elevation_offset

    ncols = len(cols)

    # Roadbed faces sink by a small documented drop (§8 consumer note). The game's road job
    # clamps the ground under a carriageway to the MINIMUM of the road's surface band — below
    # the asphalt top — but this grid linearly interpolates 3.5 m samples (and subsamples by
    # stride without a region), so interpolated crests poke through the road meshes above.
    # Sinking in the game's own clamp direction settles it; every vertex touching a roadbed
    # face drops, so the sink bleeds one cell into the smoothing apron rather than tearing a
    # cliff at the boundary.
    def face_roadbed(r, c):
        return surface_ids is not None and surface_ids[rows[r] * width + cols[c]] != 0

    sink = _ROADBED_SINK if surface_ids is not None else 0.0
    vertex_sunk = [False] * (len(rows) * ncols)
    if sink:
        for r in range(len(rows) - 1):
            for c in range(ncols - 1):
                if face_roadbed(r, c):
                    for corner in (r * ncols + c, r * ncols + c + 1,
                                   (r + 1) * ncols + c, (r + 1) * ncols + c + 1):
                        vertex_sunk[corner] = True

    # -- the ground grid -------------------------------------------------------------------
    verts = []
    index = 0
    for row in rows:
        z = origin_z - row * cell
        for col in cols:
            x = origin_x + col * cell
            drop = sink if vertex_sunk[index] else 0.0
            verts.append((x, z, world_y(row, col) - drop))  # Blender: X east, Y north, Z up
            index += 1

    faces = []
    face_rc = []
    for r in range(len(rows) - 1):
        for c in range(ncols - 1):
            a = r * ncols + c
            # Row 0 is the north edge, so winding (a, a+ncols, a+ncols+1, a+1) — south then
            # east — keeps the face normal pointing up in Blender's right-handed frame.
            faces.append((a, a + ncols, a + ncols + 1, a + 1))
            face_rc.append((r, c))

    mesh = bpy.data.meshes.new("bundle_terrain")
    mesh.from_pydata(verts, [], faces)

    grass = _ground_material("terrain_natural", os.path.join(surfaces_dir, "grass.png"),
                             (0.20, 0.30, 0.12, 1.0), gain=_GROUND_GAIN)
    dirt = _ground_material("terrain_roadbed", os.path.join(surfaces_dir, "dirt.png"),
                            (0.35, 0.28, 0.20, 1.0), gain=_GROUND_GAIN)
    rock = _ground_material("terrain_rock", os.path.join(surfaces_dir, "rock.png"),
                            (0.30, 0.28, 0.26, 1.0), gain=_GROUND_GAIN)
    mesh.materials.append(grass)
    mesh.materials.append(dirt)
    mesh.materials.append(rock)

    roadbed_faces = 0
    rock_faces = 0
    for face, (r, c) in zip(mesh.polygons, face_rc):
        if surface_ids is not None and surface_ids[rows[r] * width + cols[c]] != 0:
            face.material_index = 1
            roadbed_faces += 1
            continue
        # Steep natural ground reads as rock (_ROCK_SLOPE) — the gorge embankments. Sampled
        # heights, not the sunk vertices, so the roadbed drop cannot flip a boundary face.
        run_x = max((cols[c + 1] - cols[c]) * cell, 1e-6)
        run_z = max((rows[r + 1] - rows[r]) * cell, 1e-6)
        y00 = world_y(rows[r], cols[c])
        y01 = world_y(rows[r], cols[c + 1])
        y10 = world_y(rows[r + 1], cols[c])
        y11 = world_y(rows[r + 1], cols[c + 1])
        gx = max(abs(y01 - y00), abs(y11 - y10)) / run_x
        gz = max(abs(y10 - y00), abs(y11 - y01)) / run_z
        if math.hypot(gx, gz) > _ROCK_SLOPE:
            face.material_index = 2
            rock_faces += 1

    # World-anchored UVs so the ground textures tile at a plausible density without any
    # per-face unwrap. 8 m per repeat reads roughly like the game's near tiling.
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop in mesh.loops:
        x, y, _ = mesh.vertices[loop.vertex_index].co
        uv_layer.data[loop.index].uv = (x / 8.0, y / 8.0)

    mesh.validate()
    terrain_object = bpy.data.objects.new("bundle_terrain", mesh)
    collection.objects.link(terrain_object)
    placed = 1

    # -- the water surface -----------------------------------------------------------------
    # The water raster is the SURFACE LEVEL in the heightmap's encoding (§8, 0 = dry): water
    # exists where level > ground. A face is placed wherever any corner is wet; the level
    # extends one sim cell past the shoreline (producer dilation), so the surface runs flat
    # into the bank and the ground cuts the shoreline where the game's own shoreline is.
    wet_faces = 0
    if levels is not None:
        water_verts = {}
        water_faces = []

        def water_level(row, col):
            value = levels[row * width + col]
            return value * elevation_scale + elevation_offset if value else None

        def water_vertex(row, col):
            key = (row, col)
            if key not in water_verts:
                level = water_level(row, col)
                # A corner past the dilated ring carries no level; seat it on the ground so
                # the surface closes instead of tearing.
                surface_y = world_y(row, col) if level is None else level
                x = origin_x + col * cell
                z = origin_z - row * cell
                water_verts[key] = (len(water_verts), (x, z, surface_y))
            return water_verts[key][0]

        def is_wet(row, col):
            level = water_level(row, col)
            return level is not None and level > world_y(row, col)

        for r in range(len(rows) - 1):
            for c in range(ncols - 1):
                corner_rows = (rows[r], rows[r + 1])
                corner_cols = (cols[c], cols[c + 1])
                if any(is_wet(cr, cc) for cr in corner_rows for cc in corner_cols):
                    water_faces.append((water_vertex(corner_rows[0], corner_cols[0]),
                                        water_vertex(corner_rows[1], corner_cols[0]),
                                        water_vertex(corner_rows[1], corner_cols[1]),
                                        water_vertex(corner_rows[0], corner_cols[1])))

        if water_faces:
            ordered = [None] * len(water_verts)
            for index, position in water_verts.values():
                ordered[index] = position
            water_mesh = bpy.data.meshes.new("bundle_water")
            water_mesh.from_pydata(ordered, [], water_faces)
            water_material = bpy.data.materials.new("terrain_water")
            water_material.use_nodes = True
            water_bsdf = water_material.node_tree.nodes.get("Principled BSDF")
            water_bsdf.inputs["Base Color"].default_value = (0.08, 0.22, 0.35, 1.0)
            water_bsdf.inputs["Roughness"].default_value = 0.05
            if "Alpha" in water_bsdf.inputs:
                water_bsdf.inputs["Alpha"].default_value = 0.8
            if hasattr(water_material, "blend_method"):
                water_material.blend_method = "BLEND"
            water_mesh.materials.append(water_material)
            water_mesh.validate()
            water_object = bpy.data.objects.new("bundle_water", water_mesh)
            collection.objects.link(water_object)
            placed += 1
            wet_faces = len(water_faces)

    print()
    print(f"terrain:   {len(rows)}x{ncols} sample window of {width}x{height} at {cell:g} m"
          + (f", stride {stride}" if stride > 1 else ""))
    print(f"           {roadbed_faces} roadbed face(s) (sunk {_ROADBED_SINK:g} m), "
          f"{rock_faces} rock face(s), {wet_faces} water face(s)")
    return placed


# Lift for surface areas above the terrain grid, below everything that stands on them. The
# game draws them as terrain-layer decals; here they are thin geometry riding the ground.
_AREA_LIFT = 0.02


def build_areas(bundle_dir, collection, center, radius):
    """Place the §8b surface areas: driveways, parking-lot pavement, park paths.

    These are the painted patches `AreaBatchSystem` draws from area triangles — the visible
    ground of every meshless prefab (parking lots, sports courts, campsites) and of house
    driveways. One object per area prefab, world-anchored UVs, catalogue texture or colour.
    """
    path = os.path.join(bundle_dir, "areas.jsonl")
    if not os.path.isfile(path):
        print("no areas.jsonl — skipping surface areas (bundle predates §8b)")
        return 0

    catalogue = {}
    catalogue_path = os.path.join(bundle_dir, "areaprefabs.json")
    if os.path.isfile(catalogue_path):
        with open(catalogue_path, encoding="utf-8") as handle:
            catalogue = json.load(handle)

    materials = {}

    def area_material(prefab):
        if prefab not in materials:
            entry = catalogue.get(prefab) or {}
            texture = entry.get("texture")
            colour = entry.get("color") or (0.42, 0.40, 0.38, 1.0)
            materials[prefab] = _ground_material(
                f"area_{prefab}",
                os.path.join(bundle_dir, texture) if texture else None,
                (colour[0], colour[1], colour[2], 1.0), gain=_GROUND_GAIN)
        return materials[prefab]

    builders = {}
    placed = skipped_region = dropped_bad = 0
    for record in read_jsonl(path):
        nodes = record.get("nodes") or []
        triangles = record.get("triangles") or []
        if len(nodes) < 3 or not triangles:
            continue

        if radius is not None and center is not None:
            cx = sum(n[0] for n in nodes) / len(nodes)
            cz = sum(n[2] for n in nodes) / len(nodes)
            if math.hypot(cx - center[0], cz - center[1]) > radius:
                skipped_region += 1
                continue

        sink = builders.setdefault(record.get("prefab"), {"verts": [], "faces": []})
        base = len(sink["verts"])
        sink["verts"].extend((n[0], n[2], n[1] + _AREA_LIFT) for n in nodes)
        for tri in triangles:
            if max(tri) >= len(nodes) or min(tri) < 0:
                dropped_bad += 1
                continue
            # Bundle → Blender flips handedness (§2.2), so faces reverse to keep facing up —
            # the same compensation the lane bend applies.
            sink["faces"].append(tuple(base + i for i in reversed(tri)))
        placed += 1

    built = 0
    for prefab, sink in builders.items():
        if not sink["faces"]:
            continue
        mesh = bpy.data.meshes.new(f"area_{prefab}")
        mesh.from_pydata(sink["verts"], [], sink["faces"])
        mesh.materials.append(area_material(prefab))
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            x, y, _ = mesh.vertices[loop.vertex_index].co
            uv_layer.data[loop.index].uv = (x / 4.0, y / 4.0)
        mesh.validate()
        obj = bpy.data.objects.new(f"area_{prefab}", mesh)
        collection.objects.link(obj)
        built += 1

    print()
    print(f"areas:     {placed} surface area(s) into {built} object(s) "
          f"over {len(builders)} prefab(s)"
          + (f", {skipped_region} outside the region" if skipped_region else ""))
    if dropped_bad:
        print(f"           CONTRACT VIOLATION: {dropped_bad} triangle(s) index out of range")
    return built


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
    do_lanes = False
    do_skirts = False
    do_terrain = False
    do_areas = False
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
        elif arg == "--lanes":
            do_lanes = True
            i += 1
        elif arg == "--skirts":
            do_skirts = True
            i += 1
        elif arg == "--terrain":
            do_terrain = True
            i += 1
        elif arg == "--areas":
            do_areas = True
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
            if argv[i + 1] in ("auto", "downtown", "interchange"):
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
        do_lanes = True
        do_skirts = True
        do_terrain = True
        do_areas = True
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

    (mesh_dir, instances_path, networks_path, net_mesh_dir,
     lanes_path, lane_mesh_dir, lane_prefabs_path) = resolve_layers(bundle_dir)
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

    if center == "interchange":
        found = survey_interchange(instances_path, networks_path)
        if found is None:
            print("--center interchange needs highway or elevated network records with buildings "
                  "near them; use --center downtown, or give explicit X Z")
            return 2
        center = found
        print(f"           --center interchange resolved to X {center[0]:.0f} Z {center[1]:.0f}"
              f"   (tallest skyline with a highway junction in it)")

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
    decal_collection = bpy.data.collections.new("bundle_decals")
    bpy.context.scene.collection.children.link(decal_collection)

    # Park both out of the view layer for the placement pass, for the reason given on
    # `parked_out_of_view_layer`: every prefab import deselects the whole view layer, and the
    # instances placed so far are in it. They are put back before anything has to look at them.
    decal_park = parked_out_of_view_layer(decal_collection)
    decal_park.__enter__()
    instance_park = parked_out_of_view_layer(placed_collection)
    sources_park = parked_out_of_view_layer(sources)
    instance_park.__enter__()
    sources_park.__enter__()

    prefabs = {}
    skirt_prefabs = {}
    placed = 0
    skirts_placed = 0
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
        set_paint(empty, record.get("attrs"))

        # Decals go in their own collection. The producer ships a decal as the flat quad it
        # paints rather than as the projector volume it paints through, so these now lie on the
        # carriageway and read as the road markings they are — but they are still paint, not
        # surface, and must never become collision geometry in anything built from this.
        # Separating them keeps that one command away: hide the collection and the markings go,
        # and nothing else moves.
        # （貼花置於獨立集合。產出端輸出的是貼花所繪出的平面四邊形，而非其藉以投影的體積，
        #   故它們現已貼合於車道之上，呈現為其所代表的路面標線——但它們仍是塗料而非路面本身，
        #   在任何據此建構的成果中都絕不可成為碰撞幾何。分開放置使此事僅需一道指令。）
        # Foundation skirts, contract §4.1: <assetId>__base.glb placed at the same transform,
        # for instances resting on the ground. The band is shown as authored (a 1 m plinth);
        # dropping its v=0 ring to the terrain raster is a consumer's job, and this harness has
        # no terrain — so slopes will still show a gap below the band, and that is expected.
        if do_skirts and record.get("kind") == "building":
            flags = record.get("attrs", {}).get("elevationFlags")
            if not flags or flags == ["OnGround"]:
                base_id = asset_id + "__base"
                if base_id not in skirt_prefabs:
                    base_path = os.path.join(mesh_dir, base_id + ".glb")
                    skirt_prefabs[base_id] = (load_prefab_collection(base_id, mesh_dir, sources)
                                              if os.path.isfile(base_path) else None)
                if skirt_prefabs[base_id] is not None:
                    skirt = bpy.data.objects.new(f"{base_id}.{record.get('id', placed)}", None)
                    skirt.instance_type = "COLLECTION"
                    skirt.instance_collection = skirt_prefabs[base_id]
                    skirt.empty_display_size = 0.01
                    skirt.matrix_world = instance_matrix(record)
                    set_paint(skirt, record.get("attrs"))
                    placed_collection.objects.link(skirt)
                    skirts_placed += 1

        if record.get("kind") == "decal":
            # Coplanar paint z-fights the carriageway it sits on, which in the viewport looks
            # like the marking is flickering in and out of the road. A lift far below the
            # tolerance of anything downstream costs nothing and settles it.
            # （與其所在車道共面的塗料會產生深度衝突，於視埠中看似標線在路面上忽隱忽現。
            #   遠低於下游任何容差的微小抬升毫無代價，即可解決此問題。）
            empty.matrix_world = Matrix.Translation((0.0, 0.0, _DECAL_LIFT)) @ empty.matrix_world
            decal_collection.objects.link(empty)
        else:
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

    lanes = 0
    if do_lanes:
        # Contract §5B v2: lanemeshes.jsonl carries bend parameters, laneprefabs.json the part
        # catalogue, lanemeshes/ the straight part meshes stored once. The bend runs here —
        # this importer is the contract's reference implementation of it.
        if os.path.isfile(lanes_path):
            lane_collection = bpy.data.collections.new("bundle_lanemeshes")
            bpy.context.scene.collection.children.link(lane_collection)
            print()
            print("lane geometry (lanemeshes.jsonl):")
            with parked_out_of_view_layer(lane_collection):
                lanes = build_lanes(lanes_path, lane_prefabs_path, lane_mesh_dir,
                                    lane_collection, center=center, radius=radius,
                                    limit=net_limit)
        else:
            print(f"no lanemeshes.jsonl at {lanes_path} — skipping lane geometry")

    terrain_objects = 0
    if do_terrain:
        terrain_collection = bpy.data.collections.new("bundle_terrain")
        bpy.context.scene.collection.children.link(terrain_collection)
        terrain_objects = build_terrain(bundle_dir, terrain_collection, center, radius)

    area_objects = 0
    if do_areas:
        area_collection = bpy.data.collections.new("bundle_areas")
        bpy.context.scene.collection.children.link(area_collection)
        area_objects = build_areas(bundle_dir, area_collection, center, radius)

    # Everything is imported, so the placed instances can come back into the view layer. This is
    # deliberately after the networks: an instance parked here is an instance the segment imports
    # do not have to deselect, and there are two orders of magnitude more of them than there are
    # segments. Un-parking before the network pass put 40,500 empties back in front of every
    # segment import and took each one from 0.06 s to 1.5 s.
    decal_park.__exit__()
    instance_park.__exit__()
    sources_park.__exit__()

    # Every .glb is in, so every material exists: apply the per-instance paint rule (§7.2).
    tinted = apply_color_tints(bundle_dir)
    if tinted:
        print(f"tinted:    {tinted} material(s) wired to per-instance colorSet via their colorMask")

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
                  (network_collection if networks else
                   (lane_collection if lanes else placed_collection)))

    elapsed = time.time() - started
    print()
    print(f"placed {placed} instance(s) from {len(prefabs)} prefab(s) in {elapsed:.1f} s")
    if do_skirts:
        print(f"placed {skirts_placed} foundation skirt(s) (__base, as-authored 1 m bands — "
              f"the terrain drop is the consumer rule, contract §4.1)")
    if do_terrain:
        print(f"placed {terrain_objects} terrain object(s) (ground grid + water; §8)")
    if do_areas:
        print(f"placed {area_objects} surface-area object(s) (driveways, lots, paths; §8b)")
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
