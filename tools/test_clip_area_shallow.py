"""Only a DEEP clip band may delete terrain — for `area` records as well as strips (§8c).

`clipgrid._bin_deep` decides which records the containment pass (the deletion rule) sees at
all. It skipped shallow STRIPS and binned every `area` record unconditionally; on the reference
city all 266 area records are shallow (median top 0.3 m), so each one could delete the faces
under it while `deep_touch` stayed False and the build report showed nothing (validation pass
2026-08-31, A3). This pins the per-record test and the count the product reports.

Run: `python tools/test_clip_area_shallow.py`  (system Python + numpy; no bundle needed).
"""

import sys

import numpy

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


def main():
    from csiibundle import clipgrid, containment

    block = containment.CLIP_CONTAIN_BLOCK
    # an area record is `(cells (N, 2) col/row, lane_y, bottom, top)` — see `_rasterise_area`
    square = numpy.array([[10.0, 10.0], [40.0, 10.0], [40.0, 40.0], [10.0, 40.0]])
    shallow = (square, 500.0, -0.3, 0.3)  # a surface cut: top well under CLIP_DEEP_BAND_TOP
    deep = (square + 200.0, 500.0, -0.3, containment.CLIP_DEEP_BAND_TOP + 1.0)  # a portal
    bins, shallow_count = clipgrid._bin_deep([], [shallow, deep], 512, 512, block)
    binned = sorted({i for indices in bins.values() for i in indices})
    check(
        binned == [1],
        "only the DEEP area record is binned for the deletion test",
        f"binned {binned}",
    )
    check(shallow_count == 1, "and the shallow one is counted", f"{shallow_count}")
    # a record whose top is exactly the threshold is shallow: the rule is `top > CLIP_DEEP_BAND_TOP`
    edge = (square, 500.0, -0.3, containment.CLIP_DEEP_BAND_TOP)
    bins, shallow_count = clipgrid._bin_deep([], [edge], 512, 512, block)
    check(not bins and shallow_count == 1, "a top AT the threshold is shallow (strict >)")
    # the strips keep their own rule: `carves` is the last element of a strip_cells row
    lc = numpy.array([[10.0, 10.0], [20.0, 10.0]])
    strip_row = (
        lc,
        lc,
        (numpy.zeros(2), numpy.zeros(2)),
        -0.3,
        0.3,
        numpy.array([10.0]),
        numpy.array([10.0]),
        numpy.array([10.0]),
        numpy.array([20.0]),
        False,
    )
    bins, shallow_count = clipgrid._bin_deep([strip_row], [deep], 512, 512, block)
    binned = sorted({i for indices in bins.values() for i in indices})
    check(
        binned == [1] and shallow_count == 0,
        "a shallow strip is still skipped and not counted as an area",
    )
    print("test_clip_area_shallow: " + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py)."""
    assert main() == 0
