"""The §8 heightmap as a `ProjectionSurface`, in numpy — no scene graph required."""

import math

import numpy

from . import reader
from .projection import ProjectionSurface


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
        surface = cls(
            field.raw.astype(numpy.float64) * field.elevation_scale + field.elevation_offset,
            field.cell,
            field.origin_x,
            field.origin_z,
            diagonal=diagonal,
        )
        surface.fine = field.fine
        return surface

    # -- the queries ---------------------------------------------------------------------------

    def height(self, x, y):
        """The ground height at `(x, y)`, or `None` (NaN in an array) outside the raster.

        `HEIGHT_CEILING` never bites here: a heightfield is a single surface, so there is never
        more than one hit to choose between.
        """
        if isinstance(x, (float, int)) and isinstance(y, (float, int)):
            return self._sample_one(x, y)  # the scalar path — see `_sample_one`
        scalar = numpy.isscalar(x) and numpy.isscalar(y)
        out = self._sample(
            numpy.atleast_1d(numpy.asarray(x, dtype=numpy.float64)),
            numpy.atleast_1d(numpy.asarray(y, dtype=numpy.float64)),
        )
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
        u = numpy.clip(fc - c0, 0.0, 1.0)  # 0 at the west edge of the cell
        v = numpy.clip(fr - r0, 0.0, 1.0)  # 0 at the north edge

        nw = self.heights[r0, c0]
        ne = self.heights[r0, c0 + 1]
        sw = self.heights[r0 + 1, c0]
        se = self.heights[r0 + 1, c0 + 1]

        if self.diagonal == "nw_se":
            # Triangles (nw, ne, se) for u >= v, and (nw, se, sw) for u < v.
            upper = u >= v
            out = numpy.where(
                upper, nw + (ne - nw) * u + (se - ne) * v, nw + (se - sw) * u + (sw - nw) * v
            )
        else:
            # Diagonal ne-sw: triangles (nw, ne, sw) for u + v <= 1, else (ne, se, sw).
            lower = (u + v) <= 1.0
            out = numpy.where(
                lower,
                nw + (ne - nw) * u + (sw - nw) * v,
                se + (sw - se) * (1.0 - u) + (ne - se) * (1.0 - v),
            )

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
        except (ValueError, OverflowError):  # NaN or infinity: the array path answers NaN
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
        u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)  # 0 at the west edge of the cell
        v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)  # 0 at the north edge

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


# -- bulk queries on the heightfield -----------------------------------------------------------


def _heightfield_drop_many(self, x, y, z, half_up, half_down):
    """Vectorised `drop`: `(N,)` arrays in, `(N,)` heights out with NaN where nothing is in the
    window. Same rule as the scalar form: one surface, so a window test."""
    top = self._sample(numpy.asarray(x, dtype=numpy.float64), numpy.asarray(y, dtype=numpy.float64))
    z = numpy.asarray(z, dtype=numpy.float64)
    ok = (~numpy.isnan(top)) & (top >= z - half_down) & (top <= z + half_up)
    return numpy.where(ok, top, numpy.nan)


def _heightfield_height_many(self, x, y):  # one surface, so nothing to cut off
    return self._sample(
        numpy.asarray(x, dtype=numpy.float64), numpy.asarray(y, dtype=numpy.float64)
    )


HeightfieldSurface.drop_many = _heightfield_drop_many
HeightfieldSurface.height_many = _heightfield_height_many
