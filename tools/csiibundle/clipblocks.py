"""The §8c clip records as the containment pass reads them: each strip's per-station bounds in
cell space, the carving records binned into face blocks, and the quad source `contain_faces`
replays block by block.
"""

import numpy

from . import containment


def station_bounds(strip_geo, to_cell):
    """Each strip in cell space with PER-STATION bounds, so a face block replays only the
    stations that reach it. Without the per-station filter a long strip binned to a block
    re-scans all of its quads there, and the containment pass costs ~3x what it needs to."""
    strip_cells = []
    for left_pts, right_pts, _lane, bottom, top, carves in strip_geo:
        lc, rc = to_cell(left_pts), to_cell(right_pts)
        quad_r = numpy.stack((lc[:-1, 1], rc[:-1, 1], lc[1:, 1], rc[1:, 1]), axis=-1)
        quad_c = numpy.stack((lc[:-1, 0], rc[:-1, 0], lc[1:, 0], rc[1:, 0]), axis=-1)
        strip_cells.append(
            (
                lc,
                rc,
                (left_pts[:, 1], right_pts[:, 1]),
                bottom,
                top,
                quad_r.min(axis=1),
                quad_r.max(axis=1),
                quad_c.min(axis=1),
                quad_c.max(axis=1),
                carves,
            )
        )
    return strip_cells


def bin_deep(strip_cells, area_geo, width, height, block):
    """Bin each carving record into the face blocks it reaches, once. Face (r, c) spans cell
    space [c + 0.5, c + 1.5] x [r + 0.5, r + 1.5], so block (br, bc) covers cell space
    [bc*B + 0.5, (bc+1)*B + 0.5] x same in rows; one cell of slack keeps a strip that only
    clips a block edge in play."""
    boxes = [
        (rmin.min(), cmin.min(), rmax.max(), cmax.max())
        for _, _, _, _, _, rmin, rmax, cmin, cmax, _ in strip_cells
    ]
    boxes += [
        (c[:, 1].min(), c[:, 0].min(), c[:, 1].max(), c[:, 0].max()) for c, _, _, _ in area_geo
    ]
    bins = {}
    shallow_areas = 0
    for index, (top_, left_, bottom_, right_) in enumerate(boxes):
        # A shallow band never deletes, so it is never binned: it costs nothing in the
        # containment pass and cannot open a hole. See `contain_faces` for why. The test is
        # per RECORD: a strip's `carves` flag, and an area record's own `top` - the area
        # records used to be binned unconditionally, and every one of the reference city's 266
        # is shallow (median top 0.3 m), so each could delete the faces under it while
        # `deep_touch` stayed False and the report showed nothing (validation pass 2026-08-31, A3).
        if index < len(strip_cells):
            carves = bool(strip_cells[index][-1])
        else:
            carves = float(area_geo[index - len(strip_cells)][3]) > containment.CLIP_DEEP_BAND_TOP
            shallow_areas += 0 if carves else 1
        if not carves:
            continue
        br0 = max(0, int((top_ - 1.5) // block))
        br1 = min((height - 2) // block, int((bottom_ + 0.5) // block))
        bc0 = max(0, int((left_ - 1.5) // block))
        bc1 = min((width - 2) // block, int((right_ + 0.5) // block))
        for br in range(br0, br1 + 1):
            for bc in range(bc0, bc1 + 1):
                bins.setdefault((br, bc), []).append(index)
    return bins, shallow_areas


def make_quad_source(strip_cells, area_geo, bins, block):
    """`quad_source(r0, c0, r1, c1)` for `contain_faces`: every station quad whose strip
    reaches that face block, in native cell space."""

    def quad_source(r0, c0, r1, c1):
        for index in bins.get((r0 // block, c0 // block), ()):
            if index < len(strip_cells):
                lc, rc, (left_y, right_y), low, high, rmin, rmax, cmin, cmax, _ = strip_cells[index]
                live = numpy.nonzero(
                    (rmax >= r0 - 0.5)
                    & (rmin <= r1 + 1.5)
                    & (cmax >= c0 - 0.5)
                    & (cmin <= c1 + 1.5)
                )[0]
                for station in live:
                    i = int(station) + 1
                    yield (
                        [tuple(lc[i - 1]), tuple(rc[i - 1]), tuple(rc[i]), tuple(lc[i])],
                        (
                            float((left_y[i - 1] + left_y[i]) * 0.5),
                            float((right_y[i - 1] + right_y[i]) * 0.5),
                        ),
                        low,
                        high,
                    )
            else:
                cells, lane_y, low, high = area_geo[index - len(strip_cells)]
                yield (
                    [tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
                    lane_y,
                    low,
                    high,
                )

    return quad_source
