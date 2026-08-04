#!/usr/bin/env python3
"""Reconstruct a CS2 World Bundle in Blender — the PLAN Phase 3 acceptance harness.

This is a *test* tool, not a product. The repo's job is to produce a correct bundle; turning
one into a scene is a consumer's job and explicitly out of scope (see CLAUDE.md). This script
exists only to answer the acceptance question by eye: does `instances.jsonl` + `meshes/`
actually reconstruct the city? It reads nothing but the documented contract, which is also
what makes it a useful check — if it needs inside knowledge of the mod, the contract is wrong.

    blender --python tools/blender_import_bundle.py -- <bundle_dir> [options]

Options (after the `--`):
    --kinds building,tree,prop   which instance kinds to place        (default: building)
    --limit N                    stop after N instances               (default: 20000)
    --center X Z --radius R      only place within R metres of a point, bundle-frame metres
    --keep-existing              don't wipe the scene first
    --save out.blend             write a .blend and exit, so the import is paid for once

Examples:
    # the 1:1 building check — start here, it is fast and the most diagnostic
    blender --python tools/blender_import_bundle.py -- "…/spike" --kinds building

    # a textured-looking neighbourhood: everything within 300 m of the city centre
    blender --python tools/blender_import_bundle.py -- "…/spike" \\
        --kinds building,tree,prop --center 0 0 --radius 300

Placing all 1.3M instances of a full city is deliberately not the default: Blender handles the
geometry fine (every instance is a collection instance sharing one mesh) but not that many
objects in the depsgraph. Filter, or raise --limit knowing what you are asking for.
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
    else:
        meshes, instances = "meshes/", "instances.jsonl"

    return os.path.join(bundle_dir, meshes), os.path.join(bundle_dir, instances)


# -- scene building -------------------------------------------------------------------------

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def load_prefab_collection(asset_id, mesh_dir, sources):
    """Import <asset_id>.glb once and return a Collection holding it, or None.

    Each prefab becomes a collection so that instances can be collection-instanced: one object
    per instance in the scene, one copy of the geometry in memory, and multi-part prefabs keep
    their node hierarchy and per-part offsets for free.
    """
    path = os.path.join(mesh_dir, asset_id + ".glb")
    if not os.path.isfile(path):
        return None

    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.gltf(filepath=path)
    except Exception as exc:                                    # noqa: BLE001 - report and go on
        print(f"  ! {asset_id}: import failed — {exc}")
        return None

    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        return None

    collection = bpy.data.collections.new(f"prefab_{asset_id}")
    sources.children.link(collection)
    for obj in imported:
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        collection.objects.link(obj)

    return collection


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print(__doc__)
        return 1

    bundle_dir = argv[0]
    kinds = {"building"}
    limit = 20000
    center = None
    radius = None
    keep = False
    save_path = None

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--kinds":
            kinds = {k.strip() for k in argv[i + 1].split(",") if k.strip()}
            i += 2
        elif arg == "--limit":
            limit = int(argv[i + 1])
            i += 2
        elif arg == "--center":
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

    mesh_dir, instances_path = resolve_layers(bundle_dir)
    if not os.path.isfile(instances_path):
        print(f"no instances.jsonl at {instances_path}")
        return 2

    declared_gaps = read_declared_gaps(bundle_dir)
    print(f"bundle:    {bundle_dir}")
    print(f"meshes:    {mesh_dir}")
    print(f"kinds:     {sorted(kinds)}   limit: {limit}")
    if radius is not None:
        print(f"region:    within {radius} m of {center}")
    print(f"declared gaps in warnings.jsonl: {len(declared_gaps)}")

    if not keep:
        clear_scene()

    # Sources live in their own collection, excluded from the view layer, so the templates do
    # not render on top of the instances placed from them.
    sources = bpy.data.collections.new("bundle_sources")
    bpy.context.scene.collection.children.link(sources)
    placed_collection = bpy.data.collections.new("bundle_instances")
    bpy.context.scene.collection.children.link(placed_collection)

    prefabs = {}
    placed = 0
    skipped_kind = 0
    skipped_region = 0
    missing = {}
    started = time.time()

    for record in read_jsonl(instances_path):
        if placed >= limit:
            break

        if record.get("kind") not in kinds:
            skipped_kind += 1
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

    # Hide the templates from the view layer without unlinking them; instances still resolve.
    layer_child = bpy.context.view_layer.layer_collection.children.get(sources.name)
    if layer_child is not None:
        layer_child.exclude = True

    elapsed = time.time() - started
    print()
    print(f"placed {placed} instance(s) from {len(prefabs)} prefab(s) in {elapsed:.1f} s")
    print(f"skipped: {skipped_kind} by kind, {skipped_region} outside the region")

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
        bpy.ops.wm.save_as_mainfile(filepath=save_path)
        print(f"saved {save_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
