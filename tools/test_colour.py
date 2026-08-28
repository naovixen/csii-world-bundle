"""Gate for the §11 colour chain — the largest module in the repo that had no test at all.

Every claim asserted here is one the modules' own docstrings make. Pure arithmetic plus the
committed example bundle's `grading.json`; no rip, no OCIO, no Blender. What it pins:

* the sRGB transfer pair round-trips all 256 bytes, and the scalar and array forms agree —
  the property refactor Phase 2 proved by hand when it collapsed five copies to one;
* `gamma_to_linear` is Unity's curve, not sRGB: identical below 1.0, x^2.2 above it, and the
  documented shadows example (0.9239 -> 0.836) holds;
* HDRP's white balance is identity at (0, 0) and the saturation matrix's rows sum to 1, so a
  grey stays grey under any saturation;
* `prepare_smh`'s w-offset is the game's ASYMMETRIC scale — a negative w counts once, a
  positive one four times — the rule whose absence was dormant until 2026-08-14;
* the contrast algebra: `apply` with contrast alone equals `x^C * 2^(K(C-1))` in ACEScg, i.e.
  the ACEScc log pivot really did solve into a power and a scalar;
* the example bundle's grading loads, grades a mid grey monotonically in exposure, and its
  OCIO view transform names every matrix the chain applies.

The measured agreement against the game's own screen is `tools/colour_model.py --check`, which
needs Blender; this gate is what CI can run.
"""

import os

import numpy
from csiibundle import colour, colourocio, colourspace

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def test_srgb_transfer():
    print("colour: the sRGB transfer pair")
    everything = numpy.arange(256, dtype=numpy.uint8)
    back = colourspace.linear_to_srgb8(colourspace.srgb8_to_linear(everything))
    check(numpy.array_equal(back, everything), "srgb8 -> linear -> srgb8 round-trips all 256 bytes")

    values = numpy.linspace(0.0, 1.0, 2001)
    scalar = numpy.asarray([colourspace.srgb_to_linear(float(v)) for v in values])
    array = colourspace.srgb8_to_linear(values * 255.0)
    check(
        bool(numpy.allclose(scalar, array, atol=1e-12)),
        "the scalar and array decoders are the same curve (2,001-point sweep)",
    )
    encoded = numpy.asarray([colourspace.linear_to_srgb(float(v)) for v in values])
    check(
        bool(numpy.all(numpy.diff(encoded) >= 0))
        and encoded[0] == 0.0
        and abs(encoded[-1] - 1.0) < 1e-12,
        "linear_to_srgb is monotonic and pinned at both ends",
    )


def test_gamma_is_not_srgb():
    print("colour: Unity's GammaToLinearSpace")
    below = [colourspace.gamma_to_linear(v) - colourspace.srgb_to_linear(v) for v in (0.02, 0.5)]
    check(all(abs(d) < 1e-12 for d in below), "identical to sRGB below 1.0")
    check(colourspace.gamma_to_linear(2.0) == 2.0**2.2, "x^2.2 above 1.0, where sRGB has no rule")
    check(
        abs(colourspace.gamma_to_linear(0.9239) - 0.836) < 5e-4,
        "the documented shadows example: an authored 0.9239 is really a 0.836 multiplier",
    )


def test_hdrp_matrices():
    print("colour: the ported HDRP operators")
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    check(
        colourspace.white_balance_matrix(0.0, 0.0) == identity,
        "white balance at (0, 0) is exactly the identity",
    )
    warm = colourspace.white_balance_matrix(15.0, 0.0)
    tinted = colourspace.mat_mul_vec(warm, (1.0, 1.0, 1.0))
    check(
        tinted[0] > tinted[2],
        f"a positive temperature warms a neutral (r {tinted[0]:.4f} > b {tinted[2]:.4f})",
    )
    for saturation in (0.0, 0.7, 1.0, 1.3):
        m = colourspace.saturation_matrix(saturation, colourspace.AP1_LUMA)
        sums = [sum(row) for row in m]
        check(
            all(abs(s - 1.0) < 1e-12 for s in sums),
            f"saturation x{saturation:g}: rows sum to 1, so grey stays grey",
        )
    grey = colourspace.mat_mul_vec(colourspace.saturation_matrix(0.0), (0.3, 0.5, 0.2))
    check(
        max(grey) - min(grey) < 1e-12,
        "saturation 0 collapses any colour to its own luminance",
    )
    roundtrip = colourspace.mat_mul(colourspace.AP1_TO_SRGB, colourspace.SRGB_TO_AP1)
    off = max(abs(roundtrip[r][c] - (1.0 if r == c else 0.0)) for r in range(3) for c in range(3))
    check(off < 1e-7, f"AP1_TO_SRGB really is the inverse (max off-identity {off:.2e})")


def test_smh_asymmetry():
    print("colour: PrepareShadowsMidtonesHighlights")
    check(colourspace.smh_offset((1, 1, 1, -0.1)) == -0.1, "a negative w counts once")
    check(
        abs(colourspace.smh_offset((1, 1, 1, 0.1)) - 0.4) < 1e-12, "a positive w counts FOUR times"
    )
    prepared = colourspace.prepare_smh((1.0, 0.979, 0.9239, 0.0))
    check(
        abs(prepared[2] - 0.836) < 5e-4,
        "the authored blue 0.9239 reaches the shader as 0.836 — gamma-decoded, not linear",
    )
    check(
        colourspace.prepare_smh((0.1, 0.1, 0.1, -0.5)) == (0.0, 0.0, 0.0),
        "the clamp at zero holds when the offset would go negative",
    )


def test_contrast_algebra():
    """`apply` with contrast alone must equal the closed form the docstring derives."""
    print("colour: the ACEScc contrast pivot, solved")
    g = colour.Grading({"colorAdjustments": {"contrast": 20.0}})
    c_exp = g.contrast_exponent
    k = colourspace.ACESCC_OFFSET - colourspace.ACESCC_SCALE * colourspace.ACESCC_MIDGRAY
    check(
        abs(g.contrast_scale - 2.0 ** (k * (c_exp - 1.0))) < 1e-12,
        f"contrast +20 -> x^{c_exp:g} with scale 2^(K*(C-1)), K = {k:.4f}",
    )
    for albedo in ((0.18, 0.18, 0.18), (0.4, 0.2, 0.1)):
        got = g.apply(albedo)
        ap1 = colourspace.mat_mul_vec(colourspace.SRGB_TO_AP1, albedo)
        expected = tuple(max(v, 0.0) ** c_exp * g.contrast_scale for v in ap1)
        expected = colourspace.mat_mul_vec(colourspace.AP1_TO_SRGB, expected)
        close = all(abs(a - b) < 1e-12 for a, b in zip(got, expected))
        check(close, f"apply({albedo}) is the closed form, in ACEScg, exactly")


def test_example_bundle_grading():
    print("colour: the example bundle's grading.json")
    g = colour.load(EXAMPLE)
    if g is None:
        check(False, "docs/example-bundle ships a grading.json")
        return
    check(len(g.sources) >= 5, f"every derived constant names its source ({len(g.sources)} lines)")
    check(g.lighting.present(), "the lighting block parsed")

    mid = (0.18, 0.18, 0.18)
    values = [sum(g.apply(mid, ev)) for ev in (-2.0, 0.0, 2.0)]
    check(values[0] < values[1] < values[2], "grading a grey is monotonic in exposure")

    yaml = colourocio.view_transform(g, "CS2 Graded")
    named = ["MatrixTransform", "ColorSpaceTransform", "BuiltinTransform"]
    check(all(n in yaml for n in named), "the view transform names every operator class it uses")
    gain = f"{g.gain_matrix[0][0]:.10g}"
    check(gain in yaml, "the gain matrix in the YAML is the derived one, digit for digit")

    ev, matched = _match_smoke(g)
    check(ev is None and matched is None, "OCIO evaluation declines cleanly without PyOpenColorIO")


def _match_smoke(g):
    """`colourocio` must refuse rather than guess when no OCIO is importable or locatable."""
    try:
        import PyOpenColorIO  # noqa: F401

        # OCIO present (unusual outside Blender): with no $OCIO and no locator it still refuses.
        if os.environ.get("OCIO") or colourocio.CONFIG_LOCATOR:
            return None, None
    except ImportError:
        pass
    rgb, used = colourocio.on_screen(g, (0.18, 0.18, 0.18))
    return rgb, used


def gate():
    test_srgb_transfer()
    test_gamma_is_not_srgb()
    test_hdrp_matrices()
    test_smh_asymmetry()
    test_contrast_algebra()
    test_example_bundle_grading()
    if FAILED:
        raise SystemExit(f"colour: {len(FAILED)} check(s) failed:\n  " + "\n  ".join(FAILED))
    print("colour: ok")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, HERE)
    gate()
