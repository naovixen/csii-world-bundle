#!/usr/bin/env python3
"""The Blender/CLI adapter over `csiibundle.colour` — the locator and the command line.

The derivation itself moved into the package (`tools/csiibundle/colour.py`) so that a consumer
with no Blender can import it; what is left here is the part that is *about* Blender or about
running this as a program:

  * `blender_config_path()` — finding Blender's shipped `config.ocio`, which `write_ocio` needs
    as the base to graft the CS2 view onto. A locator, not colour maths.
  * `DEFAULT_BUNDLE` — the producer's own output directory on this machine.
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
from csiibundle.colour import *            # noqa: F401,F403 - the derivation, re-exported
from csiibundle.colour import Grading, hex_of, hsv_of, srgb_to_linear   # noqa: F401
from csiibundle.colour import load as _load
from csiibundle import colour as _colour

# The producer writes one bundle per install, here. Not a bundle-format fact — just where this
# machine's rips land, so the CLI has a default.
DEFAULT_BUNDLE = os.path.join(
    os.path.expanduser("~"), "AppData", "LocalLow", "Colossal Order",
    "Cities Skylines II", "ModsData", "Carto")


def load(bundle_dir=None):
    """`csiibundle.colour.load`, with this machine's bundle directory as the default."""
    return _load(bundle_dir or DEFAULT_BUNDLE)


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


# Give the package a way to find a config without it knowing what Blender is.
_colour.CONFIG_LOCATOR = blender_config_path


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
