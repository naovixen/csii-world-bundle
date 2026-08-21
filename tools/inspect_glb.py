#!/usr/bin/env python3
"""Inspect and sanity-check .glb files produced by the ripper.

Standalone: Python 3.8+, standard library only. Where `validate_bundle.py` checks a whole
bundle, this drills into individual meshes — useful during PLAN Phase 1, when the question is
"did real geometry actually come out of the game?" and the answer wants to be objective before
anyone opens Blender.

Checks per file:
  * GLB container and glTF 2.0 structure
  * accessor counts, and POSITION min/max against the vertices actually stored
  * every index within the vertex range, index count a multiple of 3
  * degenerate triangles (two shared vertices), which indicate a bad index base
  * the mesh's real-world size in metres — the strongest signal that readback worked,
    since a building should measure tens of metres, not 0.001 or 1e6

Usage:  python tools/inspect_glb.py <file-or-directory> [-v]
Exit:   0 = all files look sane, 1 = a problem was found, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

COMPONENT = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
ARITY = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if len(data) < 12:
        raise ValueError("shorter than a GLB header")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError(f"bad magic {magic!r}")
    if version != 2:
        raise ValueError(f"container version {version}, expected 2")
    if length != len(data):
        raise ValueError(f"header length {length} != file size {len(data)}")

    offset, gltf, blob = 12, None, b""
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if offset + chunk_len > len(data):
            raise ValueError(f"chunk {chunk_type!r} overruns the file")
        payload = data[offset : offset + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            blob = payload
        offset += chunk_len + (-chunk_len % 4)

    if gltf is None:
        raise ValueError("no JSON chunk")
    if gltf.get("asset", {}).get("version") != "2.0":
        raise ValueError(f"asset.version {gltf.get('asset', {}).get('version')!r}, expected '2.0'")
    return gltf, blob


def read_accessor(gltf: dict[str, Any], blob: bytes, index: int) -> list[Any]:
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    fmt, width = COMPONENT[acc["componentType"]]
    arity = ARITY[acc["type"]]
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or (arity * width)

    out = []
    for i in range(acc["count"]):
        values = struct.unpack_from("<" + fmt * arity, blob, base + i * stride)
        out.append(values[0] if arity == 1 else values)
    return out


def inspect(path: Path, verbose: bool) -> list[str]:
    problems: list[str] = []
    try:
        gltf, blob = read_glb(path)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, struct.error) as exc:
        return [f"not a valid GLB — {exc}"]

    meshes = gltf.get("meshes", [])
    if not meshes:
        problems.append("no meshes")

    for mi, mesh in enumerate(meshes):
        name = mesh.get("name", f"<mesh {mi}>")
        total_tris = 0
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3

        for pi, prim in enumerate(mesh.get("primitives", [])):
            label = f"{name}/prim{pi}"
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                problems.append(f"{label}: no POSITION attribute")
                continue

            try:
                positions = read_accessor(gltf, blob, attrs["POSITION"])
            except (struct.error, KeyError, IndexError) as exc:
                problems.append(f"{label}: POSITION accessor unreadable — {exc}")
                continue

            for p in positions:
                for a in range(3):
                    lo[a] = min(lo[a], p[a])
                    hi[a] = max(hi[a], p[a])

            # Declared bounds must match the stored data; viewers cull against them, so a
            # stale min/max silently makes a mesh invisible.
            acc = gltf["accessors"][attrs["POSITION"]]
            if "min" in acc and "max" in acc:
                actual_lo = [min(p[a] for p in positions) for a in range(3)]
                actual_hi = [max(p[a] for p in positions) for a in range(3)]
                for a in range(3):
                    if (
                        abs(acc["min"][a] - actual_lo[a]) > 1e-4
                        or abs(acc["max"][a] - actual_hi[a]) > 1e-4
                    ):
                        problems.append(
                            f"{label}: POSITION accessor bounds disagree with the data on axis {a} "
                            f"(declared {acc['min'][a]:.4f}..{acc['max'][a]:.4f}, "
                            f"actual {actual_lo[a]:.4f}..{actual_hi[a]:.4f})"
                        )
                        break
            else:
                problems.append(f"{label}: POSITION accessor has no min/max (required by glTF)")

            for other in ("NORMAL", "TEXCOORD_0", "TANGENT"):
                if other in attrs and gltf["accessors"][attrs[other]]["count"] != len(positions):
                    problems.append(f"{label}: {other} count != POSITION count")

            if "indices" not in prim:
                problems.append(f"{label}: non-indexed geometry, which the ripper never emits")
                continue

            try:
                indices = read_accessor(gltf, blob, prim["indices"])
            except (struct.error, KeyError, IndexError) as exc:
                problems.append(f"{label}: index accessor unreadable — {exc}")
                continue

            if len(indices) % 3 != 0:
                problems.append(f"{label}: {len(indices)} indices is not a multiple of 3")
            if indices and (max(indices) >= len(positions) or min(indices) < 0):
                problems.append(
                    f"{label}: index range {min(indices)}..{max(indices)} escapes the "
                    f"{len(positions)}-vertex buffer — likely a submesh index base error"
                )
                continue

            degenerate = sum(
                1
                for t in range(0, len(indices) - 2, 3)
                if len({indices[t], indices[t + 1], indices[t + 2]}) < 3
            )
            if degenerate:
                problems.append(f"{label}: {degenerate} degenerate triangle(s)")

            total_tris += len(indices) // 3
            if verbose:
                mat = (
                    gltf.get("materials", [])[prim["material"]].get("name")
                    if "material" in prim
                    else None
                )
                print(
                    f"    {label}: {len(positions)} verts, {len(indices) // 3} tris, material {mat!r}"
                )

        if total_tris == 0:
            problems.append(f"{name}: no triangles")

        if lo[0] != float("inf"):
            size = [hi[a] - lo[a] for a in range(3)]
            print(
                f"  {name}: {total_tris} tris, "
                f"size {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} m, "
                f"y {lo[1]:.2f}..{hi[1]:.2f}"
            )
            biggest = max(size)
            if biggest < 0.01:
                problems.append(
                    f"{name}: {biggest:.5f} m across — implausibly tiny, check units/scale"
                )
            elif biggest > 10000:
                problems.append(
                    f"{name}: {biggest:.1f} m across — implausibly huge, check units/scale"
                )

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Inspect ripper-produced .glb files.")
    parser.add_argument("target", type=Path, help="a .glb file, or a directory of them")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every primitive")
    args = parser.parse_args(argv)

    if args.target.is_dir():
        files = sorted(args.target.rglob("*.glb"))
    elif args.target.is_file():
        files = [args.target]
    else:
        print(f"error: {args.target} not found", file=sys.stderr)
        return 2

    if not files:
        print(f"error: no .glb files under {args.target}", file=sys.stderr)
        return 2

    total_problems = 0
    for path in files:
        print(f"\n=== {path.name}  ({path.stat().st_size} bytes)")
        problems = inspect(path, args.verbose)
        for p in problems:
            print(f"  PROBLEM {p}")
        total_problems += len(problems)

    print(f"\n{len(files)} file(s), {total_problems} problem(s)")
    return 1 if total_problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
