"""What the level contains, and whether the files it points at are there.

Three passes over the level tree: read every object, resolve every path it names, and read the
forest's own type table. They are one module because the second and third depend on the first
and each adds to the same three lists (`objects`, `missing`, `dae_files`).
"""

import json
import os

from ..level import read_objects


def check_objects(ctx):
    """Every object parses, carries a class and a name, and has a unique `persistentId`.

    A `persistentId` collision is not cosmetic: BeamNG keys its own edits by it, so two objects
    sharing one are two the editor cannot tell apart.
    """
    ids = {}
    parse_errors = 0
    for rel, line_no, obj in read_objects(ctx.level_dir):
        if obj is None:
            parse_errors += 1
            if parse_errors <= 5:
                ctx.note(f"{rel}:{line_no}: not a JSON object")
            continue
        if "class" not in obj or "name" not in obj:
            ctx.note(f"{rel}:{line_no}: object without class/name")
            continue
        pid = obj.get("persistentId")
        if not pid:
            ctx.note(f"{rel}:{line_no}: {obj['name']} has no persistentId")
        elif pid in ids:
            ctx.note(f"{rel}:{line_no}: persistentId {pid} duplicates {ids[pid]}")
        else:
            ids[pid] = f"{rel}:{line_no}"
        ctx.objects.append((rel, line_no, obj))
    for _rel, _n, obj in ctx.objects:
        ctx.by_class.setdefault(obj["class"], []).append(obj)
    ctx.checks["objects"] = len(ctx.objects)
    ctx.checks["classes"] = {k: len(v) for k, v in sorted(ctx.by_class.items())}
    ctx.checks["parseErrors"] = parse_errors
    for cls, want in (("TerrainBlock", 1), ("LevelInfo", 1)):
        have = len(ctx.by_class.get(cls, []))
        if have != want:
            ctx.note(f"expected exactly {want} {cls}, found {have}")
    if not ctx.by_class.get("SpawnSphere"):
        ctx.note("no SpawnSphere")


def check_referenced_files(ctx):
    """Every `terrainFile`, `shapeName` and `minimapImage` the level names is on disk."""
    for _rel, _n, obj in ctx.objects:
        for field in ("terrainFile", "shapeName", "minimapImage"):
            path = ctx.resolve(obj.get(field))
            if path is not None and not shape_present(path):
                ctx.missing.append(f"{obj['class']} {obj['name']}: {field} {obj[field]}")
    for _rel, _n, obj in ctx.objects:
        if obj["class"] == "TSStatic":
            path = ctx.resolve(obj.get("shapeName"))
            if path and os.path.isfile(path) and path.lower().endswith(".dae"):
                if path not in ctx.seen_dae:
                    ctx.seen_dae.add(path)
                    ctx.dae_files.append((obj["name"], path))


def check_forest(ctx):
    """The forest's types resolve, and every placed item names a type that exists (A3).

    Every check here has a load behind it: a `type` with no `TSForestItemData` is silently
    dropped, and a `shapeFile` that does not resolve leaves the type in the item data with
    nothing to draw. Forest shapes never reach `dae_files` through a TSStatic, so without this
    they would skip every check before and after.
    """
    item_path = os.path.join(ctx.level_dir, "art", "forest", "managedItemData.json")
    if os.path.isfile(item_path):
        try:
            with open(item_path, encoding="utf-8") as handle:
                ctx.item_data = json.load(handle) or {}
        except ValueError:
            ctx.note("art/forest/managedItemData.json: not JSON")
    for name, entry in ctx.item_data.items():
        if not isinstance(entry, dict):
            continue
        path = ctx.resolve(entry.get("shapeFile"))
        if path is None or not shape_present(path):
            ctx.missing.append(f"TSForestItemData {name}: shapeFile {entry.get('shapeFile')}")
        elif path.lower().endswith(".dae") and path not in ctx.seen_dae and os.path.isfile(path):
            # `shape_present` above accepts a cooked shape whose SOURCE was dropped; only a
            # source that is really there can be XML-parsed below.
            ctx.seen_dae.add(path)
            ctx.dae_files.append((name, path))
    forest_items, unknown_types = _walk_forest(ctx)
    ctx.checks["forestItems"] = forest_items
    ctx.checks["forestTypes"] = len(ctx.item_data)
    ctx.checks["forestUnknownTypes"] = len(unknown_types)
    for bad in sorted(unknown_types)[:10]:
        ctx.note(f"forest item type {bad!r} has no TSForestItemData - BeamNG drops it silently")
    if forest_items and not ctx.by_class.get("Forest"):
        ctx.note(f"{forest_items} forest items but no Forest object in the level tree")


def _walk_forest(ctx):
    """`(items, unknown types)` over every `.forest4.json` in the level."""
    forest_dir = os.path.join(ctx.level_dir, "forest")
    forest_items = 0
    unknown_types = set()
    if not os.path.isdir(forest_dir):
        return forest_items, unknown_types
    for name in sorted(os.listdir(forest_dir)):
        if not name.endswith(".forest4.json"):
            continue
        with open(os.path.join(forest_dir, name), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                forest_items += 1
                try:
                    item = json.loads(line)
                except ValueError:
                    ctx.note(f"forest/{name}:{number}: not a JSON object")
                    break
                if item.get("type") not in ctx.item_data:
                    unknown_types.add(item.get("type"))
    return forest_items, unknown_types


def shape_present(path):
    """Whether a referenced file is actually there — counting a `.dae` as present when only its
    COOKED `.cdae` is.

    `shapeName` always names the `.dae`, but the engine loads the `.cdae` beside it and never the
    source, which is why `keep_dae: false` can drop 5.4 GB of sources (A8/P7). Vanilla does the
    same: 182 of BeamNG's 6,361 shipped `.cdae` have no `.dae` at all. Checking only the literal
    path called every one of those shapes missing.
    """
    if os.path.isfile(path):
        return True
    stem, ext = os.path.splitext(path)
    return ext.lower() == ".dae" and os.path.isfile(stem + ".cdae")
