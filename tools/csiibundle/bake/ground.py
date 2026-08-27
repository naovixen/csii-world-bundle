"""The ground the bake conforms to: the §8 heightfield with a chosen triangulation.

A heightmap-native engine (BeamNG) triangulates each square between four samples along ONE
diagonal, and which one is a fact about the engine, not a choice we make.

**MEASURED 2026-08-19 (audit §73): BeamNG splits SW-NE, uniformly — `ne_sw` here.**
`tools/beamng/probe_diagonal.py` samples vanilla DecalRoad nodes, which lie on the terrain, inside
their own square under every hypothesis. Six shipped levels with enough discriminating samples
agree unanimously and EXACTLY: median |err| 0.0000 m for `ne_sw` against 0.032-0.039 m for
`nw_se`, and on `east_coast_usa` p90 and p99 are 0.0000 too. A per-square alternating
(checkerboard) split was tested and falsified.

It did not need the in-game probe this file used to ask for: `probe_ter.py` had already settled
the `.ter` orientation offline by the same observation, to 0.020 m, which is an order of
magnitude under what this question needs.

`envelope` remains selectable and is what the default WAS. It took the MAX of both diagonals for
areas and the MIN for skirts — conservative for each product's own failure mode while the answer
was unknown, and simply **wrong** now that it is known: `max(A, B) >= A` by construction, so paint
conformed to it floats above the ground everywhere by that square's twist (A6.5 — p90 +0.116 m
against the real triangulation, and a car dropping through the visible plaza onto relief the
plaza was hiding).

`diagonal` is `nw_se`, `ne_sw`, or `envelope`; `mode` is `max`/`min` for the envelope case.

**Beware the frame when comparing with the probe.** `drape.HeightfieldSurface` has row 0 NORTH,
so its `ne_sw` joins the NE and SW corners. `probe_diagonal.py` works in the `.ter`'s frame, where
row 0 is SOUTH, so the same split is called `sw_ne` there. The probe prints the name to use here.

**An envelope decodes the raster ONCE and both diagonals share the decoded array.** The two
`HeightfieldSurface`s differ only in which way they split a quad, not in the heights they split;
building each from `from_bundle` decoded the 8192² fine map twice and kept two float64 copies of
it — 1 GiB per ground, which is what stopped the areas conform from being parallelised at all.
`from_heights` is the same door for a worker process handed the array through shared memory.
"""

from .. import drape, reader

# How BeamNG actually splits a terrain square, measured — see the module docstring.
# `tools/beamng/probe_diagonal.py` re-checks it in seconds on a new BeamNG version.
#
# **A BeamNG fact in an engine-neutral package.** It is the default because `cs2beamng` is the
# only consumer of this bake that ships terrain, so "the measured one" and "the only one anybody
# asks for" are the same string today. They will not be for a second engine: the diagonal must
# match how that engine meshes the grid (`drape.HeightfieldSurface`), so it is a PARAMETER
# everywhere — `GroundSurface(diagonal=...)`, `options["diagonal"]` — and this constant is only
# what a caller gets for saying nothing.
MEASURED_DIAGONAL = "ne_sw"


class GroundSurface(drape.ProjectionSurface):
    def __init__(self, bundle_dir, diagonal=MEASURED_DIAGONAL, mode="max", prefer_fine=True):
        field = reader.load_heightfield(bundle_dir, prefer_fine=prefer_fine) if bundle_dir else None
        if field is None:
            self.mode, self.a, self.b, self.present = mode, None, None, False
            return
        import numpy

        heights = field.raw.astype(numpy.float64) * field.elevation_scale + field.elevation_offset
        self._init(heights, field.cell, field.origin_x, field.origin_z, diagonal, mode)
        self.fine = field.fine

    @classmethod
    def from_heights(
        cls, heights, cell, origin_x, origin_y, diagonal=MEASURED_DIAGONAL, mode="max"
    ):
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

    def height_many(self, x, y):
        import numpy

        if self.a is None:
            return numpy.full(len(numpy.atleast_1d(x)), numpy.nan)
        ha = self.a.height_many(x, y)
        if self.b is None:
            return ha
        hb = self.b.height_many(x, y)
        both = numpy.stack([ha, hb])
        with numpy.errstate(invalid="ignore"):
            out = numpy.nanmax(both, axis=0) if self.mode == "max" else numpy.nanmin(both, axis=0)
        return out

    def height(self, x, y):
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

        out = self.height_many(numpy.array([x], float), numpy.array([y], float))
        return None if numpy.isnan(out[0]) else float(out[0])

    def drop_many(self, x, y, z, half_up, half_down):
        import numpy

        top = self.height_many(x, y)
        z = numpy.asarray(z, dtype=float)
        ok = (~numpy.isnan(top)) & (top >= z - half_down) & (top <= z + half_up)
        return numpy.where(ok, top, numpy.nan)

    def drop(self, x, y, z, half_up, half_down):
        import numpy

        out = self.drop_many(
            numpy.array([x], float),
            numpy.array([y], float),
            numpy.array([z], float),
            half_up,
            half_down,
        )
        return None if numpy.isnan(out[0]) else float(out[0])
