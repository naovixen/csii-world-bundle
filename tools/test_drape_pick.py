"""Drape-pick check: how far a road marking is draped off the road it is painted on.

The harness projects each lane-decal vertex straight down onto the road and writes
`surface + lift` (`_ProjectionSurface.drop` -> `_drape_block`). Which surface it picks is a
choice, and this measures the cost of that choice against the surface of the marking's OWN
segment directly beneath the sample — the road it is actually painted on.

WHAT THIS SETTLED (2026-08-11). The regression plan held that a kerb inside the marking's own
merged segment mesh was shadowing the carriageway: `BVHTree.ray_cast` returns only the FIRST hit
along a ray, so one cast per object yields that object's topmost surface alone, and a network
segment is ONE object holding the whole composition. The remedy proposed was to re-cast below
each hit and then pick the highest candidate at or below the lane curve. Both halves are false
on the shipped bundle, and this file is what showed it:

  * Nothing is being shadowed. Segment meshes are single-layer shells — 24,059 of 24,060
    samples have exactly one surface beneath them. Enumerating every hit and keeping the
    nearest-to-expected rule reproduces the shipped rule's numbers to the digit.
  * Picking the highest candidate below the lane curve is twice as bad (0.46% of samples more
    than 2 cm above their own road, against the shipped rule's 0.21%).
  * A choice only ever arises between NEIGHBOURING objects, and only where they stack: 2.1% of
    samples at the city's densest 300 m window, 0.0% in a plain road window.

So the test now pins the shipped rule against those measurements and keeps the two alternatives
alongside it, so the comparison is rerunnable rather than remembered.

Ground truth is independent of the harness: this file parses the .glb and intersects the vertical
ray itself, in the bundle frame, in numpy. It shares no code with the implementation it checks,
so agreement means the geometry agrees, not that one bug is being compared against itself.

Run: `python tools/test_drape_pick.py [densest]`  (system Python + numpy; skipped when no bundle
is on disk).
"""

import json
import os
import sys

import numpy
import pytest
from csiibundle import bezier as bezier_math
from csiibundle import decals as decal_rules

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drapetruth import glb_triangles, hits_below

BUNDLE = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
    r"\Cities Skylines II\ModsData\Carto"
)

# The window to sweep, and how densely each marking is sampled. Stations run along the lane
# curve, laterals across the decal part's own width — the two axes `_drape_block` samples. The
# window is the A/B render's, so a failure here and a pixel in that render are the same place.
# `REACH` widens the segment set beyond the marking set: a marking at the window's edge can be
# draped onto a segment whose own origin sits outside it.
CENTER = (2994.0, 1853.0)
RADIUS = 300.0
REACH = 150.0
STATIONS = 9
LATERALS = 5

# `python tools/test_drape_pick.py densest` re-centres on the busiest window in the city
# instead — where segments stack (interchanges, overpasses, bridges over surface roads) and
# more than one object can therefore stand under one sample. A rule about choosing BETWEEN
# surfaces is only exercised where there is a choice, so the quiet window alone would prove
# nothing about it.
DENSEST = "densest" in sys.argv[1:]

# The lifts and the curve evaluator come from `csiibundle` rather than being regexed out of the
# harness, which imports `bpy` at module scope and cannot be imported under system Python. `H`
# stays as the one place naming what this test pins, so a rename fails loudly here.
H = {
    "_DECAL_LIFT": decal_rules.BEND_LIFT,
    "_DECAL_DRAPE_MARKING": decal_rules.LIFT["lane_marking"],
    "_DECAL_DRAPE_TINT": decal_rules.LIFT["lane_tint"],
    "_bezier_point_vec": bezier_math.evaluate,
}

# How far above the lane curve a candidate may stand and still plausibly be the surface the
# marking is painted on. The lane curve IS the carriageway (measured `road_top - lane_path.y`
# has median 0.000 m) and every kerb, median and parapet stands at least 0.15 m proud, so this
# separates two well-spaced populations rather than being fitted to either. Used by the
# `highest-below-the-curve` alternative and to describe the residual, never by the harness.
TOLERANCE = 0.05

# Recorded baselines, measured 2026-08-11 on the 2026-08-10 full-city rip. `lift` is the share
# of samples landing more than 2 cm above their own segment's surface; `worst` the largest such
# excursion, in metres. The shipped rule must not regress past these.
BASELINE = {"default": {"lift": 0.0005, "worst": 0.02}, "densest": {"lift": 0.0030, "worst": 0.31}}


def pick_old(per_object, expected):
    """The rule this test retires: ONE candidate per object — its topmost surface in the
    window, all `BVHTree.ray_cast` can return — then the candidate nearest the expected
    height."""
    tops = [hits[0] for hits in per_object if len(hits)]
    if not tops:
        return None
    return float(min(tops, key=lambda c: abs(c - expected)))


def _every(per_object):
    live = [hits for hits in per_object if len(hits)]
    return numpy.concatenate(live) if live else numpy.zeros(0)


def pick_new(per_object, expected):
    """Every surface of every object is a candidate; take the highest that is not standing
    above the lane curve, falling back to the lowest when they all are."""
    every = _every(per_object)
    if not len(every):
        return None
    below = every[every <= expected + TOLERANCE]
    return float(below.max()) if len(below) else float(every.min())


def pick_all_nearest(per_object, expected):
    """Enumerate every surface, but keep the OLD nearest-to-expected choice among them —
    separating "which candidates exist" from "which one wins", so the measurement says which
    of the two halves of the change is worth anything."""
    every = _every(per_object)
    if not len(every):
        return None
    return float(every[numpy.argmin(numpy.abs(every - expected))])


class _Sweep:
    """The sweep's accumulators, shared by the samplers and the report."""

    def __init__(self, rules):
        self.rules = rules
        self.stats = {key: [] for key, _ in rules}
        self.excess = {key: [] for key, _ in rules}
        self.roles = {}
        self.multi = 0
        self.samples = 0


def _window_center(every):
    """The sweep window's centre — `CENTER`, or the city's densest cell under `densest`."""
    if not DENSEST:
        return CENTER
    counts = {}
    for record in every:
        key = (int(record["pos"][0] // RADIUS), int(record["pos"][2] // RADIUS))
        counts[key] = counts.get(key, 0) + 1
    best = max(counts, key=counts.get)
    center = ((best[0] + 0.5) * RADIUS, (best[1] + 0.5) * RADIUS)
    print(f"densest {RADIUS:g} m cell: {counts[best]} segment(s) at {center[0]:g},{center[1]:g}")
    return center


def _segment_geometry(segments, mesh_dir):
    """World-frame triangles per segment, plus each one's XZ bounding box. This is the
    harness's `_ProjectionSurface` in miniature: it bins objects by bbox and casts into EVERY
    object near the sample, so a test that casts only into the marking's own segment cannot see
    the failure mode at all — at a junction it is a NEIGHBOURING segment's kerb or deck that
    wins."""
    geometry = {}
    for identifier, record in segments.items():
        path = os.path.join(mesh_dir, record["assetId"] + ".glb")
        if not os.path.isfile(path):
            continue
        try:
            tris = glb_triangles(path) + numpy.asarray(record["pos"], dtype=numpy.float64)
        except Exception as error:
            print(f"  note: {os.path.basename(path)} unreadable ({error})")
            continue
        if len(tris):
            flat = tris.reshape(-1, 3)
            geometry[identifier] = (
                tris,
                flat[:, 0].min(),
                flat[:, 0].max(),
                flat[:, 2].min(),
                flat[:, 2].max(),
            )
    return geometry


def _sample_point(sweep, geometry, order, boxes, owner, x, z, expected, top, bottom, station_y):
    """One drape sample through every rule, scored against the marking's own segment."""
    near = numpy.nonzero(
        (boxes[:, 0] <= x) & (x <= boxes[:, 1]) & (boxes[:, 2] <= z) & (z <= boxes[:, 3])
    )[0]
    per_object = []
    own = numpy.zeros(0)
    for k in near:
        hits = hits_below(geometry[order[k]][0], x, z, top, bottom)
        if len(hits):
            per_object.append(hits)
            if order[k] == owner:
                own = hits
    if not per_object:
        return
    sweep.samples += 1
    if len(per_object) > 1:
        sweep.multi += 1
    for key, pick in sweep.rules:
        chosen = pick(per_object, expected)
        sweep.stats[key].append(chosen - station_y)
        # The invariant's reference: the surface of the marking's OWN segment directly beneath
        # this point. That is the road the marking is painted on; anything a rule picks above
        # it came off something else.
        if len(own):
            sweep.excess[key].append(chosen - float(own[0]))


def _sweep_marking(sweep, record, decals, geometry, order, boxes):
    """Every decal part of one marking record, at STATIONS x LATERALS samples."""
    owner = record.get("owner")
    curve = numpy.asarray(record["curve"], dtype=numpy.float64)
    t = numpy.linspace(0.06, 0.94, STATIONS)
    points = H["_bezier_point_vec"](curve, t)
    tangent = numpy.gradient(points, axis=0)
    normal = numpy.stack((tangent[:, 2], numpy.zeros(len(t)), -tangent[:, 0]), axis=-1)
    normal = normal / numpy.maximum(numpy.linalg.norm(normal, axis=1, keepdims=True), 1e-9)

    for part in decals:
        sweep.roles[part.get("decalRole")] = sweep.roles.get(part.get("decalRole"), 0) + 1
        bmin = numpy.asarray(part["boundsMin"], dtype=numpy.float64)
        bmax = numpy.asarray(part["boundsMax"], dtype=numpy.float64)
        half_height = float(bmax[1] - bmin[1]) * 0.5
        half_width = float(bmax[0] - bmin[0]) * 0.5
        for lateral in numpy.linspace(-half_width, half_width, LATERALS):
            for i in range(len(t)):
                sample = points[i] + normal[i] * lateral
                expected = float(points[i][1]) + H["_DECAL_LIFT"]
                _sample_point(
                    sweep,
                    geometry,
                    order,
                    boxes,
                    owner,
                    float(sample[0]),
                    float(sample[2]),
                    expected,
                    expected + half_height,
                    expected - half_height,
                    float(points[i][1]),
                )


def main():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP no bundle at {BUNDLE}")
        return 0

    mesh_dir = os.path.join(BUNDLE, "networks")
    every = [
        record
        for record in _jsonl(os.path.join(BUNDLE, "networks.jsonl"))
        if record.get("kind") == "segment" and record.get("pos")
    ]
    center = _window_center(every)
    segments = {}
    for record in every:
        pos = record["pos"]
        if abs(pos[0] - center[0]) <= RADIUS + REACH and abs(pos[2] - center[1]) <= RADIUS + REACH:
            segments[record["id"]] = record

    with open(os.path.join(BUNDLE, "laneprefabs.json"), encoding="utf-8") as handle:
        catalogue = json.load(handle)

    geometry = _segment_geometry(segments, mesh_dir)
    if len(geometry) < 20:
        print(f"SKIP only {len(geometry)} segment mesh(es) in the window — nothing to measure")
        return 0
    order = list(geometry)
    boxes = numpy.asarray([geometry[i][1:] for i in order])

    sweep = _Sweep((("old", pick_old), ("new", pick_new), ("all-near", pick_all_nearest)))
    for record in _jsonl(os.path.join(BUNDLE, "lanemeshes.jsonl")):
        if record.get("owner") not in geometry:
            continue
        entry = catalogue.get(record.get("prefab"))
        if entry is None:
            continue
        decals = [
            entry["parts"][i]
            for i in record.get("parts", ())
            if i < len(entry.get("parts", ())) and entry["parts"][i].get("decal")
        ]
        if decals:
            _sweep_marking(sweep, record, decals, geometry, order, boxes)

    if not sweep.stats["new"]:
        print("SKIP no drape sample landed on any segment — nothing to measure")
        return 0
    return _report(sweep, geometry, center)


def _report(sweep, geometry, center):
    rules, _stats, excess = sweep.rules, sweep.stats, sweep.excess
    roles, multi, samples = sweep.roles, sweep.multi, sweep.samples
    failures = 0
    print(
        f"swept {len(geometry)} segment mesh(es) in a {RADIUS:g} m window at "
        f"{center[0]:g},{center[1]:g}; {samples} drape sample(s), {multi} with more than one "
        f"object under them; decal parts by role: "
        + ", ".join(f"{k or 'MISSING'}={v}" for k, v in sorted(roles.items(), key=str))
    )

    for key, _ in rules:
        over = numpy.asarray(excess[key])
        above = over[over > 0]
        print(
            f"  {key:3s}: above its own road  >2 cm {(over > 0.02).mean() * 100:5.2f}%   "
            f">5 cm {(over > 0.05).mean() * 100:5.2f}%   "
            f">10 cm {(over > 0.10).mean() * 100:5.2f}%   "
            f"max {above.max() if len(above) else 0.0:+.3f} m   "
            f"below it >5 cm {(over < -0.05).mean() * 100:5.2f}%"
        )

    # "old" IS the shipped rule — per-object topmost, then nearest-to-expected. The other two
    # are the alternatives this test retired; they are measured every run so the comparison
    # stays live rather than being a claim in a comment.
    shipped = numpy.asarray(excess["old"])
    lift = float((shipped > 0.02).mean())
    worst = float(shipped.max()) if len(shipped) else 0.0
    baseline = BASELINE["densest" if DENSEST else "default"]

    if lift > baseline["lift"] or worst > baseline["worst"]:
        print(
            f"FAIL the shipped pick regressed: {lift * 100:.2f}% of samples over 2 cm above "
            f"their own road (baseline {baseline['lift'] * 100:.2f}%), worst {worst:+.3f} m "
            f"(baseline {baseline['worst']:+.3f} m)"
        )
        failures += 1
    else:
        print(
            f"PASS the shipped pick is within its baseline: {lift * 100:.2f}% over 2 cm "
            f"(<= {baseline['lift'] * 100:.2f}%), worst {worst:+.3f} m "
            f"(<= {baseline['worst']:+.3f} m) over {len(shipped)} sample(s)"
        )

    # And no alternative may quietly become the better rule without this saying so.
    better = [
        key
        for key, _ in rules
        if key != "old" and (numpy.asarray(excess[key]) > 0.02).mean() < lift - 1e-9
    ]
    if better:
        print(
            f"NOTE {', '.join(better)} now lifts fewer samples than the shipped rule — "
            "the choice made on 2026-08-11 deserves revisiting"
        )

    print("test_drape_pick:", "FAIL" if failures else "OK")
    return 1 if failures else 0


def _jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


if __name__ == "__main__":
    sys.exit(main())


@pytest.mark.needs_rip
def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_drape_pick.py`."""
    assert main() in (None, 0)
