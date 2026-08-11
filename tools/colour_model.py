"""The game's colour grading, as one derivation — shared by the importer, the probe and the docs.

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

USAGE

    python tools/colour_model.py --explain                  # the whole derivation, with sources
    python tools/colour_model.py --predict 0.1848 0.1629 0.1334      # linear albedo -> pre-display
    python tools/colour_model.py --write-ocio DIR           # emit a Blender OCIO config

Run it under Blender's Python (`blender.exe --background --python colour_model.py -- ...`) and
`--predict` also reports the final on-screen colour, because that build bundles PyOpenColorIO.
"""

import json
import math
import os
import sys

DEFAULT_BUNDLE = os.path.join(
    os.path.expanduser("~"), "AppData", "LocalLow", "Colossal Order",
    "Cities Skylines II", "ModsData", "Carto")

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
SRGB_TO_AP1 = ((0.6130974, 0.3395231, 0.0473795),
               (0.0701937, 0.9163538, 0.0134526),
               (0.0206156, 0.1095698, 0.8698146))
# Inverted rather than transcribed: a hand-typed inverse is a silent-error waiting to happen and
# nothing downstream would show it as anything but a slight colour cast.
def _mat_inverse(m):
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    return tuple(tuple(((m[(c + 1) % 3][(r + 1) % 3] * m[(c + 2) % 3][(r + 2) % 3]
                         - m[(c + 1) % 3][(r + 2) % 3] * m[(c + 2) % 3][(r + 1) % 3]) / det)
                       for c in range(3)) for r in range(3))


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
    "external": None,        # a 3D LUT the game blends per time of day; see `Grading.warnings`
}


# -- small colour helpers -------------------------------------------------------------------

def linear_to_srgb(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


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
    return c ** 2.2


def hex_of(rgb_srgb):
    return "#" + "".join("%02x" % int(round(max(0.0, min(1.0, v)) * 255)) for v in rgb_srgb)


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
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3))
                 for r in range(3))


# -- HDRP white balance ---------------------------------------------------------------------
#
# `ColorUtils.ColorBalanceToLMSCoeffs` and the LMS matrices, ported from HDRP's managed code so
# the numbers are the game's rather than a look-alike. HDRP moves the white point in LMS (a
# linear space), so the whole operation collapses to a single 3x3 on linear RGB with no loss —
# which is what makes it expressible as an OCIO MatrixTransform.

_LIN_TO_LMS = ((3.90405e-1, 5.49941e-1, 8.92632e-3),
               (7.08416e-2, 9.63172e-1, 1.35775e-3),
               (2.31082e-2, 1.28021e-1, 9.36245e-1))
_LMS_TO_LIN = ((2.85847e+0, -1.62879e+0, -2.48910e-2),
               (-2.10182e-1, 1.15820e+0, 3.24281e-4),
               (-4.18120e-2, -1.18169e-1, 1.06867e+0))


def _standard_illuminant_y(x):
    return 2.87 * x - 3.0 * x * x - 0.27509507


def _cie_xy_to_lms(x, y):
    big_y = 1.0
    big_x = big_y * x / y
    big_z = big_y * (1.0 - x - y) / y
    return (0.7328 * big_x + 0.4296 * big_y - 0.1624 * big_z,
            -0.7036 * big_x + 1.6975 * big_y + 0.0061 * big_z,
            0.0030 * big_x + 0.0136 * big_y + 0.9834 * big_z)


def white_balance_matrix(temperature, tint):
    """HDRP's white balance as a 3x3 on linear RGB. Identity when both are 0."""
    if temperature == 0.0 and tint == 0.0:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    t1 = temperature / 65.0
    t2 = tint / 65.0
    x = 0.31271 - t1 * (0.1 if t1 < 0 else 0.05)
    y = _standard_illuminant_y(x) + t2 * 0.05
    d65 = (0.949237, 1.03542, 1.08728)          # D65 in LMS, HDRP's own constant
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
    return tuple(tuple(saturation * (1.0 if r == c else 0.0) + (1.0 - saturation) * luma[c]
                       for c in range(3)) for r in range(3))


# -- the grading ------------------------------------------------------------------------------

class Grading(object):
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

        self.sources = []
        self.warnings = []

        # Exposure. HDRP's two EV contributions add; both are stops, and stops add.
        self.compensation = float(exposure.get("compensation", 0.0) or 0.0)
        self.post_exposure = float(adjust.get("postExposure", 0.0) or 0.0)
        self.exposure_mode = str(exposure.get("mode", "") or "")
        self.sources.append(
            f"exposure EV {self.compensation:+.4g} (Exposure.compensation) "
            f"{self.post_exposure:+.4g} (ColorAdjustments.postExposure) = "
            f"{self.exposure_ev:+.4g} stops")

        # Colour filter and white balance are both linear per-channel/linear-matrix operations,
        # so they fold into one matrix applied before the contrast power.
        self.color_filter = tuple(float(c) for c in
                                  (adjust.get("colorFilter") or [1.0, 1.0, 1.0]))[:3]
        self.wb_temperature = float(balance.get("temperature", 0.0) or 0.0)
        self.wb_tint = float(balance.get("tint", 0.0) or 0.0)
        wb = white_balance_matrix(self.wb_temperature, self.wb_tint)
        self.gain_matrix = tuple(tuple(self.color_filter[r] * wb[r][c] for c in range(3))
                                 for r in range(3))
        self.sources.append(
            f"gain matrix = colorFilter {tuple(round(c, 4) for c in self.color_filter)} x "
            f"WhiteBalance(temperature {self.wb_temperature:g}, tint {self.wb_tint:g}) through "
            f"HDRP ColorUtils.ColorBalanceToLMSCoeffs; on neutral that is "
            f"{tuple(round(v, 4) for v in _mat_mul_vec(self.gain_matrix, (1.0, 1.0, 1.0)))}")

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
            f"{ACESCC_MIDGRAY} (K = 9.72 - 17.52*M = {k:.4f}); exact, not fitted")

        # Saturation, about AP1 luminance because the whole chain runs in ACEScg.
        self.saturation = max(0.0, 1.0 + float(adjust.get("saturation", 0.0) or 0.0) / 100.0)
        self.saturation_matrix = saturation_matrix(self.saturation, AP1_LUMA)
        self.sources.append(
            f"saturation {float(adjust.get('saturation', 0.0) or 0.0):+g} -> "
            f"x{self.saturation:g} as a 3x3 about AP1 luminance {AP1_LUMA}")
        self.sources.append(
            "working space: ACEScg (AP1). HDRP's HDR grading path works there, and measured "
            "against three in-game surfaces it is one of the biggest terms in the chain - "
            "grading in linear Rec.709 instead left grass 23% out on blue")

        # Tonemapping -> the view transform that stands in for it.
        self.tonemap_mode = str(tonemap.get("mode", "") or "")
        self.view_transform = TONEMAP_TO_VIEW.get(self.tonemap_mode.lower(), "Standard")
        self.sources.append(
            f"tonemapping {self.tonemap_mode or '(absent)'} -> Blender view transform "
            f"{self.view_transform or '(none - see warnings)'}")

        hue = float(adjust.get("hueShift", 0.0) or 0.0)
        self.hue_shift = hue
        if hue:
            self.warnings.append(
                f"hueShift {hue:+g} deg is NOT replayed: it is an HSV rotation, not a matrix, so "
                f"it cannot ride the same linear chain. Nothing else in this bundle needs it "
                f"(the value is normally 0)")
        if self.smh:
            self.smh_prepared = {
                k: tuple(gamma_to_linear(c) for c in self.smh[k][:3])
                for k in ("shadows", "midtones", "highlights") if k in self.smh}
            self.sources.append(
                "shadows/midtones/highlights: each authored colour through "
                "ColorUtils.PrepareShadowsMidtonesHighlights (managed, ported) — it applies "
                f"Mathf.GammaToLinearSpace first, so shadows {tuple(self.smh['shadows'][:3])} "
                f"become {tuple(round(c, 4) for c in self.smh_prepared['shadows'])}; blended "
                "across the three ranges by luminance against "
                f"[{self.smh.get('shadowsStart')}, {self.smh.get('shadowsEnd')}] and "
                f"[{self.smh.get('highlightsStart')}, {self.smh.get('highlightsEnd')}]")
            self.warnings.append(
                "the shadows/midtones/highlights BLEND is the one modelled step in this file. The "
                "values are exact (managed code) but the weighting across the three ranges is "
                "HLSL: it is reproduced here as smoothstep over Rec.709 luminance, which is the "
                "standard Unity form. Everything else in the chain is ported or algebraic")
        if self.tonemap_mode.lower() == "external":
            self.warnings.append(
                "tonemapping is External, i.e. the game was using a 3D LUT (Game.Rendering."
                "LightingSystem blends DayLUT/NightLUT/SunriseAndSunsetLUT per time of day). No "
                "view transform stands in for that; the LUT itself is needed")
        if self.exposure_mode.lower().startswith("automatic"):
            self.warnings.append(
                f"exposure mode is {self.exposure_mode}: the game meters every frame, so absolute "
                f"brightness is a property of where the camera was pointed, not of the city. The "
                f"{self.exposure_ev:+.2f} EV above is the manual offset only - set Color "
                f"Management > Exposure to taste")

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
            missing.append(f"saturation x{self.saturation:g} (cross-channel; needs the view "
                           f"transform, see --write-ocio)")
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
        shadow_w = 1.0 - smoothstep(float(self.smh.get("shadowsStart", 0.0)),
                                    float(self.smh.get("shadowsEnd", 0.3)), luma)
        highlight_w = smoothstep(float(self.smh.get("highlightsStart", 0.55)),
                                 float(self.smh.get("highlightsEnd", 1.0)), luma)
        mid_w = 1.0 - shadow_w - highlight_w
        return tuple(linear_rgb[i] * (shadows[i] * shadow_w + midtones[i] * mid_w
                                      + highlights[i] * highlight_w) for i in range(3))

    def apply(self, linear_rgb, exposure_ev=None):
        """Grade one linear Rec.709 colour. Returns the scene-linear value the display sees.

        The tonemap is deliberately NOT applied here: it is a display transform and belongs to
        the view, not to the pixel. Feed the result to `display()` for the on-screen colour.
        """
        ev = self.exposure_ev if exposure_ev is None else exposure_ev
        c = tuple(v * (2.0 ** ev) for v in linear_rgb)
        c = _mat_mul_vec(SRGB_TO_AP1, c)                     # into ACEScg, where HDRP grades
        c = _mat_mul_vec(self.gain_matrix, c)
        c = tuple(max(v, 0.0) ** self.contrast_exponent * self.contrast_scale for v in c)
        c = _mat_mul_vec(self.saturation_matrix, c)
        c = self.apply_smh(tuple(max(v, 0.0) for v in c))
        c = _mat_mul_vec(AP1_TO_SRGB, c)                     # and back out to the scene's space
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
        path = os.environ.get("OCIO") or blender_config_path()
        if not path or not os.path.isfile(path):
            return None, None, None
        config = ocio.Config.CreateFromFile(path)
        display = config.getDefaultDisplay()
        views = list(config.getViews(display))
        chosen = next((v for v in (view, self.view_name, self.view_transform, "Standard")
                       if v and v in views), None)
        return (config, display, chosen) if chosen else (None, None, None)

    def on_screen(self, albedo_linear, exposure_ev=None, view=None):
        """Albedo -> the colour on screen, grading it exactly ONCE. Returns `(rgb, view_used)`.

        Which half does the work depends on which view is available, and getting that wrong is a
        silent doubling: with the generated config active the VIEW carries the whole chain, so
        the albedo must be handed over raw (exposed only). Under a stock view the view carries
        only the tonemap, so the chain has to be applied here first.
        """
        ev = self.exposure_ev if exposure_ev is None else exposure_ev
        config, display, used = self.pick_view(view)
        if used is None:
            return None, None
        if used == self.view_name:
            scene_linear = tuple(v * (2.0 ** ev) for v in albedo_linear)
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
        cpu = config.getProcessor(ocio.ROLE_SCENE_LINEAR, display, chosen,
                                  ocio.TRANSFORM_DIR_FORWARD).getDefaultCPUProcessor()
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
        lines = ['TITLE "CS2 shadows/midtones/highlights, from grading.json"',
                 f"LUT_3D_SIZE {size}",
                 "DOMAIN_MIN 0.0 0.0 0.0",
                 "DOMAIN_MAX 1.0 1.0 1.0",
                 ""]
        # LOG IN, LOG OUT — and the config puts the shaper back afterwards. A LUT that took log
        # coordinates but emitted LINEAR values interpolated a multiply as a straight line
        # between exponentially spaced nodes, which measured 2/255 off against the analytic
        # model. In log space a per-channel gain is an offset, so the same grid is near-exact.
        def encode(value):
            value = max(value, 2.0 ** self.SHAPER_MIN)
            return min(1.0, max(0.0, (math.log(value, 2.0) - self.SHAPER_MIN) / span))

        # .cube order: red varies fastest, then green, then blue.
        axis = [2.0 ** (self.SHAPER_MIN + span * (i / (size - 1.0))) for i in range(size)]
        for b in axis:
            for g in axis:
                for r in axis:
                    lines.append("%.7g %.7g %.7g"
                                 % tuple(encode(v) for v in self.apply_smh((r, g, b))))
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
            "        - !<MatrixTransform> {matrix: %s}" % matrix44(SRGB_TO_AP1),
            "        # colour filter x white balance (HDRP ColorBalanceToLMSCoeffs, ported)",
            "        - !<MatrixTransform> {matrix: %s}" % matrix44(self.gain_matrix),
        ]
        if self.contrast_exponent != 1.0:
            children += [
                "        # contrast %+g, as the ACEScc log pivot solved into a power and a scalar"
                % self.contrast,
                # ExponentTransform clamps negatives by default, which is HDRP's own max(0, ...).
                "        - !<ExponentTransform> {value: [%.10g, %.10g, %.10g, 1]}"
                % ((self.contrast_exponent,) * 3),
                "        - !<MatrixTransform> {matrix: %s}"
                % matrix44(((1, 0, 0), (0, 1, 0), (0, 0, 1)), self.contrast_scale),
            ]
        if self.saturation != 1.0:
            children += [
                "        # saturation x%g about Rec.709 luminance" % self.saturation,
                "        - !<MatrixTransform> {matrix: %s}" % matrix44(self.saturation_matrix),
            ]
        if smh_lut:
            children += [
                "        # shadows/midtones/highlights - luminance-weighted, so neither a matrix",
                "        # nor a power. Sampled onto a grid in a log2 shaper (log in, log out, so",
                "        # a per-channel gain is an offset and interpolates cleanly), then undone.",
                "        - !<AllocationTransform> {allocation: lg2, vars: [%.8g, %.8g]}"
                % (self.SHAPER_MIN, self.SHAPER_MAX),
                "        - !<FileTransform> {src: %s, interpolation: tetrahedral}" % smh_lut,
                "        - !<AllocationTransform> {allocation: lg2, vars: [%.8g, %.8g], "
                "direction: inverse}" % (self.SHAPER_MIN, self.SHAPER_MAX),
            ]
        children += [
            "        # out of ACEScg, back to the scene's linear space for the view's tonemap",
            "        - !<MatrixTransform> {matrix: %s}" % matrix44(AP1_TO_SRGB),
            "        - !<ColorSpaceTransform> {src: Linear Rec.709, dst: ACES2065-1}",
            "        # the tonemap the grading above was authored against",
            "        - !<BuiltinTransform> {style: ACES-OUTPUT - "
            "ACES2065-1_to_CIE-XYZ-D65 - SDR-VIDEO_1.0}",
        ]
        return ("  - !<ViewTransform>\n"
                "    name: %s\n"
                "    family: CS2\n"
                "    description: |\n"
                "      Cities: Skylines II colour grading, derived from this bundle's "
                "grading.json\n"
                "    from_scene_reference: !<GroupTransform>\n"
                "      children:\n%s\n" % (name, "\n".join(children)))

    def write_ocio(self, directory, blender_config, name="CS2 Graded"):
        """Write a Blender OCIO config that adds this grading as a view.

        Blender's shipped config is read and extended rather than replaced, so every other view
        (Standard, AgX, Filmic, plain ACES) still works and a scene can be flipped between them
        for comparison. The search path is rewritten to ABSOLUTE paths back into the Blender
        install, so the 19 MB of LUTs beside it do not have to be copied.

        THE SEARCH PATH IS WRITTEN AS A YAML LIST, and on Windows it has to be. OCIO's string
        form is colon-separated, and a Windows absolute path contains a colon after the drive
        letter — so `C:/x:G:/y` parses as four nonsense directories, and every FileTransform in
        the config stops resolving. That takes out AgX and Filmic as well as our own LUT, and it
        fails as "file not found" rather than as a parse error, so it reads like a missing file.
        """
        with open(blender_config, encoding="utf-8") as handle:
            text = handle.read()

        if not os.path.isdir(directory):
            os.makedirs(directory)
        lut = None
        if self.smh:
            lut = "cs2_smh.cube"
            self.write_smh_lut(os.path.join(directory, lut))

        base = os.path.dirname(os.path.abspath(blender_config))
        # Our own folder first, so the generated LUT resolves; then Blender's, so every view we
        # did not touch still finds the files it needs.
        search = ([os.path.abspath(directory).replace("\\", "/")]
                  + [os.path.join(base, part).replace("\\", "/")
                     for part in ("icc", "luts", "filmic")])
        out = []
        for line in text.splitlines():
            if line.startswith("search_path:"):
                line = "search_path: [%s]" % ", ".join('"%s"' % p for p in search)
            elif line.startswith("active_views:"):
                # A view that is not in `active_views` still WORKS if you name it, but Blender
                # never offers it in the Color Management dropdown and `getViews` does not list
                # it — so the importer's "is this view available?" test says no and silently
                # takes the fallback path while the config is in fact loaded and correct.
                line = line.replace("active_views: [", "active_views: [%s, " % name, 1)
            elif line.strip().startswith("- !<View> {name: ACES 1.3, view_transform: "
                                         "ACES 1.3 Rec.1886, display_colorspace: sRGB}"):
                indent = line[:len(line) - len(line.lstrip())]
                out.append(line)
                line = ("%s- !<View> {name: %s, view_transform: %s Rec.1886, "
                        "display_colorspace: sRGB}" % (indent, name, name))
            elif line.startswith("view_transforms:"):
                out.append(line)
                line = self.ocio_view_transform("%s Rec.1886" % name, lut).rstrip("\n")
            out.append(line)

        path = os.path.join(directory, "config.ocio")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\n")
        return path

    # -- reporting -----------------------------------------------------------------------
    def explain(self, say=print):
        say("  DERIVED FROM grading.json")
        for line in self.sources:
            say("    * " + line)
        for line in self.warnings:
            say("    ! " + line)


def load(bundle_dir=None):
    path = os.path.join(bundle_dir or DEFAULT_BUNDLE, "grading.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return Grading(json.load(handle))


def blender_config_path():
    """Blender's shipped OCIO config, if it can be found."""
    env = os.environ.get("CARTO_BLENDER_COLORMANAGEMENT")
    if env and os.path.isfile(os.path.join(env, "config.ocio")):
        return os.path.join(env, "config.ocio")
    try:
        import bpy
        here = os.path.join(os.path.dirname(bpy.app.binary_path), bpy.app.version_string.split()[0])
    except Exception:
        here = None
    candidates = []
    if here:
        candidates.append(os.path.join(here, "datafiles", "colormanagement", "config.ocio"))
    for root in (r"G:\SteamLibrary\steamapps\common\Blender",
                 r"C:\Program Files\Blender Foundation"):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), reverse=True):
            candidates.append(os.path.join(root, entry, "datafiles", "colormanagement",
                                           "config.ocio"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


# -- CLI --------------------------------------------------------------------------------------

def _argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


def main():
    args = _argv()
    bundle = DEFAULT_BUNDLE
    mode = "--explain"
    values = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--bundle" and i + 1 < len(args):
            bundle = args[i + 1]; i += 2
        elif a in ("--explain", "--predict", "--write-ocio", "--check"):
            mode = a; i += 1
            while i < len(args) and not args[i].startswith("--"):
                values.append(args[i]); i += 1
        else:
            i += 1

    grading = load(bundle)
    if grading is None:
        print(f"no grading.json under {bundle}")
        return 1

    if mode == "--write-ocio":
        target = values[0] if values else os.path.join(bundle, "colour")
        config = blender_config_path()
        if config is None:
            print("could not find Blender's shipped config.ocio; set "
                  "CARTO_BLENDER_COLORMANAGEMENT to the folder holding it")
            return 1
        path = grading.write_ocio(target, config)
        print(f"wrote {path}")
        print("Launch Blender with it, then pick the 'CS2 Graded' view in Color Management:")
        print(f'  $env:OCIO = "{path}"; blender.exe <scene.blend>')
        return 0

    print("=" * 78)
    print("  CS2 COLOUR MODEL")
    print("=" * 78)
    grading.explain()

    if mode == "--predict":
        albedo = tuple(float(v) for v in values[:3]) if len(values) >= 3 else REFERENCES[0][2]
        print()
        print("  PREDICTION")
        _report_sample(grading, "given", albedo)
    elif mode == "--check":
        print()
        print("  CHECK against the in-game references")
        for label, coord, albedo, reference, note in REFERENCES:
            _report_sample(grading, label, albedo)
            if reference is None:
                print(f"      no in-game reading for {label} yet — {note}")
                continue
            target = tuple(srgb_to_linear(int(reference[i:i + 2], 16) / 255.0)
                           for i in (1, 3, 5))
            target_srgb = tuple(int(reference[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
            th, ts, tv = hsv_of(target_srgb)
            print(f"      in-game at ({coord[0]}, {coord[1]}): {reference}  "
                  f"hue {th:.1f} sat {ts:.3f} val {tv:.3f}   [{note}]")
            ev, model, used = grading.match_value(albedo, tv)
            if model is None:
                print("      (no PyOpenColorIO here, so no residual)")
                continue
            mh, ms, mv = hsv_of(model)
            print(f"      model re-exposed to that value ({ev:+.2f} EV, {used}): "
                  f"{hex_of(model)}  hue {mh:.1f} sat {ms:.3f} val {mv:.3f}")
            model_lin = tuple(srgb_to_linear(c) for c in model)
            ratio = tuple(target[i] / max(model_lin[i], 1e-6) for i in range(3))
            norm = tuple(r / ratio[1] for r in ratio)
            print(f"      RESIDUAL, green-normalised (exposure is free): "
                  f"({norm[0]:.3f}, 1.000, {norm[2]:.3f})")
            print(f"      hue {th - mh:+.1f}, saturation {ts - ms:+.3f}")
    print("=" * 78)
    return 0


# The surfaces every colour claim in this repo is measured against, so two ends of a comparison
# always name the same ground. Each in-game figure is the mean, IN LINEAR LIGHT, of a full-frame
# screenshot crop of one surface — not an eyedropper pixel. That is deliberate: it is the same
# quantity the model works in (a texture mean), and it does not depend on which grain a pointer
# landed on. Albedo is the mean of the texture the probe says is actually rendered there.
#
# The pavement is the control that matters: its albedo is linear (0.1019, 0.1018, 0.1010),
# neutral to under 1%, and it still renders warm beige in game. Nothing an albedo does can
# explain that.
REFERENCES = [
    ("sand  extra4", (5732.9, 1798.0), (0.1848, 0.1629, 0.1334), "#ac995e",
     "in-game crop mean, 2026-08-10; probe says extra4 at opacity 1.000"),
    ("grass", (5606.9, 1841.4), (0.0586, 0.0699, 0.0371), "#515417",
     "in-game crop mean, 2026-08-10; probe says grass at opacity 1.000 over dirt"),
    ("pavement (area)", (107.7, -223.0), (0.1019, 0.1018, 0.1010), "#b5a882",
     "in-game crop mean, 2026-08-10; area Pavement_Surface_02, NOT the splat ground the point "
     "probe reports there - the probe prints what is under the area unless you import --areas"),
]


def _report_sample(grading, label, albedo):
    graded = grading.apply(albedo)
    print(f"    {label:<14} albedo {tuple(round(v, 4) for v in albedo)} "
          f"-> graded {tuple(round(v, 4) for v in graded)}")
    shown, used = grading.on_screen(albedo)
    if shown:
        hue, sat, val = hsv_of(shown)
        print(f"      on screen through {used} at {grading.exposure_ev:+.2f} EV: "
              f"{hex_of(shown)}  hue {hue:.1f} sat {sat:.3f} val {val:.3f}")
    else:
        print("      (no PyOpenColorIO here - run under Blender's Python for the screen colour)")


if __name__ == "__main__":
    sys.exit(main())
