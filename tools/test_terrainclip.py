"""Gate for `csiibundle.terrainclip` — the exact terrain cut (audit §46).

Pure geometry, no bundle needed. Checks the properties the cut has to have for the mesh to be
right: area is conserved, nothing kept is inside a prism, nothing outside a prism is removed, and
two triangles sharing an edge cut it at the same points (the watertightness that replaces the old
snap).
"""

import numpy
from csiibundle import terrainclip as tc

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def area3(poly):
    """Plan area of a 3-D polygon."""
    return abs(tc.polygon_area([(p[0], p[2]) for p in poly]))


def flat_tri(x0, z0, x1, z1, x2, z2, y=10.0):
    return [(x0, y, z0), (x1, y, z1), (x2, y, z2)]


def box_prism(xmin, xmax, zmin, zmax, lane_y, bottom, top):
    """A prism over an axis-aligned box, as two sub-triangles."""
    c = [(xmin, lane_y, zmin), (xmax, lane_y, zmin), (xmax, lane_y, zmax), (xmin, lane_y, zmax)]
    return [([c[0], c[1], c[2]], bottom, top), ([c[0], c[2], c[3]], bottom, top)]


def test_full_containment():
    print("a triangle wholly inside the band is removed entirely")
    tri = flat_tri(1, 1, 3, 1, 3, 3, y=10.0)
    prisms = box_prism(0, 10, 0, 10, lane_y=10.0, bottom=-0.5, top=0.3)
    kept = tc.clip_triangle(tri, prisms)
    check(not kept, f"nothing kept ({len(kept)} piece(s))")


def test_no_overlap():
    print("a triangle the prism does not reach is untouched")
    tri = flat_tri(20, 20, 23, 20, 23, 23)
    prisms = box_prism(0, 10, 0, 10, lane_y=10.0, bottom=-0.5, top=0.3)
    kept = tc.clip_triangle(tri, prisms)
    check(
        len(kept) == 1 and abs(area3(kept[0]) - area3(tri)) < 1e-9,
        f"one piece, same area ({len(kept)} piece(s), {area3(kept[0]) if kept else 0:.4f} vs {area3(tri):.4f} m2)",
    )


def test_above_the_band_is_kept():
    print("ground above the band's roof is kept — the hill over a portal")
    tri = flat_tri(1, 1, 9, 1, 9, 9, y=40.0)  # 30 m above the lane
    prisms = box_prism(0, 10, 0, 10, lane_y=10.0, bottom=-0.5, top=12.3)
    kept = tc.clip_triangle(tri, prisms)
    check(
        len(kept) == 1 and abs(area3(kept[0]) - area3(tri)) < 1e-9,
        "the whole triangle survives (it is over the roof, not in the band)",
    )
    tri_low = flat_tri(1, 1, 9, 1, 9, 9, y=15.0)  # 5 m up: inside a 12.3 m band
    check(not tc.clip_triangle(tri_low, prisms), "the same triangle inside the band is removed")


def test_partial_cut_conserves_area():
    print("a partial cut: kept + removed = the whole triangle")
    tri = flat_tri(0, 0, 10, 0, 10, 10, y=10.0)
    prisms = box_prism(3, 20, -5, 20, lane_y=10.0, bottom=-0.5, top=0.3)
    kept = tc.clip_triangle(tri, prisms)
    kept_area = sum(area3(p) for p in kept)
    # the triangle spans z in [0, x] for x in [0, 10], so its area to the left of x = 3 is
    # the integral of x, not a similar triangle: 3**2 / 2 = 4.5 m2 (the whole is 50)
    kept_expected = 3.0**2 / 2.0
    check(
        abs(kept_area - kept_expected) < 1e-6,
        f"kept {kept_area:.4f} m2 = the integral of x over [0, 3] ({kept_expected:.4f}), of a {area3(tri):.0f} m2 triangle "
        f"({len(kept)} piece(s))",
    )
    inside = [p for piece in kept for p in piece if p[0] > 3.0 + 1e-6]
    check(not inside, f"no kept vertex is inside the prism ({len(inside)} were)")


def test_height_iso_cut():
    print("the roof iso-edge: a slope entering the band is cut where h = lane + top")
    # ground rises from y=5 to y=25 across x in [0, 10]; band roof is lane(10) + 5 = 15
    tri = [(0.0, 5.0, 0.0), (10.0, 25.0, 0.0), (10.0, 25.0, 10.0)]
    prisms = box_prism(-5, 20, -5, 20, lane_y=10.0, bottom=-100.0, top=5.0)
    kept = tc.clip_triangle(tri, prisms)
    # h(x) = 5 + 2x; the roof is at 15 -> the cut is at x = 5
    xs = [p[0] for piece in kept for p in piece]
    check(
        kept and min(xs) >= 5.0 - 1e-6,
        f"everything kept is beyond the iso-line x = 5 (min x {min(xs) if xs else float('nan'):.4f})",
    )
    kept_area = sum(area3(p) for p in kept)
    # same shape in plan: the part beyond x = 5 is the integral of x over [5, 10] = 37.5 m2
    check(
        abs(kept_area - 37.5) < 1e-6,
        f"kept area {kept_area:.4f} m2 (the integral of x over [5, 10])",
    )


def test_shared_edge_is_watertight():
    print("two triangles sharing an edge cut it at the same points")
    # a unit square split into two triangles, sharing the diagonal (0,0)-(10,10)
    a = [(0.0, 12.0, 0.0), (10.0, 8.0, 0.0), (10.0, 11.0, 10.0)]
    b = [(0.0, 12.0, 0.0), (10.0, 11.0, 10.0), (0.0, 9.0, 10.0)]
    prisms = box_prism(-5, 20, -5, 20, lane_y=10.0, bottom=-0.5, top=0.3)  # band 9.5 .. 10.3
    ka = tc.clip_triangle(a, prisms)
    kb = tc.clip_triangle(b, prisms)

    # collect the cut vertices that lie on the shared edge (0,0,12)-(10,10,11)
    def on_shared(p):
        t = p[2] / 10.0
        return abs(p[0] - 10.0 * t) < 1e-6 and abs(p[1] - (12.0 - t)) < 1e-6

    va = sorted(round(p[2], 9) for piece in ka for p in piece if on_shared(p))
    vb = sorted(round(p[2], 9) for piece in kb for p in piece if on_shared(p))
    check(va == vb and va, f"the same {len(va)} vertex/vertices on the shared edge ({va} vs {vb})")


def test_two_prisms():
    print("two prisms in a row remove both their parts and nothing else")
    tri = flat_tri(0, 0, 12, 0, 12, 12, y=10.0)
    prisms = box_prism(2, 4, -5, 20, 10.0, -0.5, 0.3) + box_prism(7, 9, -5, 20, 10.0, -0.5, 0.3)
    kept = tc.clip_triangle(tri, prisms)
    bad = [
        p
        for piece in kept
        for p in piece
        if (2.0 + 1e-6 < p[0] < 4.0 - 1e-6) or (7.0 + 1e-6 < p[0] < 9.0 - 1e-6)
    ]
    check(not bad, f"no kept vertex inside either prism ({len(bad)} were)")
    check(sum(area3(p) for p in kept) < area3(tri) - 1e-6, "some area was removed")


def test_sampled_agreement():
    print("point-sample agreement with the game's own predicate over a random field")
    rng = numpy.random.default_rng(7)
    tri = [(0.0, 9.0, 0.0), (10.0, 13.0, 0.0), (4.0, 11.0, 10.0)]
    prisms = box_prism(1, 6, 1, 8, lane_y=10.0, bottom=-0.5, top=1.5)  # band 9.5 .. 11.5
    kept = tc.clip_triangle(tri, prisms)
    plane = tc.plane_through(tri)

    def in_tri(x, z):
        (x0, _, z0), (x1, _, z1), (x2, _, z2) = tri
        d = (z1 - z2) * (x0 - x2) + (x2 - x1) * (z0 - z2)
        l1 = ((z1 - z2) * (x - x2) + (x2 - x1) * (z - z2)) / d
        l2 = ((z2 - z0) * (x - x2) + (x0 - x2) * (z - z2)) / d
        return l1 >= 1e-9 and l2 >= 1e-9 and (1 - l1 - l2) >= 1e-9

    def in_piece(poly, x, z):
        n = len(poly)
        sign = 0
        for i in range(n):
            x1, _, z1 = poly[i]
            x2, _, z2 = poly[(i + 1) % n]
            cr = (x2 - x1) * (z - z1) - (z2 - z1) * (x - x1)
            if abs(cr) < 1e-12:
                continue
            s = 1 if cr > 0 else -1
            if sign == 0:
                sign = s
            elif s != sign:
                return False
        return True

    disagree = 0
    tested = 0
    for _ in range(4000):
        x, z = rng.uniform(-1, 11), rng.uniform(-1, 11)
        if not in_tri(x, z):
            continue
        tested += 1
        h = plane[0] * x + plane[1] * z + plane[2]
        # the game's predicate: inside the footprint AND the height inside the band
        discarded = (1.0 <= x <= 6.0) and (1.0 <= z <= 8.0) and (9.5 <= h <= 11.5)
        covered = any(in_piece(p, x, z) for p in kept)
        if discarded == covered:
            disagree += 1
    check(
        tested > 500 and disagree == 0,
        f"{tested} interior samples: kept == not discarded by the game's predicate ({disagree} disagreed)",
    )


def main():
    print("terrainclip — the exact cut (audit §46)")
    test_full_containment()
    test_no_overlap()
    test_above_the_band_is_kept()
    test_partial_cut_conserves_area()
    test_height_iso_cut()
    test_shared_edge_is_watertight()
    test_two_prisms()
    test_sampled_agreement()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED:")
        for f in FAILED:
            print("  - " + f)
        raise SystemExit(1)
    print("all terrainclip checks passed")


if __name__ == "__main__":
    main()


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_terrainclip.py`."""
    assert main() in (None, 0)
