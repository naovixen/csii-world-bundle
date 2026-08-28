"""An arbitrary triangle soup as a `ProjectionSurface` — the network meshes, indexed."""

import numpy

from .projection import HEIGHT_CEILING, ProjectionSurface


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
        out = self.drop_many(
            numpy.array([x], numpy.float64),
            numpy.array([y], numpy.float64),
            numpy.array([z], numpy.float64),
            half_up,
            half_down,
        )
        return None if numpy.isnan(out[0]) else float(out[0])

    def height(self, x, y):
        out = self.height_many(numpy.array([x], numpy.float64), numpy.array([y], numpy.float64))
        return None if numpy.isnan(out[0]) else float(out[0])

    def drop_many(self, x, y, z, half_up, half_down):
        """`(N,)` heights, NaN where no surface lies within `[z - half_down, z + half_up]`;
        among those that do, the one NEAREST `z`."""
        return self._query(x, y, z, "drop", half_up, half_down)

    def height_many(self, x, y):
        """`(N,)` topmost heights at or below `HEIGHT_CEILING`, NaN where nothing is under (x, y)."""
        return self._query(x, y, None, "height", HEIGHT_CEILING, None)

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
        for s, e in zip(starts, ends):
            cid = int(sorted_ids[s])
            t0, t1 = self._cell_start[cid], self._cell_start[cid + 1]
            if t1 <= t0:
                continue
            q = order[s:e]
            self._cell_pick(q, self._cell_tris[t0:t1], x, y, z, mode, a, b, out)
        return out

    def _hit_heights(self, q, cand, x, y):
        """`(hit, h)`, both `(Q, T)`: which of one cell's candidate triangles each query point
        lies in (barycentric, 2-D), and the surface height of every (point, triangle) pair."""
        tri = self.tris[cand]
        a0 = self.verts[tri[:, 0]].astype(numpy.float64)
        a1 = self.verts[tri[:, 1]].astype(numpy.float64)
        a2 = self.verts[tri[:, 2]].astype(numpy.float64)
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
        h = w0 * a0[:, 2][None, :] + w1 * a1[:, 2][None, :] + w2 * a2[:, 2][None, :]
        return hit, h

    def _cell_pick(self, q, cand, x, y, z, mode, a, b, out):
        """One cell's queries against its candidate triangles, picked by the protocol's rule
        (topmost for `height`, nearest-to-expected inside the window for `drop`) into `out`."""
        hit, h = self._hit_heights(q, cand, x, y)
        if not hit.any():
            return
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
