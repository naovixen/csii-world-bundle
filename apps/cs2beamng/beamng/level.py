"""The level folder model: SimGroups, objects, materials, textures, `info.json`.

    level = Level(level_dir, "cs2_city", "CS2 City")
    level.add(["Level_objects"], {"name": "theTerrain", "class": "TerrainBlock", ...})
    level.write_materials("terrains", {...})           # art/terrains/main.materials.json
    level.write_objects()                                # main/**/items.level.json
    level.write_info({...})

Objects are appended to a group path (each element a SimGroup name under `MissionGroup`);
groups are created on demand. `write_objects` lays the tree out the way BeamNG's own levels
do: `main/items.level.json` holds `MissionGroup`, and every group with children owns a
directory `main/MissionGroup/<a>/<b>/items.level.json` listing them, one JSON object per line
with `__parent`. `persistentId`s are uuid5 of `<level>:<key>` so a rebuild is a diff, not a
new world.
"""

import json
import os
import shutil
import uuid

NAMESPACE = uuid.UUID("7d3d3a3e-2c7a-4a1e-9f6a-5c9b8e2d1a4f")     # cs2beamng persistentId namespace, never change


class Level:
    def __init__(self, level_dir, name, title):
        self.dir = os.path.abspath(level_dir)
        self.name = name
        self.title = title
        self._groups = {}          # tuple(path) -> list of objects (dicts) in order
        self._group_meta = {}      # tuple(path) -> extra fields on the SimGroup itself
        self._ids = set()
        os.makedirs(self.dir, exist_ok=True)

    # -- ids and paths -------------------------------------------------------------------------

    def pid(self, key):
        """A stable persistentId for `key` (any string unique within the level)."""
        return str(uuid.uuid5(NAMESPACE, f"{self.name}:{key}"))

    def game_path(self, *parts):
        """`/levels/<name>/<parts>` — how the level refers to its own files."""
        return "/levels/" + self.name + "/" + "/".join(p.replace("\\", "/") for p in parts)

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    # -- objects -------------------------------------------------------------------------------

    def group(self, path, **fields):
        """Ensure the SimGroup chain `path` exists; `fields` land on the last group."""
        path = tuple(path)
        for depth in range(1, len(path) + 1):
            self._groups.setdefault(path[:depth], [])
        if fields:
            self._group_meta.setdefault(path, {}).update(fields)
        return path

    def add(self, path, obj, key=None):
        """Append `obj` (needs `class` and `name`) under the group `path`. `key` seeds the
        persistentId (defaults to the object's name, which must then be unique)."""
        path = self.group(path)
        obj = dict(obj)
        obj.setdefault("persistentId", self.pid(key or obj["name"]))
        if obj["persistentId"] in self._ids:
            raise ValueError(f"duplicate persistentId for {obj.get('name')!r} (key {key!r})")
        self._ids.add(obj["persistentId"])
        obj["__parent"] = path[-1] if path else "MissionGroup"
        self._groups[path].append(obj)
        return obj

    def write_objects(self):
        """Write `main/**/items.level.json`. Returns the number of objects written."""
        main = self.path("main")
        if os.path.isdir(main):
            shutil.rmtree(main)                          # the tree is rewritten whole: no stale groups from an older build
        os.makedirs(main, exist_ok=True)
        root = {"name": "MissionGroup", "class": "SimGroup", "persistentId": self.pid("MissionGroup"), "enabled": "1"}
        _write_lines(os.path.join(main, "items.level.json"), [root])
        count = 0
        # every group's own file lists its child groups first (BeamNG's order), then its objects
        by_parent = {}
        for path in sorted(self._groups):
            by_parent.setdefault(path[:-1], []).append(path)
        for path in sorted(set(list(self._groups) + [()])):
            children = []
            for child in by_parent.get(path, []):
                meta = dict(self._group_meta.get(child, {}))
                meta.update({"name": child[-1], "class": "SimGroup", "persistentId": self.pid("group:" + "/".join(child)),
                             "__parent": path[-1] if path else "MissionGroup"})
                children.append(meta)
            children.extend(self._groups.get(path, []))
            if not children:
                continue
            directory = os.path.join(main, "MissionGroup", *path)
            os.makedirs(directory, exist_ok=True)
            _write_lines(os.path.join(directory, "items.level.json"), children)
            count += len(self._groups.get(path, []))
        return count

    # -- materials, textures, info -------------------------------------------------------------

    def write_materials(self, art_subdir, materials):
        """`art/<art_subdir>/main.materials.json` from `{name: {...}}` (v1.5 style objects)."""
        directory = self.path("art", *art_subdir.split("/"))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "main.materials.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(materials, handle, indent=1, sort_keys=True)
        return path

    def copy_file(self, src, *rel_dst):
        """Copy `src` into the level at `rel_dst`. A real copy, never a hard link: a later stage
        that opens the level's copy for writing must not reach the bundle's original through a
        shared inode (that happened once — the bundle's terrain textures lost their alpha)."""
        dst = self.path(*rel_dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copyfile(src, dst)
        return dst

    def write_info(self, info):
        with open(self.path("info.json"), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(info, handle, indent=2)

    def write_json(self, doc, *rel):
        path = self.path(*rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(doc, handle, indent=1)
        return path


def _write_lines(path, objects):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")


def read_objects(level_dir):
    """Every object in `main/**/items.level.json` as `(relative_file, line_no, dict)`; a parse
    error yields `(file, line_no, None)`."""
    main = os.path.join(level_dir, "main")
    for root, _dirs, files in os.walk(main):
        for name in files:
            if name != "items.level.json":
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, level_dir).replace("\\", "/")
            with open(full, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield rel, number, json.loads(line)
                    except ValueError:
                        yield rel, number, None
