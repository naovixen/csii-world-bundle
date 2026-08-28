"""The clip-cut gate's independent truth — window records, truth rasters, road cover.

Support for `tools/test_clip_cut.py`, not a test itself: everything here builds the finer-than-
the-rule sampling the gate scores against, off the real bundle. It reuses the shared scanline
(`clipraster.fill_clip_quad`) at TRUTH samples per cell deliberately — agreement then means the
cut respects the prisms, not that one rasteriser agrees with itself, because the DECISION under
test (containment at `CLIP_CONTAIN_SAMPLES`) is what varies.
"""

import json
import math
import os
import pathlib

import numpy
from csiibundle import bezier as bezier_math
from csiibundle import clipraster, containment

BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto"
)

# Independent truth sampling, finer than the rule's own. A sliver narrower than this is below
# what any 3.5 m mesh can represent and is not what this test is about.

# Independent truth sampling, finer than the rule's own. A sliver narrower than this is below
# what any 3.5 m mesh can represent and is not what this test is about.
TRUTH = 12


def load_terrain():
    from PIL import Image

    rasters = json.load(open(os.path.join(BUNDLE, "terrain", "rasters.json"), encoding="utf-8"))
    geo = json.load(open(os.path.join(BUNDLE, "bundle.json"), encoding="utf-8"))["rasters"]
    pixels = numpy.asarray(Image.open(os.path.join(BUNDLE, "terrain", "heightmap.png")))
    if pixels.ndim == 3:
        pixels = pixels[..., 0]
    heights = pixels.astype(numpy.float64) * float(rasters["elevationScale"]) + float(
        rasters["elevationOffset"]
    )
    return (
        heights,
        float(rasters["resolution"]),
        float(geo["originWorld"][0]),
        float(geo["originWorld"][2]),
    )


def strips_near(x0, x1, z0, z1):
    """Clip records whose control hull meets the window."""
    found = []
    with open(os.path.join(BUNDLE, "clipvolumes.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "area":
                nodes = record.get("nodes") or []
                if not nodes:
                    continue
                hull = numpy.asarray(nodes, dtype=numpy.float64)
            else:
                if not record.get("left") or not record.get("right"):
                    continue
                hull = numpy.asarray(record["left"] + record["right"], dtype=numpy.float64)
            if (
                hull[:, 0].max() < x0
                or hull[:, 0].min() > x1
                or hull[:, 2].max() < z0
                or hull[:, 2].min() > z1
            ):
                continue
            found.append(record)
    return found


def carves(record):
    """Is this record one the shipped rule lets DELETE terrain?

    A shallow band flattens and paints; only a trench or portal band carves. An `area` record
    is an authored clip surface and keeps its own behaviour.
    """
    if record.get("kind") == "area":
        return True
    top = record.get("top")
    return top is None or float(top) > containment.CLIP_DEEP_BAND_TOP


def quads_of(record, to_cell, cell):
    """`(quad, lane_y, bottom, top)` per station, in cell space — the harness's own stepping."""
    bottom = float(record["bottom"])
    top = float(record["top"]) if record.get("top") is not None else float("inf")
    if record.get("kind") == "area":
        nodes, triangles = record.get("nodes") or [], record.get("triangles") or []
        for tri in triangles:
            if any(not 0 <= i < len(nodes) for i in tri):
                continue
            pts = numpy.asarray([nodes[i] for i in tri], dtype=numpy.float64)
            cells = to_cell(pts)
            yield (
                [tuple(cells[0]), tuple(cells[1]), tuple(cells[2]), tuple(cells[2])],
                float(pts[:, 1].mean()),
                bottom,
                top,
            )
        return
    left, right = record["left"], record["right"]
    polygon = numpy.asarray(left, dtype=numpy.float64)
    span = float(numpy.linalg.norm(numpy.diff(polygon, axis=0), axis=1).sum())
    steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))
    t = numpy.linspace(0.0, 1.0, steps + 1)
    left_pts, right_pts = bezier_math.evaluate(left, t), bezier_math.evaluate(right, t)
    left_cells, right_cells = to_cell(left_pts), to_cell(right_pts)
    # The two sides separately, as the harness rasterises them — a band is a prism between the
    # road's own curves and they tilt across its width (`_lane_ramp`). Collapsing them to a
    # mean here would test a band the harness no longer builds.
    left_y, right_y = left_pts[:, 1], right_pts[:, 1]
    for i in range(1, steps + 1):
        yield (
            [
                tuple(left_cells[i - 1]),
                tuple(right_cells[i - 1]),
                tuple(right_cells[i]),
                tuple(left_cells[i]),
            ],
            (float((left_y[i - 1] + left_y[i]) * 0.5), float((right_y[i - 1] + right_y[i]) * 0.5)),
            bottom,
            top,
        )


def road_cover(cx, cz, radius, origin_x, origin_z, col0, row0, cell, fine_h, fine_w):
    """What the road meshes actually draw over, at TRUTH samples per cell.

    The bundle's segment meshes are the only honest answer to "is this ground hidden?" — the
    clip prism is not, which is the whole point of this test. glTF vertices are placed the way
    contract §2.2 places an instance, with the mirror the harness applies:
    `world = pos + diag(-1, 1, 1) * v` (every network record ships an identity rotation).
    """
    from inspect_glb import read_accessor, read_glb

    cover = numpy.zeros((fine_h, fine_w), dtype=bool)
    mesh_dir = os.path.join(BUNDLE, "networks")
    if not os.path.isdir(mesh_dir):
        return None
    segments = 0
    with open(os.path.join(BUNDLE, "networks.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            pos = record.get("pos")
            # A segment's own length is its reach past `pos`; 120 m clears the longest.
            if not pos or abs(pos[0] - cx) > radius + 120 or abs(pos[2] - cz) > radius + 120:
                continue
            path = os.path.join(mesh_dir, record["assetId"] + ".glb")
            if not os.path.isfile(path):
                continue
            try:
                gltf, blob = read_glb(pathlib.Path(path))
            except ValueError:
                continue
            segments += 1
            for mesh in gltf.get("meshes", []):
                for prim in mesh.get("primitives", []):
                    if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                        continue
                    verts = numpy.asarray(
                        read_accessor(gltf, blob, prim["attributes"]["POSITION"]),
                        dtype=numpy.float64,
                    )
                    if "indices" in prim:
                        idx = numpy.asarray(
                            read_accessor(gltf, blob, prim["indices"]), dtype=numpy.int64
                        )
                    else:
                        idx = numpy.arange(len(verts), dtype=numpy.int64)
                    tri = verts[idx].reshape(-1, 3, 3)
                    wx = pos[0] - tri[:, :, 0]
                    wz = pos[2] + tri[:, :, 2]
                    px = ((wx - origin_x) / cell + 0.5 - col0 - 0.5) * TRUTH
                    pz = ((origin_z - wz) / cell + 0.5 - row0 - 0.5) * TRUTH
                    fill_triangles(cover, px, pz)
    return cover if segments else None


def fill_triangles(mask, px, pz):
    """Scan-fill triangles given per-vertex in fine-pixel coordinates."""
    fine_h, fine_w = mask.shape
    for k in range(len(px)):
        y0 = max(0, int(math.floor(pz[k].min())))
        y1 = min(fine_h - 1, int(math.ceil(pz[k].max())))
        x0 = max(0, int(math.floor(px[k].min())))
        x1 = min(fine_w - 1, int(math.ceil(px[k].max())))
        if y1 < y0 or x1 < x0:
            continue
        ax, bx, cx_ = px[k]
        az, bz, cz_ = pz[k]
        det = (bz - cz_) * (ax - cx_) + (cx_ - bx) * (az - cz_)
        if abs(det) < 1e-12:
            continue
        gx, gz = numpy.meshgrid(numpy.arange(x0, x1 + 1) + 0.5, numpy.arange(y0, y1 + 1) + 0.5)
        l1 = ((bz - cz_) * (gx - cx_) + (cx_ - bx) * (gz - cz_)) / det
        l2 = ((cz_ - az) * (gx - cx_) + (ax - cx_) * (gz - cz_)) / det
        inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
        if inside.any():
            mask[y0 : y1 + 1, x0 : x1 + 1] |= inside


def truth_rasters(terrain, quads, lat_h, lat_w):
    """The band and the footprint at TRUTH samples per cell: `(fine_y, band, footprint)`."""
    fine_h, fine_w = (lat_h - 1) * TRUTH, (lat_w - 1) * TRUTH
    gc = numpy.clip((numpy.arange(fine_w) + 0.5) / TRUTH, 0, lat_w - 1.001)
    gr = numpy.clip((numpy.arange(fine_h) + 0.5) / TRUTH, 0, lat_h - 1.001)
    ci, ri = gc.astype(int), gr.astype(int)
    fu, fv = gc - ci, gr - ri
    fine_y = (
        terrain[numpy.ix_(ri, ci)] * (1 - fu)[None, :]
        + terrain[numpy.ix_(ri, ci + 1)] * fu[None, :]
    ) * (1 - fv)[:, None] + (
        terrain[numpy.ix_(ri + 1, ci)] * (1 - fu)[None, :]
        + terrain[numpy.ix_(ri + 1, ci + 1)] * fu[None, :]
    ) * fv[:, None]
    band = numpy.zeros((fine_h, fine_w), dtype=bool)
    footprint = numpy.zeros((fine_h, fine_w), dtype=bool)
    for quad, lane_y, bottom, top in quads:
        local = [((px - 0.5) * TRUTH, (py - 0.5) * TRUTH) for px, py in quad]
        clipraster.fill_clip_quad(band, fine_y, fine_w, fine_h, local, lane_y, bottom, top)
        # Footprint: the same scanline with a band that cannot reject anything.
        clipraster.fill_clip_quad(footprint, fine_y, fine_w, fine_h, local, 0.0, -1e18, 1e18)
    return fine_y, band, footprint
