"""Turning what a person typed into a setting's value, and saying what is wrong with it.

Three jobs: `coerce` (one string to one typed value), `validate` (the whole dict to a list of
problems the UI prints), and `DRIVES` (the settings one control now sets on another's behalf).
"""

import os

from .catalog import SCHEMA

# What a control sets on another setting's behalf. `Setting.driven_by` is the SENTENCE shown in
# place of the missing control; this is the behaviour, in one place, so the two cannot say
# different things.
#
#   * `terrain_cut` follows `networks`. Cutting the ground open at a trench or a tunnel mouth is
#     not a thing a person wants separately - it is how the roads there get their ground - and
#     with the roads off the hole has nothing in it.
#   * A density slider at zero turns its kind OFF rather than keeping none of it. "0 % of trees"
#     and "no trees" are the same level; saying it twice lets the two disagree, and a report that
#     says a stage kept 0 of 376,187 items has done the work of deciding that 376,187 times.
#
# `{key: (driven, rule)}`, applied by `server.update_settings` - which is the APP. `--set` never
# passes through it and stays literal, so a headless build does exactly and only what it was
# told, and `--set terrain_cut=true networks=false` is still a thing a person may ask for.
DRIVES = {
    "networks": ("terrain_cut", bool),
    "foliage_density": ("trees", lambda v: int(v or 0) > 0),
    "prop_density": ("props", lambda v: int(v or 0) > 0),
}


def driven_changes(incoming):
    """The extra `{key: value}` these changes imply, minus anything the caller set itself.

    A caller that names both halves means both halves: the rule fills in the one you did not
    mention, it does not overrule the one you did.
    """
    out = {}
    for key, value in (incoming or {}).items():
        rule = DRIVES.get(key)
        if rule is None:
            continue
        driven, decide = rule
        if driven not in incoming:
            out[driven] = decide(value)
    return out


def coerce(key, raw):
    """A string from the CLI/UI into the setting's type. Raises ValueError with a readable reason."""
    if key not in SCHEMA:
        raise ValueError(f"unknown setting {key!r}")
    _default, kind, _help = SCHEMA[key]
    if raw is None:
        return None
    text = raw.strip() if isinstance(raw, str) else raw
    if kind == "bool":
        return _as_bool(key, raw, text)
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
        return _as_choice(key, raw, text, kind.split(":", 1)[1].split("|"))
    if kind in ("str", "path"):
        return None if text in ("", None) else str(text)
    return text


def _as_bool(key, raw, text):
    if isinstance(text, bool):
        return text
    low = str(text).lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key}: expected true/false, got {raw!r}")


def _as_choice(key, raw, text, choices):
    value = str(text)
    if value not in choices:
        raise ValueError(f"{key}: expected one of {choices}, got {raw!r}")
    return int(value) if all(c.lstrip("-").isdigit() for c in choices) else value


def parse_region(text):
    """`"X Z R"` (commas allowed) as `(x, z, half_width)`, or None when it is not one.

    The one place the region string is read. It used to be parsed twice - `validate` caught a
    malformed one and `region_of` raised `ValueError` on the same input - so whether a bad region
    was a printed problem or a traceback depended on which of the two a caller reached first.
    """
    if not text:
        return None
    try:
        parts = [float(p) for p in str(text).replace(",", " ").split()]
    except ValueError:
        return None
    if len(parts) != 3 or parts[2] <= 0:
        return None
    return tuple(parts)


def validate(values):
    """Problems as a list of strings (empty = ok). Does not touch the filesystem beyond exists()."""
    problems = []
    if not values.get("bundle") or not os.path.isfile(
        os.path.join(values["bundle"], "bundle.json")
    ):
        problems.append(f"bundle: no bundle.json at {values.get('bundle')!r}")
    if not values.get("output"):
        problems.append("output: no output directory")
    name = values.get("level_name") or ""
    if not name or any(c for c in name if not (c.isalnum() or c in "_-")):
        problems.append("level_name: letters, digits, '_' and '-' only")
    for key in ("foliage_density", "prop_density"):
        value = values.get(key)
        if not isinstance(value, int) or not 0 <= value <= 100:
            problems.append(f"{key}: 0..100")
    if values.get("region") and parse_region(values["region"]) is None:
        problems.append("region: 'X Z R' with R > 0, or empty")
    return problems


def region_of(values):
    """The region these settings name, as the bake and the stages want it, or None."""
    parsed = parse_region(values.get("region"))
    if parsed is None:
        return None
    x, z, r = parsed
    return {"centerX": x, "centerZ": z, "radius": r}
