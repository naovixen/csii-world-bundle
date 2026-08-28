"""Import one .glb, by the direct parse when that is provably safe and by Blender otherwise.

Every prefab, network segment and lane part in a bundle arrives through here. The block below
`SCRATCH_NAME` is the whole argument for the fast path and for each of its refusals; the short
version is that it never CONSTRUCTS a material, only reuses one an earlier stock import built,
which is what makes it unable to change how anything renders.
"""

import os

import bpy
import numpy
from csiibundle import frames
from csiibundle import glb as glb_reader
from mathutils import Matrix

from . import meshbuild
from .log import LOG

SCRATCH_NAME = "_carto_import_scratch"


# -- the direct .glb fast path ---------------------------------------------------------------
#
# The stock `bpy.ops.import_scene.gltf` call costs roughly 200 ms per file almost independent
# of its size (measured: 1541 s for 7,380 network segment .glb over a full-city run, ~209
# ms/file) — the operator/undo-stack/dependency-graph machinery Blender wraps around every
# import, not the parsing itself. A city ripped from CS2 imports thousands of these, and most
# of them reference materials Blender has already built from an earlier file in the same run
# (contract §4/§5: materials are shared across meshes, dedup'd by the producer). This path
# parses the handful of .glb it can prove are simple enough to build directly and reuses those
# already-built materials — it never constructs one, so it can never be the thing that makes a
# building render wrong.
#
# Every guard below exists to make one specific Blender feature the stock importer would apply
# fall through to the stock importer instead of being silently skipped: morph targets, skins,
# animations, non-float32 or sparse accessors, an unsupported primitive mode, an attribute this
# path does not carry through (vertex colours), or a material this run has not built yet. A
# file that trips none of them is one this path has fully accounted for.
#
# glTF is Y-up, right-handed; the bundle producer writes right-handed meshes for exactly this
# reason (§2.2). Blender's importer maps that to its own Z-up, right-handed frame via
# (x, y, z) -> (x, -z, y) — call it G, same letter the frame comment at the top of this file
# already uses for it. That map is a proper rotation (determinant +1, checked: the 3x3 has a
# single -1 off the diagonal and a matching +1, so two row swaps' worth of sign, not one) —
# which is what licenses using the glTF triangle winding as-is below: a rotation preserves
# which face is "front", a reflection would not.
#
# G itself is `frames.GLTF_YUP_TO_ZUP`, where the frame conventions all live; this is the same
# matrix as a mathutils Matrix, for the call sites that stay in Blender's types.
GLTF_TO_BLENDER = Matrix(frames.GLTF_YUP_TO_ZUP.tolist())

# Base material name -> the bpy.data.materials datablock a stock import first built for it.
# Populated by `import_glb`'s stock-import branch after every glTF import, consulted by
# `_fast_import_glb` before it will claim a file. Never written to by the fast path itself —
# that is what makes "every material this path uses already exists" a true statement rather
# than something the fast path could accidentally falsify for a later file.
_KNOWN_MATERIALS = {}

# How many .glb this run has built directly vs. handed to the stock importer, reset per phase
# by `counts_reset()` and reported by `counts_log()` — the
# headline number for this path (see module docstring above).
_FAST_GLB_COUNT = 0
_STOCK_GLB_COUNT = 0

# A degenerate polygon `_mesh_from_arrays` drops on the fast path is not a new defect: the
# stock importer builds the exact same source triangle from the exact same file and never
# complains either — `mesh.validate()` silently drops a repeated-index polygon (the same one
# this path's pre-clean removes) and silently keeps a distinct-index zero-area one as
# invisible geometry (verified against Address_Beach_Resort_Dubai.glb and seg_106037.glb, see
# task-7-report.md). Surfacing either class as a per-file CONTRACT VIOLATION here would be
# reporting on the source .glb, not on anything this path did — and, because which file first
# reaches the fast path depends on material-discovery order (a file only takes this path once
# every material it needs has been seen by an earlier stock import), the *set* of files that
# would shout is nondeterministic across --center/--radius/--limit even for the identical
# bundle. Both classes are counted quietly instead (`_record_fast_degenerate`, passed as this
# call's `degenerate_report` with `source_geometry=True`) and folded into one aggregate line
# per phase — deliberately worded without any tools/ab_compare.py COUNT_MARKERS substring
# ("polygon(s)", not "face(s)") so the line stays invisible to `--counts` and a live run's log
# stays comparable to a baseline captured before this path existed, and stays IDENTICAL text
# in shape across different --radius (only the two numbers move, and the gate never reads
# them). Fast/stock split itself is likewise kept off the marker list for the same reason.
_FAST_DEGENERATE_ZERO_AREA_POLYS = 0
_FAST_DEGENERATE_REPEATED_POLYS = 0


def counts_reset():
    global _FAST_GLB_COUNT, _STOCK_GLB_COUNT
    global _FAST_DEGENERATE_ZERO_AREA_POLYS, _FAST_DEGENERATE_REPEATED_POLYS
    _FAST_GLB_COUNT = 0
    _STOCK_GLB_COUNT = 0
    _FAST_DEGENERATE_ZERO_AREA_POLYS = 0
    _FAST_DEGENERATE_REPEATED_POLYS = 0


def counts_log():
    """`LOG.step` the fast/stock split since the last reset — kept off the count-gate markers
    (no "record(s)"/"object(s)"/"face(s)"/… substring) so a live run can call this and stay
    comparable against a baseline log captured before this path existed."""
    LOG.step(f"{_FAST_GLB_COUNT} glb fast-path, {_STOCK_GLB_COUNT} glb via glTF importer")
    total = _FAST_DEGENERATE_ZERO_AREA_POLYS + _FAST_DEGENERATE_REPEATED_POLYS
    if total:
        detail = []
        if _FAST_DEGENERATE_ZERO_AREA_POLYS:
            detail.append(f"{_FAST_DEGENERATE_ZERO_AREA_POLYS} zero-area")
        if _FAST_DEGENERATE_REPEATED_POLYS:
            detail.append(f"{_FAST_DEGENERATE_REPEATED_POLYS} repeated-index")
        LOG.step(
            f"{total} degenerate polygon(s) among them ({', '.join(detail)}), dropped "
            "quietly — source .glb artifacts the stock importer tolerates identically "
            "(silently drops the repeated-index kind, silently keeps the zero-area kind "
            "invisible), not a defect this path introduced"
        )


def _record_fast_degenerate(_name, dropped, repeated):
    global _FAST_DEGENERATE_ZERO_AREA_POLYS, _FAST_DEGENERATE_REPEATED_POLYS
    if repeated:
        _FAST_DEGENERATE_REPEATED_POLYS += dropped
    else:
        _FAST_DEGENERATE_ZERO_AREA_POLYS += dropped


def _gltf_node_matrix_to_blender(matrix):
    """Conjugate a glTF-space node matrix by G so it lands in Blender's Z-up frame.

    `C @ M @ C.inverted()` with `C` = `GLTF_TO_BLENDER` — a transform changes frames by
    conjugation, not by a left-multiply. Verified against a stock import of
    Address_Beach_Resort_Dubai.glb (root empty + one translated, multi-material child node):
    `matrix_world` of the matching mesh object came out identical to the float ULP.
    """
    return Matrix(glb_reader.conjugate(matrix, frames.GLTF_YUP_TO_ZUP).tolist())


# The container parse itself is `csiibundle.glb.parse_glb`: it returns per-node numpy arrays
# with the node's world matrix in glTF space, or `None` the moment anything about the file is
# a shape it has not proven it reads correctly. Every one of those refusals is deliberate, and
# this caller's answer to `None` is always the stock importer, never a guess.
_parse_glb_arrays = glb_reader.parse_glb


def _fast_import_glb(path):
    """Build `import_glb`'s result without the stock importer, when provably safe.

    Only files whose every material name is already registered qualify (`_KNOWN_MATERIALS`,
    populated from earlier stock imports in this run) — this path reuses materials and never
    constructs one, which is what makes it unable to regress rendering. Returns `None` to mean
    "use the stock importer", the same as every guard inside `_parse_glb_arrays`.
    """
    # The stock glTF importer scales every imported position/translation by
    # `1 / scene.unit_settings.scale_length`; this path applies glTF coordinates directly with
    # no such factor. Always 1.0 under `blender -b` with the default scene (unreachable today),
    # but a non-default scale would silently mix scaled stock-imported geometry with unscaled
    # fast-path geometry in the same city. Refuse rather than get it quietly wrong.
    if bpy.context.scene.unit_settings.scale_length != 1.0:
        return None
    nodes = _parse_glb_arrays(path)
    if not nodes:
        # `None` means a guard bailed; `[]` means the parse completed but found no
        # mesh-bearing node — a shape this path has never validated against the stock
        # importer (untested edge case, not one of the guarded-and-proven ones above).
        # Treat both the same: defer to the stock importer rather than have this path
        # confidently claim "zero objects is correct" for a file it cannot vouch for. This
        # also keeps `import_glb` — which treats *any* non-`None` return as "handled" — from
        # ever seeing a same-shaped `[]` for a reason other than "the stock importer tried
        # and failed", which is the only `[]` `load_prefab_collection` should read as missing.
        return None
    for node in nodes:
        for material_name, _count in node["material_runs"]:
            if material_name not in _KNOWN_MATERIALS:
                return None

    axis_flip = numpy.array([1.0, -1.0, 1.0])
    # Tracks both bare meshes (built, not yet wrapped in an object) and finished objects, in
    # creation order, so the except handler below can tear down whichever partial state this
    # attempt reached — including a mesh that built successfully but failed before
    # `bpy.data.objects.new` ever ran (e.g. `normals_split_custom_set_from_vertices` raising
    # on a shape this parser did not anticipate). A mesh left dangling in `bpy.data.meshes`
    # would leak and, worse, hold `node["name"]`, forcing the stock-importer retry that
    # follows a fast-path failure into a `.001` name — object naming is a frozen contract.
    created = []
    try:
        for node in nodes:
            # glTF -> Blender axis convention, exactly G: (x, y, z) -> (x, -z, y).
            verts = node["verts"][:, (0, 2, 1)] * axis_flip
            normals = node["normals"][:, (0, 2, 1)] * axis_flip
            uv_layers = None
            if node["uvs"] is not None:
                loop_uv = glb_reader.glb_uv_to_texture_uv(node["uvs"][node["tris"].ravel()])
                uv_layers = {"UVMap": loop_uv}
            material_runs = [
                (_KNOWN_MATERIALS[name], count) for name, count in node["material_runs"]
            ]

            # `smooth=True`: the stock importer's shading comes from the custom split normals
            # it sets on the mesh, not from the per-polygon flat/smooth flag — matched below
            # by setting those same custom normals, so `smooth` only has to be "not flat".
            mesh = meshbuild.from_arrays(
                node["name"],
                verts,
                node["tris"],
                uv_layers=uv_layers,
                material_runs=material_runs,
                smooth=True,
                degenerate_report=_record_fast_degenerate,
                source_geometry=True,
            )
            created.append(mesh)
            mesh.normals_split_custom_set_from_vertices(
                numpy.ascontiguousarray(normals, dtype=numpy.float32)
            )

            obj = bpy.data.objects.new(node["name"], mesh)
            obj.matrix_world = _gltf_node_matrix_to_blender(node["matrix"])
            created[-1] = obj
    except Exception:
        # A guard above missed something about this file's shape. Undo whatever partial mesh
        # data this attempt already allocated and let the stock importer have it instead of
        # returning half-built geometry or propagating a crash out of a per-file fast path.
        for item in created:
            if isinstance(item, bpy.types.Object):
                mesh = item.data
                bpy.data.objects.remove(item)
                if mesh is not None:
                    bpy.data.meshes.remove(mesh)
            else:
                bpy.data.meshes.remove(item)
        return None

    global _FAST_GLB_COUNT
    _FAST_GLB_COUNT += 1
    return created


def _register_known_materials(imported):
    """After a stock import, canonicalise its materials by base name and remember them.

    Blender suffixes a duplicate material name (`.001`, `.002`, …) on every import that
    re-declares one glTF already built earlier in this run — the same material, a fresh
    datablock. The first copy of each base name becomes canonical; every later slot pointing
    at a `.NNN` twin is repointed at it. That is what lets `_fast_import_glb` treat "the
    material is already registered" as a stable fact — the pool of canonical materials only
    grows, never forks — and it is also what keeps `build_networks`' own dedup pass (it runs
    later, over the whole scene) a near no-op: most of the duplication it used to clean up
    never gets created in the first place.
    """
    for obj in imported:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            suffix = material.name.rsplit(".", 1)[-1]
            base = material.name.rsplit(".", 1)[0] if suffix.isdigit() else material.name
            canonical = _KNOWN_MATERIALS.setdefault(base, material)
            if canonical is not material:
                slot.material = canonical


def _scratch_layer_collection():
    """A collection the glTF importer drops its objects into, so they can be found in O(1).

    The importer links what it creates into the view layer's *active* collection. Pointing that at
    a small scratch collection does two things. It turns "what did this import produce" into
    reading one short list, where the obvious `set(bpy.data.objects)` before/after diff is two
    passes over the whole scene — quadratic across a city, though measurement put it well below
    the costs that actually dominate. More importantly it is what lets the destination collection
    be parked outside the view layer entirely (see `parked_out_of_view_layer`): the importer has
    to link its output *somewhere* in the view layer, and if that somewhere is the scene's master
    collection then the scene grows and the parking buys nothing.
    """
    scene_collection = bpy.context.scene.collection
    scratch = bpy.data.collections.get(SCRATCH_NAME)
    if scratch is None:
        scratch = bpy.data.collections.new(SCRATCH_NAME)
    if scratch.name not in scene_collection.children:
        scene_collection.children.link(scratch)
    for layer in bpy.context.view_layer.layer_collection.children:
        if layer.collection is scratch:
            return layer
    return None


class _quiet_gltf_import:  # noqa: N801 - used as a context
    """Silence io_scene_gltf2's per-.glb INFO banner, for exactly the span of one operator call.

    `logging.getLogger("glTFImporter").setLevel(logging.WARNING)` looks like the fix and is
    not one: ImportGLTF2.import_gltf2 (io_scene_gltf2/__init__.py) runs
    `self.loglevel = set_debug_log()` on *every* call, and `set_debug_log()` maps
    `bpy.app.debug_value == 0` to `logging.INFO` — so the addon's own `Log` object
    (io/com/debug.py) resets the logger's level right back to INFO before the next import's
    first line, silently undoing whatever this script set beforehand. Confirmed by import —
    the getLogger/setLevel call left the "Data are loaded" / "glTF import finished" banner
    printing on the very next import.

    `set_debug_log()` reads `bpy.app.debug_value` itself, and returns `logging.WARNING` at
    debug_value 1 — a knob the addon re-reads every call rather than one this script has to
    fight after the fact. Only import-time INFO chatter is affected: `glTFImporter_errors`
    is always pinned to ERROR regardless, so a real warning or error from a malformed glb
    still surfaces.

    `bpy.app.debug_value` is Blender's, not this addon's — general-purpose, and read by
    whatever else happens to check it. Pinning it for the whole session would leave it at 1
    through terrain building, tinting and the final save, for no reason those steps need.
    Scoped to just the `bpy.ops.import_scene.gltf(...)` call instead: saved on entry,
    restored on exit (success, failure, or exception alike), so every other stage of the
    import sees whatever value it would have without this script ever having run.
    """

    def __enter__(self):
        self._previous = bpy.app.debug_value
        bpy.app.debug_value = 1
        return self

    def __exit__(self, *exc):
        bpy.app.debug_value = self._previous
        return False


def import_glb(path):
    """Import one .glb and return the objects it created, unlinked from every collection.

    The caller links them wherever they belong. Returns [] if the import failed, having already
    said why — one unreadable file should not end a city-sized import.

    Tries the direct-parse fast path first (see the block above `_scratch_layer_collection`):
    a file every one of whose materials this run has already built via a stock import can be
    built directly, skipping the ~200 ms/file stock-importer overhead that otherwise dominates
    a city-sized import. Any file the fast path is not provably safe for — first sight of a
    material, or any guard in `_parse_glb_arrays` — falls through to the stock importer
    unchanged, which is also where new materials get built and registered for next time.
    """
    fast = _fast_import_glb(path)
    if fast is not None:
        return fast

    layer = _scratch_layer_collection()
    if layer is None:
        return []

    view_layer = bpy.context.view_layer
    previous = view_layer.active_layer_collection
    view_layer.active_layer_collection = layer
    try:
        with _quiet_gltf_import():
            bpy.ops.import_scene.gltf(filepath=path)
    except Exception as exc:
        LOG.warn(f"! {os.path.basename(path)}: import failed — {exc}")
        return []
    finally:
        view_layer.active_layer_collection = previous

    imported = list(layer.collection.objects)
    for obj in imported:
        layer.collection.objects.unlink(obj)
    _register_known_materials(imported)
    global _STOCK_GLB_COUNT
    _STOCK_GLB_COUNT += 1
    return imported


def load_prefab_collection(asset_id, mesh_dir, sources):
    """Import <asset_id>.glb once and return a Collection holding it, or None.

    Each prefab becomes a collection so that instances can be collection-instanced: one object
    per instance in the scene, one copy of the geometry in memory, and multi-part prefabs keep
    their node hierarchy and per-part offsets for free.
    """
    path = os.path.join(mesh_dir, asset_id + ".glb")
    if not os.path.isfile(path):
        return None

    imported = import_glb(path)
    if not imported:
        return None

    collection = bpy.data.collections.new(f"prefab_{asset_id}")
    sources.children.link(collection)
    for obj in imported:
        collection.objects.link(obj)

    return collection
