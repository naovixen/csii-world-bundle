"""Settings — every knob the app exposes, with defaults, validation, presets and per-OS paths.

One flat dict, persisted at `~/.cs2beamng/settings.json`, edited by the UI and by
`cs2beamng build --set key=value`. Everything the pipeline reads comes from here, so a headless
build and a UI build with the same settings produce the same level.

Defaults lean towards **fidelity with performance** (the "High" preset): every layer on,
foliage at 100 %, textures capped at 1024, generated LODs on, collision on for everything
physical, paint collision off. Presets are whole-dict overrides applied on top of the defaults;
a user's explicit changes after picking a preset stick.

Where things are:

* `model` — the `Setting` record: one declaration per knob, and what each of its fields means.
* `paths`, `world`, `collision`, `textures`, `navigation`, `tuning`, `scenery`, `build`,
  `driven` — the settings themselves, grouped the way the screen groups them.
* `catalog` — joins those into `SETTINGS` and derives `SCHEMA`, `LABELS`, `BLURBS` and the rest.
* `sections` — where each key appears on screen; the placement authority.
* `screen` — the view model: `schema_for_ui`, `control_of`, `section_of`.
* `values` — `coerce`, `validate`, `region_of`, and what one control sets on another's behalf.
* `store` / `presets` — the saved file, the quality presets, the custom snapshot.
* `discovery` — where this machine keeps the app's files, BeamNG, and its levels.

Import this package, not its modules: `from cs2beamng import settings as settings_mod`.
"""

from .catalog import (
    BLURBS,
    BY_KEY,
    CHOICE_SETS,
    CONTROLS,
    DETAILS,
    DRIVEN_BY,
    LABELS,
    NEEDS,
    PARENT_OFF,
    RANGES,
    SCHEMA,
    SETTINGS,
    SUBSET_OF,
    UNITS,
    ZERO_LABEL,
)
from .discovery import (
    APP_DIR_NAME,
    BEAMNG_APP_ID,
    app_dir,
    candidate_beamng_exes,
    candidate_userfolders,
    default_levels_dir,
    detected,
    find_beamng_exe,
)
from .model import Setting
from .presets import (
    CUSTOM_FILE,
    PRESET_INFO,
    PRESETS,
    apply_custom,
    apply_preset,
    load_custom,
    preset_changes,
    save_custom,
)
from .screen import (
    control_of,
    is_technical,
    quality_keys,
    schema_for_ui,
    section_of,
    sections_for_ui,
)
from .sections import BASIC, BASIC_MAX, BASICS, SECTIONS
from .store import SETTINGS_FILE, defaults, load, save
from .values import DRIVES, coerce, driven_changes, parse_region, region_of, validate

__all__ = [
    "APP_DIR_NAME",
    "BASIC",
    "BASICS",
    "BASIC_MAX",
    "BEAMNG_APP_ID",
    "BLURBS",
    "BY_KEY",
    "CHOICE_SETS",
    "CONTROLS",
    "CUSTOM_FILE",
    "DETAILS",
    "DRIVEN_BY",
    "DRIVES",
    "LABELS",
    "NEEDS",
    "PARENT_OFF",
    "PRESETS",
    "PRESET_INFO",
    "RANGES",
    "SCHEMA",
    "SECTIONS",
    "SETTINGS",
    "SETTINGS_FILE",
    "SUBSET_OF",
    "UNITS",
    "ZERO_LABEL",
    "Setting",
    "app_dir",
    "apply_custom",
    "apply_preset",
    "candidate_beamng_exes",
    "candidate_userfolders",
    "coerce",
    "control_of",
    "default_levels_dir",
    "defaults",
    "detected",
    "driven_changes",
    "find_beamng_exe",
    "is_technical",
    "load",
    "load_custom",
    "parse_region",
    "preset_changes",
    "quality_keys",
    "region_of",
    "save",
    "save_custom",
    "schema_for_ui",
    "section_of",
    "sections_for_ui",
    "validate",
]
