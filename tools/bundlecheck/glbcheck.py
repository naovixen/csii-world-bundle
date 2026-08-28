"""The GLB container walk, and the per-mesh, per-texture and quaternion checks."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from pathlib import Path
from typing import Any

from .report import Report

QUAT_TOLERANCE = 1e-3


def read_glb(path: Path) -> tuple[dict[str, Any], int]:
    """Return (glTF JSON, BIN chunk length). Raises ValueError on a malformed container."""
    data = path.read_bytes()
    if len(data) < 12:
        raise ValueError("shorter than a GLB header")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError(f"bad magic {magic!r}, expected b'glTF'")
    if version != 2:
        raise ValueError(f"GLB container version {version}, expected 2")
    if length != len(data):
        raise ValueError(f"header length {length} != file size {len(data)}")

    offset = 12
    gltf: dict[str, Any] | None = None
    bin_len = 0
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if offset + chunk_len > len(data):
            raise ValueError(f"chunk {chunk_type!r} overruns the file")
        payload = data[offset : offset + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            bin_len = chunk_len
        offset += chunk_len + (-chunk_len % 4)

    if gltf is None:
        raise ValueError("no JSON chunk")
    if gltf.get("asset", {}).get("version") != "2.0":
        raise ValueError(
            f"asset.version is {gltf.get('asset', {}).get('version')!r}, expected '2.0'"
        )
    return gltf, bin_len


def check_quat(rot: Any, label: str, report: Report) -> None:
    if not isinstance(rot, list) or len(rot) != 4:
        return  # already reported structurally
    norm = math.sqrt(sum(component * component for component in rot))
    if abs(norm - 1.0) > QUAT_TOLERANCE:
        report.error(f"{label}: rot is not a unit quaternion (|q| = {norm:.6f})")


def check_texture(path: Path, rel: str, label: str, report: Report) -> None:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        report.error(f"{label}: {rel!r} is not a PNG")
        return
    digest = hashlib.sha256(data).hexdigest()
    if path.stem != digest:
        report.error(
            f"{label}: {rel!r} must be named by its own SHA-256 (content hashes to {digest}); "
            "dedup-by-hash is broken otherwise"
        )


def _check_images(
    path: Path,
    gltf: dict[str, Any],
    bundle_dir: Path,
    referenced_textures: set,
    report: Report,
) -> None:
    for i, image in enumerate(gltf.get("images", [])):
        uri = image.get("uri")
        if uri is None:
            continue  # embedded in the BIN chunk; allowed, just not deduped
        if uri.startswith("data:"):
            report.warn(f"{path.name}: images[{i}] is a data: URI, which defeats texture dedup")
            continue
        target = (path.parent / uri).resolve()
        try:
            target.relative_to(bundle_dir.resolve())
        except ValueError:
            report.error(f"{path.name}: images[{i}].uri {uri!r} escapes the bundle directory")
            continue
        if not target.is_file():
            report.error(f"{path.name}: images[{i}].uri {uri!r} does not resolve to a file")
            continue
        referenced_textures.add(target)
        check_texture(target, uri, f"{path.name} images[{i}]", report)


def check_mesh(
    path: Path,
    bundle_dir: Path,
    materials: dict[str, Any],
    have_materials: bool,
    referenced_textures: set,
    report: Report,
    names: set | None = None,
) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", path.stem):
        report.error(f"{path.name}: filename stem is not a valid assetId")
    try:
        gltf, _ = read_glb(path)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.error(f"{path.name}: not a valid GLB — {exc}")
        return

    if names is not None:
        names.add(path.stem)
        for group in ("nodes", "meshes"):
            for item in gltf.get(group, []):
                name = item.get("name")
                if name:
                    names.add(name)

    for i, material in enumerate(gltf.get("materials", [])):
        name = material.get("name")
        if not name:
            report.error(
                f"{path.name}: materials[{i}] has no name, so it cannot join into materials.json"
            )
        elif have_materials and name not in materials:
            report.error(
                f"{path.name}: materials[{i}].name {name!r} is not a key in materials.json"
            )

    _check_images(path, gltf, bundle_dir, referenced_textures, report)
