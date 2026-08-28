"""The rigid-tile gate (§5B v2) — when a lane tile is one matrix instead of baked vertices.

On a straight span the bend IS a rigid transform, so the tile can be one instance of a shared
mesh rather than a copy of its vertices; `csiibundle.lanes`'s module docstring carries the
measurement behind that. `rigid_tile_matrix` returns the transform or `None` when the window is
not straight enough or the width tapers — exact where it applies, absent where it is not. The
quaternion helpers live here because the gate and the bend share them.
"""

import math

import numpy

from . import bezier

# When a tile may be instanced instead of baked. Both are accuracy bounds on the claim "this
# window's bend is rigid", so tightening them costs speed and never fidelity. TUNED.
TILE_CHORD_TOL = 0.02  # m: max deviation of the tile's curve window from its 3D chord
TILE_WIDTH_TOL = 0.01  # fraction: max widthScale variation across the tile


def quat_apply(quat, vectors):
    """Rotate an `(N, 3)` array of bundle-frame vectors by a bundle quaternion `[x, y, z, w]`.

    The two-cross-product form rather than building a matrix: for a part's few dozen source
    vertices it is the cheaper path, and it avoids materialising a 3×3 per part.
    """
    x, y, z, w = quat
    v = numpy.asarray(vectors, dtype=numpy.float64)
    tx = 2 * (y * v[:, 2] - z * v[:, 1])
    ty = 2 * (z * v[:, 0] - x * v[:, 2])
    tz = 2 * (x * v[:, 1] - y * v[:, 0])
    return numpy.stack(
        (
            v[:, 0] + w * tx + y * tz - z * ty,
            v[:, 1] + w * ty + z * tx - x * tz,
            v[:, 2] + w * tz + x * ty - y * tx,
        ),
        axis=1,
    )


def quat_to_mat3(quat):
    """A bundle quaternion `[x, y, z, w]` as a 3×3 rotation matrix (columns = rotated axes)."""
    x, y, z, w = quat
    return numpy.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=numpy.float64,
    )


def _straight_chord(curve, t_table, arc_table, arc_start, arc_per_z):
    """`(t, points, chord, chord_len)` for a tile's five-sample curve window, or `None` when the
    window bends past `TILE_CHORD_TOL` (measured in 3D so grade curvature counts too)."""
    arcs = numpy.array(
        [
            arc_start,
            arc_start + 0.25 * arc_per_z,
            arc_start + 0.5 * arc_per_z,
            arc_start + 0.75 * arc_per_z,
            arc_start + arc_per_z,
        ]
    )
    t = numpy.interp(arcs, arc_table, t_table)
    points = bezier.evaluate(curve, t)
    chord = points[-1] - points[0]
    chord_len = float(numpy.linalg.norm(chord))
    if chord_len < 1e-6:
        return None
    deviation = numpy.linalg.norm(numpy.cross(points[1:4] - points[0], chord), axis=1) / chord_len
    if float(deviation.max()) > TILE_CHORD_TOL:
        return None
    return t, points, chord, chord_len


def rigid_tile_matrix(
    curve,
    t_table,
    arc_table,
    arc_start,
    arc_per_z,
    span,
    inverted,
    width_scale,
    pos,
    rot,
    bmin_z,
    mesh_length,
):
    """The BUNDLE-frame 4×4 placing one straight tile rigidly, or `None` if the bend is not
    rigid over this tile's window.

    When the curve window is straight (within `TILE_CHORD_TOL`, measured in 3D so grade
    curvature counts too) and the width is constant (within `TILE_WIDTH_TOL`), `P(arc)` is
    linear in `z` and the whole bend collapses to one affine transform: columns
    `right · width`, `ŷ`, and `chord / mesh_length`. The chord carries the grade, which is why a
    fence on a straight climb shears exactly as the bend sheared it rather than staying level.

    Composed with the part's own `(rot, pos)`, the result applies verbatim to the shared,
    un-bent part mesh — so an instanced tile and a baked tile are the same triangles in the same
    place. A consumer whose meshes are in another frame converts this transform the same way it
    converts any other (`frames.to_zup_transform` for the Z-up case).
    """
    probe = _straight_chord(curve, t_table, arc_table, arc_start, arc_per_z)
    if probe is None:
        return None
    t, points, chord, _chord_len = probe

    t_global = numpy.where(
        inverted,
        span[1] - (span[1] - span[0]) * t[[0, -1]],
        span[0] + (span[1] - span[0]) * t[[0, -1]],
    )
    w0, w1 = width_scale[0] + (width_scale[1] - width_scale[0]) * t_global
    if abs(w1 - w0) > TILE_WIDTH_TOL * max(abs(w0), abs(w1), 1e-6):
        return None
    width = (w0 + w1) * 0.5

    # right = normalize(cross(up, forward)), horizontal — exactly the bend's own frame.
    flat = math.hypot(chord[0], chord[2])
    if flat < 1e-6:
        return None
    right = numpy.array([chord[2] / flat, 0.0, -chord[0] / flat])

    outer = numpy.empty((3, 3), dtype=numpy.float64)
    outer[:, 0] = right * width
    outer[:, 1] = (0.0, 1.0, 0.0)
    outer[:, 2] = chord / mesh_length
    matrix = numpy.eye(4)
    matrix[:3, :3] = outer @ quat_to_mat3(rot)
    matrix[:3, 3] = (
        points[0] - outer[:, 2] * bmin_z + outer @ numpy.asarray(pos, dtype=numpy.float64)
    )
    return matrix
