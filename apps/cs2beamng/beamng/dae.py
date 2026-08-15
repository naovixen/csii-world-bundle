"""COLLADA 1.4.1 writer for BeamNG shapes — the vanilla `.dae` conventions, nothing more.

Read from shipped levels (small_island rocks/trees/buildings) and BeamNG's docs:

* `<asset>`: `<unit meter="1">`, `<up_axis>Z_UP</up_axis>`; positions in metres, Z up.
* `<library_materials>`: `<material id="<name>-material" name="<name>">`; the `name` is what
  BeamNG matches against `main.materials.json` `mapTo`. Effects are a placeholder phong.
* One `<geometry>` per mesh: sources for positions / normals / TEXCOORD (set 0) / COLOR, a
  `<vertices>` element, and one `<triangles material="<sym>">` per material run. All inputs
  share offset 0 (allowed by COLLADA; BeamNG's loader is fine with it), so `<p>` is one index
  per corner and our per-vertex glTF arrays go out unchanged.
* Scene: `base00 → { bb_autobillboard<px>, nulldetail<px>, start01 → { <name>_a<px> per LOD,
  Colmesh_<name>-1 … } }`. `<px>` on a detail node is the on-screen size in pixels at which
  that level starts to be used (bigger = closer); `nulldetail` = draw nothing below;
  `bb_autobillboard` = the engine renders an imposter below that size.
* Frame: callers hand in BeamNG-local coordinates. From a bundle `.glb` (glTF RH Y-up, X
  mirrored per §2.2) that is `(−gx, gz, gy)`; the two reflections cancel, so triangle winding
  and normals are used as stored.

`Mesh` is plain data; `write_shape` writes the file. Floats go out through `ndarray.tofile`,
which is what keeps a 300k-vertex tile at a couple of seconds.
"""

import os
import re

import numpy

FLOAT_FMT = "%.4f"
UV_FMT = "%.5f"


class Mesh:
    """One geometry: `verts (N,3)`, optional `normals (N,3)`, `uvs (N,2)`, `colors (N,4)`,
    `tris (M,3)` int, `runs [(material_name, triangle_count), ...]` in `tris` order."""

    __slots__ = ("name", "verts", "normals", "uvs", "colors", "tris", "runs")

    def __init__(self, name, verts, tris, runs, normals=None, uvs=None, colors=None):
        self.name = name
        self.verts = numpy.ascontiguousarray(verts, dtype=numpy.float32)
        self.tris = numpy.ascontiguousarray(tris, dtype=numpy.int64)
        self.runs = list(runs)
        self.normals = None if normals is None else numpy.ascontiguousarray(normals, dtype=numpy.float32)
        self.uvs = None if uvs is None else numpy.ascontiguousarray(uvs, dtype=numpy.float32)
        self.colors = None if colors is None else numpy.ascontiguousarray(colors, dtype=numpy.float32)
        if sum(c for _m, c in self.runs) != len(self.tris):
            raise ValueError(f"{name}: material runs cover {sum(c for _m, c in self.runs)} triangles, mesh has {len(self.tris)}")

    @property
    def materials(self):
        seen = []
        for m, _c in self.runs:
            if m not in seen:
                seen.append(m)
        return seen


class Shape:
    """`levels`: [(px, [Mesh, ...]), ...] from nearest (largest px) to farthest; `collision`:
    [Mesh, ...] (`Colmesh_<name>-N`); `billboard_px` / `null_px`: thresholds or None."""

    def __init__(self, name, levels, collision=(), billboard_px=None, null_px=None):
        self.name = name
        self.levels = list(levels)
        for px, _ms in self.levels:
            if int(px) <= 0:
                raise ValueError(f"{name}: detail size must be > 0 (BeamNG rejects the shape: 'invalid detail levels')")
        self.collision = list(collision)
        self.billboard_px = billboard_px
        self.null_px = null_px


def sanitize(name):
    """A COLLADA id / BeamNG material name: letters, digits, `_`, `-`, `.`."""
    out = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name))
    return out or "m"


def write_shape(path, shape):
    """Write `shape` as a `.dae`. Returns `{materials: [...], triangles: n, vertices: n}`."""
    meshes = []
    seen_ids = set()
    names_used = {}
    for m in [m for _px, ms in shape.levels for m in ms] + list(shape.collision):
        if id(m) in seen_ids:
            continue                                     # one geometry however many nodes instance it
        seen_ids.add(id(m))
        base = sanitize(m.name)
        if base in names_used:                          # distinct meshes with one name: make the ids unique
            names_used[base] += 1
            m.name = f"{base}_{names_used[base]}"
        else:
            names_used[base] = 0
        meshes.append(m)
    materials = []
    for m in meshes:
        for name in m.materials:
            if name not in materials:
                materials.append(name)
    tri_total = sum(len(m.tris) for m in meshes)
    vert_total = sum(len(m.verts) for m in meshes)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
        f.write("  <asset>\n    <contributor><authoring_tool>cs2beamng</authoring_tool></contributor>\n"
                '    <unit name="meter" meter="1"/>\n    <up_axis>Z_UP</up_axis>\n  </asset>\n')
        # effects + materials
        f.write("  <library_effects>\n")
        for name in materials:
            sid = sanitize(name)
            f.write(f'    <effect id="{sid}-effect"><profile_COMMON><technique sid="common"><phong>'
                    '<diffuse><color sid="diffuse">0.8 0.8 0.8 1</color></diffuse></phong></technique></profile_COMMON></effect>\n')
        f.write("  </library_effects>\n  <library_materials>\n")
        for name in materials:
            sid = sanitize(name)
            f.write(f'    <material id="{sid}-material" name="{sid}"><instance_effect url="#{sid}-effect"/></material>\n')
        f.write("  </library_materials>\n")
        # geometries
        f.write("  <library_geometries>\n")
        for m in meshes:
            _write_geometry(f, m)
        f.write("  </library_geometries>\n")
        # scene
        f.write('  <library_visual_scenes>\n    <visual_scene id="Scene" name="Scene">\n')
        f.write('      <node id="base00" name="base00" type="NODE">\n')
        if shape.billboard_px is not None:
            px = int(shape.billboard_px)
            f.write(f'        <node id="bb_autobillboard{px}" name="bb_autobillboard{px}" type="NODE"/>\n')
        if shape.null_px is not None:
            px = int(shape.null_px)
            f.write(f'        <node id="nulldetail{px}" name="nulldetail{px}" type="NODE"/>\n')
        f.write('        <node id="start01" name="start01" type="NODE">\n')
        for px, ms in shape.levels:
            for i, m in enumerate(ms):
                node = f"{sanitize(shape.name)}_a{int(px)}" + (f"_{i}" if i else "")
                _write_node(f, node, m)
        for i, m in enumerate(shape.collision):
            _write_node(f, f"Colmesh_{sanitize(shape.name)}-{i + 1}", m)
        f.write("        </node>\n      </node>\n    </visual_scene>\n  </library_visual_scenes>\n")
        f.write('  <scene><instance_visual_scene url="#Scene"/></scene>\n</COLLADA>\n')
    return {"materials": materials, "triangles": tri_total, "vertices": vert_total}


def _geometry_id(mesh):
    return sanitize(mesh.name) + "-mesh"


def _write_node(f, node_id, mesh):
    gid = _geometry_id(mesh)
    f.write(f'          <node id="{node_id}" name="{node_id}" type="NODE">\n')
    f.write('            <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>\n')
    f.write(f'            <instance_geometry url="#{gid}" name="{node_id}">\n              <bind_material><technique_common>\n')
    for name in mesh.materials:
        sid = sanitize(name)
        f.write(f'                <instance_material symbol="{sid}-material" target="#{sid}-material">'
                '<bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/></instance_material>\n')
    f.write("              </technique_common></bind_material>\n            </instance_geometry>\n          </node>\n")


def _write_source(f, sid, array, params, fmt):
    n, stride = array.shape
    f.write(f'        <source id="{sid}">\n          <float_array id="{sid}-array" count="{n * stride}">')
    array.astype(numpy.float32).ravel().tofile(f, sep=" ", format=fmt)
    f.write("</float_array>\n          <technique_common>\n"
            f'            <accessor source="#{sid}-array" count="{n}" stride="{stride}">\n')
    for p in params:
        f.write(f'              <param name="{p}" type="float"/>\n')
    f.write("            </accessor>\n          </technique_common>\n        </source>\n")


def _write_geometry(f, mesh):
    gid = _geometry_id(mesh)
    f.write(f'    <geometry id="{gid}" name="{sanitize(mesh.name)}">\n      <mesh>\n')
    _write_source(f, f"{gid}-positions", mesh.verts, ("X", "Y", "Z"), FLOAT_FMT)
    if mesh.normals is not None:
        _write_source(f, f"{gid}-normals", mesh.normals, ("X", "Y", "Z"), FLOAT_FMT)
    if mesh.uvs is not None:
        _write_source(f, f"{gid}-map-0", mesh.uvs, ("S", "T"), UV_FMT)
    if mesh.colors is not None:
        _write_source(f, f"{gid}-colors", mesh.colors, ("R", "G", "B", "A"), UV_FMT)
    f.write(f'        <vertices id="{gid}-vertices"><input semantic="POSITION" source="#{gid}-positions"/></vertices>\n')
    start = 0
    for material, count in mesh.runs:
        sid = sanitize(material)
        f.write(f'        <triangles material="{sid}-material" count="{count}">\n'
                f'          <input semantic="VERTEX" source="#{gid}-vertices" offset="0"/>\n')
        if mesh.normals is not None:
            f.write(f'          <input semantic="NORMAL" source="#{gid}-normals" offset="0"/>\n')
        if mesh.uvs is not None:
            f.write(f'          <input semantic="TEXCOORD" source="#{gid}-map-0" offset="0" set="0"/>\n')
        if mesh.colors is not None:
            f.write(f'          <input semantic="COLOR" source="#{gid}-colors" offset="0" set="0"/>\n')
        f.write("          <p>")
        mesh.tris[start:start + count].ravel().tofile(f, sep=" ", format="%d")
        f.write("</p>\n        </triangles>\n")
        start += count
    f.write("      </mesh>\n    </geometry>\n")


# -- from bundle glb data ---------------------------------------------------------------------------

def glb_to_local(verts, normals=None, matrix=None):
    """glTF (RH, Y-up, X mirrored per §2.2) → BeamNG local (Z-up): `(−gx, gz, gy)`. `matrix` is
    the node's world matrix in glTF space, applied first."""
    v = numpy.asarray(verts, dtype=numpy.float64)
    n = None if normals is None else numpy.asarray(normals, dtype=numpy.float64)
    if matrix is not None:
        m = numpy.asarray(matrix, dtype=numpy.float64)
        v = v @ m[:3, :3].T + m[:3, 3]
        if n is not None:
            n = n @ numpy.linalg.inv(m[:3, :3])          # normals by the inverse transpose
    out = numpy.empty_like(v)
    out[:, 0] = -v[:, 0]
    out[:, 1] = v[:, 2]
    out[:, 2] = v[:, 1]
    if n is None:
        return out, None
    nn = numpy.empty_like(n)
    nn[:, 0] = -n[:, 0]
    nn[:, 1] = n[:, 2]
    nn[:, 2] = n[:, 1]
    length = numpy.linalg.norm(nn, axis=1, keepdims=True)
    nn = numpy.where(length > 1e-12, nn / numpy.maximum(length, 1e-12), nn)
    return out, nn


def glb_uvs(uvs):
    """glTF UVs (origin top-left, V down) → COLLADA/BeamNG (V up): `v' = 1 − v`. Torque's DAE
    importer reads texcoords Blender-style; a bundle mesh written as-is renders upside-down
    textures (the standard glTF↔COLLADA flip)."""
    if uvs is None:
        return None
    out = numpy.asarray(uvs, dtype=numpy.float32).copy()
    out[:, 1] = 1.0 - out[:, 1]
    return out


def merge_meshes(name, parts):
    """Concatenate `[(verts, normals, uvs, colors, tris, runs), ...]` into one Mesh with the runs
    re-based; parts without normals/uvs/colors get zeros so the arrays stay rectangular."""
    verts, normals, uvs, colors, tris, runs = [], [], [], [], [], []
    base = 0
    has_n = any(p[1] is not None for p in parts)
    has_uv = any(p[2] is not None for p in parts)
    has_c = any(p[3] is not None for p in parts)
    for v, n, uv, c, t, r in parts:
        v = numpy.asarray(v, dtype=numpy.float32)
        verts.append(v)
        if has_n:
            normals.append(numpy.asarray(n, dtype=numpy.float32) if n is not None else numpy.tile([0, 0, 1], (len(v), 1)).astype(numpy.float32))
        if has_uv:
            uvs.append(numpy.asarray(uv, dtype=numpy.float32) if uv is not None else numpy.zeros((len(v), 2), numpy.float32))
        if has_c:
            colors.append(numpy.asarray(c, dtype=numpy.float32) if c is not None else numpy.ones((len(v), 4), numpy.float32))
        tris.append(numpy.asarray(t, dtype=numpy.int64) + base)
        runs.extend(r)
        base += len(v)
    return Mesh(name, numpy.concatenate(verts) if verts else numpy.zeros((0, 3), numpy.float32),
                numpy.concatenate(tris) if tris else numpy.zeros((0, 3), numpy.int64), _coalesce(runs),
                normals=numpy.concatenate(normals) if normals else None,
                uvs=numpy.concatenate(uvs) if uvs else None,
                colors=numpy.concatenate(colors) if colors else None)


def _coalesce(runs):
    """Adjacent runs of the same material merge; order is preserved (it is the triangle order)."""
    out = []
    for m, c in runs:
        if c <= 0:
            continue
        if out and out[-1][0] == m:
            out[-1] = (m, out[-1][1] + c)
        else:
            out.append((m, c))
    return out


def group_by_material(mesh):
    """Reorder triangles so each material is one contiguous run (fewer `<triangles>` blocks)."""
    if len(mesh.runs) <= len(mesh.materials):
        return mesh
    order = []
    start = 0
    spans = []
    for m, c in mesh.runs:
        spans.append((m, start, start + c))
        start += c
    new_runs = []
    for mat in mesh.materials:
        total = 0
        for m, a, b in spans:
            if m == mat:
                order.append(numpy.arange(a, b))
                total += b - a
        new_runs.append((mat, total))
    idx = numpy.concatenate(order)
    return Mesh(mesh.name, mesh.verts, mesh.tris[idx], new_runs, normals=mesh.normals, uvs=mesh.uvs, colors=mesh.colors)
