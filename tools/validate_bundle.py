#!/usr/bin/env python3
"""Standalone validator for a CS2 World Bundle (schema 1).

Standalone means exactly that: Python 3.8+ and the standard library, no pip installs, no
game, no Carto source. It is the reference check for the Phase-0 contract, and the thing a
consumer author runs to confirm a bundle they were handed is well-formed.

It does two jobs:

  1. Structural — validates bundle.json, every line of instances.jsonl / networks.jsonl and
     materials.json against docs/schema/*.schema.json, using a small built-in evaluator for
     the JSON Schema subset those files use.
  2. Semantic — the cross-file invariants a schema cannot state: every assetId resolves to a
     mesh file, every texture reference resolves and is named by its own SHA-256, every .glb
     is a well-formed glTF 2.0 container whose material names join into materials.json,
     manifest counts match reality, and bounds actually enclose the content.

Usage:  python tools/validate_bundle.py <bundle_dir> [--schema-dir DIR] [-q]
Exit:   0 = valid (warnings allowed), 1 = invalid, 2 = could not run.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_DIR = REPO_ROOT / "docs" / "schema"

QUAT_TOLERANCE = 1e-3
BOUNDS_TOLERANCE = 1e-6


# ======================================================================================
# At-rest compression (contract §12.1)
# ======================================================================================
#
# A bulk layer may ship compressed, and when it does the suffix says so: `instances.jsonl.gz`,
# `meshes/<assetId>.glb.gz`. Both forms are valid schema-1 bundles, so every path this
# validator opens goes through `bundle_file` and is read through the two helpers below rather
# than with a bare `open`. Textures and terrain rasters are excluded on purpose — PNG is
# already deflate-compressed, and re-wrapping it gains nothing.

COMPRESSED_SUFFIX = ".gz"


def bundle_file(path: Path) -> Optional[Path]:
    """The file actually on disk for a layer's logical path, compressed or not, or None."""
    if path.is_file():
        return path
    packed = path.with_name(path.name + COMPRESSED_SUFFIX)
    return packed if packed.is_file() else None


def read_bundle_bytes(path: Path) -> bytes:
    """Read a layer whole, decompressing if it is stored compressed."""
    if path.name.endswith(COMPRESSED_SUFFIX):
        with gzip.open(path, "rb") as handle:
            return handle.read()
    return path.read_bytes()


def open_bundle_text(path: Path):
    """Open a layer as UTF-8 text, decompressing if it is stored compressed."""
    if path.name.endswith(COMPRESSED_SUFFIX):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def layer_stem(path: Path) -> str:
    """The assetId a mesh file names, with the compression suffix stripped first.

    `Path.stem` alone answers `AppleTree01.glb` for `AppleTree01.glb.gz`, which would break
    every join between a file on disk and the assetId that references it.
    """
    name = path.name
    if name.endswith(COMPRESSED_SUFFIX):
        name = name[: -len(COMPRESSED_SUFFIX)]
    return name.rsplit(".", 1)[0]


def glob_layer(directory: Path, extension: str) -> List[Path]:
    """Every file of one extension in a layer directory, in either storage form."""
    return sorted(list(directory.glob(f"*{extension}"))
                  + list(directory.glob(f"*{extension}{COMPRESSED_SUFFIX}")))


# ======================================================================================
# Minimal JSON Schema evaluator (the draft 2020-12 subset used by docs/schema/*.json)
# ======================================================================================

class SchemaError(Exception):
    """Raised when a schema itself uses a keyword this evaluator does not implement."""


SUPPORTED = {
    "$schema", "$id", "$defs", "$ref", "title", "description", "default", "examples",
    "type", "const", "enum", "required", "properties", "additionalProperties",
    "patternProperties", "propertyNames", "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "oneOf", "anyOf", "allOf", "not",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_ok(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return _is_int(value) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return _is_num(value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise SchemaError(f"unknown type name {name!r}")


class Validator:
    """Evaluates one schema document (with local $ref into its own $defs)."""

    def __init__(self, schema: Dict[str, Any], name: str) -> None:
        self.root = schema
        self.name = name
        self._check_supported(schema, "#")

    def _check_supported(self, node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, sub in node.items():
                if key in ("properties", "$defs", "patternProperties"):
                    for prop, prop_schema in sub.items():
                        self._check_supported(prop_schema, f"{path}/{key}/{prop}")
                    continue
                if key not in SUPPORTED:
                    raise SchemaError(f"{self.name}: unsupported keyword {key!r} at {path}")
                if isinstance(sub, (dict, list)):
                    self._check_supported(sub, f"{path}/{key}")
        elif isinstance(node, list):
            for i, sub in enumerate(node):
                self._check_supported(sub, f"{path}/{i}")

    def _resolve(self, ref: str) -> Dict[str, Any]:
        if not ref.startswith("#/"):
            raise SchemaError(f"{self.name}: only local $ref is supported, got {ref!r}")
        node: Any = self.root
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[token]
        return node

    def validate(self, instance: Any) -> List[str]:
        errors: List[str] = []
        self._apply(self.root, instance, "$", errors)
        return errors

    def _apply(self, schema: Any, value: Any, path: str, errors: List[str]) -> None:
        if schema is True or schema == {}:
            return
        if schema is False:
            errors.append(f"{path}: schema forbids any value here")
            return

        if "$ref" in schema:
            self._apply(self._resolve(schema["$ref"]), value, path, errors)
            # Sibling keywords alongside $ref are still applied (2020-12 semantics).

        if "type" in schema:
            names = schema["type"]
            names = [names] if isinstance(names, str) else names
            if not any(_type_ok(value, n) for n in names):
                errors.append(f"{path}: expected type {'|'.join(names)}, got {_kind(value)}")
                return

        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

        if isinstance(value, str):
            self._apply_string(schema, value, path, errors)
        if _is_num(value):
            self._apply_number(schema, value, path, errors)
        if isinstance(value, list):
            self._apply_array(schema, value, path, errors)
        if isinstance(value, dict):
            self._apply_object(schema, value, path, errors)

        for key, combine in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "one")):
            if key not in schema:
                continue
            passing = []
            branch_errors: List[str] = []
            for i, branch in enumerate(schema[key]):
                sub: List[str] = []
                self._apply(branch, value, path, sub)
                if sub:
                    label = branch.get("title", f"branch {i}") if isinstance(branch, dict) else f"branch {i}"
                    branch_errors.extend(f"[{label}] {e}" for e in sub)
                else:
                    passing.append(i)
            if combine == "all" and branch_errors:
                errors.extend(branch_errors)
            elif combine == "any" and not passing:
                errors.append(f"{path}: matched no anyOf branch ({'; '.join(branch_errors)})")
            elif combine == "one" and len(passing) != 1:
                if not passing:
                    errors.append(f"{path}: matched no oneOf branch ({'; '.join(branch_errors)})")
                else:
                    errors.append(f"{path}: matched {len(passing)} oneOf branches, expected exactly 1")

        if "not" in schema:
            sub: List[str] = []
            self._apply(schema["not"], value, path, sub)
            if not sub:
                errors.append(f"{path}: value must NOT match {json.dumps(schema['not'])}")

    def _apply_string(self, schema: Dict[str, Any], value: str, path: str, errors: List[str]) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")

    def _apply_number(self, schema: Dict[str, Any], value: float, path: str, errors: List[str]) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")

    def _apply_array(self, schema: Dict[str, Any], value: List[Any], path: str, errors: List[str]) -> None:
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: has {len(value)} items, minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: has {len(value)} items, maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") and len(
            {json.dumps(v, sort_keys=True) for v in value}
        ) != len(value):
            errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(value):
                self._apply(schema["items"], item, f"{path}[{i}]", errors)

    def _apply_object(self, schema: Dict[str, Any], value: Dict[str, Any], path: str, errors: List[str]) -> None:
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")

        props = schema.get("properties", {})
        pattern_props = schema.get("patternProperties", {})
        for key, sub in value.items():
            handled = False
            if key in props:
                self._apply(props[key], sub, f"{path}.{key}", errors)
                handled = True
            for pattern, pattern_schema in pattern_props.items():
                if re.search(pattern, key):
                    self._apply(pattern_schema, sub, f"{path}.{key}", errors)
                    handled = True
            if "propertyNames" in schema:
                self._apply(schema["propertyNames"], key, f"{path}.<key {key!r}>", errors)
            if not handled and "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    errors.append(f"{path}: unexpected property {key!r}")
                else:
                    self._apply(extra, sub, f"{path}.{key}", errors)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if _is_int(value):
        return "integer"
    return "number"


# ======================================================================================
# GLB / glTF container inspection
# ======================================================================================

def read_glb(path: Path) -> Tuple[Dict[str, Any], int]:
    """Return (glTF JSON, BIN chunk length). Raises ValueError on a malformed container."""
    data = read_bundle_bytes(path)
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
    gltf: Optional[Dict[str, Any]] = None
    bin_len = 0
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if offset + chunk_len > len(data):
            raise ValueError(f"chunk {chunk_type!r} overruns the file")
        payload = data[offset:offset + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            bin_len = chunk_len
        offset += chunk_len + (-chunk_len % 4)

    if gltf is None:
        raise ValueError("no JSON chunk")
    if gltf.get("asset", {}).get("version") != "2.0":
        raise ValueError(f"asset.version is {gltf.get('asset', {}).get('version')!r}, expected '2.0'")
    return gltf, bin_len


# ======================================================================================
# Bundle validation
# ======================================================================================

class Report:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.notes: List[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def _load_jsonl(path: Path, validator: Validator, label: str, report: Report) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open_bundle_text(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                report.error(f"{label}:{lineno}: blank line (JSON Lines allows no blank lines)")
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                report.error(f"{label}:{lineno}: not valid JSON — {exc}")
                continue
            if not isinstance(record, dict):
                report.error(f"{label}:{lineno}: expected a JSON object, got {_kind(record)}")
                continue
            for err in validator.validate(record):
                report.error(f"{label}:{lineno}: {err}")
            records.append(record)
    return records


def _load_declared_missing(bundle_dir: Path) -> Dict[str, str]:
    """Read warnings.jsonl and return {assetId: reason} for assets the producer could not deliver.

    Advisory and not schema-validated (§10), so this is deliberately forgiving: an unreadable or
    malformed file simply declares nothing, which leaves every gap an error rather than silently
    excusing it.
    """
    path = bundle_file(bundle_dir / "warnings.jsonl")
    declared: Dict[str, str] = {}
    if path is None:
        return declared

    try:
        with open_bundle_text(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                asset_id = record.get("assetId")
                if isinstance(asset_id, str):
                    declared[asset_id] = str(record.get("reason", "no reason given"))
    except OSError:
        pass

    return declared


def _resolve_layer(bundle_dir: Path, layers: Dict[str, str], key: str) -> Optional[Path]:
    rel = layers.get(key)
    return None if rel is None else bundle_dir / rel


def validate_bundle(bundle_dir: Path, schema_dir: Path) -> Report:
    report = Report()

    schemas = {}
    for name in ("bundle", "instance", "network", "materials"):
        path = schema_dir / f"{name}.schema.json"
        schemas[name] = Validator(json.loads(path.read_text(encoding="utf-8")), path.name)

    manifest_path = bundle_dir / "bundle.json"
    if not manifest_path.is_file():
        report.error("bundle.json is missing at the bundle root")
        return report

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for err in schemas["bundle"].validate(manifest):
        report.error(f"bundle.json: {err}")
    if report.errors:
        return report

    layers: Dict[str, str] = manifest["layers"]

    # -- every declared layer must exist, as the right kind of thing -------------------
    # A compressed layer is named for it, so `layers` names the file that is really there —
    # `instances.jsonl.gz`, not `instances.jsonl` with the compression left implicit. The
    # fallback below still accepts a manifest that names the logical path, because that is what
    # a bundle produced before §12.1's amendment does.
    for key, rel in layers.items():
        target = bundle_dir / rel
        if rel.endswith("/"):
            if not target.is_dir():
                report.error(f"layers.{key}: directory {rel!r} does not exist")
        elif bundle_file(target) is None:
            report.error(f"layers.{key}: file {rel!r} does not exist")

    mesh_dir = _resolve_layer(bundle_dir, layers, "meshes")
    net_mesh_dir = _resolve_layer(bundle_dir, layers, "networkMeshes")
    tex_dir = _resolve_layer(bundle_dir, layers, "textures")

    # -- materials.json ----------------------------------------------------------------
    materials: Dict[str, Any] = {}
    referenced_textures: set = set()
    materials_path = _resolve_layer(bundle_dir, layers, "materials")
    if materials_path and materials_path.is_file():
        materials = json.loads(materials_path.read_text(encoding="utf-8"))
        for err in schemas["materials"].validate(materials):
            report.error(f"materials.json: {err}")
        for mat_id, material in materials.items():
            if not isinstance(material, dict):
                continue
            for slot in ("baseColor", "normal", "mask", "emissive", "colorMask"):
                rel = material.get(slot)
                if rel is None:
                    continue
                tex_path = bundle_dir / rel
                if not tex_path.is_file():
                    report.error(f"materials.json[{mat_id}].{slot}: {rel!r} does not exist")
                    continue
                referenced_textures.add(tex_path.resolve())
                _check_texture(tex_path, rel, f"materials.json[{mat_id}].{slot}", report)

    # -- instances.jsonl ---------------------------------------------------------------
    instances: List[Dict[str, Any]] = []
    instances_path = _resolve_layer(bundle_dir, layers, "instances")
    instances_path = bundle_file(instances_path) if instances_path else None
    if instances_path:
        instances = _load_jsonl(instances_path, schemas["instance"], layers["instances"], report)

    # A producer may declare, in warnings.jsonl, an asset it could not deliver (§10). That is the
    # honest alternative to dropping the instances, which would lose their placement for nothing,
    # so a declared gap is a warning rather than an error. An *undeclared* one stays an error.
    declared_missing = _load_declared_missing(bundle_dir)

    seen_ids: Dict[int, str] = {}
    missing_meshes: Dict[str, int] = {}
    for i, record in enumerate(instances):
        rid = record.get("id")
        if rid is not None:
            if rid in seen_ids:
                report.error(f"instances.jsonl: duplicate id {rid}")
            seen_ids[rid] = "instance"
        _check_quat(record.get("rot"), f"instances.jsonl line {i + 1}", report)
        asset_id = record["assetId"]
        if mesh_dir is None:
            report.error(f"instances.jsonl line {i + 1}: assetId {asset_id!r} but no 'meshes' layer is declared")
        elif bundle_file(mesh_dir / f"{asset_id}.glb") is None:
            # Aggregated by assetId: one broken prefab can be referenced by tens of thousands of
            # instances, and a line-per-instance report buries every other finding.
            missing_meshes[asset_id] = missing_meshes.get(asset_id, 0) + 1

    for asset_id, count in sorted(missing_meshes.items(), key=lambda kv: -kv[1]):
        where = f"{layers['meshes']}{asset_id}.glb"
        plural = "instance" if count == 1 else "instances"
        if asset_id in declared_missing:
            report.warn(
                f"assetId {asset_id!r} ({count} {plural}) has no mesh at {where}; "
                f"declared in warnings.jsonl: {declared_missing[asset_id]}"
            )
        else:
            report.error(f"assetId {asset_id!r} ({count} {plural}) has no mesh at {where}")

    for i, record in enumerate(instances):
        parent = record.get("parent")
        if parent is not None and parent not in seen_ids:
            report.error(f"instances.jsonl line {i + 1}: parent {parent} matches no instance id")

    # -- networks.jsonl ----------------------------------------------------------------
    networks: List[Dict[str, Any]] = []
    networks_path = _resolve_layer(bundle_dir, layers, "networks")
    networks_path = bundle_file(networks_path) if networks_path else None
    if networks_path:
        networks = _load_jsonl(networks_path, schemas["network"], layers["networks"], report)

    net_ids = {r["id"] for r in networks if "id" in r}
    for i, record in enumerate(networks):
        label = f"networks.jsonl line {i + 1}"
        if "rot" in record:
            _check_quat(record["rot"], label, report)
        asset_id = record.get("assetId")
        if asset_id is not None:
            if net_mesh_dir is None:
                report.error(f"{label}: assetId {asset_id!r} but no 'networkMeshes' layer is declared")
            elif bundle_file(net_mesh_dir / f"{asset_id}.glb") is None:
                report.error(f"{label}: assetId {asset_id!r} has no mesh in {layers['networkMeshes']}")
        for key in ("startNode", "endNode"):
            if key in record and record[key] not in net_ids:
                report.error(f"{label}: {key} {record[key]} matches no network record id")

    # -- lanemeshes.jsonl + laneprefabs.json (contract §5B v2) -------------------------
    # Not part of the manifest's layer map yet (that lands with the Phase 7 manifest), so the
    # conventional paths are checked when present: parameter records against their schema, the
    # prefab join against the catalogue, and every part's assetId against lanemeshes/.
    lanes_path = bundle_file(bundle_dir / "lanemeshes.jsonl")
    catalogue_path = bundle_dir / "laneprefabs.json"
    lane_mesh_dir = bundle_dir / "lanemeshes"
    if lanes_path is not None:
        lane_schema_path = schema_dir / "lanemesh.schema.json"
        lane_schema = Validator(json.loads(lane_schema_path.read_text(encoding="utf-8")),
                                lane_schema_path.name)
        lanes = _load_jsonl(lanes_path, lane_schema, "lanemeshes.jsonl", report)

        catalogue: Dict[str, Any] = {}
        if catalogue_path.is_file():
            catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
        elif lanes:
            report.error("lanemeshes.jsonl is present but laneprefabs.json is missing")

        checked_parts: set = set()
        for entry_key, entry in catalogue.items():
            for j, part in enumerate(entry.get("parts", [])):
                asset_id = part.get("assetId")
                if not asset_id:
                    report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: no assetId")
                elif asset_id not in checked_parts:
                    checked_parts.add(asset_id)
                    if bundle_file(lane_mesh_dir / f"{asset_id}.glb") is None:
                        report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: "
                                     f"assetId {asset_id!r} has no mesh in lanemeshes/")

        for i, record in enumerate(lanes):
            label = f"lanemeshes.jsonl line {i + 1}"
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                report.error(f"{label}: prefab {record.get('prefab')!r} is not in laneprefabs.json")
                continue
            part_count = len(entry.get("parts", []))
            for index in record.get("parts", []):
                if not 0 <= index < part_count:
                    report.error(f"{label}: part index {index} out of range "
                                 f"(catalogue entry has {part_count})")

    # -- meshes ------------------------------------------------------------------------
    mesh_files: List[Path] = glob_layer(mesh_dir, ".glb") if mesh_dir and mesh_dir.is_dir() else []
    net_mesh_files: List[Path] = glob_layer(net_mesh_dir, ".glb") if net_mesh_dir and net_mesh_dir.is_dir() else []
    lane_mesh_files: List[Path] = glob_layer(lane_mesh_dir, ".glb") if lane_mesh_dir.is_dir() else []
    for path in mesh_files + net_mesh_files + lane_mesh_files:
        _check_mesh(path, bundle_dir, materials, materials_path is not None, referenced_textures, report)

    used_assets = {r["assetId"] for r in instances}
    for path in mesh_files:
        if layer_stem(path) not in used_assets:
            report.warn(f"{path.name}: mesh is not referenced by any instance")

    # -- textures ----------------------------------------------------------------------
    # Not glob_layer: a texture is a PNG, which is already deflate-compressed, and §12.1
    # deliberately leaves the raster layers alone.
    texture_files: List[Path] = sorted(tex_dir.glob("*.png")) if tex_dir and tex_dir.is_dir() else []
    for path in texture_files:
        if path.resolve() not in referenced_textures:
            report.warn(f"{path.name}: texture is not referenced by any material or mesh")

    # -- counts ------------------------------------------------------------------------
    counts = manifest["counts"]
    for key, actual in (
        ("meshes", len(mesh_files)),
        ("materials", len(materials)),
        ("textures", len(texture_files)),
        ("instances", len(instances)),
        ("networkSegments", len(networks)),
    ):
        if counts[key] != actual:
            report.error(f"bundle.json counts.{key} is {counts[key]}, but the bundle contains {actual}")

    # -- bounds ------------------------------------------------------------------------
    lo = manifest["bounds"]["min"]
    hi = manifest["bounds"]["max"]
    for axis in range(3):
        if lo[axis] > hi[axis]:
            report.error(f"bundle.json bounds: min[{axis}] > max[{axis}]")
    points: List[Tuple[str, List[float]]] = [
        (f"instances.jsonl line {i + 1}", r["pos"]) for i, r in enumerate(instances)
    ]
    for i, record in enumerate(networks):
        points.append((f"networks.jsonl line {i + 1}", record["pos"])) if "pos" in record else None
        for j, point in enumerate(record.get("centerline", [])):
            points.append((f"networks.jsonl line {i + 1} centerline[{j}]", point))
    for label, point in points:
        for axis in range(3):
            if point[axis] < lo[axis] - BOUNDS_TOLERANCE or point[axis] > hi[axis] + BOUNDS_TOLERANCE:
                report.error(f"{label}: axis {axis} value {point[axis]} lies outside bundle.json bounds")

    # -- raster layers need raster geo-referencing -------------------------------------
    raster_layers = [k for k in ("terrain", "worldTerrain", "surfaceIndex", "water", "worldWater") if k in layers]
    if raster_layers and "rasters" not in manifest:
        report.error(f"bundle.json declares raster layers {raster_layers} but has no 'rasters' block")
    elif raster_layers:
        _check_raster_layers(bundle_dir, manifest, layers, report)

    # -- surface areas (§8b, optional) -------------------------------------------------
    _check_areas(bundle_dir, report)
    _check_clip_volumes(bundle_dir, report)

    report.note(f"schema {manifest['schema']}, generator {manifest['generator']!r}")
    report.note(f"{len(instances)} instances, {len(networks)} network records")
    report.note(f"{len(mesh_files)} meshes, {len(materials)} materials, {len(texture_files)} textures")
    return report


def _png_header(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """Return (width, height, bit depth, colour type) from a PNG's IHDR, or None."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 29 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[24], data[25]


def _check_raster_layers(bundle_dir: Path, manifest: Dict[str, Any],
                         layers: Dict[str, str], report: Report) -> None:
    """The terrain-layer invariants of contract §8.

    Every raster is a single-channel PNG on the one grid the manifest's `rasters` block
    geo-references: elevation and the water surface level at bit depth 16 (the game's
    native sample width, value * scale + offset = metres; the water level shares the
    elevation encoding, 0 = dry) and the surface index at bit depth 8 (ids, not colours).
    The world rasters (`worldTerrain`/`worldWater`) cover a larger area at their own
    size, so only the playable-area rasters are held to `rasters.size`.
    """
    rasters = manifest.get("rasters", {})
    size = rasters.get("size")

    expectations = (
        ("terrain", 16, True, ("elevationScale", "elevationOffset")),
        ("surfaceIndex", 8, True, ()),
        ("water", 16, True, ("elevationScale", "elevationOffset")),
        ("worldTerrain", 16, False, ("elevationScale", "elevationOffset")),
        ("worldWater", 16, False, ("elevationScale", "elevationOffset")),
    )
    for key, depth, playable_grid, needed_keys in expectations:
        rel = layers.get(key)
        if rel is None:
            continue
        path = bundle_dir / rel
        if not path.is_file():
            continue  # the existence pass has already reported it
        header = _png_header(path)
        if header is None:
            report.error(f"layers.{key}: {rel!r} is not a PNG")
            continue
        width, height, bit_depth, colour_type = header
        if colour_type != 0:
            report.error(f"layers.{key}: {rel!r} colour type is {colour_type}, contract §8 requires 0 (grayscale)")
        if bit_depth != depth:
            report.error(f"layers.{key}: {rel!r} bit depth is {bit_depth}, contract §8 requires {depth}")
        if playable_grid and isinstance(size, list) and len(size) == 2 and [width, height] != size:
            report.error(f"layers.{key}: {rel!r} is {width}x{height}, but rasters.size is {size[0]}x{size[1]}")
        for needed in needed_keys:
            if needed not in rasters:
                report.error(f"layers.{key} is declared but rasters.{needed} is missing")

    surfaces_dir = layers.get("surfaces")
    if surfaces_dir is not None:
        index_path = bundle_dir / surfaces_dir / "index.json"
        if not index_path.is_file():
            report.error(f"layers.surfaces: {surfaces_dir!r} has no index.json")
        else:
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                report.error(f"{surfaces_dir}index.json: not valid JSON ({exc})")
                index = {}
            for surface_id, entry in index.items():
                label = f"{surfaces_dir}index.json[{surface_id}]"
                if not surface_id.isdigit() or not 0 <= int(surface_id) <= 255:
                    report.error(f"{label}: id must be an integer 0..255")
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    report.error(f"{label}: entry must carry a string 'name'")
                    continue
                texture = entry.get("texture")
                if texture is not None and not (bundle_dir / texture).is_file():
                    report.error(f"{label}: texture {texture!r} does not exist")


def _check_areas(bundle_dir: Path, report: Report) -> None:
    """The surface-area invariants of contract §8b, when the optional layer is present.

    Every record's triangles index into its own nodes; every `prefab` key resolves in
    `areaprefabs.json`; a catalogue `texture`, when named, exists on disk.
    """
    areas_path = bundle_file(bundle_dir / "areas.jsonl")
    if areas_path is None:
        return

    catalogue: Dict[str, Any] = {}
    catalogue_path = bundle_dir / "areaprefabs.json"
    if catalogue_path.is_file():
        try:
            catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            report.error(f"areaprefabs.json: not valid JSON ({exc})")

    for key, entry in catalogue.items():
        texture = entry.get("texture") if isinstance(entry, dict) else None
        if texture is not None and not (bundle_dir / texture).is_file():
            report.error(f"areaprefabs.json[{key}]: texture {texture!r} does not exist")

    records = 0
    with open_bundle_text(areas_path) as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            label = f"areas.jsonl line {line_no}"
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"{label}: not valid JSON ({exc})")
                continue
            records += 1
            nodes = record.get("nodes")
            triangles = record.get("triangles")
            if not isinstance(nodes, list) or len(nodes) < 3:
                report.error(f"{label}: 'nodes' must be a list of at least 3 positions")
                continue
            if not isinstance(triangles, list) or not triangles:
                report.error(f"{label}: 'triangles' must be a non-empty list")
                continue
            for tri in triangles:
                if (not isinstance(tri, list) or len(tri) != 3
                        or any(not isinstance(i, int) or i < 0 or i >= len(nodes) for i in tri)):
                    report.error(f"{label}: triangle {tri!r} does not index its {len(nodes)} nodes")
                    break
            prefab = record.get("prefab")
            if not isinstance(prefab, str) or not prefab:
                report.error(f"{label}: 'prefab' must be a non-empty string")
            elif prefab not in catalogue:
                report.error(f"{label}: prefab {prefab!r} is not in areaprefabs.json")

    report.note(f"{records} surface areas over {len(catalogue)} area prefabs (§8b)")


def _check_clip_volumes(bundle_dir: Path, report: Report) -> None:
    """The clip-volume invariants of contract §8c, when the optional layer is present.

    This layer replaced `terrain/cutout.png` on 2026-08-08 — the precomputed intersection the
    producer used to ship. What is checkable here is the record shape, that a band is ordered,
    and that an `area` record joins to a real area; whether a volume opens the right terrain is
    a question about the rip, not about the file.
    """
    path = bundle_file(bundle_dir / "clipvolumes.jsonl")
    if path is None:
        return

    area_ids: set = set()
    areas_path = bundle_file(bundle_dir / "areas.jsonl")
    if areas_path is not None:
        with open_bundle_text(areas_path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    area_ids.add(json.loads(line).get("id"))
                except ValueError:
                    continue

    kinds: Dict[str, int] = {}
    records = 0
    with open_bundle_text(path) as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            label = f"clipvolumes.jsonl line {line_no}"
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"{label}: not valid JSON ({exc})")
                continue
            records += 1

            kind = record.get("kind")
            if kind not in ("edge", "node", "orphan", "area"):
                report.error(f"{label}: 'kind' must be edge, node, orphan or area, got {kind!r}")
                continue
            kinds[kind] = kinds.get(kind, 0) + 1

            bottom = record.get("bottom")
            if not isinstance(bottom, (int, float)) or isinstance(bottom, bool):
                report.error(f"{label}: 'bottom' must be a number")
                continue

            # An absent top means unbounded (§8c), which is not the same as a missing field:
            # it is how an authored clip surface says "however high the cover is".
            top = record.get("top")
            if top is not None:
                if not isinstance(top, (int, float)) or isinstance(top, bool):
                    report.error(f"{label}: 'top' must be a number when present")
                elif top <= bottom:
                    report.error(f"{label}: band is empty or inverted (bottom {bottom}, top {top})")

            if kind == "area":
                area = record.get("area")
                if area is None:
                    report.error(f"{label}: an 'area' record must name the area it clips")
                elif area_ids and area not in area_ids:
                    report.error(f"{label}: area {area} matches no record in areas.jsonl")
                if "left" in record or "right" in record:
                    report.error(f"{label}: an 'area' record carries no curves — "
                                 "its geometry is areas.jsonl's triangles (§8c)")
                continue

            for side in ("left", "right"):
                curve = record.get(side)
                if (not isinstance(curve, list) or len(curve) != 4
                        or any(not isinstance(p, list) or len(p) != 3
                               or any(not isinstance(c, (int, float)) or isinstance(c, bool) for c in p)
                               for p in curve)):
                    report.error(f"{label}: {side!r} must be 4 control points of 3 numbers each")
                    break

    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    report.note(f"{records} clip volume strips ({summary}) (§8c)")


def _check_quat(rot: Any, label: str, report: Report) -> None:
    if not isinstance(rot, list) or len(rot) != 4:
        return  # already reported structurally
    norm = math.sqrt(sum(component * component for component in rot))
    if abs(norm - 1.0) > QUAT_TOLERANCE:
        report.error(f"{label}: rot is not a unit quaternion (|q| = {norm:.6f})")


def _check_texture(path: Path, rel: str, label: str, report: Report) -> None:
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


def _check_mesh(
    path: Path,
    bundle_dir: Path,
    materials: Dict[str, Any],
    have_materials: bool,
    referenced_textures: set,
    report: Report,
) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", layer_stem(path)):
        report.error(f"{path.name}: filename stem is not a valid assetId")
    try:
        gltf, _ = read_glb(path)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.error(f"{path.name}: not a valid GLB — {exc}")
        return

    for i, material in enumerate(gltf.get("materials", [])):
        name = material.get("name")
        if not name:
            report.error(f"{path.name}: materials[{i}] has no name, so it cannot join into materials.json")
        elif have_materials and name not in materials:
            report.error(f"{path.name}: materials[{i}].name {name!r} is not a key in materials.json")

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
        _check_texture(target, uri, f"{path.name} images[{i}]", report)


# ======================================================================================

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate a CS2 World Bundle (schema 1).")
    parser.add_argument("bundle", type=Path, help="path to the bundle directory (the one holding bundle.json)")
    parser.add_argument("--schema-dir", type=Path, default=DEFAULT_SCHEMA_DIR, help="directory holding *.schema.json")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only errors and the verdict")
    args = parser.parse_args(argv)

    if not args.bundle.is_dir():
        print(f"error: {args.bundle} is not a directory", file=sys.stderr)
        return 2
    if not args.schema_dir.is_dir():
        print(f"error: schema directory {args.schema_dir} not found", file=sys.stderr)
        return 2

    try:
        report = validate_bundle(args.bundle, args.schema_dir)
    except SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read the bundle — {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        for note in report.notes:
            print(f"  {note}")
    for warning in report.warnings:
        print(f"WARN  {warning}")
    for error in report.errors:
        print(f"ERROR {error}")

    if report.errors:
        print(f"\nINVALID — {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
        return 1
    print(f"\nVALID — {len(report.warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
