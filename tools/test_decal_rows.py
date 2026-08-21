"""Lane-decal subdivision check: a marking must never sag below the road it is painted on.

The harness bends a road marking into a flat lattice and drapes each station onto the road
surface (`_drape_block`). Between two draped stations the mesh is a straight edge, so wherever
the surface *rises* above that edge by more than the marking's own lift the marking is buried —
the gap you see on a crest. The station count therefore has to be driven by that rise, measured
against the lift, and not by a tolerance chosen independently of it.

This test pins that invariant. For each case it measures the worst rise of the curve's own
vertical profile above the chord polyline the chosen rows produce, on a dense independent
resampling of the real bézier rather than on the 33-sample arc table the implementation reuses.

It used to regex those functions out of `blender_import_bundle.py`, which imports `bpy` at
module scope and so cannot be imported under system Python. They live in `csiibundle` now, so
this imports them; `H` below is kept only as the one place naming every package symbol the test
pins, so a rename fails here loudly instead of matching nothing.

Run: `python tools/test_decal_rows.py`  (system Python + numpy; the bundle sweep is skipped when
no bundle is on disk).
"""

import json
import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle import bezier as bezier_math
from csiibundle import decals as decal_rules
from csiibundle import lanes as lane_math

BUNDLE = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
    r"\Cities Skylines II\ModsData\Carto"
)
DENSE = 513

# Local names are `bezier` (a curve) and `decals` (a list of parts) throughout the body below,
# so the modules are reached through this table rather than shadowed by them.
H = {
    "_LANE_ARC_SAMPLES": bezier_math.ARC_SAMPLES,
    "_LANE_MAX_TILES": lane_math.MAX_TILES,
    "_DECAL_MAX_ROWS": decal_rules.MAX_ROWS,
    "_DECAL_SHAPE_TOLERANCE": decal_rules.SHAPE_TOLERANCE,
    "_DECAL_DIP_FRACTION": decal_rules.DIP_FRACTION,
    "_DECAL_DRAPE_MARKING": decal_rules.LIFT["lane_marking"],
    "_DECAL_DRAPE_TINT": decal_rules.LIFT["lane_tint"],
    "_bezier_point_vec": bezier_math.evaluate,
    "_cut_bezier": bezier_math.cut,
    "_arc_table_vec": bezier_math.arc_table,
    "_curve_length": bezier_math.length,
    "_shape_rows": decal_rules.shape_rows,
    "_chord_dip": decal_rules.chord_dip,
    "_decal_rows": decal_rules.station_count,
    "_decal_station_rows": decal_rules.station_rows,
    "_lane_tile_count": lane_math.tile_count,
    "_decal_lattice": decal_rules.lattice,
}

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
    return (
        (0.0, 0.0, 0.0),
        (length / 3.0, lift, side),
        (2.0 * length / 3.0, lift, side),
        (length, 0.0, 0.0),
    )


def check(label, bezier, eps, expect_rows=None):
    global failures
    rows = rows_for(bezier, eps)
    dip = measured_dip(bezier, rows)
    bad = dip > eps or (expect_rows is not None and rows != expect_rows)
    failures += bool(bad)
    print(
        f"{'FAIL' if bad else 'ok  '} {label}: rows={rows} dip={dip * 1000:.1f} mm "
        f"lift={eps * 1000:.1f} mm"
        + (f" (expected rows={expect_rows})" if expect_rows is not None else "")
    )


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
check(
    "90 m plan bend, no grade, marking",
    flat_bend,
    H["_DECAL_DRAPE_MARKING"],
    expect_rows=shape_only,
)

print("\n-- texture-period stations: repeated V, seam-split, never smeared (audit §39) --")


def check_stations(label, rows, n_total, expect):
    """`expect` is a dict of named invariant checks; every one must hold."""
    global failures
    stations, v_frac = H["_decal_station_rows"](rows, float(n_total))
    problems = []
    if numpy.any(numpy.diff(stations) < 0):
        problems.append("stations not ascending")
    if stations[0] != 0.0 or stations[-1] != 1.0:
        problems.append(f"endpoints {stations[0]}..{stations[-1]}")
    if numpy.any(v_frac < 0.0) or numpy.any(v_frac > 1.0):
        problems.append("v_frac outside [0, 1]")
    boundaries = int(numpy.sum(numpy.diff(stations) == 0.0))
    if boundaries != expect.get("boundaries", boundaries):
        problems.append(
            f"{boundaries} duplicated boundary station(s), expected {expect['boundaries']}"
        )
    # Each zero-extent gap must be a v=1.0 row followed by a v=0.0 row (end before start).
    for i in numpy.nonzero(numpy.diff(stations) == 0.0)[0]:
        if not (v_frac[i] == 1.0 and v_frac[i + 1] == 0.0):
            problems.append(
                f"seam at station {stations[i]:.4f} is "
                f"v {v_frac[i]:.4f}->{v_frac[i + 1]:.4f}, not 1->0"
            )
    if "last_v" in expect and abs(float(v_frac[-1]) - expect["last_v"]) > 1e-9:
        problems.append(f"last v_frac {v_frac[-1]:.6f}, expected {expect['last_v']}")
    # Between seams, n = n_total*station - period must be continuous and rising: v at any
    # station equals n_total*station minus the count of seams passed.
    seam_count = numpy.concatenate(([0], numpy.cumsum(numpy.diff(stations) == 0.0)))
    residual = numpy.abs(v_frac - (float(n_total) * stations - seam_count))
    if float(residual.max()) > 1e-9:
        problems.append(f"v_frac drifts from n - period by {residual.max():.2e}")
    failures += bool(problems)
    print(
        f"{'FAIL' if problems else 'ok  '} {label}: {len(stations)} station(s), "
        f"{boundaries} seam(s)" + ("; " + "; ".join(problems) if problems else "")
    )


check_stations("N=6 integer, 4 curvature rows", 4, 6.0, {"boundaries": 5, "last_v": 1.0})
check_stations(
    "N=6.5 fractional (74 m highway dash line)", 8, 6.5, {"boundaries": 6, "last_v": 0.5}
)
check_stations("N=0.22 sub-period (short yield line)", 4, 0.22, {"boundaries": 0, "last_v": 0.22})
check_stations("N=1 exactly (span == mesh length)", 4, 1.0, {"boundaries": 0, "last_v": 1.0})
# Curvature stations landing exactly on boundaries must yield to the seam pair, not stack.
check_stations(
    "N=4 with rows=4 (every station is a boundary)", 4, 4.0, {"boundaries": 3, "last_v": 1.0}
)

print("\n-- lattice: seams split UVs, no zero-area faces --")
QUAD = {
    "near_left": (-0.25, 0.0, -6.0),
    "near_right": (0.25, 0.0, -6.0),
    "far_left": (-0.25, 0.0, 6.0),
    "far_right": (0.25, 0.0, 6.0),
    "uv_near_left": (0.125, 1.0),
    "uv_near_right": (0.1458, 1.0),
    "uv_far_left": (0.125, 0.0),
    "uv_far_right": (0.1458, 0.0),
    "flip": False,
}
stations, v_frac = H["_decal_station_rows"](4, 6.5)
verts, tris, tri_uvs = H["_decal_lattice"](QUAD, stations, v_frac)
areas = numpy.linalg.norm(
    numpy.cross(verts[tris[:, 1]] - verts[tris[:, 0]], verts[tris[:, 2]] - verts[tris[:, 0]]),
    axis=1,
)
gaps = int(numpy.sum(numpy.diff(stations) > 0.0))
lattice_bad = len(tris) != 2 * gaps or float(areas.min()) <= 0.0 or len(verts) != 2 * len(stations)
failures += lattice_bad
print(
    f"{'FAIL' if lattice_bad else 'ok  '} N=6.5 lattice: {len(verts)} vert(s), "
    f"{len(tris)} tri(s) over {gaps} gap(s), min area {areas.min():.3e}"
)
# The V written at each seam's two rows must be the quad's far/near V — the full authored span.
seam_rows = numpy.nonzero(numpy.diff(stations) == 0.0)[0]
uv_by_vert = numpy.empty((len(verts), 2))
uv_by_vert[tris.reshape(-1)] = tri_uvs.reshape(-1, 2)
seam_v = uv_by_vert[seam_rows * 2, 1], uv_by_vert[(seam_rows + 1) * 2, 1]
seam_bad = numpy.any(seam_v[0] != QUAD["uv_far_left"][1]) or numpy.any(
    seam_v[1] != QUAD["uv_near_left"][1]
)
failures += bool(seam_bad)
print(
    f"{'FAIL' if seam_bad else 'ok  '} seam rows carry far-V then near-V "
    f"({QUAD['uv_far_left'][1]} / {QUAD['uv_near_left'][1]})"
)

print("\n-- clip gate: GetTileCount parity — the game draws nothing on a sub-half-period stub --")
for label, span_len, mesh_len, tiling, want_tiles, want_clip in (
    ("L=2.9 S=12 T=2 (dash stub)", 2.9, 12.0, 2, 0, 0),
    ("L=3.1 S=12 T=2 (just long enough)", 3.1, 12.0, 2, 1, 1),
    ("L=74 S=12 T=1 (highway dash line)", 74.0, 12.0, 1, 1, 6),
    ("L=74 S=12 T=2 (car dash line)", 74.0, 12.0, 2, 1, 12),
    ("L=16 S=12 T=16 (crosswalk)", 16.0, 12.0, 16, 1, 21),
    ("L=40 S=12 T=0 (solid line)", 40.0, 12.0, 0, 1, 0),
):
    tiles, clip = H["_lane_tile_count"](span_len, mesh_len, tiling, False)
    bad = (tiles, clip) != (want_tiles, want_clip)
    failures += bad
    print(
        f"{'FAIL' if bad else 'ok  '} {label}: tiles={tiles} clip={clip}"
        + (f" (expected {want_tiles}/{want_clip})" if bad else "")
    )

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
            decals = [
                parts[i]
                for i in record.get("parts", [])
                if 0 <= i < len(parts) and parts[i].get("decal")
            ]
            if not decals:
                continue
            curve = record["curve"]
            curve_length = H["_curve_length"](curve)
            for span in record.get("spans") or [[0.0, 1.0]]:
                if curve_length * (span[1] - span[0]) <= 0.1:
                    continue
                base = H["_cut_bezier"](curve, span[0], span[1])
                for part in decals:
                    bezier = (base[3], base[2], base[1], base[0]) if part.get("inverted") else base
                    asset = part.get("assetId", "")
                    # The producer's own `decalRole`, through the shipped rule — NOT the
                    # assetId substring test this line used to carry. The two disagree on 7 of
                    # the 43 decal parts in the shipped catalogue, all one way: real markings
                    # demoted to the wear tint's lower lift, i.e. this sweep was scoring them
                    # against a tolerance the importer never uses.
                    eps = decal_rules.drape_lift(part.get("decalRole"))
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
                            print(
                                f"FAIL {asset}: dip {dip * 1000:.1f} mm > lift "
                                f"{eps * 1000:.1f} mm at rows={rows} (cap not reached)"
                            )
                    if dip - eps > worst[0]:
                        worst = (dip - eps, asset)
    print(
        f"{'FAIL' if buried > capped else 'ok  '} {spans_seen} decal spans, "
        f"{buried} buried ({capped} spans at the {H['_DECAL_MAX_ROWS']}-row cap), "
        f"{verts} decal vertices"
    )
    if worst[1]:
        print(f"     worst overshoot {worst[0] * 1000:.1f} mm on {worst[1]}")

sys.exit(1 if failures else 0)
