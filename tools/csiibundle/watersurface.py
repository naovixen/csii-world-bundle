"""The §8 water surface, on the SAME lattice as the ground.

**Water exists where the level raster stands above the ground** (§8; 0 means dry, so the raster
is a level, not a depth). A face is placed wherever ANY of its corners is wet, decided per face
family so the shoreline steps at native resolution inside a refined corridor rather than at the
coarse block size — the shoreline IS a corridor, by construction.

Three details that are each a visible defect when missed:

* **The level extends one sim cell past the shoreline.** The producer dilates it, so the
  surface runs flat INTO the bank and the ground cuts the shoreline where the game's own
  shoreline is. Clipping the water to exactly the wet cells leaves a rim of bare ground.
* **A corner past that dilated ring carries no level at all**, and is seated on the GROUND so
  the surface closes instead of tearing open along its outer edge.
* **Water faces are NOT filtered by the terrain cut.** A clipped trench under a lake still
  has water over it — the tunnel is under the water, not instead of it.
"""

from collections import namedtuple

import numpy

WaterSurface = namedtuple("WaterSurface", "verts loops sizes faces")


def _wet_faces(lattice, stride):
    """The wet members of each face family — any-corner-wet, per family."""
    wet = lattice.wet
    cbr, cbc = lattice.cbr, lattice.cbc
    cell_rr, cell_cc = lattice.cell_rr, lattice.cell_cc

    coarse_wet = (
        wet[cbr * stride, cbc * stride]
        | wet[(cbr + 1) * stride, cbc * stride]
        | wet[(cbr + 1) * stride, (cbc + 1) * stride]
        | wet[cbr * stride, (cbc + 1) * stride]
    )
    water_coarse = lattice.coarse_loops[coarse_wet]

    refined_wet = (
        wet[cell_rr, cell_cc]
        | wet[cell_rr, cell_cc + 1]
        | wet[cell_rr + 1, cell_cc]
        | wet[cell_rr + 1, cell_cc + 1]
    )
    # NOT filtered by the cut: a clipped trench under a lake still has water over it,
    # exactly as the coarse rule always kept it.
    water_refined = lattice.refined_loops_all[refined_wet]

    if len(lattice.fan_loops):
        fan_tris = lattice.fan_loops.reshape(-1, 3)
        water_fans = fan_tris[wet.reshape(-1)[fan_tris].any(axis=1)]
    else:
        water_fans = numpy.zeros((0, 3), dtype=numpy.int64)
    return water_coarse, water_refined, water_fans


def water_surface(lattice, stride, grid, window):
    """The §8 water surface for one window, on the SAME lattice as the ground, or `None`.

    `grid` is the heightmap's `clipgrid.Grid` frame and `window` the same
    `(row0, col0, row1, col1)` the lattice was built for. Returns a `WaterSurface` of `verts`
    `(N, 3)`, flat `loops`, per-face `sizes` and a face count. Nothing is meshed here; that is
    the consumer's. The placement rule and its three non-obvious details are the module
    docstring.
    """
    lat_h, lat_w = lattice.lat_h, lattice.lat_w
    if lattice.level_value is None or not lattice.wet.any():
        return None
    water_coarse, water_refined, water_fans = _wet_faces(lattice, stride)
    wet_faces = len(water_coarse) + len(water_refined) + len(water_fans)
    if not wet_faces:
        return None

    water_loops_all = numpy.concatenate(
        (water_coarse.reshape(-1), water_refined.reshape(-1), water_fans.reshape(-1))
    )
    water_sizes = numpy.concatenate(
        (
            numpy.full(len(water_coarse), 4, dtype=numpy.int64),
            numpy.full(len(water_refined), 4, dtype=numpy.int64),
            numpy.full(len(water_fans), 3, dtype=numpy.int64),
        )
    )

    # A corner past the dilated ring carries no level; seat it on the ground so the
    # surface closes instead of tearing.
    surface_y = numpy.where(lattice.has_level, lattice.level_value, lattice.native_y)

    wused = numpy.zeros(lat_h * lat_w, dtype=bool)
    wused[water_loops_all] = True
    wused_idx = numpy.nonzero(wused)[0]
    wvid = numpy.full(lat_h * lat_w, -1, dtype=numpy.int64)
    wvid[wused_idx] = numpy.arange(len(wused_idx))
    wur, wuc = numpy.divmod(wused_idx, lat_w)
    row0, col0 = window[0], window[1]
    water_verts = numpy.stack(
        (
            grid.origin_x + (col0 + wuc).astype(numpy.float64) * grid.cell,
            grid.origin_z - (row0 + wur).astype(numpy.float64) * grid.cell,
            surface_y[wur, wuc].astype(numpy.float64),
        ),
        axis=-1,
    )
    return WaterSurface(
        verts=water_verts, loops=wvid[water_loops_all], sizes=water_sizes, faces=wet_faces
    )
