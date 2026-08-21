"""Normal-map packing check: which layout each map is in, and whether the rebuild is right.

A two-channel normal map ships X and Y and lets the shader reconstruct Z. CS2's art arrives in
both of the layouts Unity's `UnpackNormalmapRGorAG` straddles — **AG** (X in alpha, red pinned
white) and **RG** (X in red, blue pinned black) — and a consumer reading either as ordinary
tangent RGB gets a normal that faces away from every light: AG gives `x = +1`, flat along the
surface, and RG gives `z = -1`, pointing into it. Both shade BLACK, which is what
"the areas are rendering black" was.

`csiibundle.materials.detect_normal_packing` tells the three apart and `unpack_normal` rebuilds
one. This test pins the claims that rest on:

  1. The detector is right on maps whose layout is known by construction — a synthetic AG map,
     a synthetic RG map, and a conforming RGB one — and it does NOT reclassify an RGB map that
     merely happens to be flat, or one whose alpha carries unrelated data (the terrain grounds
     are exactly that, and misreading them would break a path that works today).
  2. The rebuild is exact: pack a known unit normal into each layout, detect, unpack, and the
     original comes back within 8-bit quantisation.
  3. The reading a consumer would get WITHOUT the rebuild really is the black one — asserted as
     the shading term N.L against a sun overhead, not as a hand-wave.
  4. On the bundle as it stands, every normal map is classified, and the counts are reported by
     layer so a re-rip can be checked against them.

Run: `python tools/test_normal_packing.py`  (system Python + numpy; the real-bundle half is
skipped when no bundle is on disk, and needs PIL only if a map is not 8-bit RGBA PNG).
"""

import json
import math
import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle import materials
from csiibundle import png as png_reader

BUNDLE = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    "AppData",
    "LocalLow",
    "Colossal Order",
    "Cities Skylines II",
    "ModsData",
    "Carto",
)

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


def quantise(value):
    """0-1 through an 8-bit channel, the way a PNG stores it."""
    return round(value * 255.0) / 255.0


def pack(normal, layout):
    """One unit normal `(x, y, z)` as an RGBA texel in `layout`."""
    x, y, z = normal
    if layout == materials.NORMAL_RGB:
        channels = (x * 0.5 + 0.5, y * 0.5 + 0.5, z * 0.5 + 0.5, 1.0)
    elif layout == materials.NORMAL_AG:
        # Red pinned white, blue left mirroring green, X hidden in alpha.
        channels = (1.0, y * 0.5 + 0.5, y * 0.5 + 0.5, x * 0.5 + 0.5)
    else:
        # Blue left at black, alpha at one.
        channels = (x * 0.5 + 0.5, y * 0.5 + 0.5, 0.0, 1.0)
    return tuple(quantise(c) for c in channels)


def normals(count=64):
    """A spread of unit normals leaning in every direction, none of them degenerate."""
    out = []
    for i in range(count):
        angle = (i / count) * 2.0 * math.pi
        tilt = 0.15 + 0.55 * (i % 5) / 4.0  # 0.15..0.70 of a right angle
        x, y = math.cos(angle) * tilt, math.sin(angle) * tilt
        out.append((x, y, math.sqrt(max(0.0, 1.0 - x * x - y * y))))
    return out


print("1. detection on maps whose layout is known by construction")
for layout in (materials.NORMAL_RGB, materials.NORMAL_AG, materials.NORMAL_RG):
    samples = [pack(n, layout) for n in normals()]
    got = materials.detect_normal_packing(samples)
    check(f"a synthetic {layout} map detects as {layout}", got == layout, f"got {got}")

flat = [pack((0.0, 0.0, 1.0), materials.NORMAL_RGB)] * 64
check(
    "a perfectly flat RGB map is not mistaken for a packing",
    materials.detect_normal_packing(flat) == materials.NORMAL_RGB,
    f"got {materials.detect_normal_packing(flat)}",
)

# The terrain grounds: tangent RGB whose ALPHA carries unrelated data (mean 0.27-0.66, varying).
# An AG test that keyed on "alpha varies" alone would swallow these and wreck a working path.
data_alpha = [
    (*list(pack(n, materials.NORMAL_RGB)[:3]), quantise(0.2 + 0.5 * (i % 3) / 2))
    for i, n in enumerate(normals())
]
check(
    "an RGB map with data in its alpha stays RGB",
    materials.detect_normal_packing(data_alpha) == materials.NORMAL_RGB,
    f"got {materials.detect_normal_packing(data_alpha)}",
)

check(
    "an empty sample is RGB rather than an exception",
    materials.detect_normal_packing([]) == materials.NORMAL_RGB,
)

print("\n2. the rebuild returns the normal that was packed")
for layout in (materials.NORMAL_RGB, materials.NORMAL_AG, materials.NORMAL_RG):
    worst = 0.0
    for source in normals():
        texel = pack(source, layout)
        got = materials.unpack_normal(layout, *texel)
        worst = max(worst, max(abs(a - b) for a, b in zip(source, got)))
    # One 8-bit step is 1/255 in the stored 0-1 channel, so 2/255 in the -1..1 normal, and the
    # reconstructed Z compounds two of those through a square root near its flat end.
    check(f"{layout} round-trips within 8-bit quantisation", worst < 0.02, f"worst {worst:.5f}")

print("\n3. reading a packed map as RGB really is the black one")
sun = (0.0, 0.0, 1.0)  # straight down onto level ground: the friendliest possible
for layout, expect in (
    (materials.NORMAL_AG, "x = +1, flat along the surface"),
    (materials.NORMAL_RG, "z = -1, into the surface"),
):
    lit_raw, lit_rebuilt = [], []
    for source in normals():
        r, g, b, a = pack(source, layout)
        raw = (2 * r - 1, 2 * g - 1, 2 * b - 1)  # what a consumer reads without this
        length = math.sqrt(sum(c * c for c in raw)) or 1.0
        lit_raw.append(sum(c * s for c, s in zip(raw, sun)) / length)
        lit_rebuilt.append(
            sum(c * s for c, s in zip(materials.unpack_normal(layout, r, g, b, a), sun))
        )

    # The honest claim is the MEAN, not the worst texel. An AG map read as RGB is pinned to
    # x = +1 and takes its z from blue, which mirrors green — so a texel whose y leans hard
    # still catches some light (up to N.L 0.49 on this synthetic spread, which leans further
    # than real ground art does: the bundle's own AG maps have green centred at 128, i.e.
    # y = 0, i.e. N.L = 0). What makes the surface read as black is that on average it keeps
    # almost nothing, and that is what this asserts.
    raw_mean, rebuilt_mean = sum(lit_raw) / len(lit_raw), sum(lit_rebuilt) / len(lit_rebuilt)
    check(
        f"{layout} read as RGB loses ~all its light ({expect})",
        raw_mean < 0.1 * rebuilt_mean,
        f"mean N.L {raw_mean:+.3f} vs {rebuilt_mean:+.3f} rebuilt",
    )
    check(
        f"{layout} rebuilt shades like ground facing the sun",
        min(lit_rebuilt) > 0.7,
        f"worst N.L {min(lit_rebuilt):+.3f}",
    )

# The bundle's own AG maps, whose green really is centred at 128 — the flat-y case the note
# above appeals to, checked rather than asserted.
flat_y_ag = [
    pack((x, 0.0, math.sqrt(1.0 - x * x)), materials.NORMAL_AG)
    for x in (i / 32.0 - 0.5 for i in range(32))
]
worst_lit = max(
    abs((2 * b - 1) / math.sqrt((2 * r - 1) ** 2 + (2 * g - 1) ** 2 + (2 * b - 1) ** 2))
    for r, g, b, a in flat_y_ag
)
check(
    "an AG map with green centred at 128 reads as exactly zero light",
    worst_lit < 0.01,
    f"worst |N.L| {worst_lit:.4f}",
)

print("\n4. the bundle as it stands")
catalogue_path = os.path.join(BUNDLE, "areaprefabs.json")
if not os.path.isfile(catalogue_path):
    print(f"  SKIP  no bundle at {BUNDLE}")
else:

    def sample_png(path, stride=64):
        pixels = png_reader.rgba_array(path).reshape(-1, 4).astype(numpy.float32) / 255.0
        return [tuple(float(v) for v in texel) for texel in pixels[::stride]]

    def report(label, paths):
        counts = {}
        for path in paths:
            packing = materials.detect_normal_packing(sample_png(path))
            counts[packing] = counts.get(packing, 0) + 1
        print(f"  {label}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
        return counts

    with open(catalogue_path, encoding="utf-8") as handle:
        catalogue = json.load(handle)
    area_maps = sorted(
        {os.path.join(BUNDLE, v["normalMap"]) for v in catalogue.values() if v.get("normalMap")}
    )
    area_counts = report("area surfaces (§8b)", area_maps)

    surfaces = os.path.join(BUNDLE, "terrain", "surfaces")
    ground_maps = sorted(
        os.path.join(surfaces, f) for f in os.listdir(surfaces) if f.endswith("_normal.png")
    )
    ground_counts = report("terrain grounds (§8)", ground_maps)

    materials_path = os.path.join(BUNDLE, "materials.json")
    with open(materials_path, encoding="utf-8") as handle:
        table = json.load(handle)
    object_maps = sorted(
        {os.path.join(BUNDLE, v["normal"]) for v in table.values() if v.get("normal")}
    )[:40]
    object_counts = report("object materials (§7, first 40)", object_maps)

    check(
        "terrain grounds are all tangent RGB",
        set(ground_counts) == {materials.NORMAL_RGB},
        str(ground_counts),
    )
    check(
        "object normal maps are all tangent RGB",
        set(object_counts) == {materials.NORMAL_RGB},
        str(object_counts),
    )
    packed = sum(n for k, n in area_counts.items() if k != materials.NORMAL_RGB)
    if packed:
        print(
            f"  NOTE  {packed} area normal map(s) are still packed on disk — expected until "
            "the next re-rip carries TerrainRipSystem's rebuild; the harness rebuilds them"
        )

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
