"""What a built BeamNG level costs, against what BeamNG's own levels cost.

    cs2beamng budget <level_dir> [--view 4000] [--spawn X,Y] [--json out.json]
    cs2beamng budget <level_dir> --vanilla <BeamNG install>   # re-measure the maxima
    python tools/level_budget.py <level_dir> ...              # the same, from a checkout

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

import json
import math
import os
import re
import zipfile
from collections import defaultdict

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
    bounds = [
        (m.group(1).decode("ascii", "replace"), m.start()) for m in GEOM_RE.finditer(data, 0, scene)
    ]
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


# Written by the build beside the shapes (`beamng.dae.write_ladder_index`) so that the detail
# ladder survives `keep_dae: false`. Without it a level whose `.dae` sources were dropped can
# report its BYTES but not one triangle it draws — which is the gate this whole tool exists for.
LADDER_INDEX = "shape_ladders.json"
_LADDERS = {}


def _ladder_from_sidecar(level_dir, rel):
    """`(levels, collision, radius)` for a shape, from the build's own index.

    The ladder and the collision count stand in for a `.dae` that is gone; the radius is the
    build's own measurement of the shape's bounding sphere and is read whether the source is
    there or not — `scan_dae` never computes it (`radius_hint` explains why it matters)."""
    if level_dir not in _LADDERS:
        path = os.path.join(level_dir, LADDER_INDEX)
        try:
            with open(path, encoding="utf-8") as handle:
                _LADDERS[level_dir] = json.load(handle).get("shapes") or {}
        except (OSError, ValueError):
            _LADDERS[level_dir] = {}
    entry = _LADDERS[level_dir].get(rel)
    if not entry:
        return None, None, None
    radius = entry.get("radius")
    return (
        {int(px): int(t) for px, t in (entry.get("levels") or {}).items()},
        int(entry.get("collision") or 0),
        float(radius) if radius else None,
    )


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
    from .level import read_objects

    level_dir = os.path.abspath(level_dir)
    name = os.path.basename(level_dir)

    # -- shapes: bytes and the detail ladder -----------------------------------------------
    shapes = {}
    shapes_root = os.path.join(level_dir, "art", "shapes")
    # Keyed on the SOURCE path even when only the cooked file is there. `keep_dae: false` drops
    # every `.dae` whose `.cdae` is current (A8/P7), and a scan that keys on `.dae` alone then
    # reports a level with 8,164 shapes as having none — which is what this did on the first A8
    # build, and it is exactly the P8 blindness the plan warned about in its own instrument.
    for root, _dirs, files in os.walk(shapes_root):
        for file_name in files:
            lower = file_name.lower()
            if lower.endswith(".cdae"):
                if os.path.isfile(os.path.join(root, file_name[:-5] + ".dae")):
                    continue  # the source is there; it is scanned below
                full = os.path.join(root, file_name[:-5] + ".dae")
            elif lower.endswith(".dae"):
                full = os.path.join(root, file_name)
            else:
                continue
            rel = os.path.relpath(full, level_dir).replace("\\", "/")
            size, cooked = file_bytes(full)
            # the detail ladder and the collision count come out of the XML; with the source
            # dropped they are UNKNOWN rather than zero, and are reported as such
            side_levels, side_collision, radius = _ladder_from_sidecar(level_dir, rel)
            levels, collision = (
                scan_dae(full) if os.path.isfile(full) else (side_levels, side_collision)
            )
            shapes[rel] = {
                "bytes": size,
                "cooked": cooked,
                "levels": levels,
                "collision": collision,
                "radius": radius,
                "group": group_of(rel),
            }

    unknown_ladders = set()

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
        rel = (
            shape_name.split(f"/levels/{name}/", 1)[-1]
            if f"/levels/{name}/" in shape_name
            else None
        )
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
        radius = shape.get("radius") or radius_hint(rel, shape)
        if shape["levels"] is None:
            unknown_ladders.add(shape["group"])
            continue
        in_view[shape["group"]] += detail_at(
            shape["levels"], radius, max(distance, 1.0), screen_height
        )
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
        "heaviestShapes": sorted(((s["bytes"], name) for name, s in shapes.items()), reverse=True)[
            :20
        ],
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
        "collisionTriangles": sum(s["collision"] or 0 for s in shapes.values()),
        # the groups whose `.dae` sources were dropped, so their ladders could not be read
        "unknownLadders": sorted(unknown_ladders),
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
    """The bounding-sphere radius a Torque detail size is measured against, when the build's
    index does not carry the shape's own (`shape_ladders.json` `radius`, written by the cook).

    A merged world tile is a `<product>_<tx>_<tz>` shape covering one tile of the grid, so its
    radius is the tile's own half-diagonal — the same quantity `beamng/tilelod.radius_of`
    computed when the ladder was built. An object shape with no recorded radius takes a nominal
    2 m — DECLARED APPROXIMATION, and a bad one: Torque picks the detail by projected pixel size,
    which is linear in radius, so at 2 m every building past ~150 m read as "not drawn" and the
    Phase-0 baseline reported 15,019 object triangles in view where a 20 m radius gives millions
    (refactor Phase 10 survey §0). The index has carried the real radius since that survey; this
    fallback is for a level built before it.
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
        row = {
            "shapeFiles": 0,
            "tsStatics": 0,
            "forestTypes": 0,
            "forestItems": 0,
            "materials": 0,
            "visibleDistance": None,
            "meshBytes": 0,
            "textureBytes": 0,
            "residentTotal": 0,
        }
        try:
            with zipfile.ZipFile(path) as archive:
                # a `.dae` whose `.cdae` is also in the archive is the source file and is not
                # loaded; collect the cooked stems first so the second pass can skip it
                cooked_stems = {
                    os.path.splitext(i.filename.lower())[0]
                    for i in archive.infolist()
                    if i.filename.lower().endswith(".cdae")
                }
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
