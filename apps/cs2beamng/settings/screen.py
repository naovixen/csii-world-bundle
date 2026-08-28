"""The settings screen's model: where each key is drawn and what it is drawn as.

`SCHEMA` is the contract (name, type, default) and `DETAILS` is what the reference page is
generated from. Neither is a description of a *screen*: the help strings are written for a page
with no labels beside them, and `DETAILS` is the argument for a default rather than an
explanation of a control.

What is here is the screen. Every key gets a plain label, a one-line blurb and a control kind;
every key lands in exactly one place — the basics strip, one sidebar section, or the driven list,
which is the settings that still exist for `--set` and the docs but no longer have a control of
their own because something else on screen now sets them. The test suite checks the partition,
the length and the language (no identifiers, no research figures, no section numbers) — see
`tools/test_app_shell.py`.
"""

from . import sections as sections_mod
from .catalog import BY_KEY, SCHEMA, SETTINGS
from .model import CONTROL_BY_KIND
from .sections import BASIC, BASICS, SECTIONS

# `SECTIONS` is reached through the module rather than through the name bound above, so that
# adding a section stays the data change `sections.py` says it is: the test suite appends one at
# runtime and expects the sidebar to serve it, which a name copied at import time would not see.


def control_of(key):
    """The control kind for a key: its own `control`, else what its type implies."""
    setting = BY_KEY[key]
    if setting.control:
        return setting.control
    return CONTROL_BY_KIND.get(setting.base_kind, "text")


def section_of(key):
    """`"basics"`, a section id, or `"driven"` - the one place on screen this key is shown."""
    setting = BY_KEY.get(key)
    return setting.section if setting else "driven"


def is_technical(key):
    """Whether this key lives in a section that keeps its jargon (see SECTIONS)."""
    where = section_of(key)
    return any(s.get("technical") and s["id"] == where for s in sections_mod.SECTIONS)


def quality_keys():
    """The keys whose value is a QUALITY choice, so that changing one puts you on `custom`.

    Everything in a sidebar section, which is every setting that changes the level that gets
    built - minus the two that only decide where the window is. The basics strip is not in it:
    naming your level folder is not a change of quality.
    """
    ui_only = {"ui_section", "ui_browser"}
    keys = {k for s in sections_mod.SECTIONS for _t, ks in s["blocks"] for k in ks}
    return sorted(keys - ui_only)


def sections_for_ui():
    """`SECTIONS` as the page wants it: blocks as dicts, holding only keys the app knows."""
    out = []
    for section in sections_mod.SECTIONS:
        entry = {k: v for k, v in section.items() if k != "blocks"}
        entry["blocks"] = [
            {"title": title, "keys": [k for k in keys if k in SCHEMA]}
            for title, keys in section["blocks"]
        ]
        out.append(entry)
    return out


def schema_for_ui():
    """The settings as a list the UI can render: one entry per key, sparse fields omitted.

    `help` is always shown and is one sentence; `detail` is the long form and is shown only when
    the user asks for it. `tier` is "basic" or "advanced" — the basic ones are the default panel.
    """
    basic = set(BASIC)
    out = []
    for setting in SETTINGS:
        key = setting.key
        entry = {
            "key": key,
            "kind": setting.base_kind,
            "default": setting.default,
            "help": setting.help,
            "tier": "basic" if key in basic else "advanced",
            # E1: what the redesigned screen needs. `label`/`blurb` are the plain words,
            # `control` is how it is drawn, `section` is the one place it appears.
            "label": setting.label,
            "blurb": setting.blurb,
            "control": control_of(key),
            "section": setting.section,
        }
        if setting.range is not None:
            low, high, step, suffix = setting.range
            entry["range"] = {"min": low, "max": high, "step": step, "suffix": suffix}
        if setting.zero_label is not None:
            entry["zeroLabel"] = setting.zero_label
        if setting.driven_by is not None:
            entry["drivenBy"] = setting.driven_by
        if setting.unit is not None:
            entry["unit"] = setting.unit
        if setting.subset_of is not None:
            entry["subsetOf"] = setting.subset_of
        if setting.needs is not None:
            entry["needs"] = setting.needs
        if setting.choice_set is not None:
            entry["choiceSet"] = [{"id": cid, "label": label} for cid, label in setting.choice_set]
        if setting.detail is not None:
            entry["detail"] = setting.detail
        if setting.choices is not None:
            entry["choices"] = setting.choices
        out.append(entry)
    return out


__all__ = [
    "BASIC",
    "BASICS",
    "SECTIONS",
    "control_of",
    "is_technical",
    "quality_keys",
    "schema_for_ui",
    "section_of",
    "sections_for_ui",
]
