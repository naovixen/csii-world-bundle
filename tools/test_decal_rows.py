"""Lane-decal subdivision check: a marking must never sag below the road it is painted on.

The harness bends a road marking into a flat lattice and drapes each station onto the road
surface (`_drape_block`). Between two draped stations the mesh is a straight edge, so wherever
the surface *rises* above that edge by more than the marking's own lift the marking is buried —
the gap you see on a crest. The station count therefore has to be driven by that rise, measured
against the lift, and not by a tolerance chosen independently of it.

This test pins that invariant. It extracts the row-count functions out of the harness source
(which imports `bpy` at module scope, so it cannot be imported under system Python — same
technique as `test_png_decode.py`), and for each case measures the worst rise of the curve's own
vertical profile above the chord polyline the chosen rows produce, on a dense independent
resampling of the real bézier rather than on the 33-sample arc table the implementation reuses.

Run: `python tools/test_decal_rows.py`  (system Python + numpy; the bundle sweep is skipped when
no bundle is on disk).
"""
import json
import os
import re
import sys

import numpy

SRC = os.path.join(os.path.dirname(__file__), "blender_import_bundle.py")
BUNDLE = os.path.expandvars(r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
                            r"\Cities Skylines II\ModsData\Carto\spike")
DENSE = 513

source = open(SRC, encoding="utf-8").read()
H = {"numpy": numpy}
exec(compile("import math", "<math>", "exec"), H)
for name in ("_LANE_ARC_SAMPLES", "_DECAL_MAX_ROWS", "_DECAL_SHAPE_TOLERANCE",
             "_DECAL_DIP_FRACTION", "_DECAL_DRAPE_MARKING", "_DECAL_DRAPE_TINT"):
    match = re.search(rf"^{name} = .*$", source, re.M)
    assert match, f"{name} not found in harness source"
    exec(compile(match.group(0), f"<{name}>", "exec"), H)
for name in ("_bezier_point_vec", "_cut_bezier", "_arc_table_vec", "_curve_length",
             "_shape_rows", "_chord_dip", "_decal_rows"):
    match = re.search(rf"\ndef {name}\(.*?(?=\n\ndef |\n\nclass |\Z)", source, re.S)
    assert match, f"{name} not found in harness source"
    exec(compile(match.group(0), f"<{name}>", "exec"), H)

failures = 0


def measured_dip(bezier, rows):
    """Worst rise of the curve's vertical profile above the `rows`-row chord, in metres.

    Ground truth for the test: a dense resampling of the actual bézier, arc-parameterised the
    way the lattice stations are placed (`arc_pos = stations * total_arc` in `_bend_lane_part`).
    """
    t_table, arc_table = H["_arc_table_vec"](bezier)
    total = float(arc_table[-1])
    station_arc = numpy.linspace(0.0, total, rows + 1)
    station_y = H["_bezier_point_vec"](bezier, numpy.interp(station_arc, arc_table, t_table))[:, 1]
    dense_arc = numpy.linspace(0.0, total, DENSE)
    dense_y = H["_bezier_point_vec"](bezier, numpy.interp(dense_arc, arc_table, t_table))[:, 1]
    return float((dense_y - numpy.interp(dense_arc, station_arc, station_y)).max())


def rows_for(bezier, eps):
    t_table, arc_table = H["_arc_table_vec"](bezier)
    points = H["_bezier_point_vec"](bezier, t_table)
    return H["_decal_rows"](points, arc_table, eps)


def crest(length, height, plan_bend=0.0):
    """A cubic whose midpoint rises `height` above the straight chord (and optionally bends
    `plan_bend` sideways) — a road crest, in bundle frame (x east, y up, z north)."""
    lift = height / 0.75
    side = plan_bend / 0.75
    return ((0.0, 0.0, 0.0),
            (length / 3.0, lift, side),
            (2.0 * length / 3.0, lift, side),
            (length, 0.0, 0.0))


def check(label, bezier, eps, expect_rows=None):
    global failures
    rows = rows_for(bezier, eps)
    dip = measured_dip(bezier, rows)
    bad = dip > eps or (expect_rows is not None and rows != expect_rows)
    failures += bool(bad)
    print(f"{'FAIL' if bad else 'ok  '} {label}: rows={rows} dip={dip*1000:.1f} mm "
          f"lift={eps*1000:.1f} mm"
          + (f" (expected rows={expect_rows})" if expect_rows is not None else ""))


print("-- synthetic crests: the chord must stay within the marking's own lift --")
# A crest whose 3D sagitta sits just under the shape tolerance: invisible to a rule that only
# looks at deviation from the 3D chord, fatal to the marking, which is lifted less than that.
check("80 m run, 19 mm crest, marking", crest(80.0, 0.019), H["_DECAL_DRAPE_MARKING"])
check("80 m run, 19 mm crest, wear tint", crest(80.0, 0.019), H["_DECAL_DRAPE_TINT"])
# A pronounced crest: subdivided by the shape rule, but not nearly far enough for the 8 mm tint.
check("100 m run, 200 mm crest, wear tint", crest(100.0, 0.200), H["_DECAL_DRAPE_TINT"])
check("100 m run, 200 mm crest, marking", crest(100.0, 0.200), H["_DECAL_DRAPE_MARKING"])
check("30 m ramp, 1 m crest, marking", crest(30.0, 1.0), H["_DECAL_DRAPE_MARKING"])

print("\n-- a vertically flat curve must not pay for the drape criterion --")
# Bending in plan costs the shape rule rows (the marking has to follow the bend), but a
# vertically flat curve can never bury itself, so the drape refinement must add nothing.
flat_bend = ((0.0, 0.0, 0.0), (30.0, 0.0, 8.0), (60.0, 0.0, 8.0), (90.0, 0.0, 0.0))
shape_only = rows_for(flat_bend, None)
check("90 m plan bend, no grade, marking", flat_bend, H["_DECAL_DRAPE_MARKING"],
      expect_rows=shape_only)

print("\n-- full-city sweep over the shipped bundle --")
catalogue_path = os.path.join(BUNDLE, "laneprefabs.json")
lanes_path = os.path.join(BUNDLE, "lanemeshes.jsonl")
if not (os.path.isfile(catalogue_path) and os.path.isfile(lanes_path)):
    print(f"skipped — no bundle at {BUNDLE}")
else:
    catalogue = json.load(open(catalogue_path, encoding="utf-8"))
    spans_seen = buried = capped = 0
    worst = (0.0, "")
    verts = 0
    with open(lanes_path, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                continue
            parts = entry.get("parts", [])
            decals = [parts[i] for i in record.get("parts", [])
                      if 0 <= i < len(parts) and parts[i].get("decal")]
            if not decals:
                continue
            curve = record["curve"]
            curve_length = H["_curve_length"](curve)
            for span in (record.get("spans") or [[0.0, 1.0]]):
                if curve_length * (span[1] - span[0]) <= 0.1:
                    continue
                base = H["_cut_bezier"](curve, span[0], span[1])
                for part in decals:
                    bezier = ((base[3], base[2], base[1], base[0])
                              if part.get("inverted") else base)
                    asset = part.get("assetId", "")
                    eps = (H["_DECAL_DRAPE_MARKING"]
                           if ("Line" in asset or "Crosswalk" in asset)
                           else H["_DECAL_DRAPE_TINT"])
                    rows = rows_for(bezier, eps)
                    dip = measured_dip(bezier, rows)
                    spans_seen += 1
                    verts += 2 * (rows + 1)
                    if rows >= H["_DECAL_MAX_ROWS"]:
                        capped += 1
                    if dip > eps:
                        buried += 1
                        # Only the row cap may bury a marking: everything else is a rule defect.
                        if rows < H["_DECAL_MAX_ROWS"]:
                            failures += 1
                            print(f"FAIL {asset}: dip {dip*1000:.1f} mm > lift "
                                  f"{eps*1000:.1f} mm at rows={rows} (cap not reached)")
                    if dip - eps > worst[0]:
                        worst = (dip - eps, asset)
    print(f"{'FAIL' if buried > capped else 'ok  '} {spans_seen} decal spans, "
          f"{buried} buried ({capped} spans at the {H['_DECAL_MAX_ROWS']}-row cap), "
          f"{verts} decal vertices")
    if worst[1]:
        print(f"     worst overshoot {worst[0]*1000:.1f} mm on {worst[1]}")

sys.exit(1 if failures else 0)
