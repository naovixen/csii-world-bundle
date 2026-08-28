"""Polyline maths — resampling, framing, simplification, weld keys. Beside `bezier`, which
owns the curve evaluation every linear layer is stored as; this module owns what consumers do
with the evaluated points. Bundle frame throughout (X east, Y up, Z north)."""

import math

import numpy


def length(path):
    """Total arclength of a polyline."""
    return float(numpy.sum(numpy.linalg.norm(numpy.diff(path, axis=0), axis=1)))


def norm_sq(delta):
    """Squared length of one vector — the comparison form of a distance."""
    return float(numpy.dot(delta, delta))


def weld_key(point, quantum=0.05):
    """A weld key: the point rounded to the tolerance grid. Two lane ends CS2 placed at the same
    coordinate hash together; a grid is used rather than a kd-tree because a segment's own lanes
    are exact to the millimetre and this runs 6,667 times."""
    return (
        round(float(point[0]) / quantum),
        round(float(point[1]) / quantum),
        round(float(point[2]) / quantum),
    )


def resample(path, step):
    """The polyline resampled at roughly `step` metres per station, endpoints kept."""
    n = max(2, int(math.ceil(length(path) / step)) + 1)
    return resample_count(path, n)


def resample_count(path, n):
    """The polyline resampled to exactly `n` stations by arclength fraction."""
    seg = numpy.linalg.norm(numpy.diff(path, axis=0), axis=1)
    s = numpy.concatenate([[0.0], numpy.cumsum(seg)])
    if s[-1] < 1e-9:
        return numpy.repeat(path[:1], n, axis=0)
    t = numpy.linspace(0.0, s[-1], n)
    return numpy.stack([numpy.interp(t, s, path[:, k]) for k in range(3)], axis=1)


def frame(points):
    """Per-vertex tangent and left-hand HORIZONTAL normal of a polyline. The offset is taken in
    the horizontal plane only, for the same reason `networks.ribbon` does: CS2 does not bank a
    road, and a horizontal offset keeps a ramp the right width in plan."""
    delta = numpy.gradient(points, axis=0)
    flat = numpy.stack((delta[:, 0], delta[:, 2]), axis=1)
    norm = numpy.linalg.norm(flat, axis=1, keepdims=True)
    flat = numpy.where(
        norm < 1e-9, numpy.array([1.0, 0.0]), flat / numpy.where(norm < 1e-9, 1.0, norm)
    )
    tangents = numpy.zeros_like(points)
    tangents[:, 0] = flat[:, 0]
    tangents[:, 2] = flat[:, 1]
    normals = numpy.zeros_like(points)
    normals[:, 0] = -flat[:, 1]
    normals[:, 2] = flat[:, 0]
    return tangents, normals


def nearest_station(points, target):
    """Index of the polyline station nearest to `target`."""
    d = points - numpy.asarray(target, dtype=numpy.float64)
    return int(numpy.argmin(numpy.einsum("ij,ij->i", d, d)))


def simplify(points, tolerance):
    """Ramer-Douglas-Peucker, iterative. Endpoints are always kept."""
    points = numpy.asarray(points, dtype=numpy.float64)
    if len(points) <= 2 or tolerance <= 0:
        return points
    keep = numpy.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = points[i], points[j]
        ab = b - a
        denom = float(numpy.dot(ab, ab))
        seg = points[i + 1 : j] - a
        if denom < 1e-12:
            dist = numpy.linalg.norm(seg, axis=1)
        else:
            t = numpy.clip((seg @ ab) / denom, 0.0, 1.0)[:, None]
            dist = numpy.linalg.norm(seg - t * ab, axis=1)
        k = int(numpy.argmax(dist))
        if dist[k] > tolerance:
            keep[i + 1 + k] = True
            stack.append((i, i + 1 + k))
            stack.append((i + 1 + k, j))
    return points[keep]
