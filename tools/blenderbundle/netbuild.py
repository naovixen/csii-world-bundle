"""Extrude the §5 network layer: one merged mesh per (category, form), plus the mesh-form records.

A network record carries geometry in exactly one of two forms, and §5 is explicit that a
consumer must implement both. Only the centerline form is implemented here, because that is
the only one the ripper emits (§5.1) — a mesh-form record is counted and reported
rather than silently dropped, so the day a producer starts emitting them this says so.
"""

import math
import os

import bpy
from csiibundle import frames, networks
from csiibundle.reader import read_jsonl
from mathutils import Vector

from . import frame, glbimport, materiallayer
from .log import LOG

# Colour by `form`, which is the whole Phase 5 acceptance question: an elevated run and a
# tunnel have to be distinguishable without opening the JSON.
FORM_COLOURS = {
    "normal": (0.22, 0.22, 0.24, 1.0),
    "elevated": (0.20, 0.45, 0.85, 1.0),
    "tunnel": (0.85, 0.30, 0.20, 1.0),
    "bridge": (0.85, 0.70, 0.20, 1.0),
}


def ribbon(centerline, width, lift, mitre_limit=networks.DEFAULT_MITRE_LIMIT):
    """`csiibundle.networks.ribbon`, converted into Blender's frame.

    The mitre maths and the "a ribbon is a stand-in, not a road" caveat both live in the
    package; this only swaps the axes and hands back the list-of-tuples shape `build_networks`
    already merges.
    """
    built = networks.ribbon(centerline, width, lift, mitre_limit=mitre_limit)
    if built is None:
        return None
    verts, quads = built
    return (
        [Vector(frames.bundle_to_zup_point(v)) for v in verts],
        [tuple(int(i) for i in quad) for quad in quads],
    )


def place_network_mesh(record, mesh_dir, collection):
    """Import <assetId>.glb and place it by the record's pos/rot, contract §2.2.

    Unlike a prefab mesh, a network mesh is unique to its segment, so it is imported once and
    used once — no collection instancing to set up.
    """
    path = os.path.join(mesh_dir, record["assetId"] + ".glb")
    if not os.path.isfile(path):
        return False

    imported = glbimport.import_glb(path)
    if not imported:
        return False

    matrix = frame.instance_matrix(record)
    for obj in imported:
        collection.objects.link(obj)
        if obj.parent is None:
            obj.matrix_world = matrix @ obj.matrix_world
        # Lane records carry resolved paint too (contract §5B) — hedges are the visible case.
        materiallayer.set_paint(obj, record.get("attrs"))

    return True


def _in_window(record, center, radius):
    """Is this record inside the region? Importing a network mesh costs a fixed ~100 ms per
    file whatever its size, so a whole city is a quarter of an hour of overhead. Both this and
    the record limit exist to make looking at one neighbourhood cheap enough to do repeatedly."""
    if radius is None or center is None:
        return True
    where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
    return math.hypot(where[0] - center[0], where[2] - center[1]) <= radius


def _read_records(networks_path, categories, collection, net_mesh_dir, lift, center, radius, limit):
    """Walk networks.jsonl once: extrude centerlines into per-(category, form) vertex/face
    accumulators, place the mesh-form records straight into `collection`, and tally the rest."""
    groups = {}
    counts = {"form": {}, "category": {}}
    tally = dict.fromkeys(
        (
            "total",
            "mesh_form",
            "missing_mesh",
            "category",
            "region",
            "limit",
            "placed",
            "degenerate",
        ),
        0,
    )

    for record in read_jsonl(networks_path):
        tally["total"] += 1
        # Label deliberately avoids every ab_compare.py count marker ("networks:",
        # "record(s)", …) — a live counter must never accidentally add a gated line the
        # baseline log does not have, whatever stride the helper throttles it to.
        LOG.progress("networks read", tally["total"], None)
        attrs = record.get("attrs", {})
        category = attrs.get("category", "other")
        form = attrs.get("form", "normal")

        if categories is not None and category not in categories:
            tally["category"] += 1
            continue
        if not _in_window(record, center, radius):
            tally["region"] += 1
            continue
        if limit is not None and tally["placed"] >= limit:
            tally["limit"] += 1
            continue
        tally["placed"] += 1

        if "centerline" not in record:
            _place_mesh_form(record, net_mesh_dir, collection, category, form, counts, tally)
            continue

        built = ribbon(record["centerline"], float(record["width"]), lift)
        if built is None:
            tally["degenerate"] += 1
            continue
        _merge_into_group(groups, (category, form), built)
        counts["form"][form] = counts["form"].get(form, 0) + 1
        counts["category"][category] = counts["category"].get(category, 0) + 1

    return groups, counts, tally


def _place_mesh_form(record, net_mesh_dir, collection, category, form, counts, tally):
    """Mesh form: the producer assembled real geometry for this segment. It is placed the same
    way an instance is, and is unique per segment rather than deduped, so there is no point
    caching the collection — link the imported objects straight in."""
    tally["mesh_form"] += 1
    if net_mesh_dir is None:
        return
    if place_network_mesh(record, net_mesh_dir, collection):
        counts["form"][form] = counts["form"].get(form, 0) + 1
        counts["category"][category] = counts["category"].get(category, 0) + 1
    else:
        tally["missing_mesh"] += 1


def _merge_into_group(groups, key, built):
    """Append one ribbon's vertices and faces to its (category, form) accumulator."""
    verts, faces = built
    if key not in groups:
        groups[key] = ([], [])
    all_verts, all_faces = groups[key]
    base = len(all_verts)
    all_verts.extend(verts)
    all_faces.extend(tuple(index + base for index in face) for face in faces)


def _merge_duplicate_materials(collection):
    """Blender suffixes a duplicate material name on every import, so 7,380 segments sharing
    ~170 cross-section pieces produce ~91,000 datablocks that are all the same handful of
    materials. Collapse them back onto the first of each name: same look, a fraction of the
    memory, and a .blend that opens."""
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
        LOG.step(
            f"merged {merged} duplicate material slot(s) onto {len(bpy.data.materials)} material(s)"
        )


def _place_groups(groups, collection):
    """One object per (category, form), coloured by form. Returns the quad count placed."""
    placed = 0
    for (category, form), (verts, faces) in sorted(groups.items()):
        mesh = bpy.data.meshes.new(f"net_{category}_{form}")
        mesh.from_pydata([tuple(v) for v in verts], [], faces)
        mesh.update()

        material = bpy.data.materials.new(f"net_{form}")
        material.diffuse_color = FORM_COLOURS.get(form, (0.5, 0.5, 0.5, 1.0))
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
    return placed


def _report(groups, counts, tally, placed, net_mesh_dir):
    """The phase's summary and every skip/violation count behind it."""
    LOG.done(
        f"networks:  {tally['total']} record(s) read, {sum(counts['category'].values())} extruded "
        f"into {len(groups)} mesh(es), {placed} quad(s)"
    )
    if net_mesh_dir is not None and tally["mesh_form"]:
        glbimport.counts_log()
    for form, count in sorted(counts["form"].items()):
        LOG.step(f"form {form}: {count}")
    for category, count in sorted(counts["category"].items()):
        LOG.step(f"category {category}: {count}")
    if tally["category"]:
        LOG.step(f"{tally['category']} skipped by --net-categories")
    if tally["region"]:
        LOG.step(f"{tally['region']} skipped outside the region")
    if tally["limit"]:
        LOG.step(f"{tally['limit']} skipped by --net-limit")
    if tally["degenerate"]:
        LOG.warn(f"{tally['degenerate']} centerline(s) collapsed to a point")
    if tally["mesh_form"]:
        LOG.step(f"{tally['mesh_form']} record(s) use the mesh form")
    if tally["missing_mesh"]:
        # A referenced mesh that is not on disk is a broken reference, not a style choice.
        LOG.warn(
            f"CONTRACT VIOLATION: {tally['missing_mesh']} mesh-form record(s) reference a "
            f"missing .glb under {net_mesh_dir}"
        )


def build_networks(
    networks_path,
    categories,
    collection,
    net_mesh_dir=None,
    lift=0.05,
    center=None,
    radius=None,
    limit=None,
):
    """Extrude every centerline record into one merged mesh per (category, form).

    Merged rather than one object per segment: a city has tens of thousands of edges, and
    Blender's depsgraph is the thing that struggles with object count, not with polygons.
    """
    if not os.path.isfile(networks_path):
        LOG.warn(f"no networks.jsonl at {networks_path} — skipping networks")
        return 0

    glbimport.counts_reset()
    groups, counts, tally = _read_records(
        networks_path, categories, collection, net_mesh_dir, lift, center, radius, limit
    )
    if net_mesh_dir is not None:
        _merge_duplicate_materials(collection)
    placed = _place_groups(groups, collection)
    _report(groups, counts, tally, placed, net_mesh_dir)
    return sum(counts["category"].values())
