"""Gate for `cs2beamng.beamng.daeread` — the COLLADA reader against the writer beside it.

Run: `python tools/test_daeread.py` (instant, no BeamNG level and no Blender needed).

The reader spent its life in `tools/blender_import_level.py`, parsing a format written by a
module in a different package with no shared schema and no test between them. Refactor Phase 6
moved it to `beamng/daeread.py`; this is what that move buys — `dae.write_shape` produces the
input, `daeread.read_shape` consumes it, and every claim below is a round trip rather than a
fixture someone typed out.

The consumer of all this is the level oracle (`tools/blender_import_level.py`), which answers
"what will the game draw here" by reading what BeamNG reads. It is only an oracle if it reads
the file the same way the game does, so the LOD filtering and the material-run mapping matter
as much as the vertices.
"""

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "apps"))

import numpy  # noqa: E402
from cs2beamng.beamng import dae, daeread  # noqa: E402

CHECKS = 0
FAILS = []


def check(ok, claim):
    """One claim, counted; a failure is recorded and the run continues."""
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(claim)
    print(f"  {'ok  ' if ok else 'FAIL'}  {claim}")


QUAD_VERTS = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 3.0, 0.0), (2.0, 3.0, 1.5)]
QUAD_TRIS = [(0, 1, 2), (1, 3, 2)]
QUAD_UVS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]


def _mesh(name="m", runs=(("road_asphalt", 2),), **kwargs):
    return dae.Mesh(name=name, verts=QUAD_VERTS, tris=QUAD_TRIS, runs=list(runs), **kwargs)


def _round_trip(shape):
    """Write `shape` to a temp `.dae` and read it straight back."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "shape.dae")
        dae.write_shape(path, shape)
        return daeread.read_shape(path)


def test_a_single_mesh_round_trips():
    """Positions, triangles and UVs survive the write and the read."""
    parts = _round_trip(dae.Shape("thing", [(2, [_mesh(uvs=QUAD_UVS)])]))
    check(len(parts) == 1, f"one detail node, one part (got {len(parts)})")
    name, verts, _normals, uvs, tris, _runs, matrix = parts[0]
    check(name == "thing_a2", f"the node is named `<shape>_a<px>` (got {name!r})")
    check(len(tris) == 2, f"two triangles (got {len(tris)})")
    # Corners are un-shared, so six vertices come back for four written — the reader's
    # documented behaviour, and what keeps a file with disagreeing input offsets correct.
    check(len(verts) == 6, f"the two triangles come back as six corners (got {len(verts)})")
    written = numpy.asarray(QUAD_VERTS, dtype=numpy.float64)[numpy.asarray(QUAD_TRIS).ravel()]
    check(numpy.allclose(verts, written, atol=1e-5), "every corner is where the writer put it")
    check(uvs is not None and len(uvs) == 6, "the UVs come back per corner too")
    check(numpy.allclose(matrix, numpy.eye(4)), "a shape with no node transform reads as identity")


def test_material_runs_resolve_to_level_names():
    """A `<triangles>` block names a material SYMBOL; the reader resolves it through
    `<instance_material>` to the target the level's `.materials.json` knows."""
    mesh = _mesh(runs=(("road_asphalt", 1), ("kerb_concrete", 1)))
    parts = _round_trip(dae.Shape("two", [(4, [mesh])]))
    runs = parts[0][5]
    check(len(runs) == 2, f"two material runs (got {len(runs)})")
    check([count for _t, count in runs] == [1, 1], "each covering one triangle")
    names = [target for target, _c in runs]
    check(
        all("road_asphalt" in n or "kerb_concrete" in n for n in names),
        f"and naming the writer's own materials (got {names})",
    )


def test_nearest_detail_keeps_one_level():
    """A shape with an LOD ladder writes a node per level. Importing all of them stacks three
    copies of the same tree on top of each other, so the oracle keeps the LARGEST `_a<px>` —
    the nearest — and drops the rest."""
    shape = dae.Shape("tree", [(8, [_mesh("near")]), (4, [_mesh("mid")]), (2, [_mesh("far")])])
    parts = _round_trip(shape)
    check(len(parts) == 3, f"all three levels are in the file (got {len(parts)})")
    kept = daeread.nearest_detail(parts)
    check(len(kept) == 1, f"one survives the filter (got {len(kept)})")
    check(kept[0][0].endswith("_a8"), f"and it is the largest px (got {kept[0][0]!r})")


def test_nearest_detail_drops_the_non_visual_nodes():
    """`Colmesh_*` is collision, `bb_autobillboard<px>` is the engine's own imposter and
    `nulldetail<px>` is "draw nothing below this". None of the three is geometry to look at."""
    shape = dae.Shape(
        "thing",
        [(4, [_mesh()])],
        collision=[_mesh("col")],
        billboard_px=2,
        null_px=1,
    )
    parts = _round_trip(shape)
    names = [p[0] for p in parts]
    check(any("Colmesh" in n for n in names), f"the collision node is in the file ({names})")
    kept = [p[0] for p in daeread.nearest_detail(parts)]
    check(not any("Colmesh" in n for n in kept), f"and the filter drops it (kept {kept})")
    check(not any("bb_autobillboard" in n for n in kept), "the imposter placeholder too")
    check(not any("nulldetail" in n for n in kept), "and the null-detail placeholder")


def test_a_shape_with_no_ladder_is_kept_whole():
    """No `_a<px>` anywhere means nothing to choose between — keep every visual node rather
    than returning an empty scene."""
    parts = [("plain_node", None, None, None, None, [], None)]
    check(daeread.nearest_detail(parts) == parts, "a shape with no detail suffix is unfiltered")


def test_node_matrices_compose_down_the_tree():
    """`base00 -> start01 -> <name>_a<px>` is three levels deep, so a part's matrix is the
    product down the branch, not the leaf's own."""
    parts = _round_trip(dae.Shape("nested", [(2, [_mesh()])]))
    check(parts[0][6].shape == (4, 4), "every part carries a 4x4")
    check(
        numpy.allclose(parts[0][6][:3, :3], numpy.eye(3)),
        "identity here, because the writer's own scene tree carries no rotations",
    )


def gate():
    """Every claim above, as one pytest test."""
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            print(f"\n{name}")
            function()
    print(f"\n{CHECKS} checks, {len(FAILS)} failed")
    for claim in FAILS:
        print(f"  FAILED: {claim}")
    assert not FAILS, f"{len(FAILS)} of {CHECKS} daeread claims failed"


if __name__ == "__main__":
    gate()
