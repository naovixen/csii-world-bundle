"""Turn a built level's distant `Objects/km_x_y` cells into `loadMode "Manual"` prefabs — the
never-loading half of the streaming question (Phase 10 residency, 2026-08-30).

    python tools/beamng/cellprefab.py <level_dir> [--keep-radius 2000] [--spawn X Y]
    python tools/beamng/cellprefab.py <level_dir> --restore

Record §7 measured that DELETING the city after load frees no GPU memory. It never measured the
other direction: a `TSStatic` opens its `.cdae` in `onAdd()`, and a `Prefab` with
`loadMode "Manual"` adds nothing until `:load()` is called — so a cell that is never loaded
should never mount its shapes. This rewrites every object cell further than `--keep-radius`
from the spawn into `prefabs/<cell>.prefab.json` (the items.level.json dialect with a root
`SimGroup`; positions stay world positions under a `groupPosition` of 0 0 0, which is what
vanilla's own `soccer.prefab.json` does) and leaves a `Prefab` object in its place.

In-game, the reading that decides it:

    dump(Engine.Render.getMemoryInfo())                       -- before
    for _, n in ipairs(scenetree.findClassObjects('Prefab')) do scenetree.findObject(n):load() end
    be:reloadCollision()
    dump(Engine.Render.getMemoryInfo())                       -- after: a jump = lazy loading works

`--restore` puts the original `Objects/` tree back from the backup this makes beside it.
"""

import argparse
import json
import math
import os
import shutil
import sys
import uuid

OBJECTS = ("main", "MissionGroup", "Objects")
NAMESPACE = uuid.UUID("7a1f8d1a-6f1e-5d0e-9b8c-0c3e11a8ef00")


def _lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write(path, objs):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for obj in objs:
            handle.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def _spawn(level_dir):
    path = os.path.join(level_dir, "main", "MissionGroup", "PlayerDropPoints", "items.level.json")
    if os.path.isfile(path):
        for obj in _lines(path):
            if obj.get("class") == "SpawnSphere" and obj.get("position"):
                return float(obj["position"][0]), float(obj["position"][1])
    return None


def _cell_centre(name, metres=1000.0):
    _km, x, y = name.split("_")
    return (int(x) + 0.5) * metres, (int(y) + 0.5) * metres


def convert(level_dir, keep_radius, spawn):
    level_name = os.path.basename(os.path.normpath(level_dir))
    objects_dir = os.path.join(level_dir, *OBJECTS)
    backup = objects_dir + ".cellprefab-backup"
    if os.path.isdir(backup):
        sys.exit(f"{backup} exists — run --restore first")
    shutil.copytree(objects_dir, backup)
    prefab_dir = os.path.join(level_dir, "prefabs")
    os.makedirs(prefab_dir, exist_ok=True)
    index_path = os.path.join(objects_dir, "items.level.json")
    index = _lines(index_path)
    kept, converted, moved_objects = [], [], 0
    for obj in index:
        name = obj.get("name", "")
        if obj.get("class") != "SimGroup" or not name.startswith("km_"):
            kept.append(obj)
            continue
        cx, cy = _cell_centre(name)
        cell_dir = os.path.join(objects_dir, name)
        cell_file = os.path.join(cell_dir, "items.level.json")
        if math.hypot(cx - spawn[0], cy - spawn[1]) <= keep_radius or not os.path.isfile(cell_file):
            kept.append(obj)
            continue
        members = _lines(cell_file)
        root = {
            "name": name,
            "class": "SimGroup",
            "persistentId": str(uuid.uuid5(NAMESPACE, f"{level_name}:prefabroot:{name}")),
            "__parent": "MissionGroup",
            "groupPosition": "0.000000 0.000000 0.000000",
        }
        _write(os.path.join(prefab_dir, f"{name}.prefab.json"), [root, *members])
        kept.append(
            {
                "name": f"{name}_prefab",
                "class": "Prefab",
                "persistentId": str(uuid.uuid5(NAMESPACE, f"{level_name}:prefab:{name}")),
                "__parent": "Objects",
                "position": [0, 0, 0],
                "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                "scale": [1, 1, 1],
                "filename": f"/levels/{level_name}/prefabs/{name}.prefab.json",
                "loadMode": "Manual",
            }
        )
        shutil.rmtree(cell_dir)
        converted.append(name)
        moved_objects += len(members)
    _write(index_path, kept)
    print(
        f"{len(converted)} cells beyond {keep_radius:.0f} m of spawn ({spawn[0]:.0f}, {spawn[1]:.0f}) "
        f"-> Manual prefabs, {moved_objects:,} objects; backup at {backup}"
    )
    print(__doc__.split("In-game, the reading that decides it:")[1].split("`--restore`")[0])


def restore(level_dir):
    objects_dir = os.path.join(level_dir, *OBJECTS)
    backup = objects_dir + ".cellprefab-backup"
    if not os.path.isdir(backup):
        sys.exit("no backup to restore")
    shutil.rmtree(objects_dir)
    shutil.move(backup, objects_dir)
    prefab_dir = os.path.join(level_dir, "prefabs")
    if os.path.isdir(prefab_dir):
        shutil.rmtree(prefab_dir)
    print("restored")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("level_dir")
    parser.add_argument("--keep-radius", type=float, default=2000.0)
    parser.add_argument("--spawn", type=float, nargs=2, metavar=("X", "Y"))
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if not os.path.isdir(os.path.join(args.level_dir, *OBJECTS)) and not args.restore:
        sys.exit(f"{args.level_dir!r} is not a built level (no main/MissionGroup/Objects under it)")
    if args.restore:
        return restore(args.level_dir)
    spawn = tuple(args.spawn) if args.spawn else _spawn(args.level_dir)
    if spawn is None:
        sys.exit("no SpawnSphere found; pass --spawn X Y")
    convert(args.level_dir, args.keep_radius, spawn)


if __name__ == "__main__":
    main()
