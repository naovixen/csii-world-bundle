"""Build a Blender mesh datablock from numpy arrays.

The hot path of every layer this adapter reconstructs: terrain grids, bent lane parts, the
direct .glb fast path and the network ribbons all arrive as arrays and land here. The
degenerate-face policy — what a dropped face means, and which class of drop is allowed to be
quiet — is the whole of the docstring below, and it is the reason this is one function rather
than a `from_pydata` call at each site.
"""

import bpy
import numpy
from csiibundle import glb as glb_reader

from .log import LOG


def from_arrays(
    name,
    verts,
    tris,
    uv_layers=None,
    material_runs=None,
    poly_size=3,
    smooth=True,
    degenerate_report=None,
    source_geometry=False,
    poly_sizes=None,
):
    """Build a mesh from numpy arrays via foreach_set — the hot-path alternative to
    per-face `from_pydata` / per-loop attribute assignment.

    `verts` is `(N, 3)` float64 already in Blender frame (X east, Y north, Z up). `tris` is
    `(M, poly_size)` int, one row per polygon in loop order. `poly_size` defaults to 3
    (triangles); a caller that already has same-diagonal quads — the terrain grid — passes 4,
    so its faces tessellate exactly as `from_pydata` quads always did instead of picking a
    fresh triangulation diagonal that could flip a slope's silhouette under the pixel A/B
    gate. `uv_layers` is `{layer_name: (M*poly_size, 2)}` in the same loop order.

    `poly_sizes`, when given, replaces the uniform `poly_size`: `tris` is then a FLAT 1-D
    array of loop vertex indices and `poly_sizes` is `(M,)` per-polygon loop counts (their sum
    must equal `len(tris)`). This is how the terrain grid mixes 14 m quads, native 3.5 m
    quads and transition-fan triangles in one mesh. All degenerate-face semantics below apply
    unchanged; UV layers are per-loop in the same flat order.
    `material_runs` is `[(material, poly_count)]` consumed in polygon order; a `None`
    material keeps the default slot 0. `smooth` sets `use_smooth` on every polygon — there is
    no safe default across callers (the game's continuous heightfields want it on; faceted
    geometry such as lane decals must pass `smooth=False` explicitly), so every call site
    states its own choice rather than relying on this default.

    A row in `tris` with a repeated vertex index, or one that is geometrically zero-area
    (distinct indices that still collapse to a line or point — e.g. two part-seam vertices
    authored at the same position), is dropped BEFORE the mesh is built, with a matching
    trim of `material_runs` and `uv_layers` so the survivors keep the material/UV they were
    given. This is deliberately not left to `mesh.validate()`: validate() drops bad polygons
    positionally, and a caller has no way to tell afterward which of ITS polygons a run or a
    UV block was meant for, so re-zipping runs/UVs onto whatever validate() kept would
    silently mis-assign materials and UVs rather than fail loudly. Concatenated per-part
    arrays (lane meshes built from bent segments) are exactly the shape most likely to
    contain one of these at a seam. If `mesh.validate()` still disagrees with the polygon
    count after this pre-clean, that means it rejected something this check does not know
    about — raises rather than silently building a mis-aligned mesh.

    By default every drop is reported as `CONTRACT VIOLATION` — the wording is deliberately
    alarming, because for most callers (terrain, water, lanes) a degenerate face means the
    producer's MATH handed over bad geometry: `verts`/`tris` are numbers this harness just
    computed (a bend, a grid, a drape), so a zero-length edge means that computation broke.
    `source_geometry=True` says the opposite: `verts`/`tris` were read verbatim off a file on
    disk (the direct .glb fast path) — a degenerate triangle there is a faithful rip of
    whatever the game's own mesh already contained, not something this harness introduced,
    and the stock glTF importer builds the identical triangle from the identical file without
    complaint (`mesh.validate()` either drops the same repeated-index polygon or keeps the
    same zero-area one — either way the fast path isn't what put it there). The two ways a
    face can be degenerate are NOT equally diagnostic, and are always reported separately:

    - **Geometrically zero-area with distinct indices** (three different vertices that
      happen to be collinear or coincident) has one well-understood, non-defect source in
      COMPUTED lane geometry: a heavily tiled part's last tile can clip a whole cross-section
      to a single arc position (see `build_lanes`), producing a zero-area strip that is
      neither a bundle defect nor specific to any one instance. Any caller — computed or
      source — that has already established its drops are of that kind can pass
      `degenerate_report(name, zero_area_count, False)` to receive JUST this count instead of
      a per-call `CONTRACT VIOLATION` line, and aggregate/report it its own way; passing
      nothing (the default) keeps the alarming wording for this class too.
    - A **repeated vertex index** within one polygon's row is the classic sign of bad DATA
      (a genuinely zero-length edge). For a COMPUTED caller (`source_geometry=False`, the
      default — terrain, water, lanes) this means the bend/grid/drape math itself broke, so
      it ALWAYS gets the loud `CONTRACT VIOLATION` line, unconditionally — `degenerate_report`
      has no say over this class, by construction, so a future regression in computed geometry
      can never be swallowed by a caller's soft aggregate. Only a `source_geometry=True`
      caller with a `degenerate_report` may downgrade this class too, via
      `degenerate_report(name, repeated_count, True)` — because for that caller the defect (if
      any) is in the ripped file, not in anything this run computed.
    """
    verts = numpy.asarray(verts, dtype=numpy.float64)
    if verts.size == 0:
        verts = verts.reshape(0, 3)
    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError(
            f"mesh.from_arrays({name!r}): verts must be (N, 3), got shape {verts.shape}"
        )

    if poly_sizes is None:
        tris = numpy.asarray(tris, dtype=numpy.int64)
        if tris.size == 0:
            tris = tris.reshape(0, poly_size)
        if tris.ndim != 2 or tris.shape[1] != poly_size:
            raise ValueError(
                f"mesh.from_arrays({name!r}): tris must be (M, {poly_size}), got shape {tris.shape}"
            )
        loops_flat = tris.ravel()
        sizes = numpy.full(len(tris), poly_size, dtype=numpy.int64)
    else:
        loops_flat = numpy.asarray(tris, dtype=numpy.int64).ravel()
        sizes = numpy.asarray(poly_sizes, dtype=numpy.int64)
        if int(sizes.sum()) != len(loops_flat):
            raise ValueError(
                f"mesh.from_arrays({name!r}): poly_sizes sum "
                f"{int(sizes.sum())} != {len(loops_flat)} flat loop indices"
            )

    # The drop itself is `csiibundle.glb.clean_degenerate_polygons` — engine-neutral, and it
    # trims `material_runs` and the UV layers in the same pass so the survivors keep what they
    # were given. What stays here is the REPORTING, because what a degenerate face means
    # depends on where the arrays came from and only this caller knows that.
    (loops_flat, sizes, uv_layers, material_runs, repeated_count, zero_area_count) = (
        glb_reader.clean_degenerate_polygons(
            verts, loops_flat, sizes, uv_layers=uv_layers, material_runs=material_runs
        )
    )
    poly_count = len(sizes)

    if repeated_count:
        # Downgradable ONLY for a source-geometry caller with its own aggregate report — every
        # computed caller (the default) gets the unconditional loud line, by construction: a
        # repeated index in COMPUTED math is historically real bad data, and no caller can opt
        # that class out of the alarm for itself.
        if source_geometry and degenerate_report is not None:
            degenerate_report(name, repeated_count, True)
        else:
            LOG.warn(
                f"CONTRACT VIOLATION: {name} array input had {repeated_count} "
                "face(s) with a repeated vertex index; dropping before build so "
                "materials/UVs stay aligned to the surviving polygons"
            )
    if zero_area_count:
        if degenerate_report is not None:
            degenerate_report(name, zero_area_count, False)
        else:
            LOG.warn(
                f"CONTRACT VIOLATION: {name} array input had {zero_area_count} "
                "geometrically zero-area face(s) (distinct indices collapsed to "
                "a line or point); dropping before build so materials/UVs stay "
                "aligned to the surviving polygons"
            )

    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    mesh.loops.add(len(loops_flat))
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(loops_flat, dtype=numpy.int32))
    mesh.polygons.add(poly_count)
    loop_start = (
        numpy.concatenate(([0], numpy.cumsum(sizes)[:-1])).astype(numpy.int32)
        if poly_count
        else numpy.zeros(0, dtype=numpy.int32)
    )
    mesh.polygons.foreach_set("loop_start", loop_start)
    mesh.polygons.foreach_set("loop_total", numpy.ascontiguousarray(sizes, dtype=numpy.int32))
    if material_runs:
        indices = numpy.empty(poly_count, dtype=numpy.int32)
        slots = {}
        cursor = 0
        for material, count in material_runs:
            if material is not None and material.name not in slots:
                slots[material.name] = len(mesh.materials)
                mesh.materials.append(material)
            indices[cursor : cursor + count] = slots[material.name] if material is not None else 0
            cursor += count
        mesh.polygons.foreach_set("material_index", indices)
    for layer_name, uvs in (uv_layers or {}).items():
        layer = mesh.uv_layers.new(name=layer_name)
        layer.data.foreach_set("uv", numpy.ascontiguousarray(uvs, dtype=numpy.float32).ravel())
    mesh.update()
    mesh.validate()
    if len(mesh.polygons) != poly_count:
        # The pre-clean above is supposed to make this unreachable. Surfacing it as a hard
        # error rather than silently building a mesh whose materials/UVs no longer line up
        # with whatever validate() decided to keep. Remove the mesh datablock before raising —
        # it was never returned to the caller, so nothing else can hold or clean up a
        # reference to it, and leaving it in `bpy.data.meshes` would both leak memory and,
        # for a caller re-trying under this same `name` (the stock-importer fallback after a
        # fast-path failure), steal the name out from under the retry's own datablock,
        # forcing it into a `.001` suffix — object/mesh naming is a frozen bundle contract.
        dropped_extra = poly_count - len(mesh.polygons)
        bpy.data.meshes.remove(mesh)
        raise RuntimeError(
            f"mesh.from_arrays({name!r}): mesh.validate() dropped "
            f"{dropped_extra} more face(s) after the degenerate-face "
            "pre-clean — it rejected something this check does not detect"
        )
    mesh.polygons.foreach_set("use_smooth", numpy.full(poly_count, smooth, dtype=bool))
    return mesh
