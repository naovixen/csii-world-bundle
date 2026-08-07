"""A/B gate: pixel-diff two renders, or diff the numbers in two import logs.

  python tools/ab_compare.py a.png b.png
  python tools/ab_compare.py --counts a.log b.log
"""
import re
import sys

import numpy
from PIL import Image

COUNT_MARKERS = ("lanes:", "draped", "splatmap calibrated", "terrain:", "face(s)",
                 "networks:", "category ", "record(s)", "object(s)",
                 "instance(s)", "prefab(s)", "tinted:", "skipped:")

# Elapsed-time numbers (e.g. "placed 101 instance(s) from 58 prefab(s) in 21.2 s")
# are run-to-run noise, not a count worth gating on. Strip them before extracting
# digits so timing jitter never fails an otherwise-identical comparison.
_TIMING_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?\s*s\b")


def compare_pixels(path_a, path_b):
    a = numpy.asarray(Image.open(path_a).convert("RGB")).astype(int)
    b = numpy.asarray(Image.open(path_b).convert("RGB")).astype(int)
    if a.shape != b.shape:
        print(f"FAIL shape {a.shape} vs {b.shape}")
        return 1
    diff = numpy.abs(a - b).max(axis=2)
    frac = (diff <= 2).mean()
    peak = diff.max()
    verdict = "PASS" if frac >= 0.999 and peak <= 16 else "FAIL"
    print(f"{verdict} frac={frac:.5f} max={peak}")
    return 0 if verdict == "PASS" else 1


def count_lines(path):
    keep = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if any(marker in line for marker in COUNT_MARKERS):
                cleaned = _TIMING_RE.sub("in TIME s", line)
                keep.append(re.findall(r"-?\d+(?:\.\d+)?", cleaned))
    return keep


def compare_counts(path_a, path_b):
    a, b = count_lines(path_a), count_lines(path_b)
    if not a and not b:
        # An empty match on both sides is never evidence of equality -- it means
        # COUNT_MARKERS found nothing to gate on, which is a coverage bug, not a pass.
        print("FAIL no count lines matched in either log")
        return 1
    if a == b:
        print(f"PASS {len(a)} count line(s) identical")
        return 0
    print(f"FAIL count lines differ ({len(a)} vs {len(b)} lines)")
    for i, (la, lb) in enumerate(zip(a, b)):
        if la != lb:
            print(f"  line {i}: {la} != {lb}")
    return 1


if __name__ == "__main__":
    if sys.argv[1] == "--counts":
        sys.exit(compare_counts(sys.argv[2], sys.argv[3]))
    sys.exit(compare_pixels(sys.argv[1], sys.argv[2]))
