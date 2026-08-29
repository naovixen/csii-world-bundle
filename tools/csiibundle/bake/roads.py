"""The road surface for the bake: every network segment mesh placed into Z-up world space and
indexed as one `drape.TriangleSoupSurface`.

Network meshes are world-space already except for a translation (`networks.jsonl` `pos`
places the segment's origin, §5) and the §2.2 X mirror; the loop below applies both, swaps to
Z-up, and hands the pieces to the surface. Full city: 7,289 files, 9.3M triangles, ~8 s to
load and ~4 s to index (measured 2026-08-15). A `region` (`csiibundle.region`, a bundle-frame SQUARE) keeps only the
segments whose origin lies within `radius + reach` of its centre on both axes, so a marking at
the window's edge still finds the neighbouring segment it drapes onto.
"""

import os

import numpy

from .. import drape, glb, reader
from .. import region as region_rule

REACH = 150.0  # metres past the region a segment origin may sit and still matter (test_drape_pick's REACH)


# The last surface built, keyed on what shaped it. Two products in one bake — `markings` and
# `areas` — need the same road surface, and each built its own: 7,289 files and 9.3 M triangles
# parsed and indexed twice per bake. One entry, not a cache: a bake has one bundle and one
# region, so the second product is a hit and nothing else ever is.
_last = None


def load_road_surface(bundle_dir, region=None, progress=None, cell=8.0):
    """`(surface, stats)`; `surface` may be an empty soup when the layer is absent."""
    global _last
    key = (os.path.abspath(bundle_dir), None if region is None else tuple(region), cell)
    if _last is not None and _last[0] == key:
        if progress is not None:
            progress.begin("road surface", total=1, unit="segments")
            progress.end("reused the surface the previous product built")
        return _last[1], dict(_last[2])
    surface, stats = _build_road_surface(bundle_dir, region, progress, cell)
    _last = (key, surface, stats)
    return surface, dict(stats)


def _build_road_surface(bundle_dir, region, progress, cell):
    lay = reader.resolve_layers(bundle_dir)
    if not os.path.isfile(lay.networks) or not os.path.isdir(lay.net_meshes):
        return drape.TriangleSoupSurface(
            numpy.zeros((0, 3), numpy.float32), numpy.zeros((0, 3), numpy.int64), cell
        ), {"segments": 0, "files": 0, "refused": 0, "tris": 0}
    records = [
        r
        for r in reader.read_jsonl(lay.networks)
        if r.get("kind") == "segment" and r.get("assetId") and r.get("pos")
    ]
    if region:
        records = [
            rec
            for rec in records
            if region_rule.contains(rec["pos"][0], rec["pos"][2], region, reach=REACH)
        ]
    if progress is not None:
        progress.begin("road surface", total=len(records), unit="segments")
    pieces = []
    files = refused = tris = 0
    for i, rec in enumerate(records):
        path = os.path.join(lay.net_meshes, rec["assetId"] + ".glb")
        if not os.path.isfile(path):
            continue
        nodes = glb.parse_glb(path)
        files += 1
        if nodes is None:
            refused += 1
            continue
        pos = numpy.asarray(rec["pos"], dtype=numpy.float64)
        for node in nodes:
            v = numpy.asarray(node["verts"], dtype=numpy.float64)
            m = numpy.asarray(node["matrix"], dtype=numpy.float64)
            v = v @ m[:3, :3].T + m[:3, 3]
            v[:, 0] *= -1.0  # undo the §2.2 mirror
            v += pos  # bundle world
            pieces.append((v[:, [0, 2, 1]].astype(numpy.float32), node["tris"]))  # -> Z-up
            tris += len(node["tris"])
        if progress is not None and i % 200 == 0:
            progress.advance(200 if i + 200 <= len(records) else len(records) - i)
            progress.check()
    surface = drape.TriangleSoupSurface.from_pieces(pieces, cell=cell)
    if progress is not None:
        progress.end(f"{files} segment meshes, {tris:,} triangles indexed ({refused} refused)")
    return surface, {"segments": len(records), "files": files, "refused": refused, "tris": tris}
