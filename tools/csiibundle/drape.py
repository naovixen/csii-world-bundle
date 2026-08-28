"""Draping — putting a rebuilt projection back onto the surface the game painted it on.

Two layers need this and they need the same thing: lane decals project onto the road (§5B) and
surface areas project onto the terrain (§8b). In the game both are projector volumes and neither
has a height of its own; rebuilt as geometry they do, and it has to come from somewhere.

**The frame here is Z-up** — X east, Y north, Z up, i.e. `frames.bundle_to_zup_point`'s output —
because a surface is a thing the CONSUMER owns and every consumer that has one is already
working in its own frame. `drape_block` converts from the bundle frame on the way in, so a
caller hands it bundle-frame blocks and a Z-up surface and the two meet correctly.

`ProjectionSurface` is the protocol, and it is deliberately small: two queries, no geometry
access. A consumer with a scene graph implements it over whatever spatial index it already has
(a BVH, a physics collider, a depth pre-pass); a consumer with nothing implements nothing and
uses `HeightfieldSurface`, which ray-casts the §8 heightmap directly in numpy.

The two queries are not the same question and substituting one for the other is a real defect:

* `drop` — the surface **nearest an expected height**, inside a window the caller supplies from
  the projector's OWN reach. This is for a marking, which has a height it should be near. The
  alternative — take the highest hit in the window — snapped ~2.5% of drape samples onto kerb
  tops, median walls and bridge railings, because a kerb 0.4 m above the road is still inside a
  ±0.5 m reach and wins on height alone. Nearest-to-expected cannot make that mistake.
* `height` — the **topmost** surface, from far above. This is for an area, which has no expected
  height, only the ground it is painted on.

Measured and settled, 2026-08-11: enumerating every hit under a sample instead of the topmost
per object changes nothing (24,059 of 24,060 samples have exactly ONE surface beneath them —
segment meshes are single-layer shells), and choosing the highest candidate at or below the lane
curve is twice as bad as the nearest rule. Run `tools/test_drape_pick.py` before re-deriving any
of this from a static reading.
"""

import numpy

from .heightsurface import HeightfieldSurface  # noqa: F401  (re-export: the drape's surfaces)
from .meshsurface import TriangleSoupSurface  # noqa: F401
from .projection import HEIGHT_CEILING, ProjectionSurface  # noqa: F401

# Quantise world (x, y) to this many millimetres before deduping surface queries. Two DIFFERENT
# curve stations that clip to the same arc position, or two literal duplicate source vertices,
# then cost one query — but a station's left and right edge are different x and are never
# merged, which is what keeps this exact rather than an approximation. Deduping on the curve
# STATION instead was a real defect: it drapes a cambered carriageway's two edges to a third
# point's height, neither edge's own. TUNED.
DEDUP_MM = 1.0

# Share of vertices that may find no surface at all before the drape should say so out loud.
# Region-edge markings whose road sits outside the window are the legitimate cause and they are
# a small tail; a big number means the layer is not landing on anything. TUNED.
MISS_WARN_FRACTION = 0.01


def drape_block(block, road_surface, counters, ground_surface=None):
    """Snap one bent block's vertices onto the surface beneath them, in place.

    `block` is a `lanes.bend_part` block: bundle-frame `verts`, plus `half_height` (the
    projector's own reach) and `drape_eps` (the rung of the lift ladder this part rides at).
    `counters` is a dict this adds `queries`, `draped`, `grounded` and `missed` to.

    Each vertex is sampled at its OWN (x, z) — including its lateral offset — not at the curve's
    centreline. That distinction is not a refinement: sampling the centreline per station and
    broadcasting drapes the left and right edge of a cambered carriageway to a THIRD point's
    height, neither edge's own, which is a milder version of the exact error the projector-reach
    window exists to remove.

    `ground_surface`, when given, is tried where the road surface has nothing: the two surfaces
    are disjoint, and a marking that overhangs the carriageway (or whose segment fell outside the
    imported region) otherwise keeps an authored height with nothing beneath it. The terrain is
    the other thing the game paints onto. It is queried at the SAME window, so this can never
    drag a marking down to the ground from a bridge.

    A vertex that finds no surface at all keeps its bent height. Nothing is borrowed from a
    neighbour — a miss is reported, not filled in.
    """
    verts = block["verts"]
    half = block["half_height"]
    eps = block["drape_eps"]

    x, y_pre, z = verts[:, 0], verts[:, 1], verts[:, 2]
    scale = 1000.0 / DEDUP_MM
    keys = numpy.stack(
        (numpy.round(x * scale).astype(numpy.int64), numpy.round(z * scale).astype(numpy.int64)),
        axis=1,
    )
    _, first_index, inverse = numpy.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)

    new_y = y_pre.copy()
    draped = grounded = missed = 0
    for u, idx in enumerate(first_index):
        counters["queries"] += 1
        # Bundle (x, y_up, z_north) -> the surface's Z-up (x, y_north, z_up).
        sx, sy, sz = float(x[idx]), float(z[idx]), float(y_pre[idx])
        top = road_surface.drop(sx, sy, sz, half, half)
        at_key = inverse == u
        if top is None and ground_surface is not None:
            counters["queries"] += 1
            top = ground_surface.drop(sx, sy, sz, half, half)
            if top is not None:
                new_y[at_key] = top + eps
                grounded += int(at_key.sum())
                continue
        if top is None:
            missed += int(at_key.sum())
            continue
        new_y[at_key] = top + eps
        draped += int(at_key.sum())

    verts[:, 1] = new_y
    counters["draped"] += draped
    counters["grounded"] += grounded
    counters["missed"] += missed


# -- bulk draping -------------------------------------------------------------------------------


def drape_blocks(blocks, road_surface, ground_surface=None, counters=None):
    """`drape_block` over many blocks with ONE bulk surface query — the form the bake uses.

    Same rule per vertex as `drape_block` (own (x, z), projector window, nearest-to-expected,
    ground fallback at the same window, a miss keeps its height); differs only in that queries
    are deduplicated across all blocks and issued through `drop_many` when the surface has it.
    The dedup key includes the EXPECTED height as well as (x, z) — two markings stacked on a
    bridge and the road beneath it share (x, z) and must not share an answer.

    Returns the counters dict (`queries`, `draped`, `grounded`, `missed`), also updated in place
    when one is passed.
    """
    counters = counters if counters is not None else {}
    for key in ("queries", "draped", "grounded", "missed"):
        counters.setdefault(key, 0)
    live = [b for b in blocks if len(b["verts"])]
    if not live:
        return counters
    xs = numpy.concatenate([numpy.asarray(b["verts"][:, 0], numpy.float64) for b in live])
    ys = numpy.concatenate(
        [numpy.asarray(b["verts"][:, 1], numpy.float64) for b in live]
    )  # expected (up)
    zs = numpy.concatenate([numpy.asarray(b["verts"][:, 2], numpy.float64) for b in live])
    halves = numpy.concatenate([numpy.full(len(b["verts"]), float(b["half_height"])) for b in live])
    epss = numpy.concatenate([numpy.full(len(b["verts"]), float(b["drape_eps"])) for b in live])
    scale = 1000.0 / DEDUP_MM
    keys = numpy.stack(
        (
            numpy.round(xs * scale),
            numpy.round(zs * scale),
            numpy.round(ys * scale),
            numpy.round(halves * scale),
        ),
        axis=1,
    ).astype(numpy.int64)
    _, first, inverse = numpy.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    counters["queries"] += len(first)
    # Bundle (x, y_up, z_north) -> Z-up (x, y_north, z_up).
    qx, qy, qz, qh = xs[first], zs[first], ys[first], halves[first]
    top = _bulk_drop(road_surface, qx, qy, qz, qh)
    status = numpy.where(numpy.isnan(top), 0, 1)  # 1 = draped, 2 = grounded
    if ground_surface is not None:
        miss = numpy.isnan(top)
        if miss.any():
            counters["queries"] += int(miss.sum())
            g = _bulk_drop(ground_surface, qx[miss], qy[miss], qz[miss], qh[miss])
            got = ~numpy.isnan(g)
            idx = numpy.flatnonzero(miss)[got]
            top[idx] = g[got]
            status[idx] = 2
    per_vertex_top = top[inverse]
    per_vertex_status = status[inverse]
    new_y = numpy.where(numpy.isnan(per_vertex_top), ys, per_vertex_top + epss)
    counters["draped"] += int((per_vertex_status == 1).sum())
    counters["grounded"] += int((per_vertex_status == 2).sum())
    counters["missed"] += int((per_vertex_status == 0).sum())
    cursor = 0
    for b in live:
        n = len(b["verts"])
        b["verts"][:, 1] = new_y[cursor : cursor + n]
        cursor += n
    return counters


def _bulk_drop(surface, x, y, z, half):
    """`drop_many` when the surface has it, else the scalar protocol in a loop; NaN = miss.
    Windows may differ per query (`half` is an array), so a surface with a scalar-window
    `drop_many` is called once per distinct half-height."""
    out = numpy.full(len(x), numpy.nan)
    if len(x) == 0:
        return out
    if hasattr(surface, "drop_many"):
        for h in numpy.unique(half):
            sel = half == h
            out[sel] = surface.drop_many(x[sel], y[sel], z[sel], float(h), float(h))
        return out
    for i in range(len(x)):
        r = surface.drop(float(x[i]), float(y[i]), float(z[i]), float(half[i]), float(half[i]))
        if r is not None:
            out[i] = r
    return out
