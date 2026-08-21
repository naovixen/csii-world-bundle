"""cs2beamng A2 gate — the COLLADA writer, the material builder, and the frame conversion.

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


def test_materials(tmp):
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.beamng import materials as bm
    from csiibundle import png

    print("materials v1.5 + texture export")
    lv = level_mod.Level(os.path.join(tmp, "lvl"), "lvl", "L")
    rec = {
        "alphaMode": "mask",
        "alphaCutoff": 0.4,
        "doubleSided": True,
        "tintable": True,
        "baseColorFactor": [1, 1, 1, 1],
    }
    mat = bm.material_v15(
        lv,
        "lvl_x",
        rec,
        {
            "baseColor": "/a.color.png",
            "normal": "/n.normal.png",
            "colorMask": "/m.data.png",
            "roughness": "/r.data.png",
            "opacity": "/a_o.data.png",
        },
    )
    st = mat["Stages"][0]
    check(
        mat["alphaTest"] is True and mat["alphaRef"] == 102 and mat["doubleSided"] is True,
        "mask -> alphaTest/alphaRef/doubleSided",
    )
    check(st["opacityMap"] == "/a_o.data.png", "opacityMap bound (v1.5 opacity source)")
    mat3 = bm.material_v15(
        lv,
        "lvl_z",
        {"alphaMode": "blend", "baseColorFactor": [1, 1, 1, 0.5]},
        {"baseColor": "/a.color.png", "opacity": "/o.png"},
    )
    check(
        mat3["Stages"][0].get("opacityFactor") == 0.5 and "diffuseColor" not in mat3["Stages"][0],
        "factor alpha -> opacityFactor",
    )
    p3 = (
        bm.export_texture(lv, os.path.join(tmp, "t.png"), "opacity", 32)
        if os.path.isfile(os.path.join(tmp, "t.png"))
        else None
    )
    check(
        st["colorPaletteMap"] == "/m.data.png"
        and st["roughnessMap"] == "/r.data.png"
        and mat["mapTo"] == "lvl_x",
        "maps + palette + mapTo",
    )
    # export_texture is idempotent: a second export of the same source leaves the file (and its
    # mtime, BeamNG's cooked-cache key) alone; a different cap replaces it
    src_i = os.path.join(tmp, "idem.png")
    from PIL import Image as _Image

    _Image.fromarray(numpy.full((64, 64, 3), 90, numpy.uint8)).save(src_i)
    p1 = bm.export_texture(lv, src_i, "color", 64)
    f1 = os.path.join(lv.dir, "art", "textures", os.path.basename(p1))
    st1 = os.stat(f1)
    import time as _time

    _time.sleep(0.05)
    bm.export_texture(lv, src_i, "color", 64)
    st2 = os.stat(f1)
    check(
        st1.st_mtime_ns == st2.st_mtime_ns and st1.st_size == st2.st_size,
        "unchanged texture is not rewritten (mtime kept)",
    )
    bm.export_texture(lv, src_i, "color", 32)
    st3 = os.stat(f1)
    check(st3.st_size != st1.st_size, "a different cap replaces the file")
    mat2 = bm.material_v15(
        lv,
        "lvl_y",
        {"alphaMode": "blend", "roughness": 0.3, "metallic": 0.1},
        {"baseColor": "/a.color.png"},
    )
    check(
        mat2.get("translucent") is True
        and mat2["Stages"][0]["roughnessFactor"] == 0.3
        and mat2["Stages"][0]["metallicFactor"] == 0.1,
        "blend -> translucent; scalar factors",
    )
    check(bm.mapto("lvl", "area:Foo Bar") == "lvl_area_Foo_Bar", "mapto sanitises and prefixes")
    # texture export: RGBA 64x64 with alpha, cap 32
    src = os.path.join(tmp, "t.png")
    arr = numpy.zeros((64, 64, 4), numpy.uint8)
    arr[..., 0] = 200
    arr[..., 3] = 30
    with open(src, "wb") as f:
        f.write(png.encode_rgba(arr))
    p1 = bm.export_texture(lv, src, "color", 32, keep_alpha=False, dds=False)
    bm.export_texture(lv, src, "color", 32, keep_alpha=True, dds=False)
    d1 = open(lv.path("art", "textures", "t.color.png"), "rb").read()
    d2 = open(lv.path("art", "textures", "t_a.color.png"), "rb").read()
    import struct

    check(
        struct.unpack(">II", d1[16:24]) == (32, 32) and d1[25] == 2, "capped to 32 and written RGB"
    )
    check(struct.unpack(">II", d2[16:24]) == (32, 32) and d2[25] == 6, "keep_alpha writes RGBA")
    check(
        p1.endswith(".color.png") and p1.startswith("/levels/lvl/art/textures/"), f"game path {p1}"
    )
    p3 = bm.export_texture(lv, src, "opacity", 32, dds=False)
    d3 = open(lv.path("art", "textures", "t_o.data.png"), "rb").read()
    check(
        p3.endswith("_o.data.png") and d3[25] == 0 and struct.unpack(">II", d3[16:24]) == (32, 32),
        "opacity map = grey of the alpha",
    )
    # the DDS path (default when etcpak is installed): the cooker's own formats + a full mip chain
    from cs2beamng.beamng import dds as dds_mod

    if not dds_mod.available():
        print("  (etcpak not installed: DDS export untested here)")
        return
    import io as _io

    from PIL import Image as _Image

    nsrc = os.path.join(tmp, "n.png")
    narr = numpy.zeros((64, 32, 3), numpy.uint8)
    narr[..., 0] = 128
    narr[..., 1] = 200
    narr[..., 2] = 255
    _Image.fromarray(narr).save(nsrc)
    gsrc = os.path.join(tmp, "g.png")
    garr = numpy.broadcast_to(
        (numpy.arange(64) * 4).astype(numpy.uint8), (64, 64)
    ).copy()  # a smooth ramp
    with open(gsrc, "wb") as f:
        f.write(png.encode_gray(garr, bit_depth=8))
    q1 = bm.export_texture(lv, src, "color", 0)
    q2 = bm.export_texture(lv, src, "opacity", 0)
    q3 = bm.export_texture(lv, nsrc, "normal", 0)
    q4 = bm.export_texture(lv, gsrc, "data", 0)
    q5 = bm.export_texture(lv, src, "data", 0)
    check(
        q1.endswith("t.color.dds")
        and q2.endswith("t_o.data.dds")
        and q3.endswith("n.normal.dds")
        and q4.endswith("g.data.dds"),
        f"dds names {q1} {q2} {q3} {q4}",
    )
    hdrs = {}
    for q in (q1, q2, q3, q4, q5):
        data = open(lv.path(*q.split("/")[3:]), "rb").read()
        hdrs[q] = dds_mod.read_header(data)
        im = _Image.open(_io.BytesIO(data))
        im.load()
        hdrs[q] += (numpy.asarray(im.convert("RGBA")),)
    check(
        hdrs[q1][:4] == (64, 64, 7, dds_mod.BC7_UNORM_SRGB),
        f"colour -> BC7 sRGB, 7 mips {hdrs[q1][:4]}",
    )
    check(hdrs[q2][:4] == (64, 64, 7, dds_mod.BC4_UNORM), f"opacity -> BC4 {hdrs[q2][:4]}")
    check(
        hdrs[q3][:4] == (32, 64, 7, dds_mod.BC5_UNORM),
        f"normal -> BC5, mips to 1x1 from 32x64 {hdrs[q3][:4]}",
    )
    check(hdrs[q4][:4] == (64, 64, 7, dds_mod.BC4_UNORM), f"grey data -> BC4 {hdrs[q4][:4]}")
    check(hdrs[q5][:4] == (64, 64, 7, dds_mod.BC7_UNORM), f"RGB data -> BC7 linear {hdrs[q5][:4]}")
    check(
        abs(int(hdrs[q1][4][..., 0].mean()) - 200) <= 2 and hdrs[q1][4][..., 3].min() >= 254,
        "colour BC7 round-trips (alpha forced opaque)",
    )
    check(abs(int(hdrs[q2][4][..., 0].mean()) - 30) <= 1, "opacity BC4 carries the albedo alpha")
    check(
        abs(int(hdrs[q3][4][..., 0].mean()) - 128) <= 1
        and abs(int(hdrs[q3][4][..., 1].mean()) - 200) <= 1,
        "normal BC5 carries R and G",
    )
    check(
        numpy.abs(hdrs[q4][4][..., 0].astype(int) - garr.astype(int)).mean() < 2.0,
        "grey ramp survives BC4",
    )
    size_ok = len(open(lv.path(*q1.split("/")[3:]), "rb").read()) == 148 + 16 * (
        256 + 64 + 16 + 4 + 1 + 1 + 1
    )
    check(size_ok, "BC7 file = header + 7 mip levels of whole 4x4 blocks")
    # write-if-changed holds for DDS too
    f1 = lv.path(*q1.split("/")[3:])
    s1 = os.stat(f1)
    _time.sleep(0.05)
    bm.export_texture(lv, src, "color", 0)
    s2 = os.stat(f1)
    check(s1.st_mtime_ns == s2.st_mtime_ns, "unchanged DDS is not rewritten")


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


def test_autogenerated_materials(tmp):
    """The headless compiler writes opaque placeholder `main.materials.json` files beside the
    shapes for names it never loaded (load #10: crosswalks / "45" / lane lines as light boxes).
    The cook stage removes them; the validator refuses a level that still has one, or any
    material name defined twice."""
    import json

    from cs2beamng.beamng import validate_level
    from cs2beamng.stages import cook

    print("auto-generated materials beside the shapes")
    level = os.path.join(tmp, "lvl")
    shapes = os.path.join(level, "art", "shapes", "markings")
    os.makedirs(shapes)
    os.makedirs(os.path.join(level, "art", "cs2"))
    ours = {
        "lvl_LaneLine": {
            "name": "lvl_LaneLine",
            "mapTo": "lvl_LaneLine",
            "class": "Material",
            "version": 1.5,
            "translucent": True,
            "Stages": [{}, {}, {}, {}],
        }
    }
    with open(os.path.join(level, "art", "cs2", "main.materials.json"), "w", encoding="utf-8") as h:
        json.dump(ours, h)
    theirs = {
        "lvl_LaneLine": {
            "name": "lvl_LaneLine",
            "mapTo": "lvl_LaneLine",
            "class": "material",
            "Stages": [{"diffuseColor": [0.8, 0.8, 0.8, 1]}, {}, {}, {}],
            "translucentBlendOp": "None",
        }
    }
    with open(os.path.join(shapes, "main.materials.json"), "w", encoding="utf-8") as h:
        json.dump(theirs, h)
    with open(os.path.join(shapes, "markings_0_0.dae"), "w", encoding="utf-8") as h:
        h.write(
            '<?xml version="1.0"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema"><asset><up_axis>Z_UP</up_axis></asset>'
            '<library_materials><material id="lvl_LaneLine-material" name="lvl_LaneLine"/></library_materials></COLLADA>'
        )
    checks, problems = validate_level.check(level, EXAMPLE)
    check(
        checks.get("strayMaterialFiles") == 1,
        "validator counts the stray materials file under art/shapes/",
    )
    check(checks.get("duplicateMaterials") == 1, "validator counts the shadowed name")
    check(
        any("art/shapes/markings/main.materials.json" in p for p in problems),
        "validator reports the stray file",
    )
    check(
        any("defined in 2 files" in p for p in problems),
        "validator reports the duplicate definition",
    )
    removed = cook._remove_autogenerated(os.path.join(level, "art", "shapes"))
    check(
        len(removed) == 1 and removed[0][1] == ["lvl_LaneLine"],
        "cook removes it and names the placeholder",
    )
    check(not os.path.exists(os.path.join(shapes, "main.materials.json")), "the file is gone")
    check(
        os.path.isfile(os.path.join(level, "art", "cs2", "main.materials.json")),
        "ours under art/cs2/ is untouched",
    )
    checks, problems = validate_level.check(level, EXAMPLE)
    check(
        checks.get("strayMaterialFiles") == 0 and checks.get("duplicateMaterials") == 0,
        "validator clean afterwards",
    )


def main():
    tmp = tempfile.mkdtemp(prefix="cs2beamng_a2_")
    try:
        test_writer(tmp)
        test_split(tmp)
        test_frame()
        test_materials(tmp)
        test_example_cube(tmp)
        test_autogenerated_materials(tmp)
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
