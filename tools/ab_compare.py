"""A/B gate: pixel-diff two renders, or diff the numbers in two import logs.

python tools/ab_compare.py a.png b.png
python tools/ab_compare.py --counts a.log b.log
"""

import re
import sys
from collections import defaultdict

import numpy
from PIL import Image

COUNT_MARKERS = (
    "lanes:",
    "draped",
    "splatmap calibrated",
    "terrain:",
    "face(s)",
    "networks:",
    "category ",
    "record(s)",
    "object(s)",
    "instance(s)",
    "prefab(s)",
    "tinted:",
    "skipped:",
)

# Elapsed-time numbers (e.g. "placed 101 instance(s) from 58 prefab(s) in 21.2 s")
# are run-to-run noise, not a count worth gating on. Strip them before extracting
# digits so timing jitter never fails an otherwise-identical comparison.
_TIMING_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?\s*s\b")

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


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
    """One entry per COUNT_MARKERS line: `(key, numbers, raw_line, line_number)`.

    `key` is the line with every number blanked to `#` — the identity a line is matched on
    between two logs. `numbers` is the list of numbers actually being gated. Matching on this
    text key rather than list position is what lets one inserted or removed line (a phase that
    gained a new summary line between two runs, say) be reported as exactly that, instead of
    shifting every later line's position and cascading a false mismatch through the rest of
    the file — the position-indexed comparison this replaced (plain `zip()` over both lists)
    could not tell "a line moved" from "a number changed".
    """
    entries = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            if any(marker in line for marker in COUNT_MARKERS):
                cleaned = _TIMING_RE.sub("in TIME s", line)
                numbers = _NUMBER_RE.findall(cleaned)
                key = _NUMBER_RE.sub("#", cleaned).strip()
                entries.append((key, numbers, line.rstrip("\n"), lineno))
    return entries


def compare_counts(path_a, path_b):
    a, b = count_lines(path_a), count_lines(path_b)
    if not a and not b:
        # An empty match on both sides is never evidence of equality -- it means
        # COUNT_MARKERS found nothing to gate on, which is a coverage bug, not a pass.
        print("FAIL no count lines matched in either log")
        return 1

    # Group each side by key (marker text with digits blanked), preserving each key's
    # within-file order. Two lines sharing a key (e.g. a repeated progress-stride line) are
    # matched to each other in that order; a key present more times on one side than the
    # other reports the surplus as added/removed rather than pairing it with an unrelated line.
    groups_a, groups_b = defaultdict(list), defaultdict(list)
    for key, numbers, text, lineno in a:
        groups_a[key].append((numbers, text, lineno))
    for key, numbers, text, lineno in b:
        groups_b[key].append((numbers, text, lineno))

    changed, added, removed = [], [], []
    # Stable order: as first seen in a, then any key new to b.
    ordered_keys = list(dict.fromkeys([k for k, *_ in a] + [k for k, *_ in b]))

    for key in ordered_keys:
        list_a, list_b = groups_a.get(key, []), groups_b.get(key, [])
        matched = min(len(list_a), len(list_b))
        for i in range(matched):
            numbers_a, text_a, line_a = list_a[i]
            numbers_b, text_b, line_b = list_b[i]
            if numbers_a != numbers_b:
                changed.append((line_a, line_b, text_a, text_b, numbers_a, numbers_b))
        for _numbers, text, lineno in list_a[matched:]:
            removed.append((lineno, text))
        for _numbers, text, lineno in list_b[matched:]:
            added.append((lineno, text))

    if not changed and not added and not removed:
        print(f"PASS {len(a)} count line(s) identical")
        return 0

    print(
        f"FAIL {len(changed)} line(s) changed, {len(added)} added, {len(removed)} removed "
        f"(of {len(a)} vs {len(b)} count line(s))"
    )
    for line_a, line_b, text_a, text_b, numbers_a, numbers_b in changed:
        print(f"  changed a:{line_a} vs b:{line_b}: {numbers_a} != {numbers_b}")
        print(f"    a: {text_a}")
        print(f"    b: {text_b}")
    for lineno, text in added:
        print(f"  added   b:{lineno}: {text}")
    for lineno, text in removed:
        print(f"  removed a:{lineno}: {text}")
    return 1


if __name__ == "__main__":
    if sys.argv[1] == "--counts":
        sys.exit(compare_counts(sys.argv[2], sys.argv[3]))
    sys.exit(compare_pixels(sys.argv[1], sys.argv[2]))
