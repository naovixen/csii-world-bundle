"""Place the §8 terrain under everything: the ground grid, its materials, and the water surface.

The terrain is the ground everything else stands on, with the road-flattened cells recorded in
surface_index.png. (It is NOT what closes junctions — the arms' node meshes pave those; audit
§18 corrected an earlier claim here.)

The rasters are decoded in plain Python rather than through Blender's image loader so that no
colour-management transform can touch the samples — they carry NUMBERS, not pictures. That
decoder is `csiibundle.png`, where it also serves as a reference implementation of §8's file
format for any consumer to port.

The clip rasterisation, the corridor lattice, the hole-rim snap and the shallow-band clamp are
`csiibundle.clipraster`/`containment`/`clipgrid`/`carriageway`. Read their docstrings before
touching any of them — the containment invariant (only a DEEP band may carve; a shallow one
flattens and never deletes), the sub-sample deletion rule and the sideways-only rim snap are
each a correction this project shipped the wrong way round at least once.
"""

import math
import os

import bpy
import numpy
from csiibundle import clipgrid, reader, terrainclip, water
from csiibundle import lattice as lattice_math
from csiibundle import png as png_reader
from csiibundle import splat as splat_reader
from csiibundle import watersurface as watersurface_math

from . import meshbuild, shadernodes, splatground, terrainreport
from .log import LOG


def _load_field(bundle_dir, rasters, coarse):
    """The heightfield to mesh, and the log line saying which raster it is.

    The fine 1.75 m `terrainFine` raster when the bundle carries it — the game's near-camera
    ground, its road grading rasterised at that texel size — else the 3.5 m base map (§8,
    `reader.load_heightfield`). Everything downstream is in cells of whichever it is; the 3.5 m
    companions (roadbed ids, water level) are read onto that grid by nearest sample.
    """
    field = reader.load_heightfield(bundle_dir, prefer_fine=not coarse)
    height, width = field.raw.shape
    cell = field.cell
    base_size = tuple(int(v) for v in rasters["size"])
    if field.fine:
        LOG.step(
            f"terrain: meshing the fine {width}x{height} raster @ {cell:.3f} m "
            f"(layers.terrainFine — the game's near-camera ground); the "
            f"{base_size[0]}x{base_size[1]} base map stays the reference for surface ids and water"
        )
    elif coarse:
        LOG.step(
            f"terrain: meshing the {width}x{height} base raster @ {cell:.3f} m "
            "(--coarse-terrain: the fine raster, if any, is deliberately ignored)"
        )
    else:
        LOG.step(
            f"terrain: meshing the {width}x{height} base raster @ {cell:.3f} m (no "
            "layers.terrainFine in this rip — the game's finer near-camera grading is not "
            "available; kerb seams sit within a 3.5 m texel)"
        )
    return field, base_size


def _companion_raster(path, base_size, shape, label):
    """One 3.5 m companion raster (surface ids, water level) read onto the meshed grid."""
    if not os.path.isfile(path):
        return None
    height, width = shape
    rw, rh, values = png_reader.decode_png_gray(path)
    if (rw, rh) != base_size:
        LOG.warn(
            f"CONTRACT VIOLATION: {label} is {rw}x{rh}, heightmap {base_size[0]}x{base_size[1]}"
        )
        return None
    if (rw, rh) != (width, height):
        return reader.resample_nearest(
            numpy.asarray(values).reshape(rh, rw), (height, width)
        ).reshape(-1)
    return values


def _window(center, radius, field, shape):
    """The crop window in raster indices, plus the sample stride. Row 0 is the northernmost
    (§3: rows toward -Z). Without a region the coarse grid keeps at most ~1024 blocks per axis
    and the decision corridors are meshed at native resolution inside it."""
    height, width = shape
    cell, origin_x, origin_z = field.cell, field.origin_x, field.origin_z
    if center is None or radius is None:
        return 0, width - 1, 0, height - 1, max(1, (max(width, height) + 1023) // 1024)
    margin = 2.0 * cell
    col0 = max(0, int((center[0] - radius - margin - origin_x) / cell))
    col1 = min(width - 1, int(math.ceil((center[0] + radius + margin - origin_x) / cell)))
    row0 = max(0, int((origin_z - (center[1] + radius + margin)) / cell))
    row1 = min(height - 1, int(math.ceil((origin_z - (center[1] - radius - margin)) / cell)))
    return col0, col1, row0, row1, 1


def _read_splat(rasters, splat_path):
    """The splatmap and its channel mapping, or `(None, None, None)`.

    terrain/splatmap.png is the 4096² weight map the terrain shader samples per pixel, and
    the rasters "splatmap" block names which ground each channel weights (contract §8). It
    is read, not calibrated: a calibration used to run here and got three of four channels
    wrong — see `splatground.CHANNELS_DEFAULT` for exactly which and why.
    """
    splat_block = rasters.get("splatmap") if isinstance(rasters, dict) else None
    if not splat_block or not os.path.isfile(splat_path):
        return None, None, None
    try:
        splat = splatground.Splat(splat_path, splat_block)
    except Exception as error:
        LOG.warn(f"splatmap unreadable ({error}); falling back to per-face ground painting")
        return None, None, None
    mapping, published = splat_reader.channel_mapping(splat_block)
    named = ", ".join(f"{key}={mapping[key] or 'unknown'}" for key in "RGBA")
    source = "published by the bundle" if published else "assumed"
    LOG.step(
        f"splatmap channels {source}: "
        f"{named}, base={mapping.get('base') or 'none'} (the residual of the first channel)"
    )
    return splat, mapping, splat_block


def _per_class_runs(face_class, surfaces_dir):
    """The legacy fallback: one flat material per face class, run-length encoded.

    The array is already spatially coherent (grass/dirt/rock form contiguous runs along each
    sampled row), so this stays a handful of runs, not one per face, while giving every face the
    same material assignment the scalar `face.material_index = (0, 1, 2)[cls]` walk did.
    """
    grass = shadernodes.ground_material(
        "terrain_natural", os.path.join(surfaces_dir, "grass.png"), (0.20, 0.30, 0.12, 1.0)
    )
    dirt = shadernodes.ground_material(
        "terrain_roadbed", os.path.join(surfaces_dir, "dirt.png"), (0.35, 0.28, 0.20, 1.0)
    )
    rock = shadernodes.ground_material(
        "terrain_rock", os.path.join(surfaces_dir, "rock.png"), (0.30, 0.28, 0.26, 1.0)
    )
    by_class = (grass, dirt, rock)
    if not len(face_class):
        return []
    change = numpy.nonzero(numpy.diff(face_class) != 0)[0] + 1
    run_starts = numpy.concatenate(([0], change))
    run_ends = numpy.concatenate((change, [len(face_class)]))
    return [
        (by_class[int(face_class[start])], int(end - start))
        for start, end in zip(run_starts, run_ends)
    ]


def _ground_surface(lattice, rasters, splat_path, surfaces_dir):
    """`(uv_layers, material_runs)` for the ground grid — the splat blend, or the fallback.

    World-anchored UVs so the ground textures tile at a plausible density without any per-face
    unwrap. Per-LOOP, in the same flat loop order as `loops` (coarse quads, refined quads, fan
    triangles). Two layers, deliberately: `UVMap` keeps the legacy 8 m-per-repeat convention the
    per-class fallback materials are built against, and `WorldUV` is plain world METRES, which
    the splat material scales per ground from the shipped `terrainTiling` — one ground tiles at
    14.34 m and another at 68.27 m, so a single baked-in rate cannot serve both.
    """
    loop_x = lattice.verts[lattice.loops, 0]
    loop_z = lattice.verts[lattice.loops, 1]
    uv_layers = {"UVMap": numpy.stack((loop_x / 8.0, loop_z / 8.0), axis=-1)}

    splat, mapping, _block = _read_splat(rasters, splat_path)
    if mapping is None:
        return uv_layers, _per_class_runs(lattice.face_class, surfaces_dir)

    uv_layers["WorldUV"] = numpy.stack((loop_x, loop_z), axis=-1)
    tiling_block = rasters.get("terrainTiling") if isinstance(rasters, dict) else None
    terrainreport.tiling(tiling_block)
    material_runs = [
        (
            splatground.ground_material(surfaces_dir, splat, mapping, tiling_block),
            len(lattice.sizes),
        )
    ]
    # No face overrides on the blend. The roadbed keeps whatever the splat says, as in
    # game — the blend has no roadbed response at all, and the roadbed LOOK is carried by
    # the ~13%-alpha wear decals and surface areas drawn on top (the sink still applies).
    # Cliffs need no slope override either: where the dirt weight falls away the rock base
    # shows through, which is what keeps gorge walls smooth instead of the per-face
    # checkerboard.

    # The splat blend samples in its own geo-referenced UV space.
    uv_layers["SplatUV"] = numpy.stack(splat.uv(loop_x, loop_z), axis=-1)
    return uv_layers, material_runs


def _ground_object(lattice, uv_layers, material_runs, collection):
    """The ground grid as one smooth-shaded object.

    Smooth-shade the grid: the game's terrain is a continuous heightfield, and a flat-shaded
    quad is uniformly lit — at the full-city coarse blocks that reads as a brightness
    checkerboard over every slope, squares no texture blend can hide. The shared lattice makes
    the smoothing correct: every seam's normal averages both sides.

    Snapping two neighbouring boundary vertices onto the same point of a prism side collapses
    their shared sliver face to zero area — a legitimate consequence of closing the hole
    exactly, not broken math, so it gets a calm aggregate rather than the CONTRACT VIOLATION
    wording (repeated-index faces still alarm loudly, by design).
    """
    degenerates = {"faces": 0}

    def report(_name, count, repeated):
        assert not repeated
        degenerates["faces"] += count

    mesh = meshbuild.from_arrays(
        "bundle_terrain",
        lattice.verts,
        lattice.loops,
        uv_layers=uv_layers,
        material_runs=material_runs,
        poly_sizes=lattice.sizes,
        smooth=True,
        degenerate_report=report,
    )
    if degenerates["faces"]:
        LOG.step(
            f"{degenerates['faces']} sliver face(s) collapsed by the hole-boundary snap and dropped"
        )
    terrain_object = bpy.data.objects.new("bundle_terrain", mesh)
    collection.objects.link(terrain_object)
    return terrain_object


def _water_material(bundle_dir):
    """The ocean's appearance, AUTHORED and shipped — `rasters.water` carries
    WaterRenderSettingsPrefab verbatim. It used to be three hard-coded numbers here, which is
    how the sea ended up the wrong colour and with nothing to reflect. Same block the swimming
    pools use, because in the game it is the same shader."""
    appearance = water.load(bundle_dir)
    material = bpy.data.materials.new("terrain_water")
    shadernodes.ensure_nodes(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = appearance.base_color
    bsdf.inputs["Roughness"].default_value = appearance.roughness
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = water.WATER_IOR
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 1.0
    else:
        bsdf.inputs["Alpha"].default_value = appearance.surface_alpha()
        shadernodes.set_alpha_blend(material)
    if appearance.present:
        LOG.step(
            f"water:     {appearance.prefab} — colour "
            f"{tuple(round(c, 3) for c in appearance.base_color[:3])}, roughness "
            f"{appearance.roughness:.2f} near / {appearance.roughness_distant:.2f} far, "
            f"absorption {appearance.absorption_distance:g} m"
        )
    return material


# The water raster is the SURFACE LEVEL in the heightmap's encoding (§8, 0 = dry): water
# exists where level > ground. A face is placed wherever any corner is wet, decided per
# face family on the same lattice as the ground — native cells inside refined blocks
# (the shoreline is a corridor, so the water boundary steps at native resolution, not at
# the coarse block size), fans in transition blocks, coarse quads in the open water. The
# level extends one sim cell past the shoreline (producer dilation), so the surface runs
# flat into the bank and the ground cuts the shoreline where the game's own shoreline is.
# The wet-face rules — the dilated shoreline ring, the ground-seated outer corners, and
# water NOT being filtered by the terrain cut — are `csiibundle.watersurface`.
# NOT named `water` — that is the `csiibundle.water` module, and shadowing it here is a
# bug this file has already made once.
def _water_object(bundle_dir, lattice, stride, grid, window, collection):
    """The water surface, or `(None, 0)` where nothing is wet."""
    water_mesh = watersurface_math.water_surface(lattice, stride, grid, window)
    if water_mesh is None:
        return None, 0
    wet_faces = water_mesh.faces
    material = _water_material(bundle_dir)
    mesh = meshbuild.from_arrays(
        "bundle_water",
        water_mesh.verts,
        water_mesh.loops,
        material_runs=[(material, wet_faces)],
        poly_sizes=water_mesh.sizes,
        smooth=False,
    )
    water_object = bpy.data.objects.new("bundle_water", mesh)
    collection.objects.link(water_object)
    return water_object, wet_faces


def _build_lattice(bundle_dir, field, surface_ids, levels, window, stride, clip_floor):
    """The corridor lattice and the prisms it was cut against, or `(None, None, 0)`.

    `csiibundle.lattice.build_lattice` is the whole of it: the corridor that decides where the
    mesh goes native, the three face families welded onto one vertex set, the containment
    deletion — which is now the EXACT cut against the clip prisms rather than a score.
    """
    height, width = field.raw.shape
    heights = field.raw.reshape(-1)
    grid = clipgrid.Grid(
        width,
        height,
        field.cell,
        field.origin_x,
        field.origin_z,
        field.elevation_scale,
        field.elevation_offset,
    )
    # Contract §8c. The terrain covering tunnel portals and sunken trenches is never carved out
    # of the heightmap — the game hides it per-frame with a stencil pass — so the clip volumes
    # are rasterised here against this heightmap. The layer is mandatory; clip_grid raises on
    # a bundle without it.
    clip = clipgrid.clip_grid(bundle_dir, grid, heights, floor_override=clip_floor)
    # The prisms themselves, for the EXACT cut (audit §46): the game hides terrain by discarding
    # the pixels inside its clip volume, so the hole's boundary is the prism's own surface and
    # the mesh is CUT on it — `csiibundle.terrainclip`, shared with the heightfield bake.
    prisms, shallow_skipped = terrainclip.load_prisms(bundle_dir, field.cell)
    lattice = lattice_math.build_lattice(
        heights, surface_ids, levels, grid, window, stride, clip, prisms=prisms
    )
    return lattice, grid, shallow_skipped


def build_terrain(bundle_dir, collection, center, radius, clip_floor=None, coarse=False):
    """Place the §8 terrain under everything: ground grid, roadbed split, water surface.

    Returns `(placed, ground)` — the number of objects placed (0 when the layer is absent)
    and the ground grid itself as `(object, cell_size)` for the layers painted onto it
    (`areabuild`), or None when no grid was built. The region filter crops the grid to the
    same window as everything else. `clip_floor` overrides every clip strip's `bottom`
    (diagnostic, `--clip-floor`).
    """
    paths = reader.terrain_paths(bundle_dir)
    if not os.path.isfile(paths.heightmap):
        LOG.warn(f"no terrain layer at {paths.heightmap} — skipping terrain")
        return 0, None
    if paths.rasters is None:
        LOG.warn(
            "terrain rasters present but no geo-reference (bundle.json rasters or "
            "terrain/rasters.json) — skipping terrain"
        )
        return 0, None

    field, base_size = _load_field(bundle_dir, paths.rasters, coarse)
    shape = field.raw.shape
    surface_ids = _companion_raster(paths.surface_index, base_size, shape, "surface_index")
    levels = _companion_raster(paths.water, base_size, shape, "water_level")

    col0, col1, row0, row1, stride = _window(center, radius, field, shape)
    if col0 >= col1 or row0 >= row1:
        LOG.warn("terrain: the region window misses the raster entirely — skipping terrain")
        return 0, None
    window = (row0, col0, row1, col1)

    lattice, grid, shallow_skipped = _build_lattice(
        bundle_dir, field, surface_ids, levels, window, stride, clip_floor
    )
    if lattice is None:
        LOG.warn("terrain: the region window is smaller than one face — skipping terrain")
        return 0, None
    if lattice.stray_cells:
        LOG.warn(
            f"terrain: {lattice.stray_cells} cut cell(s) escaped the refine corridor — "
            "coarse faces may wall off part of a tunnel"
        )
    for row_number in range(lattice.lat_h):
        # "terrain rows" — deliberately not "terrain:" — stays outside every ab_compare.py count
        # marker so this progress line can print at whatever resolution the lattice has without
        # ever becoming a gated line the A/B baseline does not have.
        LOG.progress("terrain rows", row_number + 1, lattice.lat_h)

    uv_layers, material_runs = _ground_surface(
        lattice, paths.rasters, paths.splatmap, paths.surfaces
    )
    terrain_object = _ground_object(lattice, uv_layers, material_runs, collection)
    water_object, wet_faces = _water_object(bundle_dir, lattice, stride, grid, window, collection)
    terrainreport.summary(lattice, shape, field.cell, stride, wet_faces, shallow_skipped)
    return (1 if water_object is None else 2), (terrain_object, field.cell * stride)
