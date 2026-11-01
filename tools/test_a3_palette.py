"""cs2beamng A3 gate, part 2b — the colour palette (A6.3).

Run: `python tools/test_a3_palette.py`. Split from `test_a3_rules.py` (refactor Phase 4): CS2's
§7.2 mask under BeamNG's own shadergen semantics, the alpha carrying tint coverage, the material
that switches the palette on, and the export path that must not drop the mask.
"""

import os
import sys
import tempfile

import numpy
from a3common import check, finish, start


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
    from cs2beamng.beamng.textureencode import _colormask_rgba

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
    from cs2beamng.beamng import textureplan

    try:
        name = textureplan.texture_name("/src/abc.png", "colormask", use_dds=False)
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
    out = textureplan.export_texture(_LW(), src, "colormask", dds=False)
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
    test_color_palette()
    test_palette_material()
    test_colormask_export()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_palette.py`."""
    assert main() in (None, 0)
