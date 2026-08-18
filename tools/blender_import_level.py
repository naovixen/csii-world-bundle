"""Reconstruct a cs2beamng BeamNG level in Blender — the consumer-side oracle.

    blender --background --python tools/blender_import_level.py -- --level <levels/cs2_city> \
        --center <X> <Y> --radius 250 [--products networks,markings,lanegeometry,holewalls] \
        [--no-terrain] [--no-forest] [--forest-max N] [--ter other.ter] --save out.blend

Reads what BeamNG reads — the level folder, nothing from the bundle — so a render answers
"what will the game draw here" for the parts Blender can show: the `.ter` heightfield with its
holes and per-sample material colour, every `TSStatic` `.dae` tile whose footprint meets the
region (Blender 5 ships no COLLADA importer, so `read_dae` here is a small general COLLADA 1.4
triangles reader — sources/accessors, `<vertices>`, per-input offsets, node matrices), and
the level's v1.5 materials wired as Principled BSDF: `baseColorMap` (+ `opacityMap` as alpha,
`alphaTest`/`translucent` → blend), `diffuseColor` when there is no map. Center/radius are
BeamNG world x/y (= bundle x/z). Terrain material colours are flat stand-ins per ground name.

**Forest items too** (A3): `forest/*.forest4.json` + `art/forest/managedItemData.json`, one mesh
datablock per type and a linked duplicate per item. Without them the oracle shows an empty city,
because a forest item is referenced by no `TSStatic`. Only the NEAREST detail level of a shape is
imported - a shape with an LOD ladder writes one node per level.

Companion of `tools/blender_import_bundle.py` (the producer-side truth): import the same
region from both and compare — the difference is the app's doing. Frame: BeamNG is Z-up
right-handed like Blender, so nothing is transformed beyond each tile's `position`.
"""

import json
import os
import re
import sys
import time

import bpy
import numpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
sys.path.insert(0, HERE)

from cs2beamng.beamng import level as level_mod  # noqa: E402
from cs2beamng.beamng import ter as ter_mod  # noqa: E402

GROUND_COLOURS = {
    "grass": (0.18, 0.36, 0.10), "rock": (0.42, 0.40, 0.38), "dirt": (0.36, 0.26, 0.16),
    "roadbed": (0.22, 0.20, 0.18), "extra1": (0.5, 0.45, 0.3), "extra2": (0.4, 0.5, 0.3),
    "extra3": (0.5, 0.3, 0.3), "extra4": (0.3, 0.4, 0.5),
}


def log(msg):
    print(f"[level] {msg}", flush=True)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"level": None, "center": None, "radius": 250.0, "products": None, "terrain": True,
            "save": None, "ter": None, "forest": True, "forest_max": 40000}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--level":
            opts["level"] = argv[i + 1]; i += 2
        elif a == "--center":
            opts["center"] = (float(argv[i + 1]), float(argv[i + 2])); i += 3
        elif a == "--radius":
            opts["radius"] = float(argv[i + 1]); i += 2
        elif a == "--products":
            opts["products"] = set(argv[i + 1].split(",")); i += 2
        elif a == "--no-terrain":
            opts["terrain"] = False; i += 1
        elif a == "--no-forest":
            opts["forest"] = False; i += 1
        elif a == "--forest-max":
            opts["forest_max"] = int(argv[i + 1]); i += 2
        elif a == "--ter":
            # diagnostic: another level's .ter (same size/origin) in place of this level's — the
            # A/B of a terrain-stage change against the shapes that did not change
            opts["ter"] = argv[i + 1]; i += 2
        elif a == "--save":
            opts["save"] = argv[i + 1]; i += 2
        else:
            raise SystemExit(f"unknown argument {a}")
    if not opts["level"] or not opts["center"]:
        raise SystemExit("--level and --center are required")
    return opts


# -- level data ----------------------------------------------------------------------------------

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


# -- terrain -------------------------------------------------------------------------------------

def build_terrain(level_dir, objects, center, radius, ter_override=None):
    block = next((o for o in objects if o.get("class") == "TerrainBlock"), None)
    if block is None:
        log("no TerrainBlock in the level")
        return
    ter_path = ter_override or resolve(level_dir, block["terrainFile"])
    size, heights, layers, names = ter_mod.read_ter(ter_path)
    pos = [float(v) for v in block["position"]]
    cell = float(block.get("squareSize") or 1.0)
    max_height = float(block.get("maxHeight") or 2048.0)
    scale = max_height / 65536.0
    cx, cy = center
    c0 = max(0, int((cx - radius - pos[0]) / cell)); c1 = min(size - 1, int((cx + radius - pos[0]) / cell) + 1)
    r0 = max(0, int((cy - radius - pos[1]) / cell)); r1 = min(size - 1, int((cy + radius - pos[1]) / cell) + 1)
    if c1 <= c0 or r1 <= r0:
        log("region outside the terrain block")
        return
    hs = heights[r0:r1 + 1, c0:c1 + 1].astype(numpy.float64) * scale + pos[2]
    ls = layers[r0:r1 + 1, c0:c1 + 1]
    nr, nc = hs.shape
    xs = pos[0] + (numpy.arange(c0, c1 + 1) * cell)
    ys = pos[1] + (numpy.arange(r0, r1 + 1) * cell)
    verts = numpy.empty((nr * nc, 3), dtype=numpy.float64)
    verts[:, 0] = numpy.tile(xs, nr)
    verts[:, 1] = numpy.repeat(ys, nc)
    verts[:, 2] = hs.ravel()
    # a face's layer byte is the one at its south-west sample (BeamNG's convention: the square
    # starting at sample (r, c)); HOLE = 255 removes the square
    idx = numpy.arange(nr * nc).reshape(nr, nc)
    sw = idx[:-1, :-1]; se = idx[:-1, 1:]; nw = idx[1:, :-1]; ne = idx[1:, 1:]
    keep = ls[:-1, :-1] != ter_mod.HOLE
    holes = int((~keep).sum())
    quads = numpy.stack([sw[keep], se[keep], ne[keep], nw[keep]], axis=1)
    face_layer = ls[:-1, :-1][keep]
    mesh = bpy.data.meshes.new("ter")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(len(quads) * 4)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", numpy.arange(len(quads)) * 4)
    mesh.polygons.foreach_set("loop_total", numpy.full(len(quads), 4))
    mats = []
    for i, name in enumerate(names):
        m = bpy.data.materials.new(f"ter:{name}")
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get("Principled BSDF")
        col = next((c for k, c in GROUND_COLOURS.items() if name == k or name.endswith("_" + k)), (0.5, 0.5, 0.5))
        bsdf.inputs["Base Color"].default_value = (*col, 1.0)
        bsdf.inputs["Roughness"].default_value = 1.0
        mesh.materials.append(m)
        mats.append(m)
    mesh.polygons.foreach_set("material_index", numpy.clip(face_layer, 0, max(0, len(names) - 1)).astype(numpy.int32))
    mesh.update(calc_edges=True)
    mesh.validate()
    obj = bpy.data.objects.new("terrain", mesh)
    bpy.context.scene.collection.objects.link(obj)
    log(f"terrain: {nr}x{nc} samples, {len(quads):,} faces, {holes:,} holes in the region, materials {names}")


# -- shapes ----------------------------------------------------------------------------------------

def tiles_in_region(level_dir, objects, center, radius, products):
    cx, cy = center
    out = []
    for o in objects:
        if o.get("class") != "TSStatic" or not o.get("shapeName"):
            continue
        product = o["shapeName"].split("/")[-2] if "/" in o["shapeName"] else ""
        if products and product not in products:
            continue
        pos = [float(v) for v in o.get("position") or (0, 0, 0)]
        # tile shapes are written relative to their tile centre; a tile is 256 m (default) —
        # take everything whose centre lies within radius + 200 m and let the geometry decide
        if abs(pos[0] - cx) <= radius + 200 and abs(pos[1] - cy) <= radius + 200:
            out.append((o["name"], resolve(level_dir, o["shapeName"]), pos, product))
    return out


NS = "{http://www.collada.org/2005/11/COLLADASchema}"


def read_dae(path):
    """Parse a COLLADA 1.4 file into `[(node_name, verts (N,3), normals|None, uvs|None,
    tris (M,3), [(material_symbol_target, count), ...], matrix (4,4))]`, one entry per
    `<instance_geometry>` in the visual scene (geometry ids resolved). Handles per-input
    offsets (indices are un-shared into per-corner vertices when inputs disagree)."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    geoms = {}
    for g in root.iter(NS + "geometry"):
        mesh = g.find(NS + "mesh")
        if mesh is None:
            continue
        sources = {}
        for src in mesh.findall(NS + "source"):
            arr = src.find(NS + "float_array")
            acc = src.find(f"{NS}technique_common/{NS}accessor")
            if arr is None or acc is None:
                continue
            stride = int(acc.get("stride") or 1)
            vals = numpy.fromstring(arr.text, sep=" ", dtype=numpy.float64)
            sources["#" + src.get("id")] = vals.reshape(-1, stride)
        vert_map = {}
        for v in mesh.findall(NS + "vertices"):
            for inp in v.findall(NS + "input"):
                vert_map.setdefault("#" + v.get("id"), {})[inp.get("semantic")] = inp.get("source")
        pos_all, nrm_all, uv_all, tri_all, runs = [], [], [], [], []
        base = 0
        for t in mesh.findall(NS + "triangles"):
            count = int(t.get("count") or 0)
            if count == 0:
                continue
            inputs = []
            for inp in t.findall(NS + "input"):
                sem, off, srcid = inp.get("semantic"), int(inp.get("offset") or 0), inp.get("source")
                if sem == "VERTEX":
                    for vsem, vsrc in vert_map.get(srcid, {}).items():
                        inputs.append((vsem, off, vsrc))
                else:
                    inputs.append((sem, off, srcid))
            stride = max(off for _s, off, _src in inputs) + 1
            p = numpy.fromstring(t.find(NS + "p").text, sep=" ", dtype=numpy.int64).reshape(-1, stride)
            by = {sem: (off, sources[src]) for sem, off, src in inputs if src in sources}
            pos_off, pos_src = by["POSITION"]
            n = len(p)                                    # corners
            pos_all.append(pos_src[p[:, pos_off]])
            if "NORMAL" in by:
                o, srcv = by["NORMAL"]; nrm_all.append(srcv[p[:, o]])
            if "TEXCOORD" in by:
                o, srcv = by["TEXCOORD"]; uv_all.append(srcv[p[:, o]][:, :2])
            tri_all.append(numpy.arange(base, base + n).reshape(-1, 3))
            runs.append((t.get("material"), count))
            base += n
        if not tri_all:
            continue
        geoms["#" + g.get("id")] = (numpy.concatenate(pos_all), numpy.concatenate(nrm_all) if nrm_all and len(nrm_all) == len(pos_all) else None,
                                    numpy.concatenate(uv_all) if uv_all and len(uv_all) == len(pos_all) else None,
                                    numpy.concatenate(tri_all), runs)
    out = []

    def walk(node, parent_m):
        m = parent_m
        mx = node.find(NS + "matrix")
        if mx is not None:
            m = parent_m @ numpy.fromstring(mx.text, sep=" ", dtype=numpy.float64).reshape(4, 4)
        for ig in node.findall(NS + "instance_geometry"):
            geo = geoms.get(ig.get("url"))
            if geo is None:
                continue
            symbols = {}
            for im in ig.iter(NS + "instance_material"):
                symbols[im.get("symbol")] = im.get("target", "").lstrip("#")
            runs = [(symbols.get(sym, sym or ""), c) for sym, c in geo[4]]
            out.append((node.get("name") or node.get("id"), geo[0], geo[1], geo[2], geo[3], runs, m))
        for child in node.findall(NS + "node"):
            walk(child, m)

    for scene in root.iter(NS + "visual_scene"):
        for node in scene.findall(NS + "node"):
            walk(node, numpy.eye(4))
    return out


def import_tiles(tiles, materials, level_dir, center, radius):
    imported = 0
    for name, path, pos, product in tiles:
        if not os.path.isfile(path):
            log(f"missing shape {path}")
            continue
        try:
            parts = read_dae(path)
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: dae read failed: {exc!r}")
            continue
        for node_name, verts, normals, uvs, tris, runs, matrix in _nearest_detail(parts):
            v = verts @ matrix[:3, :3].T + matrix[:3, 3] + numpy.asarray(pos)
            _mesh_object(f"{name}:{node_name}", v, tris, runs, uvs, product)
        imported += 1
    log(f"{imported} shape tiles imported")
    return imported


DETAIL_RE = re.compile(r"_a(\d+)$")


def _nearest_detail(parts):
    """The parts of ONE detail level - the largest `_a<px>` - with the collider and the
    imposter/null placeholders dropped. A shape with an LOD ladder writes a node per level, and
    importing all of them stacks three copies of the same tree on top of each other."""
    keep = [p for p in parts
            if "Colmesh" not in p[0] and "bb_autobillboard" not in p[0] and "nulldetail" not in p[0]]
    sizes = [int(m.group(1)) for m in (DETAIL_RE.search(p[0]) for p in keep) if m]
    if not sizes:
        return keep
    nearest = max(sizes)
    out = []
    for part in keep:
        m = DETAIL_RE.search(part[0])
        if m is None or int(m.group(1)) == nearest:
            out.append(part)
    return out


def import_forest(level_dir, center, radius, limit):
    """Place the level's `forest/*.forest4.json` items - the A3 stage's whole output, which no
    `TSStatic` references and which the oracle would otherwise show as an empty city.

    Every item of one type shares ONE mesh datablock (Blender linked duplicates), so a region
    with 18k trees is 18k objects over a few hundred meshes rather than 18k meshes.
    """
    item_path = os.path.join(level_dir, "art", "forest", "managedItemData.json")
    forest_dir = os.path.join(level_dir, "forest")
    if not os.path.isfile(item_path) or not os.path.isdir(forest_dir):
        log("no forest in this level")
        return 0
    with open(item_path, encoding="utf-8") as handle:
        item_data = json.load(handle) or {}
    cx, cy = center
    cache = {}
    placed = skipped = 0
    for name in sorted(os.listdir(forest_dir)):
        if not name.endswith(".forest4.json"):
            continue
        with open(os.path.join(forest_dir, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                pos = item["pos"]
                if abs(pos[0] - cx) > radius or abs(pos[1] - cy) > radius:
                    continue
                if placed >= limit:
                    skipped += 1
                    continue
                meshes = _forest_meshes(cache, level_dir, item_data, item["type"])
                if not meshes:
                    continue
                scale = float(item.get("scale") or 1.0)
                rot = numpy.asarray(item["rotationMatrix"], dtype=numpy.float64).reshape(3, 3)
                for mesh, local in meshes:
                    obj = bpy.data.objects.new(f"{item['type']}:{placed}", mesh)
                    m = numpy.eye(4)
                    m[:3, :3] = rot @ local[:3, :3] * scale
                    m[:3, 3] = numpy.asarray(pos) + rot @ (local[:3, 3] * scale)
                    # a mathutils.Matrix, NOT a list of numpy rows: Blender takes the
                    # assignment silently and leaves the object at the origin
                    obj.matrix_world = Matrix([[float(v) for v in row] for row in m])
                    obj["cs2_product"] = "forest"
                    bpy.context.scene.collection.objects.link(obj)
                placed += 1
    log(f"{placed} forest items placed ({len(cache)} types, {skipped} beyond --forest-max {limit})")
    return placed


def _forest_meshes(cache, level_dir, item_data, type_name):
    """`[(mesh datablock, node matrix), ...]` for one forest type, read once and reused."""
    if type_name in cache:
        return cache[type_name]
    entry = item_data.get(type_name) or {}
    path = resolve(level_dir, entry.get("shapeFile"))
    out = []
    if path and os.path.isfile(path):
        try:
            for node_name, verts, _n, uvs, tris, runs, matrix in _nearest_detail(read_dae(path)):
                obj = _mesh_object(f"forestproto:{type_name}:{node_name}", verts, tris, runs, uvs, "forest")
                mesh = obj.data
                bpy.data.objects.remove(obj)               # keep the datablock, drop the prototype
                out.append((mesh, numpy.asarray(matrix, dtype=numpy.float64)))
        except Exception as exc:  # noqa: BLE001
            log(f"forest type {type_name}: dae read failed: {exc!r}")
    cache[type_name] = out
    return out


def _material(name):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
    return m


def _mesh_object(name, verts, tris, runs, uvs, product):
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    n = len(tris)
    mesh.loops.add(n * 3)
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(tris, dtype=numpy.int32).ravel())
    mesh.polygons.add(n)
    mesh.polygons.foreach_set("loop_start", numpy.arange(n, dtype=numpy.int32) * 3)
    mesh.polygons.foreach_set("loop_total", numpy.full(n, 3, dtype=numpy.int32))
    mat_index = numpy.zeros(n, dtype=numpy.int32)
    start = 0
    slot = {}
    for target, count in runs:
        # material target id "<name>-material" -> the level's mapTo name
        mname = re.sub(r"-material$", "", target)
        if mname not in slot:
            slot[mname] = len(mesh.materials)
            mesh.materials.append(_material(mname))
        mat_index[start:start + count] = slot[mname]
        start += count
    mesh.polygons.foreach_set("material_index", mat_index)
    if uvs is not None and len(uvs) == len(verts):
        layer = mesh.uv_layers.new(name="UVMap")
        uv_loops = numpy.asarray(uvs, dtype=numpy.float32)[numpy.asarray(tris).ravel()]
        layer.data.foreach_set("uv", uv_loops.ravel())
    mesh.update(calc_edges=True)
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    obj["cs2_product"] = product
    bpy.context.scene.collection.objects.link(obj)
    return obj


def wire_materials(materials, level_dir):
    """Turn the COLLADA importer's placeholder materials into the level's v1.5 look."""
    wired = missing = 0
    cache = {}
    for m in bpy.data.materials:
        if m.name.startswith("ter:") or m.name.startswith("probe"):
            continue
        key = m.name
        rec = materials.get(key)
        if rec is None:
            missing += 1
            continue
        stage = (rec.get("Stages") or [{}])[0]
        m.use_nodes = True
        nodes = m.node_tree.nodes
        links = m.node_tree.links
        bsdf = nodes.get("Principled BSDF")
        if bsdf is None:
            continue
        bsdf.inputs["Roughness"].default_value = float(stage.get("roughnessFactor", 0.9))
        bsdf.inputs["Metallic"].default_value = float(stage.get("metallicFactor", 0.0))
        color = stage.get("baseColorMap")
        if color:
            img = _image(cache, resolve(level_dir, color), "sRGB")
            if img is not None:
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = img
                links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        elif stage.get("diffuseColor"):
            c = [float(v) for v in stage["diffuseColor"]]
            bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)
        opacity = stage.get("opacityMap")
        translucent = bool(rec.get("translucent") or rec.get("alphaTest"))
        if opacity and translucent:
            img = _image(cache, resolve(level_dir, opacity), "Non-Color")
            if img is not None:
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = img
                links.new(tex.outputs["Color"], bsdf.inputs["Alpha"])
                if "opacityFactor" in stage:
                    pass
        elif translucent and "opacityFactor" in stage:
            bsdf.inputs["Alpha"].default_value = float(stage["opacityFactor"])
        if translucent:
            _blend(m, "HASHED" if rec.get("alphaTest") else "BLEND")
        wired += 1
    log(f"materials: {wired} wired from the level, {missing} unknown to it")


def _blend(m, mode):
    # Blender 4.2+ removed blend_method for EEVEE Next in favour of surface_render_method
    if hasattr(m, "surface_render_method"):
        m.surface_render_method = "DITHERED" if mode == "HASHED" else "BLENDED"
    if hasattr(m, "blend_method"):
        try:
            m.blend_method = mode
        except TypeError:
            pass
    if hasattr(m, "use_backface_culling"):
        m.use_backface_culling = False


def _image(cache, path, colorspace):
    if path in cache:
        return cache[path]
    if not os.path.isfile(path):
        cache[path] = None
        return None
    img = bpy.data.images.load(path)
    try:
        img.colorspace_settings.name = colorspace
    except TypeError:
        pass
    cache[path] = img
    return img


# -- main ------------------------------------------------------------------------------------------

def main():
    opts = parse_args()
    started = time.monotonic()
    level_dir = os.path.abspath(opts["level"])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    objects, materials = read_level(level_dir)
    log(f"{len(objects)} objects, {len(materials)} materials in {level_dir}")
    if opts["terrain"]:
        build_terrain(level_dir, objects, opts["center"], opts["radius"], ter_override=opts["ter"])
    tiles = tiles_in_region(level_dir, objects, opts["center"], opts["radius"], opts["products"])
    log(f"{len(tiles)} shape tiles meet the region")
    import_tiles(tiles, materials, level_dir, opts["center"], opts["radius"])
    if opts["forest"]:
        import_forest(level_dir, opts["center"], opts["radius"], opts["forest_max"])
    wire_materials(materials, level_dir)
    if opts["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(opts["save"]))
        log(f"saved {opts['save']}")
    log(f"done in {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    main()
