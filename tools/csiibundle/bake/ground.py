"""The ground the bake conforms to: the §8 heightfield with a chosen triangulation.

A heightmap-native engine (BeamNG) triangulates each square between four samples along ONE
diagonal, and which one is a fact about the engine we measure in-game, not a choice we make.
Until it is measured, `envelope` answers conservatively for each product's own failure mode:

* areas (paint on the ground) take the MAX of the two diagonals — a surface that floats a few
  centimetres over a saddle is invisible, one that sinks under it is a hole in the paint;
* skirts (bands that must reach the ground) take the MIN — a band a few centimetres into the
  ground is invisible, one that stops short shows sky under the building.

`diagonal` is `nw_se`, `ne_sw`, or `envelope`; `mode` is `max`/`min` for the envelope case.

**An envelope decodes the raster ONCE and both diagonals share the decoded array.** The two
`HeightfieldSurface`s differ only in which way they split a quad, not in the heights they split;
building each from `from_bundle` decoded the 8192² fine map twice and kept two float64 copies of
it — 1 GiB per ground, which is what stopped the areas conform from being parallelised at all.
`from_heights` is the same door for a worker process handed the array through shared memory.
"""

from .. import drape, reader


class GroundSurface(drape.ProjectionSurface):
    def __init__(self, bundle_dir, diagonal="envelope", mode="max", prefer_fine=True):
        field = reader.load_heightfield(bundle_dir, prefer_fine=prefer_fine) if bundle_dir else None
        if field is None:
            self.mode, self.a, self.b, self.present = mode, None, None, False
            return
        import numpy
        heights = field.raw.astype(numpy.float64) * field.elevation_scale + field.elevation_offset
        self._init(heights, field.cell, field.origin_x, field.origin_z, diagonal, mode)
        self.fine = field.fine

    @classmethod
    def from_heights(cls, heights, cell, origin_x, origin_y, diagonal="envelope", mode="max"):
        """A ground over an already-decoded `(rows, cols)` float64 array, row 0 north.

        The array is NOT copied — both diagonals reference it — so a worker process can attach a
        `multiprocessing.shared_memory` block and pay for the raster once for the whole pool.
        """
        self = cls.__new__(cls)
        self._init(heights, cell, origin_x, origin_y, diagonal, mode)
        return self

    def _init(self, heights, cell, origin_x, origin_y, diagonal, mode):
        self.mode = mode
        make = drape.HeightfieldSurface
        if diagonal == "envelope":
            self.a = make(heights, cell, origin_x, origin_y, diagonal="nw_se")
            self.b = make(heights, cell, origin_x, origin_y, diagonal="ne_sw")
            # the same array, not a copy: `asarray` on a float64 ndarray is a no-op
            assert self.b.heights is self.a.heights
        else:
            self.a = make(heights, cell, origin_x, origin_y, diagonal=diagonal)
            self.b = None
        self.present = True
        self.fine = None

    @property
    def cell(self):
        return self.a.cell if self.a is not None else None

    def height_many(self, x, y, ceiling=4000.0):
        import numpy
        if self.a is None:
            return numpy.full(len(numpy.atleast_1d(x)), numpy.nan)
        ha = self.a.height_many(x, y, ceiling)
        if self.b is None:
            return ha
        hb = self.b.height_many(x, y, ceiling)
        both = numpy.stack([ha, hb])
        with numpy.errstate(invalid="ignore"):
            out = numpy.nanmax(both, axis=0) if self.mode == "max" else numpy.nanmin(both, axis=0)
        return out

    def height(self, x, y, ceiling=4000.0):
        """The ground at one point, `None` off the raster.

        Scalars take a plain-Python path straight through `HeightfieldSurface._sample_one`: the
        conform asks this tens of millions of times and going via `height_many` cost two
        1-element allocations, two full vectorised samples, a `stack` and a `nanmax` inside an
        `errstate` block for every one of them. The envelope rule is the same rule the array
        path applies — `nanmax`/`nanmin` ignore a NaN, so one diagonal missing the raster and
        the other hitting it answers with the hit.
        """
        if self.a is None:
            return None
        if isinstance(x, (float, int)) and isinstance(y, (float, int)):
            ha = self.a._sample_one(x, y)
            if self.b is None:
                return ha
            hb = self.b._sample_one(x, y)
            if ha is None:
                return hb
            if hb is None:
                return ha
            return max(ha, hb) if self.mode == "max" else min(ha, hb)
        import numpy
        out = self.height_many(numpy.array([x], float), numpy.array([y], float), ceiling)
        return None if numpy.isnan(out[0]) else float(out[0])

    def drop_many(self, x, y, z, half_up, half_down):
        import numpy
        top = self.height_many(x, y)
        z = numpy.asarray(z, dtype=float)
        ok = (~numpy.isnan(top)) & (top >= z - half_down) & (top <= z + half_up)
        return numpy.where(ok, top, numpy.nan)

    def drop(self, x, y, z, half_up, half_down):
        import numpy
        out = self.drop_many(numpy.array([x], float), numpy.array([y], float), numpy.array([z], float), half_up, half_down)
        return None if numpy.isnan(out[0]) else float(out[0])
