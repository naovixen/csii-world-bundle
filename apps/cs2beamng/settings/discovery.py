"""What this machine offers: where the app keeps its files, and where BeamNG is.

Everything here is a guess about a filesystem, so everything here is allowed to find nothing.
`detected()` is what the settings page shows beside the three path boxes — the bundles that
exist, the levels folders that exist, the executable if there is one — and it never raises,
because it runs on every settings request and a missing BeamNG log must not blank the page.
"""

import os
import sys

from csiibundle.bake.locate import steam_roots_linux

APP_DIR_NAME = ".cs2beamng"


def app_dir():
    """Where this app keeps its own files: `CS2BEAMNG_HOME`, else `~/.cs2beamng`. Created."""
    base = os.environ.get("CS2BEAMNG_HOME") or os.path.join(os.path.expanduser("~"), APP_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return base


BEAMNG_APP_ID = "284160"


def candidate_userfolders():
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return [
            os.path.join(local, "BeamNG", "BeamNG.drive", "current"),
            os.path.join(local, "BeamNG.drive", "current"),
            os.path.join(local, "BeamNG.drive"),
        ]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "BeamNG.drive", "current")]
    roots = [
        os.path.join(home, ".local", "share", "BeamNG.drive", "current"),
        os.path.join(home, ".local", "share", "BeamNG", "BeamNG.drive", "current"),
    ]
    for steam in steam_roots_linux():
        roots.append(
            os.path.join(
                steam,
                "steamapps",
                "compatdata",
                BEAMNG_APP_ID,
                "pfx",
                "drive_c",
                "users",
                "steamuser",
                "AppData",
                "Local",
                "BeamNG",
                "BeamNG.drive",
                "current",
            )
        )
    return roots


def default_levels_dir():
    for folder in candidate_userfolders():
        if os.path.isdir(folder):
            return os.path.join(folder, "levels")
    return os.path.join(os.path.expanduser("~"), "cs2beamng", "levels")


def candidate_beamng_exes():
    """Likely BeamNG executables on this machine (Steam libraries), most likely first."""
    out = []
    if sys.platform.startswith("win"):
        libraries = [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam")
        ]
        vdf = os.path.join(libraries[0], "steamapps", "libraryfolders.vdf")
        libraries += _steam_libraries(vdf)
        for lib in libraries:
            out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BeamNG.drive.exe"))
    elif sys.platform.startswith("linux"):
        for steam in steam_roots_linux():
            libraries = [
                steam,
                *_steam_libraries(os.path.join(steam, "steamapps", "libraryfolders.vdf")),
            ]
            for lib in libraries:
                out.append(
                    os.path.join(
                        lib, "steamapps", "common", "BeamNG.drive", "BinLinux", "BeamNG.drive.x64"
                    )
                )
    return out


def _steam_libraries(vdf_path):
    """The extra library folders named in Steam's `libraryfolders.vdf`, or none.

    This has no counterpart in `csiibundle.bake.locate`, which the refactor plan expected it to
    duplicate: that module finds the CS2 *bundle*, which lives in a userfolder at a fixed path
    under a Steam root, so it never needs to know where a game is INSTALLED. Only BeamNG's
    executable can be on a second drive, and only this reads the file that says so. What the two
    genuinely shared was the list of Steam roots, and that is `steam_roots_linux` now.
    """
    libs = []
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith('"path"'):
                    parts = line.split('"')
                    if len(parts) >= 4:
                        libs.append(parts[3].replace("\\\\", "\\"))
    except OSError:
        pass
    return libs


def find_beamng_exe(values=None):
    explicit = (values or {}).get("beamng_exe")
    if explicit and os.path.isfile(explicit):
        return explicit
    for cand in candidate_beamng_exes():
        if os.path.isfile(cand):
            return cand
    return None


def detected(values=None):
    """What this machine offers, for the UI's path pickers: bundles that exist, levels folders that
    exist, the BeamNG executable if any. Never raises."""
    from csiibundle.bake.locate import candidate_bundle_dirs

    bundles = [p for p in candidate_bundle_dirs() if os.path.isfile(os.path.join(p, "bundle.json"))]
    levels = [os.path.join(f, "levels") for f in candidate_userfolders() if os.path.isdir(f)]
    out = {
        "bundles": bundles,
        "levelsDirs": levels,
        "beamngExe": find_beamng_exe(values),
        "platform": sys.platform,
        "appDir": app_dir(),
    }
    # The machine the level is FOR, read the same way the build's own budget reads it, so the
    # UI's "target machine" line and the build can never disagree. Never raises: this runs on
    # every settings request and a missing BeamNG log must not blank the page.
    #
    # It says WHY it has no answer, rather than just having none. The blanket `except` here once
    # swallowed an ImportError — a relative import left one level too shallow when this moved
    # into a package — and the target-machine line simply went blank, which is indistinguishable
    # from "BeamNG has never been run on this machine". A guard that hides its own bugs inside
    # the condition it was written for is not a guard.
    try:
        from ..beamng import machine

        vram, pixels, source = machine.render_target(values)
        out["budget"] = {
            "vramMiB": vram,
            "pixels": pixels,
            "source": source,
            "residentMiB": round(machine.resident_budget_mib(vram, pixels)),
            "lighting": machine.light_manager()[0],
        }
        out["budgetError"] = None
    except Exception as exc:
        out["budget"] = None
        out["budgetError"] = f"{type(exc).__name__}: {exc}"
    return out
