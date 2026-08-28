"""Where does CS2 actually put the ground under and beside a road?

The game does not merely *hide* terrain at a carriageway (that is the stencil prism, §46) — it
also *grades* it. `Game.Simulation.TerrainSystem.CullRoadsJob.AddSegment` builds a `LaneSection`
per road piece carrying

    minOffset = maxOffset = (edgeHeights.x|z, surfaceHeight.min, edgeHeights.y|w)   [+ the
                             composition's TerrainComposition m_Min/m_MaxHeightOffset]

and `DrawHeightAdjustments` renders those sections into `m_HeightmapCascade` *on top of* the
stored heightmap, every frame, before `UpdateGPUReadback` reads the cascade back into
`m_CPUHeights`. So `GetHeightData()` — and therefore the bundle's `heightmap.png` — is the
graded ground, and for an ordinary at-grade road min == max, i.e. the height is ASSIGNED, not
clamped: `terrain = lane + profile(u)`, `profile` interpolating the three offsets across the
width and blending back to natural terrain over `NetUtils.GetTerrainSmoothingWidth` = 8 m.

The clip band the bundle ships is derived from the same triple:

    bottom = cmin(triple) + clipHeightOffset.x - 0.3      top = cmax(triple) + clipHeightOffset.y + 0.3

so the game's own grade must land in `[bottom + 0.3, top - 0.3]` — a window this probe can test
per sample without any tuned constant. That is the predicate.

    python tools/check_roadgrade_vs_game.py [--base] [--limit N]

Reads only the shipped bundle; no built level, no Blender. It is a probe, not a gate.

DECLARED SINGLE-FILE EXCEPTION to the 400-line ceiling (Phase 4, 2026-08-28): the nine
numbered sections are one linear derivation, each consuming the previous one's rasters, and
splitting that narrative across modules would scatter a measurement a reader follows top to
bottom. The file rides the shrink-only allowlist.
"""

import math
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import itertools  # noqa: E402 - after the sys.path bootstrap above

from csiibundle import bezier, clipraster, containment, png, reader  # noqa: E402
from csiibundle.bake.locate import default_bundle_dir  # noqa: E402

BUNDLE = default_bundle_dir()  # CS2_BUNDLE_DIR overrides; see `locate.BUNDLE_DIR_ENV`

# `TerrainRipSystem.ClipBandExpansion` — GenerateClipDataJob's +-0.3 m, added to the band AFTER
# the offsets are taken, so subtracting it recovers the game's own cmin/cmax profile bounds.
CLIP_BAND_EXPANSION = 0.3

prefer_fine = "--base" not in sys.argv
limit = None
if "--limit" in sys.argv:
    limit = int(sys.argv[sys.argv.index("--limit") + 1])

field = reader.load_heightfield(BUNDLE, prefer_fine=prefer_fine)
H, W = field.raw.shape
cell, ox, oz = field.cell, field.origin_x, field.origin_z
terrain_y = field.raw.astype(numpy.float64) * field.elevation_scale + field.elevation_offset
print(
    f"ground  {W}x{H} @ {cell} m  ({'terrainFine' if field.fine else 'base heightmap'}), "
    f"quantum {field.elevation_scale:.4f} m"
)

# -- the game's shallow road bands, rasterised, keeping lane / cmin / cmax per sample ------------
# Same walk as `clipgrid.clip_grid`, but recording what the band was measured against instead of
# only whether the sample fell inside it. Lowest floor wins where two strips overlap, exactly as
# `clip_span` does, so a cell under two roads is scored against the lower one.
lane_r = numpy.full((H, W), numpy.nan)
lat_r = numpy.full((H, W), numpy.nan)  # lateral parameter across the strip, 0..1
wid_r = numpy.full((H, W), numpy.nan)  # the strip's width at that station, metres
cmin_r = numpy.full((H, W), numpy.nan)
cmax_r = numpy.full((H, W), numpy.nan)
floor_r = numpy.full((H, W), numpy.inf)
strip_r = numpy.full((H, W), -1, dtype=numpy.int32)  # which strip's band won this sample
strip_stats = []  # per strip: (sag, bend, cross, length)


def to_cell(points):
    return numpy.stack(
        ((points[:, 0] - ox) / cell + 0.5, (oz - points[:, 2]) / cell + 0.5), axis=-1
    )


def mark(quad, lane_y, bottom, top, sid=-1):
    """One station quad: mark the samples the band covers and record lane/cmin/cmax there."""
    lane_u, ramp = clipraster.lane_ramp(quad, lane_y)
    rows = [q[1] for q in quad]
    r0 = max(0, int(math.ceil(min(rows) - 0.5)))
    r1 = min(H - 1, int(math.floor(max(rows) - 0.5)))
    for row in range(r0, r1 + 1):
        y = row + 0.5
        crossings = []
        for e in range(4):
            px, py = quad[e]
            qx, qy = quad[(e + 1) & 3]
            if (py <= y) == (qy <= y):
                continue
            crossings.append(px + (y - py) / (qy - py) * (qx - px))
        if len(crossings) < 2:
            continue
        crossings.sort()
        for pair in range(0, len(crossings) - 1, 2):
            lo = max(0, int(math.ceil(crossings[pair] - 0.5)))
            hi = min(W - 1, int(math.floor(crossings[pair + 1] - 0.5)))
            if hi < lo:
                continue
            # The lateral parameter across the strip, ALWAYS (not only when the two sides differ
            # in height, which is all `lane_ramp` needs it for): the profile is a function of it.
            lx = (quad[0][0] + quad[3][0]) * 0.5
            lz = (quad[0][1] + quad[3][1]) * 0.5
            rx = (quad[1][0] + quad[2][0]) * 0.5
            rz = (quad[1][1] + quad[2][1]) * 0.5
            ax_, az_ = rx - lx, rz - lz
            len2 = ax_ * ax_ + az_ * az_
            px = numpy.arange(lo, hi + 1, dtype=numpy.float64) + 0.5
            if len2 < 1e-9:
                lat = numpy.full(hi - lo + 1, 0.5)
                width = 0.0
            else:
                lat = numpy.clip(((px - lx) * ax_ + (y - lz) * az_) / len2, 0.0, 1.0)
                width = math.sqrt(len2) * cell
            if ramp is None:
                lane = numpy.full(hi - lo + 1, lane_u)
            else:
                y_left, dy, rlx, rlz, rax, raz, inv = ramp
                sfrac = numpy.clip(((px - rlx) * rax + (y - rlz) * raz) * inv, 0.0, 1.0)
                lane = y_left + dy * sfrac
            over = terrain_y[row, lo : hi + 1] - lane
            inside = (over > bottom) & (over < top)
            if not inside.any():
                continue
            wins = inside & (lane + bottom < floor_r[row, lo : hi + 1])
            if not wins.any():
                continue
            idx = numpy.nonzero(wins)[0] + lo
            floor_r[row, idx] = (lane + bottom)[wins]
            lane_r[row, idx] = lane[wins]
            cmin_r[row, idx] = bottom + CLIP_BAND_EXPANSION
            cmax_r[row, idx] = top - CLIP_BAND_EXPANSION
            strip_r[row, idx] = sid
            lat_r[row, idx] = lat[wins]
            wid_r[row, idx] = width


strips = list(reader.read_jsonl(reader.layer_path(BUNDLE, "clipVolumes")))
if limit:
    strips = strips[:limit]
shallow = deep = 0
for n, strip in enumerate(strips):
    if n and n % 5000 == 0:
        print(f"  ... {n:,}/{len(strips):,} clip volumes", flush=True)
    bottom = float(strip["bottom"])
    top = float(strip["top"]) if strip.get("top") is not None else float("inf")
    # Only SHALLOW bands: a deep one is a trench or a portal, where the game hides the cover
    # rather than grading it, and there is no carriageway grade to test against.
    if top > containment.CLIP_DEEP_BAND_TOP:
        deep += 1
        continue
    shallow += 1
    if strip.get("kind") == "area":
        nodes, tris = strip.get("nodes") or [], strip.get("triangles") or []
        for tri in tris:
            if any(not 0 <= i < len(nodes) for i in tri):
                continue
            pts = numpy.asarray([nodes[i] for i in tri], dtype=numpy.float64)
            c = to_cell(pts)
            mark(
                [tuple(c[0]), tuple(c[1]), tuple(c[2]), tuple(c[2])],
                float(pts[:, 1].mean()),
                bottom,
                top,
            )
        continue
    left, right = strip.get("left"), strip.get("right")
    if not left or not right:
        continue
    poly = numpy.asarray(left, dtype=numpy.float64)
    span = float(numpy.linalg.norm(numpy.diff(poly, axis=0), axis=1).sum())
    steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))
    t = numpy.linspace(0.0, 1.0, steps + 1)
    lp, rp = bezier.evaluate(left, t), bezier.evaluate(right, t)
    lc, rc = to_cell(lp), to_cell(rp)
    ly, ry = lp[:, 1], rp[:, 1]

    # -- what shape is this piece of road? ------------------------------------------------------
    # Three covariates, all from the strip's own curves, so "not flat / curved" becomes a number:
    #   sag   the lane centreline's largest departure from the straight chord between its ends,
    #         VERTICALLY (metres). A crest or a dip; zero on a road with constant grade.
    #   bend  total heading change in plan (degrees) — how much the road turns.
    #   cross the largest |left.y - right.y| (metres): crossfall / superelevation, which is what
    #         a road picks up through a curve.
    axis = (lp + rp) * 0.5
    sid = len(strip_stats)
    if len(axis) >= 3:
        arc = numpy.concatenate(
            ([0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(axis[:, [0, 2]], axis=0), axis=1)))
        )
        u = arc / max(arc[-1], 1e-9)
        chord = axis[0, 1] + (axis[-1, 1] - axis[0, 1]) * u
        sag = float(numpy.abs(axis[:, 1] - chord).max())
        d = numpy.diff(axis[:, [0, 2]], axis=0)
        head = numpy.arctan2(d[:, 1], d[:, 0])
        bend = float(numpy.abs(numpy.degrees(numpy.diff(numpy.unwrap(head)))).sum())
        length = float(arc[-1])
    else:
        sag, bend, length = 0.0, 0.0, 0.0
    strip_stats.append((sag, bend, float(numpy.abs(ly - ry).max()), length))

    for i in range(1, steps + 1):
        mark(
            [tuple(lc[i - 1]), tuple(rc[i - 1]), tuple(rc[i]), tuple(lc[i])],
            (float((ly[i - 1] + ly[i]) * 0.5), float((ry[i - 1] + ry[i]) * 0.5)),
            bottom,
            top,
            sid,
        )

covered = numpy.isfinite(lane_r)
n = int(covered.sum())
print(
    f"clip volumes: {shallow:,} shallow, {deep:,} deep; {n:,} samples under a shallow band "
    f"({n * cell * cell / 1e6:.2f} km2)"
)
if not n:
    sys.exit("no shallow band covered a sample — nothing to measure")

over = (terrain_y - lane_r)[covered]
cmin, cmax = cmin_r[covered], cmax_r[covered]


def pct(a, *qs):
    return "  ".join(f"p{q}={numpy.percentile(a, q):+.3f}" for q in qs)


print()
print("== 1. the game's own grade, terrain - lane, over every covered sample ==")
print(f"   {pct(over, 1, 5, 25, 50, 75, 95, 99)}   mean={over.mean():+.3f}")
print()
print("== 2. the predicate: does it land in the game's own profile window [cmin, cmax]? ==")
inside = (over >= cmin - 1e-6) & (over <= cmax + 1e-6)
below = over < cmin - 1e-6
above = over > cmax + 1e-6
print(
    f"   inside {inside.sum():,} ({inside.mean() * 100:.2f} %)   "
    f"below cmin {below.sum():,} ({below.mean() * 100:.2f} %)   "
    f"above cmax {above.sum():,} ({above.mean() * 100:.2f} %)"
)
if below.any():
    d = (cmin - over)[below]
    print(f"   below by: {pct(d, 50, 95, 99)}  max={d.max():.3f} m")
if above.any():
    d = (over - cmax)[above]
    print(f"   above by: {pct(d, 50, 95, 99)}  max={d.max():.3f} m")
print(
    f"   (one elevation quantum is {field.elevation_scale:.4f} m; a sample whose texel centre "
    f"sits in the 8 m smoothing apron\n    of a NEIGHBOURING road, or on the far side of the "
    f"profile's lateral ramp, legitimately misses the window)"
)

print()
print(
    "== 3. by band class -> the game's profile window, and the missing TerrainComposition offset =="
)
print("   The band brackets `baseHeights` only; the GRADE is `baseHeights + m_MinHeightOffset`")
print("   (NetCompositionSystem merges it per composition from each NetTerrainPiece), and the")
print("   bundle does not carry it. `off` below is that offset recovered per class as")
print("   median(terrain-lane) - cmin; `in-window+off` is the predicate with it restored.")
classes = {}
for b, t in zip(cmin_r[covered], cmax_r[covered]):
    classes[(round(b, 3), round(t, 3))] = classes.get((round(b, 3), round(t, 3)), 0) + 1
corrected = numpy.zeros(over.shape, dtype=bool)
for (b, t), c in sorted(classes.items(), key=lambda kv: -kv[1]):
    sel = (numpy.round(cmin, 3) == b) & (numpy.round(cmax, 3) == t)
    o = over[sel]
    off = float(numpy.median(o)) - (b if b == t else numpy.clip(numpy.median(o), b, t))
    q = field.elevation_scale
    ok = (o >= b + off - q) & (o <= t + off + q)
    corrected[sel] = ok
    if c >= 1000:
        print(
            f"   cmin={b:+.3f} cmax={t:+.3f}  {c:>9,} samples  terrain-lane {pct(o, 5, 50, 95)}  "
            f"off={off:+.3f}  in-window {((o >= b - 1e-6) & (o <= t + 1e-6)).mean() * 100:5.1f} % "
            f"-> in-window+off {ok.mean() * 100:5.1f} %"
        )
print(
    f"   ALL CLASSES, one elevation quantum ({field.elevation_scale:.4f} m) of slack: "
    f"{corrected.sum():,} / {n:,} = {corrected.mean() * 100:.2f} % agree with the game's grade"
)

sipath = os.path.join(BUNDLE, "terrain", "surface_index.png")
roadbed = None
if os.path.isfile(sipath):
    si = png.gray_array(sipath)
    if si.shape != (H, W):
        si = reader.resample_nearest(si, (H, W))
    roadbed = si == 1

print()
print("== 3b. the residual: samples standing above the game's own profile top ==")
print("   These are the only ones a consumer that leaves the heightmap alone would show over a")
print("   road. `surface_index == 1` is the CARRIAGEWAY footprint (the producer deliberately does")
print("   not stamp the 8 m smoothing apron), so a residual outside it has no asphalt to pierce.")
if roadbed is not None:
    on_road = roadbed[covered]
    print(
        f"   above cmax and ON the carriageway:  {int((above & on_road).sum()):,} "
        f"({(above & on_road).sum() / max(1, n) * 100:.2f} % of covered samples)"
    )
    if (above & on_road).any():
        d = (over - cmax)[above & on_road]
        print(f"       excess over the profile top: {pct(d, 50, 95, 99)}  max={d.max():.3f} m")
    print(
        f"   above cmax and OFF it (apron/ramp): {int((above & ~on_road).sum()):,} "
        f"({(above & ~on_road).sum() / max(1, n) * 100:.2f} %)"
    )

# -- 4. what the consumer does with that -------------------------------------------------------
print()
print("== 4. what the consumer does to the game's answer ==")
print(
    "   `carriageway.ground_heights` keeps the shipped heights and applies `clamp_carriageway` and"
)
print("   nothing else: a sample standing above the game's own profile top is lowered onto it.")
ceiling = cmax  # == lane + top - expansion, minus lane
moved = over > ceiling
print(
    f"   clamp_carriageway lowers {int(moved.sum()):,} of {n:,} covered samples "
    f"({moved.mean() * 100:.2f} %) onto the carriageway profile"
)
if moved.any():
    d = (over - ceiling)[moved]
    print(f"       drop applied: {pct(d, 50, 95, 99)}  max={d.max():.3f} m")
    print(f"       ground volume removed: {float(d.sum()) * cell * cell:,.0f} m3")
print(
    f"   every other sample under a road is left exactly where CS2 graded it "
    f"({int((~moved).sum()):,}, {(~moved).mean() * 100:.2f} %)"
)

print()
print("== 5. against the two rules audit §51 deleted ==")
print("   For scale, what the retired constants would have done to this same rip:")
SINK = 0.15
if roadbed is not None:
    sink_mask = clipraster.dilate(roadbed, 1)
    print(
        f"   ROADBED_SINK {SINK} m would move {int(sink_mask.sum()):,} samples "
        f"({int(sink_mask.sum()) * cell * cell / 1e6:.2f} km2), "
        f"{int((sink_mask & ~covered).sum()):,} of them under no clip band at all"
    )
band_bottom = (floor_r - lane_r)[covered]  # == bottom, by construction
old = over - band_bottom  # the drop the band-FLOOR clamp applied
print(
    f"   the band-FLOOR clamp would move {int((old > 0).sum()):,} of {n:,} covered samples "
    f"({(old > 0).mean() * 100:.2f} %), median {numpy.median(old[old > 0]):.3f} m, "
    f"landing them {numpy.median(cmin[old > 0] - band_bottom[old > 0]):.3f} m below the game's cmin"
)

# Composed, in the order the old `ground_heights` applied them, so the overlap is not counted
# twice: sink everywhere the roadbed reaches, THEN clamp whatever still stands above the floor.
g = terrain_y.copy()
touched = numpy.zeros((H, W), dtype=bool)
if roadbed is not None:
    g -= numpy.where(sink_mask, SINK, 0.0)
    touched |= sink_mask
g = numpy.where(numpy.isfinite(floor_r) & (g > floor_r), floor_r, g)
touched |= covered
delta = (terrain_y - g)[touched]
hit = delta > 1e-9
print(
    f"   the two composed: {int(hit.sum()):,} samples moved, median "
    f"{numpy.median(delta[hit]):.3f} m, p95 {numpy.percentile(delta[hit], 95):.3f} m, "
    f"max {delta.max():.3f} m"
)
print(
    f"   ground volume: {float(delta[hit].sum()) * cell * cell:,.0f} m3 removed by the pair, "
    f"against {float((over - ceiling)[moved].sum()) * cell * cell:,.0f} m3 by what ships now"
)

# -- 6. does the residual follow "not flat / curved"? -------------------------------------------
print()
print("== 6. what the residual after the clamp correlates with ==")
print("   A sample the clamp leaves is one at or below the profile TOP (cmax, the highest point of")
print("   the cross-section) but still above the road SURFACE beside it. Bucketed by three shape")
print("   covariates of the strip whose band won the sample, all from its own curves.")
sid_r = strip_r[covered]
have = sid_r >= 0
stats = numpy.asarray(strip_stats, dtype=numpy.float64) if strip_stats else numpy.zeros((1, 4))
after = numpy.minimum(over, cmax)  # terrain - lane, after clamp_carriageway
excess = after - cmin  # how far above the cross-section's LOW point
for label, col, edges in (
    ("vertical sag (m)", 0, (0.0, 0.02, 0.05, 0.1, 0.25, 1e9)),
    ("plan bend (deg)", 1, (0.0, 1.0, 5.0, 15.0, 45.0, 1e9)),
    ("crossfall (m)", 2, (0.0, 0.02, 0.05, 0.1, 0.3, 1e9)),
):
    v = numpy.full(sid_r.shape, numpy.nan)
    v[have] = stats[sid_r[have], col]
    print(f"   by {label}:")
    for lo_e, hi_e in itertools.pairwise(edges):
        sel = have & (v >= lo_e) & (v < hi_e)
        if sel.sum() < 500:
            continue
        e = excess[sel]
        print(
            f"      [{lo_e:>5.2f}, {hi_e:>6.2f})  {int(sel.sum()):>9,} samples  "
            f"above the cross-section low point: p50={numpy.median(e):+.3f}  "
            f"p95={numpy.percentile(e, 95):+.3f}  over 0.05 m: {(e > 0.05).mean() * 100:5.1f} %"
        )

# -- 7. what each candidate ceiling would buy ---------------------------------------------------
print()
print("== 7. would a re-rip close it? what each candidate ceiling removes ==")
print("   Every candidate is a quantity the GAME computes; the question is only which of them the")
print("   bundle carries. Scored as the ground left standing above the cross-section's low point")
print("   (`lane + cmin`), which is the worst case for a sample in the middle of the carriageway.")
base = excess
cands = [
    ("cmax                        (today, band top - 0.3)", cmax),
    ("cmax - 0.05                 (+ m_MinHeightOffset, the ONE field a re-rip adds)", cmax - 0.05),
    ("cmin                        (the cross-section LOW point: full lateral profile)", cmin),
]
for label, ceil in cands:
    a2 = numpy.minimum(over, ceil)
    e = a2 - cmin
    print(f"   {label}")
    print(
        f"      left over 0.05 m: {int((e > 0.05).sum()):>9,} ({(e > 0.05).mean() * 100:5.2f} %)   "
        f"p95 {numpy.percentile(e, 95):+.3f} m   max {e.max():+.3f} m   "
        f"volume above the low point {float(numpy.clip(e, 0, None).sum()) * cell * cell:,.0f} m3"
    )
print()
print("   NOTE `cmin` is an upper bound, not a proposal: at the road's outer edge the ground is")
print("   MEANT to rise to the kerb, so clamping the whole footprint to the low point would cut")
print("   into the verge. What closes it properly is the lateral profile itself \u2014 the triple")
print("   (edgeHeight, surfaceHeight.min, edgeHeight) plus m_MiddleSize \u2014 which is the same")
print("   producer change, one field wider.")

# -- 8. which end of the band is the ASPHALT? ---------------------------------------------------
# `baseHeights` is (edgeHeight, surfaceHeight.min, edgeHeight) and the band ships only its min and
# max, so which of cmin/cmax is the middle of the carriageway and which is its outer edge is not
# stated anywhere in the bundle. Settle it from the geometry: erode the stamped carriageway so
# "interior" and "rim" can be compared. If the interior sits at cmin and the rim at cmax, the
# asphalt is the LOW end and today's ceiling (cmax) is the kerb top - up to (cmax - cmin) above
# the road surface in the middle.
if roadbed is not None:
    print()
    print("== 8. is the asphalt at cmin or at cmax? ==")
    interior = roadbed.copy()
    for _ in range(2):
        e = interior.copy()
        e[1:, :] &= interior[:-1, :]
        e[:-1, :] &= interior[1:, :]
        e[:, 1:] &= interior[:, :-1]
        e[:, :-1] &= interior[:, 1:]
        interior = e
    rim = roadbed & ~interior
    for label, m in (("carriageway INTERIOR (eroded 2 cells)", interior), ("carriageway RIM", rim)):
        sel = m[covered]
        if sel.sum() < 500:
            continue
        o = over[sel]
        # distance from each end of the band, in the band's own units
        print(f"   {label}: {int(sel.sum()):>9,} samples  terrain-lane {pct(o, 5, 50, 95)}")
        print(
            f"      (terrain-lane) - cmin: {pct(o - cmin[sel], 5, 50, 95)}"
            f"      (terrain-lane) - cmax: {pct(o - cmax[sel], 5, 50, 95)}"
        )

# -- 9. the game's own lateral profile, evaluated from shipped data -----------------------------
# `AddSegment` builds `middleSize = saturate(1 - max(width*0.2, 3) / max(1, width))` from the
# SAME trimmed width the shipped clip curves span, so it is recoverable here with no new field.
# The profile is then: a flat plateau over the central `middleSize` of the width at the middle
# offset, ramping to the edge offset over the outer half-margin on each side.
#
# The one thing the bundle does NOT state is which of the band's two ends is the middle and which
# is the edge. Section 8 answers it from the geometry (interior sits at cmin, rim climbs to cmax),
# and that is exactly the assumption a re-rip would remove.
print()
print("== 9. what a PROFILE-aware ceiling would buy (the re-rip's real prize) ==")
lat = lat_r[covered]
wid = wid_r[covered]
ok = numpy.isfinite(lat) & numpy.isfinite(wid) & (wid > 0)
mid_size = numpy.clip(1.0 - numpy.maximum(wid * 0.2, 3.0) / numpy.maximum(wid, 1.0), 0.0, 1.0)
d = numpy.abs(lat - 0.5) * 2.0  # 0 at the centreline, 1 at a side
t = numpy.clip((d - mid_size) / numpy.maximum(1.0 - mid_size, 1e-9), 0.0, 1.0)
profile = cmin + (cmax - cmin) * t  # the game's cross-section
profile = numpy.where(ok, profile, cmax)  # no width -> today's behaviour
print(
    f"   middleSize recovered from the shipped curves: p50 {numpy.median(mid_size[ok]):.2f}, "
    f"width p50 {numpy.median(wid[ok]):.1f} m"
)
for label, ceil in (
    ("cmax (today: the cross-section's TOP, everywhere)", cmax),
    ("the game's lateral profile at each sample's own position", profile),
):
    lowered_n = int((over > ceil).sum())
    resid = numpy.clip(numpy.minimum(over, ceil) - profile, 0.0, None)
    print(f"   {label}")
    print(
        f"      clamp fires on {lowered_n:,} ({lowered_n / max(1, n) * 100:.2f} %); "
        f"ground left above the game's own road surface: {int((resid > 0.02).sum()):,} samples "
        f"({(resid > 0.02).mean() * 100:.2f} %), p95 {numpy.percentile(resid, 95):.3f} m, "
        f"max {resid.max():.3f} m, {float(resid.sum()) * cell * cell:,.0f} m3"
    )
