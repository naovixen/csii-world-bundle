"""BeamNG `.ter` v9 heightmap files — write, read, and the orientation contract.

Binary layout (`small_island.terrain.json` spells it out and `tools/beamng/probe_ter.py`
verified the orientation on 3,800 road nodes at 2 cm median):

    u8   version (9)
    u32  size                       square: size × size samples
    u16  heights[size*size]         h = stored * maxHeight / 65536 + TerrainBlock.position.z
    u8   layers[size*size]          material index into `names`; 255 = hole (square not drawn)
    u32  count
    (u8 len, bytes) × count         material internal names (short Torque strings)

Orientation: `heights[row][col]` with `x = position.x + col * squareSize` (col 0 WEST) and
`y = position.y + row * squareSize` (row 0 SOUTH). The bundle's rasters have row 0 NORTH
(§8), so `from_bundle_arrays` flips vertically. A layer value at sample `(r, c)` applies to the
square that starts at that sample and extends toward +row/+col.

The `.terrain.json` sidecar carries the material names again plus sizes; BeamNG reads both.
"""

import json
import struct

import numpy

VERSION = 9
HOLE = 255


def write_ter(path, heights_u16, layers_u8, names):
    """Write a v9 `.ter`. `heights_u16` and `layers_u8` are `(size, size)` in BeamNG row order
    (row 0 south); `names` the material list the layer bytes index."""
    heights = numpy.ascontiguousarray(heights_u16, dtype="<u2")
    layers = numpy.ascontiguousarray(layers_u8, dtype="u1")
    if heights.ndim != 2 or heights.shape[0] != heights.shape[1]:
        raise ValueError(f".ter must be square, got {heights.shape}")
    if layers.shape != heights.shape:
        raise ValueError(f"layer map {layers.shape} does not match heights {heights.shape}")
    size = heights.shape[0]
    used = numpy.unique(layers)
    if len(names) > 254:
        raise ValueError("a .ter carries at most 254 materials (255 is the hole marker)")
    for value in used:
        if value != HOLE and value >= len(names):
            raise ValueError(f"layer index {value} has no material name")
    with open(path, "wb") as handle:
        handle.write(struct.pack("<BI", VERSION, size))
        handle.write(heights.tobytes())
        handle.write(layers.tobytes())
        handle.write(struct.pack("<I", len(names)))
        for name in names:
            raw = name.encode("utf-8")
            if len(raw) > 255:
                raise ValueError(f"material name too long: {name!r}")
            handle.write(struct.pack("<B", len(raw)))
            handle.write(raw)


def read_ter(path):
    """`(size, heights_u16, layers_u8, names)`; the inverse of `write_ter`."""
    with open(path, "rb") as handle:
        data = handle.read()
    version = data[0]
    if version != VERSION:
        raise ValueError(f"unsupported .ter version {version}")
    size = struct.unpack_from("<I", data, 1)[0]
    off = 5
    heights = numpy.frombuffer(data, dtype="<u2", count=size * size, offset=off).reshape(size, size)
    off += size * size * 2
    layers = numpy.frombuffer(data, dtype="u1", count=size * size, offset=off).reshape(size, size)
    off += size * size
    count = struct.unpack_from("<I", data, off)[0]
    off += 4
    names = []
    for _ in range(count):
        length = data[off]
        off += 1
        names.append(data[off : off + length].decode("utf-8", "replace"))
        off += length
    return size, heights, layers, names


def terrain_json(level_name, ter_rel_path, size, names):
    """The `.terrain.json` sidecar BeamNG keeps beside the binary."""
    return {
        "binaryFormat": "version(char), size(unsigned int), heightMap(heightMapSize * heightMapItemSize), "
        "layerMap(layerMapSize * layerMapItemSize), layerTextureMap(layerMapSize * layerMapItemSize), "
        "materialNames",
        "datafile": ter_rel_path,
        "heightMapItemSize": 2,
        "heightMapSize": size * size,
        "layerMapItemSize": 1,
        "layerMapSize": size * size,
        "materials": list(names),
        "size": size,
        "version": VERSION,
    }


def write_terrain_json(path, level_name, ter_rel_path, size, names):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(terrain_json(level_name, ter_rel_path, size, names), handle, indent=2)


def from_bundle_arrays(heights_m, layers_bundle, elevation_scale, elevation_offset):
    """Turn bundle-order arrays (row 0 north) into `.ter` payloads (row 0 south).

    Heights are quantised LOSSLESSLY into the bundle's own encoding: `stored = (h − offset) /
    scale`, so `maxHeight = 65536 · scale` and `position.z = offset` reproduce every sample
    exactly. Returns `(heights_u16, layers_u8, max_height, position_z)`.
    """
    h = numpy.asarray(heights_m, dtype=numpy.float64)
    stored = numpy.rint((h - elevation_offset) / max(elevation_scale, 1e-12))
    stored = numpy.clip(stored, 0, 65535).astype("<u2")
    layers = numpy.asarray(layers_bundle, dtype="u1")
    return (
        stored[::-1].copy(),
        layers[::-1].copy(),
        65536.0 * elevation_scale,
        float(elevation_offset),
    )


def sample_height(heights_u16, max_height, position, square_size, x, y):
    """Bilinear `.ter` height at BeamNG world `(x, y)`, or None outside the block.

    **APPROXIMATED, declared and counted.** BeamNG renders the heightfield as TRIANGLES, and
    `csiibundle.drape.HeightfieldSurface` says outright that interpolating bilinearly instead of
    picking a diagonal "answers a different question from the one the rendered mesh answers, by
    a measured p95 of 9.4 cm at 3.5 m cells and 41 cm at a 14 m stride". This is the bilinear
    one, so it is not the height a wheel would touch.

    It stays bilinear because the deviation was measured on the thing that uses it and is not
    there. `levelcheck.terrain._ground_check` asks whether a building sits within
    `GROUND_TOLERANCE` = 0.5 m of the ground; over 1,441 sampled buildings on `cs2_city_fine`
    (square size **1.75 m**, the fine cascade) bilinear and the measured `ne_sw` triangulation
    disagree by a median and p95 of **0.0000 m** and a maximum of **0.0154 m**, and **no**
    sample moves across the tolerance. Buildings sit on ground the game has graded flat, which
    is exactly where the two agree.

    The 9.4 cm and 41 cm figures are at 3.5 m and 14 m; a caller sampling a COARSE grid, or
    sampling somewhere the ground is not flat (a slope, a cut, a shoulder), must use the
    diagonal-aware surface instead. `sampler` in the ground check's result records which one
    produced its numbers.
    """
    size = heights_u16.shape[0]
    fc = (x - position[0]) / square_size
    fr = (y - position[1]) / square_size
    if fc < 0 or fr < 0 or fc > size - 1 or fr > size - 1:
        return None
    c0, r0 = int(fc), int(fr)
    c1, r1 = min(c0 + 1, size - 1), min(r0 + 1, size - 1)
    tc, tr = fc - c0, fr - r0
    scale = max_height / 65536.0
    h00 = float(heights_u16[r0, c0]) * scale
    h01 = float(heights_u16[r0, c1]) * scale
    h10 = float(heights_u16[r1, c0]) * scale
    h11 = float(heights_u16[r1, c1]) * scale
    top = h00 * (1 - tc) + h01 * tc
    bottom = h10 * (1 - tc) + h11 * tc
    return top * (1 - tr) + bottom * tr + position[2]
