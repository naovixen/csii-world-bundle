"""The game's colour grading (§11), as one derivation — importer, probe and docs all import it.

WHY THIS FILE EXISTS. `grading.json` ships the game's resolved HDRP grading as *HDRP's own
parameters* (saturation +30 on a -100..100 scale, contrast +20, white balance temperature +15).
Nothing downstream can use those directly: Blender has no "HDRP saturation" control. Turning them
into something a renderer can apply is a derivation with several steps, and doing it twice — once
in the importer, once in whatever measures the result — is how the last attempt ended up comparing
two different transforms and calling the difference a discovery. So it is done once, here, and
both ends import it.

EVERY CONSTANT BELOW IS DERIVED, AND SAYS FROM WHAT. The rule this repo works under is that a
number with no derivation beside it is not allowed, because a fitted constant cannot tell you it
is absorbing a bug: a hand-fitted saturation of 3.1x was once shown to be hiding three unrelated
defects at once, against the game's real value of 1.3x.

WHAT IS EXACT AND WHAT IS NOT
-----------------------------
Exact — ported maths, no approximation:
  * exposure          EV adds. `compensation + postExposure`.
  * colour filter     a linear per-channel multiplier.
  * white balance     HDRP's `ColorUtils.ColorBalanceToLMSCoeffs`, ported. HDRP scales in LMS,
                      which is linear, so it collapses to a 3x3 matrix on linear RGB with no loss.
  * contrast          HDRP pivots about ACEScc mid grey in log space. That is algebraically a
                      per-channel power law plus a scalar, both of which OCIO and Blender can
                      express natively — see `contrast_exponent` for the derivation.
  * saturation        HDRP's `lerp(luminance, colour, saturation)` is a 3x3 matrix.
  * shadows/midtones/highlights — the three colours through
                      `ColorUtils.PrepareShadowsMidtonesHighlights`, ported. That step applies
                      `Mathf.GammaToLinearSpace` first, which nearly doubles the correction: this
                      city's shadows (1, 0.979, 0.9239) are really (1, 0.953, 0.836). Reading them
                      as linear multipliers left a third of the blue error on the table.

One step is modelled rather than read:
  * the SMH BLEND across the three ranges is luminance-weighted, and that weighting is HLSL. It is
    reproduced as smoothstep over Rec.709 luminance against the shipped start/end limits, which is
    Unity's standard form. Being luminance-dependent it is also the only step that is neither a
    matrix nor a power, so the generated OCIO config carries it as a log2 shaper plus a 3D LUT.

Not replayed, and it says so rather than approximating:
  * hue shift — an HSV rotation, not a matrix; this bundle's value is 0 so nothing is lost here.
    A non-zero value warns instead of guessing.

ASSUMPTIONS THAT CANNOT BE CHECKED FROM HERE. HDRP's grading maths lives in HLSL, which is not
shipped and cannot be decompiled (`tools/decomp` reads managed code only). Two things are
therefore taken from HDRP's documented behaviour rather than read:
  * the ORDER — exposure, white balance, filter, contrast, hue/saturation, SMH, tonemap. This is
    what `grading.json` itself records, from the same source.
  * the WORKING SPACE — this module grades in ACEScg, which is where HDRP's HDR path works. It
    used to grade in linear Rec.709 on the strength of a check that put the difference "under 2%";
    that check compared PRE-TONEMAP values on ONE surface and was badly misleading. Measured
    through the tonemap against three in-game surfaces the space is one of the largest terms in
    the chain. Where exactly the white balance sits relative to the space change is the part that
    still cannot be read; applying it in ACEScg fits the measurements best.

WHAT A CONSUMER NEEDS FROM THIS. `load(bundle_dir)` returns a `Grading`, or `None` when the
bundle ships no `grading.json`. From there:

  * `grading.apply(linear_rgb)` — the graded, pre-display value. Every step but the tonemap.
  * `grading.exposure_ev`, `.white_balance`, `.saturation_matrix`, `.contrast_exponent`, … — the
    same chain as separate constants, for a renderer that would rather apply them in its own
    pipeline than call into Python per pixel.
  * `grading.ocio_view_transform()` — the whole chain as an OCIO view transform, which is how it
    can run on final radiance in the right order in any OCIO-aware application; and
    `grading.write_smh_lut(path)` for the one step a matrix cannot express. Writing a config for
    a PARTICULAR application is that application's job — `tools/colour_model.write_ocio` does it
    for Blender by extending Blender's own shipped config.
  * `grading.warnings` — what was NOT replayed. Read it. A non-zero hue shift or an external LUT
    means the model is knowingly incomplete for this bundle.

**Apply it to radiance, not to albedo.** Every step here belongs after lighting. Folding the
saturation into the material instead costs 18% on the blue channel of this city's grass — the
same size as the effect the derivation exists to measure.

USAGE (the CLI lives in `tools/colour_model.py`, which is this module plus a Blender locator)

    python tools/colour_model.py --explain                  # the whole derivation, with sources
    python tools/colour_model.py --predict 0.1848 0.1629 0.1334      # linear albedo -> pre-display
    python tools/colour_model.py --write-ocio DIR           # emit an OCIO config

Run it under Blender's Python (`blender.exe --background --python colour_model.py -- ...`) and
`--predict` also reports the final on-screen colour, because that build bundles PyOpenColorIO.
"""

import json
import math
import os

# Rec.709 luminance weights, for anything measured in the scene's own space.
LUMA = (0.2126, 0.7152, 0.0722)

# How `pick_view` finds an OCIO config when `$OCIO` is unset: a callable returning a path, or
# None. Left unset here on purpose — WHERE a config lives is a property of the host application,
# not of the grading, and this module has no business knowing about any particular one. The
# Blender adapter (`tools/colour_model.py`) installs its own locator.
CONFIG_LOCATOR = None

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

# The view transform that matches a given HDRP tonemapping mode, in Blender's shipped config.
# Blender 5.2 ships ACES 1.3 and ACES 2.0 as view transforms; an earlier note in this repo that
# "Blender has no ACES view transform" was wrong and cost a session.
TONEMAP_TO_VIEW = {
    "aces": "ACES 1.3",
    "neutral": "Khronos PBR Neutral",
    "none": "Standard",
    "external": None,  # a 3D LUT the game blends per time of day; see `Grading.warnings`
}


# -- small colour helpers -------------------------------------------------------------------


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


def _smh_offset(rgbw):
    """The lift `PrepareShadowsMidtonesHighlights` adds to all three channels, from `w`.

    `num = w * (sign(w) < 0 ? 1 : 4)` — the game's own ASYMMETRIC scale: a negative w counts
    once, a positive one four times. Verified against decompiled
    `UnityEngine.Rendering.ColorUtils`.
    """
    w = float(rgbw[3]) if len(rgbw) > 3 else 0.0
    return w * (1.0 if w < 0.0 else 4.0)


def _prepare_smh(rgbw):
    """One authored `[r, g, b, w]` through `PrepareShadowsMidtonesHighlights`, ported whole.

    `gamma_to_linear` on rgb, then `max(component + offset, 0)`. The offset half was missing
    here until 2026-08-14: `w` was sliced off with `[:3]` and the sources string still claimed
    the step was an exact managed port. Dormant on every rip so far — all three `w` read 0.0 —
    but a volume authoring w = 0.05 grades every shadow-range pixel 0.2 too dark on all three
    channels, and nothing would have said so.
    """
    offset = _smh_offset(rgbw)
    return tuple(max(gamma_to_linear(c) + offset, 0.0) for c in rgbw[:3])


def hex_of(rgb_srgb):
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, v)) * 255)):02x}" for v in rgb_srgb)


def hsv_of(rgb):
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


def _mat_mul_vec(m, v):
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))


def _mat_mul(a, b):
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
    return _mat_mul(_LMS_TO_LIN, scaled)


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


# -- the grading ------------------------------------------------------------------------------


class Grading:
    """`grading.json`, turned into constants a renderer can apply.

    Attributes are the derived form; `sources` records where each came from, and `warnings` what
    was left out. `explain()` prints both.
    """

    def __init__(self, document):
        self.raw = document or {}
        adjust = self.raw.get("colorAdjustments") or {}
        balance = self.raw.get("whiteBalance") or {}
        exposure = self.raw.get("exposure") or {}
        tonemap = self.raw.get("tonemapping") or {}
        self.smh = self.raw.get("shadowsMidtonesHighlights") or {}
        # The illumination the whole chain above sits ON TOP OF. Parsed rather than left in
        # `raw` because a consumer that reproduces the albedo and the grading but lights the
        # scene white still misses the game's colour — the bundle's own semantics string says
        # exactly that, and the sun here is 110,000 lux at 66 degrees, not a default tilt.
        self.lighting = Lighting(self.raw.get("lighting"))

        self.sources = []
        self.warnings = []

        # Exposure. HDRP's two EV contributions add; both are stops, and stops add.
        self.compensation = float(exposure.get("compensation", 0.0) or 0.0)
        self.post_exposure = float(adjust.get("postExposure", 0.0) or 0.0)
        self.exposure_mode = str(exposure.get("mode", "") or "")
        self.sources.append(
            f"exposure EV {self.compensation:+.4g} (Exposure.compensation) "
            f"{self.post_exposure:+.4g} (ColorAdjustments.postExposure) = "
            f"{self.exposure_ev:+.4g} stops"
        )

        # Colour filter and white balance are both linear per-channel/linear-matrix operations,
        # so they fold into one matrix applied before the contrast power.
        self.color_filter = tuple(float(c) for c in (adjust.get("colorFilter") or [1.0, 1.0, 1.0]))[
            :3
        ]
        self.wb_temperature = float(balance.get("temperature", 0.0) or 0.0)
        self.wb_tint = float(balance.get("tint", 0.0) or 0.0)
        wb = white_balance_matrix(self.wb_temperature, self.wb_tint)
        self.gain_matrix = tuple(
            tuple(self.color_filter[r] * wb[r][c] for c in range(3)) for r in range(3)
        )
        self.sources.append(
            f"gain matrix = colorFilter {tuple(round(c, 4) for c in self.color_filter)} x "
            f"WhiteBalance(temperature {self.wb_temperature:g}, tint {self.wb_tint:g}) through "
            f"HDRP ColorUtils.ColorBalanceToLMSCoeffs; on neutral that is "
            f"{tuple(round(v, 4) for v in _mat_mul_vec(self.gain_matrix, (1.0, 1.0, 1.0)))}"
        )

        # Contrast. HDRP pivots about ACEScc mid grey in log space:
        #     v  = (log2(x) + 9.72) / 17.52                 (the ACEScc encoding)
        #     v' = (v - M) * C + M                          (the contrast operator, M = mid grey)
        # Substituting and solving for x' gives a per-channel POWER LAW and a scalar:
        #     x' = x^C * 2^(K * (C - 1)),  K = 9.72 - 17.52 * M = 2.4739
        # which is why contrast needs no curve fitting and no LUT: `contrast_exponent` goes in an
        # OCIO ExponentTransform (or Blender's Color Management gamma, which is x^(1/g)), and
        # `contrast_scale` is one more linear multiply.
        self.contrast = float(adjust.get("contrast", 0.0) or 0.0)
        self.contrast_exponent = 1.0 + self.contrast / 100.0
        k = ACESCC_OFFSET - ACESCC_SCALE * ACESCC_MIDGRAY
        self.contrast_scale = 2.0 ** (k * (self.contrast_exponent - 1.0))
        self.sources.append(
            f"contrast {self.contrast:+g} -> x^{self.contrast_exponent:g} x "
            f"{self.contrast_scale:.5g}, from the ACEScc pivot about mid grey "
            f"{ACESCC_MIDGRAY} (K = 9.72 - 17.52*M = {k:.4f}); exact, not fitted"
        )

        # Saturation, about AP1 luminance because the whole chain runs in ACEScg.
        self.saturation = max(0.0, 1.0 + float(adjust.get("saturation", 0.0) or 0.0) / 100.0)
        self.saturation_matrix = saturation_matrix(self.saturation, AP1_LUMA)
        self.sources.append(
            f"saturation {float(adjust.get('saturation', 0.0) or 0.0):+g} -> "
            f"x{self.saturation:g} as a 3x3 about AP1 luminance {AP1_LUMA}"
        )
        self.sources.append(
            "working space: ACEScg (AP1). HDRP's HDR grading path works there, and measured "
            "against three in-game surfaces it is one of the biggest terms in the chain - "
            "grading in linear Rec.709 instead left grass 23% out on blue"
        )

        # Tonemapping -> the view transform that stands in for it.
        self.tonemap_mode = str(tonemap.get("mode", "") or "")
        self.view_transform = TONEMAP_TO_VIEW.get(self.tonemap_mode.lower(), "Standard")
        self.sources.append(
            f"tonemapping {self.tonemap_mode or '(absent)'} -> Blender view transform "
            f"{self.view_transform or '(none - see warnings)'}"
        )

        hue = float(adjust.get("hueShift", 0.0) or 0.0)
        self.hue_shift = hue
        if hue:
            self.warnings.append(
                f"hueShift {hue:+g} deg is NOT replayed: it is an HSV rotation, not a matrix, so "
                f"it cannot ride the same linear chain. Nothing else in this bundle needs it "
                f"(the value is normally 0)"
            )
        if self.smh:
            self.smh_prepared = {
                k: _prepare_smh(self.smh[k])
                for k in ("shadows", "midtones", "highlights")
                if k in self.smh
            }
            offsets = {k: round(_smh_offset(self.smh[k]), 4) for k in self.smh_prepared}
            self.sources.append(
                "shadows/midtones/highlights: each authored colour through "
                "ColorUtils.PrepareShadowsMidtonesHighlights (managed, ported) — "
                "Mathf.GammaToLinearSpace on rgb, then + w*(w<0 ? 1 : 4) clamped at 0, so "
                f"shadows {tuple(self.smh['shadows'][:3])} with w {self.smh['shadows'][3]} "
                f"become {tuple(round(c, 4) for c in self.smh_prepared['shadows'])}; blended "
                "across the three ranges by luminance against "
                f"[{self.smh.get('shadowsStart')}, {self.smh.get('shadowsEnd')}] and "
                f"[{self.smh.get('highlightsStart')}, {self.smh.get('highlightsEnd')}]"
            )
            if any(offsets.values()):
                self.sources.append(
                    f"shadows/midtones/highlights offsets from w: {offsets} — the game's own "
                    "asymmetric scale (a NEGATIVE w counts once, a positive one four times)"
                )
            self.warnings.append(
                "the shadows/midtones/highlights BLEND is the one modelled step in this file. The "
                "values are exact (managed code) but the weighting across the three ranges is "
                "HLSL: it is reproduced here as smoothstep over Rec.709 luminance, which is the "
                "standard Unity form. Everything else in the chain is ported or algebraic"
            )
        if self.tonemap_mode.lower() == "external":
            self.warnings.append(
                "tonemapping is External, i.e. the game was using a 3D LUT (Game.Rendering."
                "LightingSystem blends DayLUT/NightLUT/SunriseAndSunsetLUT per time of day). No "
                "view transform stands in for that; the LUT itself is needed"
            )
        if self.exposure_mode.lower().startswith("automatic"):
            self.warnings.append(
                f"exposure mode is {self.exposure_mode}: the game meters every frame, so absolute "
                f"brightness is a property of where the camera was pointed, not of the city. The "
                f"{self.exposure_ev:+.2f} EV above is the manual offset only - set Color "
                f"Management > Exposure to taste"
            )

    @property
    def exposure_ev(self):
        return self.compensation + self.post_exposure

    def colour_management(self, view_available):
        """What to put in Blender's Color Management, and what that leaves out.

        Returns `(view_transform, exposure_ev, gamma, light_gain, missing)`.

        With the generated OCIO config active (`view_available`), the entire chain rides the view
        transform and Color Management carries nothing but the exposure — which is right, because
        exposure is the one term the bundle genuinely cannot supply (the game meters per frame).

        Without it, the per-channel half of the chain still fits Blender's native controls and
        stays viewport-visible:
          * the gain matrix goes on the LIGHTS. Scaling every light per channel scales the whole
            render per channel, exactly, bounces included, so this is not an approximation.
          * contrast is `x^C`, and Blender's gamma is `x^(1/g)`, hence `gamma = 1/C`. The scalar
            that rides with it folds into exposure: multiplying by S after a power of C is the
            same as adding log2(S)/C stops before it.
          * saturation is cross-channel and has nowhere to go. It is reported as missing rather
            than moved onto the materials, where the reordering costs up to 18% on blue.
        """
        neutral_gain = _mat_mul_vec(self.gain_matrix, (1.0, 1.0, 1.0))
        if view_available:
            return (self.view_name, self.exposure_ev, 1.0, (1.0, 1.0, 1.0), [])
        missing = []
        if self.saturation != 1.0:
            missing.append(
                f"saturation x{self.saturation:g} (cross-channel; needs the view "
                f"transform, see --write-ocio)"
            )
        exposure = self.exposure_ev
        gamma = 1.0
        if self.contrast_exponent != 1.0:
            gamma = 1.0 / self.contrast_exponent
            exposure += math.log(self.contrast_scale, 2.0) / self.contrast_exponent
        return (self.view_transform or "Standard", exposure, gamma, neutral_gain, missing)

    @property
    def view_name(self):
        """The name of the view the generated OCIO config adds."""
        return "CS2 Graded"

    # -- application ---------------------------------------------------------------------
    def apply_smh(self, linear_rgb):
        """HDRP's shadows/midtones/highlights: a per-channel gain weighted by luminance.

        The three colours are already gamma-decoded (`smh_prepared`) exactly as the game's own
        `PrepareShadowsMidtonesHighlights` does. The weighting is smoothstep over luminance
        against the shipped start/end limits — Unity's standard form, and the one part of this
        file taken from the shader's documented behaviour rather than read.

        Being luminance-dependent, this is the only step that is not a matrix or a power, which
        is why the generated OCIO config carries it as a shaper plus a 3D LUT instead.
        """
        if not self.smh:
            return linear_rgb
        shadows = self.smh_prepared.get("shadows", (1.0, 1.0, 1.0))
        midtones = self.smh_prepared.get("midtones", (1.0, 1.0, 1.0))
        highlights = self.smh_prepared.get("highlights", (1.0, 1.0, 1.0))

        def smoothstep(edge0, edge1, x):
            if edge1 <= edge0:
                return 0.0 if x < edge0 else 1.0
            t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
            return t * t * (3.0 - 2.0 * t)

        luma = sum(AP1_LUMA[i] * linear_rgb[i] for i in range(3))
        shadow_w = 1.0 - smoothstep(
            float(self.smh.get("shadowsStart", 0.0)), float(self.smh.get("shadowsEnd", 0.3)), luma
        )
        highlight_w = smoothstep(
            float(self.smh.get("highlightsStart", 0.55)),
            float(self.smh.get("highlightsEnd", 1.0)),
            luma,
        )
        mid_w = 1.0 - shadow_w - highlight_w
        return tuple(
            linear_rgb[i]
            * (shadows[i] * shadow_w + midtones[i] * mid_w + highlights[i] * highlight_w)
            for i in range(3)
        )

    def apply(self, linear_rgb, exposure_ev=None):
        """Grade one linear Rec.709 colour. Returns the scene-linear value the display sees.

        The tonemap is deliberately NOT applied here: it is a display transform and belongs to
        the view, not to the pixel. Feed the result to `display()` for the on-screen colour.
        """
        ev = self.exposure_ev if exposure_ev is None else exposure_ev
        c = tuple(v * (2.0**ev) for v in linear_rgb)
        c = _mat_mul_vec(SRGB_TO_AP1, c)  # into ACEScg, where HDRP grades
        c = _mat_mul_vec(self.gain_matrix, c)
        c = tuple(max(v, 0.0) ** self.contrast_exponent * self.contrast_scale for v in c)
        c = _mat_mul_vec(self.saturation_matrix, c)
        c = self.apply_smh(tuple(max(v, 0.0) for v in c))
        c = _mat_mul_vec(AP1_TO_SRGB, c)  # and back out to the scene's space
        return tuple(max(v, 0.0) for v in c)

    def pick_view(self, view=None):
        """Which view transform will actually be used, and the config carrying it.

        Returns `(config, display, view_name)`, or `(None, None, None)` when the on-screen colour
        cannot be computed here (no PyOpenColorIO, or no config to read).
        """
        try:
            import PyOpenColorIO as ocio
        except ImportError:
            return None, None, None
        path = os.environ.get("OCIO") or (CONFIG_LOCATOR() if CONFIG_LOCATOR else None)
        if not path or not os.path.isfile(path):
            return None, None, None
        config = ocio.Config.CreateFromFile(path)
        display = config.getDefaultDisplay()
        views = list(config.getViews(display))
        chosen = next(
            (
                v
                for v in (view, self.view_name, self.view_transform, "Standard")
                if v and v in views
            ),
            None,
        )
        return (config, display, chosen) if chosen else (None, None, None)

    def on_screen(self, albedo_linear, exposure_ev=None, view=None):
        """Albedo -> the colour on screen, grading it exactly ONCE. Returns `(rgb, view_used)`.

        Which half does the work depends on which view is available, and getting that wrong is a
        silent doubling: with the generated config active the VIEW carries the whole chain, so
        the albedo must be handed over raw (exposed only). Under a stock view the view carries
        only the tonemap, so the chain has to be applied here first.
        """
        ev = self.exposure_ev if exposure_ev is None else exposure_ev
        _config, _display, used = self.pick_view(view)
        if used is None:
            return None, None
        if used == self.view_name:
            scene_linear = tuple(v * (2.0**ev) for v in albedo_linear)
        else:
            scene_linear = self.apply(albedo_linear, ev)
        return self.display(scene_linear, used)[0], used

    def match_value(self, albedo_linear, target_value, view=None):
        """The exposure at which this albedo reaches `target_value` on screen, and the colour.

        Exposure is the one term the bundle genuinely cannot supply — the game meters every
        frame — so a comparison that does not free it is comparing brightness, not colour. Match
        on VALUE, then compare hue and saturation, which are the parts the bundle does determine.
        """
        low, high = -8.0, 12.0
        for _ in range(40):
            mid = (low + high) / 2.0
            rgb, used = self.on_screen(albedo_linear, mid, view)
            if rgb is None:
                return None, None, None
            if hsv_of(rgb)[2] < target_value:
                low = mid
            else:
                high = mid
        ev = (low + high) / 2.0
        rgb, used = self.on_screen(albedo_linear, ev, view)
        return ev, rgb, used

    def display(self, scene_linear, view=None):
        """Scene-linear -> on-screen sRGB through a view transform. Returns `(rgb, view_used)`.

        Needs PyOpenColorIO, which Blender's bundled Python has and a system Python normally does
        not; returns `(None, None)` elsewhere, so callers fall back to rendering a pixel rather
        than reporting an untonemapped value as if it were the screen colour.

        The config is loaded BY PATH — `$OCIO` if the generated one is active, otherwise
        Blender's shipped one. `Config.CreateFromEnv()` alone would quietly return OCIO's own
        built-in config when `$OCIO` is unset, whose views are not Blender's, and the numbers
        would look plausible and mean nothing.
        """
        config, display, chosen = self.pick_view(view)
        if chosen is None:
            return None, None
        import PyOpenColorIO as ocio

        cpu = config.getProcessor(
            ocio.ROLE_SCENE_LINEAR, display, chosen, ocio.TRANSFORM_DIR_FORWARD
        ).getDefaultCPUProcessor()
        return tuple(cpu.applyRGB(list(scene_linear))), chosen

    # -- the OCIO config -----------------------------------------------------------------
    #
    # The shaper range for the SMH LUT. A 3D LUT is indexed on [0, 1], but scene-linear values
    # are unbounded, so the data is put through a log2 allocation first and the LUT is built in
    # that same space. These are the bounds Blender's own AgX config uses for the same job
    # (2^-12.47 to 2^12.53, i.e. 25 stops), which is more range than any terrain needs and keeps
    # this consistent with the config it is being merged into.
    SHAPER_MIN = -12.47393
    SHAPER_MAX = 12.5260688117

    def write_smh_lut(self, path, size=33):
        """Write the shadows/midtones/highlights step as a 3D LUT in the shaper's log space.

        Everything else in the chain is a matrix or a power, which OCIO expresses natively and
        auditably. This one step is luminance-weighted, so it cannot be either — and rather than
        approximate it with something that IS a matrix (which is how a grade quietly becomes a
        different grade), it is sampled exactly onto a grid.
        """
        span = self.SHAPER_MAX - self.SHAPER_MIN
        lines = [
            'TITLE "CS2 shadows/midtones/highlights, from grading.json"',
            f"LUT_3D_SIZE {size}",
            "DOMAIN_MIN 0.0 0.0 0.0",
            "DOMAIN_MAX 1.0 1.0 1.0",
            "",
        ]

        # LOG IN, LOG OUT — and the config puts the shaper back afterwards. A LUT that took log
        # coordinates but emitted LINEAR values interpolated a multiply as a straight line
        # between exponentially spaced nodes, which measured 2/255 off against the analytic
        # model. In log space a per-channel gain is an offset, so the same grid is near-exact.
        def encode(value):
            value = max(value, 2.0**self.SHAPER_MIN)
            return min(1.0, max(0.0, (math.log(value, 2.0) - self.SHAPER_MIN) / span))

        # .cube order: red varies fastest, then green, then blue.
        axis = [2.0 ** (self.SHAPER_MIN + span * (i / (size - 1.0))) for i in range(size)]
        for b in axis:
            for g in axis:
                for r in axis:
                    lines.append(
                        "{:.7g} {:.7g} {:.7g}".format(
                            *tuple(encode(v) for v in self.apply_smh((r, g, b)))
                        )
                    )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def ocio_view_transform(self, name, smh_lut=None):
        """This grading as an OCIO ViewTransform, as YAML.

        A view transform is the only place in Blender where a CROSS-CHANNEL operator can run on
        final radiance and still show in the viewport: the compositor is render-only, and Color
        Management's own controls (exposure, gamma, curves) are all per-channel. Saturation is a
        3x3, so it has to be here.
        """

        def matrix44(m, scale=1.0):
            rows = []
            for r in range(3):
                rows.append(", ".join("%.10g" % (m[r][c] * scale) for c in range(3)) + ", 0")
            rows.append("0, 0, 0, 1")
            return "[" + ", ".join(rows) + "]"

        children = [
            "        # into the scene's own linear space, then into ACEScg where HDRP grades.",
            "        # The AP1 matrix is written out rather than using the config's ACEScg space",
            "        # so this chain and tools/colour_model.py are the same arithmetic by",
            "        # construction - they are checked against each other to 0.1/255.",
            "        - !<ColorSpaceTransform> {src: ACES2065-1, dst: Linear Rec.709}",
            f"        - !<MatrixTransform> {{matrix: {matrix44(SRGB_TO_AP1)}}}",
            "        # colour filter x white balance (HDRP ColorBalanceToLMSCoeffs, ported)",
            f"        - !<MatrixTransform> {{matrix: {matrix44(self.gain_matrix)}}}",
        ]
        if self.contrast_exponent != 1.0:
            children += [
                f"        # contrast {self.contrast:+g}, as the ACEScc log pivot solved into a power and a scalar",
                # ExponentTransform clamps negatives by default, which is HDRP's own max(0, ...).
                "        - !<ExponentTransform> {value: [%.10g, %.10g, %.10g, 1]}"
                % ((self.contrast_exponent,) * 3),
                f"        - !<MatrixTransform> {{matrix: {matrix44(((1, 0, 0), (0, 1, 0), (0, 0, 1)), self.contrast_scale)}}}",
            ]
        if self.saturation != 1.0:
            children += [
                f"        # saturation x{self.saturation:g} about Rec.709 luminance",
                f"        - !<MatrixTransform> {{matrix: {matrix44(self.saturation_matrix)}}}",
            ]
        if smh_lut:
            children += [
                "        # shadows/midtones/highlights - luminance-weighted, so neither a matrix",
                "        # nor a power. Sampled onto a grid in a log2 shaper (log in, log out, so",
                "        # a per-channel gain is an offset and interpolates cleanly), then undone.",
                f"        - !<AllocationTransform> {{allocation: lg2, vars: [{self.SHAPER_MIN:.8g}, {self.SHAPER_MAX:.8g}]}}",
                f"        - !<FileTransform> {{src: {smh_lut}, interpolation: tetrahedral}}",
                f"        - !<AllocationTransform> {{allocation: lg2, vars: [{self.SHAPER_MIN:.8g}, {self.SHAPER_MAX:.8g}], "
                "direction: inverse}",
            ]
        children += [
            "        # out of ACEScg, back to the scene's linear space for the view's tonemap",
            f"        - !<MatrixTransform> {{matrix: {matrix44(AP1_TO_SRGB)}}}",
            "        - !<ColorSpaceTransform> {src: Linear Rec.709, dst: ACES2065-1}",
            "        # the tonemap the grading above was authored against",
            "        - !<BuiltinTransform> {style: ACES-OUTPUT - "
            "ACES2065-1_to_CIE-XYZ-D65 - SDR-VIDEO_1.0}",
        ]
        return (
            "  - !<ViewTransform>\n"
            "    name: {}\n"
            "    family: CS2\n"
            "    description: |\n"
            "      Cities: Skylines II colour grading, derived from this bundle's "
            "grading.json\n"
            "    from_scene_reference: !<GroupTransform>\n"
            "      children:\n{}\n".format(name, "\n".join(children))
        )

    # -- reporting -----------------------------------------------------------------------
    def explain(self, say=print):
        say("  DERIVED FROM grading.json")
        for line in self.sources:
            say("    * " + line)
        for line in self.warnings:
            say("    ! " + line)


# -- the illumination the grading sits on -------------------------------------------------------


class DirectionalLight:
    """The sun or the moon, from `grading.json`'s `lighting` block.

    **`direction` is where the light GOES, not where it is** — the unit vector it travels along,
    in the bundle's left-handed Y-up frame. Negate it to point at the light. Getting this
    backwards puts the sun underground, which reads as "the import is too dark" rather than as a
    sign error.

    **The effective colour is a PRODUCT of two terms.** `color` is the light's authored filter;
    when `use_color_temperature` is set, HDRP multiplies it by the blackbody colour of
    `temperature` Kelvin. Both are needed. On the reference city the flag is off and the filter
    is white, which is why the warm cast there comes from the grading rather than from the light
    — but that is a property of one save, not of the format.

    `intensity` is **lux** for a directional light. `initial_intensity` is the authored value
    before the game scales it down toward the horizon (`PlanetarySystem` multiplies it by a
    smoothstep on the sun's height), so `intensity` is what was actually lighting the city at rip
    time and is the one to use.
    """

    def __init__(self, block, which):
        block = block or {}
        self.which = which
        self.raw = block
        self.enabled = bool(block.get("enabled", False))
        self.color = tuple(float(c) for c in (block.get("color") or (1.0, 1.0, 1.0))[:3])
        self.use_color_temperature = bool(block.get("useColorTemperature", False))
        self.temperature = float(block.get("colorTemperature", 6500.0) or 6500.0)
        self.intensity = float(block.get("intensity", 0.0) or 0.0)
        self.initial_intensity = float(block.get("initialIntensity", self.intensity) or 0.0)
        self.intensity_units = str(block.get("intensityUnits", "") or "")
        travel = block.get("direction")
        self.travel = tuple(float(v) for v in travel[:3]) if travel and len(travel) >= 3 else None
        self.elevation_degrees = block.get("elevationDegrees")
        self.azimuth_degrees = block.get("azimuthDegrees")
        self.shadows = str(block.get("shadows", "") or "")

    @property
    def toward(self):
        """The unit vector pointing AT the light, bundle frame, or `None`.

        `None` also when the light is at or below the horizon — a night rip lights nothing, and
        a consumer placing a sun there gets a scene lit from underneath.
        """
        if not self.travel:
            return None
        x, y, z = (-v for v in self.travel)
        length = math.sqrt(x * x + y * y + z * z)
        if length < 1e-6 or y <= 0.0:
            return None
        return (x / length, y / length, z / length)

    def __repr__(self):
        return (
            f"<{self.which} {'on' if self.enabled else 'off'} "
            f"{self.intensity:g} {self.intensity_units.split()[0] if self.intensity_units else ''} "
            f"elev {self.elevation_degrees}>"
        )


class Sky:
    """HDRP's PhysicallyBasedSky — the AMBIENT half, and what fills every shadow.

    `exposure` is EV on the sky's own contribution and the three tints multiply it. A consumer
    that lights only with the sun gets black shadows where the game has blue-grey ones, which is
    the second most common reason a correct import looks wrong (the first being white light).
    """

    def __init__(self, block):
        block = block or {}
        self.raw = block
        self.exposure = float(block.get("exposure", 0.0) or 0.0)
        self.zenith_tint = tuple(float(c) for c in (block.get("zenithTint") or (1, 1, 1))[:3])
        self.horizon_tint = tuple(float(c) for c in (block.get("horizonTint") or (1, 1, 1))[:3])
        self.ground_tint = tuple(float(c) for c in (block.get("groundTint") or (1, 1, 1))[:3])


class Lighting:
    """`grading.json`'s `lighting` block: sun, moon, sky, indirect multipliers and the clock.

    Everything else in this module is a transform applied to radiance. This is where the
    radiance comes from, and it is the half a consumer is most likely to invent instead of read.

    `time` matters more than it looks: the sun's angle and colour follow the in-game clock and
    the map's latitude/longitude, so a reference screenshot taken at another hour is not
    describing this grading and cannot be used to check it.
    """

    def __init__(self, block):
        block = block or {}
        self.raw = block
        self.sun = DirectionalLight(block.get("sun"), "sun")
        self.moon = DirectionalLight(block.get("moon"), "moon")
        self.sky = Sky(block.get("sky"))
        indirect = block.get("indirect") or {}
        self.indirect_diffuse = float(indirect.get("diffuseMultiplier", 1.0) or 1.0)
        self.indirect_reflection = float(indirect.get("reflectionMultiplier", 1.0) or 1.0)
        self.time = block.get("time") or {}

    @property
    def key_light(self):
        """Whichever of sun/moon is actually lighting the scene, or `None` for a dark rip."""
        for light in (self.sun, self.moon):
            if light.enabled and light.intensity > 0.0 and light.toward is not None:
                return light
        return None

    def present(self):
        """Whether the bundle carried a lighting block at all (older rips did not)."""
        return bool(self.raw)


def load(bundle_dir):
    """This bundle's `grading.json` as a `Grading`, or `None` when the layer is absent.

    Absent is normal and not an error: a partial rip may not have run the stage, and a bundle
    with no grading means "draw the albedo", which is a faithful 1:1 view of what was ripped.
    """
    path = os.path.join(bundle_dir, "grading.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return Grading(json.load(handle))
