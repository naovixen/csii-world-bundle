"""Replay the game's own colour grading — the `--colour` flag.

CS2 authors ground albedo near-grey (rock sRGB saturation 0.111, sand 0.143); the on-screen
palette is HDRP post-processing on top. The producer ships the game's **resolved** volume-stack
values, so this replays real numbers rather than approximating a screenshot. The derivation is
`tools/colour_model.py`; what is here is WHERE in Blender each half of the chain has to run,
which `apply_grading` sets out and which matters more than the numbers do.
"""

import os

import bpy
import colour_model

from .log import LOG


def _view_transform_available(name):
    """Is `name` one of this Blender's view transforms?

    The enum is OCIO-driven, so it cannot be read off `bl_rna` (that reports a placeholder); the
    only reliable test is to try the assignment and put back what was there.
    """
    view_settings = bpy.context.scene.view_settings
    previous = view_settings.view_transform
    try:
        view_settings.view_transform = name
        return True
    except (TypeError, AttributeError):
        return False
    finally:
        try:
            view_settings.view_transform = previous
        except (TypeError, AttributeError):
            pass


def apply_grading(bundle_dir):
    """Replay the game's colour grading from `grading.json` (the `--colour` flag).

    CS2 authors ground albedo near-grey (rock sRGB saturation 0.111, sand 0.143); the on-screen
    palette is HDRP post-processing on top. The producer ships the game's **resolved** volume-stack
    values, so this replays real numbers rather than approximating a screenshot. OFF by default:
    a plain import stays a faithful 1:1 albedo view.

    WHERE THE GRADING RUNS, AND WHY IT MATTERS MORE THAN THE NUMBERS

    A colour grade is a transform on *final radiance*, applied in a fixed order. There are three
    places to put it in Blender and only one of them is both correct and visible:

      * the COMPOSITOR runs on rendered output only. A scene graded there looks raw in the
        viewport, which is where an imported city is actually looked at. This is where the
        previous version put contrast, and it is why measurements of "what the importer produces"
        kept disagreeing with what was on screen.
      * the MATERIALS are visible, but they carry albedo, not radiance, and they come before the
        lighting rather than after it. Saturation happens to survive the move (it is homogeneous,
        so it commutes with a scalar light), but it then runs BEFORE the contrast instead of
        after, and measured on this city's grass that reordering costs 18% on the blue channel.
      * the VIEW TRANSFORM runs on final radiance, in order, and shows in the viewport and in F12
        alike. Everything cross-channel therefore belongs there — which needs an OCIO config, and
        `tools/colour_model.py` writes one from this bundle's own numbers.

    So: with the generated config active, this sets the view and the exposure and touches nothing
    else. Without it, the per-channel part of the chain still goes into Blender's native Color
    Management (exact, viewport-visible) and the cross-channel part is reported as MISSING rather
    than being moved somewhere it would silently change order.

    Exposure is left as the user's control on purpose. The game meters every frame
    (`AutomaticHistogram`), so absolute brightness is a property of where the camera was pointed,
    not of the city; the value set here is only the manual offset the bundle does know about.
    """
    grading = colour_model.load(bundle_dir)
    if grading is None:
        LOG.warn(
            "--colour: this bundle has no grading.json (ripped before the grading layer); "
            "leaving the scene ungraded"
        )
        return

    available = _view_transform_available(grading.view_name)
    view, exposure, gamma, gain, missing = grading.colour_management(available)
    _set_colour_management(view, exposure, gamma)
    _apply_light_gain(gain)
    _report(grading, view, exposure, gamma, gain, available, missing)
    if available:
        return
    for line in missing:
        LOG.warn("--colour: NOT applied - " + line)
    _write_ocio_config(grading, bundle_dir)


def _set_colour_management(view, exposure, gamma):
    """Blender's own Color Management, carrying the per-channel half of the chain."""
    scene = bpy.context.scene
    scene.view_settings.exposure = exposure
    if hasattr(scene.view_settings, "gamma"):
        scene.view_settings.gamma = gamma
    if hasattr(scene.view_settings, "look"):
        scene.view_settings.look = "None"
    if hasattr(scene.view_settings, "use_curve_mapping"):
        scene.view_settings.use_curve_mapping = False
    if hasattr(scene.view_settings, "use_white_balance"):
        # Blender's own white balance is a Kelvin control on a different scale from HDRP's
        # -100..100; mapping between them would be a fit. The game's value is applied exactly
        # instead, either in the view transform or as a light gain.
        scene.view_settings.use_white_balance = False
    try:
        scene.view_settings.view_transform = view
    except (AttributeError, TypeError):
        LOG.warn(
            f"--colour: this Blender has no '{view}' view transform; leaving "
            f"{scene.view_settings.view_transform}"
        )


def _apply_light_gain(gain):
    """The per-channel gain rides the lights when it cannot ride the view. Scaling every light
    per channel scales the whole render per channel, bounces included, so this is exact rather
    than an approximation — but it is only reached on the fallback path."""
    if not any(abs(c - 1.0) > 1e-6 for c in gain):
        return
    scene = bpy.context.scene
    for ob in scene.objects:
        if ob.type == "LIGHT":
            ob.data.color = (gain[0], gain[1], gain[2])
    background = (
        scene.world.node_tree.nodes.get("Background")
        if scene.world and scene.world.node_tree
        else None
    )
    if background is not None:
        base = background.inputs["Color"].default_value
        background.inputs["Color"].default_value = (
            base[0] * gain[0],
            base[1] * gain[1],
            base[2] * gain[2],
            1.0,
        )


def _report(grading, view, exposure, gamma, gain, available, missing):
    """What was applied, and — when the view is missing — what could not be."""
    LOG.step(
        f"grading applied from grading.json: view transform {view}, "
        f"exposure {exposure:+.2f} EV"
        + (f", gamma {gamma:.4g}" if gamma != 1.0 else "")
        + (
            f", light gain {tuple(round(c, 4) for c in gain)}"
            if any(abs(c - 1.0) > 1e-6 for c in gain)
            else ""
        )
    )
    for line in grading.warnings:
        LOG.warn("--colour: " + line)
    if available:
        LOG.step(
            f"the '{grading.view_name}' view is carrying the whole chain — gain, contrast, "
            f"saturation and shadows/midtones/highlights all run on final radiance, in the "
            f"viewport"
        )


def _write_ocio_config(grading, bundle_dir):
    """Write the generated view beside the bundle, and say — LOUDLY — how to switch it on.

    Loud because this is the one step a person can silently skip and then judge the whole
    feature by the result. Without the config Blender has no way to run a cross-channel
    operator before the display transform, so the scene loses the saturation entirely and
    reads flat and dark — which looks exactly like "the grading does not work".
    """
    config = colour_model.blender_config_path()
    if config is None:
        LOG.warn(
            "--colour: could not find Blender's own config.ocio to extend; set "
            "CARTO_BLENDER_COLORMANAGEMENT to the folder holding it"
        )
        return
    try:
        path = colour_model.write_ocio(grading, os.path.join(bundle_dir, "colour"), config)
    except OSError as exc:
        LOG.warn(f"--colour: could not write the OCIO config ({exc})")
        return

    LOG.warn("=" * 78)
    LOG.warn("--colour is only HALF APPLIED. Blender loads its colour config at startup, so")
    LOG.warn(f"the '{grading.view_name}' view cannot be switched on from inside this script.")
    LOG.warn("")
    LOG.warn("  1. close Blender")
    LOG.warn(f'  2. setx OCIO "{path}"')
    LOG.warn("  3. reopen Blender, re-import, and set Render Properties > Color Management >")
    LOG.warn(f"     View Transform to '{grading.view_name}'")
    LOG.warn("")
    LOG.warn("`setx` sets it permanently and is safe: this config is Blender's own with one view")
    LOG.warn("added, so Standard, AgX, Filmic and the rest keep working exactly as before.")
    LOG.warn("=" * 78)
