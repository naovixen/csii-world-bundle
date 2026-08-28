"""Gate for `blenderbundle.survey` — where a region import decides to point itself.

Run: `python tools/test_survey.py` (instant, no Blender and no real bundle needed).

This is the one module of the Blender adapter that never imports `bpy`, and that is deliberate:
it is arithmetic over `instances.jsonl` and `networks.jsonl`, so it is the one part of the
adapter a test can reach at all. Everything else in `tools/blenderbundle/` needs Blender's own
interpreter, which is why the adapter's gate is `tools/ab_scene.py` run under `blender -b`.

What is pinned here is the three decisions behind `--center auto`, `--center downtown` and
`--center interchange`. They matter because the alternative to getting them right is not a
crash: it is importing a patch of empty ground and concluding the bundle is broken. The
skyline weight in particular is a JUDGEMENT — the square of height, so five towers outrank a
hundred houses — and the numbers in its docstring are checked below rather than trusted.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from blenderbundle import survey  # noqa: E402

CHECKS = 0
FAILS = []


def check(ok, claim):
    """One claim, counted; a failure is recorded and the run continues."""
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(claim)
    print(f"  {'ok  ' if ok else 'FAIL'}  {claim}")


def _write(directory, name, records):
    """One JSONL layer written into `directory`; returns its path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _building(x, z, height=None):
    attrs = {} if height is None else {"height": height}
    return {"kind": "building", "assetId": "b", "pos": [x, 0.0, z], "attrs": attrs}


def test_skyline_weight_is_height_squared():
    """The weight is height², and a building with no usable height still counts as one."""
    check(survey.skyline_weight(_building(0, 0, 60.0)) == 3600.0, "a 60 m tower weighs 3600")
    check(survey.skyline_weight(_building(0, 0, 4.0)) == 16.0, "a 4 m house weighs 16")
    check(survey.skyline_weight(_building(0, 0)) == 1.0, "no height attribute weighs 1, not 0")
    check(survey.skyline_weight(_building(0, 0, "tall")) == 1.0, "an unparseable height weighs 1")
    check(survey.skyline_weight(_building(0, 0, 0.5)) == 1.0, "height below 1 m is floored at 1")


def test_skyline_weight_beats_a_plain_count():
    """The docstring's own arithmetic: five 60 m towers outrank a hundred 4 m houses 18000 to
    1600, where a plain count puts the houses twenty to one. That inversion is the whole reason
    `--center downtown` exists beside `--center auto`."""
    towers = 5 * survey.skyline_weight(_building(0, 0, 60.0))
    houses = 100 * survey.skyline_weight(_building(0, 0, 4.0))
    check(towers == 18000.0, f"five 60 m towers score 18000 (got {towers:g})")
    check(houses == 1600.0, f"a hundred 4 m houses score 1600 (got {houses:g})")
    check(towers > houses, "the towers win on skyline weight")
    check(100 > 5, "and lose on a plain count — which is what --center auto would pick")


def test_locate_bounds_centroid_and_densest():
    """`locate` reports where the selection is, and picks the densest 250 m cell by count."""
    with tempfile.TemporaryDirectory() as directory:
        path = _write(
            directory,
            "instances.jsonl",
            [
                _building(10, 10),
                _building(20, 30),
                _building(1010, 1010),
                {"kind": "prop", "pos": [0, 0, 0]},
            ],
        )
        where = survey.locate(path, {"building"})
    check(where["count"] == 3, "counts only the selected kinds")
    check(
        where["bounds"] == (10, 1010, 10, 1010), f"bounds are min/max X then Z ({where['bounds']})"
    )
    check(
        where["centroid"] == ((10 + 20 + 1010) / 3, (10 + 30 + 1010) / 3),
        "the centroid is the mean of the selected positions",
    )
    check(
        where["densest"] == (125.0, 125.0),
        f"the densest 250 m cell is centred ({where['densest']})",
    )
    check(where["densest_count"] == 2, "and holds the two buildings near the origin")


def test_locate_is_none_on_an_empty_selection():
    """No records of the selected kinds is None, not a zero-division — the caller prints
    'no instances of kinds ...' and exits rather than importing empty ground."""
    with tempfile.TemporaryDirectory() as directory:
        path = _write(directory, "instances.jsonl", [{"kind": "prop", "pos": [0, 0, 0]}])
        check(survey.locate(path, {"building"}) is None, "an empty selection surveys as None")
        check(survey.locate(path, set()) is None, "and so does an empty kind set")


def test_locate_weight_moves_the_densest_cell():
    """`--center downtown` is `--center auto` with the skyline weight, and the weight is what
    moves the answer: one tower outranks three houses in the next cell."""
    with tempfile.TemporaryDirectory() as directory:
        path = _write(
            directory,
            "instances.jsonl",
            [
                _building(100, 100, 60.0),
                _building(400, 100, 4.0),
                _building(410, 100, 4.0),
                _building(420, 100, 4.0),
            ],
        )
        by_count = survey.locate(path, {"building"})
        by_skyline = survey.locate(path, {"building"}, weight=survey.skyline_weight)
    check(by_count["densest"] == (375.0, 125.0), "by count, the three houses win")
    check(by_skyline["densest"] == (125.0, 125.0), "by skyline weight, the one tower wins")


def test_interchange_needs_a_highway():
    """No highway and no elevated record is None — the caller then says so and suggests
    `--center downtown`, rather than centring on nothing."""
    with tempfile.TemporaryDirectory() as directory:
        instances = _write(directory, "instances.jsonl", [_building(100, 100, 60.0)])
        plain = _write(
            directory,
            "networks.jsonl",
            [{"pos": [100, 0, 100], "attrs": {"category": "road", "form": "normal"}}],
        )
        check(
            survey.interchange(instances, plain) is None, "a city of plain roads has no interchange"
        )
        check(
            survey.interchange(instances, os.path.join(directory, "absent.jsonl")) is None,
            "and a bundle with no networks.jsonl at all is None, not an error",
        )


def test_interchange_scores_the_neighbourhood():
    """A junction cell often holds no buildings itself, so the score is the skyline over its 3x3
    neighbourhood. Two junctions here: one beside a tower, one beside nothing."""
    with tempfile.TemporaryDirectory() as directory:
        instances = _write(directory, "instances.jsonl", [_building(100, 100, 60.0)])
        networks = _write(
            directory,
            "networks.jsonl",
            [
                # In the cell NORTH of the tower's — no buildings of its own.
                {"pos": [100, 0, 300], "attrs": {"category": "highway", "form": "normal"}},
                # Far away, and elevated rather than highway — the other half of the test.
                {"pos": [5000, 0, 5000], "attrs": {"category": "road", "form": "elevated"}},
            ],
        )
        found = survey.interchange(instances, networks)
    check(found == (125.0, 375.0), f"the junction next to the skyline wins ({found})")
    check(found != (5125.0, 5125.0), "the empty elevated junction does not")


def test_interchange_reads_a_centerline_record():
    """A record with no `pos` is located by the first point of its centerline — the form every
    network record in a real rip actually takes."""
    with tempfile.TemporaryDirectory() as directory:
        instances = _write(directory, "instances.jsonl", [_building(100, 100, 60.0)])
        networks = _write(
            directory,
            "networks.jsonl",
            [
                {
                    "centerline": [[100, 0, 100], [200, 0, 200]],
                    "attrs": {"category": "highway", "form": "normal"},
                }
            ],
        )
        check(
            survey.interchange(instances, networks) == (125.0, 125.0),
            "a centerline record is placed at its first point",
        )


def test_the_example_bundle_surveys():
    """End to end on the committed contract artefact, so the reader knows this runs on a real
    layer and not only on fixtures."""
    bundle = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")
    where = survey.locate(os.path.join(bundle, "instances.jsonl"), {"building"})
    check(where["count"] == 2, f"docs/example-bundle holds 2 buildings (got {where['count']})")
    check(where["densest_count"] == 2, "both in one 250 m cell")
    check(
        survey.interchange(
            os.path.join(bundle, "instances.jsonl"), os.path.join(bundle, "networks.jsonl")
        )
        is None,
        "its one road is not a highway, so there is no interchange to centre on",
    )


def gate():
    """Every claim above, as one pytest test."""
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            print(f"\n{name}")
            function()
    print(f"\n{CHECKS} checks, {len(FAILS)} failed")
    for claim in FAILS:
        print(f"  FAILED: {claim}")
    assert not FAILS, f"{len(FAILS)} of {CHECKS} survey claims failed"


if __name__ == "__main__":
    gate()
