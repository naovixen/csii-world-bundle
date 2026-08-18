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

import math
import os

import numpy

from . import png, reader

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


class ProjectionSurface:
    """The interface `drape_block` and the area conformance need from a surface.

    Implement both methods over whatever spatial structure you already have. All coordinates are
    Z-up (X east, Y north, Z up) and all heights are world metres.

    Subclassing is optional — anything with these two methods will do.
    """

    def drop(self, x, y, z, half_up, half_down):
        """The surface height nearest `z` under `(x, y)`, or `None` if nothing is in reach.

        Only surfaces within `[z - half_down, z + half_up]` count, and among those the one
        NEAREST `z` wins — not the highest. See the module docstring for why that distinction
        is load-bearing. The window comes from the projector's own bounds, not from a constant.
        """
        raise NotImplementedError

    def height(self, x, y, ceiling=4000.0):
        """The topmost surface under `(x, y)` seen from `ceiling` metres up, or `None`.

        The plain "what is the ground here" query, for a layer with no expected height.
        """
        raise NotImplementedError


class HeightfieldSurface(ProjectionSurface):
    """A `ProjectionSurface` over the §8 heightmap, in numpy — no scene graph required.

    This is the fallback for a consumer that has no ray-caster yet, and the reference for what
    "the ground" means. It is the TERRAIN only: it knows nothing about roads, bridges or
    buildings, so a marking on an elevated deck draped against this lands on the ground under
    the deck. Use it for the area layer and for the terrain fallback in `drape_block`, and give
    the road layer a surface built from the real network meshes.

    **The diagonal is a choice, and it must match how you mesh the grid.** Contract §3 puts a
    lattice vertex at each cell centre, so four vertices bound a quad and a quad has to be split
    into triangles somehow. Interpolating bilinearly instead of picking a diagonal is not a
    neutral simplification — it answers a different question from the one the rendered mesh
    answers, by a measured p95 of 9.4 cm at 3.5 m cells and 41 cm at a 14 m stride. `diagonal`
    selects which split this assumes: `"nw_se"` (the default) splits each quad from its
    north-west corner to its south-east one.

    Queries accept scalars or arrays; array queries are vectorised and are how a consumer should
    drape a whole layer.
    """

    def __init__(self, heights, cell, origin_x, origin_y, diagonal="nw_se"):
        """`heights` is `(rows, cols)` in world metres, row 0 NORTH (§3, the raster's own order).

        `(origin_x, origin_y)` is the world position of vertex `(0, 0)` — the top-left texel
        CENTRE, which is what the manifest's `originWorld` gives. Y decreases as the row index
        rises, because rows run north to south.
        """
        self.heights = numpy.asarray(heights, dtype=numpy.float64)
        if self.heights.ndim != 2:
            raise ValueError(f"heights must be (rows, cols), got {self.heights.shape}")
        self.cell = float(cell)
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        if diagonal not in ("nw_se", "ne_sw"):
            raise ValueError(f"diagonal must be 'nw_se' or 'ne_sw', got {diagonal!r}")
        self.diagonal = diagonal

    @classmethod
    def from_bundle(cls, bundle_dir, diagonal="nw_se", prefer_fine=True):
        """Build one straight from a bundle's §8 terrain layer, or `None` if it has none —
        the fine 1.75 m raster when the bundle carries it (`reader.load_heightfield`), which is
        the ground a heightfield engine will actually be given."""
        field = reader.load_heightfield(bundle_dir, prefer_fine=prefer_fine)
        if field is None:
            return None
        surface = cls(field.raw.astype(numpy.float64) * field.elevation_scale + field.elevation_offset,
                      field.cell, field.origin_x, field.origin_z, diagonal=diagonal)
        surface.fine = field.fine
        return surface

    # -- the queries ---------------------------------------------------------------------------

    def height(self, x, y, ceiling=4000.0):     # noqa: ARG002 - a heightfield has no occluders
        """The ground height at `(x, y)`, or `None` (NaN in an array) outside the raster.

        `ceiling` is accepted for protocol compatibility and ignored: a heightfield is a single
        surface, so there is never more than one hit to choose between.
        """
        if isinstance(x, (float, int)) and isinstance(y, (float, int)):
            return self._sample_one(x, y)           # the scalar path — see `_sample_one`
        scalar = numpy.isscalar(x) and numpy.isscalar(y)
        out = self._sample(numpy.atleast_1d(numpy.asarray(x, dtype=numpy.float64)),
                           numpy.atleast_1d(numpy.asarray(y, dtype=numpy.float64)))
        if not scalar:
            return out
        value = float(out[0])
        return None if numpy.isnan(value) else value

    def drop(self, x, y, z, half_up, half_down):
        """The ground height at `(x, y)` when it lies within the window around `z`, else `None`.

        With one surface there is nothing to be nearest TO, so this reduces to a window test —
        which is exactly what the general rule reduces to in the single-surface case.
        """
        top = self.height(x, y)
        if top is None:
            return None
        return top if (z - half_down) <= top <= (z + half_up) else None

    def _sample(self, x, y):
        """Vectorised planar sample. `(N,)` in, `(N,)` out, NaN outside the raster."""
        rows, cols = self.heights.shape
        # Continuous grid coordinates: column rises with X, row rises as Y falls (row 0 north).
        fc = (x - self.origin_x) / self.cell
        fr = (self.origin_y - y) / self.cell

        c0 = numpy.floor(fc).astype(numpy.int64)
        r0 = numpy.floor(fr).astype(numpy.int64)
        inside = (c0 >= 0) & (r0 >= 0) & (c0 < cols - 1) & (r0 < rows - 1)
        # A query exactly on the last row/column belongs to the last cell, not to none.
        on_edge = (~inside) & (c0 >= 0) & (r0 >= 0) & (c0 <= cols - 1) & (r0 <= rows - 1)
        c0 = numpy.clip(c0, 0, max(cols - 2, 0))
        r0 = numpy.clip(r0, 0, max(rows - 2, 0))
        u = numpy.clip(fc - c0, 0.0, 1.0)        # 0 at the west edge of the cell
        v = numpy.clip(fr - r0, 0.0, 1.0)        # 0 at the north edge

        nw = self.heights[r0, c0]
        ne = self.heights[r0, c0 + 1]
        sw = self.heights[r0 + 1, c0]
        se = self.heights[r0 + 1, c0 + 1]

        if self.diagonal == "nw_se":
            # Triangles (nw, ne, se) for u >= v, and (nw, se, sw) for u < v.
            upper = u >= v
            out = numpy.where(upper,
                              nw + (ne - nw) * u + (se - ne) * v,
                              nw + (se - sw) * u + (sw - nw) * v)
        else:
            # Diagonal ne-sw: triangles (nw, ne, sw) for u + v <= 1, else (ne, se, sw).
            lower = (u + v) <= 1.0
            out = numpy.where(lower,
                              nw + (ne - nw) * u + (sw - nw) * v,
                              se + (sw - se) * (1.0 - u) + (ne - se) * (1.0 - v))

        return numpy.where(inside | on_edge, out, numpy.nan)

    def _sample_one(self, x, y):
        """`_sample` for ONE point, in plain Python floats — bit-identical, no allocation.

        The area conform (`areas.conform_triangle`) asks for one point at a time, tens of
        millions of times: three edge midpoints and a centroid per split, per split, per source
        triangle. Through the vectorised path each of those allocated two 1-element arrays, ran
        the full `_sample` (twice over, once per diagonal of an `envelope` ground), and stacked
        and `nanmax`-ed the pair inside an `errstate` context — about 50 µs of overhead for
        4 array elements of work, which was 60 % of the whole bake.

        This is the SAME arithmetic in the same order on the same float64 values, so it must
        agree to the last bit; `tools/test_ground_scalar.py` asserts exactly that over random
        points on both diagonals and is the gate on any edit here. Keep the two in step: a
        divergence here silently moves every conformed height in the level.
        """
        heights = self.heights
        rows, cols = heights.shape
        fc = (float(x) - self.origin_x) / self.cell
        fr = (self.origin_y - float(y)) / self.cell
        try:
            c0 = math.floor(fc)
            r0 = math.floor(fr)
        except (ValueError, OverflowError):     # NaN or infinity: the array path answers NaN
            return None
        # `inside | on_edge` in one test: a query exactly on the last row/column belongs to the
        # last cell, and anything else off the raster has no ground.
        if c0 < 0 or r0 < 0 or c0 > cols - 1 or r0 > rows - 1:
            return None
        if c0 > cols - 2:
            c0 = max(cols - 2, 0)
        if r0 > rows - 2:
            r0 = max(rows - 2, 0)
        u = fc - c0
        v = fr - r0
        u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)      # 0 at the west edge of the cell
        v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)      # 0 at the north edge

        nw = heights.item(r0, c0)
        ne = heights.item(r0, c0 + 1)
        sw = heights.item(r0 + 1, c0)
        se = heights.item(r0 + 1, c0 + 1)

        if self.diagonal == "nw_se":
            if u >= v:
                return nw + (ne - nw) * u + (se - ne) * v
            return nw + (se - sw) * u + (sw - nw) * v
        if u + v <= 1.0:
            return nw + (ne - nw) * u + (sw - nw) * v
        return se + (sw - se) * (1.0 - u) + (ne - se) * (1.0 - v)


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
    keys = numpy.stack((numpy.round(x * scale).astype(numpy.int64),
                        numpy.round(z * scale).astype(numpy.int64)), axis=1)
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


# -- bulk queries on the heightfield -----------------------------------------------------------

def _heightfield_drop_many(self, x, y, z, half_up, half_down):
    """Vectorised `drop`: `(N,)` arrays in, `(N,)` heights out with NaN where nothing is in the
    window. Same rule as the scalar form: one surface, so a window test."""
    top = self._sample(numpy.asarray(x, dtype=numpy.float64), numpy.asarray(y, dtype=numpy.float64))
    z = numpy.asarray(z, dtype=numpy.float64)
    ok = (~numpy.isnan(top)) & (top >= z - half_down) & (top <= z + half_up)
    return numpy.where(ok, top, numpy.nan)


def _heightfield_height_many(self, x, y, ceiling=4000.0):   # noqa: ARG001 - single surface
    return self._sample(numpy.asarray(x, dtype=numpy.float64), numpy.asarray(y, dtype=numpy.float64))


HeightfieldSurface.drop_many = _heightfield_drop_many
HeightfieldSurface.height_many = _heightfield_height_many


# -- a triangle soup as a projection surface ---------------------------------------------------

class TriangleSoupSurface(ProjectionSurface):
    """A `ProjectionSurface` over an arbitrary triangle soup — the network meshes — in numpy.

    This is what a consumer with no scene graph uses as the ROAD surface: every `networks/*.glb`
    placed into Z-up world space and concatenated. Vertical rays only, which is exactly what the
    protocol asks (both queries are "the surface under (x, y)"), so the intersection reduces to a
    2-D point-in-triangle test plus a plane height — no BVH, no ray direction.

    **Index.** A uniform 2-D grid of `cell` metres over the soup's XY bounds; each triangle is
    listed in every cell its bounding box touches; stored CSR-style (`_cell_start`, `_cell_tris`).
    A query finds its cell, tests that cell's triangles vectorised, and picks by the protocol's
    rule (nearest-to-expected inside the window for `drop`, topmost for `height`). Queries are
    processed grouped by cell so a million of them cost a few thousand small numpy calls, not a
    million Python iterations. `cell` is TUNED (8 m: road triangles are 1–4 m, so a cell holds a
    few dozen); a denser city can pass a smaller one.

    Vertices are kept as float32 (19M of them for a full city ≈ 230 MB); the per-cell maths runs
    in float64. Edge-on (vertical) triangles are skipped by the determinant test — a vertical ray
    cannot hit them either. A vertex shared by two triangles yields two equal hits, harmless.
    """

    def __init__(self, verts_zup, tris, cell=8.0):
        verts = numpy.asarray(verts_zup, dtype=numpy.float32)
        tris = numpy.asarray(tris, dtype=numpy.int64)
        if verts.ndim != 2 or verts.shape[1] != 3:
            raise ValueError(f"verts must be (N, 3), got {verts.shape}")
        if tris.ndim != 2 or tris.shape[1] != 3:
            raise ValueError(f"tris must be (M, 3), got {tris.shape}")
        self.verts = verts
        self.tris = tris
        self.cell = float(cell)
        self.count = len(tris)
        if self.count == 0:
            self.origin = numpy.zeros(2)
            self.ncols = self.nrows = 0
            self._cell_start = numpy.zeros(1, numpy.int64)
            self._cell_tris = numpy.zeros(0, numpy.int64)
            return
        p0, p1, p2 = (verts[tris[:, k], :2].astype(numpy.float64) for k in range(3))
        lo = numpy.minimum(numpy.minimum(p0, p1), p2)
        hi = numpy.maximum(numpy.maximum(p0, p1), p2)
        self.origin = lo.min(axis=0)
        extent = hi.max(axis=0) - self.origin
        self.ncols = int(numpy.floor(extent[0] / self.cell)) + 1
        self.nrows = int(numpy.floor(extent[1] / self.cell)) + 1
        c0 = numpy.floor((lo[:, 0] - self.origin[0]) / self.cell).astype(numpy.int64)
        c1 = numpy.floor((hi[:, 0] - self.origin[0]) / self.cell).astype(numpy.int64)
        r0 = numpy.floor((lo[:, 1] - self.origin[1]) / self.cell).astype(numpy.int64)
        r1 = numpy.floor((hi[:, 1] - self.origin[1]) / self.cell).astype(numpy.int64)
        spans = (c1 - c0 + 1) * (r1 - r0 + 1)
        total = int(spans.sum())
        # Expand (triangle -> every cell its bbox touches) without a Python loop over triangles.
        tri_ids = numpy.repeat(numpy.arange(self.count), spans)
        offsets = numpy.arange(total) - numpy.repeat(numpy.cumsum(spans) - spans, spans)
        width = numpy.repeat(c1 - c0 + 1, spans)
        cols = numpy.repeat(c0, spans) + offsets % width
        rows = numpy.repeat(r0, spans) + offsets // width
        cell_ids = rows * self.ncols + cols
        order = numpy.argsort(cell_ids, kind="stable")
        self._cell_tris = tri_ids[order]
        counts = numpy.bincount(cell_ids, minlength=self.ncols * self.nrows)
        self._cell_start = numpy.concatenate([[0], numpy.cumsum(counts)]).astype(numpy.int64)

    @classmethod
    def from_pieces(cls, pieces, cell=8.0):
        """Concatenate `[(verts_zup, tris), ...]` into one surface (offsets handled)."""
        verts, tris, base = [], [], 0
        for v, t in pieces:
            v = numpy.asarray(v, dtype=numpy.float32)
            t = numpy.asarray(t, dtype=numpy.int64)
            if len(v) == 0 or len(t) == 0:
                continue
            verts.append(v)
            tris.append(t + base)
            base += len(v)
        if not verts:
            return cls(numpy.zeros((0, 3), numpy.float32), numpy.zeros((0, 3), numpy.int64), cell)
        return cls(numpy.concatenate(verts), numpy.concatenate(tris), cell)

    # -- protocol ------------------------------------------------------------------------------

    def drop(self, x, y, z, half_up, half_down):
        out = self.drop_many(numpy.array([x], numpy.float64), numpy.array([y], numpy.float64),
                             numpy.array([z], numpy.float64), half_up, half_down)
        return None if numpy.isnan(out[0]) else float(out[0])

    def height(self, x, y, ceiling=4000.0):
        out = self.height_many(numpy.array([x], numpy.float64), numpy.array([y], numpy.float64), ceiling)
        return None if numpy.isnan(out[0]) else float(out[0])

    def drop_many(self, x, y, z, half_up, half_down):
        """`(N,)` heights, NaN where no surface lies within `[z - half_down, z + half_up]`;
        among those that do, the one NEAREST `z`."""
        return self._query(x, y, z, "drop", half_up, half_down)

    def height_many(self, x, y, ceiling=4000.0):
        """`(N,)` topmost heights at or below `ceiling`, NaN where nothing is under (x, y)."""
        return self._query(x, y, None, "height", ceiling, None)

    # -- internals -----------------------------------------------------------------------------

    def _query(self, x, y, z, mode, a, b):
        x = numpy.asarray(x, dtype=numpy.float64).ravel()
        y = numpy.asarray(y, dtype=numpy.float64).ravel()
        n = len(x)
        out = numpy.full(n, numpy.nan)
        if n == 0 or self.count == 0:
            return out
        z = None if z is None else numpy.asarray(z, dtype=numpy.float64).ravel()
        cols = numpy.floor((x - self.origin[0]) / self.cell).astype(numpy.int64)
        rows = numpy.floor((y - self.origin[1]) / self.cell).astype(numpy.int64)
        inside = (cols >= 0) & (rows >= 0) & (cols < self.ncols) & (rows < self.nrows)
        if not inside.any():
            return out
        cell_ids = numpy.where(inside, rows * self.ncols + cols, -1)
        order = numpy.argsort(cell_ids, kind="stable")
        sorted_ids = cell_ids[order]
        first_valid = numpy.searchsorted(sorted_ids, 0)
        order = order[first_valid:]
        sorted_ids = sorted_ids[first_valid:]
        boundaries = numpy.flatnonzero(numpy.diff(sorted_ids)) + 1
        starts = numpy.concatenate([[0], boundaries])
        ends = numpy.concatenate([boundaries, [len(sorted_ids)]])
        verts = self.verts
        tris = self.tris
        for s, e in zip(starts, ends):
            cid = int(sorted_ids[s])
            t0, t1 = self._cell_start[cid], self._cell_start[cid + 1]
            if t1 <= t0:
                continue
            q = order[s:e]
            cand = self._cell_tris[t0:t1]
            tri = tris[cand]
            a0 = verts[tri[:, 0]].astype(numpy.float64)
            a1 = verts[tri[:, 1]].astype(numpy.float64)
            a2 = verts[tri[:, 2]].astype(numpy.float64)
            # Barycentric coordinates of every query against every candidate triangle (Q x T).
            qx = x[q][:, None]
            qy = y[q][:, None]
            d00x, d00y = (a1[:, 0] - a0[:, 0])[None, :], (a1[:, 1] - a0[:, 1])[None, :]
            d01x, d01y = (a2[:, 0] - a0[:, 0])[None, :], (a2[:, 1] - a0[:, 1])[None, :]
            det = d00x * d01y - d01x * d00y
            px, py = qx - a0[:, 0][None, :], qy - a0[:, 1][None, :]
            safe = numpy.where(numpy.abs(det) > 1e-12, det, 1.0)
            w1 = (px * d01y - d01x * py) / safe
            w2 = (d00x * py - px * d00y) / safe
            w0 = 1.0 - w1 - w2
            eps = -1e-9
            hit = (numpy.abs(det) > 1e-12) & (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
            if not hit.any():
                continue
            h = w0 * a0[:, 2][None, :] + w1 * a1[:, 2][None, :] + w2 * a2[:, 2][None, :]
            if mode == "height":
                h = numpy.where(hit & (h <= a), h, -numpy.inf)
                best = h.max(axis=1)
                got = numpy.isfinite(best)
                out[q[got]] = best[got]
            else:
                zq = z[q][:, None]
                within = hit & (h >= zq - b) & (h <= zq + a)
                dist = numpy.where(within, numpy.abs(h - zq), numpy.inf)
                k = dist.argmin(axis=1)
                got = numpy.isfinite(dist[numpy.arange(len(q)), k])
                out[q[got]] = h[numpy.arange(len(q)), k][got]
        return out


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
    ys = numpy.concatenate([numpy.asarray(b["verts"][:, 1], numpy.float64) for b in live])   # expected (up)
    zs = numpy.concatenate([numpy.asarray(b["verts"][:, 2], numpy.float64) for b in live])
    halves = numpy.concatenate([numpy.full(len(b["verts"]), float(b["half_height"])) for b in live])
    epss = numpy.concatenate([numpy.full(len(b["verts"]), float(b["drape_eps"])) for b in live])
    scale = 1000.0 / DEDUP_MM
    keys = numpy.stack((numpy.round(xs * scale), numpy.round(zs * scale), numpy.round(ys * scale),
                        numpy.round(halves * scale)), axis=1).astype(numpy.int64)
    _, first, inverse = numpy.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    counters["queries"] += int(len(first))
    # Bundle (x, y_up, z_north) -> Z-up (x, y_north, z_up).
    qx, qy, qz, qh = xs[first], zs[first], ys[first], halves[first]
    top = _bulk_drop(road_surface, qx, qy, qz, qh)
    status = numpy.where(numpy.isnan(top), 0, 1)                 # 1 = draped, 2 = grounded
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
        b["verts"][:, 1] = new_y[cursor:cursor + n]
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
