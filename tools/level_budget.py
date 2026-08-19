"""What a built BeamNG level costs, against what BeamNG's own levels cost.

    python tools/level_budget.py <level_dir> [--view 4000] [--spawn X,Y] [--json out.json]
    python tools/level_budget.py <level_dir> --vanilla <BeamNG install>   # re-measure the maxima

Two numbers decide whether a level runs, and neither is visible in a build report:

* **Resident bytes** — every mesh and texture the level mounts, whether or not it is on screen.
  BeamNG loads a shape whole (all detail levels, plus the collision node) and keeps cooked
  textures in VRAM. On an 8 GB card a working set over about 6 GiB pages continuously and never
  recovers, which is exactly the "much worse after alt-tabbing" symptom: alt-tab makes the driver
  release its allocations and the level cannot get them all back.
* **Triangles drawn at once** — for each object inside the view distance, the triangle count of
  the detail level that actually applies at ITS distance, not the shape's full count. A ladder
  whose levels all keep the same geometry (see `beamng/tilelod.py`) reads as a ladder in the
  build report and as no ladder at all here.

Both are measured from the level on disk, so this works on a level built by any version of the
app and needs nothing the build kept in memory. `.dae` files are scanned for their per-detail
triangle counts (the node names carry the detail size: `<name>_a<px>`); residency prefers the
`.cdae` beside a `.dae` when the cook has run, because that is what the game actually loads.

The comparison column is `west_coast_usa` and its neighbours, read out of the game's own
`content/levels/*.zip`. They are the only calibration that means anything: a budget invented from
first principles is a guess, and these are levels that demonstrably run.
"""

import argparse
import json
import math
import os
import re
import sys
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps"))

MIB = 1024.0 * 1024.0

# Measured from the shipped levels (2026-08-18, BeamNG 0.39) with `--vanilla`. `west_coast_usa` is
# the big open-world one and the fair comparison for a city; the maxima are over every level in
# `content/levels/`. THEIRS, not ours — re-measure rather than edit.
VANILLA = {
    "west_coast_usa": {"shapeFiles": 2608, "tsStatics": 22067, "forestTypes": 226,
                       "forestItems": 133375, "materials": 586, "visibleDistance": 15667},
    "italy": {"shapeFiles": 1419, "tsStatics": 13971, "forestTypes": 208,
              "forestItems": 206152, "materials": 171, "visibleDistance": 25000},
    "johnson_valley": {"shapeFiles": 498, "tsStatics": 15559, "forestTypes": 78,
                       "forestItems": 663057, "materials": 171, "visibleDistance": 24500},
}

# A `.dae`'s detail nodes: `<name>_a<px>`, plus the two special ones. `Colmesh_` is collision and
# is never drawn; `nulldetail`/`bb_autobillboard` carry no geometry of their own.
NODE_RE = re.compile(rb'<node id="([^"]+)" name="[^"]*" type="NODE">')
GEOM_RE = re.compile(rb'<geometry id="([^"]+)"')
TRIS_RE = re.compile(rb'<triangles [^>]*count="(\d+)"')
INSTANCE_RE = re.compile(rb'<instance_geometry url="#([^"]+)"')
DETAIL_RE = re.compile(r"_a(\d+)$")


def scan_dae(path):
    """`{detail_px: triangles}` for one `.dae`, plus `collisionTriangles`.

    One pass over the bytes. The geometries come first and each `<geometry>` owns every
    `<triangles count=>` until the next one; the scene nodes come last and each names the geometry
    it instances. A geometry instanced by two nodes is counted in both, which is what the renderer
    would do.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    scene = data.find(b"<library_visual_scenes>")
    if scene < 0:
        return {}, 0
    geometries = {}
    bounds = [(m.group(1).decode("ascii", "replace"), m.start()) for m in GEOM_RE.finditer(data, 0, scene)]
    edges = [b[1] for b in bounds] + [scene]
    for index, (gid, _start) in enumerate(bounds):
        total = 0
        for m in TRIS_RE.finditer(data, edges[index], edges[index + 1]):
            total += int(m.group(1))
        geometries[gid] = total
    levels = defaultdict(int)
    collision = 0
    nodes = list(NODE_RE.finditer(data, scene))
    for index, node in enumerate(nodes):
        name = node.group(1).decode("ascii", "replace")
        end = nodes[index + 1].start() if index + 1 < len(nodes) else len(data)
        tris = 0
        for inst in INSTANCE_RE.finditer(data, node.end(), end):
            tris += geometries.get(inst.group(1).decode("ascii", "replace"), 0)
        if not tris:
            continue
        if name.startswith("Colmesh_"):
            collision += tris
            continue
        match = DETAIL_RE.search(name)
        if match:
            levels[int(match.group(1))] += tris
    return dict(levels), collision


def detail_at(levels, radius, distance, screen_height=1080.0, fov_degrees=45.0):
    """Which detail level a shape of `radius` uses at `distance`, as its triangle count.

    Torque compares the shape's projected pixel size against each detail's size and takes the
    first whose size is at or below it (details descending), so this is the same walk the renderer
    does. `None` when every detail is above the projected size — the shape is not drawn.
    """
    if not levels:
        return 0
    px = screen_px(radius, distance, screen_height, fov_degrees)
    for size in sorted(levels, reverse=True):
        if size <= px:
            return levels[size]
    return 0


def screen_px(radius, distance, screen_height=1080.0, fov_degrees=45.0):
    """A sphere of `radius` at `distance`, in pixels of a `screen_height` viewport."""
    if distance <= 0.0:
        return float("inf")
    half = math.tan(math.radians(fov_degrees) * 0.5)
    return float(radius / (distance * half) * (screen_height * 0.5))


def file_bytes(path):
    """What the game loads for this shape: the `.cdae` when the cook has run, else the `.dae`."""
    cooked = os.path.splitext(path)[0] + ".cdae"
    if os.path.isfile(cooked):
        return os.path.getsize(cooked), True
    try:
        return os.path.getsize(path), False
    except OSError:
        return 0, False


def group_of(rel):
    """`art/shapes/<group>/…` — the product a shape belongs to, for the per-group table."""
    parts = rel.replace("\\", "/").split("/")
    if "shapes" in parts:
        index = parts.index("shapes")
        if index + 1 < len(parts) - 1:
            return parts[index + 1]
    return "other"


def measure(level_dir, view_distance, spawn=None, screen_height=1080.0):
    from cs2beamng.beamng.level import read_objects

    level_dir = os.path.abspath(level_dir)
    name = os.path.basename(level_dir)

    # -- shapes: bytes and the detail ladder -----------------------------------------------
    shapes = {}
    shapes_root = os.path.join(level_dir, "art", "shapes")
    for root, _dirs, files in os.walk(shapes_root):
        for file_name in files:
            if not file_name.endswith(".dae"):
                continue
            full = os.path.join(root, file_name)
            rel = os.path.relpath(full, level_dir).replace("\\", "/")
            size, cooked = file_bytes(full)
            levels, collision = scan_dae(full)
            shapes[rel] = {"bytes": size, "cooked": cooked, "levels": levels,
                           "collision": collision, "group": group_of(rel)}

    # -- objects ---------------------------------------------------------------------------
    statics = []
    by_class = defaultdict(int)
    spawn_xy = spawn
    for _rel, _line, obj in read_objects(level_dir):
        if not obj or "class" not in obj:
            continue
        by_class[obj["class"]] += 1
        if obj["class"] == "SpawnSphere" and spawn_xy is None:
            position = obj.get("position") or [0, 0, 0]
            spawn_xy = (float(position[0]), float(position[1]))
        if obj["class"] != "TSStatic":
            continue
        shape_name = str(obj.get("shapeName") or "")
        rel = shape_name.split(f"/levels/{name}/", 1)[-1] if f"/levels/{name}/" in shape_name else None
        position = obj.get("position") or [0, 0, 0]
        statics.append((rel, float(position[0]), float(position[1])))
    if spawn_xy is None:
        spawn_xy = (0.0, 0.0)

    # -- triangles in view ------------------------------------------------------------------
    # A tile's radius is not in the level file; it is the shape's own bounding sphere, and the
    # only cheap stand-in on disk is the tile size. Rather than guess, the radius comes from the
    # shape's own vertex extent when the .dae is small enough to trust and from the tile grid
    # otherwise — see `radius_hint`.
    in_view = defaultdict(int)
    drawn = defaultdict(int)
    for rel, x, y in statics:
        shape = shapes.get(rel)
        if shape is None:
            continue
        distance = math.hypot(x - spawn_xy[0], y - spawn_xy[1])
        if distance > view_distance:
            continue
        radius = radius_hint(rel, shape)
        in_view[shape["group"]] += detail_at(shape["levels"], radius, max(distance, 1.0),
                                             screen_height)
        drawn[shape["group"]] += 1

    # -- forest -----------------------------------------------------------------------------
    forest_items = 0
    forest_types = 0
    forest_dir = os.path.join(level_dir, "forest")
    if os.path.isdir(forest_dir):
        for file_name in os.listdir(forest_dir):
            if not file_name.endswith(".forest4.json"):
                continue
            with open(os.path.join(forest_dir, file_name), "rb") as handle:
                forest_items += count_entries(handle.read())
    for root, _dirs, files in os.walk(os.path.join(level_dir, "art")):
        for file_name in files:
            if file_name.lower().endswith(("forestitemdata.json", "manageditemdata.json")):
                with open(os.path.join(root, file_name), "rb") as handle:
                    forest_types += count_entries(handle.read())

    # -- textures ----------------------------------------------------------------------------
    texture_bytes = defaultdict(int)
    for root, _dirs, files in os.walk(os.path.join(level_dir, "art")):
        for file_name in files:
            ext = os.path.splitext(file_name)[1].lower()
            if ext in (".dds", ".png", ".jpg"):
                texture_bytes[ext] += os.path.getsize(os.path.join(root, file_name))

    # -- materials ----------------------------------------------------------------------------
    materials = 0
    for root, _dirs, files in os.walk(os.path.join(level_dir, "art")):
        for file_name in files:
            if file_name.endswith(".materials.json"):
                with open(os.path.join(root, file_name), "rb") as handle:
                    materials += count_entries(handle.read())

    mesh_bytes = defaultdict(int)
    for shape in shapes.values():
        mesh_bytes[shape["group"]] += shape["bytes"]

    return {
        "level": name,
        "viewDistance": view_distance,
        "spawn": list(spawn_xy),
        "meshBytes": dict(mesh_bytes),
        "meshBytesTotal": sum(mesh_bytes.values()),
        # WHERE the mesh bytes are, not just how many. A shape is mounted WHOLE the moment one
        # instance of it is in view — every LOD and its collision mesh — so the cost is carried by
        # the shape LIST, not the instance count, and it is never evenly spread. On the reference
        # city the heaviest 100 of 4,289 shapes are half of all object mesh bytes and the biggest
        # single one is 26 MiB. Nothing in this report said that until 2026-08-19, so "the level
        # is too big" had no next question (audit §83).
        "heaviestShapes": sorted(((s["bytes"], name) for name, s in shapes.items()),
                                 reverse=True)[:20],
        "shapeBytesTop100": sum(sorted((s["bytes"] for s in shapes.values()), reverse=True)[:100]),
        "textureBytes": dict(texture_bytes),
        "textureBytesTotal": sum(texture_bytes.values()),
        "residentTotal": sum(mesh_bytes.values()) + sum(texture_bytes.values()),
        "trianglesInView": dict(in_view),
        "trianglesInViewTotal": sum(in_view.values()),
        "staticsInView": dict(drawn),
        "shapeFiles": len(shapes),
        "cooked": sum(1 for s in shapes.values() if s["cooked"]),
        "tsStatics": by_class.get("TSStatic", 0),
        "forestItems": forest_items,
        "forestTypes": forest_types,
        "materials": materials,
        "collisionTriangles": sum(s["collision"] for s in shapes.values()),
    }


def count_entries(raw):
    """How many objects a BeamNG side-table holds, whichever way it is written.

    `managedItemData.json` and `main.materials.json` are pretty-printed JSON DICTS in the shipped
    levels and JSON LINES in some mod levels; counting lines reads west_coast_usa's 226 forest
    types as 2,663. Parse first, count lines only when parsing fails.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        value = json.loads(text)
    except ValueError:
        return sum(1 for line in text.splitlines() if line.strip())
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 1


def radius_hint(rel, shape, tile=256.0):
    """The bounding-sphere radius a Torque detail size is measured against.

    A merged world tile is a `<product>_<tx>_<tz>` shape covering one tile of the grid, so its
    radius is the tile's own half-diagonal — the same quantity `beamng/tilelod.radius_of`
    computed when the ladder was built. An object shape is small and its exact radius does not
    change which detail it picks inside a view distance, so it takes a nominal 2 m. DECLARED
    APPROXIMATION: this is the one input here that is not read off the file.
    """
    base = os.path.basename(rel)
    if re.match(r"^(net|markings|areas|skirts|lanegeometry|terraincut)_-?\d+_-?\d+", base):
        return tile * math.sqrt(2.0) * 0.5
    return 2.0


def measure_vanilla(install_dir):
    """Re-read the shipped levels' own counts out of `content/levels/*.zip`.

    Includes what each level MOUNTS — mesh and texture bytes, the same accounting `measure` runs
    over a built level, so `RESIDENT_BUDGET_MIB` can be set against levels that demonstrably run
    rather than against a number someone chose. A shape counts once: the `.cdae` when there is
    one, the `.dae` only when there is no cooked file beside it, exactly as `file_bytes` does.

    DECLARED APPROXIMATION — this is a LOWER BOUND on what a vanilla level mounts. A shipped level
    also draws shapes and textures from the shared `content/` archives (common props, vegetation,
    the sky), and those bytes are resident too but are not in the level's own zip. Ours ships
    every byte it uses inside the level, so the comparison is unfair in vanilla's favour: the real
    vanilla figure is higher than this by however much it borrows, which makes a budget derived
    from it conservative rather than optimistic.
    """
    out = {}
    levels_dir = os.path.join(install_dir, "content", "levels")
    for file_name in sorted(os.listdir(levels_dir)):
        if not file_name.endswith(".zip"):
            continue
        path = os.path.join(levels_dir, file_name)
        name = os.path.splitext(file_name)[0]
        row = {"shapeFiles": 0, "tsStatics": 0, "forestTypes": 0, "forestItems": 0,
               "materials": 0, "visibleDistance": None,
               "meshBytes": 0, "textureBytes": 0, "residentTotal": 0}
        try:
            with zipfile.ZipFile(path) as archive:
                # a `.dae` whose `.cdae` is also in the archive is the source file and is not
                # loaded; collect the cooked stems first so the second pass can skip it
                cooked_stems = {os.path.splitext(i.filename.lower())[0]
                                for i in archive.infolist() if i.filename.lower().endswith(".cdae")}
                for info in archive.infolist():
                    lower = info.filename.lower()
                    stem, ext = os.path.splitext(lower)
                    if ext == ".cdae" or (ext == ".dae" and stem not in cooked_stems):
                        row["meshBytes"] += info.file_size
                    elif ext in (".dds", ".png", ".jpg"):
                        row["textureBytes"] += info.file_size
                    if lower.endswith(".dae") or lower.endswith(".cdae"):
                        row["shapeFiles"] += 1
                    elif lower.endswith("items.level.json"):
                        for line in archive.read(info).decode("utf-8", "replace").splitlines():
                            if '"TSStatic"' in line:
                                row["tsStatics"] += 1
                            if '"visibleDistance"' in line and row["visibleDistance"] is None:
                                try:
                                    row["visibleDistance"] = json.loads(line).get("visibleDistance")
                                except ValueError:
                                    pass
                    elif lower.endswith(".forest4.json"):
                        row["forestItems"] += count_entries(archive.read(info))
                    elif lower.endswith(("forestitemdata.json", "manageditemdata.json")):
                        row["forestTypes"] += count_entries(archive.read(info))
                    elif lower.endswith(".materials.json"):
                        row["materials"] += count_entries(archive.read(info))
        except (zipfile.BadZipFile, OSError):
            continue
        row["residentTotal"] = row["meshBytes"] + row["textureBytes"]
        out[name] = row
    return out


def report(result, compare=None):
    compare = compare or VANILLA
    lines = []
    add = lines.append
    add(f"level {result['level']} — {result['viewDistance']:.0f} m view from "
        f"({result['spawn'][0]:.0f}, {result['spawn'][1]:.0f})")
    add("")
    add("  resident                    MiB")
    for group, value in sorted(result["meshBytes"].items(), key=lambda kv: -kv[1]):
        add(f"    meshes {group:<20} {value / MIB:8.0f}")
    add(f"    meshes {'TOTAL':<20} {result['meshBytesTotal'] / MIB:8.0f}")
    for ext, value in sorted(result["textureBytes"].items(), key=lambda kv: -kv[1]):
        add(f"    textures {ext:<18} {value / MIB:8.0f}")
    add(f"    {'RESIDENT TOTAL':<27}{result['residentTotal'] / MIB:8.0f}")
    add("")
    top100 = result.get("shapeBytesTop100") or 0
    if top100:
        share = 100.0 * top100 / max(1, result["meshBytesTotal"])
        add(f"  the heaviest shapes — a shape is mounted WHOLE for one instance in view")
        add(f"    top 100 of {result['shapeFiles']:,} shape files{top100 / MIB:16.0f} MiB "
            f"({share:.0f} % of all mesh bytes)")
        for value, name in (result.get("heaviestShapes") or [])[:8]:
            add(f"    {name[:44]:<44}{value / MIB:8.1f} MiB")
        add("")
    add("  triangles inside the view distance")
    for group, value in sorted(result["trianglesInView"].items(), key=lambda kv: -kv[1]):
        add(f"    {group:<27}{value:>12,}  ({result['staticsInView'].get(group, 0)} statics)")
    add(f"    {'TOTAL':<27}{result['trianglesInViewTotal']:>12,}")
    add("")
    reference = compare.get("west_coast_usa") or {}
    add(f"  counts                        this   west_coast_usa")
    for key, label in (("shapeFiles", "shape files"), ("tsStatics", "TSStatics"),
                       ("forestTypes", "forest types"), ("forestItems", "forest items"),
                       ("materials", "materials")):
        theirs = reference.get(key)
        ratio = f"{result[key] / theirs:5.1f}x" if theirs else "    —"
        add(f"    {label:<22}{result[key]:>10,}{(theirs or 0):>12,}  {ratio}")
    add(f"    {'collision triangles':<22}{result['collisionTriangles']:>10,}")
    add(f"    {'cooked shapes':<22}{result['cooked']:>10,} of {result['shapeFiles']:,}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="level_budget", description=__doc__.split("\n\n")[0])
    parser.add_argument("level", nargs="?", help="built level directory")
    parser.add_argument("--view", type=float, default=4000.0, help="view distance in metres")
    parser.add_argument("--spawn", default=None, help="X,Y to measure from (default: the SpawnSphere)")
    parser.add_argument("--screen-height", type=float, default=1080.0)
    parser.add_argument("--json", default=None, help="also write the numbers here")
    parser.add_argument("--vanilla", default=None, help="BeamNG install to re-measure the maxima from")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.vanilla:
        rows = measure_vanilla(args.vanilla)
        for name, row in sorted(rows.items()):
            print(f"{name:28s} " + "  ".join(f"{k}={v}" for k, v in row.items()))
        return 0
    if not args.level:
        parser.error("a level directory is required (or --vanilla)")
    spawn = None
    if args.spawn:
        parts = args.spawn.replace(",", " ").split()
        spawn = (float(parts[0]), float(parts[1]))
    result = measure(args.level, args.view, spawn, args.screen_height)
    print(report(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
