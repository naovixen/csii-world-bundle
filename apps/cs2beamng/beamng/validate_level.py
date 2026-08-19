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

from . import dae as dae_mod, ter as ter_mod
from .level import read_objects

GROUND_TOLERANCE = 0.5
GROUND_SAMPLES = 3000
GROUND_PASS_FRACTION = 0.97
GROUND_MIN_SAMPLES = 50          # fewer buildings than this say nothing (the example bundle has 2)


def check(level_dir, bundle_dir, progress=None, settings=None):
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
    seen_dae = set()
    for _rel, _n, obj in objects:
        if obj["class"] == "TSStatic":
            path = resolve(obj.get("shapeName"))
            if path and os.path.isfile(path) and path.lower().endswith(".dae"):
                if path not in seen_dae:
                    seen_dae.add(path)
                    dae_files.append((obj["name"], path))

    # -- the forest (A3) ---------------------------------------------------------------------
    # Every check below has a load behind it: a `type` with no `TSForestItemData` is silently
    # dropped, and a `shapeFile` that does not resolve leaves the type in the item data with
    # nothing to draw. Forest shapes never reach `dae_files` through a TSStatic, so without this
    # they would skip every check above and below.
    item_data = {}
    item_path = os.path.join(level_dir, "art", "forest", "managedItemData.json")
    if os.path.isfile(item_path):
        try:
            with open(item_path, encoding="utf-8") as handle:
                item_data = json.load(handle) or {}
        except ValueError:
            note("art/forest/managedItemData.json: not JSON")
    for name, entry in item_data.items():
        if not isinstance(entry, dict):
            continue
        path = resolve(entry.get("shapeFile"))
        if path is None or not os.path.isfile(path):
            missing.append(f"TSForestItemData {name}: shapeFile {entry.get('shapeFile')}")
        elif path.lower().endswith(".dae") and path not in seen_dae:
            seen_dae.add(path)
            dae_files.append((name, path))
    forest_dir = os.path.join(level_dir, "forest")
    forest_items = 0
    unknown_types = set()
    if os.path.isdir(forest_dir):
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
                        note(f"forest/{name}:{number}: not a JSON object")
                        break
                    if item.get("type") not in item_data:
                        unknown_types.add(item.get("type"))
    checks["forestItems"] = forest_items
    checks["forestTypes"] = len(item_data)
    checks["forestUnknownTypes"] = len(unknown_types)
    for bad in sorted(unknown_types)[:10]:
        note(f"forest item type {bad!r} has no TSForestItemData - BeamNG drops it silently")
    if forest_items and not by_class.get("Forest"):
        note(f"{forest_items} forest items but no Forest object in the level tree")

    # -- budgets -----------------------------------------------------------------------------
    # Calibrated on the vanilla maxima (plan "Budgets"): 663k forest items, 22k TSStatics, 12.7k
    # DecalRoads, 2.6k shapes. Over one of these BeamNG does not fail — it stalls the load or
    # drops frames, which is why they are notes on the build rather than silence.
    over = _budget_notes(by_class, item_data, dae_files, forest_items)
    checks["budgets"] = {name: value for name, value, _limit in over}
    for name, value, limit in over:
        if limit is not None and value > limit:
            note(f"budget: {value:,} {name} exceeds the plan's ~{limit:,} "
                 f"(calibrated on the vanilla levels' own maxima)")
    # full XML parse on a sample (a 10 MB tile takes a second), material-symbol check on all
    for _name, path in dae_files[::max(1, len(dae_files) // 8)][:8]:
        try:
            ET.parse(path)
        except ET.ParseError:
            dae_bad += 1
            note(f"{path}: not well-formed XML")
    checks["daeFiles"] = len(dae_files)

    # -- what the level MOUNTS (audit §64, recalibrated §65) -----------------------------------------------------
    resident = _resident_bytes(level_dir)
    total_mib = (resident["meshes"] + resident["textures"]) / (1024.0 * 1024.0)
    vram_mib, pixels, target_source = _render_target(settings)
    budget_mib = resident_budget_mib(vram_mib, pixels)
    failure_mib = resident_failure_mib(vram_mib, pixels)
    checks["resident"] = {"meshMiB": round(resident["meshes"] / (1024.0 * 1024.0), 1),
                          "textureMiB": round(resident["textures"] / (1024.0 * 1024.0), 1),
                          "totalMiB": round(total_mib, 1),
                          "budgetMiB": round(budget_mib),
                          "failureMiB": round(failure_mib),
                          "budgetVramMiB": vram_mib,
                          "budgetPixels": pixels,
                          "budgetSource": target_source,
                          "vanillaMaxMiB": VANILLA_MAX_RESIDENT_MIB,
                          "vanillaRatio": round(total_mib / VANILLA_MAX_RESIDENT_MIB, 2),
                          "cookedShapes": resident["cookedShapes"], "shapes": resident["shapes"]}
    if total_mib > budget_mib:
        where = ("AT OR OVER the measured failure point" if total_mib >= failure_mib
                 else "in the unmeasured band between what is known to fit and what is known "
                      "to fail")
        note(f"budget: the level mounts {total_mib:,.0f} MiB "
             f"({resident['meshes'] / (1024.0 * 1024.0):,.0f} meshes + "
             f"{resident['textures'] / (1024.0 * 1024.0):,.0f} textures) on a "
             f"{vram_mib:,} MiB card at {pixels / 1.0e6:.2f} Mpixel ({target_source}) - "
             f"{where}. Known to fit: {budget_mib:,.0f} MiB. Known to fail: "
             f"{failure_mib:,.0f} MiB, where D3D12 failed 131,073 texture creates in one "
             f"session. This level is {total_mib / VANILLA_MAX_RESIDENT_MIB:.1f}x the "
             f"{VANILLA_MAX_RESIDENT_MIB:,} MiB of west_coast_usa, the biggest level BeamNG "
             f"ships - see tools/level_budget.py for where the bytes are")

    # -- collisionType must match what the shape actually carries (A4 2.2) --------------------
    # `Collision Mesh` uses the shape's `Colmesh_` node; `Visible Mesh*` rasterises the rendered
    # triangles instead — BeamNG's docs call that "expensive" and "use only when required". A3
    # wrote `Visible Mesh Final` on all 14,658 object statics while writing a Colmesh_ node into
    # every one of them, so both halves of the pairing are worth asserting: an object that has a
    # colmesh and does not use it is the cost we came to remove, and one that asks for a colmesh
    # it does not have collides with NOTHING.
    colmesh_cache = {}
    mismatched_expensive = []
    mismatched_missing = []
    for _rel, _n, obj in objects:
        if obj["class"] != "TSStatic":
            continue
        kind = str(obj.get("collisionType") or "")
        path = resolve(obj.get("shapeName"))
        if not path or not os.path.isfile(path) or not path.lower().endswith(".dae"):
            continue
        if path not in colmesh_cache:
            colmesh_cache[path] = _has_colmesh(path)
        if colmesh_cache[path] and kind.startswith("Visible Mesh"):
            mismatched_expensive.append(obj["name"])
        elif not colmesh_cache[path] and kind == "Collision Mesh":
            mismatched_missing.append(obj["name"])
    checks["collisionTypeMismatch"] = {"visibleMeshWithColmesh": len(mismatched_expensive),
                                       "collisionMeshWithoutColmesh": len(mismatched_missing)}
    if mismatched_expensive:
        note(f"{len(mismatched_expensive):,} TSStatic(s) use a Visible Mesh collider although their shape "
             f"carries a Colmesh_ node (e.g. {', '.join(mismatched_expensive[:3])}) - the expensive path, "
             f"with the cheap one already in the file")
    for bad in mismatched_missing[:10]:
        problems.append(f"TSStatic {bad}: collisionType 'Collision Mesh' but the shape has no Colmesh_ node, "
                        f"so it collides with nothing")
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
    # every material a .dae binds must be a mapTo somewhere in the level (else BeamNG's orange checker);
    # and no name may be defined TWICE — BeamNG mounts every loose *.materials.json under levels/, so a
    # second definition (the headless compiler's auto-generated `diffuseColor 0.8` placeholder beside
    # a shape, load #10) shadows the real one. We write materials under art/cs2/ and art/terrains/
    # only: a materials file under art/shapes/ is never ours.
    mapto = set()
    defined_in = {}                                     # lower-case name -> [relative file, ...]
    stray = []
    mixed_class_files = []
    shapes_root = os.path.join(art, "shapes")
    if os.path.isdir(art):
        for root, _d, files in os.walk(art):
            for name in files:
                if name.endswith(".materials.json"):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, level_dir).replace("\\", "/")
                    if os.path.commonpath([os.path.abspath(full), os.path.abspath(shapes_root)]) == os.path.abspath(shapes_root):
                        stray.append(rel)
                    try:
                        with open(full, encoding="utf-8") as handle:
                            doc = json.load(handle)
                    except ValueError:
                        continue
                    # A plain `Material` must NOT share a file with `TerrainMaterial` entries.
                    # Measured across every shipped vanilla level: art/terrains/*.materials.json
                    # holds 191 TerrainMaterial and ZERO plain Material entries, and not one
                    # vanilla file mixes the classes. We shipped 7 ground mesh materials in there
                    # for three loads; they were DEFINED, so this validator passed, and the cut
                    # meshes still drew as flat untextured grey. Defined is not the same as
                    # applied — that is the blind spot this check closes.
                    classes = {str(m.get("class")) for m in (doc or {}).values() if isinstance(m, dict)}
                    if "TerrainMaterial" in classes and "Material" in classes:
                        mixed_class_files.append(rel)
                    for key, mat in (doc or {}).items():
                        if not isinstance(mat, dict):
                            continue
                        if mat.get("mapTo"):
                            mapto.add(str(mat["mapTo"]).lower())
                        for label in {str(mat.get("name") or key), str(mat.get("mapTo") or "")}:
                            if label:
                                defined_in.setdefault(label.lower(), []).append(rel)
    duplicates = {n: sorted(set(f)) for n, f in defined_in.items() if len(set(f)) > 1}
    checks["strayMaterialFiles"] = len(stray)
    checks["mixedClassMaterialFiles"] = len(mixed_class_files)
    for rel in mixed_class_files[:10]:
        note(f"{rel}: plain 'Material' entries share a file with 'TerrainMaterial' entries - no vanilla "
             "level does this (191 TerrainMaterial / 0 Material across the shipped levels); put the "
             "plain materials in their own art/<dir>/main.materials.json")
    checks["duplicateMaterials"] = len(duplicates)
    for rel in stray[:10]:
        note(f"{rel}: a materials file under art/shapes/ - BeamNG's compiler/importer auto-generated it "
             "(opaque placeholders that shadow the level's materials); the cook stage removes these")
    for n, files in list(duplicates.items())[:10]:
        note(f"material {n!r} is defined in {len(files)} files ({', '.join(files)}): one shadows the other")
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
    for cover in by_class.get("GroundCover", []):
        if str(cover.get("material") or "").lower() not in mapto:
            unbound.setdefault(str(cover.get("material")), cover.get("name"))
    checks["unboundMaterials"] = len(unbound)
    # detail sizes must be positive: BeamNG logs "Shape with invalid detail levels" and draws nothing (load #6)
    bad_detail = 0
    detail_re = re.compile(r'<node id="[^"]*_a(\d+)"')
    for name, path in dae_files:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 262144))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        sizes = [int(v) for v in detail_re.findall(tail)]
        if sizes and min(sizes) <= 0:
            bad_detail += 1
            if bad_detail <= 5:
                note(f"{name}: detail level size 0 (BeamNG: invalid detail levels)")
    checks["invalidDetailShapes"] = bad_detail
    # no geometry over the per-mesh vertex cap: the load #10 junction shards were exactly the tiles
    # whose one geometry passed 65,535 vertices (dae.MAX_GEOMETRY_VERTS, dae.split_mesh)
    oversize = 0
    positions_re = re.compile(rb'<float_array id="[^"]*-positions-array" count="(\d+)"')
    # A geometry over the cap needs > 60,000 x 3 floats of positions text (~1.2 MB at the
    # writer's 4-decimal format), so smaller files cannot hold one and are not read at all;
    # the few large tiles are scanned in 1 MB chunks (with a small overlap for a tag on the
    # boundary) rather than slurped — a full-city level is ~2 GB of COLLADA.
    min_bytes = dae_mod.MAX_GEOMETRY_VERTS * 3 * 6
    for name, path in dae_files:
        try:
            if os.path.getsize(path) < min_bytes:
                continue
            counts = []
            with open(path, "rb") as handle:
                tail = b""
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    buf = tail + chunk
                    counts.extend(int(c) for c in positions_re.findall(buf))
                    tail = buf[-256:]
        except OSError:
            continue
        worst = max((c // 3 for c in counts), default=0)
        if worst > dae_mod.MAX_GEOMETRY_VERTS:
            oversize += 1
            if oversize <= 5:
                note(f"{name}: a geometry with {worst:,} vertices exceeds the {dae_mod.MAX_GEOMETRY_VERTS:,} cap "
                     "(shards in-game: BeamNG's per-mesh index buffers wrap past 65,535)")
    checks["oversizeGeometries"] = oversize
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


COLMESH_TAIL_BYTES = 64 * 1024


def _has_colmesh(path):
    """Whether this `.dae` declares a `Colmesh_` node, from the tail of the file.

    The visual scene is written after every `<geometry>`, so the collision nodes are always in
    the last few hundred bytes; reading the tail keeps this check at a seek per shape instead of
    a full parse of 2.1 GB of COLLADA.
    """
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - COLMESH_TAIL_BYTES))
            return b"Colmesh_" in handle.read()
    except OSError:
        return False


def _budget_notes(by_class, item_data, dae_files, forest_items):
    """`[(name, value, limit_or_None), ...]` — the plan's per-object budgets against this level."""
    return [
        ("forest items", forest_items, None),                 # unbounded in practice
        ("forest types", len(item_data), 5000),
        ("TSStatics", len(by_class.get("TSStatic", [])), 25000),
        ("shape files", len(dae_files), 10000),
        ("lights", len(by_class.get("PointLight", [])) + len(by_class.get("SpotLight", [])), 500),
        ("WaterBlocks", len(by_class.get("WaterBlock", [])), 200),
    ]


# -- what a level may MOUNT ------------------------------------------------------------------
# OURS. BeamNG publishes no budget, so this is derived, and every term is either measured or
# declared. It was a flat 6,144 MiB until A6: the A5 build passed that at 5,224 MiB and then ran
# the card dry anyway — 131,073 failed D3D12 allocations (`hr=0x8007000e`, E_OUTOFMEMORY) in one
# session, starting the moment loading finished and still going 35 minutes later, on textures as
# small as 64x64. A constant was the wrong SHAPE: what a level may mount depends on the card it
# runs on, and on how much of that card the renderer has already spent on render targets, which
# scales with resolution. The old number was not calibrated on anything — it is 4.5x the biggest
# level BeamNG ships.
#
# Two anchors, both real:
#
#   * `tools/level_budget.py --vanilla` measures every level in the install. On BeamNG 0.39 the
#     biggest, `west_coast_usa`, mounts 1,375 MiB (818 MiB of shapes + 624 MiB of textures).
#     That is a LOWER bound — a shipped level also draws on the shared `content/` archives, which
#     ours does not — so a budget derived from it is conservative in vanilla's favour, not
#     optimistic. A5 shipped 3.8x it.
#   * BeamNG's own DirectX 12 floor, printed at startup: "7.83 GB VRAM meets the 5.50 GB
#     minimum". The engine expects its own levels to run on a 5,632 MiB card.
#
# Together: on a card at the engine's minimum, about 1,375 MiB of level content fits beside
# everything else the renderer needs. Every MiB of card above that minimum is a MiB more content
# it can hold; every pixel above the 1080p that budget is implicitly quoted at takes some back
# for the deferred G-buffer and the post chain.
VANILLA_MAX_RESIDENT_MIB = 1375        # west_coast_usa, measured 2026-08-18 (BeamNG 0.39)
BEAMNG_DX12_MIN_VRAM_MIB = 5632        # the engine's own printed "5.50 GB minimum"
REFERENCE_PIXELS = 1920 * 1080         # the resolution the vanilla anchor is quoted at

# DECLARED APPROXIMATION — this one term is an estimate, not a measurement. BeamNG's deferred
# targets are named in `lua/ge/client` (`deferred`, `matinfo`, `lightinfo`, `ssao`, velocity,
# backBuffer, the post chain's own) but their FORMATS are set in the native renderer and are not
# readable from the install, so their exact bytes per pixel cannot be read off disk. 100 MiB per
# megapixel is roughly a dozen full-resolution targets averaging four bytes a pixel plus the
# half-resolution chains. It is second order here and deliberately so: it moves the budget by
# 288 MiB across the whole span from 1080p to 3440x1440, so being wrong by a factor of two moves
# it less than the texture cap does. If it ever stops being second order, measure it.
RENDER_TARGET_MIB_PER_MPIXEL = 100.0

# The reference machine, used when the caller does not say. Not a recommendation — it is this
# project's own test card (RTX 3070, 8 GB) at the resolution the vanilla anchor is quoted at.
REFERENCE_VRAM_MIB = 8018


def resident_budget_mib(vram_mib=REFERENCE_VRAM_MIB, pixels=REFERENCE_PIXELS):
    """What a level is KNOWN to be able to mount on a `vram_mib` card at `pixels` pixels, in MiB.

    This is a LOWER bound on the true ceiling, and deliberately so. `west_coast_usa` mounting
    1,375 MiB on a card at the engine's minimum proves that at least 1,375 MiB fits there — not
    that 1,375 MiB is all that fits. BeamNG does not size its levels to the limit of its own
    minimum spec, so the real ceiling is somewhere above what this returns.

    The other end is known too, and `resident_failure_mib` is it: 5,224 MiB on an 8,018 MiB card
    at 3440x1440 failed hard. So for that machine the true ceiling is somewhere in
    [3,473, 5,224) MiB, and the only way to narrow it is a load that reports zero failed
    allocations. Build under this and it fits; between this and the failure line it is unknown.
    """
    headroom = max(0.0, float(vram_mib) - BEAMNG_DX12_MIN_VRAM_MIB)
    extra_mpixels = (float(pixels) - REFERENCE_PIXELS) / 1.0e6
    budget = VANILLA_MAX_RESIDENT_MIB + headroom - RENDER_TARGET_MIB_PER_MPIXEL * extra_mpixels
    # a card below the engine's own minimum still has to hold SOMETHING; the floor is well under
    # vanilla's median rather than zero, and the note that follows will be loud either way
    return max(384.0, budget)


# The other end of the bracket: what is KNOWN to fail, scaled the same way. A5's `cs2_city_fine`
# mounted 5,224 MiB on an 8,018 MiB card at 3440x1440 and D3D12 failed 131,073 texture creates in
# one session. Anything at or above this on an equivalent machine is not a risk, it is the
# measured failure. MEASURED, one point — narrow it by loading and reading `totalFailures`.
FAILURE_RESIDENT_MIB = 5224
FAILURE_VRAM_MIB = 8018
FAILURE_PIXELS = 3440 * 1440


def resident_failure_mib(vram_mib=REFERENCE_VRAM_MIB, pixels=REFERENCE_PIXELS):
    """The measured failure point, moved to another card and resolution on the same terms."""
    delta_vram = float(vram_mib) - FAILURE_VRAM_MIB
    delta_mpixels = (float(pixels) - FAILURE_PIXELS) / 1.0e6
    return max(resident_budget_mib(vram_mib, pixels) + 1.0,
               FAILURE_RESIDENT_MIB + delta_vram - RENDER_TARGET_MIB_PER_MPIXEL * delta_mpixels)


def _render_target(settings):
    """`(vram_mib, pixels, source)` — the machine the budget is derived for.

    Settings win when set; otherwise the numbers are READ, not assumed. BeamNG prints both at
    startup, in the logs the user's own sessions leave behind:

        Dedicated video memory: 8018 MB
        AntialiasLevel: 0, ... Resolution: 3440x1440, wideScreen: false

    which is the difference between a budget for this machine and a budget for a machine we
    imagined. Both are MiB despite the "MB" — the same line's byte count, 8407482368, is 8018
    times 1024 squared exactly.

    Reads the ROTATED logs too, newest first, and takes each number from the newest log that
    actually carries it. A session that started and quit without opening a window prints the
    VRAM and never prints a resolution — that happened on 2026-08-19 07:49 and the budget
    silently fell back to 1080p while still claiming it had read the log, which is worse than
    not reading it. `source` now names each half separately, so a fallback says so.
    """
    settings = settings or {}
    vram = int(settings.get("gpu_vram_mib") or 0)
    width = int(settings.get("render_width") or 0)
    height = int(settings.get("render_height") or 0)
    pixels = width * height
    if vram and pixels:
        return vram, pixels, "from settings"

    log_vram = log_pixels = None
    for path in _beamng_logs():
        if log_vram is not None and log_pixels is not None:
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if log_vram is None:
                        found = re.search(r"Dedicated video memory:\s*(\d+)\s*MB", line)
                        if found:
                            log_vram = int(found.group(1))
                    if log_pixels is None:
                        found = re.search(r"Resolution:\s*(\d+)x(\d+)", line)
                        if found:
                            log_pixels = int(found.group(1)) * int(found.group(2))
                    if log_vram is not None and log_pixels is not None:
                        break
        except OSError:
            continue

    parts = []
    if vram:
        parts.append("card from settings")
    elif log_vram:
        vram, _ = log_vram, parts.append("card from beamng.log")
    else:
        vram, _ = REFERENCE_VRAM_MIB, parts.append("REFERENCE card, no beamng.log")
    if pixels:
        parts.append("resolution from settings")
    elif log_pixels:
        pixels, _ = log_pixels, parts.append("resolution from beamng.log")
    else:
        pixels, _ = REFERENCE_PIXELS, parts.append("REFERENCE 1080p, no resolution logged")
    return vram, pixels, ", ".join(parts)


def _beamng_logs():
    """Every `beamng*.log` under the user's BeamNG folder, newest first.

    BeamNG rotates them (`beamng.log`, `beamng.1.log`, …), and the newest is not always the most
    informative — a session that quit before opening a window logs almost nothing.
    """
    base = os.path.join(os.path.expanduser("~"), "AppData", "Local", "BeamNG", "BeamNG.drive")
    found = []
    if not os.path.isdir(base):
        return found
    for entry in os.listdir(base):
        folder = os.path.join(base, entry)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if re.match(r"^beamng(\.\d+)?\.log$", name):
                path = os.path.join(folder, name)
                try:
                    found.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    return [path for _mtime, path in sorted(found, reverse=True)]


def _resident_bytes(level_dir):
    """`{"meshes": bytes, "textures": bytes, "cookedShapes": n, "shapes": n}` — a stat walk.

    What BeamNG mounts, not what is on screen: a shape is loaded whole (every detail level and its
    collision node) and cooked textures sit in VRAM. `.cdae` when the cook has run, because that
    is the file the game reads; the `.dae` beside it is the source and is not loaded. The
    triangles-drawn-at-once half of the budget needs each shape's ladder and lives in
    `tools/level_budget.py`, which is a minute's work on a full city and does not belong in a
    stage that runs every build.
    """
    meshes = textures = 0
    shapes = cooked = 0
    for root, _dirs, files in os.walk(os.path.join(level_dir, "art")):
        names = set(files)
        for name in files:
            path = os.path.join(root, name)
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if ext == ".dae":
                shapes += 1
                if stem + ".cdae" not in names:
                    meshes += size            # uncooked: the XML is what gets parsed and kept
            elif ext == ".cdae":
                cooked += 1
                meshes += size
            elif ext in (".dds", ".png", ".jpg", ".dxt"):
                textures += size
    return {"meshes": meshes, "textures": textures, "shapes": shapes, "cookedShapes": cooked}


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
