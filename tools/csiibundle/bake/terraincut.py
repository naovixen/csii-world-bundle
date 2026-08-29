"""Bake product `terraincut` — the terrain around every DEEP clip band, cut EXACTLY the way the
game hides it, as ground meshes for a consumer whose terrain is a heightfield.

The game's rule, validated from `Game.Simulation.TerrainSystem` (audit §46): a terrain pixel is
drawn unless its 3-D position lies inside the swept prism built from the road composition's own
trimmed curves and its clip band. Nothing is deleted, nothing is snapped, nothing is sampled — the
boundary of the hole is the prism's own surface, evaluated per pixel.

This product reproduces that on geometry rather than in a stencil. For every heightfield square a
deep clip band reaches, its two triangles are cut against the prisms with `csiibundle.terrainclip`
— an exact convex subtraction, because on one triangle each of the prism's conditions (its side
walls, its floor, its roof) is affine in `(x, z)`. The consumer punches the squares that lost any
area and places these meshes in the holes. What is left is exactly what the game leaves:

* the hole's edge follows the prism's curve, not a staircase of samples;
* ground above the band's roof stays — the hillside over a tunnel is not a hole;
* the roof iso-edge at a portal mouth is a clean cut, not a sub-sampled approximation.

Everything the consumer used to invent on top of a scored cut is gone with it: the containment
sampling, the hole-rim snap, the sub-cut, the cliff lift, the rim skirts, the band lowering. Loads
#11-#17 were that machinery's artifacts (serrated rims, teeth, curtains, needles); there is no
approximation left to tune.

**Deliberate deviation, counted:** only a DEEP band cuts (`top > CLIP_DEEP_BAND_TOP`). The game
clips the shallow ±0.3 m shell over every surface road too, but the ground there is under the
carriageway that hides it, and removing it would rely on our road meshes covering every square
metre the prism does (audit §41.7 measured where they do not). The shallow band instead lowers
the samples that stand over the carriageway onto it — `carriageway.ground_heights`, shared with the
consumer's own heightfield. The report states how many volumes were skipped this way.

Outputs (`derived/terraincut/terraincut_<tx>_<tz>.glb`, plus `derived/terrain/corridor_faces.png`):

* one glTF per tile, primitives per ground material `ground:<layer>` — the ground the consumer's
  heightfield paints there (`splat.dominant_layer`; no roadbed, because the game paints none) —
  world UVs at `GROUND_UV_M` metres per repeat, **projected triplanar**: a face is projected in
  the plane it is most parallel to, because CS2's terrain samples its grounds triplanar
  (`TRIPLANAR_PLANES` has the game's own binding and what is approximated). Faces flatter than
  45 deg — 83 % of the mesh — get the `(x, z)` projection, as before. Heightfield normals,
  bundle frame.
* `corridor_faces.png` — gray8 at FACE resolution `(h−1, w−1)`, 255 where the heightfield square
  is replaced by mesh (punch it), 0 elsewhere.

Heights are `carriageway.ground_heights` — the game's own graded heightmap with only the
over-carriageway residual clamped (audit §51) — quantised to the elevation encoding, so a
boundary vertex equals the consumer's own sample bit for bit.
"""

import json
import os
import time

import numpy

from .. import carriageway, parallel, png, reader, terrainclip
from .. import splat as splat_mod
from . import Product, register, terraincutsquare
from .tiles import TileSink

PRODUCT_NAME = "terraincut"
INPUTS = ("clipVolumes", "terrain", "terrainFine", "surfaceIndex", "splatmap")
VERSION = 18  # v13: the EXACT cut (audit §46) — `terrainclip` against the prisms, replacing the
#      containment scoring, the rim snap, the sub-cut and the cliff lift of v3-v12
# v14: sub-centimetre ribbons dropped (they rendered as hairlines: load #18) and the
#      prism construction shared with the Blender harness (`terrainclip.load_prisms`)
# v15: no roadbed stamp — the game paints no roadbed on its terrain (§17 row 2)
# v16: a square losing under MIN_REMOVED_M2 stays with the heightfield
# v17: the game's own graded heights kept — the 0.15 m roadbed sink and the
#      clamp to the band FLOOR are gone, only the over-carriageway residual
#      is lowered, onto the game's own profile top (audit §51)
# v18: TRIPLANAR UVs — CS2's terrain samples its grounds triplanar
#      (`TerrainMaterialSystem` binds `_TriplanarHeightmapBlending` and
#      `_TerrainTriplanarBlendStrengthY` as shader globals), so a face is
#      projected in the plane it is most parallel to, not always in (x, z)
#      (audit §61)

# The cut's constants — `GROUND_UV_M`, the piece and removal floors, `TRIPLANAR_PLANES` — and
# the rules behind them live with the per-square work in `terraincutsquare`; this product reads
# them from there.
GROUND_UV_M = terraincutsquare.GROUND_UV_M
GROUND_FALLBACK = terraincutsquare.GROUND_FALLBACK
MIN_REMOVED_M2 = terraincutsquare.MIN_REMOVED_M2
# squares per worker job: enough to amortise the pool's pickling, few enough to balance
CUT_CHUNK = 512


def run(context, progress):
    bundle = context.bundle_dir
    derived = context.derived
    options = context.options
    tile = float(options.get("tile", 256.0))
    prefer_fine = options.get("ground", "fine") == "fine"
    out_dir = os.path.join(derived, PRODUCT_NAME)
    started = time.monotonic()

    field = reader.load_heightfield(bundle, prefer_fine=prefer_fine)
    if field is None:
        raise RuntimeError("terraincut needs the terrain layer")
    raw = field.raw
    height, width = raw.shape
    cell, origin_x, origin_z = field.cell, field.origin_x, field.origin_z
    scale, offset = field.elevation_scale, field.elevation_offset
    paths = reader.terrain_paths(bundle)

    surface = None
    if paths.surface_index and os.path.isfile(paths.surface_index):
        surface = png.gray_array(paths.surface_index)
    layer_names, layer_idx = _paint_layers(paths, surface, raw.shape, progress)
    if surface is not None and surface.shape != raw.shape:
        surface = reader.resample_nearest(surface, raw.shape)

    # -- the ground the consumer's heightfield will hold ------------------------------------
    # The same definition it applies itself (`carriageway.ground_heights`: the game's grading, kept,
    # with only the over-carriageway residual clamped), quantised to the elevation encoding by the
    # consumer's own arithmetic (`beamng.ter.from_bundle_arrays` widens float32 to float64 before
    # `rint`).
    progress.begin("terraincut: ground", total=None, unit="")
    heights32 = raw.astype(numpy.float32) * numpy.float32(scale) + numpy.float32(offset)
    band_ceiling = _carriageway_ceiling(derived, heights32.shape, scale, offset)
    ground_y, clamped, _drop = carriageway.ground_heights(heights32, band_ceiling)
    ground_y = (
        numpy.rint((ground_y.astype(numpy.float64) - offset) / max(scale, 1e-12)) * scale + offset
    )
    progress.end(f"{clamped:,} samples lowered onto the carriageway profile")

    # -- the prisms, straight from the game's own clip volumes -------------------------------
    progress.begin("terraincut: clip volumes", total=None, unit="")
    prisms, skipped_shallow = terrainclip.load_prisms(bundle, cell)
    if not prisms:
        os.makedirs(out_dir, exist_ok=True)
        _write_mask(derived, numpy.zeros((height - 1, width - 1), dtype=bool), cell, field.fine, [])
        return {"tiles": 0, "faces": 0, "seconds": round(time.monotonic() - started, 1)}
    by_face = terrainclip.index_prisms(prisms, cell, origin_x, origin_z, width, height)
    progress.end(
        f"{len(prisms):,} deep prism triangles over {len(by_face):,} squares "
        f"({skipped_shallow:,} shallow volumes skipped: the road hides that ground)"
    )

    # -- heightfield normals, so the mesh shades like the terrain around it -------------------
    gx = numpy.zeros_like(heights32)
    gz = numpy.zeros_like(heights32)
    gx[:, 1:-1] = (heights32[:, 2:] - heights32[:, :-2]) / (2.0 * cell)
    gz[1:-1, :] = (heights32[2:, :] - heights32[:-2, :]) / (2.0 * cell)

    # -- cut every square a deep prism reaches ------------------------------------------------
    # Across worker processes: each square is independent, and everything it needs from the
    # rasters — its four corner heights, gradients and layer ids — travels with it, so no worker
    # holds a 4096² array (refactor Phase 10, 2026-08-29: the serial `cut` was 52 s clean-room).
    replaced = numpy.zeros((height - 1, width - 1), dtype=bool)
    sink = TileSink(tile, PRODUCT_NAME, out_dir)
    progress.begin("terraincut: cut", total=len(by_face), unit="squares")
    faces_cut = tris_out = pieces_out = 0
    removed_m2 = 0.0
    stats = {}
    materials_used = set()
    plane_tris = [0, 0, 0]  # faces projected in zy / xz / xy (dominant normal x/y/z)
    plane_area = [0.0, 0.0, 0.0]
    corner_names = [f"ground:{n}" for n in layer_names] + [GROUND_FALLBACK]
    layer_of = numpy.where(layer_idx < len(layer_names), layer_idx, len(layer_names))
    # insertion order — the prism index's — so the tiles' triangle order holds
    squares = list(by_face)
    jobs = max(1, int(context.options.get("jobs") or 1))
    batches = [
        terraincutsquare.cut_batch(
            squares[start : start + CUT_CHUNK],
            by_face,
            prisms,
            (ground_y, gx, gz, layer_of, corner_names),
            (cell, origin_x, origin_z, width, height),
        )
        for start in range(0, len(squares), CUT_CHUNK)
    ]
    for result in parallel.each(
        terraincutsquare.cut_chunk,
        batches,
        jobs,
        progress,
        ordered=True,
        min_parallel=2,
        advance=False,
    ):
        progress.advance(result["seen"])
        for key, value in result["stats"].items():
            stats[key] = stats.get(key, 0) + value
        for axis in range(3):
            plane_tris[axis] += result["plane_tris"][axis]
            plane_area[axis] += result["plane_area"][axis]
        for r, c, removed, pieces, verts, tris, uvs, normals, tri_mat in result["squares"]:
            replaced[r, c] = True
            removed_m2 += removed
            faces_cut += 1
            pieces_out += pieces
            if verts is None:
                continue  # wholly inside the band: a hole, as in game
            for name in sorted(set(tri_mat.tolist())):
                pick = tri_mat == name
                sink.add(verts, tris[pick], uvs[pick], name, normals=normals)
                tris_out += int(pick.sum())
                materials_used.add(name)
    written = sink.flush(progress, jobs=max(1, int(context.options.get("jobs") or 1)))
    side_tris = plane_tris[0] + plane_tris[2]
    side_area = plane_area[0] + plane_area[2]
    total_area = sum(plane_area)
    progress.end(
        f"{written['files']} tiles, {faces_cut:,} squares cut -> {pieces_out:,} pieces / "
        f"{tris_out:,} triangles, {removed_m2:,.0f} m2 removed, "
        f"{stats.get('slivers', 0):,} ribbon(s) under {terrainclip.MIN_WIDTH_M * 100:g} cm dropped "
        f"({stats.get('sliverArea', 0.0):.1f} m2 — the game resolves those per pixel)"
    )
    progress.log(
        f"terraincut: triplanar — {side_tris:,} face(s) / {side_area:,.0f} m2 "
        f"({100.0 * side_area / max(total_area, 1e-9):.1f} % of the mesh) project from a SIDE "
        f"plane, the rest from above. APPROXIMATED: the game blends the three projections "
        f"per pixel (_TerrainTriplanarBlendStrengthY, _TriplanarHeightmapBlending); a baked "
        f"UV set collapses that to the dominant plane per face, so those faces meet their "
        f"neighbours with a pattern seam the game would blend away"
    )
    _write_mask(derived, replaced, cell, field.fine, sorted(materials_used))
    return {
        "tiles": written["files"],
        "faces": faces_cut,
        "triangles": tris_out,
        "pieces": pieces_out,
        "removedSquareMetres": round(removed_m2, 1),
        "shallowVolumesSkipped": skipped_shallow,
        "sliversDropped": stats.get("slivers", 0),
        "sliverSquareMetres": round(stats.get("sliverArea", 0.0), 2),
        "materials": sorted(materials_used),
        "corridorFaces": int(replaced.sum()),
        "triplanarSideFaces": side_tris,
        "triplanarSideSquareMetres": round(side_area, 1),
        "triplanarFacesPerPlane": {"zy": plane_tris[0], "xz": plane_tris[1], "xy": plane_tris[2]},
        "_tiles": written["tiles"],
        "seconds": round(time.monotonic() - started, 1),
    }


def _carriageway_ceiling(derived, shape, scale, offset):
    """The game's carriageway profile top per sample, as the consumer reads it — `terrainmask`'s
    `carriageway_ceiling.png`, decoded exactly as the app does. `+inf` where no shallow band
    covers the sample. The bake runs `terrainmask` before this product (`bake.PRODUCT_MODULES`)."""
    ceiling = numpy.full(shape, numpy.inf, dtype=numpy.float32)
    path = os.path.join(derived, "terrain", "carriageway_ceiling.png")
    if not os.path.isfile(path):
        raise RuntimeError(
            f"terraincut needs {path} — bake `terrainmask` first (it is the same "
            "carriageway clamp the consumer's own heightfield applies)"
        )
    stored = png.gray_array(path)
    if stored.shape != shape:
        raise RuntimeError(
            f"carriageway_ceiling.png is {stored.shape}, the ground raster {shape} "
            "— re-bake `terrainmask` against the same `ground` option"
        )
    has = stored > 0
    ceiling[has] = (stored[has].astype(numpy.float64) * scale + offset).astype(numpy.float32)
    return ceiling


def _paint_layers(paths, surface, shape, progress):
    """`(names, index (h, w) uint8)` — the ground the consumer's heightfield paints per sample."""
    names = ["grass"]
    index = numpy.zeros(shape, dtype=numpy.uint8)
    block = (paths.rasters or {}).get("splatmap") if paths.rasters else None
    if block and paths.splatmap and os.path.isfile(paths.splatmap):
        try:
            sp = splat_mod.Splat(paths.splatmap, block)
            mapping, _published = splat_mod.channel_mapping(block)
            # no roadbed stamp: the game paints no roadbed material on its terrain (its splatmap
            # never sees net or lane data — audit §17 row 2), and the consumer's heightfield does
            # not either, so a mesh in a hole must wear the same grounds the terrain beside it does
            index, names = splat_mod.dominant_layer(sp, mapping)
            if index.shape != shape:
                index = reader.resample_nearest(index, shape)
        except Exception as exc:
            progress.log(
                f"terraincut: splat unreadable ({exc!r}); every face binds {GROUND_FALLBACK}"
            )
    return names, index


def _write_mask(derived, replaced, cell, fine, materials):
    out_dir = os.path.join(derived, "terrain")
    os.makedirs(out_dir, exist_ok=True)
    png.write_gray(
        os.path.join(out_dir, "corridor_faces.png"),
        numpy.where(replaced, 255, 0).astype(numpy.uint8),
    )
    doc = {
        "schema": "csiibundle.bake.terraincut/3",
        "faceSize": [int(replaced.shape[0]), int(replaced.shape[1])],
        "cell": cell,
        "fine": bool(fine),
        "corridorFaces": int(replaced.sum()),
        "groundUvMetres": GROUND_UV_M,
        "materials": list(materials),
    }
    with open(
        os.path.join(out_dir, "corridor.json"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(doc, handle, indent=1)


register(Product(PRODUCT_NAME, INPUTS, "terraincut/", run, version=VERSION, options=("ground",)))
