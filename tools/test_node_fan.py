"""Node fans must be paved: no junction flare sector may be left bare.

WHAT THIS GATES (audit §43, the close-out of §41.9 "FLARE"). At a node the game hands the
composition a fan of boundary curves and every cross-section piece is stretched across it. If a
piece that owns the outer part of a half frame produces no geometry, the fan's clip footprint is
still emitted, terrain is still treated as roadbed under it, and the sector renders as a bare
wedge at the junction. That is what §41.9 reported, and it survived three rips.

Its cause was NOT the piece-to-frame mapping. It was `GetPieceGeometry` reading the ECS
`MeshVertex`/`MeshIndex`/`MeshMaterial` buffers, which are a RENDER CACHE the game fills on
demand (`BatchMeshSystem.LoadMeshes`) and not authored asset data. A piece the renderer never
batched in that session has no buffers, and the shoulder piece that paves the outer band of an
`Alley Oneway` flare is one the renderer had never batched in any of the three sessions — so the
absence looked deterministic and was read as a mapping bug. `0eeb24f` made the geometry asset the
fallback, and the sector is paved.

WHAT IT MEASURES. For every `kind:"node"` clip strip in a window, walk stations along the strip
and sample across from the left rail to the right rail. A sample is covered when a triangle from
some `networks/seg_*.glb` is drawn over it. The score is how far in from each rail, in METRES,
you must move before the mesh covers you.

Metres, not per cent: a node emits near-degenerate wedges a few decimetres wide, on which any
fraction is noise (two of 5,872 read 28-33% unpaved for a 0.3 m gap). The defect this gates was
2.5-3.7 m of bare rail.

THE NEGATIVE CONTROL IS PART OF THE TEST. §41.8 shipped a gate that read 0.8 m² for the exact
behaviour that put the holes on screen, because its exemptions were derived from the rule under
test. So this file does not merely assert green: it re-scores the two segments §41.9 named with
their outer geometry amputated, and fails if the census does NOT then report the defect. A pass
therefore means the gate is live.

Run: `python tools/test_node_fan.py`  (system Python + numpy; skipped when no bundle is on disk).
"""
import collections
import json
import math
import os
import pathlib
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from inspect_glb import read_accessor, read_glb  # noqa: E402

BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")

# Truth raster pitch. A gap narrower than this is below what a 0.5 m cross-section piece can
# represent and is not what this test is about.
PX = 0.20

# Stations along a strip, and samples across it. Across-count sets the gap quantum: 41 samples
# over a 25 m frame resolve 0.6 m, which is why the budget below sits well above that.
STATIONS = 11
ACROSS = 41

RADIUS = 200.0

# The six windows the 2026-08-11 clip-cut and flare defects were reported in (same set as
# `test_clip_cut.py`, so a failure here can be read beside one there), plus the ten densest
# node-strip clusters in the city, chosen from the bundle itself so the sweep follows the save.
WINDOWS = (("width transition", 4324.5, 1550.5),
           ("road junction", 2936.9, 1397.5),
           ("train tracks", 873.9, 1442.0),
           ("clip areas", 450.0, -2900.0),
           ("trench cluster", -2650.0, 3850.0),
           ("deep cluster", 2750.0, -350.0))
DENSE_WINDOWS = 10

# Metres of bare rail a strip is allowed. Measured on the 2026-08-11 23:37 full rip over 5,872
# node strips: p50 0.10, p99 0.36, p99.9 0.64, max 0.93 m — the residual where the clip fan's
# outer curve and the mesh's outer edge disagree by less than the sampling quantum. The defect
# was 2.5-3.7 m, and the negative control below drives it to 4-7 m.
GAP_BUDGET = 1.5

# The two `Alley Oneway` segments §41.9 named at the width transition, and the piece-space |x|
# the failing `Road` primitive stopped at. Amputating there reproduces the reported defect.
CONTROL_SEGMENTS = ("seg_2301693", "seg_2302757")
CONTROL_CUT = 1.88
CONTROL_MIN_GAP = 2.5

failures = 0


def bezier(points, t):
    p = numpy.asarray(points, dtype=numpy.float64)
    t = numpy.asarray(t, dtype=numpy.float64)[:, None]
    u = 1.0 - t
    return u ** 3 * p[0] + 3 * u ** 2 * t * p[1] + 3 * u * t ** 2 * p[2] + t ** 3 * p[3]


def fill_triangles(mask, px, pz):
    """Scan-fill triangles given per-vertex in truth-pixel coordinates."""
    height, width = mask.shape
    for k in range(len(px)):
        y0 = max(0, int(math.floor(pz[k].min())))
        y1 = min(height - 1, int(math.ceil(pz[k].max())))
        x0 = max(0, int(math.floor(px[k].min())))
        x1 = min(width - 1, int(math.ceil(px[k].max())))
        if y1 < y0 or x1 < x0:
            continue
        ax, bx, cx = px[k]
        az, bz, cz = pz[k]
        det = (bz - cz) * (ax - cx) + (cx - bx) * (az - cz)
        if abs(det) < 1e-12:
            continue
        gx, gz = numpy.meshgrid(numpy.arange(x0, x1 + 1) + 0.5, numpy.arange(y0, y1 + 1) + 0.5)
        l1 = ((bz - cz) * (gx - cx) + (cx - bx) * (gz - cz)) / det
        l2 = ((cz - az) * (gx - cx) + (ax - cx) * (gz - cz)) / det
        inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
        if inside.any():
            mask[y0:y1 + 1, x0:x1 + 1] |= inside


def build_cover(cx, cz, radius, amputate=None):
    """Everything the network meshes draw over the window, as a boolean truth raster.

    glTF vertex to bundle frame is contract §2.2's instance placement with the harness's mirror,
    `world = pos + diag(-1, 1, 1) * v`; every network record ships an identity rotation.

    `amputate` is the negative control: a set of assetIds whose triangles reaching past
    `CONTROL_CUT` in piece space are dropped, standing in for a cross-section piece that produced
    no geometry.
    """
    size = int(2 * radius / PX) + 1
    x0, z0 = cx - radius, cz - radius
    cover = numpy.zeros((size, size), dtype=bool)
    segments = 0
    with open(os.path.join(BUNDLE, "networks.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            pos = record.get("pos")
            # A segment's own length is its reach past `pos`; 140 m clears the longest.
            if not pos or abs(pos[0] - cx) > radius + 140 or abs(pos[2] - cz) > radius + 140:
                continue
            path = os.path.join(BUNDLE, "networks", record["assetId"] + ".glb")
            if not os.path.isfile(path):
                continue
            segments += 1
            cut = amputate and record["assetId"] in amputate
            gltf, blob = read_glb(pathlib.Path(path))
            for mesh in gltf.get("meshes", []):
                for prim in mesh.get("primitives", []):
                    if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                        continue
                    verts = numpy.asarray(
                        read_accessor(gltf, blob, prim["attributes"]["POSITION"]),
                        dtype=numpy.float64)
                    if "indices" in prim:
                        idx = numpy.asarray(read_accessor(gltf, blob, prim["indices"]),
                                            dtype=numpy.int64)
                    else:
                        idx = numpy.arange(len(verts), dtype=numpy.int64)
                    tri = verts[idx].reshape(-1, 3, 3)
                    if cut:
                        tri = tri[numpy.abs(tri[:, :, 0]).max(axis=1) <= CONTROL_CUT]
                        if not len(tri):
                            continue
                    fill_triangles(cover, (pos[0] - tri[:, :, 0] - x0) / PX,
                                   (pos[2] + tri[:, :, 2] - z0) / PX)
    return (cover, x0, z0) if segments else (None, x0, z0)


def score_window(cx, cz, radius, amputate=None, only=None):
    """`(id, worst gap in metres, unpaved fraction, min width, max width)` per node strip."""
    cover, x0, z0 = build_cover(cx, cz, radius, amputate)
    if cover is None:
        return []
    height, width = cover.shape
    t = numpy.linspace(0.02, 0.98, STATIONS)
    across = numpy.linspace(0.0, 1.0, ACROSS)
    rows = []
    with open(os.path.join(BUNDLE, "clipvolumes.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") != "node" or not record.get("left") or not record.get("right"):
                continue
            if only is not None and record["id"] not in only:
                continue
            left, right = bezier(record["left"], t), bezier(record["right"], t)
            span = numpy.linalg.norm((right - left)[:, [0, 2]], axis=1)
            # A wedge the fan collapsed to a line paves nothing and asks nothing.
            if span.max() < 0.5:
                continue
            points = left[:, None, :] + (right - left)[:, None, :] * across[None, :, None]
            gx = ((points[:, :, 0] - x0) / PX).astype(int)
            gz = ((points[:, :, 2] - z0) / PX).astype(int)
            if not ((gx >= 0) & (gx < width) & (gz >= 0) & (gz < height)).all():
                continue          # partly outside the window: skip rather than score a lie
            hit = cover[gz, gx]
            worst = 0.0
            for station in range(STATIONS):
                row = hit[station]
                for side in (row, row[::-1]):
                    steps = int(numpy.argmax(side)) if side.any() else ACROSS - 1
                    worst = max(worst, steps / (ACROSS - 1) * span[station])
            rows.append((record["id"], worst, float(1.0 - hit.mean()),
                         float(span.min()), float(span.max())))
    return rows


def dense_windows(count):
    """The busiest node-strip clusters, on a 200 m grid — chosen from the bundle, not hard-coded."""
    cells = collections.Counter()
    with open(os.path.join(BUNDLE, "clipvolumes.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") != "node" or not record.get("left"):
                continue
            anchor = record["left"][0]
            cells[(round(anchor[0] / 200) * 200, round(anchor[2] / 200) * 200)] += 1
    return [(f"dense {cx},{cz}", float(cx), float(cz)) for (cx, cz), _ in cells.most_common(count)]


def check_paved():
    """Every node strip in every window is paved to within `GAP_BUDGET` of both rails."""
    global failures
    total, worst_overall = 0, (0.0, None, None)
    for label, cx, cz in tuple(WINDOWS) + tuple(dense_windows(DENSE_WINDOWS)):
        rows = score_window(cx, cz, RADIUS)
        if not rows:
            print(f"  {label:>22}: no node strips wholly inside the window")
            continue
        total += len(rows)
        over = [r for r in rows if r[1] >= GAP_BUDGET]
        worst = max(rows, key=lambda r: r[1])
        if worst[1] > worst_overall[0]:
            worst_overall = (worst[1], worst[0], label)
        status = "FAIL" if over else "ok"
        print(f"  {label:>22}: {len(rows):4d} strips, worst gap {worst[1]:.2f} m "
              f"(id {worst[0]}, {worst[3]:.1f}-{worst[4]:.1f} m wide)  {status}")
        for record_id, gap, unpaved, wmin, wmax in sorted(over, key=lambda r: -r[1])[:6]:
            print(f"        id {record_id:<9} gap {gap:.2f} m  unpaved {unpaved * 100:.1f}%  "
                  f"width {wmin:.2f}-{wmax:.2f} m")
        failures += len(over)
    print(f"  {total} node strips scored; worst gap {worst_overall[0]:.2f} m "
          f"(id {worst_overall[1]}, {worst_overall[2]}), budget {GAP_BUDGET:.2f} m")


def check_control():
    """The gate must FAIL on a bundle that has the defect. Otherwise green means nothing."""
    global failures
    label, cx, cz = WINDOWS[0]
    only = set()
    ids = {}
    with open(os.path.join(BUNDLE, "networks.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("assetId") in CONTROL_SEGMENTS:
                ids[record["assetId"]] = record["id"]
                only.add(record["id"])
    missing = [s for s in CONTROL_SEGMENTS if s not in ids]
    if missing:
        # Segment entity ids come from the save, so a different city has different ones. Say so
        # rather than passing a control that never ran.
        print(f"  control segments absent from this bundle ({', '.join(missing)}) — "
              f"the gate above is UNPROVEN on this city")
        failures += 1
        return
    clean = score_window(cx, cz, RADIUS, only=only)
    hurt = score_window(cx, cz, RADIUS, amputate=set(CONTROL_SEGMENTS), only=only)
    clean_worst = max(r[1] for r in clean)
    hurt_worst = max(r[1] for r in hurt)
    print(f"  {len(clean)} strips on {', '.join(CONTROL_SEGMENTS)}: shipped worst gap "
          f"{clean_worst:.2f} m, amputated worst gap {hurt_worst:.2f} m")
    if hurt_worst < CONTROL_MIN_GAP:
        print(f"        FAIL: amputating the outer geometry did not reproduce the defect "
              f"({hurt_worst:.2f} m < {CONTROL_MIN_GAP:.2f} m) — this test cannot see it")
        failures += 1


def main():
    if not os.path.isfile(os.path.join(BUNDLE, "clipvolumes.jsonl")):
        print(f"no bundle at {BUNDLE} — skipped")
        return 0
    print("node fans are paved:")
    check_paved()
    print("negative control (the gate fails on a defective bundle):")
    check_control()
    print("FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
