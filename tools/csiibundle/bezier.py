"""Cubic Bézier curves — the shape every linear thing in the bundle is stored as.

Network centerlines (§5), lane curves (§5B) and clip-volume strips (§8) are all cubic Béziers
with four control points in bundle-frame metres. The game's own `MathUtils.Position` /
`MathUtils.Tangent` are these formulas, and the port is deliberate: a consumer that samples with
a different parameterisation will place lane markings and clip bands slightly off the geometry
they belong to, which reads as "the markings are wobbly" rather than as a maths bug.

**Arc length is not the curve parameter.** `t` runs 0..1 but a Bézier does not travel at
constant speed along it, and everything the bundle places along a curve — a tile, a fence post,
a lattice station — is placed by DISTANCE. `arc_table()` builds the lookup that converts between
the two, and it is designed to be handed straight to `numpy.interp`:

    t_table, arc_table = bezier.arc_table(curve)
    t = numpy.interp(distance_along, arc_table, t_table)     # arc -> parameter
    points = bezier.evaluate(curve, t)

`ARC_SAMPLES` chords is the resolution of that table. It is a TUNED cost/accuracy trade, not a
contract number, but it is the resolution every consumer of this repo has measured against, so
changing it moves lane geometry by small amounts everywhere at once.
"""

import numpy

# Chords used to approximate arc length. 32 keeps a full-city lane bend's error far below the
# centimetre tolerances downstream while costing one 33-point evaluation per span. TUNED.
ARC_SAMPLES = 32


def evaluate(control, t):
    """A cubic Bézier at parameters `t`. `control` is `(4, 3)`; `t` is `(S,)`; result `(S, 3)`.

    The game's `MathUtils.Position`, written as the plain Bernstein form rather than as nested
    lerps: identical arithmetic, one pass, and no `pow` call in the hot path.
    """
    a, b, c, d = (numpy.asarray(control[i], dtype=numpy.float64) for i in range(4))
    t = numpy.asarray(t, dtype=numpy.float64)[:, None]
    u = 1.0 - t
    return (u * u * u) * a + (3.0 * u * u * t) * b + (3.0 * u * t * t) * c + (t * t * t) * d


def tangent(control, t):
    """The derivative at `t` — `(S, 3)`, NOT normalised.

    Its length is the curve's speed, which is exactly the thing that varies; normalise it when
    you want a direction, and do not use its length as a distance.
    """
    a, b, c, d = (numpy.asarray(control[i], dtype=numpy.float64) for i in range(4))
    t = numpy.asarray(t, dtype=numpy.float64)[:, None]
    u = 1.0 - t
    return (3.0 * u * u) * (b - a) + (6.0 * u * t) * (c - b) + (3.0 * t * t) * (d - c)


def evaluate_scalar(control, t):
    """`evaluate` for a curve of scalars rather than points — `control` is `(4,)`, result `(S,)`.

    Used for the one-dimensional curves the bundle ships alongside the geometric ones: a lane's
    hang profile (§5B) is a cubic in metres of droop, not a path through space.
    """
    control = numpy.asarray(control, dtype=numpy.float64)
    t = numpy.asarray(t, dtype=numpy.float64)
    u = 1.0 - t
    return ((u * u * u) * control[0] + (3.0 * u * u * t) * control[1]
            + (3.0 * u * t * t) * control[2] + (t * t * t) * control[3])


def cut(control, t0, t1):
    """The sub-curve over `[t0, t1]`, as its own four control points.

    Two De Casteljau splits, which is exact — the result is the same curve restricted, not an
    approximation of it. This is how a lane record's spans (`[t0, t1]` ranges along the parent
    curve, §5B) become curves in their own right.
    """
    def split_after(points, t):
        p01 = tuple((1 - t) * points[0][i] + t * points[1][i] for i in range(3))
        p12 = tuple((1 - t) * points[1][i] + t * points[2][i] for i in range(3))
        p23 = tuple((1 - t) * points[2][i] + t * points[3][i] for i in range(3))
        p012 = tuple((1 - t) * p01[i] + t * p12[i] for i in range(3))
        p123 = tuple((1 - t) * p12[i] + t * p23[i] for i in range(3))
        p = tuple((1 - t) * p012[i] + t * p123[i] for i in range(3))
        return (p, p123, p23, points[3]), (points[0], p01, p012, p)

    tail, _ = split_after(control, t0)
    if t1 >= 1.0 - 1e-9:
        return tail
    local = (t1 - t0) / max(1.0 - t0, 1e-9)
    _, head = split_after(tail, local)
    return head


def arc_table(control):
    """`(t_table, arc_table)`, both `(ARC_SAMPLES + 1,)` — the arc-length lookup for a curve.

    `t_table` holds evenly spaced parameters and `arc_table` the cumulative chord length at
    each, so the pair is directly `numpy.interp`'s `(xp, fp)` in either direction:

        t = numpy.interp(arc, arc_table, t_table)      # distance along -> parameter
        arc = numpy.interp(t, t_table, arc_table)      # parameter -> distance along

    `arc_table[-1]` is the curve's length. Chord length under-estimates a curve slightly by
    construction; at 32 samples that is far below anything downstream measures.
    """
    t_table = numpy.linspace(0.0, 1.0, ARC_SAMPLES + 1)
    points = evaluate(control, t_table)
    segment_lengths = numpy.linalg.norm(numpy.diff(points, axis=0), axis=1)
    return t_table, numpy.concatenate(([0.0], numpy.cumsum(segment_lengths)))


def length(control):
    """The curve's arc length in metres, to `arc_table`'s resolution."""
    return float(arc_table(control)[1][-1])


def sagitta(points):
    """The worst perpendicular deviation of `points` from the straight chord through its ends.

    How far a curve bows away from being straight, in metres — the measure that decides how
    finely anything laid along it has to be subdivided. `points` is `(S, 3)`, normally the
    samples `arc_table` already computed, so asking this question costs no new evaluation.
    Returns 0.0 for a curve whose ends coincide (a closed loop has no chord to measure against).
    """
    points = numpy.asarray(points, dtype=numpy.float64)
    chord = points[-1] - points[0]
    chord_len = float(numpy.linalg.norm(chord))
    if chord_len < 1e-6:
        return 0.0
    direction = chord / chord_len
    rel = points - points[0]
    perp = rel - numpy.outer(rel @ direction, direction)
    return float(numpy.linalg.norm(perp, axis=1).max())
