"""The `.glb` JSON chunk alone — the material-slot order without the cost of the geometry.

Reads no accessors and no vertex data, so the whole mesh catalogue can be swept in seconds.
The position in `primitive_materials`'s list is the mesh's own material SLOT, which is what
CS2's `MeshMaterial` buffer resolves a surface by and what
`EmissiveProperties.SingleLightMapping.materialId` indexes (`bake.emissive`).
"""

import json
import struct

# glTF chunk type code for the JSON chunk, from the spec.
_CHUNK_JSON = 0x4E4F534A


def primitive_materials(path):
    """The material name of every primitive of the `.glb`, in order, or `None` if unreadable.

    **The position in this list is the mesh's own material slot.** The producer writes one
    primitive per entry of the game's `MeshMaterial` buffer, in buffer order and with
    `MaterialIndex = i` (`MeshRipSystem`), and that buffer position is what CS2 itself resolves a
    surface by — and what `EmissiveProperties.SingleLightMapping.materialId` indexes. The one
    thing that can break the correspondence is `GltfWriter.NormalizePrimitives` dropping a
    primitive whose index range is out of bounds; it logs a warning when it does, and on the
    reference city's rip it did so zero times.

    Duplicates are kept, which is the difference from `material_names`: two slots may share a
    surface, and dropping the repeat would shift every slot after it.
    """
    doc = _json_chunk(path)
    if doc is None:
        return None
    names = [entry.get("name") for entry in doc.get("materials") or ()]
    order = []
    for mesh in doc.get("meshes") or ():
        for primitive in mesh.get("primitives") or ():
            index = primitive.get("material")
            order.append(
                names[index] if isinstance(index, int) and 0 <= index < len(names) else None
            )
    return order


def _json_chunk(path):
    """The `.glb`'s glTF JSON as a dict, reading the JSON chunk alone, or `None`."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(20)
            if len(head) < 20 or head[:4] != b"glTF":
                return None
            _magic, version, _total = struct.unpack_from("<III", head, 0)
            chunk_length, chunk_type = struct.unpack_from("<II", head, 12)
            if version != 2 or chunk_type != _CHUNK_JSON:
                return None
            return json.loads(handle.read(chunk_length).decode("utf-8"))
    except (OSError, ValueError, struct.error):
        return None


def material_names(path):
    """The `.glb`'s material names in PRIMITIVE ORDER, deduplicated, or `None` if unreadable.

    Reads the JSON chunk alone — no accessors, no vertex data — so the whole mesh catalogue can
    be swept in seconds. `parse_glb` gives the same order inside `material_runs` and costs the
    file's full geometry to get it; use this when the ORDER is the answer, which is what
    `EmissiveProperties`' colour slots join on (`bake.materials`, contract §4.3).

    The order is the writer's own sub-mesh order, which is the order CS2's `MeshMaterial` buffer
    is in and therefore the order a `SingleLightMapping.materialId` indexes.
    """
    primitives = primitive_materials(path)
    if primitives is None:
        return None
    order = []
    for name in primitives:
        if name and name not in order:
            order.append(name)
    return order
