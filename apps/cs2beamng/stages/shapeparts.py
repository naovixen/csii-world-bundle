"""One glTF mesh state as the parts a `.dae` is written from.

The nodes of a bundle `.glb` become material-tagged triangle runs in BeamNG-local axes, with each
node's own transform baked in. Its own module because both the shape writer and the LOD ladder
read meshes this way, and neither should have to import the other to do it.
"""

import math

import numpy
from csiibundle import glb, lod

from ..beamng import daemesh, matbind


def split_lod_nodes(nodes):
    """`(base_nodes, {level: nodes}, lod_bias)` — a `.glb`'s nodes by `glb.lod_level`.

    Level 0 is what the game draws up close; the numbered levels are the game's own coarser
    meshes (`<node>_LOD<n>`, the producer's `LodMesh` chain), and `lod_bias` is the prefab's
    `LodProperties.m_Bias` off the part node's extras. A bundle ripped before the chain was
    shipped has no numbered nodes and comes back as `(nodes, {}, 0.0)`."""
    base, levels, bias = [], {}, 0.0
    for node in nodes:
        _stem, level = glb.lod_level(node["name"])
        if level > 0:
            levels.setdefault(level, []).append(node)
        else:
            base.append(node)
        extra = (node.get("extras") or {}).get("lodBias")
        if extra:
            bias = float(extra)
    return base, levels, bias


def collect_parts(nodes, aliases, tinted, level_name):
    """Every node of one `.glb` as BeamNG-local mesh parts, plus the bounds and triangle area the
    game's LOD rule needs. Bounds and area are measured in the mesh's own (glTF) space, which is
    where `renderPrefab.bounds` and `CalcSurfaceArea` measure them too — the frame change is a
    rotation and a reflection, so neither number moves."""
    parts, materials_used = [], set()
    lo = numpy.array([math.inf] * 3)
    hi = numpy.array([-math.inf] * 3)
    area = 0.0
    for node in nodes:
        if len(node["tris"]) == 0:
            continue
        matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
        world = numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        lo = numpy.minimum(lo, world.min(axis=0))
        hi = numpy.maximum(hi, world.max(axis=0))
        area += lod.surface_area(world, node["tris"])
        # the node's rotation belongs to its normals as much as to its positions: 166 of the
        # reference bundle's 4,453 mesh-bearing nodes carry one (a boat lying on its side), and
        # lighting them by the unrotated normal is a shading bug with no visible geometry to
        # blame. The matrix is rigid on every bundle mesh, so a plain rotation is the whole
        # inverse transpose.
        normals_in = node["normals"]
        if normals_in is not None:
            normals_in = numpy.asarray(normals_in, dtype=numpy.float64) @ matrix[:3, :3].T
        local, normals = daemesh.glb_to_local(world, normals_in)
        runs = []
        for name, count in node["material_runs"]:
            bound = _bound_material(name, aliases, tinted, level_name)
            materials_used.add(aliases.get(name, name))
            runs.append((bound, count))
        parts.append(
            (local, normals, daemesh.glb_uvs(node["uvs"]), node.get("colors"), node["tris"], runs)
        )
    return parts, lo, hi, area, materials_used


def with_wind_colours(parts):
    """Replace each part's vertex colours with BeamNG's forest wind convention
    (`daemesh.wind_vertex_colours`). Synthesised, cosmetic and off by default."""
    out = []
    for index, (verts, normals, uvs, _colors, tris, runs) in enumerate(parts):
        colours = daemesh.wind_vertex_colours(
            {"verts": verts, "tris": tris, "material_runs": runs}, up_axis=2, seed=index
        )
        out.append((verts, normals, uvs, colours, tris, runs))
    return out


def _bound_material(raw, aliases, tinted, level_name):
    """The material name this shape binds — the paint variant's when this shape is a variant and
    the bake wrote a tinted albedo for that material, else the canonical one."""
    canonical = aliases.get(raw, raw)
    variant_name = tinted.get(raw) or tinted.get(canonical)
    if variant_name:
        return variant_name

    return matbind.mapto(level_name, canonical)
