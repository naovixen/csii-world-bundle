"""Every setting, and the tables derived from them.

The nine catalog modules declare one `Setting` per key; this joins them into `SETTINGS` and
derives the dictionaries the rest of the app imports. Those tables are exactly what they were
when they were written out by hand — the difference is that they are now consequences of one
declaration rather than sixteen places to remember.

A sparse table (`UNITS`, `SUBSET_OF`, `PARENT_OFF`, …) holds only the keys whose field is set,
which is what the code reading it expects: every one of them is probed with `in` or `.get`.
"""

from . import (
    build,
    collision,
    driven,
    navigation,
    paths,
    scenery,
    sections,
    textures,
    tuning,
    world,
)
from .model import Setting

#: Declaration order, which is the order `SCHEMA` and the reference page come out in.
SETTINGS: tuple[Setting, ...] = (
    *paths.BASICS_SETTINGS,
    *world.WORLD_SETTINGS,
    *collision.COLLISION_SETTINGS,
    *textures.TEXTURE_SETTINGS,
    *navigation.NAVIGATION_SETTINGS,
    *tuning.TUNING_SETTINGS,
    *scenery.SCENERY_SETTINGS,
    *build.BUILD_SETTINGS,
    *driven.DRIVEN_SETTINGS,
)


def _placement() -> dict[str, str]:
    """Each key's section id, from `SECTIONS` — the placement authority.

    `"basics"` for the strip above the sidebar, a section id for the sidebar proper, and
    `"driven"` for a setting that has no control of its own.
    """
    where = dict.fromkeys(sections.BASICS, "basics")
    for section in sections.SECTIONS:
        for _title, keys in section["blocks"]:
            for key in keys:
                where.setdefault(key, section["id"])
    return where


def build_settings() -> tuple[Setting, ...]:
    """`SETTINGS` with each record's `section` filled in, and `ui_section`'s choices resolved.

    Adding a section is an entry in `SECTIONS` and nothing else, which includes the setting that
    remembers which one you were in: its choices are the sections that exist. That used to be a
    hand-written line restating the section ids, which is a second place to remember; it is a
    consequence now. Re-runnable so `tools/test_app_shell.py` can add a section and check that
    nothing else needed an edit.
    """
    where = _placement()
    ids = "|".join(section["id"] for section in sections.SECTIONS)
    out = []
    for setting in SETTINGS:
        changes = {"section": where.get(setting.key, "driven")}
        if setting.key == "ui_section":
            changes["kind"] = "choice:" + ids
            changes["default"] = sections.SECTIONS[0]["id"]
        out.append(Setting(**{**setting.__dict__, **changes}))
    return tuple(out)


SETTINGS = build_settings()

#: `key -> Setting`, for anything that wants the record rather than one of the tables below.
BY_KEY: dict[str, Setting] = {setting.key: setting for setting in SETTINGS}

# -- the derived tables --------------------------------------------------------------------------
#
# Each is `Setting.<field>` pivoted by key. They exist because ~30 call sites across the app, the
# server payload, the docs generator and the test suite import them by these names; the field
# docs on `Setting` say what each one means.

SCHEMA = {s.key: (s.default, s.kind, s.help) for s in SETTINGS}
LABELS = {s.key: s.label for s in SETTINGS}
BLURBS = {s.key: s.blurb for s in SETTINGS}
DETAILS = {s.key: s.detail for s in SETTINGS if s.detail is not None}
UNITS = {s.key: s.unit for s in SETTINGS if s.unit is not None}
CONTROLS = {s.key: s.control for s in SETTINGS if s.control is not None}
RANGES = {s.key: s.range for s in SETTINGS if s.range is not None}
ZERO_LABEL = {s.key: s.zero_label for s in SETTINGS if s.zero_label is not None}
CHOICE_SETS = {
    s.key: [tuple(pair) for pair in s.choice_set] for s in SETTINGS if s.choice_set is not None
}
SUBSET_OF = {s.key: s.subset_of for s in SETTINGS if s.subset_of is not None}
PARENT_OFF = {s.key: s.parent_off for s in SETTINGS if s.parent_off is not None}
NEEDS = {s.key: s.needs for s in SETTINGS if s.needs is not None}
DRIVEN_BY = {s.key: s.driven_by for s in SETTINGS if s.driven_by is not None}
