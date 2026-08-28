"""cs2beamng A3 gate, part 2 — the BeamNG-writer rules.

Run: `python tools/test_a3_rules.py`. Split from `test_a3_objects.py` (Phase 4); the stage
tests live there and the real-city sweeps in `test_a3_city.py`.

* **rotationMatrix** is the object's basis vectors in groups of three, anchored against
  BeamNG's own shipped levels rather than our writers;
* **LOD switch points** may only ever DEFER a generated level, never advance it (A6.7);
* **the A8 performance predicates** — every texture kind capped, the footprint rule;
* **the colour palette** (A6.3) — CS2's §7.2 mask under BeamNG's own shadergen semantics,
  the alpha carrying tint coverage, and the export path that must not drop it.
"""

import itertools
import json
import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import check, finish, start  # noqa: E402

BEAMNG_INSTALL = r"G:\SteamLibrary\steamapps\common\BeamNG.drive"


def test_rotation_matrix_is_basis_vectors():
    """`rotationMatrix` holds the object's OWN AXES in groups of three, not a row-major matrix.

    The only non-circular gate in the rotation chain: it asks BeamNG's own shipped levels, not
    our writers. A tunnel's spot light points at the road, so reading the field as groups must
    put a vanilla SpotLight's +Y DOWN — and reading it row-major puts it up, which is what our
    level shipped for months (A6.8, audit §78)."""
    print("rotationMatrix is the object's basis vectors (vanilla-anchored)")
    from cs2beamng.beamng import frame

    # 1. the serialisation itself, on a rotation whose axes are unmistakable: a +90 deg yaw
    yaw = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]  # columns are the axes
    flat = frame.basis_flat(yaw)
    check(flat[0:3] == [0.0, 1.0, 0.0], f"group 0 is the object's X axis ({flat[0:3]})")
    check(flat[3:6] == [-1.0, 0.0, 0.0], f"group 1 is its Y axis ({flat[3:6]})")
    check(flat[6:9] == [0.0, 0.0, 1.0], f"group 2 is its Z axis ({flat[6:9]})")
    east = frame.heading_matrix(1.0, 0.0)
    check(east[3:6] == [1.0, 0.0, 0.0], f"an object facing east has +Y east ({east[3:6]})")

    # 2. the convention, against BeamNG's own levels
    levels = os.path.join(BEAMNG_INSTALL, "content", "levels")
    if not os.path.isdir(levels):
        print("  ..  vanilla levels absent, the anchor is skipped")
        return
    import zipfile

    groups_down = columns_down = total = 0
    for name in sorted(f for f in os.listdir(levels) if f.endswith(".zip"))[:6]:
        with zipfile.ZipFile(os.path.join(levels, name)) as z:
            for entry in [n for n in z.namelist() if n.endswith(".level.json")]:
                for line in z.read(entry).splitlines():
                    line = line.strip()
                    if not line.startswith(b"{") or b'"SpotLight"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    m = obj.get("rotationMatrix")
                    if obj.get("class") != "SpotLight" or not m or len(m) != 9:
                        continue
                    total += 1
                    groups_down += int(m[5] <= -0.9)  # group 1 (+Y) points down
                    columns_down += int(m[7] <= -0.9)  # column 1 (+Y) points down
    if not total:
        print("  ..  no vanilla SpotLights found, the anchor is skipped")
        return
    check(total > 100, f"{total:,} vanilla SpotLights read from the first levels on disk")
    check(
        groups_down > 10 * max(columns_down, 1),
        f"read as GROUPS {groups_down:,} of {total:,} point down; read row-major {columns_down:,} do",
    )


def _switch_fixture(tmp):
    """An 8-triangle box mesh plus its two pre-written generated levels (4 and 2 triangles)."""
    from cs2beamng.beamng import dae
    from csiibundle import glbwrite

    box_v = numpy.array(
        [[0.0, 0, 0], [4, 0, 0], [4, 0, 6], [0, 0, 6], [0, 3, 0], [4, 3, 0], [4, 3, 6], [0, 3, 6]]
    )
    box_t = numpy.array(
        [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [2, 3, 7], [2, 7, 6]]
    )
    os.makedirs(os.path.join(tmp, "lods"), exist_ok=True)
    for level in (1, 2):
        glbwrite.write_glb(
            os.path.join(tmp, "lods", f"src_LOD{level}.glb"),
            [
                {
                    "name": "box",
                    "primitives": [
                        {
                            "verts": box_v,
                            "normals": None,
                            "uvs": None,
                            "tris": box_t[: 4 if level == 1 else 2],
                            "material": "mat",
                        }
                    ],
                }
            ],
        )
    return dae.Mesh("box", box_v, box_t, [("mat", len(box_t))])


def test_switch_points_only_ever_defer():
    """The A6.7 regression, as a gate: a measured displacement may push a generated level FURTHER
    out than the ladder puts it, never nearer.

    Allowing both directions shipped 2,586 of 2,903 prop shapes onto their decimated geometry
    closer than the ladder ever had — a streetlight's 24-triangle level from 13 m — and the
    level came back "cranes rotated and floating, highrises all diagonal". Surface distance is
    not visual error for thin geometry; the ladder's size-keyed spacing protects it."""
    print("LOD switch points defer, never advance")
    from cs2beamng.stages import shapelods, shapewrite  # noqa: F401

    with tempfile.TemporaryDirectory() as tmp:
        mesh = _switch_fixture(tmp)
        options = {"generate_lods": True, "switch_error_px": 6.0}
        moved = 0
        for null_px in (3.0, 9.0, 40.0):
            for metres in (None, 0.0, 0.001, 0.5, 5.0):
                _levels, stats, meshes = shapelods.lod_levels(
                    tmp,
                    "src",
                    "shape",
                    mesh,
                    null_px,
                    8.0,
                    [metres, metres],
                    "building",
                    {},
                    {},
                    "level",
                    options,
                )
                sizes = stats["detailSizes"]
                ladder = [
                    max(2, int(round(null_px * r)))
                    for r in reversed(shapelods.LADDER_RATIOS[: len(meshes)])
                ]
                check(len(meshes) == 3, f"nullPx {null_px}: the two generated levels loaded")
                check(
                    all(got <= want + 0.5 for got, want in zip(sizes, ladder)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} never above the ladder {ladder}",
                )
                check(
                    all(a > b for a, b in itertools.pairwise(sizes)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} strictly decreases",
                )
                if any(got < want - 0.5 for got, want in zip(sizes, ladder)):
                    moved += 1
        check(moved > 0, f"and a level that IS out of shape is deferred ({moved} of 15 cases)")
        # vegetation is exempt by kind, and exempt means exactly the ladder
        _levels, veg, _m = shapelods.lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "tree", {}, {}, "level", options
        )
        ladder = [
            max(2, int(round(9.0 * r)))
            for r in reversed(shapelods.LADDER_RATIOS[: len(veg["triangles"])])
        ]
        check(
            veg["detailSizes"] == ladder,
            f"a tree keeps the ladder exactly ({veg['detailSizes']} == {ladder})",
        )


def test_a8_performance_rules():
    """A8 — the two new predicates, both of which remove something a viewer can see."""
    print("A8 performance rules")
    from cs2beamng.beamng import materials as bm
    from cs2beamng.stages import instances as inst
    from cs2beamng.stages import shapelods

    # P1: EVERY exported kind must be capped. A kind missing from the table reads as 0 = uncapped,
    # which is how 767 colour masks shipped at 1024 beside 256-pixel albedos for 1,025 MiB.
    caps = bm.caps_from_settings(
        {"texture_max": 256, "texture_max_normal": 128, "texture_max_data": 64}
    )
    check(
        caps.get("colormask") == 256,
        f"the colour mask takes the colour cap ({caps.get('colormask')})",
    )
    missing = [
        k
        for k in ("color", "normal", "data", "opacity", "colormask")
        if k not in bm.KIND_CAP_SETTING
    ]
    check(not missing, f"every exported texture kind has a cap setting (missing: {missing})")

    # P4: the footprint predicate. Height alone passed a hydrant (0.46 m across, 0.97 m tall).
    hydrant = {"boundsMin": [-0.23, 0.0, -0.23], "boundsMax": [0.23, 0.97, 0.23]}
    sign = {"boundsMin": [-0.27, 0.0, -0.05], "boundsMax": [0.27, 2.13, 0.05]}
    kiosk = {"boundsMin": [-1.2, 0.0, -1.2], "boundsMax": [1.2, 2.4, 1.2]}
    check(inst._shape_footprint(hydrant) < 0.8, "a fire hydrant is under the footprint threshold")
    check(inst._shape_footprint(sign) < 0.8, "a road sign is under it, though it is 2.13 m TALL")
    check(
        inst._shape_height(sign) > inst.FOREST_COLLIDABLE_MIN_HEIGHT,
        "...and the HEIGHT rule alone would have kept its collider - the defect A8 fixes",
    )
    check(inst._shape_footprint(kiosk) >= 0.8, "a 2.4 m kiosk keeps its collider")
    check(
        inst._shape_footprint({}) == float("inf"), "an unmeasurable shape NEVER loses its collider"
    )

    # P2: promotion is gated on the MEASURED displacement, and vegetation is exempt from it for
    # the same reason it is exempt from the switch rule (a lamp-post LOD measures 0.06 m and is
    # visually total, A6.7).
    check(shapelods.LOD0_MAX_ERROR_DEFAULT > 0, "LOD0 promotion has a default threshold")
    check(
        "tree" not in shapelods.DISPLACEMENT_RULE_KINDS,
        "vegetation is exempt from the displacement rule, so it never loses LOD 0",
    )


def _cs2_tint(base, mask, colours):
    """§7.2 verbatim: each colour lerps the running tint by its own mask channel."""
    t = numpy.asarray(base, dtype=numpy.float64)
    for i in range(3):
        t = t * (1.0 - mask[i]) + (t * numpy.asarray(colours[i])) * mask[i]
    return t


def _beamng_palette(base, mask_rgba, colours):
    """`shadergen.h.hlsl::getColorPalette` verbatim: RGB normalised to a selector, ALPHA is the
    tint amount, and the untinted remainder passes the base through."""
    s = numpy.asarray(mask_rgba, dtype=numpy.float64).copy()
    s[:3] = s[:3] / (s[0] + s[1] + s[2] + 1e-10)
    a = s[3]
    s[:3] *= a
    palette = sum(s[i] * numpy.asarray(colours[i]) for i in range(3)) + (1.0 - a)
    return numpy.asarray(base, dtype=numpy.float64) * palette


class _Level:
    name = "lvl"

    def game_path(self, *p):
        return "/" + "/".join(p)

    def pid(self, k):
        return k


def test_color_palette():
    """A6.3 — CS2's colour mask converted into BeamNG's palette semantics.

    Both sides implemented literally from their own sources: CS2 from `docs/bundle-schema.md`
    §7.2, BeamNG from `shaders/common/material/shadergen/shadergen.h.hlsl::getColorPalette`.
    The old export forced the mask's alpha to 255, which is why an untinted texel rendered BLACK.
    """
    print("colour palette (A6.3)")
    from cs2beamng.beamng.materials import _colormask_rgba

    base = [0.55, 0.50, 0.47]
    colours = [[0.90, 0.30, 0.20], [0.20, 0.55, 0.85], [0.0, 0.0, 0.0]]

    worst = 0.0
    for m in (
        [0, 0, 0],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
        [0.3, 0, 0],
        [0, 0.75, 0],
        [0, 0, 0.5],
        [0.05, 0, 0],
    ):
        mask = numpy.asarray(m, dtype=numpy.float64)
        conv = _colormask_rgba((mask * 255).round().astype(numpy.uint8).reshape(1, 1, 3), "rgb")
        worst = max(
            worst,
            float(
                numpy.abs(
                    _beamng_palette(base, conv[0, 0] / 255.0, colours)
                    - _cs2_tint(base, mask, colours)
                ).max()
            ),
        )
    check(
        worst < 0.002,
        f"a DISJOINT mask reproduces CS2's tint exactly (worst {worst:.5f}, the alpha's own step)",
    )

    # the bug this replaces: alpha forced to 255 makes an untinted texel BLACK
    black = _beamng_palette(base, [0.0, 0.0, 0.0, 1.0], colours)
    check(
        float(numpy.abs(black).max()) < 1e-9,
        "the OLD alpha-255 export renders an untinted texel pure black (the reported defect)",
    )
    conv = _colormask_rgba(numpy.zeros((1, 1, 3), numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 0, "an untinted texel now carries alpha 0 - no tint, not black")
    conv = _colormask_rgba(numpy.array([[[255, 0, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "a fully tinted texel carries alpha 255")
    conv = _colormask_rgba(numpy.array([[[200, 100, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "alpha saturates rather than wrapping")


def test_palette_material():
    """The material must actually switch the palette on, and the joined emissive slot's nits
    reach it — including a slot authored to emit NOTHING (A6.9/D5)."""
    print("colour palette: the material")
    from cs2beamng.beamng import materials as bm

    stage = bm.material_v15(_Level(), "m", {"tintable": True}, {"colorMask": "/t.dds"})
    stage0 = stage["Stages"][0] if "Stages" in stage else stage
    check(
        stage0.get("paletteBaseColor") is True,
        "a tintable material sets paletteBaseColor - without it the palette applies to nothing",
    )

    # A6.9/D5: the emissive nits come from the joined slot, INCLUDING when the slot says zero.
    # 115 of the reference city's slots do, and every one is the unlit face of a billboard; a
    # falsy test would light all of them at the flat fallback.
    lit = bm.material_v15(_Level(), "m", {"emissiveNits": 5000.0}, {"emissive": "/e.dds"})
    lit0 = lit["Stages"][0] if "Stages" in lit else lit
    check(lit0.get("emissiveIntensityNits") == 5000.0, "a joined slot's nits are used")
    off = bm.material_v15(_Level(), "m", {"emissiveNits": 0.0}, {"emissive": "/e.dds"})
    off0 = off["Stages"][0] if "Stages" in off else off
    check(
        off0.get("emissiveIntensityNits") == 0.0,
        "a slot authored to emit NOTHING stays at zero, not the flat fallback",
    )
    none = bm.material_v15(_Level(), "m", {}, {"emissive": "/e.dds"})
    none0 = none["Stages"][0] if "Stages" in none else none
    check(
        none0.get("emissiveIntensityNits") == bm.EMISSIVE_NITS
        and none0.get("emissiveFactor") == [1.0, 1.0, 1.0],
        "a material no slot reaches takes the declared fallback, white at 200 nits",
    )


def test_colormask_export():
    """THE MASK MUST REACH THE LEVEL. `199f084` wrote the conversion into `texture_name`, the
    NAMING function, where `arr` does not exist: every `colormask` export raised NameError, all
    724 masks were dropped, and the built level carried ZERO `colorPaletteMap` — so the whole of
    A6.3 was in the build report and nowhere else. `material_v15` alone cannot see that."""
    print("colour palette: the export path")
    from cs2beamng.beamng import materials as bm

    try:
        name = bm.texture_name("/src/abc.png", "colormask", use_dds=False)
        ok = name.endswith(".colormask.png")
    except Exception as exc:
        name, ok = repr(exc), False
    check(ok, f"texture_name('colormask') returns a name rather than raising ({name})")

    written = {}

    class _LW(_Level):
        def write_bytes_if_changed(self, data, *parts):
            written[parts[-1]] = data

    src = os.path.join(tempfile.mkdtemp(), "mask.png")
    from csiibundle import png as _png

    with open(src, "wb") as h:
        h.write(_png.encode_rgb(numpy.array([[[200, 100, 0], [0, 0, 0]]], numpy.uint8)))
    out = bm.export_texture(_LW(), src, "colormask", dds=False)
    check(bool(out) and written, f"a colormask actually exports ({out})")
    if written:
        tmp = os.path.join(tempfile.mkdtemp(), "out.png")
        with open(tmp, "wb") as h:
            h.write(next(iter(written.values())))
        back = _png.rgba_array(tmp)
        check(
            back.shape[2] == 4 and int(back[0, 0, 3]) == 255 and int(back[0, 1, 3]) == 0,
            "the exported mask carries the tint coverage in ALPHA (255 tinted, 0 untinted)",
        )


def main():
    start()
    test_rotation_matrix_is_basis_vectors()
    test_switch_points_only_ever_defer()
    test_a8_performance_rules()
    test_color_palette()
    test_palette_material()
    test_colormask_export()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_rules.py`."""
    assert main() in (None, 0)
