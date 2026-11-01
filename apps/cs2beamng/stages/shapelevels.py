"""A shape's level meshes as the bake wrote them: the generated LOD1/LOD2 and the refined copies.

Everything here only LOADS what the bake decided and measured (`bake/lods`); where each level is
drawn is the ladder's business (`shapelods`, `shapechain`).
"""

import hashlib
import os

import numpy
from csiibundle import glb

from ..beamng import daemesh
from .shapeparts import collect_parts


def mesh_digest(mesh):
    """A content hash of a merged, material-grouped mesh: the triangle indices and the vertex
    positions. Two levels built by the same `merge_meshes` + `group_by_material` path from the
    same source bytes hash the same; anything else differs."""
    # Order-independent: `group_by_material` reorders triangles by material name, and a copied
    # level binds its own material names (`TorreAlvear_LOD.001_...`), so the same surface comes
    # out in a different triangle order. Hash the sorted corner positions instead.
    corners = numpy.ascontiguousarray(mesh.verts)[numpy.asarray(mesh.tris)].reshape(-1, 9)
    corners = corners[numpy.lexsort(corners.T[::-1])]
    return hashlib.sha1(numpy.ascontiguousarray(corners).tobytes()).digest()


# The kinds that may ship the bake's REFINED copy of a level in place of the level itself.
#
# Vegetation is exempt for the same reason it is exempt from the displacement switch rule
# (`DISPLACEMENT_RULE_KINDS`): the metric is a surface distance and a canopy is a cloud of
# alpha-tested cards, so moving its leaves measures like moving a wall and reads as nothing - the
# number cannot admit the level honestly. A decal is a projection, not a mesh to thin.
REFINED_KINDS = ("building", "prop")


def refined_level(
    derived_dir, source, shape_name, mesh, level, kind, aliases, tinted, level_name, options, stats
):
    """`mesh`, or the bake's refined copy of that level where one was earned.

    The bake made the decision AND the measurement (`bake/lodrefine._refine_one`: the deepest of three
    MeshLab targets whose measured p95 surface displacement passed the tolerance, each candidate
    judged on its own). This only loads it. Nothing else about the shape moves - the thresholds,
    the bounds and the collider are all computed from the source geometry - so this changes the
    shape's BYTES and not its distances.
    """
    if not options.get("lod0_refine", True) or kind not in REFINED_KINDS:
        return mesh
    path = os.path.join(derived_dir, "lods", f"{source}_LOD{level}R.glb")
    if not os.path.isfile(path):
        return mesh
    nodes = glb.parse_glb(path)
    if not nodes:
        return mesh
    parts, _lo, _hi, _area, _used = collect_parts(nodes, aliases, tinted, level_name)
    if not parts:
        return mesh
    refined = daemesh.group_by_material(daemesh.merge_meshes(shape_name, parts))
    if len(refined.tris) == 0 or len(refined.tris) >= len(mesh.tris):
        return mesh
    refined.colors = None  # the vertex-albedo stream belongs to the far level (C4)
    stats["refinedFrom"] = stats.get("refinedFrom", 0) + len(mesh.tris)
    stats["refinedTo"] = stats.get("refinedTo", 0) + len(refined.tris)
    stats.setdefault("refinedLevels", []).append(level)
    return refined


def load_generated(
    meshes,
    generated,
    stats,
    derived_dir,
    source,
    shape_name,
    aliases,
    tinted,
    level_name,
    allowed=(1, 2),
):
    """Append the bake's LOD1/LOD2 meshes, skipping any it did not write, that came out empty,
    that is the same geometry as the level above it (`identicalGenerated`, counted), or that is
    not in `allowed` (a building's MeshLab-shaped levels only; `generatedRefused`, counted).

    The last is the authored chain's identical-level rule (`authored_chain`, §8.13) applied to
    the bake's own output: a decimator that stalls at one count for two targets
    (LuxuryTowerResidence, 61,594 twice; 182 of the reference bake's 2,625 LOD 2 files) hands
    Torque a switch that redraws the same pixels for the bytes. The bake stops writing those
    (`bake/lodworker._nodes_digest`); this catches them in a bake made before it did.
    """
    for level in (1, 2):
        path = os.path.join(derived_dir, "lods", f"{source}_LOD{level}.glb")
        if not os.path.isfile(path):
            continue
        if level not in allowed:
            stats["generatedRefused"] = stats.get("generatedRefused", 0) + 1
            continue
        nodes = glb.parse_glb(path)
        if not nodes:
            continue
        parts, _lo, _hi, _area, _used = collect_parts(nodes, aliases, tinted, level_name)
        if not parts:
            continue
        mesh = daemesh.group_by_material(daemesh.merge_meshes(f"{shape_name}_LOD{level}", parts))
        if len(mesh.tris) == 0:
            continue
        # The bake samples an albedo per vertex onto its last level for an engine that binds an
        # untextured vertex-colour material to it (`lod_vertcol`); this level binds the shape's
        # own textured materials, which BeamNG shades without vertex colour (no `vertColor`),
        # so the stream was bytes in every `.cdae` for nothing (validation pass 2026-08-31, C4)
        mesh.colors = None
        if mesh_digest(mesh) == mesh_digest(meshes[-1]):
            stats["identicalGenerated"] = stats.get("identicalGenerated", 0) + 1
            continue
        meshes.append(mesh)
        generated.append(level)
        stats["levels"] += 1
        stats["triangles"].append(len(mesh.tris))
