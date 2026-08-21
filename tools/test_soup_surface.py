"""Gate for `csiibundle.drape.TriangleSoupSurface` and `drape_blocks`.

Three oracles, none of which share code with the surface under test:

1. A brute-force per-query intersector over a random synthetic soup (every triangle tested for
   every query) — proves the CSR grid finds exactly what a full scan finds, including queries on
   cell boundaries and outside the grid, and that scalar and bulk paths agree bit for bit.
2. `HeightfieldSurface` on a meshed heightfield — the soup built from the same triangles the
   heightfield assumes (nw_se diagonal) must return the same heights.
3. `tools/test_drape_pick.py`'s own vertical intersector (`glb_triangles` + `hits_below`) on
   real segment meshes from the bundle on disk — the drape-pick rule (nearest-to-expected inside
   the projector window) reproduced against the reference implementation. Skipped when no
   bundle is on disk (`CARTO_BUNDLE` overrides the path).

Plain `python tools/test_soup_surface.py`. Reports throughput as a note, not a gate.
"""

import os
import sys
import time

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from csiibundle import drape  # noqa: E402

BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto"
)

failures = []


def check(cond, msg):
    if cond:
        print("ok:  ", msg)
    else:
        failures.append(msg)
        print("FAIL:", msg)


def brute_hits(verts, tris, x, y):
    """Every plane height under (x, y), any order — the full-scan oracle."""
    a0, a1, a2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    d00 = a1[:, :2] - a0[:, :2]
    d01 = a2[:, :2] - a0[:, :2]
    det = d00[:, 0] * d01[:, 1] - d01[:, 0] * d00[:, 1]
    live = numpy.abs(det) > 1e-12
    p = numpy.array([x, y]) - a0[:, :2]
    safe = numpy.where(live, det, 1.0)
    w1 = (p[:, 0] * d01[:, 1] - d01[:, 0] * p[:, 1]) / safe
    w2 = (d00[:, 0] * p[:, 1] - p[:, 0] * d00[:, 1]) / safe
    w0 = 1 - w1 - w2
    inside = live & (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
    return (w0 * a0[:, 2] + w1 * a1[:, 2] + w2 * a2[:, 2])[inside]


def synthetic():
    rng = numpy.random.RandomState(7)
    # 400 random small triangles in a 100 x 100 field, stacked at three height bands, plus a
    # few vertical ones (edge-on) and a big flat quad underneath everything.
    n = 400
    centres = rng.uniform(0, 100, (n, 2))
    offsets = rng.uniform(-3, 3, (n, 3, 2))
    band = rng.choice([0.0, 5.0, 12.0], n)
    verts = []
    tris = []
    for i in range(n):
        for k in range(3):
            verts.append(
                [
                    centres[i, 0] + offsets[i, k, 0],
                    centres[i, 1] + offsets[i, k, 1],
                    band[i] + rng.uniform(-0.2, 0.2),
                ]
            )
        tris.append([3 * i, 3 * i + 1, 3 * i + 2])
    base = len(verts)
    verts += [[-10, -10, -1], [110, -10, -1], [110, 110, -1], [-10, 110, -1]]
    tris += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    base = len(verts)
    verts += [[50, 0, 0], [50, 0, 30], [50, 100, 30], [50, 100, 0]]  # vertical wall, edge-on
    tris += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    verts = numpy.asarray(verts, numpy.float64)
    tris = numpy.asarray(tris, numpy.int64)
    soup = drape.TriangleSoupSurface(verts, tris, cell=4.0)

    q = 3000
    qx = rng.uniform(-15, 115, q)
    qy = rng.uniform(-15, 115, q)
    # some queries exactly on cell boundaries and on the wall's x
    qx[:50] = soup.origin[0] + 4.0 * rng.randint(0, 20, 50)
    qx[50:80] = 50.0
    qz = rng.uniform(-3, 15, q)
    half = 1.0

    t0 = time.perf_counter()
    got_h = soup.height_many(qx, qy)
    got_d = soup.drop_many(qx, qy, qz, half, half)
    dt = time.perf_counter() - t0
    exp_h = numpy.full(q, numpy.nan)
    exp_d = numpy.full(q, numpy.nan)
    for i in range(q):
        hits = brute_hits(verts, tris, qx[i], qy[i])
        if len(hits):
            exp_h[i] = hits.max()
            win = hits[(hits >= qz[i] - half) & (hits <= qz[i] + half)]
            if len(win):
                exp_d[i] = win[numpy.argmin(numpy.abs(win - qz[i]))]
    same_h = (numpy.isnan(got_h) == numpy.isnan(exp_h)).all() and numpy.allclose(
        got_h[~numpy.isnan(got_h)], exp_h[~numpy.isnan(exp_h)], atol=1e-6
    )
    same_d = (numpy.isnan(got_d) == numpy.isnan(exp_d)).all() and numpy.allclose(
        got_d[~numpy.isnan(got_d)], exp_d[~numpy.isnan(exp_d)], atol=1e-6
    )
    check(
        same_h,
        f"synthetic height_many == brute force ({int((~numpy.isnan(exp_h)).sum())} hits of {q})",
    )
    check(
        same_d,
        f"synthetic drop_many == brute force nearest-in-window ({int((~numpy.isnan(exp_d)).sum())} hits)",
    )
    print(f"      note: {q} queries x 2 modes on {len(tris)} tris in {dt * 1000:.1f} ms")

    # scalar protocol == bulk
    ok = True
    for i in range(0, q, 97):
        h = soup.height(qx[i], qy[i])
        d = soup.drop(qx[i], qy[i], qz[i], half, half)
        ok &= (h is None) == numpy.isnan(got_h[i]) and (h is None or abs(h - got_h[i]) < 1e-9)
        ok &= (d is None) == numpy.isnan(got_d[i]) and (d is None or abs(d - got_d[i]) < 1e-9)
    check(ok, "scalar drop/height agree with the bulk path")

    # empty soup and out-of-grid
    empty = drape.TriangleSoupSurface(numpy.zeros((0, 3)), numpy.zeros((0, 3), numpy.int64))
    check(
        empty.height(1, 1) is None and numpy.isnan(empty.drop_many([1], [1], [1], 1, 1)).all(),
        "empty soup answers None/NaN",
    )
    check(soup.height(1000, 1000) is None, "outside the grid is None")

    # from_pieces offsets
    s2 = drape.TriangleSoupSurface.from_pieces(
        [(verts[:3], tris[:1]), (verts[3:], tris[1:] - 3)], cell=4.0
    )
    check(
        numpy.allclose(numpy.nan_to_num(s2.height_many(qx, qy)), numpy.nan_to_num(got_h)),
        "from_pieces reproduces the concatenated soup",
    )

    # drape_blocks: block 1 sits over the base quad (-1) with an expected height in reach;
    # block 2 lies outside the soup but on the ground heightfield; block 3 is out of reach.
    ground = drape.HeightfieldSurface(numpy.full((60, 60), -0.5), 4.0, -100.0, 200.0)
    b1 = {
        "verts": numpy.array([[20, -0.9, 20], [21, -0.9, 20], [21, -0.9, 21]], numpy.float64),
        "half_height": 0.5,
        "drape_eps": 0.014,
    }
    b2 = {
        "verts": numpy.array([[120, 0.0, 0], [120, 0.0, 1]], numpy.float64),
        "half_height": 0.5,
        "drape_eps": 0.02,
    }
    b3 = {
        "verts": numpy.array([[120, 30.0, 0]], numpy.float64),
        "half_height": 0.5,
        "drape_eps": 0.02,
    }
    counters = drape.drape_blocks([b1, b2, b3], soup, ground)
    check(
        numpy.allclose(b1["verts"][:, 1], -1.0 + 0.014),
        "drape_blocks lands block 1 on the soup + lift",
    )
    check(
        numpy.allclose(b2["verts"][:, 1], -0.5 + 0.02),
        "drape_blocks falls back to the ground surface + lift",
    )
    check(numpy.allclose(b3["verts"][:, 1], 30.0), "a miss keeps its height")
    check(
        counters["missed"] == 1 and counters["grounded"] == 2 and counters["draped"] == 3,
        f"counters {counters}",
    )


def heightfield_agreement():
    rng = numpy.random.RandomState(3)
    rows, cols, cell = 12, 15, 3.5
    heights = rng.uniform(100, 130, (rows, cols))
    origin_x, origin_y = 10.0, 500.0  # row 0 north: y decreases with row
    hf = drape.HeightfieldSurface(heights, cell, origin_x, origin_y, diagonal="nw_se")
    verts = []
    for r in range(rows):
        for c in range(cols):
            verts.append([origin_x + c * cell, origin_y - r * cell, heights[r, c]])
    verts = numpy.asarray(verts)
    tris = []
    idx = lambda r, c: r * cols + c  # noqa: E731
    for r in range(rows - 1):
        for c in range(cols - 1):
            nw, ne, sw, se = idx(r, c), idx(r, c + 1), idx(r + 1, c), idx(r + 1, c + 1)
            tris.append([nw, ne, se])  # u >= v half
            tris.append([nw, se, sw])  # u < v half
    soup = drape.TriangleSoupSurface(verts, numpy.asarray(tris), cell=7.0)
    qx = rng.uniform(origin_x, origin_x + (cols - 1) * cell, 2000)
    qy = rng.uniform(origin_y - (rows - 1) * cell, origin_y, 2000)
    a = hf.height_many(qx, qy)
    b = soup.height_many(qx, qy)
    good = ~numpy.isnan(a)
    check(
        good.all() and (~numpy.isnan(b)).all() and numpy.allclose(a, b, atol=1e-6),
        f"soup over a meshed heightfield == HeightfieldSurface (max |diff| {numpy.nanmax(numpy.abs(a - b)):.2e})",
    )


def real_bundle():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP real-bundle oracle: no bundle at {BUNDLE}")
        return
    import test_drape_pick as oracle

    center, radius = oracle.CENTER, 300.0
    records = [
        r
        for r in oracle._jsonl(os.path.join(BUNDLE, "networks.jsonl"))
        if r.get("kind") == "segment"
        and r.get("pos")
        and abs(r["pos"][0] - center[0]) <= radius
        and abs(r["pos"][2] - center[1]) <= radius
    ]
    pieces = []
    tri_soup = []
    for r in records:
        path = os.path.join(BUNDLE, "networks", r["assetId"] + ".glb")
        if not os.path.isfile(path):
            continue
        tris = oracle.glb_triangles(path) + numpy.asarray(
            r["pos"], numpy.float64
        )  # bundle frame (x, y up, z)
        if not len(tris):
            continue
        tri_soup.append(tris)
        flat = tris.reshape(-1, 3)
        # bundle (x, y, z) -> Z-up (x, z, y)
        pieces.append((flat[:, [0, 2, 1]], numpy.arange(len(flat)).reshape(-1, 3)))
    if len(pieces) < 20:
        print(f"SKIP real-bundle oracle: only {len(pieces)} segment meshes in the window")
        return
    all_tris = numpy.concatenate(tri_soup)
    t0 = time.perf_counter()
    soup = drape.TriangleSoupSurface.from_pieces(pieces, cell=8.0)
    build = time.perf_counter() - t0
    rng = numpy.random.RandomState(11)
    n = 1500
    flat = all_tris.reshape(-1, 3)
    lo, hi = flat.min(axis=0), flat.max(axis=0)
    x = rng.uniform(lo[0], hi[0], n)
    z = rng.uniform(lo[2], hi[2], n)
    exp_top = numpy.full(n, numpy.nan)
    exp_drop = numpy.full(n, numpy.nan)
    expected_y = numpy.full(n, numpy.nan)
    for i in range(n):
        hits = oracle.hits_below(all_tris, x[i], z[i], numpy.inf, -numpy.inf)
        if len(hits):
            exp_top[i] = hits[0]
            expected_y[i] = hits[0] + 0.1 + rng.uniform(-0.05, 0.05)
            win = hits[(hits >= expected_y[i] - 0.5) & (hits <= expected_y[i] + 0.5)]
            if len(win):
                exp_drop[i] = win[numpy.argmin(numpy.abs(win - expected_y[i]))]
    t0 = time.perf_counter()
    got_top = soup.height_many(x, z)
    got_drop = soup.drop_many(x, z, numpy.nan_to_num(expected_y, nan=-1e9), 0.5, 0.5)
    q = time.perf_counter() - t0
    hit = ~numpy.isnan(exp_top)
    same_top = (numpy.isnan(got_top) == numpy.isnan(exp_top)).all() and numpy.allclose(
        got_top[hit], exp_top[hit], atol=1e-4
    )
    same_drop = (numpy.isnan(got_drop) == numpy.isnan(exp_drop)).all() and numpy.allclose(
        got_drop[~numpy.isnan(exp_drop)], exp_drop[~numpy.isnan(exp_drop)], atol=1e-4
    )
    check(
        same_top,
        f"real segments: height_many == test_drape_pick.hits_below topmost ({int(hit.sum())}/{n} hits, {len(pieces)} segments, {len(all_tris)} tris)",
    )
    check(same_drop, "real segments: drop_many == nearest-to-expected inside the ±0.5 m window")
    print(f"      note: build {build:.2f} s, {n} queries x 2 modes {q * 1000:.0f} ms")


def main():
    synthetic()
    heightfield_agreement()
    real_bundle()
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all soup surface checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_soup_surface.py`."""
    assert main() in (None, 0)
