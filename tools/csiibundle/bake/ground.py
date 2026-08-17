"""The ground the bake conforms to: the §8 heightfield with a chosen triangulation.

A heightmap-native engine (BeamNG) triangulates each square between four samples along ONE
diagonal, and which one is a fact about the engine we measure in-game, not a choice we make.
Until it is measured, `envelope` answers conservatively for each product's own failure mode:

* areas (paint on the ground) take the MAX of the two diagonals — a surface that floats a few
  centimetres over a saddle is invisible, one that sinks under it is a hole in the paint;
* skirts (bands that must reach the ground) take the MIN — a band a few centimetres into the
  ground is invisible, one that stops short shows sky under the building.

`diagonal` is `nw_se`, `ne_sw`, or `envelope`; `mode` is `max`/`min` for the envelope case.
"""

from .. import drape


class GroundSurface(drape.ProjectionSurface):
    def __init__(self, bundle_dir, diagonal="envelope", mode="max", prefer_fine=True):
        self.mode = mode
        if diagonal == "envelope":
            self.a = drape.HeightfieldSurface.from_bundle(bundle_dir, diagonal="nw_se", prefer_fine=prefer_fine)
            self.b = drape.HeightfieldSurface.from_bundle(bundle_dir, diagonal="ne_sw", prefer_fine=prefer_fine)
        else:
            self.a = drape.HeightfieldSurface.from_bundle(bundle_dir, diagonal=diagonal, prefer_fine=prefer_fine)
            self.b = None
        self.present = self.a is not None

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
