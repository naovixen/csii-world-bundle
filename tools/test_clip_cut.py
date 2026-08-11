"""Terrain clip cut: no ground may be deleted that nothing is drawn over.

The game never cuts terrain geometry. It draws the whole heightfield and discards per pixel
against a stencil built from the §8c clip volumes (`GenerateClipDataJob` ->
`TerrainSurface.RenderClipAreas`). A mesh cut at 3.5 m can only approximate that boundary, and
this test pins which way it errs.

WHAT THIS TEST SCORES AGAINST, AND WHY IT CHANGED (2026-08-11, second round). It used to score
the deleted set against the clip PRISM: sky meant "deleted where no prism reaches". That budget
was met — 1 m² over the width-transition window — while two triangles of ground were plainly
missing on screen, because the prism is not the pavement. At a node the clip fan runs out to the
segment's own side curve and the road mesh reaches only part of that sector (measured: short by
2.2 m at the rail's start, 4.4 m at the junction centre), so terrain deleted well inside the
prism had nothing over it. A test that cannot tell those apart cannot gate this rule.

So the primary score is now against the REAL segment meshes out of `networks/seg_*.glb`: a
pixel is sky when the cut deleted the ground AND no road triangle covers it. The prism check is
kept as a secondary, because it still catches the other failure direction.

The shipped rule that follows from this: a shallow band flattens but never carves; only a
trench or portal band (`top > _CLIP_DEEP_BAND_TOP`) deletes. See `_contain_faces`.

WHAT THIS SETTLED (2026-08-11). Three defects were reported after the split-band round — gaps at
junction intersections, square terrain overlapping cutout walls, and terrain clipping through
roads. Measured here, the first is the harness deleting terrain no prism covers, and its cause
was the deletion rule ANDing a face's four CORNER CELLS against the UNION of every prism: at a
junction the four corners are covered by four DIFFERENT prisms while the face's own interior
escapes them all. Sampling the face's interior instead (`_contain_faces`) removes it.

  window                    corner-AND | above     face interior (shipped)
  train tracks (873,1442)        1,380 m² sky            13 m²
  road junction (2936,1397)        398 m² sky            16 m²
  trench cluster (-2650,3850)      197 m² sky             1 m²
  deep cluster (2750,-350)         741 m² sky             8 m²
  clip areas (450,-2900)           618 m² sky             3 m²

The rule under test is the harness's own — `_contain_faces` and `_fill_clip_quad` are extracted
from the harness source (which imports `bpy` at module scope, so it cannot be imported under
system Python — the same trick as `tools/test_decal_rows.py`). The TRUTH it is scored against is
built here, at a finer sampling than the rule uses, so agreement means the cut respects the
prisms rather than that one rasteriser agrees with itself.

Run: `python tools/test_clip_cut.py`  (system Python + numpy + PIL; skipped when no bundle is on
disk).
"""
import json
import math
import os
import pathlib
import random
import re
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC = os.path.join(os.path.dirname(__file__), "blender_import_bundle.py")
BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")

# Independent truth sampling, finer than the rule's own. A sliver narrower than this is below
# what any 3.5 m mesh can represent and is not what this test is about.
TRUTH = 12

# The five windows the 2026-08-11 defects were reported and located in: the user's two, the two
# densest deep-band (trench / tunnel-portal) clusters, and the densest clip-area cluster.
WINDOWS = (("train tracks", 873.9, 1442.0, 120.0),
           ("road junction", 2936.9, 1397.5, 120.0),
           ("trench cluster", -2650.0, 3850.0, 120.0),
           ("deep cluster", 2750.0, -350.0, 120.0),
           ("clip areas", 450.0, -2900.0, 120.0),
           # The 2026-08-11 width transition: a 24 m Medium Road narrowing to an 8 m Alley
           # Oneway, whose node fans opened the two triangles the prism score could not see.
           ("width transition", 4324.5, 1550.5, 120.0))

# Sky, in m² per window, that the shipped rule is allowed to leave. Above zero only because the
# containment test samples at `_CLIP_CONTAIN_SAMPLES` per cell and the truth here is finer; the
# residual is sub-sample sliver along a prism wall.
SKY_BUDGET = 25.0

# The one that matters: ground deleted with no road mesh over it, per window, outside the two
# deliberately-open classes below. The shipped rule leaves 0.0-0.2 m² — sub-sample sliver where
# the exemption footprint and the cut disagree by less than a truth pixel — so a budget of 1 m²
# is a real gate rather than a formality. Verified by probe: letting shallow bands carve again
# takes it to 4.3 / 2.3 / 0.9 / 0.9 / 14.1 m² and fails the width-transition window, which is
# the one the two triangles were reported in.
ROAD_SKY_BUDGET = 1.0

source = open(SRC, encoding="utf-8").read()
H = {"numpy": numpy}
exec(compile("import math", "<math>", "exec"), H)
for _name in ("_CLIP_CONTAIN_SAMPLES", "_CLIP_CONTAIN_BLOCK", "_CLIP_DEEP_BAND_TOP",
              "_CLIP_SCANLINE_VECTOR_ROWS"):
    _m = re.search(rf"^{_name} = .*$", source, re.M)
    assert _m, f"{_name} not found in harness source"
    exec(compile(_m.group(0), f"<{_name}>", "exec"), H)
for _name in ("_bezier_points", "_fill_clip_quad_rows", "_fill_clip_quad", "_dilate",
              "_contain_faces"):
    _m = re.search(rf"\ndef {_name}\(.*?(?=\n\ndef |\n\nclass |\Z)", source, re.S)
    assert _m, f"{_name} not found in harness source"
    exec(compile(_m.group(0), f"<{_name}>", "exec"), H)

failures = 0


def fuzz_scanline_paths(trials=4000, seed=20260811):
    """The whole-array scanline must agree with the per-row one, quad for quad.

    `_fill_clip_quad` splits on row count: short quads take `_fill_clip_quad_rows`, tall ones
    the whole-array path. A fast path that silently disagrees with its slow one is what sank
    the exact clip reverted on 2026-08-11 (`2878393` -> `22a8ca2`), whose own note says the
    missing test was exactly this. So: random quads, including the shapes the layer really
    ships — triangles with a doubled corner (`area` records), degenerate zero-area quads (a
    node strip whose far side collapses to a point) and self-intersecting bowties — against
    both paths directly, bypassing the dispatch.
    """
    global failures
    rng = random.Random(seed)
    bad = 0
    for trial in range(trials):
        w, h = rng.randint(3, 24), rng.randint(3, 24)
        terrain = numpy.asarray([[rng.uniform(-5.0, 5.0) for _ in range(w)] for _ in range(h)])
        corner = [(rng.uniform(-3.0, w + 3.0), rng.uniform(-3.0, h + 3.0)) for _ in range(4)]
        shape = trial % 4
        if shape == 1:                                    # triangle, as `area` records ship
            corner[3] = corner[2]
        elif shape == 2:                                  # degenerate: both sides collapsed
            corner = [corner[0], corner[1], corner[0], corner[1]]
        elif shape == 3:                                  # bowtie
            corner = [corner[0], corner[2], corner[1], corner[3]]
        lane = rng.uniform(-2.0, 2.0)
        bottom = rng.uniform(-3.0, 0.0)
        top = bottom + rng.uniform(0.0, 4.0)
        offset = rng.choice([0.5, 1.0])
        ys = [p[1] for p in corner]
        row_lo = max(0, int(math.ceil(min(ys) - offset)))
        row_hi = min(h - 1, int(math.floor(max(ys) - offset)))

        rows_mask = numpy.zeros((h, w), dtype=bool)
        rows_refs = numpy.full((h, w), -1, dtype=numpy.int32)
        if row_hi >= row_lo:
            H["_fill_clip_quad_rows"](rows_mask, terrain, w, h, corner, lane, bottom, top,
                                      rows_refs, 7, offset, row_lo, row_hi)
        both_mask = numpy.zeros((h, w), dtype=bool)
        both_refs = numpy.full((h, w), -1, dtype=numpy.int32)
        H["_fill_clip_quad"](both_mask, terrain, w, h, corner, lane, bottom, top,
                             refs=both_refs, ref_value=7, offset=offset)

        if not (numpy.array_equal(rows_mask, both_mask)
                and numpy.array_equal(rows_refs, both_refs)):
            bad += 1
            if bad <= 3:
                print(f"  FAIL scanline paths disagree: shape {shape}, offset {offset}, "
                      f"quad {corner}")
    if bad:
        print(f"  FAIL {bad} of {trials} random quads rasterise differently on the two paths")
        failures += 1
    else:
        print(f"  ok   scanline paths agree on {trials} random quads "
              f"(triangles, degenerates, bowties)")


def load_terrain():
    from PIL import Image
    rasters = json.load(open(os.path.join(BUNDLE, "terrain", "rasters.json"), encoding="utf-8"))
    geo = json.load(open(os.path.join(BUNDLE, "bundle.json"), encoding="utf-8"))["rasters"]
    pixels = numpy.asarray(Image.open(os.path.join(BUNDLE, "terrain", "heightmap.png")))
    if pixels.ndim == 3:
        pixels = pixels[..., 0]
    heights = (pixels.astype(numpy.float64) * float(rasters["elevationScale"])
               + float(rasters["elevationOffset"]))
    return (heights, float(rasters["resolution"]),
            float(geo["originWorld"][0]), float(geo["originWorld"][2]))


def strips_near(x0, x1, z0, z1):
    """Clip records whose control hull meets the window."""
    found = []
    with open(os.path.join(BUNDLE, "clipvolumes.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "area":
                nodes = record.get("nodes") or []
                if not nodes:
                    continue
                hull = numpy.asarray(nodes, dtype=numpy.float64)
            else:
                if not record.get("left") or not record.get("right"):
                    continue
                hull = numpy.asarray(record["left"] + record["right"], dtype=numpy.float64)
            if (hull[:, 0].max() < x0 or hull[:, 0].min() > x1
                    or hull[:, 2].max() < z0 or hull[:, 2].min() > z1):
                continue
            found.append(record)
    return found


def carves(record):
    """Is this record one the shipped rule lets DELETE terrain?

    A shallow band flattens and paints; only a trench or portal band carves. An `area` record
    is an authored clip surface and keeps its own behaviour.
    """
    if record.get("kind") == "area":
        return True
    top = record.get("top")
    return top is None or float(top) > H["_CLIP_DEEP_BAND_TOP"]


def quads_of(record, to_cell, cell):
    """`(quad, lane_y, bottom, top)` per station, in cell space — the harness's own stepping."""
    bottom = float(record["bottom"])
    top = float(record["top"]) if record.get("top") is not None else float("inf")
    if record.get("kind") == "area":
        nodes, triangles = record.get("nodes") or [], record.get("triangles") or []
        for tri in triangles:
            if any(not 0 <= i < len(nodes) for i in tri):
                continue
            pts = numpy.asarray([nodes[i] for i in tri], dtype=numpy.float64)
            cells = to_cell(pts)
            yield ([tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
                   float(pts[:, 1].mean()), bottom, top)
        return
    left, right = record["left"], record["right"]
    polygon = numpy.asarray(left, dtype=numpy.float64)
    span = float(numpy.linalg.norm(numpy.diff(polygon, axis=0), axis=1).sum())
    steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))
    t = numpy.linspace(0.0, 1.0, steps + 1)
    left_pts, right_pts = H["_bezier_points"](left, t), H["_bezier_points"](right, t)
    left_cells, right_cells = to_cell(left_pts), to_cell(right_pts)
    lane = (left_pts[:, 1] + right_pts[:, 1]) * 0.5
    for i in range(1, steps + 1):
        yield ([tuple(left_cells[i - 1]), tuple(right_cells[i - 1]),
                tuple(right_cells[i]), tuple(left_cells[i])],
               float((lane[i - 1] + lane[i]) * 0.5), bottom, top)


def road_cover(cx, cz, radius, origin_x, origin_z, col0, row0, cell, fine_h, fine_w):
    """What the road meshes actually draw over, at TRUTH samples per cell.

    The bundle's segment meshes are the only honest answer to "is this ground hidden?" — the
    clip prism is not, which is the whole point of this test. glTF vertices are placed the way
    contract §2.2 places an instance, with the mirror the harness applies:
    `world = pos + diag(-1, 1, 1) * v` (every network record ships an identity rotation).
    """
    from inspect_glb import read_glb, read_accessor

    cover = numpy.zeros((fine_h, fine_w), dtype=bool)
    mesh_dir = os.path.join(BUNDLE, "networks")
    if not os.path.isdir(mesh_dir):
        return None
    segments = 0
    with open(os.path.join(BUNDLE, "networks.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            pos = record.get("pos")
            # A segment's own length is its reach past `pos`; 120 m clears the longest.
            if not pos or abs(pos[0] - cx) > radius + 120 or abs(pos[2] - cz) > radius + 120:
                continue
            path = os.path.join(mesh_dir, record["assetId"] + ".glb")
            if not os.path.isfile(path):
                continue
            try:
                gltf, blob = read_glb(pathlib.Path(path))
            except ValueError:
                continue
            segments += 1
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
                    wx = pos[0] - tri[:, :, 0]
                    wz = pos[2] + tri[:, :, 2]
                    px = ((wx - origin_x) / cell + 0.5 - col0 - 0.5) * TRUTH
                    pz = ((origin_z - wz) / cell + 0.5 - row0 - 0.5) * TRUTH
                    _fill_triangles(cover, px, pz)
    return cover if segments else None


def _fill_triangles(mask, px, pz):
    """Scan-fill triangles given per-vertex in fine-pixel coordinates."""
    fine_h, fine_w = mask.shape
    for k in range(len(px)):
        y0 = max(0, int(math.floor(pz[k].min())))
        y1 = min(fine_h - 1, int(math.ceil(pz[k].max())))
        x0 = max(0, int(math.floor(px[k].min())))
        x1 = min(fine_w - 1, int(math.ceil(px[k].max())))
        if y1 < y0 or x1 < x0:
            continue
        ax, bx, cx_ = px[k]
        az, bz, cz_ = pz[k]
        det = (bz - cz_) * (ax - cx_) + (cx_ - bx) * (az - cz_)
        if abs(det) < 1e-12:
            continue
        gx, gz = numpy.meshgrid(numpy.arange(x0, x1 + 1) + 0.5,
                                numpy.arange(y0, y1 + 1) + 0.5)
        l1 = ((bz - cz_) * (gx - cx_) + (cx_ - bx) * (gz - cz_)) / det
        l2 = ((cz_ - az) * (gx - cx_) + (ax - cx_) * (gz - cz_)) / det
        inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
        if inside.any():
            mask[y0:y1 + 1, x0:x1 + 1] |= inside


def check(label, cx, cz, radius, heights, cell, origin_x, origin_z):
    """Score the harness's deletion mask on one window against a finer independent truth."""
    global failures
    raster_h, raster_w = heights.shape
    col0 = max(0, int((cx - radius - origin_x) / cell) - 2)
    col1 = min(raster_w - 1, int((cx + radius - origin_x) / cell) + 2)
    row0 = max(0, int((origin_z - (cz + radius)) / cell) - 2)
    row1 = min(raster_h - 1, int((origin_z - (cz - radius)) / cell) + 2)
    lat_h, lat_w = row1 - row0 + 1, col1 - col0 + 1
    terrain = heights[row0:row1 + 1, col0:col1 + 1]
    records = strips_near(cx - radius, cx + radius, cz - radius, cz + radius)

    def to_cell(points):
        return numpy.stack(((points[:, 0] - origin_x) / cell + 0.5 - col0,
                            (origin_z - points[:, 2]) / cell + 0.5 - row0), axis=-1)

    quads = [q for record in records for q in quads_of(record, to_cell, cell)]
    # Only a carving record reaches the deletion rule, exactly as `_clip_grid` bins it. Every
    # record still fills `cell_cut`, which is the refine corridor, not the cut.
    cutting = [q for record in records if carves(record)
               for q in quads_of(record, to_cell, cell)]
    if not quads:
        print(f"  {label:<16} no clip volumes in window — skipped")
        return

    # -- the harness's own rule ------------------------------------------------------------
    cell_cut = numpy.zeros((lat_h, lat_w), dtype=bool)
    for quad, lane_y, bottom, top in quads:
        H["_fill_clip_quad"](cell_cut, terrain, lat_w, lat_h, quad, lane_y, bottom, top)
    contain, band_count, deep_touch, _sub = H["_contain_faces"](
        lat_w, lat_h, terrain, cell_cut,
        lambda r0, c0, r1, c1: iter(cutting),
        H["_CLIP_CONTAIN_SAMPLES"], H["_CLIP_CONTAIN_BLOCK"])

    # -- truth: the band and the footprint at TRUTH samples per cell -----------------------
    fine_h, fine_w = (lat_h - 1) * TRUTH, (lat_w - 1) * TRUTH
    gc = numpy.clip((numpy.arange(fine_w) + 0.5) / TRUTH, 0, lat_w - 1.001)
    gr = numpy.clip((numpy.arange(fine_h) + 0.5) / TRUTH, 0, lat_h - 1.001)
    ci, ri = gc.astype(int), gr.astype(int)
    fu, fv = gc - ci, gr - ri
    fine_y = ((terrain[numpy.ix_(ri, ci)] * (1 - fu)[None, :]
               + terrain[numpy.ix_(ri, ci + 1)] * fu[None, :]) * (1 - fv)[:, None]
              + (terrain[numpy.ix_(ri + 1, ci)] * (1 - fu)[None, :]
                 + terrain[numpy.ix_(ri + 1, ci + 1)] * fu[None, :]) * fv[:, None])
    band = numpy.zeros((fine_h, fine_w), dtype=bool)
    footprint = numpy.zeros((fine_h, fine_w), dtype=bool)
    for quad, lane_y, bottom, top in quads:
        local = [((px - 0.5) * TRUTH, (py - 0.5) * TRUTH) for px, py in quad]
        H["_fill_clip_quad"](band, fine_y, fine_w, fine_h, local, lane_y, bottom, top)
        # Footprint: the same scanline with a band that cannot reject anything.
        H["_fill_clip_quad"](footprint, fine_y, fine_w, fine_h, local, 0.0,
                             -1e18, 1e18)

    area = (cell / TRUTH) ** 2
    deleted = numpy.kron(contain, numpy.ones((TRUTH, TRUTH), dtype=bool))
    sky = float((deleted & ~footprint).sum()) * area
    band_area = float(band.sum()) * area
    deleted_area = float(contain.sum()) * cell * cell
    ratio = deleted_area / band_area if band_area else 0.0
    poke = float((~deleted & band).sum()) * area

    # The primary score: ground deleted with no road triangle over it. This is what a viewer
    # sees as a hole, and the only measure that would have caught the width-transition notches.
    #
    # Two exclusions, and they are exclusions of SCOPE, not amnesties. A tunnel portal's mouth
    # is deliberately open — that is the whole reason a deep band carves — and its road mesh
    # runs down inside the hill rather than over the hole, so it can never "cover" it. A lot
    # clip is covered by the building or area surface that owns it, which is not a network mesh
    # and is not this test's subject. What is left is exactly the class the notches belong to:
    # ground under a SHALLOW network band. That is 0 while only deep bands carve, and it is
    # what goes non-zero the moment a shallow band is let back into the deletion — which is the
    # regression this test exists to catch.
    # NOTE the exemption is defined here, NOT as `carves()`. Deriving it from the rule under
    # test makes the test vacuous: flip the rule and the exemption flips with it, so every
    # deletion is excused and the score reads 0 whatever the rule does. (Tried; it read 0.8 m²
    # for the very behaviour that put two triangles on screen.)
    cover = road_cover(cx, cz, radius, origin_x, origin_z, col0, row0, cell, fine_h, fine_w)
    seen_through = None
    if cover is not None:
        exempt = numpy.zeros((fine_h, fine_w), dtype=bool)
        for record in records:
            deep = record.get("top") is None or float(record["top"]) > H["_CLIP_DEEP_BAND_TOP"]
            if not (record.get("kind") == "area" or deep):
                continue
            for quad, lane_y, bottom, top in quads_of(record, to_cell, cell):
                local = [((px - 0.5) * TRUTH, (py - 0.5) * TRUTH) for px, py in quad]
                H["_fill_clip_quad"](exempt, fine_y, fine_w, fine_h, local, 0.0, -1e18, 1e18)
        seen_through = float((deleted & ~cover & ~exempt).sum()) * area

    ok = True
    if seen_through is not None and seen_through > ROAD_SKY_BUDGET:
        print(f"  FAIL {label}: {seen_through:.1f} m² of ground deleted with no road mesh over "
              f"it (budget {ROAD_SKY_BUDGET:.1f} m²) — visible holes")
        ok = False
    if sky > SKY_BUDGET:
        print(f"  FAIL {label}: {sky:.0f} m² deleted where no clip prism reaches "
              f"(budget {SKY_BUDGET:.0f} m²) — see-through gaps")
        ok = False
    if ratio > 1.0:
        print(f"  FAIL {label}: cut {deleted_area:.0f} m² against {band_area:.0f} m² clipped "
              f"(x{ratio:.2f}) — above 1.00 is sky")
        ok = False
    if not ok:
        failures += 1
        return
    # Faces a TRENCH or PORTAL band meets with no deleted face beside them — a strip too narrow
    # for a whole face to sit inside, so containment deletes nothing along it and the snap has
    # no hole rim to push out. `build_terrain` re-cuts exactly these at the containment
    # sampling; the figure is reported so their number stays visible, not because they are left.
    recut = int((deep_touch & ~contain & ~H["_dilate"](contain, 1)).sum())
    hole = f"{seen_through:5.1f} m²" if seen_through is not None else "  (no meshes)"
    print(f"  ok   {label:<16} hole {hole}   sky {sky:5.0f} m²   cut/clipped x{ratio:.2f}   "
          f"kept-in-band {100 * poke / max(band_area, 1e-9):4.1f}%   "
          f"re-cut {recut * cell * cell:5.0f} m²   "
          f"({len(records)} record(s), {len(cutting)} carving quad(s))")


def main():
    print("clip cut, scanline paths:")
    fuzz_scanline_paths()
    if not os.path.isfile(os.path.join(BUNDLE, "clipvolumes.jsonl")):
        print(f"no bundle at {BUNDLE} — window checks skipped")
        return 1 if failures else 0
    heights, cell, origin_x, origin_z = load_terrain()
    print(f"clip cut, {H['_CLIP_CONTAIN_SAMPLES']} sample(s)/cell against {cell / TRUTH:.2f} m "
          f"truth, {cell:g} m faces:")
    for label, cx, cz, radius in WINDOWS:
        check(label, cx, cz, radius, heights, cell, origin_x, origin_z)
    print("clip cut: FAILED" if failures else "clip cut: ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
