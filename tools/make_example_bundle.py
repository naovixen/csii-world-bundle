#!/usr/bin/env python3
"""Generate the hand-authored example CS2 World Bundle (schema 1).

The example is the executable half of the Phase-0 schema freeze: it is a tiny but
*complete* bundle (one cube mesh, one texture, one material, two instances, one
centerline network segment, a manifest) that `tools/validate_bundle.py` accepts.
A consumer can be built and smoke-tested against it with no game and no mod.

Everything here is deterministic — re-running rewrites byte-identical files.

Usage:  python tools/make_example_bundle.py [output_dir]
        (default output_dir: docs/example-bundle)
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "example-bundle"

ASSET_ID = "carto_example_cube"
MATERIAL_ID = "carto_example_cube_mat"

# Cube dimensions in metres: a 4 x 4 footprint, 4 tall, origin at the base centre.
HALF = 2.0
HEIGHT = 4.0


# --------------------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def make_checker_png(size: int = 64, cell: int = 8) -> bytes:
    """An 8-bit RGBA checkerboard, so a consumer can see UVs are being applied."""
    a = (198, 202, 206, 255)
    b = (126, 132, 140, 255)
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


# --------------------------------------------------------------------------------------
# GLB
# --------------------------------------------------------------------------------------

def _cube_geometry():
    """Return (positions, normals, uvs, indices) for a unit-ish cube.

    Authored directly in the bundle's *mesh frame*: right-handed, Y-up, front faces
    counter-clockwise. The real exporter reaches the same frame from CS2 world space by
    negating X and reversing triangle winding (see bundle.json frame.meshFrame).
    """
    edge = HALF * 2.0
    # (origin, u, v) per face; u x v == the outward normal, so (o, o+u, o+u+v, o+v) is CCW.
    faces = [
        ((HALF, 0.0, HALF), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),      # +X
        ((-HALF, 0.0, -HALF), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),     # -X
        ((-HALF, HEIGHT, HALF), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),  # +Y (top)
        ((-HALF, 0.0, -HALF), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),     # -Y (bottom)
        ((-HALF, 0.0, HALF), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),      # +Z
        ((HALF, 0.0, -HALF), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),     # -Z
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

    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]

    gltf = {
        "asset": {"version": "2.0", "generator": "carto-ripper example bundle generator"},
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
        "buffers": [{"byteLength": len(bin_blob)}],
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
            {"bufferView": 1, "componentType": 5126, "count": len(normals), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": len(uvs), "type": "VEC2"},
            {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
    }

    json_blob = json.dumps(gltf, separators=(",", ":"), sort_keys=False).encode("utf-8")
    json_blob += b" " * (-len(json_blob) % 4)

    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    out = bytearray()
    out += struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(json_blob), b"JSON") + json_blob
    out += struct.pack("<I4s", len(bin_blob), b"BIN\x00") + bin_blob
    return bytes(out)


# --------------------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------------------

def _yaw_quat(degrees: float) -> list[float]:
    """Left-handed rotation about +Y, as [x, y, z, w]."""
    half = math.radians(degrees) * 0.5
    return [0.0, round(math.sin(half), 9), 0.0, round(math.cos(half), 9)]


def build(out_dir: Path) -> None:
    (out_dir / "meshes").mkdir(parents=True, exist_ok=True)
    (out_dir / "textures").mkdir(parents=True, exist_ok=True)

    png = make_checker_png()
    tex_hash = hashlib.sha256(png).hexdigest()
    tex_rel = f"textures/{tex_hash}.png"
    (out_dir / tex_rel).write_bytes(png)

    glb = make_cube_glb(f"../{tex_rel}")
    (out_dir / "meshes" / f"{ASSET_ID}.glb").write_bytes(glb)

    materials = {
        MATERIAL_ID: {
            "baseColor": tex_rel,
            "tint": [1.0, 1.0, 1.0, 1.0],
            "alphaMode": "opaque",
            "doubleSided": False,
            "flags": [],
        }
    }
    (out_dir / "materials.json").write_text(
        json.dumps(materials, indent=2) + "\n", encoding="utf-8"
    )

    instances = [
        {
            "id": 1001,
            "assetId": ASSET_ID,
            "kind": "building",
            "pos": [120.0, 12.5, -340.0],
            "rot": [0.0, 0.0, 0.0, 1.0],
            "scale": [1.0, 1.0, 1.0],
            "attrs": {
                "name": "Example Building A",
                "category": "Residential",
                "zone": "Residential Low Density",
                "theme": "European",
                "level": 3,
                "elevation": 12.5,
                "height": 4.0,
                "state": "normal",
            },
        },
        {
            "id": 1002,
            "assetId": ASSET_ID,
            "kind": "building",
            "pos": [160.0, 12.5, -340.0],
            "rot": _yaw_quat(90.0),
            "attrs": {
                "name": "Example Building B",
                "category": "Residential",
                "zone": "Residential Low Density",
                "theme": "European",
                "level": 1,
                "elevation": 12.5,
                "height": 4.0,
                "state": "normal",
            },
        },
    ]
    with (out_dir / "instances.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for record in instances:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    networks = [
        {
            "id": 2001,
            "kind": "segment",
            "centerline": [[100.0, 12.0, -360.0], [140.0, 12.0, -360.0], [180.0, 12.2, -360.0]],
            "width": 16.0,
            "attrs": {
                "name": "Example Road",
                "asset": "Medium Road",
                "category": "Medium",
                "form": "normal",
                "lanes": 4,
                "direction": "twoway",
                "limit": 50.0,
                "length": 80.0,
                "width": 16.0,
                "elevation": 0.0,
            },
        }
    ]
    with (out_dir / "networks.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for record in networks:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    # Bounds must enclose every placed mesh. The cube reaches HALF*sqrt(2) horizontally
    # under an arbitrary yaw, so pad the footprint by that instead of by HALF.
    reach = HALF * math.sqrt(2.0)
    pts: list[tuple[float, float, float]] = []
    for record in instances:
        x, y, z = record["pos"]
        pts.append((x - reach, y, z - reach))
        pts.append((x + reach, y + HEIGHT, z + reach))
    for record in networks:
        for x, y, z in record["centerline"]:
            half_w = record["width"] * 0.5
            pts.append((x - half_w, y, z - half_w))
            pts.append((x + half_w, y, z + half_w))

    bounds = {
        "min": [round(min(p[i] for p in pts), 6) for i in range(3)],
        "max": [round(max(p[i] for p in pts), 6) for i in range(3)],
    }

    manifest = {
        "schema": 1,
        "generator": "carto-ripper example bundle generator",
        "generatedAt": "2026-01-01T00:00:00Z",
        "game": {"name": "Cities: Skylines II", "version": "1.5.7f1", "mode": "Game"},
        "city": "Example City",
        "map": "Example Map",
        "frame": {
            "units": "m",
            "handedness": "left",
            "up": "Y",
            "axes": "X=east,Y=up,Z=north",
            "meshFrame": {
                "handedness": "right",
                "conversion": "negate-x",
                "windingReversed": True,
                "note": (
                    "glTF requires a right-handed frame, so mesh payloads are mirrored on X. "
                    "To place a mesh: v_world = T(pos) * R(rot) * S(scale) * diag(-1,1,1) * v_glb. "
                    "Instance transforms, bounds and rasters are raw bundle-frame values and are "
                    "NOT mirrored."
                ),
            },
        },
        "geo": {
            "crs": "EPSG:32651",
            "projection": "UTM",
            "tmParams": {
                "ellipsoid": "WGS84",
                "semiMajor": 6378137.0,
                "inverseFlattening": 298.257223563,
                "originLon": 123.0,
                "originLat": 0.0,
                "falseEasting": 500000.0,
                "falseNorthing": 0.0,
                "scaleFactor": 0.9996,
                "utmZone": 51,
                "hemisphere": "N",
                "toWgs84": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            },
            "originLatLon": [25.033, 121.5654],
            "originProjected": [353128.0, 2770346.0],
            "originWorld": [0.0, 0.0, 0.0],
            "north": 0.0,
        },
        "bounds": bounds,
        "mapSize": 14336.0,
        "counts": {
            "meshes": 1,
            "materials": len(materials),
            "textures": 1,
            "instances": len(instances),
            "networkSegments": len(networks),
            "warnings": 0,
        },
        "layers": {
            "instances": "instances.jsonl",
            "networks": "networks.jsonl",
            "materials": "materials.json",
            "meshes": "meshes/",
            "textures": "textures/",
        },
        "ripOptions": {
            "meshSource": "runtime",
            "lodCap": 0,
            "textureSizeCap": 2048,
            "includeSubObjects": True,
            "includeVector": False,
        },
    }
    (out_dir / "bundle.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    print(f"wrote example bundle to {out_dir}")
    print(f"  texture sha256 {tex_hash}")
    print(f"  {ASSET_ID}.glb {len(glb)} bytes")


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT)
