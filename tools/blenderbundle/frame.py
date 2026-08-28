"""The bundle frame as Blender sees it — contract §2, plus Blender's own two conventions.

`csiibundle.frames` carries the derivation; what is here is the ONE thing that is Blender's
rather than the bundle's, and the two conversions every layer calls.
"""

from csiibundle import frames, instances
from mathutils import Matrix, Vector

# The whole derivation is `csiibundle.frames`; read its module docstring for why the formula is
# what it is. What this file adds is the ONE convention that is Blender's rather than the
# bundle's: the stock glTF importer rewrites Y-up to Z-up on import, so the mesh sitting in the
# scene is already G·v_glb and that has to be undone. `frames.GLTF_YUP_TO_ZUP` is that G, and
# `frames.zup_instance_matrix(record, G)` composes the lot:
#
#     P · T · R · S · diag(-1,1,1) · G⁻¹
#
# The sanity check on it is that P · diag(-1,1,1) · G⁻¹ = diag(-1,-1,1), a 180° turn about Z
# with no reflection — the mesh was already made right-handed by the producer, so reconstructing
# it in a right-handed application must not mirror anything again.

# The mesh-side basis Blender's glTF importer applies. Passed to `frames.zup_instance_matrix`
# for every placement; a consumer reading the .glb buffers directly passes None instead.
GLTF_BASIS = frames.GLTF_YUP_TO_ZUP


def to_blender_point(pos):
    """Bundle-frame position (X east, Y up, Z north) to Blender (X east, Y north, Z up)."""
    return Vector(frames.bundle_to_zup_point(pos))


def instance_matrix(record):
    """The full world matrix for one instance record, ready to assign to matrix_world.

    `csiibundle.instances` composes it — the §2 frame formula plus the two per-record
    corrections that are silent when missed: `attrs.growthScale` (a tree that has not finished
    growing) and the §4.2a decal lift. This only converts to mathutils.
    """
    return Matrix(instances.instance_matrix(record, GLTF_BASIS, zup=True).tolist())
