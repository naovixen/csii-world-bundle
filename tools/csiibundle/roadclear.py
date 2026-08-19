"""Road clearance — which vegetation stands close enough to a drivable lane to obstruct it.

**A DECLARED DEVIATION, not a fidelity rule.** CS2 has no such rule: it clears objects when a
road is BUILT, and whatever survives in the save is what the city contains. This exists because
a city ripped 1:1 has trees standing in the carriageway and is unpleasant to drive. It is off
unless a consumer asks for it, and whatever it removes must be counted and declared.

What the game DOES decide is next door and is not this: an object something was built over
carries `Game.Common.Overridden`, and CS2 does not render it — see `instances.should_draw`, which
is a ported rule and applies whether or not this deviation is switched on. Measured on the
reference city, `Overridden` accounts for 275 of the 4,890 trees within 2 m of a lane centre, so
it is real but it is not the same question.

**Only what COLLIDES is worth removing.** A plant (`instances.is_tree` false — a bush, a flower
pot, long grass) is driven straight through once a consumer stops giving it collision, so
removing it costs fidelity and buys no drivability. The default rule therefore clips trees only:
on the reference city that is 3,466 items at 1.5 m — **1.12 % of trees, 0.50 % of all planting** —
against 8,303 if plants were included for no gain.

**There is no natural threshold in the data, and the sweep is why we know.** Trees per 0.25 m
band from the lane centre are almost flat from 0.5 m out to 5 m (667, 682, 709, 675, 685, 739,
703, 796, 826 …). If the survivors were kerbside plantings there would be a trough at the centre
and a peak at the kerb; there is not. So the number is a judgement and is documented as one: the
default 1.5 m is the vehicle envelope, not a discovered edge — a passenger car is ~1.9 m wide, so
its flank sits ~0.95 m from the lane centre, and a trunk radius of ~0.4 m (the measured median
for this city's tree prefabs) reaches it at ~1.35 m.

Numpy only, no scipy: this module is shared with the Blender adapter and Blender's Python has no
scipy (see `terrain` for the same constraint).
"""

import numpy

# Lane kinds a vehicle actually drives on. `pedestrian`, `track` (rail) and `connection` are not
# carriageway, and clipping foliage along a footpath would remove planting for no reason.
DRIVABLE_KINDS = ("car", "parking")

# The deviation's default, in metres. See the module docstring: the vehicle envelope, not a
# discovered edge in the data.
DEFAULT_CLEARANCE_M = 1.5


class PointGrid:
    """`within(queries, radius)` over 2-D points — a uniform spatial hash, numpy only.

    Cells are the query radius, so every point within the radius lies in the 3x3 neighbourhood of
    the query's own cell. Built once per lane layer and queried with every vegetation record at
    once; the alternative, `scipy.spatial.cKDTree`, is not available to both consumers.
    """

    def __init__(self, points, cell):
        self._cell = float(cell)
        pts = numpy.asarray(points, dtype=numpy.float64).reshape(-1, 2)
        self._points = pts
        if len(pts) == 0:
            self._keys = numpy.zeros(0, dtype=numpy.int64)
            self._order = numpy.zeros(0, dtype=numpy.int64)
            return
        cells = numpy.floor(pts / self._cell).astype(numpy.int64)
        self._base = cells.min(axis=0)
        self._span = int(cells[:, 1].max() - self._base[1]) + 3
        keys = self._key(cells)
        self._order = numpy.argsort(keys, kind="stable")
        self._keys = keys[self._order]

    def _key(self, cells):
        return (cells[:, 0] - self._base[0]) * self._span + (cells[:, 1] - self._base[1])

    def within(self, queries, radius):
        """Boolean array: is there a point within `radius` of each query? `radius <= cell`."""
        q = numpy.asarray(queries, dtype=numpy.float64).reshape(-1, 2)
        out = numpy.zeros(len(q), dtype=bool)
        if len(self._keys) == 0 or len(q) == 0:
            return out
        r2 = float(radius) ** 2
        base_cells = numpy.floor(q / self._cell).astype(numpy.int64)
        index = numpy.arange(len(q))
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                todo = ~out                       # a query already satisfied needs no more cells
                if not todo.any():
                    return out
                cells = base_cells[todo] + numpy.array([dx, dz], dtype=numpy.int64)
                keys = self._key(cells)
                lo = numpy.searchsorted(self._keys, keys, side="left")
                hi = numpy.searchsorted(self._keys, keys, side="right")
                counts = hi - lo
                total = int(counts.sum())
                if total == 0:
                    continue
                # expand [lo, hi) into one flat index array, and remember which query each came
                # from — the standard ragged-range gather, no Python loop over queries
                offsets = numpy.repeat(numpy.cumsum(counts) - counts, counts)
                flat = numpy.repeat(lo, counts) + (numpy.arange(total) - offsets)
                owner = numpy.repeat(index[todo], counts)
                delta = self._points[self._order[flat]] - q[owner]
                hit = (delta[:, 0] ** 2 + delta[:, 1] ** 2) <= r2
                if hit.any():
                    out[numpy.unique(owner[hit])] = True
        return out


def lane_points(lane_records, kinds=DRIVABLE_KINDS):
    """`(N, 2)` of every station on a drivable lane's centreline, in world X/Z.

    A lane's `path` is `[x, y, z]` with Y up (bundle §8), so the horizontal pair is (x, z).
    """
    kinds = set(kinds)
    out = []
    for record in lane_records:
        if record.get("kind") not in kinds:
            continue
        for point in record.get("path") or ():
            if len(point) >= 3:
                out.append((point[0], point[2]))
    return numpy.asarray(out, dtype=numpy.float64).reshape(-1, 2)


def obstructing(records, grid, clearance=DEFAULT_CLEARANCE_M, trees_only=True):
    """Boolean array over `records`: which stand within `clearance` of a drivable lane centre.

    `trees_only` keeps the deviation tied to its own justification — a plant that a consumer has
    already made non-collidable obstructs nothing, so removing it would be fidelity spent for no
    drivability. Pass False only if a consumer gives plants collision.
    """
    from . import instances as instances_mod

    if clearance <= 0.0 or not len(records):
        return numpy.zeros(len(records), dtype=bool)
    positions = numpy.empty((len(records), 2), dtype=numpy.float64)
    eligible = numpy.zeros(len(records), dtype=bool)
    for i, record in enumerate(records):
        pos = record.get("pos") or (0.0, 0.0, 0.0)
        positions[i] = (pos[0], pos[2])
        if record.get("kind") != "tree":
            continue
        eligible[i] = instances_mod.is_tree(record) if trees_only else True
    out = numpy.zeros(len(records), dtype=bool)
    if eligible.any():
        out[eligible] = grid.within(positions[eligible], clearance)
    return out
