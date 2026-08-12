"""The frame conversion — `docs/bundle-schema.md` §2, and the one thing to get right first.

Three frames are in play and confusing two of them is the classic way to produce a city that
is mirrored, rotated 90 degrees, or inside out:

**Bundle world (§2.1)** is CS2 world space verbatim: **left-handed, Y up, X east, Z north**.
Every `pos`, every `centerline`, every terrain origin in the bundle is in this frame, and it
is the frame the numbers mean. It is left-handed, which is why a mesh cannot simply be dropped
into it.

**Mesh payload (§2.2)** is what is actually inside a `.glb`: right-handed, Y up, with the
vertices **mirrored on X** so the file opens correctly on its own in any glTF viewer. The
mirror is the bridge between the two handednesses, and it is why the contract's instance
formula ends in `diag(-1,1,1)`:

    v_world = T(pos) · R(rot) · S(scale) · diag(-1,1,1) · v_glb

That is `instance_matrix()`, and it is the whole of §2.2. Everything else here is convenience
for a consumer whose own frame is not the bundle's.

**Consumer frame** is whatever the target application uses. For the common right-handed,
**Z-up** case (Blender, BeamNG, most CAD) the change of basis is `P`: bundle `(x, y, z)` maps
to `(x, z, y)`. `P` has determinant −1, and that is exactly the point — the reflection is what
converts the bundle's left-handed frame into a right-handed one, so **no further mirror is
needed anywhere**. If you find yourself adding a second flip to make something look right, the
error is elsewhere.

The composed transform for a Z-up consumer is

    P · T · R · S · diag(-1,1,1) · B⁻¹

where `B` is whatever basis change the consumer's own glTF loader already applied to the mesh
vertices. `B` is the identity when the loader leaves glTF coordinates alone; it is
`GLTF_YUP_TO_ZUP` for a loader (Blender's stock importer among them) that rewrites Y-up to
Z-up on import. `zup_instance_matrix()` composes it. The sanity check on the result is that

    P · diag(-1,1,1) · GLTF_YUP_TO_ZUP⁻¹  =  diag(-1,-1,1)

is a 180° turn about Z with determinant **+1**: a proper rotation, no reflection. The mesh was
made right-handed by the producer, so reconstructing it in a right-handed application must not
mirror anything a second time.

Matrices here are `(4, 4)` numpy float64, row-vector-on-the-right convention (`M @ v`), which
is what glTF, Unity, Blender and every text on the subject use. Convert to the target's own
type at the boundary; the extra precision costs nothing and float32 truncation at assignment
is identical either way.
"""

import numpy

# P — bundle Y-up (left-handed) to a Z-up right-handed consumer frame: (x, y, z) -> (x, z, y).
# A row swap, determinant -1. CONTRACT (§2.1).
P_ZUP = numpy.array([[1.0, 0.0, 0.0, 0.0],
                     [0.0, 0.0, 1.0, 0.0],
                     [0.0, 1.0, 0.0, 0.0],
                     [0.0, 0.0, 0.0, 1.0]])

# diag(-1, 1, 1) — the X mirror that takes a `.glb`'s right-handed vertices into the bundle's
# left-handed world frame. The last term of the §2.2 instance formula. CONTRACT.
MIRROR_X = numpy.array([[-1.0, 0.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0]])

# G — the basis change a Y-up-to-Z-up glTF loader applies to mesh vertices: (x, y, z) ->
# (x, -z, y). This is NOT a bundle convention; it is a property of one class of loader, and it
# appears here only so `zup_instance_matrix` can undo it. A consumer that reads the `.glb`
# vertices as they are stored passes `mesh_basis=None` instead.
GLTF_YUP_TO_ZUP = numpy.array([[1.0, 0.0, 0.0, 0.0],
                               [0.0, 0.0, -1.0, 0.0],
                               [0.0, 1.0, 0.0, 0.0],
                               [0.0, 0.0, 0.0, 1.0]])

# Identity rotation in the bundle's storage order. CONTRACT: the bundle stores quaternions
# **[x, y, z, w]**, glTF's order — not the [w, x, y, z] that most maths libraries take.
IDENTITY_QUAT = (0.0, 0.0, 0.0, 1.0)


def quat_matrix(quat):
    """A bundle quaternion `[x, y, z, w]` as a `(4, 4)` rotation matrix.

    The quaternion is used as stored, without renormalising — the same choice every engine's
    `quat_to_mat` makes, so a producer that ever shipped a non-unit quaternion would show up as
    a scaled instance rather than being silently corrected here.

    The rotation is applied in BUNDLE coordinates and the frame change happens afterwards,
    which is why no term flips sign: the same quaternion algebra holds in a left-handed basis,
    and conjugating the result by `P` is what moves it into a right-handed one.
    """
    x, y, z, w = (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return numpy.array([
        [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy), 0.0],
        [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx), 0.0],
        [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy), 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])


def translation_matrix(pos):
    m = numpy.identity(4)
    m[0, 3], m[1, 3], m[2, 3] = float(pos[0]), float(pos[1]), float(pos[2])
    return m


def scale_matrix(scale):
    return numpy.diag([float(scale[0]), float(scale[1]), float(scale[2]), 1.0])


def trs_matrix(pos, rot=IDENTITY_QUAT, scale=(1.0, 1.0, 1.0)):
    """`T(pos) · R(rot) · S(scale)` in the bundle frame, without the §2.2 mesh mirror.

    Use this for anything positioned in the bundle that is not a `.glb` payload — a light, a
    probe point, a bounding box. For placing a mesh, use `instance_matrix`.
    """
    return translation_matrix(pos) @ quat_matrix(rot) @ scale_matrix(scale)


def instance_matrix(record):
    """The §2.2 world matrix for one instance record: glTF vertices to bundle world space.

        v_world = T(pos) · R(rot) · S(scale) · diag(-1,1,1) · v_glb

    `rot` and `scale` are optional in the record and default to identity, so this is safe on
    every layer that carries a transform — `instances.jsonl`, the network and lane records, the
    sub-object rows.
    """
    return trs_matrix(record["pos"],
                      record.get("rot", IDENTITY_QUAT),
                      record.get("scale", (1.0, 1.0, 1.0))) @ MIRROR_X


def zup_instance_matrix(record, mesh_basis=None):
    """`instance_matrix` expressed for a right-handed **Z-up** consumer.

    `mesh_basis` is the basis change the consumer's own glTF loader already applied to the mesh
    vertices, and it is undone here so the two do not compound:

    * `None` — the loader kept glTF coordinates (Y-up, right-handed). The common case for a
      consumer that reads the buffers itself, `csiibundle.glb` included.
    * `GLTF_YUP_TO_ZUP` — the loader rewrote Y-up to Z-up on import, as Blender's stock glTF
      importer does.
    """
    world = P_ZUP @ instance_matrix(record)
    if mesh_basis is not None:
        world = world @ numpy.linalg.inv(mesh_basis)
    return world


def bundle_to_zup_point(pos):
    """A bundle-frame point `(X east, Y up, Z north)` as Z-up `(X east, Y north, Z up)`.

    The point form of `P_ZUP`, as a plain tuple so it converts to any vector type. Direction
    vectors take the same map (`P_ZUP` is its own inverse), which is why there is no separate
    function for them.
    """
    return (float(pos[0]), float(pos[2]), float(pos[1]))


def zup_to_bundle_point(pos):
    """The inverse of `bundle_to_zup_point` — which is the same swap. Named for readability at
    a call site that is going the other way (a picked world coordinate back into bundle terms)."""
    return (float(pos[0]), float(pos[2]), float(pos[1]))


def transform_points(matrix, points):
    """Apply a `(4, 4)` matrix to an `(N, 3)` array of points. Returns `(N, 3)` float64.

    The vectorised form of the per-instance maths above, for the layers that ship raw geometry
    rather than a transform — centerlines, clip-volume corners, area rings.
    """
    points = numpy.asarray(points, dtype=numpy.float64).reshape(-1, 3)
    return points @ numpy.asarray(matrix, dtype=numpy.float64)[:3, :3].T + matrix[:3, 3]
