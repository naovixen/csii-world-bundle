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

import math
import os
from collections import defaultdict

import numpy

from .. import glbwrite

WELD_MM = 0.5     # positions within half a millimetre AND equal UV/colour weld into one vertex


class TileSink:
    def __init__(self, tile_size, product, out_dir, material_specs=None):
        self.tile = float(tile_size)
        self.product = product
        self.out_dir = out_dir
        self.material_specs = material_specs or {}
        self._buckets = defaultdict(lambda: {"corners": [], "uvs": [], "colors": []})
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

    def add(self, verts, tris, uvs, material, variant=None, colors=None):
        """Add triangles. `verts` `(N,3)` bundle frame; `tris` `(M,3)`; `uvs` `(M,3,2)` per corner
        or `(N,2)` per vertex or None; `colors` `(N,4)` per vertex or None."""
        verts = numpy.asarray(verts, dtype=numpy.float64)
        tris = numpy.asarray(tris, dtype=numpy.int64)
        if len(tris) == 0:
            return
        corners = verts[tris]                              # (M, 3, 3)
        if uvs is None:
            corner_uv = numpy.zeros((len(tris), 3, 2))
        else:
            uvs = numpy.asarray(uvs, dtype=numpy.float64)
            corner_uv = uvs if uvs.ndim == 3 else uvs[tris]
        corner_col = None if colors is None else numpy.asarray(colors, dtype=numpy.float64)[tris]
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
        for s, e in zip(starts, ends):
            sel = order[s:e]
            key = (int(tx[sel[0]]), int(tz[sel[0]]), name)
            bucket = self._buckets[key]
            bucket["corners"].append(corners[sel])
            bucket["uvs"].append(corner_uv[sel])
            bucket["colors"].append(None if corner_col is None else corner_col[sel])
        self.triangles += len(tris)

    def tiles(self):
        return sorted({(k[0], k[1]) for k in self._buckets})

    def flush(self, progress=None, texture_uri_base=None):
        """Write every tile. Returns `{"tiles": [...], "files": n, "verts": n, "tris": n}`."""
        by_tile = defaultdict(list)
        for (tx, tz, name), bucket in self._buckets.items():
            by_tile[(tx, tz)].append((name, bucket))
        tiles_written = []
        total_verts = total_tris = 0
        for (tx, tz) in sorted(by_tile):
            if progress is not None:
                progress.check()
            prims = []
            for name, bucket in sorted(by_tile[(tx, tz)], key=lambda kv: kv[0]):
                corners = numpy.concatenate(bucket["corners"])          # (M,3,3)
                uvs = numpy.concatenate(bucket["uvs"])                  # (M,3,2)
                cols = None
                if all(c is not None for c in bucket["colors"]) and bucket["colors"]:
                    cols = numpy.concatenate(bucket["colors"])          # (M,3,4)
                v, n_uv, n_col, t = weld(corners, uvs, cols)
                if len(t) == 0:
                    continue
                prim = {"verts": v, "uvs": n_uv, "tris": t, "material": name}
                if n_col is not None:
                    prim["colors"] = n_col
                prims.append(prim)
                total_verts += len(v)
                total_tris += len(t)
            if not prims:
                continue
            asset_id = f"{self.product}_{tx}_{tz}"
            path = os.path.join(self.out_dir, asset_id + ".glb")
            specs = self._specs_for(prims, texture_uri_base)
            glbwrite.write_glb(path, [{"name": asset_id, "primitives": prims}], materials=specs,
                               from_bundle_frame=True)
            tiles_written.append((tx, tz))
            if progress is not None:
                progress.advance()
        return {"tiles": tiles_written, "files": len(tiles_written), "verts": total_verts, "tris": total_tris}

    def _specs_for(self, prims, texture_uri_base):
        specs = {}
        for prim in prims:
            name = prim["material"]
            base_name = name.split("@")[0]
            spec = self.material_specs.get(base_name)
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


def weld(corners, uvs, colors=None):
    """Per-corner arrays → indexed mesh: `(verts (N,3), uvs (N,2), colors (N,4)|None, tris (M,3))`,
    merging corners whose position (to `WELD_MM`), uv and colour agree."""
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
    return verts, out_uv, out_col, tris[keep]


def tile_of(x, z, tile):
    return int(math.floor(x / tile)), int(math.floor(z / tile))
