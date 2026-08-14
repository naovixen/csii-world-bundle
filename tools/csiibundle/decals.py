"""Road markings, wear tint and crosswalks — projections rebuilt as geometry, and the cost.

In the game a lane decal is **one instanced projector box** (§5B, audit §19): three matrices, a
texture, and the GPU paints it onto whatever surface the box encloses. Zero CPU work per
marking, and it cannot z-fight or sag because it is not geometry at all. A consumer that has no
projector pass has to rebuild each marking as a real mesh laid on the real surface, and that
substitution is where every defect in this module's history came from. Three of them are worth
carrying forward:

**The texture repeats; the box stretches.** `BuildTransformMatrix`'s decal path stretches the
geometry chord-to-chord along the span, but `BuildCurveMatrix` scales the along-curve texture
parameter by `N = length / max(1, meshLength)`, quantised to `round(N·T)/T` when
`T = tilingCount` is non-zero (audit §39). Reasoning from the box's scale to the UVs paints one
enormous arrow where the game draws fourteen small ones. `station_rows()` is where N enters.

**A sub-half-period stub draws nothing.** `GetTileCount` runs for decals exactly as for tiled
geometry, and `T != 0` with `clip = round(L/S·T) == 0` means the game emits ZERO instances.
Drawing one anyway puts a marking on stubs the game leaves bare. CONTRACT.

**Subdivision is driven by burial, not by shape.** Between two draped stations the rebuilt mesh
is a straight edge, and a road crest RISES above that edge; wherever it rises further than the
marking's own lift, the asphalt wins the depth test and the marking vanishes. So the row count
cannot come from a plan-shape tolerance chosen independently of the lift — it has to be a
fraction OF the lift. Measured over a full city's 83,110 decal spans: buried 3.6% with the old
fixed 3 m step, **13.0%** with a single 3D shape tolerance, 2 spans (0.002%) with the two-rule
form here. `tools/test_decal_rows.py` pins it; run that before re-deriving anything below.

The falsified theory, recorded so it is not rediscovered: the marking lift is **not** a kerb
height. That hypothesis was tested and reverted.
"""

import math

import numpy

from . import bezier

# -- the lift ladder ---------------------------------------------------------------------------
#
# Everything the game draws by PROJECTION lands on one and the same ground in-game and cannot
# z-fight. Rebuilt as geometry it can, so each layer is lifted clear of the one it paints over,
# in the order the game composites them. THE ORDER IS WHAT MATTERS, not the values: they are all
# far below the tolerance of the ground they sit on (a 3 cm error is already inside it).
#
# Getting the order wrong is visible: an area lifted ABOVE the decal instances standing on it
# paints the parking bays out of a parking lot, which is exactly what 0.02 vs 0.015 did before
# 2026-08-08. Kept in ONE table because splitting it across modules is how it broke.
#
# TUNED (the magnitudes) / CONTRACT (the order).
LIFT_LADDER = (
    ("terrain", 0.000),        # the thing being painted on
    ("lane_tint", 0.008),      # lane wear tint          ) on the ROAD, draped onto it
    ("lane_marking", 0.014),   # markings, crosswalks    )  per vertex
    ("area", 0.020),           # §8b surface areas — pavement, driveways, paths
    ("instance_decal", 0.030),  # §4.2a decal instances — bay markings, arrows
)
LIFT = dict(LIFT_LADDER)

# The lane bend's PRE-DRAPE offset. NOT a rung of the ladder above and must not be tuned with
# it: the drape centres each marking's ±half-height search window on the height this produces,
# so raising it to 0.030 along with the ladder pushed 8 of one region's 4,694 decal vertices out
# of the top of their own projector window — a silent accuracy loss caught only by a count gate.
BEND_LIFT = 0.015

# Which rung a decal part drapes to, by its `decalRole` (§5B). The producer classifies the part;
# do NOT revive the assetId substring test that used to stand here — on the shipped catalogue
# the two disagree on 7 of 43 decal parts, all one way, demoting real markings to the tint's
# lower lift. CONTRACT.
DRAPE_LIFT_BY_ROLE = {"marking": LIFT["lane_marking"]}
DRAPE_LIFT_DEFAULT = LIFT["lane_tint"]


def drape_lift(decal_role):
    """The lift a decal part with this `decalRole` drapes to. Markings ride a hair above the
    ~12%-alpha wear tint so the two never z-fight where a crosswalk crosses a drive lane."""
    return DRAPE_LIFT_BY_ROLE.get(decal_role, DRAPE_LIFT_DEFAULT)


# -- subdivision ------------------------------------------------------------------------------

# How faithfully the flat lattice follows the curve IN PLAN. Cutting a corner by 2 cm is
# invisible and cannot bury anything — the drape puts every vertex on the surface at the
# vertex's OWN (x, z), wherever the chord placed it. TUNED.
SHAPE_TOLERANCE = 0.02

# The share of the part's own lift that the vertical rise over a chord may consume. This one
# cannot be picked independently of the lift or the rule permits exactly the burial the lift
# exists to prevent; a half leaves the marking clear by at least its remaining half. CONTRACT
# (as a relationship — the number is TUNED).
DIP_FRACTION = 0.5

# Ceiling on curvature refinement, so a pathological curve cannot cost unbounded rows. Period
# boundaries are NOT subject to it (see `station_rows`). TUNED.
MAX_ROWS = 64


def shape_rows(points):
    """Fewest lattice rows whose chords stay within `SHAPE_TOLERANCE` of the curve, in plan.

    `points` are the arc-table samples the bend already computed for this span, so curvature
    costs nothing extra. A flat run gets 1 row (2 stations); a curved one subdivides, using that
    a smooth arc's sagitta falls off roughly as the square of the subdivision count.

    Plan fidelity only — burial is `station_count`'s job, on a different axis and a different
    tolerance, and conflating the two is the 13%-buried mistake in the module docstring.
    """
    max_sagitta = bezier.sagitta(points)
    if max_sagitta <= SHAPE_TOLERANCE:
        return 1
    return max(1, min(MAX_ROWS, int(math.ceil(math.sqrt(max_sagitta / SHAPE_TOLERANCE)))))


def chord_dip(arc_table, height, rows):
    """How far the span's vertical profile rises above the chord polyline of `rows` rows.

    This is the quantity that buries a marking. Measured on the arc table's own samples,
    arc-parameterised exactly as the bend places stations (`arc_pos = station · total_arc`), so
    it costs two interpolations and no curve evaluation at all.

    `height` is the bundle-frame up axis of those samples. The lane curve stands in for the road
    surface profile here — the carriageway is built along the same curve — so this sees grade
    changes, not the crown, which the per-vertex drape handles on its own.
    """
    total = float(arc_table[-1])
    station_arc = numpy.linspace(0.0, total, rows + 1)
    station_height = numpy.interp(station_arc, arc_table, height)
    return float((height - numpy.interp(arc_table, station_arc, station_height)).max())


def station_count(points, arc_table, drape_eps):
    """Lattice rows for one decal span: plan fidelity first, then refined until it cannot bury.

    The shape rule alone measures deviation from the 3D chord, which on a road is dominated by
    the horizontal bend — the axis that cannot bury a marking. So it both over-subdivides flat
    bends and, worse, leaves a long straight run over a crest at a single row: a 19 mm crest is
    inside a 20 mm shape tolerance and outside a 14 mm lift. The refinement doubles the rows
    until the MEASURED rise over a chord is at most `DIP_FRACTION` of the part's own lift — a
    measured bound, not the `1/rows²` extrapolation the shape rule assumes, which a curve with
    concentrated curvature does not obey.

    `drape_eps` is `None` for a part that will not be draped, which needs plan fidelity only.
    """
    rows = shape_rows(points)
    if drape_eps is None or float(arc_table[-1]) < 1e-6:
        return rows
    target = drape_eps * DIP_FRACTION
    height = numpy.asarray(points, dtype=numpy.float64)[:, 1]
    while rows < MAX_ROWS and chord_dip(arc_table, height, rows) > target:
        rows = min(MAX_ROWS, rows * 2)
    return rows


def station_rows(rows, n_total):
    """Lattice stations for one decal span, plus each station's within-period V parameter.

    The game repeats the texture `n_total` times along the span (see the module docstring).
    Baking that into vertex UVs needs a SEAM at every integer of `n = n_total · station`, so
    this merges the curvature-driven rows with a DUPLICATED station at each interior period
    boundary: the first copy ends the old period (`v_frac = 1`), the second starts the next
    (`v_frac = 0`). Authored V per row is `v_near + (v_far − v_near) · v_frac` — the corner-lerp
    form, because the quad's UV rect is arbitrary after the `m_TextureArea` rescale (§4.2a).
    **Never assume [0, 1].** The last period is partial when `n_total` is fractional, which is
    game-true — the shader's `frac()` of the repeated parameter.

    Curvature stations within 1e-6 of a boundary yield to the boundary pair, so a third
    coincident station cannot add a zero-extent gap. Boundary rows are correctness-mandatory and
    therefore NOT capped by `MAX_ROWS`: that cap bounds curvature refinement cost, and clamping
    a period seam would smear the texture across the wrap — the very defect this prevents.

    Returns `(stations, v_frac)`, both ascending-station `(M,)` float arrays.
    """
    base = numpy.linspace(0.0, 1.0, rows + 1)
    bounds = numpy.arange(1, math.ceil(n_total - 1e-9)) / n_total
    if len(bounds) == 0:
        return base, base * n_total
    keep = base[numpy.abs(base[:, None] - bounds[None, :]).min(axis=1) > 1e-6]
    tag = numpy.concatenate([numpy.zeros(len(keep)), numpy.ones(len(bounds)),
                             numpy.full(len(bounds), 2.0)])
    val = numpy.concatenate([keep, bounds, bounds])
    order = numpy.lexsort((tag, val))  # by station; period-end copy before period-start copy
    stations, tag = val[order], tag[order]
    # Period index per station: `searchsorted(left)` puts an interior station in its own period
    # and a boundary station in the period it ENDS; the start copy (tag 2) bumps up.
    period = numpy.searchsorted(bounds, stations, side="left") + (tag == 2.0)
    v_frac = n_total * stations - period
    return stations, numpy.clip(v_frac, 0.0, 1.0)


# -- the quad and its lattice -------------------------------------------------------------------

def quad_info(verts, tris, uvs):
    """Reduce a two-triangle decal rectangle to its four corners, for direct lattice generation.

    Returns `None` for anything that is not a simple rectangle — a rare non-rectangular decal
    source, which the caller must then take through its general unsubdivided path rather than
    guess at. `verts` is `(4, 3)`, `tris` `(2, 3)` int, `uvs` `(2, 3, 2)`, all part-local in the
    bundle frame.

    Called once per unique source asset (there are dozens, not thousands), so this stays plain
    Python; no part of it runs per marking.

    `flip` records whether the corner ordering below reverses the source's own winding. Winding
    matters more than it looks: a marking draped flat and lit from above renders BLACK the
    moment it faces down.
    """
    if len(verts) != 4 or len(tris) != 2:
        return None
    corner_uv = {}
    for tri, tri_uv in zip(tris, uvs):
        for vertex, uv in zip(tri, tri_uv):
            corner_uv.setdefault(int(vertex), (float(uv[0]), float(uv[1])))
    z_sorted = sorted(float(v[2]) for v in verts)
    if abs(z_sorted[0] - z_sorted[1]) > 1e-4 or abs(z_sorted[2] - z_sorted[3]) > 1e-4:
        return None
    near = sorted((i for i in range(4) if abs(float(verts[i][2]) - z_sorted[0]) <= 1e-4),
                  key=lambda i: verts[i][0])
    far = sorted((i for i in range(4) if abs(float(verts[i][2]) - z_sorted[2]) <= 1e-4),
                 key=lambda i: verts[i][0])
    if len(near) != 2 or len(far) != 2:
        return None

    def tri_up(a, b, c):
        # y-component of the cross product: the winding as seen from above (part-local space)
        return (b[0] - a[0]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[0] - a[0])

    v0, v1, v2 = (verts[i] for i in tris[0])
    lattice_v0, lattice_v1, lattice_v3 = verts[near[0]], verts[near[1]], verts[far[0]]
    flip = tri_up(v0, v1, v2) * tri_up(lattice_v0, lattice_v1, lattice_v3) < 0
    return {
        "near_left": tuple(float(c) for c in verts[near[0]]),
        "near_right": tuple(float(c) for c in verts[near[1]]),
        "far_left": tuple(float(c) for c in verts[far[0]]),
        "far_right": tuple(float(c) for c in verts[far[1]]),
        "uv_near_left": corner_uv[int(near[0])], "uv_near_right": corner_uv[int(near[1])],
        "uv_far_left": corner_uv[int(far[0])], "uv_far_right": corner_uv[int(far[1])],
        "flip": flip,
    }


def lattice(quad, stations, v_frac):
    """The `(M, 2)`-station lattice for one decal quad, part-local.

    Returns `(verts, tris, tri_uvs)`: verts `(2M, 3)` interleaved
    `[left0, right0, left1, right1, …]`, tris and per-corner UVs to match.

    `stations`/`v_frac` come from `station_rows`: POSITIONS lerp by station, UVs by the
    within-period parameter — so a duplicated boundary station produces two bitwise-identical
    rows whose UVs differ, which is exactly the split seam a repeated texture needs. Triangles
    bridge only gaps with positive station extent, so the duplicate pair emits no zero-area
    faces.

    This is the per-marking hot path — tens of thousands of calls per city — so it is pure numpy
    with no Python loop over rows.
    """
    # Coerce ONCE and use `st` everywhere below. The `[:, None]` view alone is not enough:
    # `numpy.nonzero(stations[1:] > stations[:-1])` further down indexed the raw argument, so a
    # caller passing a plain list got a lexicographic list compare yielding a scalar bool and
    # then `ValueError: Calling nonzero on 0d arrays is not allowed`.
    st = numpy.asarray(stations, dtype=numpy.float64)
    t = st[:, None]
    nl, nr = numpy.array(quad["near_left"]), numpy.array(quad["near_right"])
    fl, fr = numpy.array(quad["far_left"]), numpy.array(quad["far_right"])
    left = nl + (fl - nl) * t
    right = nr + (fr - nr) * t
    verts = numpy.empty((2 * len(st), 3), dtype=numpy.float64)
    verts[0::2] = left
    verts[1::2] = right

    w = numpy.asarray(v_frac, dtype=numpy.float64)[:, None]
    unl, unr = numpy.array(quad["uv_near_left"]), numpy.array(quad["uv_near_right"])
    ufl, ufr = numpy.array(quad["uv_far_left"]), numpy.array(quad["uv_far_right"])
    uv = numpy.empty((2 * len(st), 2), dtype=numpy.float64)
    uv[0::2] = unl + (ufl - unl) * w
    uv[1::2] = unr + (ufr - unr) * w

    row = numpy.nonzero(st[1:] > st[:-1])[0]
    l0, r0, l1, r1 = row * 2, row * 2 + 1, row * 2 + 2, row * 2 + 3
    tris = numpy.stack((numpy.stack((l0, r0, r1), axis=1),
                        numpy.stack((l0, r1, l1), axis=1)), axis=1).reshape(-1, 3)
    if quad["flip"]:
        tris = tris[:, ::-1]
    return verts, tris, uv[tris]
