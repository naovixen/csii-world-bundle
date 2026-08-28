"""Reading and writing the saved settings file.

One flat dict, persisted at `~/.cs2beamng/settings.json` (or under `CS2BEAMNG_HOME`), edited by
the UI and by `cs2beamng build --set key=value`. Everything the pipeline reads comes from here,
so a headless build and a UI build with the same settings produce the same level.
"""

import json
import os

from csiibundle.bake.locate import default_bundle_dir

from .catalog import SCHEMA
from .discovery import APP_DIR_NAME, app_dir, default_levels_dir, find_beamng_exe

__all__ = ["APP_DIR_NAME", "SETTINGS_FILE", "app_dir", "defaults", "load", "save"]

SETTINGS_FILE = "settings.json"


def defaults():
    out = {key: spec[0] for key, spec in SCHEMA.items()}
    out["bundle"] = default_bundle_dir()
    out["output"] = default_levels_dir()
    # ...and the executable, which was detected but never written down, so the box sat empty
    # beside a line saying it had been found. The three paths the app finds for you now behave
    # the same way: found once, shown, and yours to change.
    out["beamng_exe"] = find_beamng_exe()
    return out


def load():
    """Saved settings merged over the defaults; unknown keys are kept (forward compatible)."""
    out = defaults()
    path = os.path.join(app_dir(), SETTINGS_FILE)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in SCHEMA:
                        out[key] = value
        except (OSError, ValueError):
            pass
    return out


def save(values):
    path = os.path.join(app_dir(), SETTINGS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {k: v for k, v in values.items() if k in SCHEMA}, handle, indent=1, sort_keys=True
        )
    os.replace(tmp, path)
