"""The grading as OCIO — view picking, on-screen evaluation, and the generated view transform.

`csiibundle.colour.Grading` is the derivation; this module is how it runs on final radiance in
the right order in any OCIO-aware application. Writing a config for a PARTICULAR application is
that application's job — `tools/colour_model.write_ocio` does it for Blender by extending
Blender's own shipped config.
"""

import math
import os

from .colourspace import AP1_TO_SRGB, SRGB_TO_AP1, hsv_of

# How `pick_view` finds an OCIO config when `$OCIO` is unset: a callable returning a path, or
# None. Left unset here on purpose — WHERE a config lives is a property of the host application,
# not of the grading, and this module has no business knowing about any particular one. The
# Blender adapter (`tools/colour_model.py`) installs its own locator.
CONFIG_LOCATOR = None

# The view transform that matches a given HDRP tonemapping mode, in Blender's shipped config.
# Blender 5.2 ships ACES 1.3 and ACES 2.0 as view transforms; an earlier note in this repo that
# "Blender has no ACES view transform" was wrong and cost a session.
TONEMAP_TO_VIEW = {
    "aces": "ACES 1.3",
    "neutral": "Khronos PBR Neutral",
    "none": "Standard",
    "external": None,  # a 3D LUT the game blends per time of day; see `Grading.warnings`
}

# The shaper range for the SMH LUT. A 3D LUT is indexed on [0, 1], but scene-linear values
# are unbounded, so the data is put through a log2 allocation first and the LUT is built in
# that same space. These are the bounds Blender's own AgX config uses for the same job
# (2^-12.47 to 2^12.53, i.e. 25 stops), which is more range than any terrain needs and keeps
# this consistent with the config it is being merged into.
SHAPER_MIN = -12.47393
SHAPER_MAX = 12.5260688117


def pick_view(grading, view=None):
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
            for v in (view, grading.view_name, grading.view_transform, "Standard")
            if v and v in views
        ),
        None,
    )
    return (config, display, chosen) if chosen else (None, None, None)


def display(grading, scene_linear, view=None):
    """Scene-linear -> on-screen sRGB through a view transform. Returns `(rgb, view_used)`.

    Needs PyOpenColorIO, which Blender's bundled Python has and a system Python normally does
    not; returns `(None, None)` elsewhere, so callers fall back to rendering a pixel rather
    than reporting an untonemapped value as if it were the screen colour.

    The config is loaded BY PATH — `$OCIO` if the generated one is active, otherwise
    Blender's shipped one. `Config.CreateFromEnv()` alone would quietly return OCIO's own
    built-in config when `$OCIO` is unset, whose views are not Blender's, and the numbers
    would look plausible and mean nothing.
    """
    config, display_name, chosen = pick_view(grading, view)
    if chosen is None:
        return None, None
    import PyOpenColorIO as ocio

    cpu = config.getProcessor(
        ocio.ROLE_SCENE_LINEAR, display_name, chosen, ocio.TRANSFORM_DIR_FORWARD
    ).getDefaultCPUProcessor()
    return tuple(cpu.applyRGB(list(scene_linear))), chosen


def on_screen(grading, albedo_linear, exposure_ev=None, view=None):
    """Albedo -> the colour on screen, grading it exactly ONCE. Returns `(rgb, view_used)`.

    Which half does the work depends on which view is available, and getting that wrong is a
    silent doubling: with the generated config active the VIEW carries the whole chain, so
    the albedo must be handed over raw (exposed only). Under a stock view the view carries
    only the tonemap, so the chain has to be applied here first.
    """
    ev = grading.exposure_ev if exposure_ev is None else exposure_ev
    _config, _display, used = pick_view(grading, view)
    if used is None:
        return None, None
    if used == grading.view_name:
        scene_linear = tuple(v * (2.0**ev) for v in albedo_linear)
    else:
        scene_linear = grading.apply(albedo_linear, ev)
    return display(grading, scene_linear, used)[0], used


def match_value(grading, albedo_linear, target_value, view=None):
    """The exposure at which this albedo reaches `target_value` on screen, and the colour.

    Exposure is the one term the bundle genuinely cannot supply — the game meters every
    frame — so a comparison that does not free it is comparing brightness, not colour. Match
    on VALUE, then compare hue and saturation, which are the parts the bundle does determine.
    """
    low, high = -8.0, 12.0
    for _ in range(40):
        mid = (low + high) / 2.0
        rgb, used = on_screen(grading, albedo_linear, mid, view)
        if rgb is None:
            return None, None, None
        if hsv_of(rgb)[2] < target_value:
            low = mid
        else:
            high = mid
    ev = (low + high) / 2.0
    rgb, used = on_screen(grading, albedo_linear, ev, view)
    return ev, rgb, used


def write_smh_lut(grading, path, size=33):
    """Write the shadows/midtones/highlights step as a 3D LUT in the shaper's log space.

    Everything else in the chain is a matrix or a power, which OCIO expresses natively and
    auditably. This one step is luminance-weighted, so it cannot be either — and rather than
    approximate it with something that IS a matrix (which is how a grade quietly becomes a
    different grade), it is sampled exactly onto a grid.
    """
    span = SHAPER_MAX - SHAPER_MIN
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
        value = max(value, 2.0**SHAPER_MIN)
        return min(1.0, max(0.0, (math.log(value, 2.0) - SHAPER_MIN) / span))

    # .cube order: red varies fastest, then green, then blue.
    axis = [2.0 ** (SHAPER_MIN + span * (i / (size - 1.0))) for i in range(size)]
    for b in axis:
        for g in axis:
            for r in axis:
                lines.append(
                    "{:.7g} {:.7g} {:.7g}".format(
                        *tuple(encode(v) for v in grading.apply_smh((r, g, b)))
                    )
                )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _matrix44(m, scale=1.0):
    """One 3x3 (optionally scaled) as the 16-float row-major list OCIO's MatrixTransform takes."""
    rows = []
    for r in range(3):
        rows.append(", ".join("%.10g" % (m[r][c] * scale) for c in range(3)) + ", 0")
    rows.append("0, 0, 0, 1")
    return "[" + ", ".join(rows) + "]"


def _chain_children(grading, smh_lut):
    """The YAML lines of the view transform's GroupTransform, one chain step at a time."""
    identity = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    children = [
        "        # into the scene's own linear space, then into ACEScg where HDRP grades.",
        "        # The AP1 matrix is written out rather than using the config's ACEScg space",
        "        # so this chain and tools/colour_model.py are the same arithmetic by",
        "        # construction - they are checked against each other to 0.1/255.",
        "        - !<ColorSpaceTransform> {src: ACES2065-1, dst: Linear Rec.709}",
        f"        - !<MatrixTransform> {{matrix: {_matrix44(SRGB_TO_AP1)}}}",
        "        # colour filter x white balance (HDRP ColorBalanceToLMSCoeffs, ported)",
        f"        - !<MatrixTransform> {{matrix: {_matrix44(grading.gain_matrix)}}}",
    ]
    if grading.contrast_exponent != 1.0:
        children += [
            f"        # contrast {grading.contrast:+g}, as the ACEScc log pivot solved into a power and a scalar",
            # ExponentTransform clamps negatives by default, which is HDRP's own max(0, ...).
            "        - !<ExponentTransform> {value: [%.10g, %.10g, %.10g, 1]}"
            % ((grading.contrast_exponent,) * 3),
            f"        - !<MatrixTransform> {{matrix: {_matrix44(identity, grading.contrast_scale)}}}",
        ]
    if grading.saturation != 1.0:
        children += [
            f"        # saturation x{grading.saturation:g} about Rec.709 luminance",
            f"        - !<MatrixTransform> {{matrix: {_matrix44(grading.saturation_matrix)}}}",
        ]
    if smh_lut:
        children += [
            "        # shadows/midtones/highlights - luminance-weighted, so neither a matrix",
            "        # nor a power. Sampled onto a grid in a log2 shaper (log in, log out, so",
            "        # a per-channel gain is an offset and interpolates cleanly), then undone.",
            f"        - !<AllocationTransform> {{allocation: lg2, vars: [{SHAPER_MIN:.8g}, {SHAPER_MAX:.8g}]}}",
            f"        - !<FileTransform> {{src: {smh_lut}, interpolation: tetrahedral}}",
            f"        - !<AllocationTransform> {{allocation: lg2, vars: [{SHAPER_MIN:.8g}, {SHAPER_MAX:.8g}], "
            "direction: inverse}",
        ]
    children += [
        "        # out of ACEScg, back to the scene's linear space for the view's tonemap",
        f"        - !<MatrixTransform> {{matrix: {_matrix44(AP1_TO_SRGB)}}}",
        "        - !<ColorSpaceTransform> {src: Linear Rec.709, dst: ACES2065-1}",
        "        # the tonemap the grading above was authored against",
        "        - !<BuiltinTransform> {style: ACES-OUTPUT - "
        "ACES2065-1_to_CIE-XYZ-D65 - SDR-VIDEO_1.0}",
    ]
    return children


def view_transform(grading, name, smh_lut=None):
    """This grading as an OCIO ViewTransform, as YAML.

    A view transform is the only place in Blender where a CROSS-CHANNEL operator can run on
    final radiance and still show in the viewport: the compositor is render-only, and Color
    Management's own controls (exposure, gamma, curves) are all per-channel. Saturation is a
    3x3, so it has to be here.
    """
    return (
        "  - !<ViewTransform>\n"
        "    name: {}\n"
        "    family: CS2\n"
        "    description: |\n"
        "      Cities: Skylines II colour grading, derived from this bundle's "
        "grading.json\n"
        "    from_scene_reference: !<GroupTransform>\n"
        "      children:\n{}\n".format(name, "\n".join(_chain_children(grading, smh_lut)))
    )
