"""Read back the COLLADA `beamng/dae.py` writes — the shape reader beside the shape writer.

The two halves of one file format, in one package: `dae.py` documents the scene layout this
parses (`base00 -> { bb_autobillboard<px>, nulldetail<px>, start01 -> { <name>_a<px> per LOD,
Colmesh_<name>-1 ... } }`), and `nearest_detail` filters on exactly the node names it writes.
Living apart is how the reader ended up in `tools/blender_import_level.py` with no shared
schema at all, which refactor Phase 6 moved it out of.

Deliberately general within COLLADA 1.4 rather than tuned to this writer: it resolves geometry
ids, honours per-input offsets and un-shares indices when the inputs disagree, so it also reads
a `.dae` BeamNG or another tool produced. Blender 5 ships no COLLADA importer, which is why the
level oracle needs this at all.

`numpy` is the only dependency; nothing here imports `bpy`.
"""

import re
import xml.etree.ElementTree as ET

import numpy

NS = "{http://www.collada.org/2005/11/COLLADASchema}"

# The trailing `_a<px>` `dae.py` writes on every detail node: the on-screen size in
# pixels at which that level starts to be used (bigger = closer).
DETAIL_RE = re.compile(r"_a(\d+)$")


def _sources(mesh):
    """`{"#id": (rows, stride) as an (N, stride) array}` for every `<source>` of one mesh."""
    out = {}
    for src in mesh.findall(NS + "source"):
        arr = src.find(NS + "float_array")
        acc = src.find(f"{NS}technique_common/{NS}accessor")
        if arr is None or acc is None:
            continue
        stride = int(acc.get("stride") or 1)
        vals = numpy.fromstring(arr.text, sep=" ", dtype=numpy.float64)
        out["#" + src.get("id")] = vals.reshape(-1, stride)
    return out


def _vertex_map(mesh):
    """`{"#vertices-id": {semantic: "#source-id"}}` — what a `VERTEX` input expands to."""
    out = {}
    for v in mesh.findall(NS + "vertices"):
        for inp in v.findall(NS + "input"):
            out.setdefault("#" + v.get("id"), {})[inp.get("semantic")] = inp.get("source")
    return out


def _inputs(triangles, vert_map):
    """`[(semantic, offset, "#source")]` for one `<triangles>`, with VERTEX expanded."""
    inputs = []
    for inp in triangles.findall(NS + "input"):
        sem, off, srcid = inp.get("semantic"), int(inp.get("offset") or 0), inp.get("source")
        if sem == "VERTEX":
            for vsem, vsrc in vert_map.get(srcid, {}).items():
                inputs.append((vsem, off, vsrc))
        else:
            inputs.append((sem, off, srcid))
    return inputs


def _triangles(mesh, sources, vert_map):
    """One mesh's `<triangles>` blocks, un-shared into per-corner vertices.

    A COLLADA index row carries one index per INPUT, so position, normal and UV may each
    index a different array. Rather than reconstruct a shared vertex set, every corner
    becomes its own vertex — which is what the writer's own meshes look like anyway, and what
    keeps a file with disagreeing offsets from silently losing its UVs.
    """
    pos_all, nrm_all, uv_all, tri_all, runs = [], [], [], [], []
    base = 0
    for t in mesh.findall(NS + "triangles"):
        count = int(t.get("count") or 0)
        if count == 0:
            continue
        inputs = _inputs(t, vert_map)
        stride = max(off for _s, off, _src in inputs) + 1
        p = numpy.fromstring(t.find(NS + "p").text, sep=" ", dtype=numpy.int64).reshape(-1, stride)
        by = {sem: (off, sources[src]) for sem, off, src in inputs if src in sources}
        pos_off, pos_src = by["POSITION"]
        n = len(p)  # corners
        pos_all.append(pos_src[p[:, pos_off]])
        if "NORMAL" in by:
            o, srcv = by["NORMAL"]
            nrm_all.append(srcv[p[:, o]])
        if "TEXCOORD" in by:
            o, srcv = by["TEXCOORD"]
            uv_all.append(srcv[p[:, o]][:, :2])
        tri_all.append(numpy.arange(base, base + n).reshape(-1, 3))
        runs.append((t.get("material"), count))
        base += n
    if not tri_all:
        return None
    return (
        numpy.concatenate(pos_all),
        numpy.concatenate(nrm_all) if nrm_all and len(nrm_all) == len(pos_all) else None,
        numpy.concatenate(uv_all) if uv_all and len(uv_all) == len(pos_all) else None,
        numpy.concatenate(tri_all),
        runs,
    )


def _geometries(root):
    """`{"#geometry-id": (verts, normals|None, uvs|None, tris, runs)}` for the whole file."""
    geoms = {}
    for g in root.iter(NS + "geometry"):
        mesh = g.find(NS + "mesh")
        if mesh is None:
            continue
        built = _triangles(mesh, _sources(mesh), _vertex_map(mesh))
        if built is not None:
            geoms["#" + g.get("id")] = built
    return geoms


def _walk(node, parent_m, geoms, out):
    """One visual-scene node and its children, accumulating world matrices down the tree."""
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
        _walk(child, m, geoms, out)


def read_shape(path):
    """Parse a COLLADA 1.4 file into `[(node_name, verts (N,3), normals|None, uvs|None,
    tris (M,3), [(material_symbol_target, count), ...], matrix (4,4))]`, one entry per
    `<instance_geometry>` in the visual scene (geometry ids resolved). Handles per-input
    offsets (indices are un-shared into per-corner vertices when inputs disagree)."""
    root = ET.parse(path).getroot()
    geoms = _geometries(root)
    out = []
    for scene in root.iter(NS + "visual_scene"):
        for node in scene.findall(NS + "node"):
            _walk(node, numpy.eye(4), geoms, out)
    return out


def nearest_detail(parts):
    """The parts of ONE detail level - the largest `_a<px>` - with the collider and the
    imposter/null placeholders dropped. A shape with an LOD ladder writes a node per level, and
    importing all of them stacks three copies of the same tree on top of each other."""
    keep = [
        p
        for p in parts
        if "Colmesh" not in p[0] and "bb_autobillboard" not in p[0] and "nulldetail" not in p[0]
    ]
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
