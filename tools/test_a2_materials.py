"""cs2beamng A2 gate, materials half — `material_v15`, texture export (PNG and DDS), and the
compiler's auto-generated placeholder files.

Split from `tools/test_a2_dae.py` (refactor Phase 4); the writer/split/frame half stays there.
Run: `python tools/test_a2_materials.py` (~5 s, no bundle needed beyond docs/example-bundle).
"""

import os
import shutil
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")

FAILS = []
CHECKS = 0


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def test_materials(tmp):
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.beamng import materials as bm

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
    check(
        st["colorPaletteMap"] == "/m.data.png"
        and st["roughnessMap"] == "/r.data.png"
        and mat["mapTo"] == "lvl_x",
        "maps + palette + mapTo",
    )
    _check_blend_materials(lv)
    _check_texture_export(tmp, lv)
    _check_dds_export(tmp, lv)


def _check_blend_materials(lv):
    """The blend alpha modes: factor alpha, translucency and the scalar factors."""
    from cs2beamng.beamng import matbind
    from cs2beamng.beamng import materials as bm

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
    check(
        matbind.mapto("lvl", "area:Foo Bar") == "lvl_area_Foo_Bar", "mapto sanitises and prefixes"
    )


def _check_texture_export(tmp, lv):
    """PNG export: idempotence, the size cap, alpha handling and the opacity grey."""
    from cs2beamng.beamng import textureplan
    from csiibundle import png

    # export_texture is idempotent: a second export of the same source leaves the file (and its
    # mtime, BeamNG's cooked-cache key) alone; a different cap replaces it
    src_i = os.path.join(tmp, "idem.png")
    from PIL import Image as _Image

    _Image.fromarray(numpy.full((64, 64, 3), 90, numpy.uint8)).save(src_i)
    p1 = textureplan.export_texture(lv, src_i, "color", 64)
    f1 = os.path.join(lv.dir, "art", "textures", os.path.basename(p1))
    st1 = os.stat(f1)
    import time as _time

    _time.sleep(0.05)
    textureplan.export_texture(lv, src_i, "color", 64)
    st2 = os.stat(f1)
    check(
        st1.st_mtime_ns == st2.st_mtime_ns and st1.st_size == st2.st_size,
        "unchanged texture is not rewritten (mtime kept)",
    )
    textureplan.export_texture(lv, src_i, "color", 32)
    st3 = os.stat(f1)
    check(st3.st_size != st1.st_size, "a different cap replaces the file")
    # texture export: RGBA 64x64 with alpha, cap 32
    src = os.path.join(tmp, "t.png")
    arr = numpy.zeros((64, 64, 4), numpy.uint8)
    arr[..., 0] = 200
    arr[..., 3] = 30
    with open(src, "wb") as f:
        f.write(png.encode_rgba(arr))
    p1 = textureplan.export_texture(lv, src, "color", 32, keep_alpha=False, dds=False)
    textureplan.export_texture(lv, src, "color", 32, keep_alpha=True, dds=False)
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
    p3 = textureplan.export_texture(lv, src, "opacity", 32, dds=False)
    d3 = open(lv.path("art", "textures", "t_o.data.png"), "rb").read()
    check(
        p3.endswith("_o.data.png") and d3[25] == 0 and struct.unpack(">II", d3[16:24]) == (32, 32),
        "opacity map = grey of the alpha",
    )


def _dds_sources(tmp):
    """An RGBA albedo, a normal-ish map, and a smooth grey ramp for the compressors."""
    from csiibundle import png
    from PIL import Image as _Image

    src = os.path.join(tmp, "t.png")
    arr = numpy.zeros((64, 64, 4), numpy.uint8)
    arr[..., 0] = 200
    arr[..., 3] = 30
    with open(src, "wb") as f:
        f.write(png.encode_rgba(arr))
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
    return src, nsrc, gsrc, garr


def _read_dds(lv, dds_mod, qs):
    """Header + decoded RGBA per written file, keyed by game path."""
    import io as _io

    from PIL import Image as _Image

    hdrs = {}
    for q in qs:
        data = open(lv.path(*q.split("/")[3:]), "rb").read()
        hdrs[q] = dds_mod.read_header(data)
        im = _Image.open(_io.BytesIO(data))
        im.load()
        hdrs[q] += (numpy.asarray(im.convert("RGBA")),)
    return hdrs


def _check_dds_export(tmp, lv):
    """The DDS path (default when etcpak is installed): the cooker's own formats + mip chains."""
    from cs2beamng.beamng import dds as dds_mod
    from cs2beamng.beamng import textureplan

    if not dds_mod.available():
        print("  (etcpak not installed: DDS export untested here)")
        return
    import time as _time

    src, nsrc, gsrc, garr = _dds_sources(tmp)
    q1 = textureplan.export_texture(lv, src, "color", 0)
    q2 = textureplan.export_texture(lv, src, "opacity", 0)
    q3 = textureplan.export_texture(lv, nsrc, "normal", 0)
    q4 = textureplan.export_texture(lv, gsrc, "data", 0)
    q5 = textureplan.export_texture(lv, src, "data", 0)
    check(
        q1.endswith("t.color.dds")
        and q2.endswith("t_o.data.dds")
        and q3.endswith("n.normal.dds")
        and q4.endswith("g.data.dds"),
        f"dds names {q1} {q2} {q3} {q4}",
    )
    hdrs = _read_dds(lv, dds_mod, (q1, q2, q3, q4, q5))
    _check_dds_headers(hdrs, dds_mod, (q1, q2, q3, q4, q5), garr)
    size_ok = len(open(lv.path(*q1.split("/")[3:]), "rb").read()) == 148 + 16 * (
        256 + 64 + 16 + 4 + 1 + 1 + 1
    )
    check(size_ok, "BC7 file = header + 7 mip levels of whole 4x4 blocks")
    # write-if-changed holds for DDS too
    f1 = lv.path(*q1.split("/")[3:])
    s1 = os.stat(f1)
    _time.sleep(0.05)
    textureplan.export_texture(lv, src, "color", 0)
    s2 = os.stat(f1)
    check(s1.st_mtime_ns == s2.st_mtime_ns, "unchanged DDS is not rewritten")


def _check_dds_headers(hdrs, dds_mod, qs, garr):
    q1, q2, q3, q4, q5 = qs
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


def test_autogenerated_materials(tmp):
    """The headless compiler writes opaque placeholder `main.materials.json` files beside the
    shapes for names it never loaded (load #10: crosswalks / "45" / lane lines as light boxes).
    The cook stage removes them; the validator refuses a level that still has one, or any
    material name defined twice."""

    from cs2beamng.beamng import validate_level
    from cs2beamng.stages import cooktidy

    print("auto-generated materials beside the shapes")
    level = os.path.join(tmp, "lvl2")
    shapes = os.path.join(level, "art", "shapes", "markings")
    _write_shadowed_fixture(level, shapes)
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
    removed = cooktidy.remove_autogenerated(os.path.join(level, "art", "shapes"))
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


def _write_shadowed_fixture(level, shapes):
    """Our material under art/cs2/, the compiler's opaque shadow of it beside the shapes."""
    import json

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


def main():
    tmp = tempfile.mkdtemp(prefix="cs2beamng_a2m_")
    try:
        test_materials(tmp)
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
    """pytest entry point (tools/conftest.py); same run as `python tools/test_a2_materials.py`."""
    assert main() in (None, 0)
