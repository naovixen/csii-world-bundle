"""The level folder as the game sees it: its objects, its materials, and its path scheme."""

import json
import os
import re

from cs2beamng.beamng import level as level_mod


def read_level(level_dir):
    objects = [o for _f, _n, o in level_mod.read_objects(level_dir) if o]
    materials = {}
    art = os.path.join(level_dir, "art")
    for root, _d, files in os.walk(art):
        for name in files:
            if name.endswith(".materials.json"):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    try:
                        doc = json.load(handle)
                    except ValueError:
                        continue
                for key, mat in (doc or {}).items():
                    if isinstance(mat, dict):
                        materials.setdefault(str(mat.get("mapTo") or mat.get("name") or key), mat)
    return objects, materials


def resolve(level_dir, game_path):
    """`/levels/<name>/art/...` → absolute path (the level dir replaces `/levels/<name>`)."""
    p = str(game_path).replace("\\", "/")
    m = re.match(r"^/?levels/[^/]+/(.*)$", p)
    rel = m.group(1) if m else p.lstrip("/")
    return os.path.join(level_dir, *rel.split("/"))
