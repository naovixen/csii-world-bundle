"""Writing a bundle-shaped `.glb` from arrays — the inverse of `glb.parse_glb`.

The bake writes its tiled products (§13) as GLB files that obey §6 exactly, so that every reader
that handles a producer mesh handles a derived tile without a special case: binary glTF 2.0, one
buffer in the BIN chunk, tightly packed float32 attribute views (no interleaving, no sparse
accessors), uint32 indices, one primitive per material, materials joined by NAME. That is a
deliberately small subset of glTF and it is the same subset `parse_glb` proves it reads, so
`parse_glb(write_glb(x)) == x` is the gate.

**Frame.** A bundle `.glb` stores positions and normals MIRRORED ON X with triangle winding
reversed (§2.2); `frames.instance_matrix` composes the `diag(-1, 1, 1)` back. Callers holding
bundle-frame geometry pass `from_bundle_frame=True` and this module does the mirror + winding
flip; callers holding glTF-space arrays (read from another `.glb`) pass the default and nothing
is touched. UVs are written as given — glTF's top-left origin, which is what `parse_glb` returns
and what every producer mesh already uses.

Materials are written with their `name` (the join key) and, when the caller supplies a
`materials` table, enough PBR to make a stock loader show something sensible: base colour
texture URI, normal texture URI, alphaMode/alphaCutoff/doubleSided, factors. Textures are
external URIs exactly as the producer writes them (`../textures/<sha>.png` from a subdirectory).
"""

import json
import struct

import numpy

_MAGIC = b"glTF"
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942

COMPONENT_FLOAT = 5126
COMPONENT_UINT = 5125
COMPONENT_UBYTE = 5121
TARGET_ARRAY_BUFFER = 34962
TARGET_ELEMENT_ARRAY_BUFFER = 34963


def _pad4(blob, pad=b"\x00"):
    return blob + pad * (-len(blob) % 4)


class _Buffer:
    """Accumulates tightly packed views into one BIN chunk."""

    def __init__(self):
        self.parts = []
        self.views = []
        self.length = 0

    def add(self, array, target):
        raw = numpy.ascontiguousarray(array).tobytes()
        raw = _pad4(raw)
        view = {"buffer": 0, "byteOffset": self.length, "byteLength": len(raw), "target": target}
        self.parts.append(raw)
        self.views.append(view)
        self.length += len(raw)
        return len(self.views) - 1

    def blob(self):
        return b"".join(self.parts)


def _bundle_to_gltf(verts, normals, tris):
    verts = numpy.array(verts, dtype=numpy.float32, copy=True)
    verts[:, 0] *= -1.0
    if normals is not None:
        normals = numpy.array(normals, dtype=numpy.float32, copy=True)
        normals[:, 0] *= -1.0
    tris = numpy.asarray(tris)[:, [0, 2, 1]]
    return verts, normals, tris


def write_glb(path, nodes, materials=None, from_bundle_frame=False, generator="csiibundle.bake"):
    """Write `nodes` to `path` as a GLB.

    `nodes` is a list of `{"name": str, "primitives": [prim, ...]}`; each `prim` is
    `{"verts": (N,3), "normals": (N,3) | None, "uvs": (N,2) | None, "colors": (N,4) | None,
    "tris": (M,3) int, "material": str}`. Every node becomes a scene root with an identity
    transform; every primitive becomes one glTF primitive of that node's mesh. A primitive with
    no normals gets flat normals computed here (glTF requires none, but §6 readers expect them).

    `materials` maps material name → dict with optional keys `baseColor` (URI), `normal` (URI),
    `baseColorFactor` [r,g,b,a], `metallicFactor`, `roughnessFactor`, `alphaMode`, `alphaCutoff`,
    `doubleSided`. Names not in the table are written with the name alone.

    Returns the number of bytes written.
    """
    buffer = _Buffer()
    accessors = []
    gltf_meshes = []
    gltf_nodes = []
    material_index = {}
    gltf_materials = []
    images, textures = [], []
    image_index = {}

    def texture_for(uri):
        if uri not in image_index:
            images.append({"uri": uri})
            textures.append({"source": len(images) - 1, "sampler": 0})
            image_index[uri] = len(textures) - 1
        return image_index[uri]

    def material_for(name):
        if name in material_index:
            return material_index[name]
        entry = {"name": name}
        spec = (materials or {}).get(name) or {}
        pbr = {}
        if "baseColorFactor" in spec:
            pbr["baseColorFactor"] = [float(v) for v in spec["baseColorFactor"]]
        if "metallicFactor" in spec:
            pbr["metallicFactor"] = float(spec["metallicFactor"])
        if "roughnessFactor" in spec:
            pbr["roughnessFactor"] = float(spec["roughnessFactor"])
        if spec.get("baseColor"):
            pbr["baseColorTexture"] = {"index": texture_for(spec["baseColor"])}
        if pbr:
            entry["pbrMetallicRoughness"] = pbr
        if spec.get("normal"):
            entry["normalTexture"] = {"index": texture_for(spec["normal"])}
        if spec.get("alphaMode") and spec["alphaMode"] != "OPAQUE":
            entry["alphaMode"] = spec["alphaMode"]
            if spec["alphaMode"] == "MASK":
                entry["alphaCutoff"] = float(spec.get("alphaCutoff", 0.5))
        if spec.get("doubleSided"):
            entry["doubleSided"] = True
        gltf_materials.append(entry)
        material_index[name] = len(gltf_materials) - 1
        return material_index[name]

    def accessor(view_index, component_type, count, gltf_type, minmax=None):
        entry = {
            "bufferView": view_index,
            "componentType": component_type,
            "count": int(count),
            "type": gltf_type,
        }
        if minmax is not None:
            entry["min"], entry["max"] = minmax
        accessors.append(entry)
        return len(accessors) - 1

    for node in nodes:
        prims_out = []
        for prim in node["primitives"]:
            verts = numpy.asarray(prim["verts"], dtype=numpy.float32)
            normals = prim.get("normals")
            tris = numpy.asarray(prim["tris"], dtype=numpy.int64).reshape(-1, 3)
            if from_bundle_frame:
                verts, normals, tris = _bundle_to_gltf(verts, normals, tris)
            if normals is None:
                normals = flat_normals(verts, tris)
            normals = numpy.asarray(normals, dtype=numpy.float32)
            uvs = prim.get("uvs")
            colors = prim.get("colors")
            if len(verts) == 0 or len(tris) == 0:
                continue

            v_view = buffer.add(verts, TARGET_ARRAY_BUFFER)
            n_view = buffer.add(normals, TARGET_ARRAY_BUFFER)
            attributes = {
                "POSITION": accessor(
                    v_view,
                    COMPONENT_FLOAT,
                    len(verts),
                    "VEC3",
                    (verts.min(axis=0).tolist(), verts.max(axis=0).tolist()),
                ),
                "NORMAL": accessor(n_view, COMPONENT_FLOAT, len(normals), "VEC3"),
            }
            if uvs is not None:
                uvs = numpy.asarray(uvs, dtype=numpy.float32)
                attributes["TEXCOORD_0"] = accessor(
                    buffer.add(uvs, TARGET_ARRAY_BUFFER), COMPONENT_FLOAT, len(uvs), "VEC2"
                )
            if colors is not None:
                colors = numpy.asarray(colors, dtype=numpy.float32)
                if colors.shape[1] == 3:
                    colors = numpy.concatenate(
                        [colors, numpy.ones((len(colors), 1), numpy.float32)], axis=1
                    )
                attributes["COLOR_0"] = accessor(
                    buffer.add(colors, TARGET_ARRAY_BUFFER), COMPONENT_FLOAT, len(colors), "VEC4"
                )
            indices = tris.astype(numpy.uint32).ravel()
            i_view = buffer.add(indices, TARGET_ELEMENT_ARRAY_BUFFER)
            prims_out.append(
                {
                    "attributes": attributes,
                    "indices": accessor(i_view, COMPONENT_UINT, len(indices), "SCALAR"),
                    "material": material_for(prim["material"]),
                    "mode": 4,
                }
            )
        if not prims_out:
            continue
        gltf_meshes.append({"name": node["name"], "primitives": prims_out})
        gltf_nodes.append({"name": node["name"], "mesh": len(gltf_meshes) - 1})

    doc = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(gltf_nodes)))}],
        "nodes": gltf_nodes,
        "meshes": gltf_meshes,
        "materials": gltf_materials,
        "accessors": accessors,
        "bufferViews": buffer.views,
        "buffers": [{"byteLength": buffer.length}],
    }
    if textures:
        doc["images"] = images
        doc["textures"] = textures
        doc["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]

    json_blob = _pad4(json.dumps(doc, separators=(",", ":")).encode("utf-8"), b" ")
    bin_blob = buffer.blob()
    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<4sII", _MAGIC, 2, total))
        handle.write(struct.pack("<II", len(json_blob), _CHUNK_JSON))
        handle.write(json_blob)
        handle.write(struct.pack("<II", len(bin_blob), _CHUNK_BIN))
        handle.write(bin_blob)
    return total


def flat_normals(verts, tris):
    """Per-vertex normals from face normals (area-weighted, normalised). Vertices shared by
    faces get the average; a vertex no face touches gets +Y."""
    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    face = numpy.cross(verts[tris[:, 1]] - verts[tris[:, 0]], verts[tris[:, 2]] - verts[tris[:, 0]])
    normals = numpy.zeros_like(verts)
    for k in range(3):
        numpy.add.at(normals, tris[:, k], face)
    length = numpy.linalg.norm(normals, axis=1)
    zero = length < 1e-12
    normals[zero] = (0.0, 1.0, 0.0)
    length[zero] = 1.0
    return (normals / length[:, None]).astype(numpy.float32)
