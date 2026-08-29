"""Which decimator shapes each generated level — the better of the two, by measurement.

`bake/lods` asks for a level at a ratio; this decides who makes it. Both decimators are run and
the one whose two-sided displacement from LOD 0 is smaller wins that level, so the number the
switch point is derived from is also the number that chose the geometry.
"""

import numpy

from .. import decimate, decimatequadric, displacement


def quadric_available():
    return decimatequadric.available()


def best_levels(node, targets, backend):
    """`(levels, measured)`: one level per target, each the better of the two decimators by
    its own two-sided displacement, and `measured[li] = (distances, weights)` for the winner.

    `backend="auto"` runs BOTH and keeps, per level, the one whose surface is nearer LOD 0's
    (ties to the fewer triangles). Neither wins outright: at 25 % of a high-rise the quadric
    collapse leaves the surface where it was (0.0 m p95) where clustering moves it 10 m, and on
    a parking hall the same quadric stalls at half the triangles and 2.4 m out where clustering
    hits the target at 0.05 m (measured 2026-08-29, refactor Phase 10). Choosing by the
    measurement the switch point is built from is what keeps the ladder honest either way.
    `"cluster"` or `"quadric"` names one backend and skips the contest.
    """
    if backend == "auto":
        names = ("cluster", "quadric") if decimatequadric.available() else ("cluster",)
    else:
        names = (backend,)
    ladders = {name: decimate.lod_ladder(node, targets=targets, backend=name)[0] for name in names}
    levels, measured = [], []
    for li in range(len(targets)):
        best = None
        for name in names:
            level = ladders[name][li]
            if len(level["tris"]) == 0:
                candidate = (float("inf"), 0, name, level, (numpy.zeros(0), numpy.zeros(0)))
            else:
                p95, distances, weights = displacement.level_displacement(level, node)
                candidate = (p95, len(level["tris"]), name, level, (distances, weights))
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        level = dict(best[3])
        level["backend"] = best[2]
        levels.append(level)
        measured.append(best[4])
    return levels, measured
