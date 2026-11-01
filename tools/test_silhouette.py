"""The far silhouette: closed, boxed, coloured, and inside the mesh's own box.

Run: `python tools/test_silhouette.py`; pytest collects `gate()` (tools/conftest.py).
"""

import numpy
from csiibundle import silhouette


def _open_box(hole=False):
    """A 10 × 6 × 8 shell with no floor; optionally one wall missing a 2 m window."""
    p = [[0, 0, 0], [10, 0, 0], [10, 6, 0], [0, 6, 0], [0, 0, 8], [10, 0, 8], [10, 6, 8], [0, 6, 8]]
    quads = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
    tris = []
    for a, b, c, d in quads:
        tris += [[a, b, c], [a, c, d]]
    if hole:
        # replace the front wall by two panels leaving a 2 × 2 m hole in the middle
        tris = [t for t in tris if set(t) - {0, 1, 5, 4}]
        p += [
            [4, 0, 0],
            [6, 0, 0],
            [4, 0, 8],
            [6, 0, 8],
            [4, 0, 3],
            [6, 0, 3],
            [4, 0, 5],
            [6, 0, 5],
        ]
        for a, b, c, d in ((0, 8, 10, 4), (9, 1, 5, 11), (8, 9, 13, 12), (14, 15, 11, 10)):
            tris += [[a, b, c], [a, c, d]]
    return numpy.array(p, dtype=float), numpy.array(tris)


def _watertight(tris):
    edges = {}
    for a, b, c in tris:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (min(u, v), max(u, v))
            edges[key] = edges.get(key, 0) + 1
    return edges


def test_box_is_twelve_triangles_inside_its_bounds():
    verts, tris = _open_box()
    v, n, c, _t, stats = silhouette.silhouette(
        verts, tris, numpy.zeros(len(tris), int), [[0.8, 0.2, 0.2]]
    )
    assert stats["triangles"] == 12  # greedy meshing: one rectangle per side
    assert v.min(axis=0).tolist() == [0.0, 0.0, 0.0]
    assert v.max(axis=0).tolist() == [10.0, 6.0, 8.0]  # clamped to the source box
    assert c.shape == (len(v), 4) and numpy.allclose(c[:, :3], [0.8, 0.2, 0.2])
    # every normal is axis-aligned and unit
    assert numpy.allclose(numpy.abs(n).sum(axis=1), 1.0)


def test_hole_and_open_bottom_are_sealed():
    verts, tris = _open_box(hole=True)
    _v, _n, _c, _t, stats = silhouette.silhouette(
        verts, tris, numpy.zeros(len(tris), int), [[1, 1, 1]], cells=8
    )
    # the window is smaller than a cell? no: 2 m against a 1.25 m cell — it is the interior fill
    # that closes it, because the shell around the hole encloses the volume
    assert stats["triangles"] == 12
    # the solid fills the whole box: every interior cell is solid
    occupied, _ids = silhouette.voxelize(
        verts,
        tris,
        numpy.zeros(len(tris), int),
        stats["cell"],
        verts.min(axis=0),
        tuple(stats["grid"]),
    )
    solid = silhouette.fill_from_outside(occupied)
    assert solid.all()


def test_two_colours_split_a_face():
    verts, tris = _open_box()
    ids = numpy.zeros(len(tris), int)
    ids[:2] = 1  # the front wall is colour 1
    v, _n, c, _t, _stats = silhouette.silhouette(verts, tris, ids, [[0, 0, 1], [1, 0, 0]])
    front = c[numpy.isclose(v[:, 1], 0.0)]
    assert len(front) and numpy.allclose(front[:, :3], [1, 0, 0])


def test_empty_input():
    _v, _n, _c, t, stats = silhouette.silhouette(
        numpy.zeros((0, 3)), numpy.zeros((0, 3), int), [], []
    )
    assert len(t) == 0 and stats["cells"] == 0


def main():
    test_box_is_twelve_triangles_inside_its_bounds()
    test_hole_and_open_bottom_are_sealed()
    test_two_colours_split_a_face()
    test_empty_input()
    print("  ok   silhouette: boxed, sealed, coloured, clamped")
    return 0


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_silhouette.py`."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
