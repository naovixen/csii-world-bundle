#!/usr/bin/env python3
"""The Blender/CLI adapter over `csiibundle.colour` — the locator and the command line.

The derivation itself moved into the package (`tools/csiibundle/colour.py`) so that a consumer
with no Blender can import it; what is left here is the part that is *about* Blender or about
running this as a program:

  * `blender_config_path()` — finding Blender's shipped `config.ocio`, which `write_ocio` needs
    as the base to graft the CS2 view onto. A locator, not colour maths.
  * `REFERENCES` and the `--check` report — the in-game measurements every colour claim in this
    repo is scored against.

Everything the package exports is re-exported here, so `import colour_model` keeps working for
`tools/blender_import_bundle.py` and `tools/probe_point.py`.

    python tools/colour_model.py --explain
    python tools/colour_model.py --predict 0.1848 0.1629 0.1334
    python tools/colour_model.py --write-ocio DIR
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csiibundle import colour as _colour
from csiibundle.colour import *  # noqa: F403 - the derivation, re-exported
from csiibundle.colour import Grading, hex_of, hsv_of, srgb_to_linear  # noqa: F401
from csiibundle.colour import load as _load


def load(bundle_dir=None):
    """`csiibundle.colour.load`, with this machine's bundle directory as the default.

    The default is `locate.default_bundle_dir()`, not a path spelled out here. This module used
    to build its own Windows-only `~/AppData/LocalLow/...`, which is one of four places that did
    — so on Linux under Proton, or with `CS2_BUNDLE_DIR` set, the colour tool looked somewhere
    the rest of the repo did not.
    """
    from csiibundle.bake.locate import default_bundle_dir

    return _load(bundle_dir or default_bundle_dir())


def write_ocio(grading, directory, blender_config, name="CS2 Graded"):
    """Write a Blender OCIO config that adds `grading` as a view.

    A method on `csiibundle.colour.Grading` until refactor Phase 2, which is one Blender-shaped
    method on the class both consumers share: it takes the path to Blender's own shipped
    `config.ocio`, reads it, and writes a config only Blender loads. The grading itself — the
    chain, `ocio_view_transform`, the `.cube` LUT — stays in the package, because OCIO and a cube
    LUT are formats rather than an application.

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
    if grading.smh:
        lut = "cs2_smh.cube"
        grading.write_smh_lut(os.path.join(directory, lut))

    out = _grafted_lines(text, grading, name, lut, _search_path(directory, blender_config))

    path = os.path.join(directory, "config.ocio")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    return path


def _search_path(directory, blender_config):
    """The absolute folders the grafted config resolves FileTransforms against.

    Ours first, so the generated LUT resolves; then Blender's own three, so every view we did
    not touch still finds the files it needs. Absolute, so the 19 MB of LUTs beside Blender's
    own config do not have to be copied.
    """
    base = os.path.dirname(os.path.abspath(blender_config))
    # Our own folder first, so the generated LUT resolves; then Blender's, so every view we
    # did not touch still finds the files it needs.
    search = [os.path.abspath(directory).replace("\\", "/")] + [
        os.path.join(base, part).replace("\\", "/") for part in ("icc", "luts", "filmic")
    ]
    return search


def _grafted_lines(text, grading, name, lut, search):
    """Blender's shipped config, line by line, with our view and view transform spliced in."""
    out = []
    for line in text.splitlines():
        if line.startswith("search_path:"):
            line = "search_path: [{}]".format(", ".join(f'"{p}"' for p in search))
        elif line.startswith("active_views:"):
            # A view that is not in `active_views` still WORKS if you name it, but Blender
            # never offers it in the Color Management dropdown and `getViews` does not list
            # it — so the importer's "is this view available?" test says no and silently
            # takes the fallback path while the config is in fact loaded and correct.
            line = line.replace("active_views: [", f"active_views: [{name}, ", 1)
        elif line.strip().startswith(
            "- !<View> {name: ACES 1.3, view_transform: "
            "ACES 1.3 Rec.1886, display_colorspace: sRGB}"
        ):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(line)
            line = (
                f"{indent}- !<View> {{name: {name}, view_transform: {name} Rec.1886, "
                "display_colorspace: sRGB}"
            )
        elif line.startswith("view_transforms:"):
            out.append(line)
            line = grading.ocio_view_transform(f"{name} Rec.1886", lut).rstrip("\n")
        out.append(line)
    return out


def blender_config_path():
    """Blender's shipped OCIO config, if it can be found.

    Installed as `csiibundle.colour.CONFIG_LOCATOR` at the bottom of this file, which is how
    `Grading.pick_view` finds a config when `$OCIO` is unset without the package having to know
    that Blender exists.
    """
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
    for root in (
        r"G:\SteamLibrary\steamapps\common\Blender",
        r"C:\Program Files\Blender Foundation",
    ):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), reverse=True):
            candidates.append(
                os.path.join(root, entry, "datafiles", "colormanagement", "config.ocio")
            )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


# Give the package a way to find a config without it knowing what Blender is.
_colour.CONFIG_LOCATOR = blender_config_path


# -- CLI --------------------------------------------------------------------------------------


def _argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]


def main():
    from csiibundle.bake.locate import default_bundle_dir

    args = _argv()
    bundle = default_bundle_dir()
    mode = "--explain"
    values = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--bundle" and i + 1 < len(args):
            bundle = args[i + 1]
            i += 2
        elif a in ("--explain", "--predict", "--write-ocio", "--check"):
            mode = a
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                values.append(args[i])
                i += 1
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
            print(
                "could not find Blender's shipped config.ocio; set "
                "CARTO_BLENDER_COLORMANAGEMENT to the folder holding it"
            )
            return 1
        path = write_ocio(grading, target, config)
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
            target = tuple(srgb_to_linear(int(reference[i : i + 2], 16) / 255.0) for i in (1, 3, 5))
            target_srgb = tuple(int(reference[i : i + 2], 16) / 255.0 for i in (1, 3, 5))
            th, ts, tv = hsv_of(target_srgb)
            print(
                f"      in-game at ({coord[0]}, {coord[1]}): {reference}  "
                f"hue {th:.1f} sat {ts:.3f} val {tv:.3f}   [{note}]"
            )
            ev, model, used = grading.match_value(albedo, tv)
            if model is None:
                print("      (no PyOpenColorIO here, so no residual)")
                continue
            mh, ms, mv = hsv_of(model)
            print(
                f"      model re-exposed to that value ({ev:+.2f} EV, {used}): "
                f"{hex_of(model)}  hue {mh:.1f} sat {ms:.3f} val {mv:.3f}"
            )
            model_lin = tuple(srgb_to_linear(c) for c in model)
            ratio = tuple(target[i] / max(model_lin[i], 1e-6) for i in range(3))
            norm = tuple(r / ratio[1] for r in ratio)
            print(
                f"      RESIDUAL, green-normalised (exposure is free): "
                f"({norm[0]:.3f}, 1.000, {norm[2]:.3f})"
            )
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
    (
        "sand  extra4",
        (5732.9, 1798.0),
        (0.1848, 0.1629, 0.1334),
        "#ac995e",
        "in-game crop mean, 2026-08-10; probe says extra4 at opacity 1.000",
    ),
    (
        "grass",
        (5606.9, 1841.4),
        (0.0586, 0.0699, 0.0371),
        "#515417",
        "in-game crop mean, 2026-08-10; probe says grass at opacity 1.000 over dirt",
    ),
    (
        "pavement (area)",
        (107.7, -223.0),
        (0.1019, 0.1018, 0.1010),
        "#b5a882",
        "in-game crop mean, 2026-08-10; area Pavement_Surface_02, NOT the splat ground the point "
        "probe reports there - the probe prints what is under the area unless you import --areas",
    ),
]


def _report_sample(grading, label, albedo):
    graded = grading.apply(albedo)
    print(
        f"    {label:<14} albedo {tuple(round(v, 4) for v in albedo)} "
        f"-> graded {tuple(round(v, 4) for v in graded)}"
    )
    shown, used = grading.on_screen(albedo)
    if shown:
        hue, sat, val = hsv_of(shown)
        print(
            f"      on screen through {used} at {grading.exposure_ev:+.2f} EV: "
            f"{hex_of(shown)}  hue {hue:.1f} sat {sat:.3f} val {val:.3f}"
        )
    else:
        print("      (no PyOpenColorIO here - run under Blender's Python for the screen colour)")


if __name__ == "__main__":
    sys.exit(main())
