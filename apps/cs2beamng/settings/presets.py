"""The quality presets, and the custom snapshot a person builds by hand.

A preset is a whole-dict overlay applied on top of the defaults; a user's explicit changes after
picking one stick, and `preset_changes` is what the sidebar's badges count. Applying a preset
never loses the hand-tuned values it covers: they are kept in `custom.json` so pressing Custom
comes back to them.
"""

import json
import os

from .catalog import SCHEMA
from .store import app_dir

# One line per preset, for the segmented control. What it costs, not what it contains — the
# numbers come from `pipeline/history.py`'s measured stage rates on the reference city.
PRESET_INFO = {
    "ultra": "Everything, full-size textures, longest view. Hours to build, and only an ultra card runs it.",
    "high": "Everything at 512 px. The default, and what the reference city is validated at.",
    "performance": "Everything, smaller textures and fewer drawn triangles. For an 8 GB card at high resolution.",
    "medium": "60 % of the trees, 70 % of the props, 3 km view. A mid-range card.",
    "low": "Roads, buildings and a third of the foliage. What runs on a laptop.",
}

PRESETS = {
    "ultra": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 0,
        "texture_max_normal": 0,
        "texture_max_data": 0,
        "texture_max_variant": 0,
        "generate_lods": True,
        "paint_variants_tree": 4,
        "paint_variants_prop": 2,
        "forest_min_items": 0,
        "forest_rare_type_min": 0,
        "lane_max_tris": 0,
        "tile_lod": True,
        "lane_furniture": True,
        "lights": True,
        "groundcover": False,
        "forest_shadows": True,
        "collision_preset": "exact",
        "lod_detail_scale": 3.0,
        "lod_switch_error_px": 4.0,
        "view_distance": 8000,
    },
    # 8.0 for `lod_switch_error_px`, not the 6.0 this shipped with until 2026-08-20. `high` is
    # what the defaults are, and the defaults are 8.0 - which is the value the reference city was
    # built and validated at through loads #11-#17, while 6.0 was never built at all. The two
    # disagreed silently for as long as nothing compared them; the sidebar's badge showed a fresh
    # install as "High, with 1 setting of your own", and `test_app_shell` now gates it.
    "high": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 512,
        "texture_max_normal": 512,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 10,
        "forest_rare_type_min": 10,
        "lane_max_tris": 0,
        "tile_lod": True,
        "lane_furniture": True,
        "lights": False,
        "groundcover": False,
        "forest_shadows": True,
        "collision_preset": "simplified",
        "lod_detail_scale": 6.0,
        "lod_switch_error_px": 8.0,
        "view_distance": 4000,
    },
    # Everything `high` ships, plus the knobs that cost residency rather than geometry — see
    # `docs/cs2-api-audit.md` §64 and §65. The two texture caps came down to 256 in A6.0: at 512
    # this preset mounted 5,224 MiB, which is the MEASURED point at which an 8 GB card at
    # 3440x1440 starts refusing allocations (131,073 of them in one session). 256 takes 497 MiB
    # off — 282 colour, 215 normal, measured over all 7,661 DDS — without touching a triangle,
    # which is why it goes first. It does put colour below vanilla practice (185 of
    # west_coast_usa's 256 colour maps are 512 or smaller, so some ARE 512); the geometry is
    # worth more than the texel, and `high` still ships 512 for looking at things up close.
    "performance": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 10,
        "forest_rare_type_min": 10,
        "lane_max_tris": 4000000,
        "tile_lod": True,
        "lane_furniture": True,
        "lane_furniture_categories": "fence",
        "lights": True,
        "groundcover": False,
        "forest_shadows": False,
        "collision_furniture": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 10.0,
        "lod_detail_scale_prop": 4.0,
        "lod_switch_error_px": 8.0,
        "view_distance": 4000,
    },
    "medium": {
        "foliage_density": 60,
        "prop_density": 70,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 25,
        "forest_rare_type_min": 25,
        "lane_max_tris": 3000000,
        "tile_lod": True,
        "lane_furniture": True,
        "lane_furniture_categories": "fence",
        "lights": False,
        "groundcover": False,
        "forest_shadows": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 10.0,
        "lod_switch_error_px": 12.0,
        "view_distance": 3000,
    },
    "low": {
        "foliage_density": 30,
        "prop_density": 40,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 128,
        "texture_max_variant": 128,
        "generate_lods": True,
        "paint_kinds": "tree",
        "paint_variants_tree": 1,
        "paint_variants_prop": 1,
        "forest_min_items": 50,
        "forest_rare_type_min": 25,
        "lane_max_tris": 1000000,
        "tile_lod": True,
        "lane_furniture": False,
        "lights": False,
        "groundcover": False,
        "forest_shadows": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 16.0,
        "lod_switch_error_px": 20.0,
        "markings": True,
        "areas": False,
        "skirts": False,
        "view_distance": 2000,
    },
}


CUSTOM_FILE = "custom.json"


def save_custom(values, base):
    """Remember the settings the user has made their own, and the preset they started from.

    The preset row shows Custom at all times and pressing it comes back HERE, so a person who
    has spent ten minutes tuning can look at what `Performance` does and return without losing
    any of it. `base` is what the badges are counted against - "what have I moved off the
    preset" has no answer at all once the preset name has been overwritten with "custom", which
    is why it is kept beside the values rather than in place of them.
    """
    path = os.path.join(app_dir(), CUSTOM_FILE)
    tmp = path + ".tmp"
    payload = {"base": base, "values": {k: v for k, v in values.items() if k in SCHEMA}}
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_custom():
    """`{"base": name, "values": {...}}`, or None when nothing has been made custom yet."""
    path = os.path.join(app_dir(), CUSTOM_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("values"), dict):
        return None
    base = saved.get("base")
    return {
        "base": base if base in PRESETS else None,
        "values": {k: v for k, v in saved["values"].items() if k in SCHEMA},
    }


def apply_custom(values):
    """The remembered custom settings back over the current ones. Unchanged if there are none."""
    saved = load_custom()
    if not saved:
        return dict(values)
    out = dict(values)
    out.update(saved["values"])
    out["quality_preset"] = "custom"
    return out


def apply_preset(values, name):
    if name not in PRESETS:
        raise KeyError(name)
    out = dict(values)
    out.update(PRESETS[name])
    out["quality_preset"] = name
    return out


def preset_changes(values, base=None):
    """The keys whose value has been moved off the preset last applied, sorted.

    `quality_preset` stays the name of the preset that was applied; whether the values still
    match it is derived here rather than stored as a third state ("custom") that has to be kept
    in step with them. It is also what the sidebar's badges count, which is the third answer to
    "the presets don't seem to do anything": after applying one, the sidebar shows where it
    changed things, and after hand-editing it shows what you have moved.

    `base` names the preset to compare against; without it, the one `quality_preset` holds.
    Once the values have been made custom that name is "custom", and the base comes from the
    remembered custom snapshot instead. `None` where there is nothing to compare against at all.
    """
    preset = PRESETS.get(base or values.get("quality_preset"))
    if preset is None:
        return None
    return sorted(key for key, want in preset.items() if values.get(key) != want)
