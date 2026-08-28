"""cs2beamng A2 gate — the COLLADA writer and the frame conversion.

The materials half (material_v15, texture export, the compiler's auto-generated placeholders)
is `tools/test_a2_materials.py` (refactor Phase 4).

Run: `python tools/test_a2_dae.py` (~5 s, no bundle needed beyond docs/example-bundle).

Checks: a written `.dae` parses, carries the vanilla node hierarchy, one geometry per distinct
mesh however many nodes instance it, `<triangles material>` per run with the shared-offset
`<p>`, and round-trips positions/normals/uvs/indices; `glb_to_local` maps a bundle `.glb`'s
glTF vertices to BeamNG Z-up with winding preserved (a triangle facing bundle +Y faces BeamNG
+Z), and `glb_uvs` flips V; `material_v15` emits alphaTest/translucent/doubleSided/palette
correctly; `export_texture` caps size and drops alpha unless asked; the example bundle's
cube goes through the writer and back.
"""

import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
NS = "{http://www.collada.org/2005/11/COLLADASchema}"

FAILS = []
CHECKS = 0


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def _read_dae(path):
    root = ET.parse(path).getroot()
    geoms = {}
    for g in root.iter(NS + "geometry"):
        sources = {}
        for src in g.iter(NS + "source"):
            arr = src.find(NS + "float_array")
            acc = src.find(f"{NS}technique_common/{NS}accessor")
            stride = int(acc.get("stride"))
            vals = numpy.array(arr.text.split(), dtype=numpy.float64).reshape(-1, stride)
            sources[src.get("id")] = vals
        tris = []
        runs = []
        for t in g.iter(NS + "triangles"):
            p = numpy.array(t.find(NS + "p").text.split(), dtype=numpy.int64)
            inputs = t.findall(NS + "input")
            offsets = {int(i.get("offset")) for i in inputs}
            stride = max(offsets) + 1
            idx = p.reshape(-1, 3 * stride)[:, ::stride]
            tris.append(idx)
            runs.append((t.get("material"), int(t.get("count"))))
        geoms[g.get("id")] = {
            "sources": sources,
            "tris": numpy.concatenate(tris) if tris else None,
            "runs": runs,
        }
    nodes = [n.get("id") for n in root.iter(NS + "node")]
    return root, geoms, nodes


def test_writer(tmp):
    from cs2beamng.beamng import dae

    print("dae writer")
    verts = numpy.array([[0, 0, 0], [2, 0, 0], [2, 3, 0], [0, 3, 0], [1, 1, 5]], float)
    tris = numpy.array([[0, 1, 2], [0, 2, 3], [0, 1, 4], [1, 2, 4]])
    normals = numpy.tile([0, 0, 1.0], (5, 1))
    uvs = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5]])
    colors = numpy.ones((5, 4))
    mesh = dae.Mesh(
        "thing", verts, tris, [("brick", 2), ("roof", 2)], normals=normals, uvs=uvs, colors=colors
    )
    lod = dae.Mesh(
        "thing_lod", verts[:4], tris[:2], [("brick", 2)], normals=normals[:4], uvs=uvs[:4]
    )
    col = dae.Mesh("thing_col", verts[:4], tris[:2], [("brick", 2)])
    shape = dae.Shape(
        "thing", [(300, [mesh]), (60, [lod])], collision=[col], billboard_px=25, null_px=6
    )
    path = os.path.join(tmp, "thing.dae")
    stats = dae.write_shape(path, shape)
    check(stats["materials"] == ["brick", "roof"] and stats["triangles"] == 8, f"stats {stats}")
    root, geoms, nodes = _read_dae(path)
    check(root.find(f"{NS}asset/{NS}up_axis").text == "Z_UP", "Z_UP")
    check(
        nodes[:4] == ["base00", "bb_autobillboard25", "nulldetail6", "start01"]
        and "thing_a300" in nodes
        and "thing_a60" in nodes
        and "Colmesh_thing-1" in nodes,
        f"vanilla hierarchy {nodes}",
    )
    check(len(geoms) == 3, f"three geometries ({len(geoms)})")
    g = geoms["thing-mesh"]
    pos = g["sources"]["thing-mesh-positions"]
    check(numpy.allclose(pos, verts, atol=1e-4), "positions round-trip")
    check(numpy.allclose(g["sources"]["thing-mesh-map-0"], uvs, atol=1e-5), "uvs round-trip")
    check(numpy.array_equal(g["tris"], tris), "indices round-trip (shared offset)")
    check(g["runs"] == [("brick-material", 2), ("roof-material", 2)], f"material runs {g['runs']}")
    mats = [m.get("name") for m in root.iter(NS + "material")]
    check(mats == ["brick", "roof"], f"materials by name {mats}")
    # same mesh in two nodes -> one geometry
    shape2 = dae.Shape("dup", [(300, [mesh]), (60, [mesh])], collision=[mesh])
    dae.write_shape(os.path.join(tmp, "dup.dae"), shape2)
    _r, geoms2, nodes2 = _read_dae(os.path.join(tmp, "dup.dae"))
    check(
        len(geoms2) == 1 and nodes2.count("dup_a300") == 1 and "Colmesh_dup-1" in nodes2,
        "one geometry, three nodes",
    )
    # group_by_material reorders interleaved runs
    inter = dae.Mesh("i", verts, tris, [("a", 1), ("b", 1), ("a", 1), ("b", 1)])
    grouped = dae.group_by_material(inter)
    check(
        grouped.runs == [("a", 2), ("b", 2)] and len(grouped.tris) == 4,
        f"group_by_material {grouped.runs}",
    )


def test_split(tmp):
    """`dae.split_mesh`: a geometry over the per-mesh cap becomes several under it, triangles and
    materials preserved, and the shape's nodes still END in `_a<px>` (load #10 shards)."""
    from cs2beamng.beamng import dae, validate_level

    print("dae split (per-geometry vertex cap)")
    rng = numpy.random.default_rng(7)
    n_v = 150_000
    verts = rng.random((n_v, 3)) * 200.0
    normals = numpy.tile([0, 0, 1.0], (n_v, 1))
    uvs = rng.random((n_v, 2))
    # a triangle strip through every vertex, plus a second material's fan over the first 3,000
    strip = numpy.stack(
        [numpy.arange(n_v - 2), numpy.arange(1, n_v - 1), numpy.arange(2, n_v)], axis=1
    )
    fan = numpy.stack(
        [numpy.zeros(2_998, int), numpy.arange(1, 2_999), numpy.arange(2, 3_000)], axis=1
    )
    tris = numpy.concatenate([strip, fan])
    mesh = dae.Mesh(
        "big", verts, tris, [("asphalt", len(strip)), ("kerb", len(fan))], normals=normals, uvs=uvs
    )
    parts = dae.split_mesh(mesh, 60_000)
    check(
        len(parts) >= 3 and all(len(p.verts) <= 60_000 for p in parts),
        f"{len(parts)} parts, max verts {max(len(p.verts) for p in parts)}",
    )
    check(sum(len(p.tris) for p in parts) == len(tris), "every triangle kept")
    # geometry preserved: each part's triangles, mapped back through its own vertex subset, are the originals
    ok = True
    world = numpy.concatenate([p.verts[p.tris.ravel()] for p in parts]).reshape(-1, 3, 3)
    orig = verts[tris.ravel()].reshape(-1, 3, 3).astype(numpy.float32)
    ok = numpy.allclose(
        numpy.sort(world.reshape(len(world), -1), axis=0),
        numpy.sort(orig.reshape(len(orig), -1), axis=0),
        atol=1e-4,
    )
    check(ok, "triangle corners preserved (as a multiset)")
    check(
        [m for p in parts for m, _c in p.runs].count("kerb") == 1
        and parts[-1].runs[-1][0] == "kerb",
        "material runs stay contiguous and ordered",
    )
    check(
        dae.split_mesh(dae.Mesh("small", verts[:100], tris[:50], [("asphalt", 50)]), 60_000)[0].name
        == "small",
        "a mesh under the cap is returned as is",
    )
    shape = dae.Shape("big", [(2, parts)])
    path = os.path.join(tmp, "big.dae")
    dae.write_shape(path, shape)
    _root, geoms, nodes = _read_dae(path)
    check(len(geoms) == len(parts), f"{len(geoms)} geometries written")
    lod_nodes = [n for n in nodes if n.startswith("big")]
    check(
        lod_nodes[0] == "big_a2"
        and all(n.endswith("_a2") for n in lod_nodes)
        and "big_p1_a2" in lod_nodes,
        f"part nodes keep the trailing detail size: {lod_nodes[:3]}",
    )
    # the validator flags a geometry over the cap
    over = dae.Mesh("over", verts[:70_000], strip[:60_000], [("asphalt", 60_000)])
    level_dir = os.path.join(tmp, "over_level")
    os.makedirs(os.path.join(level_dir, "art", "shapes", "x"), exist_ok=True)
    dae.write_shape(
        os.path.join(level_dir, "art", "shapes", "x", "over.dae"), dae.Shape("over", [(2, [over])])
    )
    from cs2beamng.beamng.level import Level

    lvl = Level(level_dir, "over_level", "Over")
    lvl.add(
        ["Networks"],
        {
            "name": "over",
            "class": "TSStatic",
            "shapeName": lvl.game_path("art", "shapes", "x", "over.dae"),
            "position": [0, 0, 0],
            "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            "scale": [1, 1, 1],
        },
        key="over",
    )
    lvl.write_objects()
    checks, problems = validate_level.check(level_dir, EXAMPLE)
    check(
        checks.get("oversizeGeometries") == 1 and any("exceeds" in p for p in problems),
        f"validator flags the oversize geometry ({checks.get('oversizeGeometries')})",
    )


def test_frame():
    from cs2beamng.beamng import dae

    print("frame")
    # a triangle in bundle frame facing +Y (up): bundle verts (0,0,0),(1,0,0),(0,0,1) — CCW seen from +Y?
    # bundle is left-handed; the .glb stores mirrored X with reversed winding. Take a glb triangle
    # and check the BeamNG result: normal from the transformed winding must point +Z (up).
    glb_verts = numpy.array(
        [[0, 0, 0], [-1, 0, 0], [0, 0, 1]], float
    )  # mirrored X of bundle (0,0,0),(1,0,0),(0,0,1)
    local, _ = dae.glb_to_local(glb_verts)
    check(
        numpy.allclose(local, [[0, 0, 0], [1, 0, 0], [0, 1, 0]]),
        f"(-gx, gz, gy) mapping {local.tolist()}",
    )
    a, b, c = local
    n = numpy.cross(b - a, c - a)
    # glTF winding for this triangle (as the producer wrote it, reversed) is stored order 0,1,2; the
    # producer reversed winding so that in glTF the face is CCW seen from its outside. In glTF
    # space the normal of (0,1,2) is cross((-1,0,0),(0,0,1)) = (0,1,0)... = +Y up in glTF = up. After
    # the mapping the same order gives +Z. Both reflections cancel: winding kept.
    check(n[2] > 0, f"winding preserved: face normal points BeamNG up ({n.tolist()})")
    uv = dae.glb_uvs(numpy.array([[0.25, 0.1]]))
    check(numpy.allclose(uv, [[0.25, 0.9]]), "glb_uvs flips V")
    nrm_in = numpy.array([[0, 1, 0]], float)
    _l, nn = dae.glb_to_local(glb_verts[:1], nrm_in)
    check(numpy.allclose(nn, [[0, 0, 1]]), "normals map like positions (glTF +Y -> BeamNG +Z)")


def test_example_cube(tmp):
    from cs2beamng.beamng import dae
    from csiibundle import glb

    print("example cube through the writer")
    nodes = glb.parse_glb(os.path.join(EXAMPLE, "meshes", "carto_example_cube.glb"))
    check(nodes is not None and len(nodes) >= 1, "example cube parses")
    node = nodes[0]
    local, nn = dae.glb_to_local(node["verts"], node["normals"], node["matrix"])
    mesh = dae.Mesh(
        "cube", local, node["tris"], node["material_runs"], normals=nn, uvs=dae.glb_uvs(node["uvs"])
    )
    stats = dae.write_shape(os.path.join(tmp, "cube.dae"), dae.Shape("cube", [(200, [mesh])]))
    _r, geoms, _n = _read_dae(os.path.join(tmp, "cube.dae"))
    pos = geoms["cube-mesh"]["sources"]["cube-mesh-positions"]
    check(
        stats["triangles"] == len(node["tris"]) and len(pos) == len(node["verts"]),
        "cube written whole",
    )
    check(
        pos[:, 2].max() - pos[:, 2].min() > 0 and abs(pos[:, 2].max()) > 0,
        "cube has height along BeamNG Z",
    )


def main():
    tmp = tempfile.mkdtemp(prefix="cs2beamng_a2_")
    try:
        test_writer(tmp)
        test_split(tmp)
        test_frame()
        test_example_cube(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a2_dae.py`."""
    assert main() in (None, 0)
