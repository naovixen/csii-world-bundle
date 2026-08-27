"""Binding a bundle material name to the one this level's shapes and tiles reference.

A bundle material has a name; a BeamNG level references a level-prefixed, sanitised version of
it, and the bake may alias several bundle names onto one canonical name first. Four things need
that mapping and nothing else: the `materials`, `derived` and `networks` stages, and the tile
workers those last two spawn.

**Why its own module.** It lived in `stages/materials.py`, and `derived` and `networks` imported
it from there — one stage acting as another's library. Moving it into `beamng/materials.py`
beside `mapto` fixed that and broke something else: `test_app_shell` checks that a stage declares
every setting any module it can REACH reads, and `beamng/materials.py` also exports textures and
reads `texture_max`. Two stages that never look at `texture_max` would have had to declare it,
which would then re-run them both whenever it changed. The binding is a handful of lines with no
settings in it at all, so it gets its own module and the closure stays honest.
"""

import json
import os

from . import dae


def mapto(level_name, raw_name):
    """The material name a `.dae` binds and `main.materials.json` `mapTo`s: level-prefixed."""
    return dae.sanitize(f"{level_name}_{raw_name}")


def mapto_name(shared, level, raw):
    """Bound material name for a bundle material name (alias → canonical → prefixed), and a
    record that this build binds it (the materials stage exports only what was bound)."""
    info = _aliases(shared, level)
    canonical = info["aliases"].get(raw, raw)
    if shared is not None:
        shared.setdefault("materials_used", set()).add(canonical)
    return mapto(level.name, canonical)


def aliases_of(shared, level):
    """The bake's alias table itself — what a worker process needs to bind names without the
    `shared` dict (which cannot cross a process boundary and must not: it is where a stage
    records what it bound)."""
    return _aliases(shared, level)["aliases"]


def _aliases(shared, level):
    """The bake's alias table, loaded once per build (before the materials stage runs)."""
    if shared is not None and "materials_aliases" in shared:
        return shared["materials_aliases"]
    aliases = {}
    derived_dir = getattr(level, "derived_dir", None)
    if derived_dir:
        path = os.path.join(derived_dir, "materials.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    aliases = json.load(handle).get("aliases") or {}
            except (OSError, ValueError):
                aliases = {}
    info = {"aliases": aliases}
    if shared is not None:
        shared["materials_aliases"] = info
    return info
