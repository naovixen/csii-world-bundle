"""Settings — every knob the app exposes, with defaults, validation, presets and per-OS paths.

One flat dict, persisted at `~/.cs2beamng/settings.json`, edited by the UI and by
`cs2beamng build --set key=value`. Everything the pipeline reads comes from here, so a headless
build and a UI build with the same settings produce the same level.

Defaults lean towards **fidelity with performance** (the "High" preset): every layer on,
foliage at 100 %, textures capped at 1024, generated LODs on, collision on for everything
physical, paint collision off. Presets are whole-dict overrides applied on top of the defaults;
a user's explicit changes after picking a preset stick.
"""

import json
import os
import sys

from csiibundle.bake.locate import default_bundle_dir

APP_DIR_NAME = ".cs2beamng"
SETTINGS_FILE = "settings.json"

# key -> (default, kind, help). kind: bool | int | float | str | path | choice:a|b|c
SCHEMA = {
    # where
    "bundle": (None, "path", "Bundle directory (default: the CS2 ModsData export for this OS)"),
    "output": (None, "path", "Levels directory to write into (default: the BeamNG userfolder levels/)"),
    "level_name": ("cs2_city", "str", "Level folder name (lowercase, underscores)"),
    "level_title": ("CS2 City", "str", "Level title shown in BeamNG"),
    "region": (None, "str", "Region as 'X Z R' in bundle metres, or empty for the whole city"),
    # what
    "trees": (True, "bool", "Trees (forest)"),
    "props": (True, "bool", "Props, fences, points of interest"),
    "buildings": (True, "bool", "Buildings"),
    "networks": (True, "bool", "Roads, rails and other network meshes"),
    "markings": (True, "bool", "Road markings and painted decals (pre-draped)"),
    "lane_furniture": (True, "bool", "Roadside furniture: fences, hedges, cables"),
    "lane_furniture_categories": ("fence,cable,lane", "str", "Furniture categories to include"),
    "areas": (True, "bool", "Painted surfaces (parking lots, paths)"),
    "skirts": (True, "bool", "Building foundation skirts"),
    "lights": (False, "bool", "Street and building lights (dynamic lights are expensive)"),
    "lights_max": (500, "int", "Maximum dynamic lights"),
    "water": (True, "bool", "Water planes/blocks"),
    "groundcover": (True, "bool", "Billboard grass from the ground paint"),
    "ai_roads": (False, "bool", "Hidden AI roads from the lane graph (experimental)"),
    # how much
    "foliage_density": (100, "int", "Percent of trees kept (deterministic subset)"),
    "prop_density": (100, "int", "Percent of props kept"),
    "texture_max": (1024, "choice:256|512|1024|2048|0", "Texture edge cap in pixels (0 = full size)"),
    "generate_lods": (True, "bool", "Generate LOD chains for every shape"),
    "imposters": (True, "bool", "Engine-generated billboard imposters (bb_autobillboard)"),
    "cull_px": (10, "int", "Pixel size below which small props are culled (nulldetail)"),
    "tile_size": (256, "int", "World tile size for roads/markings/areas (metres)"),
    "view_distance": (4000, "int", "LevelInfo.visibleDistance: the global far cull in metres"),
    "terrain_upsample": (1, "choice:1|2", "Terrain upsample factor (2 = 8192 @ 1.75 m)"),
    "terrain_holes": ("contain", "choice:contain|majority|none", "Which clip cells become terrain holes"),
    "forest_shadows": (True, "bool", "Forest items cast shadows"),
    "tree_wind": (False, "bool", "Synthesise wind vertex colours for trees"),
    # collision
    "collision_preset": ("simplified", "choice:exact|simplified|off", "Collision detail"),
    "collision_buildings": (True, "bool", "Buildings collide"),
    "collision_props": (True, "bool", "Props collide"),
    "collision_trees": (True, "bool", "Trees collide (trunk)"),
    "collision_furniture": (True, "bool", "Fences and lane furniture collide"),
    "collision_skirts": (True, "bool", "Foundation skirts collide"),
    "collision_paint": (False, "bool", "Markings and painted surfaces collide (not recommended)"),
    # process
    "jobs": (0, "int", "Worker processes for parallel stages (0 = CPU count - 1)"),
    "keep_partial": (False, "bool", "Keep a partially built level after cancel"),
    "cook": (False, "bool", "Run BeamNG's headless mesh compiler / cache warmer after the build"),
    "beamng_exe": (None, "path", "BeamNG executable (auto-detected when empty)"),
    "quality_preset": ("high", "choice:ultra|high|medium|low|custom", "Quality preset last applied"),
    "ui_browser": ("window", "choice:window|chrome|edge|firefox|default", "Where the UI opens: a native window, a named browser, or the system default"),
}

# How the UI groups the knobs; every SCHEMA key appears exactly once (checked by test_app_shell).
GROUPS = [
    ("Where", ["bundle", "output", "level_name", "level_title", "region"]),
    ("What", ["buildings", "networks", "trees", "props", "markings", "areas", "skirts", "lane_furniture",
              "lane_furniture_categories", "water", "groundcover", "lights", "lights_max", "ai_roads"]),
    ("How much", ["foliage_density", "prop_density", "texture_max", "generate_lods", "imposters", "cull_px",
                  "view_distance", "tile_size", "terrain_upsample", "terrain_holes", "forest_shadows", "tree_wind"]),
    ("Collision", ["collision_preset", "collision_buildings", "collision_props", "collision_trees",
                   "collision_furniture", "collision_skirts", "collision_paint"]),
    ("Process", ["jobs", "keep_partial", "cook", "beamng_exe", "ui_browser", "quality_preset"]),
]

PRESETS = {
    "ultra": {"foliage_density": 100, "prop_density": 100, "texture_max": 0, "generate_lods": True,
              "lane_furniture": True, "lights": True, "groundcover": True, "forest_shadows": True,
              "collision_preset": "exact", "cull_px": 6, "view_distance": 8000},
    "high": {"foliage_density": 100, "prop_density": 100, "texture_max": 1024, "generate_lods": True,
             "lane_furniture": True, "lights": False, "groundcover": True, "forest_shadows": True,
             "collision_preset": "simplified", "cull_px": 10, "view_distance": 4000},
    "medium": {"foliage_density": 60, "prop_density": 70, "texture_max": 512, "generate_lods": True,
               "lane_furniture": True, "lane_furniture_categories": "fence,cable", "lights": False,
               "groundcover": False, "forest_shadows": False, "collision_preset": "simplified", "cull_px": 16,
               "view_distance": 3000},
    "low": {"foliage_density": 30, "prop_density": 40, "texture_max": 256, "generate_lods": True,
            "lane_furniture": False, "lights": False, "groundcover": False, "forest_shadows": False,
            "collision_preset": "simplified", "cull_px": 24, "markings": True, "areas": False, "skirts": False,
            "view_distance": 2000},
}


def app_dir():
    base = os.environ.get("CS2BEAMNG_HOME") or os.path.join(os.path.expanduser("~"), APP_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def defaults():
    out = {key: spec[0] for key, spec in SCHEMA.items()}
    out["bundle"] = default_bundle_dir()
    out["output"] = default_levels_dir()
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
        json.dump({k: v for k, v in values.items() if k in SCHEMA}, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def apply_preset(values, name):
    if name not in PRESETS:
        raise KeyError(name)
    out = dict(values)
    out.update(PRESETS[name])
    out["quality_preset"] = name
    return out


def coerce(key, raw):
    """A string from the CLI/UI into the setting's type. Raises ValueError with a readable reason."""
    if key not in SCHEMA:
        raise ValueError(f"unknown setting {key!r}")
    default, kind, _help = SCHEMA[key]
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
    else:
        text = raw
    if kind == "bool":
        if isinstance(text, bool):
            return text
        low = str(text).lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key}: expected true/false, got {raw!r}")
    if kind == "int":
        try:
            return int(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: expected a whole number, got {raw!r}") from None
    if kind == "float":
        try:
            return float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: expected a number, got {raw!r}") from None
    if kind.startswith("choice:"):
        choices = kind.split(":", 1)[1].split("|")
        s = str(text)
        if s not in choices:
            raise ValueError(f"{key}: expected one of {choices}, got {raw!r}")
        return int(s) if all(c.lstrip("-").isdigit() for c in choices) else s
    if kind in ("str", "path"):
        return None if text in ("", None) else str(text)
    return text


def validate(values):
    """Problems as a list of strings (empty = ok). Does not touch the filesystem beyond exists()."""
    problems = []
    if not values.get("bundle") or not os.path.isfile(os.path.join(values["bundle"], "bundle.json")):
        problems.append(f"bundle: no bundle.json at {values.get('bundle')!r}")
    if not values.get("output"):
        problems.append("output: no output directory")
    name = values.get("level_name") or ""
    if not name or any(c for c in name if not (c.isalnum() or c in "_-")):
        problems.append("level_name: letters, digits, '_' and '-' only")
    for key in ("foliage_density", "prop_density"):
        v = values.get(key)
        if not isinstance(v, int) or not 0 <= v <= 100:
            problems.append(f"{key}: 0..100")
    region = values.get("region")
    if region:
        try:
            parts = [float(p) for p in str(region).replace(",", " ").split()]
            if len(parts) != 3 or parts[2] <= 0:
                raise ValueError
        except ValueError:
            problems.append("region: 'X Z R' with R > 0, or empty")
    return problems


def region_of(values):
    region = values.get("region")
    if not region:
        return None
    x, z, r = [float(p) for p in str(region).replace(",", " ").split()]
    return {"centerX": x, "centerZ": z, "radius": r}


# -- per-OS defaults -----------------------------------------------------------------------------

BEAMNG_APP_ID = "284160"


def candidate_userfolders():
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return [os.path.join(local, "BeamNG", "BeamNG.drive", "current"),
                os.path.join(local, "BeamNG.drive", "current"),
                os.path.join(local, "BeamNG.drive")]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "BeamNG.drive", "current")]
    roots = [os.path.join(home, ".local", "share", "BeamNG.drive", "current"),
             os.path.join(home, ".local", "share", "BeamNG", "BeamNG.drive", "current")]
    for steam in (os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam"),
                  os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam")):
        roots.append(os.path.join(steam, "steamapps", "compatdata", BEAMNG_APP_ID, "pfx", "drive_c", "users",
                                  "steamuser", "AppData", "Local", "BeamNG", "BeamNG.drive", "current"))
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
        libraries = [os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam")]
        vdf = os.path.join(libraries[0], "steamapps", "libraryfolders.vdf")
        libraries += _steam_libraries(vdf)
        for lib in libraries:
            out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BeamNG.drive.exe"))
    elif sys.platform.startswith("linux"):
        home = os.path.expanduser("~")
        for steam in (os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam")):
            libraries = [steam] + _steam_libraries(os.path.join(steam, "steamapps", "libraryfolders.vdf"))
            for lib in libraries:
                out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BinLinux", "BeamNG.drive.x64"))
    return out


def _steam_libraries(vdf_path):
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
    return {"bundles": bundles, "levelsDirs": levels, "beamngExe": find_beamng_exe(values),
            "platform": sys.platform, "appDir": app_dir()}


def schema_for_ui():
    """SCHEMA as a list the UI can render: [{key, kind, default, help, group, choices}]."""
    group_of = {key: title for title, keys in GROUPS for key in keys}
    out = []
    for key, (default, kind, help_text) in SCHEMA.items():
        entry = {"key": key, "kind": kind.split(":", 1)[0], "default": default, "help": help_text,
                 "group": group_of.get(key, "Other")}
        if kind.startswith("choice:"):
            entry["choices"] = kind.split(":", 1)[1].split("|")
        out.append(entry)
    return out
