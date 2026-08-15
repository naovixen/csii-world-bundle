"""Bundle frame → BeamNG frame. The one place the axis mapping lives.

Bundle (contract §2.1): left-handed, X east, Y up, Z north, metres.
BeamNG / Torque:        right-handed, X east, Y north, Z up, metres.

So `beamng = (x, z, y)` — swap Y and Z. A swap is a reflection, which is exactly the
left-to-right handedness change, so nothing else is needed for POINTS. Rotations (A2+) go
through `rotation_matrix`, and mesh vertices additionally undo the §2.2 mirror.

Headings: BeamNG objects face their local +Y axis. `spawn.lua` spawns the vehicle with
`rot = spawnSphere.rotation * quat(0,0,1,0)` and the vehicle's own forward is local −Y, so the
car ends up facing the sphere's +Y (verified against `small_island`'s spawn matrices, which
`heading_matrix` reproduces bit for bit). `rotationMatrix` in `items.level.json` is row-major
3×3 whose COLUMNS are the object's axes in world space.
"""

import math


def to_beamng(x, y, z):
    """Bundle (x, y=up, z=north) → BeamNG (x, y=north, z=up)."""
    return (float(x), float(z), float(y))


def to_bundle(x, y, z):
    """BeamNG (x, y=north, z=up) → bundle (x, y=up, z=north)."""
    return (float(x), float(z), float(y))


def heading_matrix(dx, dy):
    """Row-major 3×3 for an object standing upright and facing the horizontal direction
    `(dx, dy)` in BeamNG world axes (east, north). Column 1 (the object's +Y) is the heading,
    column 0 (its +X) is the right-hand side, column 2 is up."""
    length = math.hypot(dx, dy)
    if length < 1e-9:
        dx, dy = 0.0, 1.0
    else:
        dx, dy = dx / length, dy / length
    return [dy, dx, 0.0,
            -dx, dy, 0.0,
            0.0, 0.0, 1.0]


def identity_matrix():
    return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
