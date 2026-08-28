"""Colour spaces and the operators on them — transfer functions and ported HDRP arithmetic.

Everything here is a pure function of its inputs: the sRGB and Unity gamma transfer curves, the
Rec.709 <-> ACEScg matrices the grading chain runs between, and the HDRP operators that
collapse to a matrix (white balance, saturation) or a per-channel prepare step
(shadows/midtones/highlights). The chain that composes them is `csiibundle.colour.Grading`.

EVERY CONSTANT BELOW IS DERIVED, AND SAYS FROM WHAT. The rule this repo works under is that a
number with no derivation beside it is not allowed, because a fitted constant cannot tell you
it is absorbing a bug.
"""

# Rec.709 luminance weights, for anything measured in the scene's own space.
LUMA = (0.2126, 0.7152, 0.0722)

# THE GRADING IS DONE IN ACEScg (AP1), NOT IN LINEAR REC.709.
#
# HDRP's HDR grading path works in ACEScg, and this file used to grade in linear Rec.709 anyway,
# on the strength of a check that measured the difference at "under 2%". That check was wrong in
# a specific way worth recording: it compared PRE-TONEMAP values on ONE surface. Carried through
# the tonemap and measured against three in-game surfaces, the working space turns out to be one
# of the largest terms in the whole chain — it cut the residual error from 0.29 to 0.18 and took
# grass from 23% out on blue to within 5%.
#
# So the space is HDRP's documented one AND the measurements corroborate it; the documentation is
# what justifies the choice, the measurement is what caught the mistake.
#
# Standard published matrices, D65 Rec.709 <-> D60 AP1 with the chromatic adaptation folded in.
SRGB_TO_AP1 = (
    (0.6130974, 0.3395231, 0.0473795),
    (0.0701937, 0.9163538, 0.0134526),
    (0.0206156, 0.1095698, 0.8698146),
)


# Inverted rather than transcribed: a hand-typed inverse is a silent-error waiting to happen and
# nothing downstream would show it as anything but a slight colour cast.
def _mat_inverse(m):
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    return tuple(
        tuple(
            (
                (
                    m[(c + 1) % 3][(r + 1) % 3] * m[(c + 2) % 3][(r + 2) % 3]
                    - m[(c + 1) % 3][(r + 2) % 3] * m[(c + 2) % 3][(r + 1) % 3]
                )
                / det
            )
            for c in range(3)
        )
        for r in range(3)
    )


AP1_TO_SRGB = _mat_inverse(SRGB_TO_AP1)

# AP1 luminance weights — the ones that go with the space above, and what HDRP's `Luminance()`
# resolves to when the working space is ACEScg.
AP1_LUMA = (0.2722287, 0.6740818, 0.0536895)

# ACEScc mid grey. From the ACES specification (the encoding's 0.18 anchor), and the value HDRP
# pivots contrast about.
ACESCC_MIDGRAY = 0.4135884

# The ACEScc encoding's own constants: v = (log2(x) + 9.72) / 17.52.
ACESCC_OFFSET = 9.72
ACESCC_SCALE = 17.52


# -- transfer functions -----------------------------------------------------------------------


# The sRGB transfer function's own constants (IEC 61966-2-1). Named because the curve is written
# four times in this repo and two of those copies must stay copies — see `srgb8_to_linear`.
#
# NOT to be confused with a plain 2.2 gamma. `stages/terrain.py` raises to 2.2 in five places and
# that is a DIFFERENT function, deliberately: those sites are BeamNG terrain textures, and
# swapping the exact curve in would change every one of them. They are not copies of this.
SRGB_LINEAR_CUTOFF = 0.0031308
SRGB_ENCODED_CUTOFF = 0.04045
SRGB_SLOPE = 12.92
SRGB_OFFSET = 0.055
SRGB_SCALE = 1.055
SRGB_GAMMA = 2.4


def linear_to_srgb(c):
    """One linear value in 0..1 to its sRGB-encoded 0..1. Scalar; see `linear_to_srgb8` for arrays."""
    c = max(0.0, min(1.0, c))
    if c <= SRGB_LINEAR_CUTOFF:
        return c * SRGB_SLOPE
    return SRGB_SCALE * (c ** (1 / SRGB_GAMMA)) - SRGB_OFFSET


def srgb_to_linear(c):
    """One sRGB-encoded value in 0..1 to linear. Scalar; see `srgb8_to_linear` for arrays."""
    if c <= SRGB_ENCODED_CUTOFF:
        return c / SRGB_SLOPE
    return ((c + SRGB_OFFSET) / SRGB_SCALE) ** SRGB_GAMMA


def srgb8_to_linear(arr):
    """A uint8 sRGB array to float64 linear in 0..1 — the array form of `srgb_to_linear`.

    This and `linear_to_srgb8` were byte-identical in `bake/materials.py` and
    `measure_paint_factor.py`, which is what made the measurement tool and the thing it measures
    two separate opinions about the same curve.

    Two OTHER copies survive on purpose, both in files whose contract is to have no dependencies:
    `tools/validate_bundle.py` ("the standard library, no pip installs, no producer source") and
    `tools/probe_point.py` ("a diagnostic that only works when the thing it is diagnosing imports
    cleanly is not much of a diagnostic"). Those two are not debt.
    """
    import numpy

    a = numpy.asarray(arr, dtype=numpy.float64) / 255.0
    return numpy.where(
        a <= SRGB_ENCODED_CUTOFF,
        a / SRGB_SLOPE,
        ((a + SRGB_OFFSET) / SRGB_SCALE) ** SRGB_GAMMA,
    )


def linear_to_srgb8(arr):
    """A float linear array to uint8 sRGB — the array form of `linear_to_srgb`, rounded."""
    import numpy

    a = numpy.clip(arr, 0.0, 1.0)
    s = numpy.where(
        a <= SRGB_LINEAR_CUTOFF,
        a * SRGB_SLOPE,
        SRGB_SCALE * numpy.power(a, 1 / SRGB_GAMMA) - SRGB_OFFSET,
    )
    return numpy.clip(numpy.rint(s * 255.0), 0, 255).astype(numpy.uint8)


def gamma_to_linear(c):
    """Unity's `Mathf.GammaToLinearSpace`, ported.

    Not the same curve as sRGB above 1.0, and the difference matters: HDRP puts the authored
    shadows/midtones/highlights colours through THIS before the shader sees them, so a shipped
    0.9239 is a 0.836 multiplier, not a 0.92 one — nearly twice the correction.
    """
    if c <= 0.04045:
        return c / 12.92
    if c < 1.0:
        return ((c + 0.055) / 1.055) ** 2.4
    return c**2.2


def smh_offset(rgbw):
    """The lift `PrepareShadowsMidtonesHighlights` adds to all three channels, from `w`.

    `num = w * (sign(w) < 0 ? 1 : 4)` — the game's own ASYMMETRIC scale: a negative w counts
    once, a positive one four times. Verified against decompiled
    `UnityEngine.Rendering.ColorUtils`.
    """
    w = float(rgbw[3]) if len(rgbw) > 3 else 0.0
    return w * (1.0 if w < 0.0 else 4.0)


def prepare_smh(rgbw):
    """One authored `[r, g, b, w]` through `PrepareShadowsMidtonesHighlights`, ported whole.

    `gamma_to_linear` on rgb, then `max(component + offset, 0)`. The offset half was missing
    here until 2026-08-14: `w` was sliced off with `[:3]` and the sources string still claimed
    the step was an exact managed port. Dormant on every rip so far — all three `w` read 0.0 —
    but a volume authoring w = 0.05 grades every shadow-range pixel 0.2 too dark on all three
    channels, and nothing would have said so.
    """
    offset = smh_offset(rgbw)
    return tuple(max(gamma_to_linear(c) + offset, 0.0) for c in rgbw[:3])


def hex_of(rgb_srgb):
    """`#rrggbb` of an sRGB-encoded colour."""
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, v)) * 255)):02x}" for v in rgb_srgb)


def hsv_of(rgb):
    """`(hue degrees, saturation, value)` of one rgb triple."""
    mx, mn = max(rgb), min(rgb)
    d = mx - mn
    if d < 1e-9:
        hue = 0.0
    elif mx == rgb[0]:
        hue = ((rgb[1] - rgb[2]) / d) % 6
    elif mx == rgb[1]:
        hue = (rgb[2] - rgb[0]) / d + 2
    else:
        hue = (rgb[0] - rgb[1]) / d + 4
    return (hue * 60.0) % 360.0, (d / mx if mx > 0 else 0.0), mx


def mat_mul_vec(m, v):
    """3x3 times a 3-vector."""
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))


def mat_mul(a, b):
    """3x3 times 3x3."""
    return tuple(
        tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)) for r in range(3)
    )


# -- HDRP white balance ---------------------------------------------------------------------
#
# `ColorUtils.ColorBalanceToLMSCoeffs` and the LMS matrices, ported from HDRP's managed code so
# the numbers are the game's rather than a look-alike. HDRP moves the white point in LMS (a
# linear space), so the whole operation collapses to a single 3x3 on linear RGB with no loss —
# which is what makes it expressible as an OCIO MatrixTransform.

_LIN_TO_LMS = (
    (3.90405e-1, 5.49941e-1, 8.92632e-3),
    (7.08416e-2, 9.63172e-1, 1.35775e-3),
    (2.31082e-2, 1.28021e-1, 9.36245e-1),
)
_LMS_TO_LIN = (
    (2.85847e0, -1.62879e0, -2.48910e-2),
    (-2.10182e-1, 1.15820e0, 3.24281e-4),
    (-4.18120e-2, -1.18169e-1, 1.06867e0),
)


def _standard_illuminant_y(x):
    return 2.87 * x - 3.0 * x * x - 0.27509507


def _cie_xy_to_lms(x, y):
    big_y = 1.0
    big_x = big_y * x / y
    big_z = big_y * (1.0 - x - y) / y
    return (
        0.7328 * big_x + 0.4296 * big_y - 0.1624 * big_z,
        -0.7036 * big_x + 1.6975 * big_y + 0.0061 * big_z,
        0.0030 * big_x + 0.0136 * big_y + 0.9834 * big_z,
    )


def white_balance_matrix(temperature, tint):
    """HDRP's white balance as a 3x3 on linear RGB. Identity when both are 0."""
    if temperature == 0.0 and tint == 0.0:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    t1 = temperature / 65.0
    t2 = tint / 65.0
    x = 0.31271 - t1 * (0.1 if t1 < 0 else 0.05)
    y = _standard_illuminant_y(x) + t2 * 0.05
    d65 = (0.949237, 1.03542, 1.08728)  # D65 in LMS, HDRP's own constant
    target = _cie_xy_to_lms(x, y)
    coeffs = tuple(d65[i] / target[i] for i in range(3))
    scaled = tuple(tuple(_LIN_TO_LMS[r][c] * coeffs[r] for c in range(3)) for r in range(3))
    return mat_mul(_LMS_TO_LIN, scaled)


def saturation_matrix(saturation, luma=LUMA):
    """HDRP's `lerp(luminance, colour, saturation)` written as a 3x3.

    Being a matrix is the point: it means the whole operator can live in a view transform, where
    it runs on final radiance in the right order. Applied to ALBEDO instead it is exact only under
    achromatic light, and on this city's grass the reordering costs 18% on the blue channel — the
    same size as the effect under investigation, which is why it is not done that way.
    """
    return tuple(
        tuple(
            saturation * (1.0 if r == c else 0.0) + (1.0 - saturation) * luma[c] for c in range(3)
        )
        for r in range(3)
    )
