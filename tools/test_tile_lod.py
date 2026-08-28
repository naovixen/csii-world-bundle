"""The tile LOD ladder: does it switch where a player can see it, and can it crack?

Two claims, and the second is the one that would ship a visible defect:

  1. **The ladder is anchored on the view distance, not on CS2's null distance.** A road
     composition's own cull distance is 46 km; spacing the switches off that put LOD 1 at 7.7 km,
     outside any view distance the app ships, so a networks tile kept 82.5 % of its triangles at
     its far level and the ladder bought nothing. `tile_levels(view_distance=...)` must place the
     switches inside the view distance while `nullPx` still answers the game's question.
  2. **Two adjacent tiles must still meet after decimation.** `decimate.vertex_cluster` derives
     its grid origin and cell size from the mesh it is given, so two neighbours cluster on two
     different grids and a shared boundary vertex is averaged differently in each — a crack the
     length of every tile edge. `tilelod.boundary_pins` puts every vertex within `PIN_MARGIN_M`
     of the tile edge in a cluster of its own; this asserts that the two tiles' boundary vertex
     sets come out IDENTICAL, on a mesh built so that a failure would show (a dense grid whose
     edge vertices are shared, with different interior geometry either side so the two grids
     really do differ).

Also checked: the nearest level is never decimated (it is what a `Visible Mesh` collider
rasterises and what you stand on), and material runs survive the decimation.

Run: `python tools/test_tile_lod.py`  (system Python + numpy; no bundle needed).
"""

import sys

import numpy
from cs2beamng.beamng import dae, tilelod, tilemesh
from csiibundle import decimate, lod

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


TILE = 256.0


def grid_tile(seed, step=4.0):
    """A tile-local grid mesh spanning exactly ±TILE/2, with a seeded interior height field.

    The boundary ROW of vertices lands on ±TILE/2 exactly, which is what two neighbours share.
    The interior differs per seed so the two tiles' cluster grids genuinely differ.
    """
    rng = numpy.random.default_rng(seed)
    n = int(TILE / step) + 1
    xs = numpy.linspace(-TILE / 2, TILE / 2, n)
    gx, gy = numpy.meshgrid(xs, xs, indexing="ij")
    z = rng.normal(0.0, 1.5, gx.shape)
    verts = numpy.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1)
    tris = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j, (i + 1) * n + j + 1
            tris.append((a, b, d))
            tris.append((a, d, c))
    tris = numpy.asarray(tris, dtype=numpy.int64)
    return verts, tris


# -- 1. the anchor -------------------------------------------------------------------------------
# One tile of road: every triangle culled at CS2's 46 km, which is what makes the A4 ladder empty.
radius = TILE * (2**0.5) * 0.5
far = 46355.8
distances = numpy.full(4000, far)
distances[:1200] = far / 3.0  # a minority the game culls sooner, so a ladder is possible

old = tilelod.tile_levels(radius, distances, detail_scale=6.0)
new = tilelod.tile_levels(radius, distances, detail_scale=6.0, view_distance=4000.0)

check(
    old.get("anchorMetres") == far,
    "with no view distance the ladder still anchors on the game's null",
    f"{old.get('anchorMetres')}",
)
check(
    new["anchorMetres"] == 4000.0,
    "with a 4 km view distance the ladder anchors there",
    f"{new['anchorMetres']}",
)
check(
    new["nullPx"] == old["nullPx"],
    "nullPx is unchanged — the tile still disappears where CS2 stops drawing it",
    f"{new['nullPx']} vs {old['nullPx']}",
)


# A detail of size `px` is used from the distance at which the tile projects to `px` inwards,
# and px scales as 1/d — so that distance is `group_detail_px(radius, 1 m) / px`. This is the
# real switch distance, and asserting it is what caught `lod_detail_scale` being applied to the
# ladder as well as to the null threshold (which put every switch 10x too near, so the level had
# a hole beyond 267 m).
def switches(ladder):
    return [lod.group_detail_px(radius, 1.0) / px for px, _m in ladder["levels"]]


anchored = tilelod.tile_levels(
    radius, distances, detail_scale=10.0, view_distance=4000.0, distinct_only=False
)
switch = switches(anchored)
check(
    max(switch) <= 4000.0 * 1.01 and max(switch) >= 4000.0 * 0.99,
    "the coarsest level draws out to exactly the view distance",
    ", ".join(f"{s:.0f} m" for s in sorted(switch)),
)
check(min(switch) < 400.0, "and full detail is kept only near the camera", f"{min(switch):.0f} m")
check(
    all(s > 4000.0 for s in switches(old)[:1]) or max(switches(old)) > 4000.0,
    "the A4 ladder's switches ran past any view distance — this is the defect being fixed",
    ", ".join(f"{s:.0f} m" for s in sorted(switches(old))),
)
# Inside a 4 km view distance CS2 culls none of this (a road draws to 46 km), so every level's
# CONTENT is the same and the saving has to come from decimation — which is why `build_levels`
# turns de-duplication off when it is going to decimate. Both facts are asserted.
check(
    len(
        tilelod.tile_levels(radius, distances, 6.0, view_distance=4000.0, distinct_only=False)[
            "levels"
        ]
    )
    == len(tilelod.RATIOS),
    "with decimation on, one level per ratio survives even where the content repeats",
    f"{len(tilelod.tile_levels(radius, distances, 6.0, view_distance=4000.0, distinct_only=False)['levels'])}"
    f" of {len(tilelod.RATIOS)}",
)
check(
    len(new["levels"]) == 1,
    "and with it off they collapse to one, because the game culls nothing inside 4 km",
    f"{len(new['levels'])} level(s)",
)

# -- 2. the seam ---------------------------------------------------------------------------------
# Two tiles side by side in X. Their shared edge is tile A's +TILE/2 column and tile B's −TILE/2
# column: the SAME world vertices, expressed in each tile's own local frame.
va, ta = grid_tile(1)
vb, tb = grid_tile(2)
# make the shared column agree in world Z, as two real neighbouring tiles would
edge_a = numpy.isclose(va[:, 0], TILE / 2)
edge_b = numpy.isclose(vb[:, 0], -TILE / 2)
order_a = numpy.argsort(va[edge_a][:, 1])
order_b = numpy.argsort(vb[edge_b][:, 1])
shared_z = va[edge_a][order_a][:, 2]
tmp = vb[edge_b]
tmp[order_b, 2] = shared_z
vb[edge_b] = tmp

mesh_a = dae.Mesh("a", va, ta, [("m", len(ta))])
mesh_b = dae.Mesh("b", vb, tb, [("m", len(tb))])

pin_a = tilelod.boundary_pins(mesh_a.verts, TILE)
pin_b = tilelod.boundary_pins(mesh_b.verts, TILE)
check(
    pin_a.sum() > 0 and pin_b.sum() > 0,
    "the boundary band selects vertices",
    f"{int(pin_a.sum())} of {len(va)}",
)

out_a = tilemesh._decimated(dae, "a", mesh_a, 0.25, TILE)
out_b = tilemesh._decimated(dae, "b", mesh_b, 0.25, TILE)
check(
    len(out_a.tris) < len(ta) * 0.6,
    "the mesh really was decimated",
    f"{len(ta)} -> {len(out_a.tris)} triangles",
)


def edge_points(mesh, x):
    v = numpy.asarray(mesh.verts, dtype=numpy.float64)
    sel = numpy.isclose(v[:, 0], x, atol=1e-4)
    return sorted((round(float(p[1]), 4), round(float(p[2]), 4)) for p in v[sel])


a_edge = edge_points(out_a, TILE / 2)
b_edge = edge_points(out_b, -TILE / 2)
check(
    a_edge == b_edge,
    "the two decimated tiles' shared boundary vertices are identical",
    f"{len(a_edge)} vs {len(b_edge)} points"
    + (
        ""
        if a_edge == b_edge
        else f"; first difference "
        f"{[p for p in a_edge if p not in b_edge][:2]} / {[p for p in b_edge if p not in a_edge][:2]}"
    ),
)
check(
    len(a_edge)
    == int(pin_a[numpy.isclose(numpy.asarray(mesh_a.verts, numpy.float64)[:, 0], TILE / 2)].sum()),
    "and every one of them survived — a pinned vertex is never merged away",
    f"{len(a_edge)} out of {int(numpy.isclose(numpy.asarray(mesh_a.verts, numpy.float64)[:, 0], TILE / 2).sum())}",
)

# without the pins the same two tiles must DISAGREE, or this test proves nothing
free_a = decimate.cluster_mesh(
    {
        "verts": numpy.asarray(mesh_a.verts, numpy.float64),
        "normals": None,
        "uvs": None,
        "tris": numpy.asarray(mesh_a.tris),
        "material_runs": list(mesh_a.runs),
    },
    0.25,
    pinned=None,
)
free_b = decimate.cluster_mesh(
    {
        "verts": numpy.asarray(mesh_b.verts, numpy.float64),
        "normals": None,
        "uvs": None,
        "tris": numpy.asarray(mesh_b.tris),
        "material_runs": list(mesh_b.runs),
    },
    0.25,
    pinned=None,
)


def edge_of(verts, x):
    v = numpy.asarray(verts, dtype=numpy.float64)
    sel = numpy.isclose(v[:, 0], x, atol=1e-4)
    return sorted((round(float(p[1]), 4), round(float(p[2]), 4)) for p in v[sel])


# Unpinned, the edge does not merely disagree — it stops existing: the clusterer averages each
# boundary vertex with its inland neighbours, so almost nothing is left ON the shared plane and
# what is left is at a different height in each tile. Either way the two surfaces no longer meet.
free_edge_a = edge_of(free_a["verts"], TILE / 2)
free_edge_b = edge_of(free_b["verts"], -TILE / 2)
# What survives unpinned is the two CORNERS — extremes of the bounding box, which land in their
# own cells whatever the cell size, and which both tiles therefore still agree on. Everything
# between them is gone: 63 of the 65 shared vertices have been averaged inland, and the two
# surfaces no longer meet anywhere along the edge.
check(
    len(free_edge_a) < len(a_edge) / 4 and len(free_edge_b) < len(a_edge) / 4,
    "unpinned, the shared edge is destroyed — only the bounding-box corners survive it",
    f"{len(free_edge_a)} and {len(free_edge_b)} points survive on the plane, against {len(a_edge)} pinned",
)

# -- 3. the nearest level is untouched, and runs survive ------------------------------------------
# `#L<n>` is how the bake carries the game's own per-primitive cull distance; without one the
# tile is "never culled" and gets a single always-on level, which is the honest answer but leaves
# this check nothing to inspect.
runs = [("mat_a#L120", len(ta) // 2), ("mat_b#L96", len(ta) - len(ta) // 2)]
mesh_runs = dae.Mesh("r", va, ta, runs)
levels, ladder = tilemesh.build_levels(
    dae,
    "r",
    mesh_runs,
    6.0,
    lod.DEFAULT_LEVEL_OF_DETAIL,
    tilemesh.ALWAYS_PX,
    view_distance=4000.0,
    targets=tilemesh.TILE_TARGETS,
    tile=TILE,
)
nearest = sum(len(m.tris) for m in levels[0][1])
check(
    nearest == len(ta),
    "the nearest level keeps every triangle — collision reads detail 0",
    f"{nearest} of {len(ta)}",
)
if len(levels) > 1:
    materials = set()
    for m in levels[-1][1]:
        materials.update(m.materials)
    check(
        materials <= {"mat_a", "mat_b"} and materials,
        "the far level still binds real material runs",
        str(sorted(materials)),
    )
else:
    print("  note  this mesh produced a single level, so the run check had nothing to look at")

print("test_tile_lod: " + ("FAILED" if failures else "OK"))
if __name__ == "__main__":
    sys.exit(1 if failures else 0)


def gate():
    """pytest entry point (tools/conftest.py).

    This file is still a script: its body runs at import, and `check()` counts what failed
    rather than raising. So the gate reads the counter. Phase 4 turns the body into real
    per-claim pytest functions, at which point this goes away.
    """
    assert failures == 0, f"{failures} check(s) failed — see the captured output above"
