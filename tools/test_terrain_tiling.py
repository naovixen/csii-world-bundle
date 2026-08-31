"""Gate for audit §47, second half — what the bundle ships for the terrain materials.

Split from the original `test_terrain_blend.py` script (refactor Phase 4); the ported-shader
half is `tools/test_terrain_blend.py`. This file checks, against the real bundle when one is on
disk (and reports "unproven" rather than passing silently when none is):

* the tilings come from the game's own `rasters.terrainTiling`, not from us, and map
  near -> detail / mid -> macro / far -> base with `_tex_size` in LAYER TEXELS;
* the alpha of `<ground>_normal.png` IS a height map: its gradient reproduces the normal's own
  XY (the measurement §47 rests on, re-run so a future rip cannot quietly break it);
* ground mesh maps carry BeamNG's colour-space suffix, the detail deviation is computed in the
  space `blendDetail` runs in, and the base slot is low-passed so contrast is not added twice;
* `_height_texture` writes a centred map at `HEIGHT_SIGMA` and refuses a source with no alpha.
"""

import json
import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "AppData",
    "LocalLow",
    "Colossal Order",
    "Cities Skylines II",
    "ModsData",
    "Carto",
)
SURFACES = os.path.join(BUNDLE, "terrain", "surfaces")

from cs2beamng.stages import terraintextures as textures  # noqa: E402
from cs2beamng.stages import terraintiling as tiling  # noqa: E402
from csiibundle import png  # noqa: E402

CHECKS = 0
FAILS = []
# A claim this run could not put a number on, because it needs a rip and there is none. That is
# NOT a pass — a silent skip is how a gate stops being a gate — and it is not a failure either,
# or CI, which never has a bundle, would be permanently red. It is a third outcome, printed by
# name, and enforced the moment a bundle IS present.
UNPROVEN = []


def check(ok, label, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def finish():
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        print("FAILED:")
        for name in FAILS:
            print(f"  - {name}")
    return 1 if FAILS else 0


def enforce():
    """The gate's verdict: every check green, and with a bundle present, nothing unmeasured."""
    assert not FAILS, f"{len(FAILS)} check(s) failed — see the captured output above"
    if os.path.isfile(os.path.join(BUNDLE, "bundle.json")):
        # With a bundle on disk, "unproven" means the RIP did not carry the layer — a finding
        # about the producer, not about this machine.
        assert not UNPROVEN, (
            f"a bundle is present but {len(UNPROVEN)} claim(s) went unmeasured: {UNPROVEN}"
        )


def test_tilings():
    rasters_path = os.path.join(BUNDLE, "terrain", "rasters.json")
    print("\n-- the tilings come from the game, not from us --")
    if os.path.isfile(rasters_path):
        block = json.load(open(rasters_path, encoding="utf-8")).get("terrainTiling")
        if not isinstance(block, dict):
            check(
                False,
                "the bundle carries rasters.terrainTiling",
                "absent — this rip predates 095322a",
            )
        else:
            _check_shipped_tiling(block)
    else:
        UNPROVEN.append("the bundle carries terrain/rasters.json (no bundle on this machine)")
    missing, _, _, from_game = tiling._tiling_of(None, "grass")
    check(not from_game and missing == tiling.FALLBACK_TILE_M, "no block falls back and says so")


def _check_shipped_tiling(block):
    metres, detail_d, macro_d, from_game = tiling._tiling_of(block, "grass")
    want = block["layers"]["grass"]["metresPerRepeat"]
    blend = block["layers"]["grass"]["lodBlend"]
    check(from_game, "grass reads the shipped block")
    check(
        abs(metres["detail"] - want[0]) < 1e-6
        and abs(metres["macro"] - want[1]) < 1e-6
        and abs(metres["base"] - want[2]) < 1e-6,
        "near -> detail, mid -> macro, far -> base",
        f"detail {metres['detail']:.2f} m, macro {metres['macro']:.2f} m, "
        f"base {metres['base']:.1f} m",
    )
    # *TexSize is in LAYER TEXELS: the shader samples at (terrainUv * layerSize) / TexSize,
    # and vanilla's TexSize x squareSize lands on BeamNG's documented 2-8 / 30-80 / 128-2048 m
    for cell in (1.75, 3.5):
        got = {t: tiling._tex_size(metres[t], cell) for t in ("detail", "macro", "base")}
        err = max(abs(got[t] * cell - metres[t]) / metres[t] for t in got)
        check(
            err < 0.04,
            f"grass tiers land within 4 % of the game's metres on a {cell} m grid",
            ", ".join(
                f"{t} {got[t]} texels = {got[t] * cell:.1f} m (want {metres[t]:.1f})" for t in got
            ),
        )
    check(
        tiling._tex_size(14.336, 1.75) == 8 and tiling._tex_size(14.336, 1.0) == 14,
        "TexSize scales with squareSize - it is texels, not metres",
        "14.34 m is 8 texels at 1.75 m and 14 at 1.0 m",
    )
    check(
        metres != tiling.FALLBACK_TILE_M,
        "the pre-§47 constants are gone",
        f"was base {tiling.FALLBACK_TILE_M['base']}/macro {tiling.FALLBACK_TILE_M['macro']}/"
        f"detail {tiling.FALLBACK_TILE_M['detail']} m for every ground",
    )
    check(
        detail_d == [0.0, blend[0], blend[1], blend[3]] and macro_d == detail_d,
        "the tier ramp is the prefab's own lodBlend",
        f"near->mid {blend[0]:.0f}-{blend[1]:.0f} m, mid->far ends {blend[3]:.0f} m",
    )
    # extras take the grass tiling: the bundle's own semantics string says so
    extra, _, _, extra_from_game = tiling._tiling_of(block, "extra2")
    check(extra_from_game and extra == metres, "an extra tiles with grass")
    rock, _, _, _ = tiling._tiling_of(block, "rock")
    check(
        rock["detail"] > metres["detail"],
        "rock is coarser than grass, as authored",
        f"rock detail {rock['detail']:.2f} m vs grass {metres['detail']:.2f} m",
    )


def test_normal_alpha_is_height():
    print("\n-- <ground>_normal.png alpha IS the ground's height map --")
    if os.path.isdir(SURFACES):

        def corr(p, q):
            p = p.ravel().astype(numpy.float64)
            q = q.ravel().astype(numpy.float64)
            p, q = p - p.mean(), q - q.mean()
            return float((p * q).sum() / (numpy.sqrt((p * p).sum() * (q * q).sum()) + 1e-12))

        tested = 0
        for ground in ("grass", "dirt", "rock", "extra2"):
            path = os.path.join(SURFACES, f"{ground}_normal.png")
            if not os.path.isfile(path):
                continue
            tested += 1
            rgba = png.rgba_array(path).astype(numpy.float64)
            nx, ny, height = rgba[:, :, 0] - 128.0, rgba[:, :, 1] - 128.0, rgba[:, :, 3]
            cx = corr(nx, -numpy.gradient(height, axis=1))
            cy = corr(ny, -numpy.gradient(height, axis=0))
            # the green convention is flipped (OpenGL vs DirectX), so the two correlations are
            # equal and opposite — that pairing is the signature, not either sign on its own
            check(
                cx > 0.25 and cy < -0.25 and abs(cx + cy) < 0.25,
                f"{ground}: alpha's gradient reproduces the normal's XY",
                f"corr(nx,-dh/du) {cx:+.3f}, corr(ny,-dh/dv) {cy:+.3f}",
            )
        check(tested >= 3, "at least three grounds measured", f"{tested}")
    else:
        UNPROVEN.append("terrain/surfaces/ exists (no bundle on this machine)")


def test_ground_mesh_maps():
    print("\n-- ground mesh maps carry BeamNG's colour-space suffix --")
    # The suffix is how BeamNG decides sRGB vs linear. Without one an albedo is read as linear data
    # and renders pale: measured off load #24 the cut pieces were (174,179,166) where the terrain
    # beside them was (119,133,81), and "sRGB mistaken for linear, re-encoded" predicts (180,190,152).
    # The convention is BeamNG's own: 1404 vanilla baseColorMaps end .color.png, and our own road and
    # marking materials -- the ones that always rendered right -- end .color.dds.
    with tempfile.TemporaryDirectory() as tmp2:

        class _GroundLevel:
            name = "gate"

            def __init__(self, root):
                self.root = root

            def write_bytes_if_changed(self, data, *parts):
                path = os.path.join(self.root, *parts)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(data)

            def game_path(self, *parts):
                return "/levels/gate/" + "/".join(parts)

        lv = _GroundLevel(tmp2)
        rngc = numpy.random.default_rng(9)
        src = os.path.join(tmp2, "g.png")
        with open(src, "wb") as handle:
            handle.write(png.encode_rgb((rngc.random((64, 64, 3)) * 200 + 20).astype(numpy.uint8)))
        base, detail = tiling._ground_mesh_maps(lv, src, "gate_grass_ground_b", size=64)
        check(
            base.endswith(".color.png"), "the base map is tagged .color (sRGB)", base.split("/")[-1]
        )
        check(
            detail.endswith(".data.png"),
            "the detail map is tagged .data (raw linear)",
            detail.split("/")[-1],
        )
        nrm, nrm_detail = tiling._ground_mesh_maps(
            lv, src, "gate_grass_ground_nm", normal=True, size=64
        )
        check(
            nrm.endswith(".normal.png") and nrm_detail == nrm,
            "the normal map is tagged .normal",
            nrm.split("/")[-1],
        )
        _check_deviation_maps(tmp2, src)


def _check_deviation_maps(tmp2, src):
    """The written maps: deviation in blendDetail's linear space, base low-passed once."""
    # `.color` is decoded to linear before blendDetail runs, so the deviation must be linear too
    from PIL import Image as _Img

    det = (
        numpy.asarray(
            _Img.open(os.path.join(tmp2, "art", "terrains", "gate_grass_ground_b.data.png"))
        ).astype(numpy.float64)
        / 255.0
    )
    base_img = _Img.open(os.path.join(tmp2, "art", "terrains", "gate_grass_ground_b.color.png"))
    check(
        base_img.size == (tiling.BASE_MAP_PX, tiling.BASE_MAP_PX),
        f"the base map is written at {tiling.BASE_MAP_PX} px - the size of its content, not the albedo's",
        f"{base_img.size}",
    )
    # the sampler's bilinear filter is what brings it back to the albedo's size in the shader
    bas = (
        numpy.asarray(base_img.resize(det.shape[1::-1], _Img.BILINEAR)).astype(numpy.float64)
        / 255.0
    )
    lin = (numpy.asarray(_Img.open(src).convert("RGB")).astype(numpy.float64) / 255.0) ** 2.2
    want = 0.5 + (lin - lin.reshape(-1, 3).mean(axis=0)[None, None, :]) * 0.5
    check(
        float(numpy.abs(det - want).max()) < 3.0 / 255.0,
        "the detail deviation is computed in the space blendDetail runs in",
        f"max err {float(numpy.abs(det - want).max()):.4f}",
    )
    # The base slot must be LOW-FREQUENCY or its own contrast is added a second time: the detail
    # carries the deviation from the image MEAN, so `base + (2*detail - 1)` only reproduces the
    # albedo where the base is locally that mean. Measured on the shipped grounds before this:
    # 1.8x the albedo's sigma and 32.5 % of extra2 clamped to black.
    base_lin = bas**2.2
    check(
        base_lin.std() < lin.std() * 0.5,
        "the base map is low-passed, not the full albedo",
        f"base sigma {base_lin.std():.4f} against the albedo's {lin.std():.4f}",
    )
    real = numpy.clip(base_lin + (2 * det - 1), 0.0, 1.0)
    check(
        abs(real.std() - lin.std()) < 0.02,
        "so it carries the albedo's contrast, not twice it",
        f"composite sigma {real.std():.4f} against the albedo's {lin.std():.4f}",
    )


def test_mesh_composite_clamp():
    # Clamping is a property of the real grounds' own contrast, so it is measured on them, not on the
    # synthetic source above (uniform random, sigma 0.21 — far past any real ground, and it clamps by
    # construction). Before the low-pass this was 32.5 % of extra2 crushed to black.
    if os.path.isdir(SURFACES):
        from PIL import Image as _image2

        worst_clamp, worst_name = 0.0, None
        for ground in ("grass", "dirt", "rock", "extra1", "extra2", "extra4"):
            path = os.path.join(SURFACES, f"{ground}.png")
            if not os.path.isfile(path):
                continue
            a8 = numpy.asarray(_image2.open(path).convert("RGB")).astype(numpy.uint8)
            a = numpy.clip(
                (a8.astype(numpy.float64) / 255.0) ** 2.2 * 2.57, 0, 1
            )  # the build's gain
            enc = _image2.fromarray((a ** (1 / 2.2) * 255).astype(numpy.uint8))
            low = numpy.asarray(
                enc.resize((textures.BASE_LOWPASS,) * 2, _image2.BOX).resize(
                    enc.size, _image2.BILINEAR
                )
            )
            base = (low.astype(numpy.float64) / 255.0) ** 2.2
            comp = numpy.clip(base + (a - a.reshape(-1, 3).mean(axis=0)[None, None, :]), 0.0, 1.0)
            frac = float(((comp <= 1e-9) | (comp >= 1 - 1e-9)).any(axis=-1).mean())
            if frac > worst_clamp:
                worst_clamp, worst_name = frac, ground
        check(
            worst_clamp < 0.02,
            "and no shipped ground clamps in the mesh composite",
            f"worst {worst_name} {worst_clamp * 100:.2f} % (32.5 % before the low-pass)",
        )


class _Level:
    """The two `level` methods `_height_texture` touches."""

    name = "gate"

    def __init__(self, root):
        self.root = root
        self.written = {}

    def write_bytes_if_changed(self, data, *parts):
        self.written[parts[-1]] = data
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def game_path(self, *parts):
        return "/levels/gate/" + "/".join(parts)


def test_height_texture():
    print("\n-- _height_texture writes the relief as a tier deviation --")
    with tempfile.TemporaryDirectory() as tmp:
        level = _Level(tmp)
        rng = numpy.random.default_rng(1)
        src = os.path.join(tmp, "src.png")
        rgba = numpy.zeros((64, 64, 4), dtype=numpy.uint8)
        rgba[:, :, 3] = numpy.clip(rng.normal(70, 25, size=(64, 64)), 0, 255).astype(numpy.uint8)
        with open(src, "wb") as handle:
            handle.write(png.encode_rgba(rgba))
        out = textures._height_texture(level, src, "h.png")
        check(out is not None, "a source with alpha yields a height map", str(out))
        from PIL import Image

        written = (
            numpy.asarray(Image.open(os.path.join(tmp, "art", "terrains", "h.png"))).astype(
                numpy.float64
            )
            / 255.0
        )
        check(abs(written.mean() - 0.5) < 0.02, "centred on mid-gray", f"mean {written.mean():.4f}")
        # written at HALF sigma: the base tier is flat, so the additive overlay doubles it back
        check(
            abs(written.std() - textures.HEIGHT_SIGMA * 0.5) < 0.01,
            "written at half HEIGHT_SIGMA",
            f"sigma {written.std():.4f}, composite 0.5 + (2t - 1) = {textures.HEIGHT_SIGMA}",
        )
        composite = numpy.clip(0.5 + (2 * written - 1) * 1.0, 0.0, 1.0)
        check(
            abs(composite.std() - textures.HEIGHT_SIGMA) < 0.01,
            "and the composite lands on HEIGHT_SIGMA",
            f"sigma {composite.std():.4f}",
        )

        flat_src = os.path.join(tmp, "flat.png")
        rgba[:, :, 3] = 200
        with open(flat_src, "wb") as handle:
            handle.write(png.encode_rgba(rgba))
        check(
            textures._height_texture(level, flat_src, "flat_h.png") is None,
            "a constant alpha is NOT a height map",
            "the material keeps the neutral 128",
        )
        check(
            textures._height_texture(level, os.path.join(tmp, "nope.png"), "x.png") is None,
            "a missing source yields None",
        )


def main():
    test_tilings()
    test_normal_alpha_is_height()
    test_ground_mesh_maps()
    test_mesh_composite_clamp()
    test_height_texture()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_terrain_tiling.py`."""
    main()
    enforce()
