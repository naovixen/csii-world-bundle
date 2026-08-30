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

    __slots__ = ("colors", "name", "normals", "runs", "tris", "uvs", "verts")

    def __init__(self, name, verts, tris, runs, normals=None, uvs=None, colors=None):
        self.name = name
        self.verts = numpy.ascontiguousarray(verts, dtype=numpy.float32)
        self.tris = numpy.ascontiguousarray(tris, dtype=numpy.int64)
        self.runs = list(runs)
        self.normals = (
            None if normals is None else numpy.ascontiguousarray(normals, dtype=numpy.float32)
        )
        self.uvs = None if uvs is None else numpy.ascontiguousarray(uvs, dtype=numpy.float32)
        self.colors = (
            None if colors is None else numpy.ascontiguousarray(colors, dtype=numpy.float32)
        )
        if sum(c for _m, c in self.runs) != len(self.tris):
            raise ValueError(
                f"{name}: material runs cover {sum(c for _m, c in self.runs)} triangles, mesh has {len(self.tris)}"
            )

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

    def __init__(
        self, name, levels, collision=(), billboard_px=None, null_px=None, billboard_dimension=None
    ):
        self.name = name
        self.billboard_dimension = billboard_dimension  # imposter atlas cell px (Torque: 64)
        self.levels = list(levels)
        for px, _ms in self.levels:
            if int(px) <= 0:
                raise ValueError(
                    f"{name}: detail size must be > 0 (BeamNG rejects the shape: 'invalid detail levels')"
                )
        self.collision = list(collision)
        self.billboard_px = billboard_px
        self.null_px = null_px


def sanitize(name):
    """A COLLADA id / BeamNG material name: letters, digits, `_`, `-`.

    No `.`: a BeamNG material is a named SimObject, and the level built 2026-08-29 shipped
    `cs2_city_fine_TorreAlvear_LOD.001_3e4b…` — a custom asset's Blender-suffixed LOD material —
    as both the `.dae` material and the `materials.json` entry, byte-identical, and the tower's
    LOD 1 still drew as the engine's blank placeholder (a 72 × 196 m white slab, Naomi's
    "2d square box"). The name is used by the shape and the materials file through this one
    function, so both sides change together.
    """
    out = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name))
    return out or "m"


def write_shape(path, shape):
    """Write `shape` as a `.dae`. Returns `{materials, triangles, vertices, changed}`.

    **Written only when the bytes differ** — the pattern `Level.write_bytes_if_changed` uses for
    textures, and for the same reason one level down. A `.cdae` beside the `.dae` is what the
    game (and `compileMeshes.lua`) loads in preference to parsing the XML, and BeamNG's own
    compiler is built to reuse it: *"keep in mind that existing .cdae will be reused. Delete
    them beforehand if a clean state is required."* Deleting it unconditionally is what made a
    no-op rebuild pay the full 6m26s cook, so the `.cdae` goes **only on the path where the
    `.dae` actually changed** — where keeping it really would ship the old geometry.

    The document is built into a sibling temp file rather than a buffer because the float
    arrays go out through `ndarray.tofile`, which needs a real file handle; that is also what
    keeps a 300k-vertex tile at a couple of seconds.
    """
    meshes = []
    seen_ids = set()
    names_used = {}
    for m in [m for _px, ms in shape.levels for m in ms] + list(shape.collision):
        if id(m) in seen_ids:
            continue  # one geometry however many nodes instance it
        seen_ids.add(id(m))
        base = sanitize(m.name)
        if base in names_used:  # distinct meshes with one name: make the ids unique
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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
        f.write(
            "  <asset>\n    <contributor><authoring_tool>cs2beamng</authoring_tool></contributor>\n"
            '    <unit name="meter" meter="1"/>\n    <up_axis>Z_UP</up_axis>\n  </asset>\n'
        )
        # effects + materials
        f.write("  <library_effects>\n")
        for name in materials:
            sid = sanitize(name)
            f.write(
                f'    <effect id="{sid}-effect"><profile_COMMON><technique sid="common"><phong>'
                '<diffuse><color sid="diffuse">0.8 0.8 0.8 1</color></diffuse></phong></technique></profile_COMMON></effect>\n'
            )
        f.write("  </library_effects>\n  <library_materials>\n")
        for name in materials:
            sid = sanitize(name)
            f.write(
                f'    <material id="{sid}-material" name="{sid}"><instance_effect url="#{sid}-effect"/></material>\n'
            )
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
            # Torque reads a node's user properties out of the FCOLLADA <user_properties> text
            # (`ColladaAppNode`): `bb_dimension` is the imposter atlas CELL in pixels, default
            # 64 - the whole tree in a 256 x 128 atlas (measured), its canopy thinning to
            # nothing at the crossover (Naomi, v30). `bb_includePoles` keeps the top view.
            props = (
                f"<extra><technique profile=\"FCOLLADA\"><user_properties>"
                f"bb_dimension={int(shape.billboard_dimension)};bb_includePoles=1;"
                f"</user_properties></technique></extra>"
                if shape.billboard_dimension
                else ""
            )
            f.write(
                f'        <node id="bb_autobillboard{px}" name="bb_autobillboard{px}" type="NODE">'
                f"{props}</node>\n"
            )
        if shape.null_px is not None:
            px = int(shape.null_px)
            f.write(f'        <node id="nulldetail{px}" name="nulldetail{px}" type="NODE"/>\n')
        f.write('        <node id="start01" name="start01" type="NODE">\n')
        for px, ms in shape.levels:
            for i, m in enumerate(ms):
                # A detail level with several meshes: every node must still END in `_a<px>` (the
                # trailing number is the detail size the loader parses), so the part index goes
                # BEFORE it — `net_3_4_p1_a2`, never `net_3_4_a2_1` (that reads as detail size 1:
                # a second LOD nobody sees).
                node = f"{sanitize(shape.name)}" + (f"_p{i}" if i else "") + f"_a{int(px)}"
                _write_node(f, node, m)
        for i, m in enumerate(shape.collision):
            _write_node(f, f"Colmesh_{sanitize(shape.name)}-{i + 1}", m)
        f.write("        </node>\n      </node>\n    </visual_scene>\n  </library_visual_scenes>\n")
        f.write('  <scene><instance_visual_scene url="#Scene"/></scene>\n</COLLADA>\n')
    changed = _replace_if_changed(tmp, path)
    if changed:
        stale = os.path.splitext(path)[0] + ".cdae"
        if os.path.isfile(stale):
            os.remove(stale)  # this one WOULD ship the old geometry
    return {
        "materials": materials,
        "triangles": tri_total,
        "vertices": vert_total,
        "changed": changed,
    }


def _replace_if_changed(tmp, path):
    """Move `tmp` onto `path`, or drop it when the two hold the same bytes. True if it moved."""
    try:
        if os.path.isfile(path) and os.path.getsize(path) == os.path.getsize(tmp):
            with open(path, "rb") as old, open(tmp, "rb") as new:
                while True:
                    a, b = old.read(1 << 20), new.read(1 << 20)
                    if a != b:
                        break
                    if not a:
                        os.remove(tmp)
                        return False
    except OSError:
        pass
    os.replace(tmp, path)
    return True


def sweep_orphans(directory, keep):
    """Delete the `.dae`/`.cdae` in `directory` whose stem is not in `keep`, and return how many.

    The counterpart of not clearing `art/shapes/` up front any more (`pipeline/worker.py`
    `GENERATED_DIRS`): a shape a previous build wrote and this one does not is still mounted and
    compiled by BeamNG, and a `.cdae` with no `.dae` beside it is a ghost shape. Sweeping at the
    END of the stage that owns the directory keeps every *surviving* file's mtime, which is what
    lets both BeamNG's mesh compiler and its texture cooker reuse their work.
    """
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if name.endswith(".dae.asset.json"):  # the shape's asset companion (imposter config)
            stem, ext = name[: -len(".dae.asset.json")], ".asset"
        else:
            stem, ext = os.path.splitext(name)
        if ext not in (".dae", ".cdae", ".cs", ".asset") or stem in keep:
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    return removed


def _geometry_id(mesh):
    return sanitize(mesh.name) + "-mesh"


def _write_node(f, node_id, mesh):
    gid = _geometry_id(mesh)
    f.write(f'          <node id="{node_id}" name="{node_id}" type="NODE">\n')
    f.write('            <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>\n')
    f.write(
        f'            <instance_geometry url="#{gid}" name="{node_id}">\n              <bind_material><technique_common>\n'
    )
    for name in mesh.materials:
        sid = sanitize(name)
        f.write(
            f'                <instance_material symbol="{sid}-material" target="#{sid}-material">'
            '<bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/></instance_material>\n'
        )
    f.write(
        "              </technique_common></bind_material>\n            </instance_geometry>\n          </node>\n"
    )


def _write_source(f, sid, array, params, fmt):
    n, stride = array.shape
    f.write(
        f'        <source id="{sid}">\n          <float_array id="{sid}-array" count="{n * stride}">'
    )
    array.astype(numpy.float32).ravel().tofile(f, sep=" ", format=fmt)
    f.write(
        "</float_array>\n          <technique_common>\n"
        f'            <accessor source="#{sid}-array" count="{n}" stride="{stride}">\n'
    )
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
    f.write(
        f'        <vertices id="{gid}-vertices"><input semantic="POSITION" source="#{gid}-positions"/></vertices>\n'
    )
    start = 0
    for material, count in mesh.runs:
        sid = sanitize(material)
        f.write(
            f'        <triangles material="{sid}-material" count="{count}">\n'
            f'          <input semantic="VERTEX" source="#{gid}-vertices" offset="0"/>\n'
        )
        if mesh.normals is not None:
            f.write(f'          <input semantic="NORMAL" source="#{gid}-normals" offset="0"/>\n')
        if mesh.uvs is not None:
            f.write(
                f'          <input semantic="TEXCOORD" source="#{gid}-map-0" offset="0" set="0"/>\n'
            )
        if mesh.colors is not None:
            f.write(
                f'          <input semantic="COLOR" source="#{gid}-colors" offset="0" set="0"/>\n'
            )
        f.write("          <p>")
        mesh.tris[start : start + count].ravel().tofile(f, sep=" ", format="%d")
        f.write("</p>\n        </triangles>\n")
        start += count
    f.write("      </mesh>\n    </geometry>\n")


# -- from bundle glb data ---------------------------------------------------------------------------
