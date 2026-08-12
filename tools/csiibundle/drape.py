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
    def from_bundle(cls, bundle_dir, diagonal="nw_se"):
        """Build one straight from a bundle's §8 terrain layer, or `None` if it has none."""
        paths = reader.terrain_paths(bundle_dir)
        if paths.rasters is None or not os.path.isfile(paths.heightmap):
            return None
        raw = png.gray_array(paths.heightmap).astype(numpy.float64)
        scale = float(paths.rasters.get("elevationScale", 1.0))
        offset = float(paths.rasters.get("elevationOffset", 0.0))
        origin_x, _, origin_z = paths.rasters["originWorld"]
        return cls(raw * scale + offset, float(paths.rasters["resolution"]),
                   origin_x, origin_z, diagonal=diagonal)

    # -- the queries ---------------------------------------------------------------------------

    def height(self, x, y, ceiling=4000.0):     # noqa: ARG002 - a heightfield has no occluders
        """The ground height at `(x, y)`, or `None` (NaN in an array) outside the raster.

        `ceiling` is accepted for protocol compatibility and ignored: a heightfield is a single
        surface, so there is never more than one hit to choose between.
        """
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
