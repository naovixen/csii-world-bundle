"""cs2beamng A3 gate, part 2d — Phase 10 residency: the authored chain and the far ring.

Run: `python tools/test_a3_residency.py`. Split from `test_a3_rules.py` (refactor Phase 4): an
authored level identical to LOD 0 is not shipped, the `authored_lod0_max_tris` cap, the
authored LOD limits worked by hand against the game's loop, and the far-ring cell's band and
vertex cap.
"""

import itertools
import os
import sys
import tempfile

import numpy
from a3common import check, finish, start


def _grid_parts(n_quads, material="m", scale=1.0):
    """One mesh part of `2 * n_quads` triangles: a strip of unit quads along x."""
    verts, tris = [], []
    for i in range(n_quads):
        b = len(verts)
        verts += [[i, 0, 0], [i + 1, 0, 0], [i + 1, 1, 0], [i, 1, 0]]
        tris += [[b, b + 1, b + 2], [b, b + 2, b + 3]]
    verts = numpy.array(verts, dtype=numpy.float32) * scale
    normals = numpy.tile(numpy.array([[0, 0, 1]], dtype=numpy.float32), (len(verts), 1))
    uvs = numpy.zeros((len(verts), 2), dtype=numpy.float32)
    return [(verts, normals, uvs, None, numpy.array(tris), [(material, len(tris))])]


def test_residency_chain_rules():
    """Phase 10 residency: an authored level byte-identical to LOD 0 is not shipped, and over
    `authored_lod0_max_tris` the game's LOD 1 becomes the nearest level (LOD 0 left out)."""
    print("residency: the authored chain")
    from cs2beamng.beamng import daemesh
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.stages import shapechain

    size = numpy.array([40.0, 1.0, 10.0])
    area, radius, bias = 400.0, 20.0, 0.0
    base_parts = _grid_parts(15000)  # 30,000 triangles: a heavy building
    base = daemesh.group_by_material(daemesh.merge_meshes("Heavy", base_parts))
    copy = _grid_parts(15000)  # the LOD 1 slot filled with a copy of the full mesh
    real1 = _grid_parts(6000)  # a real LOD 1 at 40 %
    lod2 = _grid_parts(500)
    opts = {"level_of_detail": 0.7, "authored_lods": True}

    chain = shapechain.authored_chain(
        "Heavy", [(1, copy, size), (2, lod2, size)], size, area, bias, radius, 1.0, opts, base
    )
    check(chain["identicalLevels"] == [1], "a LOD 1 identical to LOD 0 is left out")
    check(len(chain["meshes"]) == 1 and len(chain["meshes"][0].tris) == 1000, "...and LOD 2 stays")
    stats = {}
    _fixed, stats, meshes = shapechain.authored_ladder(base, chain, stats)
    check(meshes[0] is base and len(meshes) == 2, "with no real LOD 1 the ladder keeps LOD 0")

    _check_lod0_cap(base, real1, lod2, size, area, bias, radius, opts)

    plain = level_mod.tsstatic("a", "/s.dae", [0, 0, 0], [1, 0, 0, 0, 1, 0, 0, 0, 1])
    tinted = level_mod.tsstatic(
        "b", "/s.dae", [0, 0, 0], [1, 0, 0, 0, 1, 0, 0, 0, 1], instanced=True
    )
    check(plain["useInstanceRenderData"] is False, "a TSStatic with no colour has no instance data")
    check(tinted["useInstanceRenderData"] is True, "...one carrying a colour asks for it")


def _check_lod0_cap(base, real1, lod2, size, area, bias, radius, opts):
    """Over `authored_lod0_max_tris` the game's LOD 1 is the nearest level; under it the old
    rule stands."""
    from cs2beamng.stages import shapechain

    opts_drop = dict(opts, authored_lod0_max_tris=20000, authored_lod1_drop=True)
    chain = shapechain.authored_chain(
        "Heavy", [(1, real1, size), (2, lod2, size)], size, area, bias, radius, 1.0, opts_drop, base
    )
    check(
        chain["lod0Dropped"] and not chain["lod1Dropped"],
        "over the cap LOD 0 goes and LOD 1 is kept",
    )
    check(len(chain["thresholds"]) == len(chain["meshes"]) == 2, "one threshold per shipped level")
    check(chain["colliderMesh"] is chain["meshes"][0], "the collider still comes off LOD 1")
    stats = {}
    _fixed, stats, meshes = shapechain.authored_ladder(base, chain, stats)
    check(
        len(meshes) == 2 and len(meshes[0].tris) == 12000 and stats["lod0Dropped"],
        "the ladder starts at the game's LOD 1 (12,000 triangles), LOD 0 is not in the shape",
    )
    check(stats["lod0Triangles"] == 30000, "...and the report knows what was left out")

    chain = shapechain.authored_chain(
        "Light",
        [(1, real1, size), (2, lod2, size)],
        size,
        area,
        bias,
        radius,
        1.0,
        dict(opts, authored_lod0_max_tris=50000, authored_lod1_drop=True),
        base,
    )
    check(
        not chain["lod0Dropped"] and chain["lod1Dropped"] and len(chain["meshes"]) == 1,
        "under the cap the old rule stands: LOD 0 kept, LOD 1 dropped, LOD 2 keeps its band",
    )


def test_authored_lod_limits_worked_example():
    """`lodchain.authored_lod_limits` against the game's loop, worked by hand for one house.

    `RequiredBatchesSystem.InitializeBatchGroup`: walk the chain COARSEST first, give each level
    the limit in hand, then raise it to `clamp(CalculateLodLimit(size of the level coarser than
    it) + 1, x + 1, 255 - j)`; LOD 0 takes what is left. So the coarsest level's limit is the
    whole mesh's own `m_MinLod`, every nearer level's limit is decided by the level COARSER than
    it, and the limits fall with the index (the lod index falls with distance).
    """
    print("authored LOD limits: the worked example")
    import numpy
    from csiibundle import lod, lodchain

    size = numpy.array([24.0, 12.0, 18.0])  # a 24 x 12 x 18 m house
    levels = [(size, 6000 * 3), (size, 800 * 3)]  # LOD 1 6,000 triangles, LOD 2 800
    limits = lodchain.authored_lod_limits(size, levels, 0.0)
    check(limits == [142, 133, 103], f"the house's limits are [142, 133, 103] (got {limits})")
    check(
        limits[-1] == int(lod.mesh_min_lod(size, 0.0, 0.0)),
        "the coarsest level's limit is the mesh's own m_MinLod - it draws to the game's cull",
    )
    check(all(a > b for a, b in itertools.pairwise(limits)), "limits fall with the index")
    metres = [round(lod.cull_distance(limit)) for limit in limits]
    check(
        metres == [181, 512, 16389],
        f"...which is LOD 0 to 181 m, LOD 1 to 512 m, LOD 2 to 16 km ({metres})",
    )
    check(
        lodchain.authored_lod_limits(size, levels[:1], 0.0) == [142, 103],
        "with one authored level LOD 0 keeps the same limit and the level takes the mesh's",
    )
    check(
        lodchain.authored_lod_limits(size, levels, 0.5) == [145, 136, 106],
        "a bias of 0.5 lifts every limit by three (half an octave = three sixths)",
    )
    _check_far_ring_band()


def _check_far_ring_band():
    """The far ring: two boxes in one 512 m cell, one merged shape, sizes strictly decreasing."""
    import os
    import tempfile

    from cs2beamng.beamng import level as level_mod
    from cs2beamng.stages import farring

    d = tempfile.mkdtemp(prefix="cs2ring_")
    lv = level_mod.Level(os.path.join(d, "demo"), "demo", "Demo")
    lv.set_stage("instances")
    box = numpy.array(
        [
            [0, 0, 0],
            [10, 0, 0],
            [10, 10, 0],
            [0, 10, 0],
            [0, 0, 20],
            [10, 0, 20],
            [10, 10, 20],
            [0, 10, 20],
        ],
        dtype=float,
    )
    tris = [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4]]
    ring_data = {
        "verts": box.tolist(),
        "normals": [[0, 0, 1]] * 8,
        "colors": [[0.5, 0.5, 0.5, 1]] * 8,
        "tris": tris,
        "cullMetres": 9000.0,
    }
    ring = farring.Ring(2500.0, 1.0)
    ring.add(ring_data, [100, 100, 50], [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [1, 1, 1])
    ring.add(ring_data, [300, 200, 50], [[0, -1, 0], [1, 0, 0], [0, 0, 1]], [2, 2, 2])
    report = ring.write(lv, {})
    check(
        len(report["cells"]) == 1 and report["members"] == 2,
        "two boxes 200 m apart share one 512 m cell",
    )
    cell = report["cells"][0]
    check(
        cell["nearPx"] > cell["ringPx"] > cell["nullPx"] >= 1,
        "the cell's sizes: near > ring > null",
    )
    shape = os.path.join(d, "demo", "art", "shapes", "farring", "farring_0_0.dae")
    text = open(shape, encoding="utf-8").read()
    import re

    start = int(re.search(r"_a(\d+)", text).group(1))
    null = int(re.search(r"nulldetail(\d+)", text).group(1))
    check(
        start > null >= 1, f"the cell starts at {start} px and stops at {null} px - a band exists"
    )
    check("COLOR" in text, "the merged mesh carries its vertex colours (lod_vertcol)")


def test_far_ring_cell_is_split_at_the_vertex_cap():
    """A ring cell merges more geometry into one node than any other product, and a geometry
    past `MAX_GEOMETRY_VERTS` shards in-game (load #10) - so it goes through `split_mesh` like
    every other writer's does (validation pass 2026-08-31, B5)."""
    print("a far-ring cell over the vertex cap is split")
    from cs2beamng.beamng import daemesh
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.stages import farring

    d = tempfile.mkdtemp(prefix="cs2ringcap_")
    lv = level_mod.Level(os.path.join(d, "demo"), "demo", "Demo")
    lv.set_stage("instances")
    box = numpy.array(
        [
            [0, 0, 0],
            [10, 0, 0],
            [10, 10, 0],
            [0, 10, 0],
            [0, 0, 20],
            [10, 0, 20],
            [10, 10, 20],
            [0, 10, 20],
        ],
        dtype=float,
    )
    ring_data = {
        "verts": box.tolist(),
        "normals": [[0, 0, 1]] * 8,
        "colors": [[0.5, 0.5, 0.5, 1]] * 8,
        "tris": [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]],
        "cullMetres": 9000.0,
    }
    ring = farring.Ring(2500.0, 1.0)
    members = daemesh.MAX_GEOMETRY_VERTS // 8 + 50  # just over the cap, all inside one 512 m cell
    for i in range(members):
        ring.add(ring_data, [100 + (i % 40), 100 + i // 40, 50], numpy.eye(3).tolist(), [1, 1, 1])
    report = ring.write(lv, {})
    check(
        report["cellCount"] == 1 and len(report["cells"]) == 1,
        f"one cell, counted under its own key ({report['cellCount']} / {len(report['cells'])})",
    )
    text = open(
        os.path.join(d, "demo", "art", "shapes", "farring", "farring_0_0.dae"), encoding="utf-8"
    ).read()
    import re

    # the writer names the first part `<name>_a<px>` and every further one `<name>_p<i>_a<px>`
    parts = re.findall(r'<node id="farring_0_0_p\d+_a\d+"', text)
    check(
        len(parts) >= 1,
        f"the cell's geometry is split into {len(parts) + 1} parts under the vertex cap",
    )


def main():
    start()
    test_far_ring_cell_is_split_at_the_vertex_cap()
    test_residency_chain_rules()
    test_authored_lod_limits_worked_example()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_residency.py`."""
    assert main() in (None, 0)
