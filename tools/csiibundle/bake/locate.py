"""Where a bundle usually is — the producer writes one bundle per install into Cities:
Skylines II's ModsData folder, and both the bake CLI and the cs2beamng app default to it.

Per platform (the game itself is Windows-only; Linux runs it under Proton, so the path is the
Windows one inside a Proton prefix; macOS has no CS2 build, but a copied bundle may still live
in the analogous place):

    Windows  %USERPROFILE%\\AppData\\LocalLow\\Colossal Order\\Cities Skylines II\\ModsData\\Carto
    Linux    <steam>/steamapps/compatdata/949230/pfx/drive_c/users/steamuser/AppData/LocalLow/
             Colossal Order/Cities Skylines II/ModsData/Carto
    macOS    ~/Library/Application Support/Colossal Order/Cities Skylines II/ModsData/Carto

`default_bundle_dir()` returns the first candidate that holds a `bundle.json`, else the first
candidate at all (so a message can name where it looked). 949230 is CS2's Steam app id.
"""

import os
import sys

from .. import reader

CS2_APP_ID = "949230"
MODSDATA_TAIL = os.path.join("Colossal Order", "Cities Skylines II", "ModsData", "Carto")


def _steam_roots_linux():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(
            home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"
        ),  # flatpak
        os.path.join(home, ".steam", "root"),
    ]


def candidate_bundle_dirs():
    """Every place a bundle might be on this OS, most likely first."""
    if sys.platform.startswith("win"):
        profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return [os.path.join(profile, "AppData", "LocalLow", MODSDATA_TAIL)]
    if sys.platform == "darwin":
        return [
            os.path.join(os.path.expanduser("~"), "Library", "Application Support", MODSDATA_TAIL)
        ]
    proton_tail = os.path.join(
        "steamapps",
        "compatdata",
        CS2_APP_ID,
        "pfx",
        "drive_c",
        "users",
        "steamuser",
        "AppData",
        "LocalLow",
        MODSDATA_TAIL,
    )
    return [os.path.join(root, proton_tail) for root in _steam_roots_linux()]


def default_bundle_dir():
    """The bundle to use when the caller names none."""
    candidates = candidate_bundle_dirs()
    for path in candidates:
        if os.path.isfile(os.path.join(path, reader.MANIFEST_NAME)):
            return path
    return candidates[0]
