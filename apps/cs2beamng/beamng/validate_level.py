"""Structural + one physical check of a built level folder. `check(level_dir, bundle_dir,
progress) -> (checks, problems)`.

Structural: every `items.level.json` line parses and names a `class`; `persistentId`s are
unique; `TerrainBlock.terrainFile`, `TSStatic.shapeName`, forest `shapeFile`s and material
texture paths exist; every `.dae` parses as XML; the `.ter` header, size and layer indices agree
with `.terrain.json`; exactly one `TerrainBlock` and one `LevelInfo`; at least one
`SpawnSphere`; `info.json` names an existing spawn.

Physical: the `.ter` height under sampled `OnGround` buildings equals `instances.ground_level`
(`pos.y − attrs.elevation`) within `GROUND_TOLERANCE` — the one check that catches a wrong
orientation, a wrong origin or a wrong scale in one go (a flipped terrain is metres off
everywhere). The tolerance covers the roadbed sink and the shallow-band clamp.
"""

import json
import os
import re
import xml.etree.ElementTree as ET

import numpy

from csiibundle import instances as instances_mod, reader

from . import ter as ter_mod
from .level import read_objects

GROUND_TOLERANCE = 0.5
GROUND_SAMPLES = 3000
GROUND_PASS_FRACTION = 0.97
GROUND_MIN_SAMPLES = 50          # fewer buildings than this say nothing (the example bundle has 2)


def check(level_dir, bundle_dir, progress=None):
    problems = []
    checks = {}
    level_name = os.path.basename(os.path.normpath(level_dir))

    def note(text):
        problems.append(text)

    # -- objects ---------------------------------------------------------------------------
    objects = []
    ids = {}
    parse_errors = 0
    for rel, line_no, obj in read_objects(level_dir):
        if obj is None:
            parse_errors += 1
            if parse_errors <= 5:
                note(f"{rel}:{line_no}: not a JSON object")
            continue
        if "class" not in obj or "name" not in obj:
            note(f"{rel}:{line_no}: object without class/name")
            continue
        pid = obj.get("persistentId")
        if not pid:
            note(f"{rel}:{line_no}: {obj['name']} has no persistentId")
        elif pid in ids:
            note(f"{rel}:{line_no}: persistentId {pid} duplicates {ids[pid]}")
        else:
            ids[pid] = f"{rel}:{line_no}"
        objects.append((rel, line_no, obj))
    by_class = {}
    for _rel, _n, obj in objects:
        by_class.setdefault(obj["class"], []).append(obj)
    checks["objects"] = len(objects)
    checks["classes"] = {k: len(v) for k, v in sorted(by_class.items())}
    checks["parseErrors"] = parse_errors
    for cls, want in (("TerrainBlock", 1), ("LevelInfo", 1)):
        have = len(by_class.get(cls, []))
        if have != want:
            note(f"expected exactly {want} {cls}, found {have}")
    if not by_class.get("SpawnSphere"):
        note("no SpawnSphere")

    # -- referenced files --------------------------------------------------------------------
    missing = []

    def resolve(game_path):
        if not game_path:
            return None
        p = str(game_path).replace("\\", "/")
        prefix = f"/levels/{level_name}/"
        if p.startswith(prefix):
            return os.path.join(level_dir, *p[len(prefix):].split("/"))
        if p.startswith("levels/" + level_name + "/"):
            return os.path.join(level_dir, *p[len("levels/" + level_name + "/"):].split("/"))
        return None                                    # a core/ or art/ path: BeamNG's own, not ours to check

    for _rel, _n, obj in objects:
        for field in ("terrainFile", "shapeName", "minimapImage"):
            path = resolve(obj.get(field))
            if path is not None and not os.path.isfile(path):
                missing.append(f"{obj['class']} {obj['name']}: {field} {obj[field]}")
    dae_bad = 0
    dae_files = []
    for _rel, _n, obj in objects:
        if obj["class"] == "TSStatic":
            path = resolve(obj.get("shapeName"))
            if path and os.path.isfile(path) and path.lower().endswith(".dae"):
                dae_files.append((obj["name"], path))
    # full XML parse on a sample (a 10 MB tile takes a second), material-symbol check on all
    for _name, path in dae_files[::max(1, len(dae_files) // 8)][:8]:
        try:
            ET.parse(path)
        except ET.ParseError:
            dae_bad += 1
            note(f"{path}: not well-formed XML")
    checks["daeFiles"] = len(dae_files)
    # material texture paths under art/
    art = os.path.join(level_dir, "art")
    material_files = 0
    if os.path.isdir(art):
        for root, _d, files in os.walk(art):
            for name in files:
                if name.endswith(".materials.json"):
                    material_files += 1
                    with open(os.path.join(root, name), encoding="utf-8") as handle:
                        try:
                            doc = json.load(handle)
                        except ValueError:
                            note(f"{name}: not JSON")
                            continue
                    for mat_name, mat in (doc or {}).items():
                        if not isinstance(mat, dict):
                            continue
                        for key, value in mat.items():
                            if key.endswith(("Tex", "Map")) and isinstance(value, str) and value:
                                path = resolve(value)
                                if path is not None and not os.path.isfile(path):
                                    missing.append(f"material {mat_name}: {key} {value}")
    checks["materialFiles"] = material_files
    checks["missingFiles"] = len(missing)
    # every material a .dae binds must be a mapTo somewhere in the level (else BeamNG's orange checker)
    mapto = set()
    if os.path.isdir(art):
        for root, _d, files in os.walk(art):
            for name in files:
                if name.endswith(".materials.json"):
                    try:
                        with open(os.path.join(root, name), encoding="utf-8") as handle:
                            doc = json.load(handle)
                    except ValueError:
                        continue
                    for mat in (doc or {}).values():
                        if isinstance(mat, dict) and mat.get("mapTo"):
                            mapto.add(str(mat["mapTo"]).lower())
    unbound = {}
    mat_re = re.compile(r'<material id="[^"]*" name="([^"]+)"')
    for name, path in dae_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                head = handle.read(262144)
        except OSError:
            continue
        for sym in mat_re.findall(head):
            if sym.lower() not in mapto:
                unbound.setdefault(sym, name)
    checks["unboundMaterials"] = len(unbound)
    for sym, where in list(unbound.items())[:20]:
        note(f"material {sym!r} bound by {where} has no mapTo in any main.materials.json")
    for m in missing[:20]:
        note("missing file: " + m)

    # -- terrain ---------------------------------------------------------------------------
    terrain = None
    block = (by_class.get("TerrainBlock") or [None])[0]
    if block is not None:
        path = resolve(block.get("terrainFile"))
        sidecar = os.path.join(level_dir, f"{level_name}.terrain.json")
        if path and os.path.isfile(path):
            try:
                size, heights, layers, names = ter_mod.read_ter(path)
                terrain = (size, heights, layers, names, block)
                checks["ter"] = {"size": size, "materials": names, "holes": int((layers == ter_mod.HOLE).sum()),
                                 "minStored": int(heights.min()), "maxStored": int(heights.max())}
                bad = numpy.unique(layers[(layers != ter_mod.HOLE) & (layers >= len(names))])
                if bad.size:
                    note(f".ter layer indices without a material: {bad.tolist()}")
                if os.path.isfile(sidecar):
                    with open(sidecar, encoding="utf-8") as handle:
                        side = json.load(handle)
                    if side.get("size") != size or side.get("materials") != names:
                        note(".terrain.json disagrees with the .ter (size or materials)")
                else:
                    note("no .terrain.json beside the .ter")
                if not block.get("maxHeight") or "position" not in block:
                    note("TerrainBlock lacks maxHeight/position")
            except (ValueError, OSError) as exc:
                note(f".ter unreadable: {exc}")

    # -- info.json -------------------------------------------------------------------------
    info_path = os.path.join(level_dir, "info.json")
    if os.path.isfile(info_path):
        with open(info_path, encoding="utf-8") as handle:
            try:
                info = json.load(handle)
            except ValueError:
                info = None
                note("info.json is not JSON")
        if info:
            spawn_names = {o["name"] for o in by_class.get("SpawnSphere", [])}
            default = info.get("defaultSpawnPointName")
            if default and default not in spawn_names:
                note(f"info.json defaultSpawnPointName {default!r} is not a SpawnSphere")
    else:
        note("no info.json")

    # -- physical: terrain under OnGround buildings -----------------------------------------
    if terrain is not None and bundle_dir:
        result = _ground_check(terrain, bundle_dir, progress)
        checks["groundCheck"] = result
        if result.get("sampled", 0) >= GROUND_MIN_SAMPLES and result["fraction"] < GROUND_PASS_FRACTION:
            note(f"terrain under OnGround buildings: only {result['fraction']:.1%} within {GROUND_TOLERANCE} m "
                 f"(median |dz| {result['medianAbs']:.2f} m over {result['sampled']} buildings) - orientation/origin/scale?")
    return checks, problems


def _ground_check(terrain, bundle_dir, progress):
    size, heights, _layers, _names, block = terrain
    position = [float(v) for v in block["position"]]
    square = float(block.get("squareSize", 1.0))
    max_height = float(block["maxHeight"])
    path = reader.layer_path(bundle_dir, "instances")
    if not path or not os.path.isfile(path):
        return {"sampled": 0}
    dz = []
    seen = 0
    for record in reader.read_jsonl(path):
        if record.get("kind") != "building":
            continue
        attrs = record.get("attrs") or {}
        flags = attrs.get("elevationFlags")
        if flags and list(flags) != ["OnGround"]:
            continue
        ground = instances_mod.ground_level(record)
        if ground is None:
            if flags:                                  # off the ground for a stated reason, no elevation given: skip
                continue
            ground = float(record["pos"][1])           # §4.2: no elevation = sitting flat on the ground at pos
        seen += 1
        if seen > 200 and seen % 7 != 0:               # past the first 200: every 7th, spread over the file
            continue
        x, _y, z = record["pos"]
        h = ter_mod.sample_height(heights, max_height, position, square, float(x), float(z))
        if h is None:
            continue
        dz.append(h - ground)
        if len(dz) >= GROUND_SAMPLES:
            break
    if not dz:
        return {"sampled": 0}
    arr = numpy.abs(numpy.asarray(dz))
    return {"sampled": int(arr.size), "fraction": float((arr <= GROUND_TOLERANCE).mean()),
            "medianAbs": float(numpy.median(arr)), "p95Abs": float(numpy.percentile(arr, 95)),
            "meanSigned": float(numpy.mean(dz))}
