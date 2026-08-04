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
    with path.open("r", encoding="utf-8") as fh:
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
    path = bundle_dir / "warnings.jsonl"
    declared: Dict[str, str] = {}
    if not path.is_file():
        return declared

    try:
        with path.open(encoding="utf-8") as handle:
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
    for key, rel in layers.items():
        target = bundle_dir / rel
        if rel.endswith("/"):
            if not target.is_dir():
                report.error(f"layers.{key}: directory {rel!r} does not exist")
        elif not target.is_file():
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
            for slot in ("baseColor", "normal", "mask", "emissive"):
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
    if instances_path and instances_path.is_file():
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
        elif not (mesh_dir / f"{asset_id}.glb").is_file():
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
    if networks_path and networks_path.is_file():
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
            elif not (net_mesh_dir / f"{asset_id}.glb").is_file():
                report.error(f"{label}: assetId {asset_id!r} has no mesh in {layers['networkMeshes']}")
        for key in ("startNode", "endNode"):
            if key in record and record[key] not in net_ids:
                report.error(f"{label}: {key} {record[key]} matches no network record id")

    # -- meshes ------------------------------------------------------------------------
    mesh_files: List[Path] = sorted(mesh_dir.glob("*.glb")) if mesh_dir and mesh_dir.is_dir() else []
    net_mesh_files: List[Path] = sorted(net_mesh_dir.glob("*.glb")) if net_mesh_dir and net_mesh_dir.is_dir() else []
    for path in mesh_files + net_mesh_files:
        _check_mesh(path, bundle_dir, materials, materials_path is not None, referenced_textures, report)

    used_assets = {r["assetId"] for r in instances}
    for path in mesh_files:
        if path.stem not in used_assets:
            report.warn(f"{path.name}: mesh is not referenced by any instance")

    # -- textures ----------------------------------------------------------------------
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

    report.note(f"schema {manifest['schema']}, generator {manifest['generator']!r}")
    report.note(f"{len(instances)} instances, {len(networks)} network records")
    report.note(f"{len(mesh_files)} meshes, {len(materials)} materials, {len(texture_files)} textures")
    return report


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
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", path.stem):
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
