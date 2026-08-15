"""Reading a bundle `.glb` into arrays, and what to do about the faces that are not faces.

Every mesh in the bundle is a binary glTF 2.0 file (§6) and any glTF loader will open one. This
module exists for the consumer that would rather have numpy arrays than a scene graph: it parses
the container directly, flattens the node hierarchy into world-space transforms, and hands back
per-node vertex/normal/UV/index arrays with the material runs intact.

**It refuses more than it handles, on purpose.** Every `return None` in `parse_glb` is a
deliberate "this file is a shape I have not proven I read correctly" — an extension, a sparse
accessor, an interleaved buffer view, a primitive mode that is not triangles. A consumer's
answer to `None` must be to fall back to a full glTF loader, never to guess or to treat the file
as empty. The bundle's own meshes are uniformly plain, so the fallback is cold in practice; the
guards are there so that the day the producer emits something exotic, it is loud instead of
subtly wrong. The blanket `try/except` serves the same end for a shape nobody anticipated: a
per-file parser must not take a city-sized import down with it.

**Sub-meshes are simultaneous parts, not variants** (§6.1). A node whose glTF mesh has several
primitives — a tree's trunk and leaves, a building's wall and window materials — is ONE entry
here with several material runs, matching what a stock importer builds. Those primitives often
share a single vertex buffer and differ only in which index range they select, so the attribute
accessors are read once per unique POSITION accessor and the runs index into the concatenation.

`clean_degenerate_polygons` is the other half of the module and belongs to no format: it is the
policy for a face that carries no area. Read its docstring before deciding a degenerate face is
someone's bug — for geometry read off a file it is a faithful rip of what the game shipped, and
for geometry a consumer just computed it means the computation broke. The two are not the same
finding and this repo reports them separately.
"""

import json
import re
import struct

import numpy

# GLTF_YUP_TO_ZUP is re-exported: it is the basis a Y-up-to-Z-up loader applies, and a consumer
# reading nodes here needs it in the same breath as the matrices below.
from .frames import (GLTF_YUP_TO_ZUP, quat_matrix,  # noqa: F401
                     scale_matrix, translation_matrix)

# glTF component-type codes, from the spec.
COMPONENT_FLOAT = 5126
COMPONENT_USHORT = 5123
COMPONENT_UINT = 5125

# glTF primitive mode 4 is TRIANGLES. Nothing in the bundle is anything else.
MODE_TRIANGLES = 4

# The attribute set the bundle uses. A file carrying anything beyond these (a second UV set,
# joints, weights) is refused rather than partially read. COLOR_0 is permitted by §6 and is
# read when present (float VEC3/VEC4, or normalised UNSIGNED_BYTE/UNSIGNED_SHORT VEC4 — the
# shapes the spec allows); a bake product may write it (the `lod_vertcol` last LOD).
KNOWN_ATTRIBUTES = {"POSITION", "NORMAL", "TEXCOORD_0", "TANGENT", "COLOR_0"}
COMPONENT_UBYTE = 5121

# §6: LOD n > 0 is a scene node named `<assetId>_LOD<n>`; LOD 0 is the unsuffixed root node.
_LOD_SUFFIX = re.compile(r"^(?P<base>.+)_LOD(?P<level>\d+)$")


def lod_level(node_name):
    """`(base_name, level)` for a node name: `("X", 0)` for `X`, `("X", 2)` for `X_LOD2`.

    A consumer that ignores LODs keeps only level 0 (§6: "A consumer that ignores LODs renders
    only the unsuffixed node"); one that builds a detail chain groups nodes by base name. The
    producer ships LOD 0 only today (`lodCap` 0), so every node is level 0 until it does not.
    """
    match = _LOD_SUFFIX.match(node_name or "")
    if not match:
        return node_name, 0
    return match.group("base"), int(match.group("level"))

_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942


def node_local_matrix(node):
    """One glTF node's local TRS (or explicit `matrix`) as a `(4, 4)` numpy array, glTF space.

    glTF stores an explicit `matrix` COLUMN-major and quaternions as `[x, y, z, w]` — the same
    quaternion order the bundle uses, which is not a coincidence.
    """
    if "matrix" in node:
        m = node["matrix"]
        return numpy.array(m, dtype=numpy.float64).reshape(4, 4).T

    t = node.get("translation", (0.0, 0.0, 0.0))
    r = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    s = node.get("scale", (1.0, 1.0, 1.0))
    return translation_matrix(t) @ quat_matrix(r) @ scale_matrix(s)


def conjugate(matrix, basis):
    """`basis · matrix · basis⁻¹` — a transform re-expressed in another frame.

    A node matrix is a transform, not a point, so moving it between frames is a conjugation and
    not a left-multiply. Getting that wrong puts the geometry in the right place with the wrong
    orientation, which reads as "some buildings are rotated" rather than as a frame bug.
    """
    basis = numpy.asarray(basis, dtype=numpy.float64)
    return basis @ numpy.asarray(matrix, dtype=numpy.float64) @ numpy.linalg.inv(basis)


def parse_glb(path):
    """Parse one `.glb` into per-node arrays, or `None` if anything about it is unproven.

    Returns a list of dicts, one per mesh-bearing node, in scene order::

        {"name": str,
         "matrix": (4, 4) float64 — the node's WORLD transform, glTF space (Y-up, RH),
         "verts": (N, 3) float32,     "normals": (N, 3) float32,
         "uvs":   (N, 2) float32 | None,
         "colors": (N, 4) float32 | None — COLOR_0 as linear RGBA in 0..1 (alpha 1 for VEC3),
         "tris":  (M, 3) int64 — indices into the concatenated vertex arrays,
         "material_runs": [(material_name, triangle_count), ...] in polygon order}

    `verts` are glTF coordinates as stored, which for a bundle mesh means right-handed, Y-up,
    and already mirrored on X (§2.2). `frames.instance_matrix` is what places them.

    An empty list means the file parsed and holds no mesh-bearing node. That is a different
    answer from `None`, and a caller that conflates them will silently accept "zero objects" for
    a file it could not read.
    """
    try:
        return _parse_glb_unguarded(path)
    except Exception:                                            # noqa: BLE001 - refuse, quietly
        return None


def _parse_glb_unguarded(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 12 or data[:4] != b"glTF":
        return None
    _magic, version, total_length = struct.unpack_from("<III", data, 0)
    if version != 2:
        return None

    json_chunk = None
    bin_chunk = None
    pos = 12
    while pos + 8 <= total_length:
        chunk_length, chunk_type = struct.unpack_from("<II", data, pos)
        payload = data[pos + 8:pos + 8 + chunk_length]
        if chunk_type == _CHUNK_JSON:
            json_chunk = payload
        elif chunk_type == _CHUNK_BIN:
            bin_chunk = payload
        pos += 8 + chunk_length
    if json_chunk is None:
        return None
    doc = json.loads(json_chunk)

    if doc.get("extensionsRequired") or doc.get("skins") or doc.get("animations"):
        return None

    meshes = doc.get("meshes", [])
    for mesh in meshes:
        for prim in mesh.get("primitives", []):
            if prim.get("mode", MODE_TRIANGLES) != MODE_TRIANGLES or prim.get("targets"):
                return None
            attrs = prim.get("attributes", {})
            if not {"POSITION", "NORMAL"} <= set(attrs):
                return None
            if set(attrs) - KNOWN_ATTRIBUTES:
                return None                                    # COLOR_0 or anything exotic
            if "indices" not in prim or "material" not in prim:
                return None

    accessors = doc.get("accessors", [])
    for accessor in accessors:
        if accessor.get("sparse"):
            return None

    buffers = doc.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0]:
        return None                                            # embedded BIN chunk only
    if bin_chunk is None or len(bin_chunk) < buffers[0].get("byteLength", 0):
        return None
    buffer_views = doc.get("bufferViews", [])

    def read_accessor(index, gltf_type, component_type, np_dtype, components):
        accessor = accessors[index]
        if accessor.get("normalized") or accessor.get("type") != gltf_type:
            return None
        if accessor["componentType"] != component_type:
            return None
        view = buffer_views[accessor["bufferView"]]
        if view.get("byteStride"):
            return None                                        # interleaved, not this path
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        count = accessor["count"] * components
        if offset + count * numpy.dtype(np_dtype).itemsize > len(bin_chunk):
            return None
        flat = numpy.frombuffer(bin_chunk, dtype=np_dtype, count=count, offset=offset)
        return flat.reshape(accessor["count"], components) if components > 1 else flat

    def read_indices(index):
        accessor = accessors[index]
        if accessor.get("normalized") or accessor.get("type") != "SCALAR":
            return None
        component_type = accessor["componentType"]
        np_dtype = {COMPONENT_USHORT: numpy.uint16, COMPONENT_UINT: numpy.uint32}.get(component_type)
        if np_dtype is None:
            return None
        return read_accessor(index, "SCALAR", component_type, np_dtype, 1)

    def read_colors(index):
        """COLOR_0 as (N, 4) float32 in 0..1, or None for a shape the spec does not allow."""
        accessor = accessors[index]
        gltf_type = accessor.get("type")
        components = {"VEC3": 3, "VEC4": 4}.get(gltf_type)
        if components is None:
            return None
        component_type = accessor["componentType"]
        view = buffer_views[accessor["bufferView"]]
        if view.get("byteStride"):
            return None
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        count = accessor["count"] * components
        if component_type == COMPONENT_FLOAT and not accessor.get("normalized"):
            raw = numpy.frombuffer(bin_chunk, dtype=numpy.float32, count=count, offset=offset)
            values = raw.reshape(-1, components).astype(numpy.float32)
        elif component_type == COMPONENT_UBYTE and accessor.get("normalized"):
            raw = numpy.frombuffer(bin_chunk, dtype=numpy.uint8, count=count, offset=offset)
            values = raw.reshape(-1, components).astype(numpy.float32) / 255.0
        elif component_type == COMPONENT_USHORT and accessor.get("normalized"):
            raw = numpy.frombuffer(bin_chunk, dtype=numpy.uint16, count=count, offset=offset)
            values = raw.reshape(-1, components).astype(numpy.float32) / 65535.0
        else:
            return None
        if components == 3:
            values = numpy.concatenate([values, numpy.ones((len(values), 1), numpy.float32)], axis=1)
        return values

    materials = doc.get("materials", [])

    scenes = doc.get("scenes", [])
    scene_index = doc.get("scene", 0)
    if not scenes or scene_index >= len(scenes):
        return None
    nodes = doc.get("nodes", [])

    mesh_nodes = []                                             # (name, world matrix, mesh index)

    def walk(node_index, parent_matrix):
        node = nodes[node_index]
        world = parent_matrix @ node_local_matrix(node)
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            if mesh_index >= len(meshes) or not meshes[mesh_index].get("primitives"):
                raise ValueError(f"node {node_index} references an empty/missing mesh")
            name = node.get("name") or meshes[mesh_index].get("name") or f"mesh{mesh_index}"
            mesh_nodes.append((name, world, mesh_index))
        for child_index in node.get("children", ()):
            walk(child_index, world)

    for root_index in scenes[scene_index].get("nodes", ()):
        walk(root_index, numpy.identity(4))

    result = []
    for name, world, mesh_index in mesh_nodes:
        prims = meshes[mesh_index]["primitives"]

        # A mesh's primitives can share ONE vertex-attribute buffer — every primitive pointing
        # at the very same POSITION/NORMAL/TEXCOORD_0 accessors and differing only in which
        # face range its `indices` selects — rather than each owning independent geometry.
        # Verified against two real bundle files: AppleTree01.glb (trunk + leaves) and
        # NA_CommercialHigh01_L4_3x4.glb (wall + windows) both do this. Reading the attribute
        # accessors once per unique POSITION accessor, keyed by its index, handles both that
        # case and the "each primitive has its own buffer" case uniformly — the latter is just
        # every POSITION accessor index happening to be unique.
        blocks = {}                                              # POSITION accessor index -> …
        block_order = []
        for prim in prims:
            attrs = prim["attributes"]
            pos_index = attrs["POSITION"]
            pairing = (attrs.get("NORMAL"), attrs.get("TEXCOORD_0"), attrs.get("COLOR_0"))
            if pos_index in blocks:
                if blocks[pos_index][3] != pairing:
                    return None                                  # same POSITION, different NORMAL/UV
                continue
            verts = read_accessor(pos_index, "VEC3", COMPONENT_FLOAT, numpy.float32, 3)
            normals = read_accessor(attrs["NORMAL"], "VEC3", COMPONENT_FLOAT, numpy.float32, 3)
            if verts is None or normals is None or len(verts) != len(normals):
                return None
            uvs = None
            if "TEXCOORD_0" in attrs:
                uvs = read_accessor(attrs["TEXCOORD_0"], "VEC2", COMPONENT_FLOAT, numpy.float32, 2)
                if uvs is None or len(uvs) != len(verts):
                    return None
            colors = None
            if "COLOR_0" in attrs:
                colors = read_colors(attrs["COLOR_0"])
                if colors is None or len(colors) != len(verts):
                    return None
            blocks[pos_index] = (verts, normals, uvs, pairing, colors)
            block_order.append(pos_index)

        has_uv = blocks[block_order[0]][2] is not None
        if any((blocks[i][2] is not None) != has_uv for i in block_order):
            return None                                          # mixed UV presence in one mesh
        has_color = blocks[block_order[0]][4] is not None
        if any((blocks[i][4] is not None) != has_color for i in block_order):
            return None                                          # mixed COLOR_0 presence

        offsets, cursor = {}, 0
        for pos_index in block_order:
            offsets[pos_index] = cursor
            cursor += len(blocks[pos_index][0])

        tri_parts, runs = [], []
        for prim in prims:
            indices = read_indices(prim["indices"])
            if indices is None or len(indices) % 3 != 0:
                return None
            material_index = prim["material"]
            if material_index >= len(materials):
                return None
            material_name = materials[material_index].get("name")
            if not material_name:
                return None

            offset = offsets[prim["attributes"]["POSITION"]]
            tri_parts.append(indices.astype(numpy.int64).reshape(-1, 3) + offset)
            runs.append((material_name, len(indices) // 3))

        result.append({
            "name": name,
            "matrix": world,
            "verts": numpy.concatenate([blocks[i][0] for i in block_order]),
            "normals": numpy.concatenate([blocks[i][1] for i in block_order]),
            "uvs": numpy.concatenate([blocks[i][2] for i in block_order]) if has_uv else None,
            "colors": numpy.concatenate([blocks[i][4] for i in block_order]) if has_color else None,
            "tris": numpy.concatenate(tri_parts),
            "material_runs": runs,
        })

    return result


def glb_uv_to_texture_uv(uvs):
    """glTF's V-down UV convention to the V-up one most renderers use: `v -> 1 - v`.

    Applied per LOOP, not per vertex, when the consumer's mesh format wants per-loop UVs.
    """
    uvs = numpy.asarray(uvs, dtype=numpy.float32)
    return numpy.stack([uvs[:, 0], 1.0 - uvs[:, 1]], axis=1)


# -- the degenerate-face policy ----------------------------------------------------------------

def clean_degenerate_polygons(verts, loops_flat, sizes, uv_layers=None, material_runs=None):
    """Drop polygons that carry no area, keeping materials and UVs aligned to the survivors.

    `verts` is `(N, 3)`; `loops_flat` is a flat array of loop vertex indices; `sizes` is `(M,)`
    per-polygon loop counts summing to `len(loops_flat)`. `uv_layers` is
    `{name: (len(loops_flat), 2)}` in the same loop order; `material_runs` is
    `[(material, polygon_count)]` consumed in polygon order.

    Returns `(loops_flat, sizes, uv_layers, material_runs, repeated_count, zero_area_count)`.

    **Why this is not left to the mesh library.** Every validator this repo has met drops bad
    polygons POSITIONALLY and says nothing, so afterwards a caller has no way to tell which of
    ITS polygons a material run or a UV block was meant for. Re-zipping runs onto whatever
    survived silently mis-assigns materials rather than failing loudly — so the drop happens
    here, before the mesh is built, with the runs and UV blocks trimmed in the same pass.
    Concatenated per-part arrays (lane meshes assembled from bent segments) are exactly the
    shape most likely to contain one at a seam.

    **The two classes are not equally diagnostic and are counted separately:**

    * `repeated_count` — a polygon with the SAME vertex index twice. In geometry a consumer just
      computed (a bend, a grid, a drape) this is the classic sign that the computation broke,
      and it deserves to be loud.
    * `zero_area_count` — distinct indices that still collapse to a line or a point. This one has
      a well-understood non-defect source in computed lane geometry (a heavily tiled part's last
      tile can clip a whole cross-section to a single arc position), and another in geometry read
      verbatim off a `.glb`, where it is a faithful rip of what the game's own mesh contained.

    Neither count is a verdict. What to do with them depends on where the arrays came from,
    which only the caller knows — that is why this function reports and does not warn.
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    loops_flat = numpy.asarray(loops_flat, dtype=numpy.int64)
    sizes = numpy.asarray(sizes, dtype=numpy.int64)
    poly_count = len(sizes)
    if not poly_count:
        return loops_flat, sizes, uv_layers, material_runs, 0, 0

    offsets = numpy.concatenate(([0], numpy.cumsum(sizes)))
    # Fan-triangulated from vertex 0 (v0, v[i], v[i+1]) so the area test covers any polygon
    # size. Sizes are processed group-wise so a mixed-size mesh (the terrain grid: 14 m quads,
    # 3.5 m quads and transition-fan triangles in one buffer) shares the uniform path's
    # arithmetic instead of needing its own.
    repeated = numpy.zeros(poly_count, dtype=bool)
    area2 = numpy.zeros(poly_count)
    for size in numpy.unique(sizes):
        group = numpy.nonzero(sizes == size)[0]
        rows = loops_flat[offsets[group][:, None] + numpy.arange(size)[None, :]]
        rep = numpy.zeros(len(group), dtype=bool)
        for i in range(size):
            for j in range(i + 1, size):
                rep |= rows[:, i] == rows[:, j]
        v0 = verts[rows[:, 0]]
        a2 = numpy.zeros(len(group))
        for i in range(1, size - 1):
            edge_a = verts[rows[:, i]] - v0
            edge_b = verts[rows[:, i + 1]] - v0
            a2 += numpy.linalg.norm(numpy.cross(edge_a, edge_b), axis=1)
        repeated[group] = rep
        area2[group] = a2

    zero_area = (~repeated) & (area2 < 1e-12)          # TUNED: below any real mesh's face area
    degenerate = repeated | zero_area
    repeated_count = int(repeated.sum())
    zero_area_count = int(zero_area.sum())
    if not degenerate.any():
        return loops_flat, sizes, uv_layers, material_runs, 0, 0

    keep = ~degenerate
    if material_runs:
        expanded = []
        for material, count in material_runs:
            expanded.extend([material] * count)
        trimmed = []
        for material, kept in zip(expanded, keep):
            if not kept:
                continue
            if trimmed and trimmed[-1][0] is material:
                trimmed[-1] = (material, trimmed[-1][1] + 1)
            else:
                trimmed.append((material, 1))
        material_runs = trimmed
    loop_keep = numpy.repeat(keep, sizes)
    if uv_layers:
        uv_layers = {layer_name: numpy.asarray(uvs)[loop_keep]
                     for layer_name, uvs in uv_layers.items()}

    return (loops_flat[loop_keep], sizes[keep], uv_layers, material_runs,
            repeated_count, zero_area_count)
