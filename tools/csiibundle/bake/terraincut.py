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
the samples that stand over the carriageway onto it — `terrain.ground_heights`, shared with the
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

Heights are `terrain.ground_heights` — the game's own graded heightmap with only the
over-carriageway residual clamped (audit §51) — quantised to the elevation encoding, so a
boundary vertex equals the consumer's own sample bit for bit.
"""

import json
import os
import time

import numpy

from .. import png, reader, splat as splat_mod, terrain, terrainclip
from . import Product, register
from .tiles import TileSink

PRODUCT_NAME = "terraincut"
INPUTS = ("clipVolumes", "terrain", "terrainFine", "surfaceIndex", "splatmap")
VERSION = 18         # v13: the EXACT cut (audit §46) — `terrainclip` against the prisms, replacing the
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

# metres per texture repeat on the ground mesh: the terrain material's MACRO tier period
GROUND_UV_M = 40.0
GROUND_FALLBACK = "ground:grass"
# a kept piece smaller than this is a rounding sliver of the cut, not ground anybody sees
MIN_PIECE_M2 = 1e-4
# A square that loses less ground than this is treated as untouched and left to the heightfield.
# The prism only grazed it — a ribbon a few millimetres wide, which the cut drops anyway
# (`terrainclip.MIN_WIDTH_M`) — and punching it would trade a terrain-shaded square for a
# mesh-shaded one over 0.3 % of its area. On the reference city the threshold is the difference
# between 46,550 punched squares and 43,043.
MIN_REMOVED_M2 = 0.01

# Which plane a face's ground texture is projected in. PORTED, with a declared collapse.
#
# CS2's terrain samples its grounds TRIPLANAR. `Game.Rendering.TerrainMaterialSystem` sets two
# shader globals from the theme's `TerrainRenderSettingsPrefab`, beside `_SplatMultiplier` and
# the `_TerrainExtra*` set the rest of this bake already ports:
#
#     _TriplanarHeightmapBlending      <- m_TriplanarHeightMapBlending  (class default 0.5)
#     _TerrainTriplanarBlendStrengthY  <- m_TriplanarBlendStrengthY     (class default 0.2)
#
# and the producer already ships both, per map, as `rasters.terrainTiling.triplanar*` — on the
# reference city's `DefaultTerrainRenderSettings` they are **0.25 and 0.75**, not the class
# defaults. So a steep face in the game is textured from its SIDE projection, not smeared from
# above.
# A pure `(x, z)` projection stretches a face by `1/cos(slope)`: measured on the built level,
# 11.6 % of the cut mesh's area sits on faces at 85 deg or more (real 10-13 m steps between
# adjacent 1.75 m samples, where CS2 graded a corridor into a hillside), and 11.0 % of the area
# samples the ground at 4-10x the intended period. A 4-10x magnified 68 m rock tiling is one
# texel over a whole wall, which renders as a flat single-colour slab — the cream and near-black
# patches of load #26. Predicted colour for the worst of them (198, 186, 172) against (200, 186,
# 173) measured off the screenshot; audit §61.
#
# APPROXIMATED, declared and counted: the game blends the three projections PER PIXEL, weighted
# by the two globals above and by the grounds' own height maps. Those weights live in an HLSL
# shader that is not in the managed assemblies, and a mesh carries ONE baked UV set, so this
# collapses the blend to the single dominant plane PER FACE — `argmax(|n|)`, the limit every
# triplanar formulation agrees on, and the choice with no density error at the 45 deg crossover
# (both planes stretch by 1.41 there). The cost is a texture-pattern discontinuity across an edge
# whose two faces pick different planes; the run report counts the faces and area per plane.
# The shipped `triplanarBlendStrengthY` is deliberately NOT used as a weight: whether it scales
# the Y projection up, scales it down, or is an exponent is not determinable without the shader,
# and reading it as a Y weight would move the crossover to 36.9 deg (at 0.75) or 11.3 deg (at the
# class default 0.2) and send near-flat ground to a side projection. When the weighting curve is
# recovered, that is the upgrade path — and it needs the multi-stage `inLayer.opacity` mechanism,
# not a different UV.
# Faces flatter than 45 deg keep the `(x, z)` projection they had, so 83 % of the mesh is
# unchanged.
TRIPLANAR_PLANES = ((2, 1), (0, 2), (0, 1))     # per dominant normal axis x/y/z: the two axes kept


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
    # The same definition it applies itself (`terrain.ground_heights`: the game's grading, kept,
    # with only the over-carriageway residual clamped), quantised to the elevation encoding by the
    # consumer's own arithmetic (`beamng.ter.from_bundle_arrays` widens float32 to float64 before
    # `rint`).
    progress.begin("terraincut: ground", total=None, unit="")
    heights32 = raw.astype(numpy.float32) * numpy.float32(scale) + numpy.float32(offset)
    band_ceiling = _carriageway_ceiling(derived, heights32.shape, scale, offset)
    ground_y, clamped, _drop = terrain.ground_heights(heights32, band_ceiling)
    ground_y = numpy.rint((ground_y.astype(numpy.float64) - offset) / max(scale, 1e-12)) * scale + offset
    progress.end(f"{clamped:,} samples lowered onto the carriageway profile")

    # -- the prisms, straight from the game's own clip volumes -------------------------------
    progress.begin("terraincut: clip volumes", total=None, unit="")
    prisms, skipped_shallow = terrainclip.load_prisms(bundle, cell)
    if not prisms:
        os.makedirs(out_dir, exist_ok=True)
        _write_mask(derived, numpy.zeros((height - 1, width - 1), dtype=bool), cell, field.fine, [])
        return {"tiles": 0, "faces": 0, "seconds": round(time.monotonic() - started, 1)}
    by_face = terrainclip.index_prisms(prisms, cell, origin_x, origin_z, width, height)
    progress.end(f"{len(prisms):,} deep prism triangles over {len(by_face):,} squares "
                 f"({skipped_shallow:,} shallow volumes skipped: the road hides that ground)")

    # -- heightfield normals, so the mesh shades like the terrain around it -------------------
    gx = numpy.zeros_like(heights32)
    gz = numpy.zeros_like(heights32)
    gx[:, 1:-1] = (heights32[:, 2:] - heights32[:, :-2]) / (2.0 * cell)
    gz[1:-1, :] = (heights32[2:, :] - heights32[:-2, :]) / (2.0 * cell)

    def raster_normals(x, z):
        c = numpy.clip((numpy.asarray(x) - origin_x) / cell, 0, width - 1.001)
        r = numpy.clip((origin_z - numpy.asarray(z)) / cell, 0, height - 1.001)
        ci = c.astype(numpy.int64); ri = r.astype(numpy.int64); fu = c - ci; fv = r - ri

        def bil(a):
            return ((a[ri, ci] * (1 - fu) + a[ri, ci + 1] * fu) * (1 - fv)
                    + (a[ri + 1, ci] * (1 - fu) + a[ri + 1, ci + 1] * fu) * fv)
        dx, dz = bil(gx), bil(gz)
        n = numpy.stack((-dx, numpy.ones_like(dx), dz), axis=-1)
        return n / numpy.linalg.norm(n, axis=1)[:, None]

    def layer_at(x, z):
        r = numpy.clip(numpy.rint((origin_z - numpy.asarray(z)) / cell).astype(numpy.int64), 0, height - 1)
        c = numpy.clip(numpy.rint((numpy.asarray(x) - origin_x) / cell).astype(numpy.int64), 0, width - 1)
        idx = layer_idx[r, c]
        return numpy.asarray([f"ground:{layer_names[i]}" if i < len(layer_names) else GROUND_FALLBACK
                              for i in idx.reshape(-1)], dtype=object).reshape(idx.shape)

    # -- cut every square a deep prism reaches ------------------------------------------------
    replaced = numpy.zeros((height - 1, width - 1), dtype=bool)
    sink = TileSink(tile, PRODUCT_NAME, out_dir)
    progress.begin("terraincut: cut", total=len(by_face), unit="squares")
    faces_cut = tris_out = pieces_out = 0
    removed_m2 = 0.0
    stats = {}
    materials_used = set()
    plane_tris = [0, 0, 0]              # faces projected in zy / xz / xy (dominant normal x/y/z)
    plane_area = [0.0, 0.0, 0.0]
    done = 0
    for (r, c), station_ids in by_face.items():
        done += 1
        if done % 4096 == 0:
            progress.advance(4096)
            progress.check()
        # the square's four corners, north-west first, at the consumer's own heights
        x0 = origin_x + c * cell
        x1 = x0 + cell
        z0 = origin_z - r * cell
        z1 = z0 - cell
        nw = (x0, float(ground_y[r, c]), z0)
        ne = (x1, float(ground_y[r, c + 1]), z0)
        sw = (x0, float(ground_y[r + 1, c]), z1)
        se = (x1, float(ground_y[r + 1, c + 1]), z1)
        local = [prisms[i] for i in station_ids]
        whole = cell * cell
        kept = []
        for tri in ((nw, ne, se), (nw, se, sw)):        # the NW-SE diagonal, as `drape` assumes
            kept.extend(terrainclip.clip_triangle(tri, local, stats))
        kept = [p for p in kept if abs(terrainclip.polygon_area([(q[0], q[2]) for q in p])) > MIN_PIECE_M2]
        kept_area = sum(abs(terrainclip.polygon_area([(q[0], q[2]) for q in p])) for p in kept)
        if whole - kept_area < MIN_REMOVED_M2:
            continue                                    # the prism only grazed it: leave it be
        replaced[r, c] = True
        removed_m2 += whole - kept_area
        faces_cut += 1
        if not kept:
            continue                                    # wholly inside the band: a hole, as in game
        verts = []
        tris = []
        for poly in kept:
            base = len(verts)
            verts.extend(poly)
            for a, b, d in terrainclip.fan(poly):
                tris.append((base + a, base + b, base + d))
            pieces_out += 1
        verts = numpy.asarray(verts, dtype=numpy.float64)
        tris = numpy.asarray(tris, dtype=numpy.int64)
        # every ground face looks up
        a, b, d = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
        nrm = numpy.cross(b - a, d - a)
        flip = nrm[:, 1] < 0
        tris[flip] = tris[flip][:, ::-1]
        # triplanar: project each face in the plane it is most parallel to (see TRIPLANAR_PLANES)
        corners = verts[tris]                               # (M, 3, 3)
        dominant = numpy.abs(nrm).argmax(axis=1)
        uvs = numpy.empty((len(tris), 3, 2), dtype=numpy.float64)
        for axis, (u_ax, v_ax) in enumerate(TRIPLANAR_PLANES):
            on = dominant == axis
            if not on.any():
                continue
            uvs[on, :, 0] = corners[on, :, u_ax] / GROUND_UV_M
            uvs[on, :, 1] = corners[on, :, v_ax] / GROUND_UV_M
            plane_tris[axis] += int(on.sum())
            plane_area[axis] += float(0.5 * numpy.linalg.norm(nrm[on], axis=1).sum())
        normals = raster_normals(verts[:, 0], verts[:, 2])
        centres = corners.mean(axis=1)
        tri_mat = layer_at(centres[:, 0], centres[:, 2])
        for name in sorted(set(tri_mat.tolist())):
            pick = tri_mat == name
            sink.add(verts, tris[pick], uvs[pick], name, normals=normals)
            tris_out += int(pick.sum())
            materials_used.add(name)
    written = sink.flush(progress, jobs=max(1, int(context.options.get("jobs") or 1)))
    side_tris = plane_tris[0] + plane_tris[2]
    side_area = plane_area[0] + plane_area[2]
    total_area = sum(plane_area)
    progress.end(f"{written['files']} tiles, {faces_cut:,} squares cut -> {pieces_out:,} pieces / "
                 f"{tris_out:,} triangles, {removed_m2:,.0f} m2 removed, "
                 f"{stats.get('slivers', 0):,} ribbon(s) under {terrainclip.MIN_WIDTH_M * 100:g} cm dropped "
                 f"({stats.get('sliverArea', 0.0):.1f} m2 — the game resolves those per pixel)")
    progress.log(f"terraincut: triplanar — {side_tris:,} face(s) / {side_area:,.0f} m2 "
                 f"({100.0 * side_area / max(total_area, 1e-9):.1f} % of the mesh) project from a SIDE "
                 f"plane, the rest from above. APPROXIMATED: the game blends the three projections "
                 f"per pixel (_TerrainTriplanarBlendStrengthY, _TriplanarHeightmapBlending); a baked "
                 f"UV set collapses that to the dominant plane per face, so those faces meet their "
                 f"neighbours with a pattern seam the game would blend away")
    _write_mask(derived, replaced, cell, field.fine, sorted(materials_used))
    return {"tiles": written["files"], "faces": faces_cut, "triangles": tris_out, "pieces": pieces_out,
            "removedSquareMetres": round(removed_m2, 1), "shallowVolumesSkipped": skipped_shallow,
            "sliversDropped": stats.get("slivers", 0), "sliverSquareMetres": round(stats.get("sliverArea", 0.0), 2),
            "materials": sorted(materials_used), "corridorFaces": int(replaced.sum()),
            "triplanarSideFaces": side_tris, "triplanarSideSquareMetres": round(side_area, 1),
            "triplanarFacesPerPlane": {"zy": plane_tris[0], "xz": plane_tris[1], "xy": plane_tris[2]},
            "_tiles": written["tiles"], "seconds": round(time.monotonic() - started, 1)}


def _carriageway_ceiling(derived, shape, scale, offset):
    """The game's carriageway profile top per sample, as the consumer reads it — `terrainmask`'s
    `carriageway_ceiling.png`, decoded exactly as the app does. `+inf` where no shallow band
    covers the sample. The bake runs `terrainmask` before this product (see
    `bake.PRODUCT_MODULES`)."""
    ceiling = numpy.full(shape, numpy.inf, dtype=numpy.float32)
    path = os.path.join(derived, "terrain", "carriageway_ceiling.png")
    if not os.path.isfile(path):
        raise RuntimeError(f"terraincut needs {path} — bake `terrainmask` first (it is the same "
                           "carriageway clamp the consumer's own heightfield applies)")
    stored = png.gray_array(path)
    if stored.shape != shape:
        raise RuntimeError(f"carriageway_ceiling.png is {stored.shape}, the ground raster {shape} "
                           "— re-bake `terrainmask` against the same `ground` option")
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
        except Exception as exc:  # noqa: BLE001 - presentation only; grass everywhere beats no product
            progress.log(f"terraincut: splat unreadable ({exc!r}); every face binds {GROUND_FALLBACK}")
    return names, index


def _write_mask(derived, replaced, cell, fine, materials):
    out_dir = os.path.join(derived, "terrain")
    os.makedirs(out_dir, exist_ok=True)
    png.write_gray(os.path.join(out_dir, "corridor_faces.png"), numpy.where(replaced, 255, 0).astype(numpy.uint8))
    doc = {"schema": "csiibundle.bake.terraincut/3", "faceSize": [int(replaced.shape[0]), int(replaced.shape[1])],
           "cell": cell, "fine": bool(fine), "corridorFaces": int(replaced.sum()), "groundUvMetres": GROUND_UV_M,
           "materials": list(materials)}
    with open(os.path.join(out_dir, "corridor.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=1)


register(Product(PRODUCT_NAME, INPUTS, "terraincut/", run, version=VERSION, options=("ground",)))
