"""Gate for `csiibundle.decimate` and `csiibundle.collision` (B1).

Synthetic meshes with known answers, plus the example bundle's cube. Plain
`python tools/test_lod_collision.py`.
"""

import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from cs2beamng.beamng import dae  # noqa: E402
from csiibundle import collision, decimate, glb  # noqa: E402

failures = []


def check(cond, msg):
    (print("ok:  ", msg) if cond else (failures.append(msg), print("FAIL:", msg)))


def grid_mesh(nx, nz, size=10.0, two_materials=True):
    xs = numpy.linspace(0, size, nx)
    zs = numpy.linspace(0, size, nz)
    verts, uvs = [], []
    for z in zs:
        for x in xs:
            verts.append([x, 0.05 * x * z, z])
            uvs.append([x / size, z / size])
    tris = []
    for j in range(nz - 1):
        for i in range(nx - 1):
            a, b, c, d = j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1
            tris += [[a, b, d], [a, d, c]]
    tris = numpy.asarray(tris)
    half = len(tris) // 2
    runs = [("m_a", half), ("m_b", len(tris) - half)] if two_materials else [("m", len(tris))]
    return {
        "verts": numpy.asarray(verts, numpy.float32),
        "normals": numpy.tile([[0, 1, 0]], (len(verts), 1)).astype(numpy.float32),
        "uvs": numpy.asarray(uvs, numpy.float32),
        "tris": tris,
        "material_runs": runs,
    }


def main():
    mesh = grid_mesh(41, 41)
    levels, stats = decimate.lod_ladder(mesh, targets=(0.25, 0.06), backend="cluster")
    n0 = len(mesh["tris"])
    r1, r2 = len(levels[0]["tris"]) / n0, len(levels[1]["tris"]) / n0
    check(0.12 <= r1 <= 0.45, f"level 1 ratio {r1:.2f} near 0.25")
    check(0.02 <= r2 <= 0.15, f"level 2 ratio {r2:.2f} near 0.06")
    check(
        [n for n, _ in levels[0]["material_runs"]] == ["m_a", "m_b"],
        "material runs survive decimation",
    )
    for lvl in levels:
        t = lvl["tris"]
        check(
            len(t) == 0
            or ((t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])).all(),
            "no degenerate faces",
        )
        check(
            t.max() < len(lvl["verts"]) and lvl["uvs"].shape[0] == len(lvl["verts"]),
            "indices and uvs consistent",
        )
        lo, hi = lvl["verts"].min(axis=0), lvl["verts"].max(axis=0)
        check(lo[0] >= -1e-3 and hi[0] <= 10 + 1e-3, "decimated mesh stays inside its bounds")
    check(stats["backend"] == "cluster" and stats["input_tris"] == n0, f"stats {stats}")

    # UV seam preservation: two coplanar patches with distinct UV bins should not merge into one cluster
    seam = grid_mesh(3, 3, two_materials=False)
    seam["uvs"][:, 0] += numpy.where(
        seam["verts"][:, 0] > 5, 0.5, 0.0
    )  # jump in U across the middle
    v, _n, _uv, t = decimate.vertex_cluster(
        seam["verts"], seam["tris"], seam["uvs"], seam["normals"], cell=100.0
    )
    check(len(v) >= 2, f"UV bin keeps a seam apart under a huge cell ({len(v)} clusters)")

    # vertex albedo sampling
    tex = numpy.zeros((4, 4, 4), numpy.uint8)
    tex[:, :2] = (255, 0, 0, 255)
    tex[:, 2:] = (0, 0, 255, 255)
    small = grid_mesh(2, 2, two_materials=False)
    small["uvs"] = small["uvs"] * 0.9  # u = 1.0 would wrap to texel 0 (tiling), so stay inside
    col = decimate.vertex_albedo(small, lambda name: tex)
    check(
        numpy.allclose(col[0], (1, 0, 0, 1)) and numpy.allclose(col[1], (0, 0, 1, 1)),
        "vertex_albedo samples by UV",
    )
    col2 = decimate.vertex_albedo(small, lambda name: (0.2, 0.3, 0.4))
    check(
        numpy.allclose(col2[:, :3], (0.2, 0.3, 0.4)) and numpy.allclose(col2[:, 3], 1.0),
        "vertex_albedo takes a flat colour",
    )

    # collision: thickness, colbox padding, colmesh gate, cylinder, wind colours
    thin = {
        "verts": numpy.array(
            [
                [0, 0, 0],
                [10, 0, 0],
                [10, 2, 0],
                [0, 2, 0],
                [0, 0, 0.05],
                [10, 0, 0.05],
                [10, 2, 0.05],
                [0, 2, 0.05],
            ],
            float,
        ),
        "tris": numpy.array([[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6]]),
        "material_runs": [("m", 4)],
    }
    check(
        abs(collision.thickness(thin["verts"]) - 0.05) < 1e-6, "thickness of a 5 cm panel is 5 cm"
    )
    rot = numpy.array(
        [[numpy.cos(0.6), 0, -numpy.sin(0.6)], [0, 1, 0], [numpy.sin(0.6), 0, numpy.cos(0.6)]]
    )
    check(
        abs(collision.thickness(thin["verts"] @ rot.T) - 0.05) < 1e-4,
        "thickness is rotation-invariant (PCA)",
    )
    check(
        collision.colmesh_from_lod(thin, dae.MIN_COLLIDER_THICKNESS) is None,
        "a thin panel is refused as a colmesh",
    )
    bv, bt = collision.colbox(*collision.bounds(thin["verts"]), dae.MIN_COLLIDER_THICKNESS)
    ext = bv.max(axis=0) - bv.min(axis=0)
    check(
        abs(ext[2] - 0.3) < 1e-6 and abs(ext[0] - 10) < 1e-6 and len(bt) == 12,
        f"colbox pads thin axis to 0.3 m {ext.round(2)}",
    )
    thick = grid_mesh(4, 4)
    thick["verts"][:, 1] += numpy.where(
        numpy.arange(len(thick["verts"])) % 2 == 0, 0.0, 1.0
    )  # 1 m tall
    check(
        collision.colmesh_from_lod(thick, dae.MIN_COLLIDER_THICKNESS) is not None,
        "a thick mesh passes as a colmesh",
    )

    # a synthetic tree: trunk (thin tall box) + canopy (wide sphere-ish cloud)
    trunk = numpy.array(
        [[dx, y, dz] for y in (0, 3) for dx in (-0.2, 0.2) for dz in (-0.2, 0.2)], float
    )
    rng = numpy.random.RandomState(0)
    canopy = rng.normal(0, 1.5, (200, 3)) + [0, 4.5, 0]  # broadcast the centre, NOT concatenation
    verts = numpy.concatenate([trunk, canopy])
    tris = numpy.array(
        [[0, 1, 2], [1, 3, 2], [4, 6, 5], [5, 6, 7]]
        + [[8 + i, 9 + i, 10 + i] for i in range(0, 190, 3)]
    )
    tree = {"verts": verts, "tris": tris, "material_runs": [("bark", 4), ("leaves", len(tris) - 4)]}
    cv, _ct = collision.trunk_cylinder(tree)
    height = cv[:, 1].max() - cv[:, 1].min()
    check(abs(height - 3.0) < 1e-6, f"trunk cylinder spans the trunk group height ({height:.2f} m)")
    radius = numpy.linalg.norm(cv[:8, [0, 2]] - cv[:8, [0, 2]].mean(axis=0), axis=1).max()
    check(0.25 <= radius <= 0.35, f"trunk radius from the base band ({radius:.2f} m)")
    tree_noname = dict(tree, material_runs=[("m1", 4), ("m2", len(tris) - 4)])
    cv2, _ = collision.trunk_cylinder(tree_noname)
    check(
        abs((cv2[:, 1].max() - cv2[:, 1].min()) - 3.0) < 1e-6,
        "trunk found by footprint when names say nothing",
    )
    wc = dae.wind_vertex_colours(tree)
    check(
        wc.shape == (len(verts), 4)
        and (wc[:8, 0] == 0).all()
        and wc[8:, 0].max() > 0.5
        and (wc >= 0).all()
        and (wc <= 1).all(),
        "wind colours: trunk rigid, canopy bends, all in 0..1",
    )

    # the example bundle's cube round-trips through the ladder
    cube = glb.parse_glb(
        os.path.join(
            os.path.dirname(HERE), "docs", "example-bundle", "meshes", "carto_example_cube.glb"
        )
    )[0]
    lv, st = decimate.lod_ladder(cube, targets=(0.5,), backend="cluster")
    check(len(lv[0]["tris"]) >= 1, f"example cube decimates ({st})")

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all lod/collision checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_lod_collision.py`."""
    assert main() in (None, 0)
