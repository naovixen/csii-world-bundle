"""Networks (§5) — and the fallback for the records that ship without a mesh.

A network record carries geometry in exactly ONE of two forms, and §5 is explicit that a
consumer must implement both:

* **Mesh form** — the producer assembled real geometry for the segment and it is placed like any
  other instance, from `networkMeshes/`. This is the overwhelming majority (7,289 of 8,259 on
  the reference city) and it is what you want: the game's own composition mesh, with its kerbs,
  crown, markings-ready surface and node fans.
* **Centerline form** — a polyline, a total width, and nothing else. The producer emits this
  when it could not build a mesh. There is no surface detail to recover, so the honest
  reconstruction is a flat ribbon of the stated width, which is what `ribbon` builds.

**Do not treat the centerline form as the general case.** A ribbon is a stand-in, not a road: it
has no crown, no kerb, no thickness, and draping markings onto one gives a different answer from
draping them onto the real carriageway. A consumer should place the mesh wherever there is one
and only fall back to the ribbon where there is not — and should count how often it fell back,
because that count is a fidelity measure.

**The mitre is the part worth getting right.** `width` is the TOTAL width, extruded symmetrically
about the line, and the offset is taken in the HORIZONTAL plane only — CS2 does not bank roads,
and a horizontal offset keeps a steep ramp the right width in plan. At a bend the offset is
pushed out by `1/cos(θ/2)` so the ribbon keeps its full width ACROSS the turn. Averaging the two
segment normals instead is the easy version and pinches the ribbon to `width·cos(θ/2)` — visibly
wrong exactly where roads matter, at the junction ends the centerlines run into.

Everything here is bundle-frame in and bundle-frame out (X east, Y up, Z north); convert with
`csiibundle.frames` at the boundary.
"""

import numpy

# Cap on the mitre push-out. An exact mitre goes to infinity as a bend approaches a hairpin, so
# something has to bound it; 4 lets a 30° turn mitre exactly and clamps anything tighter. TUNED.
DEFAULT_MITRE_LIMIT = 4.0

# Contract §4's closed `kind` set, and §5's drivable categories. Listed rather than derived, so a
# v2 addition shows up here as a deliberate edit instead of silently joining a wildcard.
KINDS = ("building", "prop", "tree", "fence", "decal", "poi")
DRIVABLE = ("road", "highway", "train", "tram", "subway", "pathway", "taxiway", "runway")


def segment_normals(points):
    """The left-hand HORIZONTAL unit normal of each segment of a polyline.

    `points` is `(N, 3)` in the bundle frame; the result is `(N-1, 3)` with a zero up-component.
    A zero-length segment would give a NaN normal, so it falls back to +X — callers are expected
    to have removed duplicate points first (`clean_polyline`), and this is the second line of
    defence rather than the first.
    """
    points = numpy.asarray(points, dtype=numpy.float64)
    delta = points[1:] - points[:-1]
    flat = numpy.stack((delta[:, 0], delta[:, 2]), axis=1)  # (east, north), up dropped
    length = numpy.linalg.norm(flat, axis=1, keepdims=True)
    degenerate = length[:, 0] < 1e-6
    flat = numpy.where(
        length < 1e-6, numpy.array([1.0, 0.0]), flat / numpy.where(length < 1e-6, 1.0, length)
    )
    normals = numpy.zeros((len(delta), 3))
    normals[:, 0] = -flat[:, 1]
    normals[:, 2] = flat[:, 0]
    normals[degenerate] = (0.0, 0.0, 1.0)
    return normals


def clean_polyline(points, tolerance=1e-4):
    """`points` with consecutive duplicates dropped — `(M, 3)`, M <= N.

    Duplicate points are common in shipped centerlines and every one of them is a zero-length
    direction and a NaN normal downstream, so this runs before anything else looks at the line.
    """
    points = numpy.asarray(points, dtype=numpy.float64).reshape(-1, 3)
    if len(points) < 2:
        return points
    keep = [0]
    for i in range(1, len(points)):
        if numpy.linalg.norm(points[i] - points[keep[-1]]) > tolerance:
            keep.append(i)
    return points[keep]


def ribbon(centerline, width, lift=0.0, mitre_limit=DEFAULT_MITRE_LIMIT):
    """Extrude a centerline into a flat, properly mitred ribbon. Bundle frame.

    Returns `(verts, quads)` — `verts` `(2M, 3)` as `[left0, right0, left1, right1, …]` and
    `quads` `(M-1, 4)` indices — or `None` when the line collapses to fewer than two distinct
    points and there is nothing to extrude.

    `width` is the TOTAL width (§5), `lift` is added to the up axis, and `mitre_limit` caps the
    push-out at a hairpin. See the module docstring for why the mitre is not optional.
    """
    points = clean_polyline(centerline)
    if len(points) < 2:
        return None

    half = width / 2.0
    normals = segment_normals(points)

    # Each vertex mitres between the segment before it and the segment after it; the ends
    # duplicate their only neighbour, which butts them square.
    before = normals[numpy.maximum(numpy.arange(len(points)) - 1, 0)]
    after = normals[numpy.minimum(numpy.arange(len(points)), len(normals) - 1)]

    bisector = before + after
    blen = numpy.linalg.norm(bisector, axis=1, keepdims=True)
    reversed_ = blen[:, 0] < 1e-6  # a perfect reversal has no sane mitre
    unit = numpy.where(blen < 1e-6, after, bisector / numpy.where(blen < 1e-6, 1.0, blen))
    # 1/cos(θ/2), where cos(θ/2) is the bisector's projection onto either segment normal.
    scale = numpy.minimum(1.0 / numpy.maximum((unit * after).sum(axis=1), 1e-3), mitre_limit)
    offset = unit * (half * scale)[:, None]
    offset[reversed_] = after[reversed_] * half  # butt it

    lifted = points.copy()
    lifted[:, 1] += lift
    verts = numpy.empty((2 * len(points), 3), dtype=numpy.float64)
    verts[0::2] = lifted - offset
    verts[1::2] = lifted + offset

    a = numpy.arange(len(points) - 1) * 2
    quads = numpy.stack((a, a + 1, a + 3, a + 2), axis=1)
    return verts, quads
