"""Tiled GLB output for the world-space bake products (contract §13 "Tiles").

World-space geometry — draped markings, bent furniture, conformed areas, dropped skirts — is
written as one `.glb` per tile of a square grid anchored at bundle-frame `(0, 0)`:
`tx = floor(x / tile)`, `tz = floor(z / tile)`, file `<product>_<tx>_<tz>.glb`, one primitive
per (material, paint variant). A triangle belongs to the tile its centroid falls in.

`TileSink.add` takes triangles in the BUNDLE frame with per-corner UVs (what `lanes.bend_part`
produces) or per-vertex UVs; `flush` welds each bucket on (position, uv, colour), writes the
files through `glbwrite` (which applies the §2.2 mirror + winding), and returns what it wrote.
Materials are written by name; when the sink is given the bundle's textures directory and the
material table, base-colour URIs are added relative to the tile file so a stock glTF viewer shows
something — the join a consumer relies on is still the NAME.
"""

import json
import os
from collections import defaultdict
from multiprocessing import Pool

import numpy

from .. import glbwrite, reader

WELD_MM = 0.5  # positions within half a millimetre AND equal UV/colour weld into one vertex

# How a per-primitive draw-distance limit is carried in a tile's material name. `#` cannot occur
# in a CS2 material name (the producer's names are asset identifiers) and glTF material names are
# free text, so this needs no second file and no ordering assumption between the `.glb` and a
# sidecar. CONTRACT with the consumers (§13 "Tiles").
MIN_LOD_MARK = "#L"


def split_material_name(name):
    """`"Asphalt@v3#L128"` -> `("Asphalt", "v3", 128)`; missing parts come back as None."""
    text = str(name)
    min_lod = None
    if MIN_LOD_MARK in text:
        text, _, tail = text.rpartition(MIN_LOD_MARK)
        try:
            min_lod = int(tail)
        except ValueError:
            min_lod = None
    material, _, variant = text.partition("@")
    return material, (variant or None), min_lod


class TileSink:
    def __init__(self, tile_size, product, out_dir, material_specs=None):
        self.tile = float(tile_size)
        self.product = product
        self.out_dir = out_dir
        self.material_specs = material_specs or {}
        self._buckets = defaultdict(lambda: {"corners": [], "uvs": [], "colors": [], "normals": []})
        self.triangles = 0
        os.makedirs(out_dir, exist_ok=True)
        # A previous bake (another region, another tile size) leaves tiles this one will not
        # rewrite; a consumer would read them as current. Clear the product's own files first.
        for name in os.listdir(out_dir):
            if name.startswith(product + "_") and name.endswith(".glb"):
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass

    def add(
        self, verts, tris, uvs, material, variant=None, colors=None, normals=None, min_lod=None
    ):
        """Add triangles. `verts` `(N,3)` bundle frame; `tris` `(M,3)`; `uvs` `(M,3,2)` per corner
        or `(N,2)` per vertex or None; `colors` `(N,4)` per vertex or None; `normals` `(N,3)` per
        vertex (bundle frame) or None — a primitive without them gets flat normals at write time.

        `min_lod` is the GAME's own draw-distance limit for these triangles (`csiibundle.lod`:
        the lane rule for a marking or a fence, the area rule per triangle, the object rule for a
        placed decal). Triangles with different limits go into different primitives and the limit
        rides in the primitive's material name as `#L<n>` — so a consumer can rebuild exactly
        what CS2 draws at a given distance instead of drawing the whole tile to the horizon.
        A tile is one merged shape in every engine we ship to, and this is what lets that shape
        still carry the per-primitive rule."""
        verts = numpy.asarray(verts, dtype=numpy.float64)
        tris = numpy.asarray(tris, dtype=numpy.int64)
        if len(tris) == 0:
            return
        corners = verts[tris]  # (M, 3, 3)
        if uvs is None:
            corner_uv = numpy.zeros((len(tris), 3, 2))
        else:
            uvs = numpy.asarray(uvs, dtype=numpy.float64)
            corner_uv = uvs if uvs.ndim == 3 else uvs[tris]
        corner_col = None if colors is None else numpy.asarray(colors, dtype=numpy.float64)[tris]
        corner_nrm = None if normals is None else numpy.asarray(normals, dtype=numpy.float64)[tris]
        centroid = corners.mean(axis=1)
        tx = numpy.floor(centroid[:, 0] / self.tile).astype(numpy.int64)
        tz = numpy.floor(centroid[:, 2] / self.tile).astype(numpy.int64)
        keys = tx * 1000003 + tz
        order = numpy.argsort(keys, kind="stable")
        sorted_keys = keys[order]
        edges = numpy.flatnonzero(numpy.diff(sorted_keys)) + 1
        starts = numpy.concatenate([[0], edges])
        ends = numpy.concatenate([edges, [len(order)]])
        name = f"{material}@{variant}" if variant else str(material)
        if min_lod is not None:
            name = f"{name}{MIN_LOD_MARK}{int(min_lod)}"
        for s, e in zip(starts, ends):
            sel = order[s:e]
            key = (int(tx[sel[0]]), int(tz[sel[0]]), name)
            bucket = self._buckets[key]
            bucket["corners"].append(corners[sel])
            bucket["uvs"].append(corner_uv[sel])
            bucket["colors"].append(None if corner_col is None else corner_col[sel])
            bucket["normals"].append(None if corner_nrm is None else corner_nrm[sel])
        self.triangles += len(tris)

    def tiles(self):
        return sorted({(k[0], k[1]) for k in self._buckets})

    def flush(self, progress=None, texture_uri_base=None, jobs=1):
        """Write every tile. Returns `{"tiles": [...], "files": n, "verts": n, "tris": n}`.

        A tile welds its own corners and writes its own file, so `jobs > 1` runs the tiles on a
        pool. This is the largest single cost of three of the four world-space products (90 s of
        `lanegeometry`'s 160 s under the profiler, and the whole of `areas`' tile phase), and the
        work is a sort per material bucket — nothing shared, nothing ordered. `imap` keeps the
        results in tile order so `tiles_written` and the vertex/triangle totals do not depend on
        which worker finished first, and the tiles themselves are byte-identical either way:
        the pool changes who runs `weld`, not what it computes.

        Buckets are handed over one tile at a time and dropped from the sink as they go, so the
        parent is not holding a second copy of a gigabyte of corners while the workers hold the
        first.
        """
        by_tile = defaultdict(list)
        for key in list(self._buckets):
            by_tile[(key[0], key[1])].append(key)
        order = sorted(by_tile)

        def jobs_iter():
            for tile_key in order:
                buckets = [
                    (k[2], self._buckets.pop(k))
                    for k in sorted(by_tile[tile_key], key=lambda k: k[2])
                ]
                yield (
                    self.out_dir,
                    self.product,
                    tile_key[0],
                    tile_key[1],
                    buckets,
                    self.material_specs,
                    texture_uri_base,
                )

        tiles_written = []
        total_verts = total_tris = 0

        def absorb(result):
            nonlocal total_verts, total_tris
            if progress is not None:
                progress.check()
                progress.advance()
            if result is None:
                return
            tx, tz, verts, tris = result
            tiles_written.append((tx, tz))
            total_verts += verts
            total_tris += tris

        if jobs > 1 and len(order) > 4:
            with Pool(processes=min(jobs, len(order))) as pool:
                for result in pool.imap(_write_tile, jobs_iter(), chunksize=1):
                    absorb(result)
        else:
            for job in jobs_iter():
                absorb(_write_tile(job))
        return {
            "tiles": tiles_written,
            "files": len(tiles_written),
            "verts": total_verts,
            "tris": total_tris,
        }

    def _specs_for(self, prims, texture_uri_base):
        return _specs_for(self.material_specs, prims, texture_uri_base)


def _write_tile(args):
    """One tile: weld each material bucket, write the `.glb`. `(tx, tz, verts, tris)` or None."""
    out_dir, product, tx, tz, buckets, material_specs, texture_uri_base = args
    prims = []
    total_verts = total_tris = 0
    for name, bucket in buckets:
        corners = numpy.concatenate(bucket["corners"])  # (M,3,3)
        uvs = numpy.concatenate(bucket["uvs"])  # (M,3,2)
        cols = None
        if all(c is not None for c in bucket["colors"]) and bucket["colors"]:
            cols = numpy.concatenate(bucket["colors"])  # (M,3,4)
        nrms = None
        if all(c is not None for c in bucket["normals"]) and bucket["normals"]:
            nrms = numpy.concatenate(bucket["normals"])  # (M,3,3)
        welded = weld(corners, uvs, cols, nrms)
        v, n_uv, n_col, t = welded[:4]
        n_nrm = welded[4] if len(welded) > 4 else None
        if len(t) == 0:
            continue
        prim = {"verts": v, "uvs": n_uv, "tris": t, "material": name}
        if n_col is not None:
            prim["colors"] = n_col
        if n_nrm is not None:
            prim["normals"] = n_nrm
        prims.append(prim)
        total_verts += len(v)
        total_tris += len(t)
    if not prims:
        return None
    asset_id = f"{product}_{tx}_{tz}"
    glbwrite.write_glb(
        os.path.join(out_dir, asset_id + ".glb"),
        [{"name": asset_id, "primitives": prims}],
        materials=_specs_for(material_specs, prims, texture_uri_base),
        from_bundle_frame=True,
    )
    return tx, tz, total_verts, total_tris


def _specs_for(material_specs, prims, texture_uri_base):
    specs = {}
    for prim in prims:
        name = prim["material"]
        base_name = split_material_name(name)[0]
        spec = material_specs.get(base_name)
        if not spec:
            continue
        entry = {}
        if spec.get("baseColor") and texture_uri_base:
            entry["baseColor"] = texture_uri_base + os.path.basename(spec["baseColor"])
        if spec.get("normal") and texture_uri_base:
            entry["normal"] = texture_uri_base + os.path.basename(spec["normal"])
        mode = (spec.get("alphaMode") or "opaque").upper()
        if mode in ("MASK", "BLEND"):
            entry["alphaMode"] = mode
            if mode == "MASK":
                entry["alphaCutoff"] = float(spec.get("alphaCutoff", 0.5))
        if spec.get("doubleSided"):
            entry["doubleSided"] = True
        if spec.get("baseColorFactor"):
            entry["baseColorFactor"] = list(spec["baseColorFactor"])
        specs[name] = entry
    return specs


def weld(corners, uvs, colors=None, normals=None):
    """Per-corner arrays → indexed mesh: `(verts (N,3), uvs (N,2), colors (N,4)|None, tris (M,3)
    [, normals (N,3)|None])`, merging corners whose position (to `WELD_MM`), uv and colour agree
    (a welded vertex takes the normalised mean of its corners' normals). The fifth element is
    returned only when `normals` is passed, so older callers keep their 4-tuple."""
    m = len(corners)
    pos = corners.reshape(-1, 3)
    uv = uvs.reshape(-1, 2)
    scale = 1000.0 / WELD_MM
    cols = [numpy.round(pos * scale).astype(numpy.int64), numpy.round(uv * 1e5).astype(numpy.int64)]
    col_flat = None
    if colors is not None:
        col_flat = colors.reshape(-1, colors.shape[-1])
        cols.append(numpy.round(col_flat * 255).astype(numpy.int64))
    key = numpy.concatenate(cols, axis=1)
    _, first, inverse = numpy.unique(key, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    verts = pos[first].astype(numpy.float32)
    out_uv = uv[first].astype(numpy.float32)
    out_col = None if col_flat is None else col_flat[first].astype(numpy.float32)
    tris = inverse.reshape(m, 3)
    keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    if normals is None:
        return verts, out_uv, out_col, tris[keep]
    nrm_flat = numpy.asarray(normals, dtype=numpy.float64).reshape(-1, 3)
    acc = numpy.zeros((len(first), 3), dtype=numpy.float64)
    numpy.add.at(acc, inverse, nrm_flat)
    length = numpy.linalg.norm(acc, axis=1)
    acc[length > 1e-9] /= length[length > 1e-9][:, None]
    acc[length <= 1e-9] = (0.0, 1.0, 0.0)
    return verts, out_uv, out_col, tris[keep], acc.astype(numpy.float32)


# -- what a tile's materials point at ----------------------------------------------------------


def material_specs(derived, bundle):
    """Base-colour paths + alpha modes for the tile writer, from the bake's own materials
    product when present, else straight from `materials.json`.

    Here because three products need it — `markings`, `skirts`, `lanegeometry` — and the two
    latter used to import it out of `markings` by its private name. It is about what a TILE
    carries, which is this module's subject.
    """
    path = os.path.join(derived, "materials.json")
    specs = {}
    try:
        if os.path.isfile(path):
            doc = json.load(open(path, encoding="utf-8"))
            for key, entry in doc.get("materials", {}).items():
                specs[key] = {
                    "baseColor": entry.get("baseColor"),
                    "normal": entry.get("normal"),
                    "alphaMode": entry.get("alphaMode"),
                    "alphaCutoff": entry.get("alphaCutoff"),
                    "doubleSided": entry.get("doubleSided"),
                    "baseColorFactor": entry.get("baseColorFactor"),
                }
            return specs
    except Exception:
        pass
    for key, entry in (reader.load_materials(bundle) or {}).items():
        specs[key] = {
            "baseColor": entry.get("baseColor"),
            "normal": entry.get("normal"),
            "alphaMode": entry.get("alphaMode"),
            "alphaCutoff": entry.get("alphaCutoff"),
            "doubleSided": entry.get("doubleSided"),
        }
    return specs


def texture_uri_base(out_dir, bundle):
    """Relative URI prefix from a tile directory to `<bundle>/textures/`, or None when no
    relative path exists (different drive)."""
    try:
        textures = reader.layer_path(bundle, "textures")
        rel = os.path.relpath(textures, out_dir).replace(os.sep, "/")
        return rel.rstrip("/") + "/"
    except ValueError:
        return None
