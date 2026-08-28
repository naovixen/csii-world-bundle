"""The example bundle's binary payloads — checkerboard PNGs, grayscale rasters, and the cube."""

from __future__ import annotations

import json
import math
import struct
import zlib

ASSET_ID = "carto_example_cube"
MATERIAL_ID = "carto_example_cube_mat"

# A prefab the producer could not deliver geometry for. Referenced by an instance and declared
# in warnings.jsonl, so the example carries the §10 amnesty as a live case rather than prose.
MISSING_ASSET_ID = "carto_example_undeliverable_prop"

# Cube dimensions in metres: a 4 x 4 footprint, 4 tall, origin at the base centre.
HALF = 2.0
HEIGHT = 4.0


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def make_checker_png(
    size: int = 64, cell: int = 8, a: tuple = (198, 202, 206, 255), b: tuple = (126, 132, 140, 255)
) -> bytes:
    """An 8-bit RGBA checkerboard, so a consumer can see UVs are being applied."""
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None) for this scanline
        for x in range(size):
            raw.extend(a if ((x // cell) + (y // cell)) % 2 == 0 else b)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit, colour type 6 = RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def make_gray_png(samples: list, width: int, height: int, depth: int) -> bytes:
    """A single-channel PNG at bit depth 8 or 16 — the contract's raster format (§8)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        for x in range(width):
            value = samples[y * width + x]
            raw.extend(struct.pack(">H", value) if depth == 16 else struct.pack("B", value))

    ihdr = struct.pack(">IIBBBBB", width, height, depth, 0, 0, 0, 0)  # colour type 0 = grayscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _cube_geometry():
    """Return (positions, normals, uvs, indices) for a unit-ish cube.

    Authored directly in the bundle's *mesh frame*: right-handed, Y-up, front faces
    counter-clockwise. The real exporter reaches the same frame from CS2 world space by
    negating X and reversing triangle winding (see bundle.json frame.meshFrame).
    """
    edge = HALF * 2.0
    # (origin, u, v) per face; u x v == the outward normal, so (o, o+u, o+u+v, o+v) is CCW.
    faces = [
        ((HALF, 0.0, HALF), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),  # +X
        ((-HALF, 0.0, -HALF), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),  # -X
        ((-HALF, HEIGHT, HALF), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),  # +Y (top)
        ((-HALF, 0.0, -HALF), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),  # -Y (bottom)
        ((-HALF, 0.0, HALF), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # +Z
        ((HALF, 0.0, -HALF), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # -Z
    ]

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    indices: list[int] = []

    for origin, u, v in faces:
        n = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        )
        # The vertical extent of the top/bottom faces is `edge`, of the sides `HEIGHT`;
        # both happen to be 4.0 here, but scale each axis explicitly anyway.
        su = edge
        sv = edge if abs(n[1]) > 0.5 else HEIGHT
        corners = [
            origin,
            tuple(origin[i] + u[i] * su for i in range(3)),
            tuple(origin[i] + u[i] * su + v[i] * sv for i in range(3)),
            tuple(origin[i] + v[i] * sv for i in range(3)),
        ]
        base = len(positions)
        positions.extend(corners)  # type: ignore[arg-type]
        normals.extend([n] * 4)
        uvs.extend([(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)])  # glTF UV origin is top-left
        indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])

    return positions, normals, uvs, indices


def _cube_buffers(pos_blob, nrm_blob, uv_blob, idx_blob, offsets, positions, counts):
    """The glTF buffers/bufferViews/accessors block for the packed cube."""
    pos_off, nrm_off, uv_off, idx_off, bin_len = offsets
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    n_normals, n_uvs, n_indices = counts
    return {
        "buffers": [{"byteLength": bin_len}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": pos_off, "byteLength": len(pos_blob), "target": 34962},
            {"buffer": 0, "byteOffset": nrm_off, "byteLength": len(nrm_blob), "target": 34962},
            {"buffer": 0, "byteOffset": uv_off, "byteLength": len(uv_blob), "target": 34962},
            {"buffer": 0, "byteOffset": idx_off, "byteLength": len(idx_blob), "target": 34963},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(positions),
                "type": "VEC3",
                "min": [min(xs), min(ys), min(zs)],
                "max": [max(xs), max(ys), max(zs)],
            },
            {"bufferView": 1, "componentType": 5126, "count": n_normals, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": n_uvs, "type": "VEC2"},
            {"bufferView": 3, "componentType": 5123, "count": n_indices, "type": "SCALAR"},
        ],
    }


def _cube_gltf(texture_rel_uri: str) -> dict:
    """The document half of the cube glTF — everything but the packed buffers."""
    return {
        "asset": {"version": "2.0", "generator": "csii-world-bundle example bundle generator"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": ASSET_ID, "mesh": 0}],
        "meshes": [
            {
                "name": ASSET_ID,
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                        "indices": 3,
                        "material": 0,
                        "mode": 4,
                    }
                ],
            }
        ],
        # The glTF material's *name* is the join key into materials.json.
        "materials": [
            {
                "name": MATERIAL_ID,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0, "texCoord": 0},
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.85,
                },
                "doubleSided": False,
            }
        ],
        "textures": [{"sampler": 0, "source": 0}],
        "images": [{"uri": texture_rel_uri}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
    }


def make_cube_glb(texture_rel_uri: str) -> bytes:
    positions, normals, uvs, indices = _cube_geometry()

    pos_blob = b"".join(struct.pack("<3f", *p) for p in positions)
    nrm_blob = b"".join(struct.pack("<3f", *n) for n in normals)
    uv_blob = b"".join(struct.pack("<2f", *t) for t in uvs)
    idx_blob = b"".join(struct.pack("<H", i) for i in indices)

    def pad4(blob: bytes) -> bytes:
        return blob + b"\x00" * (-len(blob) % 4)

    pos_off = 0
    nrm_off = pos_off + len(pad4(pos_blob))
    uv_off = nrm_off + len(pad4(nrm_blob))
    idx_off = uv_off + len(pad4(uv_blob))
    bin_blob = pad4(pos_blob) + pad4(nrm_blob) + pad4(uv_blob) + pad4(idx_blob)

    gltf = _cube_gltf(texture_rel_uri)
    gltf.update(
        _cube_buffers(
            pos_blob,
            nrm_blob,
            uv_blob,
            idx_blob,
            (pos_off, nrm_off, uv_off, idx_off, len(bin_blob)),
            positions,
            (len(normals), len(uvs), len(indices)),
        )
    )

    json_blob = json.dumps(gltf, separators=(",", ":"), sort_keys=False).encode("utf-8")
    json_blob += b" " * (-len(json_blob) % 4)

    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    out = bytearray()
    out += struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(json_blob), b"JSON") + json_blob
    out += struct.pack("<I4s", len(bin_blob), b"BIN\x00") + bin_blob
    return bytes(out)


def yaw_quat(degrees: float) -> list[float]:
    """Left-handed rotation about +Y, as [x, y, z, w]."""
    half = math.radians(degrees) * 0.5
    return [0.0, round(math.sin(half), 9), 0.0, round(math.cos(half), 9)]
