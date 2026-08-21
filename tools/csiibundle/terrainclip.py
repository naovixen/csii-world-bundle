"""Exact terrain clipping against the §8c clip prisms — the geometry analogue of the game's
stencil, validated against `Game.Simulation.TerrainSystem` in `docs/cs2-api-audit.md` §46.

**The game's rule, in one line:** a terrain pixel is drawn unless its 3-D position lies inside the
swept prism built from the road composition's own trimmed curves and its clip band
(`GenerateClipDataJob` → a closed prism mesh → a stencil volume → a full-screen resolve). The
heightfield is never modified and nothing is ever snapped or sampled: the boundary of the hole is
the prism's own surface.

For a mesh consumer that means the terrain must be **cut**, not scored. The terrain is a
heightfield `h(x, z)`, so the discarded set on its surface is

    { (x, z) : (x, z) inside the prism footprint  AND  bottom(x, z) <= h(x, z) <= top(x, z) }

and its boundary has exactly two kinds of edge — the prism's side wall (a curve in plan) and the
iso-line where the ground enters or leaves the band (the roof edge over a portal mouth). Both are
cut exactly here, because on **one triangle** of the terrain grid every one of those conditions is
affine in `(x, z)`:

* `h` is linear on the triangle (it is a plane through its three vertices);
* the band's `bottom` and `top` are the lane surface plus a constant, and the lane surface is
  fitted affinely over each station quad;
* the footprint's edges are straight lines between the evaluated curve points.

So the discarded part of a triangle is the intersection of half-planes — a convex region — and the
kept part is a convex polygon minus a convex region, which the standard convex subtraction below
splits into at most one convex piece per half-plane. No sampling rate, no snap, no sub-cut.

**Watertight by construction.** Every constraint is a function of `(x, z)` alone except `h`, and
two triangles sharing an edge interpolate the same two endpoint heights along it — so both see the
same cut parameter on that edge and their cut vertices coincide exactly.

The prism quad is split into two triangles before use: a station quad on a sharp curve need not be
convex in plan, and three side half-planes per triangle are exact where four per quad would clip
too much.
"""

import os

import numpy

# A polygon this small in plan is a rounding artefact of the cut, not ground anybody sees; it is
# dropped rather than emitted as a sliver (a square millimetre at 3.5 m cells).
MIN_AREA_M2 = 1e-6
# The narrowest kept piece, measured in PLAN as its smallest altitude (area x 2 / longest edge).
# The game's stencil resolves the cut per pixel, so a ribbon this thin simply antialiases away
# there; a polygon cannot — it renders as a bright hairline, which is what crossed the road and
# the retaining wall in BeamNG load #18. On the reference city pieces under a centimetre are
# 10.5 % of the triangles and 0.07 % of the plan area (25.7 m2 of 36,130), so dropping them costs
# nothing anybody can see and removes the artifact. `clip_triangle` counts them.
MIN_WIDTH_M = 0.01
# Vertices closer together than this, or off the chord between their neighbours by less than this,
# are the same point as far as the cut is concerned: merging them stops a fan triangulation from
# emitting a degenerate triangle through three near-collinear corners.
WELD_M = 1e-6
# Distance under which a cut vertex is treated as lying ON the boundary. The constraint values are
# metres (heights and plan distances alike), and the grids involved are 1.75-3.5 m, so this is far
# below anything geometric and far above float64 noise.
EPS = 1e-9


def split_convex(poly, a, b, c):
    """Split a convex polygon by the half-plane `a·x + b·z + c >= 0`.

    `poly` is a list of `(x, z)` tuples wound either way. Returns `(inside, outside)`, each a list
    of `(x, z)` or `None`. Vertices on the line join both parts, so the two pieces share their cut
    edge exactly.
    """
    if not poly:
        return None, None
    n = len(poly)
    d = [a * p[0] + b * p[1] + c for p in poly]
    if all(v >= -EPS for v in d):
        return poly, None
    if all(v <= EPS for v in d):
        return None, poly
    inside, outside = [], []
    for i in range(n):
        j = (i + 1) % n
        di, dj = d[i], d[j]
        pi, pj = poly[i], poly[j]
        if di >= -EPS:
            inside.append(pi)
        if di <= EPS:
            outside.append(pi)
        if (di > EPS and dj < -EPS) or (di < -EPS and dj > EPS):
            t = di / (di - dj)
            cut = (pi[0] + t * (pj[0] - pi[0]), pi[1] + t * (pj[1] - pi[1]))
            inside.append(cut)
            outside.append(cut)
    return (inside if len(inside) >= 3 else None), (outside if len(outside) >= 3 else None)


def subtract_convex(poly, halfplanes):
    """`poly` minus the convex region where every half-plane of `halfplanes` holds.

    Returns a list of convex pieces that tile the kept part. Each half-plane is `(a, b, c)` meaning
    `a·x + b·z + c >= 0` INSIDE the region to remove. The piece still inside every half-plane after
    the walk is the removed part and is dropped.
    """
    kept = []
    current = poly
    for a, b, c in halfplanes:
        if current is None:
            break
        current, outside = split_convex(current, a, b, c)
        if outside is not None:
            kept.append(outside)
    return kept


def polygon_area(poly):
    """Twice-signed area / 2 of a polygon in the plan (x, z)."""
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, z1 = poly[i]
        x2, z2 = poly[(i + 1) % n]
        s += x1 * z2 - x2 * z1
    return 0.5 * s


# Below this the three points are collinear in plan — the plane through them is vertical and
# `y = p·x + q·z + r` cannot express it. The direct solve divides by exactly this quantity, so it
# hands those cases back to `lstsq`, which answers with the minimum-norm fit it always did.
PLAN_DEGENERATE = 1e-12


def plane_through(points):
    """Affine `(p, q, r)` with `y ≈ p·x + q·z + r` fitted through `(x, y, z)` points.

    Exact for three points (the terrain triangle's own plane) and least-squares for four (the
    station quad's lane surface, whose corners are near-coplanar but need not be exactly so).

    **Three points take the closed form.** `clip_triangle` and `band_halfplanes` call this 1.72
    million times on a full-city cut, and a 3×3 system through `numpy.linalg.lstsq` — an SVD, two
    array builds and a `numpy.ones` per call — was 71 s of the product's 128 s. Three points
    determine the plane exactly, so there is nothing for a least-squares solver to do: the normal
    is `(p1-p0) × (p2-p0)`, and its Y component is twice the signed plan area, which is the same
    quantity the degenerate case turns on. Four points still need the fit and still get it.
    """
    if len(points) == 3:
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = (
            (float(q[0]), float(q[1]), float(q[2])) for q in points
        )
        ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
        vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz  # = 2 * signed plan area
        nz = ux * vy - uy * vx
        if abs(ny) > PLAN_DEGENERATE:
            p = -nx / ny
            q = -nz / ny
            return p, q, y0 - p * x0 - q * z0
    pts = numpy.asarray(points, dtype=numpy.float64)
    a = numpy.stack((pts[:, 0], pts[:, 2], numpy.ones(len(pts))), axis=-1)
    sol, *_ = numpy.linalg.lstsq(a, pts[:, 1], rcond=None)
    return float(sol[0]), float(sol[1]), float(sol[2])


def edge_halfplane(p0, p1, inside_point):
    """The half-plane of the line `p0→p1` that contains `inside_point`, as `(a, b, c)`."""
    a = -(p1[1] - p0[1])
    b = p1[0] - p0[0]
    c = -(a * p0[0] + b * p0[1])
    if a * inside_point[0] + b * inside_point[1] + c < 0.0:
        a, b, c = -a, -b, -c
    norm = max((a * a + b * b) ** 0.5, 1e-12)
    return a / norm, b / norm, c / norm


def band_halfplanes(tri_plane, quad_tri, bottom, top):
    """The five half-planes whose intersection is the part of a terrain triangle this prism
    sub-triangle discards.

    `tri_plane` is the terrain triangle's `(p, q, r)`; `quad_tri` three `(x, y, z)` corners of half
    a station quad, `y` being the lane surface the band is measured from; `bottom`/`top` the band
    offsets (already carrying `m_ClipHeightOffset`, the `ElevationLimit·3` raise and the ±0.3 m
    expansion — see §46.1 steps 3 and 4).

    Returns `[(a, b, c), …]` for `a·x + b·z + c >= 0` inside the discarded region, or None when the
    sub-triangle is degenerate in plan.
    """
    p0, p1, p2 = ((q[0], q[2]) for q in quad_tri)
    centre = ((p0[0] + p1[0] + p2[0]) / 3.0, (p0[1] + p1[1] + p2[1]) / 3.0)
    area = polygon_area([p0, p1, p2])
    if abs(area) < 1e-12:
        return None
    out = [
        edge_halfplane(p0, p1, centre),
        edge_halfplane(p1, p2, centre),
        edge_halfplane(p2, p0, centre),
    ]
    lane = plane_through(quad_tri)
    tp, tq, tr = tri_plane
    lp, lq, lr = lane
    # h(x,z) - (lane(x,z) + bottom) >= 0  — the ground is at or above the band's floor
    out.append((tp - lp, tq - lq, tr - lr - bottom))
    # (lane(x,z) + top) - h(x,z) >= 0     — and at or below its roof
    out.append((lp - tp, lq - tq, lr + top - tr))
    return out


def clean_polygon(poly, tol=WELD_M):
    """Merge coincident vertices and drop ones that lie on the chord between their neighbours.

    A convex subtraction naturally produces both — a cut that grazes a corner leaves a duplicate,
    and a cut along an existing edge leaves a collinear vertex. Either makes `fan` emit a
    degenerate triangle. Returns the cleaned ring, or None when fewer than three corners survive.
    """
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    while (
        len(out) > 1 and abs(out[0][0] - out[-1][0]) <= tol and abs(out[0][1] - out[-1][1]) <= tol
    ):
        out.pop()
    if len(out) < 3:
        return None
    keep = []
    n = len(out)
    for i in range(n):
        a, b, c = out[(i - 1) % n], out[i], out[(i + 1) % n]
        ax, az = c[0] - a[0], c[1] - a[1]
        span = (ax * ax + az * az) ** 0.5
        if span <= tol:
            continue
        # distance of b from the line a->c
        if abs((ax * (b[1] - a[1]) - az * (b[0] - a[0])) / span) > tol:
            keep.append(b)
    return keep if len(keep) >= 3 else None


def piece_width(poly):
    """The polygon's smallest altitude in plan — `2 · area / longest edge`. A ribbon's width."""
    n = len(poly)
    longest = 0.0
    for i in range(n):
        x1, z1 = poly[i]
        x2, z2 = poly[(i + 1) % n]
        longest = max(longest, ((x2 - x1) ** 2 + (z2 - z1) ** 2) ** 0.5)
    if longest <= 0.0:
        return 0.0
    return 2.0 * abs(polygon_area(poly)) / longest


def clip_triangle(tri, prisms, stats=None):
    """Cut one terrain triangle against every prism sub-triangle that reaches it.

    `tri` is three `(x, y, z)` corners (y up, the heightfield's own samples). `prisms` is an
    iterable of `(quad_tri, bottom, top)` as `band_halfplanes` takes them. Returns a list of kept
    polygons, each a list of `(x, y, z)` with `y` read off the triangle's own plane — so every
    output vertex lies exactly on the input surface.

    Pieces narrower than `MIN_WIDTH_M` in plan are dropped (see the constant); pass a dict as
    `stats` to accumulate `slivers` and `sliverArea` for the caller's report.
    """
    tri_plane = plane_through(tri)
    pieces = [[(p[0], p[2]) for p in tri]]
    for quad_tri, bottom, top in prisms:
        if not pieces:
            break
        planes = band_halfplanes(tri_plane, quad_tri, bottom, top)
        if planes is None:
            continue
        nxt = []
        for poly in pieces:
            nxt.extend(subtract_convex(poly, planes))
        cleaned = []
        for p in nxt:
            p = clean_polygon(p)
            if p is not None and abs(polygon_area(p)) > MIN_AREA_M2:
                cleaned.append(p)
        pieces = cleaned
    kept = []
    for poly in pieces:
        if piece_width(poly) < MIN_WIDTH_M:
            if stats is not None:
                stats["slivers"] = stats.get("slivers", 0) + 1
                stats["sliverArea"] = stats.get("sliverArea", 0.0) + abs(polygon_area(poly))
            continue
        kept.append(poly)
    tp, tq, tr = tri_plane
    return [[(x, tp * x + tq * z + tr, z) for x, z in poly] for poly in kept]


def fan(polygon):
    """Triangulate a convex polygon as a fan — `[(i0, i1, i2), …]` over its own vertex order."""
    return [(0, i, i + 1) for i in range(1, len(polygon) - 1)]


# -- the prisms themselves -----------------------------------------------------------------------


def load_prisms(bundle_dir, cell, deep_only=True):
    """Every clip volume of `clipvolumes.jsonl` as prism sub-triangles — `[(three (x, y, z) lane
    corners, bottom, top), …]` — plus how many volumes were skipped.

    This is the ONE place a consumer turns the §8c layer into geometry, so the Blender harness and
    the heightfield bake cut against exactly the same prisms. `y` on each corner is the lane
    surface the band is measured from, so `bottom`/`top` stay the offsets the game applies
    (`m_ClipHeightOffset`, the `ElevationLimit·3` raise and the ±0.3 m expansion are already in
    them — audit §46.1).

    A strip's swept quad is split into two triangles per station: a station quad on a sharp curve
    need not be convex in plan, and the cut wants convex pieces.

    `deep_only` keeps the documented deviation — the shallow ±0.3 m shell over a surface road is
    not cut, because the ground there is under the carriageway that hides it and removing it would
    rely on our road meshes covering every square metre the prism does (audit §41.7).
    """
    import math

    from . import bezier, reader, terrain

    path = reader.layer_path(bundle_dir, "clipVolumes")
    if not os.path.isfile(path):
        raise RuntimeError(
            "CONTRACT VIOLATION: terrain layer present but no clipvolumes.jsonl — "
            "the producer always writes the §8c clip layer; re-rip the bundle"
        )
    prisms = []
    skipped = 0
    for record in reader.read_jsonl(path):
        bottom = float(record["bottom"])
        top = float(record["top"]) if record.get("top") is not None else float("inf")
        if deep_only and not (top > terrain.CLIP_DEEP_BAND_TOP):
            skipped += 1
            continue
        if record.get("kind") == "area":
            nodes = record.get("nodes") or []
            for tri in record.get("triangles") or []:
                if any(not 0 <= i < len(nodes) for i in tri):
                    continue
                pts = [tuple(float(v) for v in nodes[i]) for i in tri]
                # the producer's own convention for an area: the lane reference is the corner mean
                lane = sum(p[1] for p in pts) / 3.0
                prisms.append(([(p[0], lane, p[2]) for p in pts], bottom, top))
            continue
        left, right = record.get("left"), record.get("right")
        if not left or not right:
            continue
        span = float(
            numpy.linalg.norm(
                numpy.diff(numpy.asarray(left, dtype=numpy.float64), axis=0), axis=1
            ).sum()
        )
        steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))
        t = numpy.linspace(0.0, 1.0, steps + 1)
        lp, rp = bezier.evaluate(left, t), bezier.evaluate(right, t)
        for i in range(1, steps + 1):
            l0, l1 = tuple(lp[i - 1]), tuple(lp[i])
            r0, r1 = tuple(rp[i - 1]), tuple(rp[i])
            prisms.append(([l0, r0, r1], bottom, top))
            prisms.append(([l0, r1, l1], bottom, top))
    return prisms, skipped


def index_prisms(prisms, cell, origin_x, origin_z, width, height, row0=0, col0=0):
    """`{(face row, face col): [prism index, …]}` over the heightfield's FACE grid — which squares
    each prism sub-triangle can reach, by its plan bounding box. Conservative: a prism listed
    against a square it turns out to miss simply removes nothing there. Rows/cols are absolute
    unless `row0`/`col0` shift them into a window's own indices."""
    import math
    from collections import defaultdict

    by_face = defaultdict(list)
    for index, (tri, _bottom, _top) in enumerate(prisms):
        xs = [p[0] for p in tri]
        zs = [p[2] for p in tri]
        c0 = int(math.floor((min(xs) - origin_x) / cell)) - col0
        c1 = int(math.floor((max(xs) - origin_x) / cell)) - col0
        r0 = int(math.floor((origin_z - max(zs)) / cell)) - row0
        r1 = int(math.floor((origin_z - min(zs)) / cell)) - row0
        for r in range(max(0, r0), min(height - 2, r1) + 1):
            for c in range(max(0, c0), min(width - 2, c1) + 1):
                by_face[(r, c)].append(index)
    return by_face
