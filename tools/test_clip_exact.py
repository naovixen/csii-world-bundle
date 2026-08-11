"""Exact terrain-clip check: the cut must land on the prism boundary, and nowhere else.

Contract §8c says a consumer deletes what falls inside the clip volumes, and
`build_terrain` is the reference implementation of that. It cuts each terrain cell along the
prism's own boundary — four lateral walls and two height-band surfaces — instead of deciding
whole 3.5 m faces by a sample point, so `deleted ⊆ covered by road mesh` holds by construction,
as it does in game.

That only works if the clipper is exact, and "looks right in a render" cannot show it. This
pins the arithmetic on cases with a known answer. It has already earned its keep twice:

  * `_split_convex` tested the sign change in ONE direction (`>` rather than `!=`), so every
    edge the cut line entered kept its first intersection and lost its second. The rings were
    plausible and not closed, and Blender's `mesh.validate()` silently dropped 875 faces.
  * A station quad of zero area has zero-length walls, so every constraint value is exactly 0 —
    and a zero constraint puts the WHOLE piece on both sides of the line, so the piece was kept
    once per wall. That put 836 duplicate faces into the mesh, which `validate()` also drops.

Both produced a terrain that rendered almost correctly. Neither would have been caught by
anything short of an area identity.

Run: `python tools/test_clip_exact.py`  (system Python + numpy; no bundle needed).
"""
import math
import re
import os
import sys

import numpy

SRC = os.path.join(os.path.dirname(__file__), "blender_import_bundle.py")
CELL = 3.5
FULL = CELL * CELL

source = open(SRC, encoding="utf-8").read()
H = {"numpy": numpy, "math": math}
for name in ("_CLIP_WELD", "_CLIP_MIN_EDGE", "_CLIP_MIN_AREA", "_CLIP_MAX_PIECES"):
    match = re.search(rf"^{name} = .*$", source, re.M)
    assert match, f"{name} not found in harness source"
    exec(compile(match.group(0), f"<{name}>", "exec"), H)
for name in ("_split_convex", "_subtract_prism", "_clean_ring"):
    match = re.search(rf"\ndef {name}\(.*?(?=\n\ndef |\n\nclass |\Z)", source, re.S)
    assert match, f"{name} not found in harness source"
    exec(compile(match.group(0), f"<{name}>", "exec"), H)


def area(ring):
    total = 0.0
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        total += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return abs(total) * 0.5


def cut(quad, lane_y, bottom, top, height=10.0):
    """Rings kept when one prism is subtracted from a single cell at a uniform height."""
    nw = (0.0, CELL, height)
    sw = (0.0, 0.0, height)
    se = (CELL, 0.0, height)
    ne = (CELL, CELL, height)
    pieces = H["_subtract_prism"]([[nw, sw, se], [nw, se, ne]], quad, lane_y, bottom, top)
    return [r for r in (H["_clean_ring"](p) for p in pieces) if r]


BOX = ((-1.0, -1.0), (5.0, -1.0), (5.0, 5.0), (-1.0, 5.0))          # covers the whole cell
LEFT_HALF = ((-1.0, -1.0), (1.75, -1.0), (1.75, 5.0), (-1.0, 5.0))
MIDDLE = ((1.0, -1.0), (2.0, -1.0), (2.0, 5.0), (1.0, 5.0))
AWAY = ((10.0, 0.0), (12.0, 0.0), (12.0, 4.0), (10.0, 4.0))
DEGENERATE = ((1.0, 1.0), (1.0, 1.0), (1.0, 1.0), (1.0, 1.0))
SLIVER = ((0.0, 0.0), (CELL, 0.0), (CELL, 0.0), (0.0, 0.0))          # zero-width, zero area

CASES = (
    ("prism misses the cell entirely", AWAY, 10.0, -0.5, 0.5, FULL),
    ("prism covers the whole cell", BOX, 10.0, -0.5, 0.5, 0.0),
    ("prism takes the left half", LEFT_HALF, 10.0, -0.5, 0.5, FULL / 2),
    ("prism takes a middle band", MIDDLE, 10.0, -0.5, 0.5, FULL - CELL),
    ("band sits above the terrain", BOX, 5.0, -0.5, 0.5, FULL),
    ("band sits below the terrain", BOX, 15.0, -0.5, 0.5, FULL),
    ("band straddles the terrain", BOX, 10.4, -0.5, 0.5, 0.0),
    ("degenerate prism, one point", DEGENERATE, 10.0, -0.5, 0.5, FULL),
    ("degenerate prism, zero width", SLIVER, 10.0, -0.5, 0.5, FULL),
)


def main():
    failures = 0
    for label, quad, lane_y, bottom, top, expected in CASES:
        rings = cut(quad, lane_y, bottom, top)
        kept = sum(area(r) for r in rings)
        keys = [tuple(sorted((round(v[0], 6), round(v[1], 6)) for v in r)) for r in rings]
        duplicates = len(keys) - len(set(keys))
        bad = abs(kept - expected) > 1e-9 or duplicates
        failures += bool(bad)
        print(f"  {'FAIL' if bad else 'ok  '} {label:34s} kept {kept:7.4f} of {FULL:.4f} "
              f"(expected {expected:7.4f}), {len(rings)} ring(s), {duplicates} duplicate(s)")

    # The identity that matters: whatever the prism, kept + removed = the cell, and no piece
    # is ever emitted twice. Swept over a prism walking across the cell at every angle.
    for step in range(60):
        angle = step * math.pi / 30.0
        cx = CELL * 0.5 + math.cos(angle) * 1.3
        cz = CELL * 0.5 + math.sin(angle) * 1.3
        half = 0.9
        quad = ((cx - half, cz - half), (cx + half, cz - half),
                (cx + half, cz + half), (cx - half, cz + half))
        rings = cut(quad, 10.0, -0.5, 0.5)
        kept = sum(area(r) for r in rings)
        overlap = (min(cx + half, CELL) - max(cx - half, 0.0)) * \
                  (min(cz + half, CELL) - max(cz - half, 0.0))
        overlap = max(overlap, 0.0)
        keys = [tuple(sorted((round(v[0], 6), round(v[1], 6)) for v in r)) for r in rings]
        if abs(kept - (FULL - overlap)) > 1e-9 or len(keys) != len(set(keys)):
            print(f"  FAIL sweep at {math.degrees(angle):5.0f} deg: kept {kept:.6f}, "
                  f"expected {FULL - overlap:.6f}, {len(keys) - len(set(keys))} duplicate(s)")
            failures += 1
            break
    else:
        print("  ok   sweep of 60 prism positions: kept + removed = the cell, no duplicates")

    print("test_clip_exact:", "FAIL" if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
