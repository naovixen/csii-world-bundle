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
  * `csiibundle.colourocio.view_transform(grading, ...)` — the whole chain as an OCIO view
    transform, which is how it can run on final radiance in the right order in any OCIO-aware
    application; and `colourocio.write_smh_lut(grading, path)` for the one step a matrix cannot
    express. Writing a config for a PARTICULAR application is that application's job —
    `tools/colour_model.write_ocio` does it for Blender by extending Blender's own shipped
    config. The transfer curves and ported HDRP operators the chain is built from are
    `csiibundle.colourspace`; the illumination it sits on is `csiibundle.illumination`.
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

from .colourocio import TONEMAP_TO_VIEW
from .colourspace import (
    ACESCC_MIDGRAY,
    ACESCC_OFFSET,
    ACESCC_SCALE,
    AP1_LUMA,
    AP1_TO_SRGB,
    SRGB_TO_AP1,
    mat_mul_vec,
    prepare_smh,
    saturation_matrix,
    smh_offset,
    white_balance_matrix,
)
from .illumination import Lighting


class Grading:
    """`grading.json`, turned into constants a renderer can apply.

    Attributes are the derived form; `sources` records where each came from, and `warnings` what
    was left out. `explain()` prints both.
    """

    def __init__(self, document):
        self.raw = document or {}
        adjust = self.raw.get("colorAdjustments") or {}
        self.smh = self.raw.get("shadowsMidtonesHighlights") or {}
        # The illumination the whole chain above sits ON TOP OF. Parsed rather than left in
        # `raw` because a consumer that reproduces the albedo and the grading but lights the
        # scene white still misses the game's colour — the bundle's own semantics string says
        # exactly that, and the sun here is 110,000 lux at 66 degrees, not a default tilt.
        self.lighting = Lighting(self.raw.get("lighting"))
        self.sources = []
        self.warnings = []
        self._parse_exposure(self.raw.get("exposure") or {}, adjust)
        self._parse_gain(adjust, self.raw.get("whiteBalance") or {})
        self._parse_contrast(adjust)
        self._parse_saturation(adjust)
        self._parse_tonemap(self.raw.get("tonemapping") or {})
        self._parse_hue(adjust)
        self._parse_smh()
        self._warn_unreplayable()

    def _parse_exposure(self, exposure, adjust):
        """Exposure. HDRP's two EV contributions add; both are stops, and stops add."""
        self.compensation = float(exposure.get("compensation", 0.0) or 0.0)
        self.post_exposure = float(adjust.get("postExposure", 0.0) or 0.0)
        self.exposure_mode = str(exposure.get("mode", "") or "")
        self.sources.append(
            f"exposure EV {self.compensation:+.4g} (Exposure.compensation) "
            f"{self.post_exposure:+.4g} (ColorAdjustments.postExposure) = "
            f"{self.exposure_ev:+.4g} stops"
        )

    def _parse_gain(self, adjust, balance):
        """Colour filter and white balance are both linear per-channel/linear-matrix operations,
        so they fold into one matrix applied before the contrast power."""
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
            f"{tuple(round(v, 4) for v in mat_mul_vec(self.gain_matrix, (1.0, 1.0, 1.0)))}"
        )

    def _parse_contrast(self, adjust):
        """Contrast. HDRP pivots about ACEScc mid grey in log space:
            v  = (log2(x) + 9.72) / 17.52                 (the ACEScc encoding)
            v' = (v - M) * C + M                          (the contrast operator, M = mid grey)
        Substituting and solving for x' gives a per-channel POWER LAW and a scalar:
            x' = x^C * 2^(K * (C - 1)),  K = 9.72 - 17.52 * M = 2.4739
        which is why contrast needs no curve fitting and no LUT: `contrast_exponent` goes in an
        OCIO ExponentTransform (or Blender's Color Management gamma, which is x^(1/g)), and
        `contrast_scale` is one more linear multiply."""
        self.contrast = float(adjust.get("contrast", 0.0) or 0.0)
        self.contrast_exponent = 1.0 + self.contrast / 100.0
        k = ACESCC_OFFSET - ACESCC_SCALE * ACESCC_MIDGRAY
        self.contrast_scale = 2.0 ** (k * (self.contrast_exponent - 1.0))
        self.sources.append(
            f"contrast {self.contrast:+g} -> x^{self.contrast_exponent:g} x "
            f"{self.contrast_scale:.5g}, from the ACEScc pivot about mid grey "
            f"{ACESCC_MIDGRAY} (K = 9.72 - 17.52*M = {k:.4f}); exact, not fitted"
        )

    def _parse_saturation(self, adjust):
        """Saturation, about AP1 luminance because the whole chain runs in ACEScg."""
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

    def _parse_tonemap(self, tonemap):
        """Tonemapping -> the view transform that stands in for it."""
        self.tonemap_mode = str(tonemap.get("mode", "") or "")
        self.view_transform = TONEMAP_TO_VIEW.get(self.tonemap_mode.lower(), "Standard")
        self.sources.append(
            f"tonemapping {self.tonemap_mode or '(absent)'} -> Blender view transform "
            f"{self.view_transform or '(none - see warnings)'}"
        )

    def _parse_hue(self, adjust):
        hue = float(adjust.get("hueShift", 0.0) or 0.0)
        self.hue_shift = hue
        if hue:
            self.warnings.append(
                f"hueShift {hue:+g} deg is NOT replayed: it is an HSV rotation, not a matrix, so "
                f"it cannot ride the same linear chain. Nothing else in this bundle needs it "
                f"(the value is normally 0)"
            )

    def _parse_smh(self):
        if not self.smh:
            return
        self.smh_prepared = {
            k: prepare_smh(self.smh[k])
            for k in ("shadows", "midtones", "highlights")
            if k in self.smh
        }
        offsets = {k: round(smh_offset(self.smh[k]), 4) for k in self.smh_prepared}
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

    def _warn_unreplayable(self):
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
        neutral_gain = mat_mul_vec(self.gain_matrix, (1.0, 1.0, 1.0))
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
        the view, not to the pixel. Feed the result to `colourocio.display()` for the on-screen
        colour.
        """
        ev = self.exposure_ev if exposure_ev is None else exposure_ev
        c = tuple(v * (2.0**ev) for v in linear_rgb)
        c = mat_mul_vec(SRGB_TO_AP1, c)  # into ACEScg, where HDRP grades
        c = mat_mul_vec(self.gain_matrix, c)
        c = tuple(max(v, 0.0) ** self.contrast_exponent * self.contrast_scale for v in c)
        c = mat_mul_vec(self.saturation_matrix, c)
        c = self.apply_smh(tuple(max(v, 0.0) for v in c))
        c = mat_mul_vec(AP1_TO_SRGB, c)  # and back out to the scene's space
        return tuple(max(v, 0.0) for v in c)

    # -- reporting -----------------------------------------------------------------------
    def explain(self, say=print):
        say("  DERIVED FROM grading.json")
        for line in self.sources:
            say("    * " + line)
        for line in self.warnings:
            say("    ! " + line)


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
