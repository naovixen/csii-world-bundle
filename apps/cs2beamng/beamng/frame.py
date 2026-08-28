"""Bundle frame → BeamNG frame. The one place the axis mapping lives.

Bundle (contract §2.1): left-handed, X east, Y up, Z north, metres.
BeamNG / Torque:        right-handed, X east, Y north, Z up, metres.

So `beamng = (x, z, y)` — swap Y and Z. A swap is a reflection, which is exactly the
left-to-right handedness change, so nothing else is needed for POINTS. Rotations (A2+) go
through `rotation_matrix`, and mesh vertices additionally undo the §2.2 mirror.

Headings: BeamNG objects face their local +Y axis. `spawn.lua` spawns the vehicle with
`rot = spawnSphere.rotation * quat(0,0,1,0)` and the vehicle's own forward is local −Y, so the
car ends up facing the sphere's +Y.

## `rotationMatrix` is the object's OWN AXES, in groups of three

The nine floats a `SceneObject` stores — `TSStatic`, forest item, light, spawn sphere, all of
them — are **elements (0,1,2) = the object's X axis, (3,4,5) = its Y axis, (6,7,8) = its Z
axis**, each in world space. That is the TRANSPOSE of writing a standard rotation matrix out row
by row, and writing it the other way stores the INVERSE rotation.

MEASURED on BeamNG's own 21 shipped levels, three independent witnesses (A6.8, audit §78):

* **4,138 vanilla SpotLights.** Read as groups, +Y points DOWN on 3,417 of them and up on 40.
  Read row-major, +Y points UP on 2,788. Tunnel lights do not shine at the ceiling.
* **13,885 vanilla barriers, guardrails and fences** laid end to end: the item's own long axis
  lies along the run to 0.982 read as groups, 0.933 read row-major.
* **The same test on 2.48 M vanilla FOREST items** (the loader is a different one, so it is
  checked separately): 0.987 against 0.908 over 153,490 elongated items; and of 23,966 items
  standing on a measurable slope, 84 % tip DOWNHILL read as groups — which is what aligning an
  item to the terrain normal does — against 50 %, i.e. no signal at all, read row-major.

`basis_flat` is the only place that ordering is written down. Everything that emits the field
goes through it.
"""

import math

from csiibundle import frames
from csiibundle import instances as bundle_instances


def to_beamng(x, y, z):
    """Bundle (x, y=up, z=north) → BeamNG (x, y=north, z=up)."""
    return (float(x), float(z), float(y))


def to_bundle(x, y, z):
    """BeamNG (x, y=north, z=up) → bundle (x, y=up, z=north)."""
    return (float(x), float(z), float(y))


def basis_flat(matrix):
    """A row-major 3×3 rotation (its COLUMNS are the object's axes) as the nine floats BeamNG
    stores: the axes themselves, in groups of three. See the module docstring for the measurement
    that settles the ordering — it is the transpose of the obvious flatten, and the obvious
    flatten stores the inverse rotation."""
    m = [[float(v) for v in row] for row in matrix]
    return [m[0][0], m[1][0], m[2][0], m[0][1], m[1][1], m[2][1], m[0][2], m[1][2], m[2][2]]


def heading_matrix(dx, dy):
    """The nine floats for an object standing upright and facing the horizontal direction
    `(dx, dy)` in BeamNG world axes (east, north): its +Y is the heading, its +X the right-hand
    side, its +Z up."""
    length = math.hypot(dx, dy)
    if length < 1e-9:
        dx, dy = 0.0, 1.0
    else:
        dx, dy = dx / length, dy / length
    return basis_flat([[dy, dx, 0.0], [-dx, dy, 0.0], [0.0, 0.0, 1.0]])


def identity_matrix():
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


# The basis `dae.glb_to_local` bakes into every `.cdae` vertex: `(gx, gy, gz) -> (-gx, gz, gy)`,
# which is `P_ZUP . MIRROR_X`. `instance_matrix` divides it out so the two do not compound.
DAE_MESH_BASIS = frames.P_ZUP @ frames.MIRROR_X


def record_transform(record):
    """`(position, rotation3x3_rowmajor, scale3)` in BeamNG world axes.

    `P . T . R . S . P^-1`, but NOT written out here. It used to be, in three lines of
    quaternion-to-matrix and axis swapping that reimplemented `csiibundle.instances`'
    `instance_matrix` — the function whose docstring says "a forgotten decal lift flickers",
    which exists so that the §4.2a lift and the tree growth scale cannot be left out one at a
    time. The copy left out the lift.

    **That was latent, not live** (measured 2026-08-21). `placement.BAKED_KINDS` is `("decal",)`,
    so a decal record never reaches this function at all: `bake/markings.py:250` places decal
    quads itself, deliberately without the lift in the matrix — "the drape sets the height" — and
    hands `decals.LIFT["instance_decal"]` to the drape as its epsilon. Every record that DOES
    reach here has a lift of 0.0. Rebuilding `cs2_city_fine` through this function moved 8,646
    objects by 0.000000000 m.

    What it changes is the quaternion. The copy renormalised, and `frames.quat_matrix`
    deliberately does not: 6.9 % of the reference city's records store a quaternion off unity by
    ~2e-6, and that is a signal about the producer, not noise to be corrected away here.
    `frames.decompose` lets it through as a scale a hair off 1.0.
    """
    return frames.decompose(
        bundle_instances.instance_matrix(record, mesh_basis=DAE_MESH_BASIS, zup=True)
    )
