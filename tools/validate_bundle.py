#!/usr/bin/env python3
"""Standalone validator for a CS2 World Bundle (schema 2, frozen 2026-08-09).

Standalone means exactly that: Python 3.8+ and the standard library, no pip installs, no
game, no producer source. It is the reference check for the Phase-0 contract, and the thing a
consumer author runs to confirm a bundle they were handed is well-formed.

It does two jobs:

  1. Structural — validates every declared layer against docs/schema/*.schema.json (fifteen
     documents as of 2026-08-14, one per layer), using a small built-in evaluator for the JSON
     Schema subset those files use.
  2. Semantic — the cross-file invariants a schema cannot state: every assetId resolves to a
     mesh file, every texture reference resolves and is named by its own SHA-256, every .glb
     is a well-formed glTF 2.0 container whose material names join into materials.json,
     an area's heightRanges runs parallel to its triangles, an area's owner joins to a
     building, manifest counts match reality, and bounds actually enclose the content.

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

# One schema document per layer of the contract. `lanemesh` is §5B's parameter record and
# `laneprefabs` its catalogue; `lane` is §5A's graph, a different file. Every name here must
# exist in the schema directory — a missing one is a setup error, not a bundle error.
SCHEMA_NAMES = (
    "bundle", "instance", "network", "materials",
    "lane", "lanemesh", "laneprefabs",
    "colors", "lights",
    "area", "areaprefabs", "clipvolume",
    "warning", "surfaceindex", "grading",
)


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


def _load_json_layer(path: Path, validator: Validator, label: str,
                     report: Report) -> Optional[Dict[str, Any]]:
    """Parse a whole-file JSON layer and validate it, or return None.

    Every whole-file layer in the contract is a JSON **object**, so anything else is reported
    and then withheld from the caller. That second half matters: a truncation leaving a valid
    JSON array parses fine, fails the schema, and used to be handed back anyway — whereupon the
    caller's `.items()` raised an `AttributeError` that `main()` does not catch, so the user got
    a traceback and no verdict in place of the INVALID the schema had already established.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.error(f"{label}: not valid JSON — {exc}")
        return None
    for err in validator.validate(document):
        report.error(f"{label}: {err}")
    if not isinstance(document, dict):
        report.error(f"{label}: expected a JSON object at the top level, got {_kind(document)}")
        return None
    return document


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


def _count_records(path: Optional[Path]) -> int:
    """Record count of a JSON Lines layer — non-blank lines (blank lines are reported elsewhere)."""
    if path is None or not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def validate_bundle(bundle_dir: Path, schema_dir: Path) -> Report:
    report = Report()

    schemas = {}
    for name in SCHEMA_NAMES:
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
    if instances_path and instances_path.is_file():
        instances = _load_jsonl(instances_path, schemas["instance"], layers["instances"], report)

    # A producer may declare, in warnings.jsonl, an asset it could not deliver (§10). That is the
    # honest alternative to dropping the instances, which would lose their placement for nothing,
    # so a declared gap is a warning rather than an error. An *undeclared* one stays an error.
    declared_missing = _load_declared_missing(bundle_dir)

    # A record id is unique per logical object; the one sanctioned exception is §4.1's stack
    # expansion, where every section record of one stacked instance shares that instance's id.
    stack_section = re.compile(r"__stack_(first|mid|last|rest)$")
    seen_ids: Dict[int, bool] = {}  # id -> every record seen so far is a stack section
    missing_meshes: Dict[str, int] = {}
    for i, record in enumerate(instances):
        rid = record.get("id")
        if rid is not None:
            is_section = bool(stack_section.search(record.get("assetId", "")))
            if rid in seen_ids and not (seen_ids[rid] and is_section):
                report.error(f"instances.jsonl: duplicate id {rid} "
                             "(only §4.1 stack-section records may share an id)")
            seen_ids[rid] = seen_ids.get(rid, True) and is_section
        _check_quat(record.get("rot"), f"instances.jsonl line {i + 1}", report)
        asset_id = record["assetId"]
        if mesh_dir is None:
            report.error(f"instances.jsonl line {i + 1}: assetId {asset_id!r} but no 'meshes' layer is declared")
        elif not (mesh_dir / f"{asset_id}.glb").is_file():
            # Aggregated by assetId: one broken prefab can be referenced by tens of thousands of
            # instances, and a line-per-instance report buries every other finding.
            missing_meshes[asset_id] = missing_meshes.get(asset_id, 0) + 1

    # A gap the producer DECLARED is not a validator warning (decided 2026-08-12, PILLARS-2).
    # warnings.jsonl is the contract's own channel for "this asset could not be delivered" (§10),
    # and re-reporting each one made the warning count a function of how honest the producer was:
    # 120 of the full city's 120 warnings were assets the bundle had already declared, which
    # buried the classes that mean something. They are counted in one note instead, and only an
    # UNDECLARED missing mesh is still an error. Nothing is hidden - `warnings.jsonl` is the
    # authority and the note points at it.
    declared_gaps = 0
    declared_instances = 0
    for asset_id, count in sorted(missing_meshes.items(), key=lambda kv: -kv[1]):
        where = f"{layers['meshes']}{asset_id}.glb"
        plural = "instance" if count == 1 else "instances"
        if asset_id in declared_missing:
            declared_gaps += 1
            declared_instances += count
        else:
            report.error(f"assetId {asset_id!r} ({count} {plural}) has no mesh at {where}")

    if declared_gaps:
        reasons: Dict[str, int] = {}
        for asset_id in missing_meshes:
            if asset_id in declared_missing:
                reason = declared_missing[asset_id]
                reasons[reason] = reasons.get(reason, 0) + 1
        breakdown = "; ".join(f"{n}x {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
        report.note(
            f"{declared_gaps} assetId(s) referenced by {declared_instances} instance(s) have no "
            f"mesh, every one of them declared in warnings.jsonl (§10): {breakdown}"
        )

    # -- networks.jsonl ----------------------------------------------------------------
    networks: List[Dict[str, Any]] = []
    networks_path = _resolve_layer(bundle_dir, layers, "networks")
    if networks_path and networks_path.is_file():
        networks = _load_jsonl(networks_path, schemas["network"], layers["networks"], report)

    net_ids = {r["id"] for r in networks if "id" in r}

    # `parent` resolves into instances.jsonl or networks.jsonl when the owner is exported
    # (street furniture is owned by road edges). Owners the bundle does not carry — utility-net
    # edges, net nodes — leave a parent that resolves to no record; §4 makes parent advisory
    # grouping, never a required join, so that is a warning, aggregated because one city has
    # tens of thousands of such owners.
    unresolved_parents: Dict[int, int] = {}
    for record in instances:
        parent = record.get("parent")
        if parent is not None and parent not in seen_ids and parent not in net_ids:
            unresolved_parents[parent] = unresolved_parents.get(parent, 0) + 1
    if unresolved_parents:
        total = sum(unresolved_parents.values())
        samples = ", ".join(str(p) for p in list(unresolved_parents)[:5])
        report.warn(
            f"{total} instance record(s) carry a parent that resolves to no exported record "
            f"({len(unresolved_parents)} distinct owners, e.g. {samples}); §4: such owners "
            "(utility-net edges, net nodes) are not in the bundle and parent is advisory"
        )
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

    # -- lanemeshes.jsonl + laneprefabs.json (contract §5B v2) -------------------------
    # Declared by the Phase 7 manifest as layers.laneGeometry / lanePrefabs / laneMeshes:
    # parameter records against their schema, the prefab join against the catalogue, and every
    # part's assetId against the lane-mesh directory.
    lanes: List[Dict[str, Any]] = []
    lanes_path = _resolve_layer(bundle_dir, layers, "laneGeometry")
    catalogue_path = _resolve_layer(bundle_dir, layers, "lanePrefabs")
    lane_mesh_dir = _resolve_layer(bundle_dir, layers, "laneMeshes")
    if lanes_path and lanes_path.is_file():
        lanes = _load_jsonl(lanes_path, schemas["lanemesh"], layers["laneGeometry"], report)

        catalogue: Dict[str, Any] = {}
        if catalogue_path and catalogue_path.is_file():
            catalogue = _load_json_layer(catalogue_path, schemas["laneprefabs"],
                                         layers["lanePrefabs"], report) or {}
        elif lanes:
            report.error("layers.laneGeometry is declared but no lanePrefabs catalogue resolves")

        checked_parts: set = set()
        for entry_key, entry in catalogue.items():
            for j, part in enumerate(entry.get("parts", [])):
                asset_id = part.get("assetId")
                if not asset_id:
                    report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: no assetId")
                elif asset_id not in checked_parts:
                    checked_parts.add(asset_id)
                    if lane_mesh_dir is None:
                        report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: "
                                     f"assetId {asset_id!r} but no 'laneMeshes' layer is declared")
                    elif not (lane_mesh_dir / f"{asset_id}.glb").is_file():
                        report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: "
                                     f"assetId {asset_id!r} has no mesh in {layers['laneMeshes']}")

        for i, record in enumerate(lanes):
            label = f"{layers['laneGeometry']} line {i + 1}"
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                report.error(f"{label}: prefab {record.get('prefab')!r} is not in the lanePrefabs catalogue")
                continue
            part_count = len(entry.get("parts", []))
            for index in record.get("parts", []):
                if not 0 <= index < part_count:
                    report.error(f"{label}: part index {index} out of range "
                                 f"(catalogue entry has {part_count})")

    # -- meshes ------------------------------------------------------------------------
    mesh_files: List[Path] = sorted(mesh_dir.glob("*.glb")) if mesh_dir and mesh_dir.is_dir() else []
    net_mesh_files: List[Path] = sorted(net_mesh_dir.glob("*.glb")) if net_mesh_dir and net_mesh_dir.is_dir() else []
    lane_mesh_files: List[Path] = sorted(lane_mesh_dir.glob("*.glb")) if lane_mesh_dir and lane_mesh_dir.is_dir() else []
    # Sub-mesh names, for the §7.2 appearance join: a palette or a procedural light is keyed by
    # the glTF node name inside the owning prefab's .glb, and a punctual light by the prefab's
    # own assetId, so both are looked up in this set. Object meshes only — a colors.json key
    # matching a network cross-section piece would be a coincidence, not a join.
    mesh_names: set = set()
    for path in mesh_files:
        _check_mesh(path, bundle_dir, materials, materials_path is not None, referenced_textures,
                    report, mesh_names)
    for path in net_mesh_files + lane_mesh_files:
        _check_mesh(path, bundle_dir, materials, materials_path is not None, referenced_textures,
                    report)

    used_assets = {r["assetId"] for r in instances}

    # Prefabs the city places as §4.1 stack expansions. Every placement of one references a
    # section (X__stack_first/mid/last/rest) and NEVER the bare prefab, so the bare mesh, its
    # skirt, and any section this city's placements happened not to need are all unreferenced
    # by construction. `Vertical_Pillar` is the type case: 1,296 first + 6,706 mid records and
    # not one reference to `Vertical_Pillar` itself.
    stacked = {stack_section.sub("", a) for a in used_assets if stack_section.search(a)}

    def _references(stem: str) -> bool:
        return (stem in used_assets) or (stem in stacked)

    for path in mesh_files:
        if _references(path.stem):
            continue

        # An unplaced section of a placed stack: the producer writes every section the prefab
        # has, and which ones a given city needs depends on how tall its pillars are.
        if stack_section.sub("", path.stem) in stacked:
            continue

        # Derived files are referenced implicitly, never by an assetId of their own: a
        # foundation skirt (__base) is placed by the consumer's terrain skirting, and a state
        # variant (__teen, __elderly, __dead, __stump, __partial1/2, __full — contract §6.1) is
        # swapped in from the instance's own attrs.treeState / attrs.fullness. The first
        # full-city rip with skirts drowned the report in 1,380 of these; variants added 225 more.
        derived = ("__base", "__teen", "__elderly", "__dead", "__stump",
                   "__partial1", "__partial2", "__full")
        suffix = next((s for s in derived if path.stem.endswith(s)), None)
        if suffix and _references(path.stem[: -len(suffix)]):
            continue

        report.warn(f"{path.name}: mesh is not referenced by any instance")

    # -- textures ----------------------------------------------------------------------
    texture_files: List[Path] = sorted(tex_dir.glob("*.png")) if tex_dir and tex_dir.is_dir() else []
    for path in texture_files:
        if path.resolve() not in referenced_textures:
            report.warn(f"{path.name}: texture is not referenced by any material or mesh")

    # -- surface areas and clip volumes (§8b / §8c, optional) --------------------------
    area_count = _check_areas(bundle_dir, layers, schemas, seen_ids, report)
    clip_count = _check_clip_volumes(bundle_dir, layers, schemas, report)

    # -- the remaining documented layers (§5A, §7.2, §8, §10) --------------------------
    _check_lanes(bundle_dir, layers, schemas, net_ids, seen_ids, report)
    _check_appearance(bundle_dir, layers, schemas, mesh_names, report)
    _check_warnings(bundle_dir, layers, schemas, report)
    _check_grading(bundle_dir, layers, schemas, report)

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

    # The record layers added after the freeze carry a count whenever their layer is declared
    # (§3); a declared layer with no count is itself an error, so truncation detection cannot
    # silently lapse.
    for key, layer_key, actual in (
        ("lanes", "lanes", _count_records(_resolve_layer(bundle_dir, layers, "lanes"))),
        ("laneGeometry", "laneGeometry", len(lanes)),
        ("areas", "areas", area_count),
        ("clipVolumes", "clipVolumes", clip_count),
    ):
        if layer_key in layers:
            if key not in counts:
                report.error(f"bundle.json: layers.{layer_key} is declared but counts.{key} is missing")
            elif counts[key] != actual:
                report.error(f"bundle.json counts.{key} is {counts[key]}, but the bundle contains {actual}")
        elif key in counts and counts[key] != 0:
            report.error(f"bundle.json counts.{key} is {counts[key]}, but no layers.{layer_key} is declared")

    # counts.warnings has been optional since the freeze — checked only when stated.
    if "warnings" in counts:
        actual = _count_records(_resolve_layer(bundle_dir, layers, "warnings"))
        if counts["warnings"] != actual:
            report.error(f"bundle.json counts.warnings is {counts['warnings']}, but the bundle contains {actual}")

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
    raster_layers = [k for k in ("terrain", "terrainFine", "worldTerrain", "surfaceIndex", "splatmap",
                                 "userSplatmap", "water", "worldWater") if k in layers]
    if raster_layers and "rasters" not in manifest:
        report.error(f"bundle.json declares raster layers {raster_layers} but has no 'rasters' block")
    elif raster_layers:
        _check_raster_layers(bundle_dir, manifest, layers, schemas, report)

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


def _check_raster_layers(bundle_dir: Path, manifest: Dict[str, Any], layers: Dict[str, str],
                         schemas: Dict[str, Validator], report: Report) -> None:
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
        ("terrainFine", 16, False, ("elevationScale", "elevationOffset")),
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

    # The splat weight maps are RGBA8, not grayscale (§8).
    for key in ("splatmap", "userSplatmap"):
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
        if colour_type != 6 or bit_depth != 8:
            report.error(f"layers.{key}: {rel!r} is colour type {colour_type} at depth {bit_depth}, "
                         "contract §8 requires RGBA8 (colour type 6, depth 8)")

    # Rasters on their own grid carry their own geo-referencing sub-block (§3), whose `file`
    # and `size` must agree with the layer it describes.
    for key in ("splatmap", "userSplatmap", "worldTerrain", "worldWater", "terrainFine"):
        rel = layers.get(key)
        if rel is None:
            continue
        block = rasters.get(key)
        if not isinstance(block, dict):
            report.error(f"layers.{key} is declared but rasters.{key} is missing")
            continue
        if block.get("file") != rel:
            report.error(f"rasters.{key}.file is {block.get('file')!r}, but layers.{key} is {rel!r}")
        path = bundle_dir / rel
        header = _png_header(path) if path.is_file() else None
        block_size = block.get("size")
        if header is not None and isinstance(block_size, list) and len(block_size) == 2:
            width, height = header[0], header[1]
            if [width, height] != block_size:
                report.error(f"layers.{key}: {rel!r} is {width}x{height}, "
                             f"but rasters.{key}.size is {block_size[0]}x{block_size[1]}")

    surfaces_dir = layers.get("surfaces")
    if surfaces_dir is not None:
        index_path = bundle_dir / surfaces_dir / "index.json"
        if not index_path.is_file():
            report.error(f"layers.surfaces: {surfaces_dir!r} has no index.json")
        else:
            label = f"{surfaces_dir}index.json"
            index = _load_json_layer(index_path, schemas["surfaceindex"], label, report)
            for surface_id, entry in (index or {}).items():
                if not isinstance(entry, dict):
                    continue
                # `texture` may be null when the theme texture could not be read (§8), which is
                # a stated absence rather than a broken reference.
                texture = entry.get("texture")
                if texture is not None and not (bundle_dir / texture).is_file():
                    report.error(f"{label}[{surface_id}]: texture {texture!r} does not exist")


def _check_areas(bundle_dir: Path, layers: Dict[str, str], schemas: Dict[str, Validator],
                 instance_ids: Dict[int, bool], report: Report) -> int:
    """The surface-area invariants of contract §8b, when the optional layer is declared.

    The record shape is `area.schema.json`'s; what stays here is what a schema cannot say.
    Every record's triangles index into its own nodes and `heightRanges`, when present, runs
    PARALLEL to them — the contract's "in the same order as triangles" is a length agreement a
    consumer indexes by, so a mismatch silently mis-bands every drape. Every `prefab` resolves
    in the areaPrefabs catalogue, every catalogue map exists on disk, and `owner` joins to an
    instance (§8b calls that join total: 27,047 of 27,156 on the reference city, none dangling).
    Returns the record count for the counts check.
    """
    areas_path = _resolve_layer(bundle_dir, layers, "areas")
    if areas_path is None or not areas_path.is_file():
        return 0

    catalogue: Dict[str, Any] = {}
    catalogue_path = _resolve_layer(bundle_dir, layers, "areaPrefabs")
    if catalogue_path is None:
        report.error("layers.areas is declared but no areaPrefabs catalogue is declared")
    elif catalogue_path.is_file():
        catalogue = _load_json_layer(catalogue_path, schemas["areaprefabs"],
                                     layers["areaPrefabs"], report) or {}

    # The catalogue's PBR block is four separate maps, not one `texture` (audit #40 P11). A
    # dangling normalMap is exactly as broken as a dangling base map and used to go unchecked.
    unresolved = 0
    for key, entry in catalogue.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("unresolved"):
            unresolved += 1
        for slot in ("texture", "normalMap", "maskMap"):
            rel = entry.get(slot)
            if rel is not None and not (bundle_dir / rel).is_file():
                report.error(f"areaprefabs.json[{key}].{slot}: {rel!r} does not exist")

    if unresolved:
        report.note(
            f"{unresolved} of {len(catalogue)} area prefabs are `unresolved` — the save "
            "references a surface asset that was not loaded in the rip session, and the game "
            "itself drew those areas grey (§8b). Enable the asset pack and re-rip; do not "
            "invent an appearance consumer-side"
        )

    records = 0
    dangling_owners: Dict[int, int] = {}
    with areas_path.open(encoding="utf-8") as handle:
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
            for err in schemas["area"].validate(record):
                report.error(f"{label}: {err}")

            nodes = record.get("nodes")
            triangles = record.get("triangles")
            if not isinstance(nodes, list) or not isinstance(triangles, list):
                continue  # already reported structurally
            for tri in triangles:
                if (not isinstance(tri, list) or len(tri) != 3
                        or any(not _is_int(i) or i < 0 or i >= len(nodes) for i in tri)):
                    report.error(f"{label}: triangle {tri!r} does not index its {len(nodes)} nodes")
                    break

            ranges = record.get("heightRanges")
            if isinstance(ranges, list) and len(ranges) != len(triangles):
                report.error(f"{label}: heightRanges has {len(ranges)} entries but the area has "
                             f"{len(triangles)} triangles; §8b indexes them in parallel")

            prefab = record.get("prefab")
            if isinstance(prefab, str) and prefab and prefab not in catalogue:
                report.error(f"{label}: prefab {prefab!r} is not in the areaPrefabs catalogue")

            owner = record.get("owner")
            if owner is not None and owner not in instance_ids:
                dangling_owners[owner] = dangling_owners.get(owner, 0) + 1

    if dangling_owners:
        total = sum(dangling_owners.values())
        samples = ", ".join(str(o) for o in list(dangling_owners)[:5])
        report.warn(
            f"{total} area record(s) carry an owner that resolves to no instance "
            f"({len(dangling_owners)} distinct, e.g. {samples}); §8b states this join is total, "
            "so a dangling owner means the instance layer and the area layer came from "
            "different rips"
        )

    report.note(f"{records} surface areas over {len(catalogue)} area prefabs (§8b)")
    return records


def _check_clip_volumes(bundle_dir: Path, layers: Dict[str, str],
                        schemas: Dict[str, Validator], report: Report) -> int:
    """The clip-volume invariants of contract §8c, when the optional layer is declared.

    This layer replaced `terrain/cutout.png` on 2026-08-08 — the precomputed intersection the
    producer used to ship. `clipvolume.schema.json` carries the record shape and the two-form
    split (curves for a net strip, an inline polygon for an area prism); what stays here is
    that a band is ordered and that an area's triangles index its own nodes. Whether a volume
    opens the right terrain is a question about the rip, not about the file. Returns the record
    count for the counts check.
    """
    path = _resolve_layer(bundle_dir, layers, "clipVolumes")
    if path is None or not path.is_file():
        return 0

    kinds: Dict[str, int] = {}
    records = 0
    unbounded = 0
    with path.open("r", encoding="utf-8") as handle:
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
            for err in schemas["clipvolume"].validate(record):
                report.error(f"{label}: {err}")

            kind = record.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1

            bottom = record.get("bottom")
            if not _is_num(bottom):
                continue  # already reported structurally

            # An absent top means unbounded (§8c), which is not the same as a missing field:
            # it is how an authored clip surface says "however high the cover is".
            top = record.get("top")
            if top is None:
                unbounded += 1
            elif _is_num(top) and top <= bottom:
                report.error(f"{label}: band is empty or inverted (bottom {bottom}, top {top})")

            nodes = record.get("nodes")
            triangles = record.get("triangles")
            if isinstance(nodes, list) and isinstance(triangles, list):
                for tri in triangles:
                    if (not isinstance(tri, list) or len(tri) != 3
                            or any(not _is_int(i) or not 0 <= i < len(nodes) for i in tri)):
                        report.error(f"{label}: triangle {tri!r} does not index its "
                                     f"{len(nodes)} nodes (§8c)")
                        break

    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    report.note(f"{records} clip volume strips ({summary}) (§8c)")
    if unbounded:
        report.note(f"{unbounded} clip volume(s) have no `top` — unbounded, hiding whatever "
                    "covers them however high (§8c)")
    return records


def _check_lanes(bundle_dir: Path, layers: Dict[str, str], schemas: Dict[str, Validator],
                 net_ids: set, instance_ids: Dict[int, bool], report: Report) -> None:
    """The lane-graph invariants of contract §5A, when the optional layer is declared.

    `lane.schema.json` carries the record shape. What stays here is `owner`, and it is a NOTE
    rather than a check on purpose: §5A used to call it "the id of the networks.jsonl record
    this lane belongs to … absent for lanes owned by a node", and measurement falsifies both
    halves — it is the generic ECS Owner, so it can be an edge, a node (which gets no record at
    all, §5.1) or a building lot. On the reference city 21.7% resolve into networks.jsonl,
    19.1% into instances.jsonl and 59.2% into neither. Reporting the split is useful; requiring
    it to resolve would fail every correct bundle.
    """
    path = _resolve_layer(bundle_dir, layers, "lanes")
    if path is None or not path.is_file():
        return

    records = 0
    kinds: Dict[str, int] = {}
    to_net = to_instance = to_neither = no_owner = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                report.error(f"lanes.jsonl:{line_no}: blank line (JSON Lines allows no blank lines)")
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"lanes.jsonl:{line_no}: not valid JSON — {exc}")
                continue
            records += 1
            for err in schemas["lane"].validate(record):
                report.error(f"lanes.jsonl:{line_no}: {err}")

            kind = record.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1

            owner = record.get("owner")
            if owner is None:
                no_owner += 1
            elif owner in net_ids:
                to_net += 1
            elif owner in instance_ids:
                to_instance += 1
            else:
                to_neither += 1

    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    report.note(f"{records} lanes ({summary}) (§5A)")
    report.note(
        f"lane owners: {to_net} resolve into networks.jsonl, {to_instance} into "
        f"instances.jsonl, {to_neither} into neither (net nodes get no record, §5.1), "
        f"{no_owner} absent — `owner` is an advisory grouping key, not a foreign key"
    )


def _check_appearance(bundle_dir: Path, layers: Dict[str, str], schemas: Dict[str, Validator],
                      mesh_names: set, report: Report) -> None:
    """`colors.json` and `lights.json` — contract §7.2, when either layer is declared.

    Both are keyed by an assetId that must be findable in the mesh layer: a sub-mesh's glTF
    node name for a palette or a procedural light, the object prefab's own assetId for a
    punctual one. An unresolvable key is not an error — it is dead weight the producer wrote
    for geometry the bundle deliberately does not carry — but it is worth counting, because it
    is the only signal that a key a consumer will look up can never match. Measured on the
    reference city: 184 of 3,620 palette keys (tree `*Child*` sub-meshes, a state §6.1 does not
    export) and 86 of 1,186 light keys (vehicles, which both rip queries exclude).
    """
    for layer_key, schema_key, what in (("colors", "colors", "colour palettes"),
                                        ("lights", "lights", "light entries")):
        path = _resolve_layer(bundle_dir, layers, layer_key)
        if path is None or not path.is_file():
            continue
        document = _load_json_layer(path, schemas[schema_key], layers[layer_key], report)
        if not isinstance(document, dict):
            continue

        entries = sum(len(v) for v in document.values() if isinstance(v, list))
        report.note(f"{entries} {what} over {len(document)} keys ({layers[layer_key]}, §7.2)")

        if not mesh_names:
            continue
        unknown = [key for key in document if key not in mesh_names]
        if unknown:
            samples = ", ".join(sorted(unknown)[:4])
            report.note(
                f"{len(unknown)} of {len(document)} {layers[layer_key]} keys match no mesh file "
                f"or glTF node name (e.g. {samples}); §7.2 keys join to the mesh layer, so these "
                "describe geometry this bundle does not carry and no consumer lookup can reach"
            )


def _check_warnings(bundle_dir: Path, layers: Dict[str, str], schemas: Dict[str, Validator],
                    report: Report) -> None:
    """`warnings.jsonl` — contract §10, when the layer is declared.

    A consumer may ignore this file entirely; it is schema-validated anyway, because it is also
    how the producer DECLARES a gap, and `_load_declared_missing` reads exactly `assetId` and
    `reason` off it. A line the reader cannot parse silently stops excusing anything, which is
    the failure this check exists to make loud.
    """
    path = _resolve_layer(bundle_dir, layers, "warnings")
    if path is None or not path.is_file():
        return

    kinds: Dict[str, int] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"warnings.jsonl:{line_no}: not valid JSON — {exc}")
                continue
            for err in schemas["warning"].validate(record):
                report.error(f"warnings.jsonl:{line_no}: {err}")
            kind = record.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1

    if kinds:
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        report.note(f"warnings.jsonl declares {sum(kinds.values())} gap(s): {summary} (§10)")


def _check_grading(bundle_dir: Path, layers: Dict[str, str], schemas: Dict[str, Validator],
                   report: Report) -> None:
    """`grading.json` — contract §8, when the layer is declared.

    The one semantic check a schema cannot make is the capture's own honesty flag: an
    all-default block with `stackEvaluated` false or `volumesFound` 0 is a FAILED READ, not a
    neutral grade, and the first cut of this layer shipped exactly that.
    """
    path = _resolve_layer(bundle_dir, layers, "grading")
    if path is None or not path.is_file():
        return

    document = _load_json_layer(path, schemas["grading"], layers["grading"], report)
    if not isinstance(document, dict):
        return

    diagnostics = document.get("diagnostics", {})
    if isinstance(diagnostics, dict):
        if diagnostics.get("stackEvaluated") is False:
            report.error(f"{layers['grading']}: diagnostics.stackEvaluated is false — the volume "
                         "stack was never evaluated, so every block in this file is HDRP's "
                         "default rather than the game's grade")
        elif diagnostics.get("volumesFound") == 0:
            report.error(f"{layers['grading']}: diagnostics.volumesFound is 0 — the stack "
                         "matched no volume, so every block in this file is a default")
        unread = diagnostics.get("unread")
        if isinstance(unread, list) and unread:
            report.note(f"{layers['grading']}: the producer names {len(unread)} volume "
                        "component(s) it does not capture (diagnostics.unread) — read them "
                        "before concluding the colour chain is complete")

    blocks = [k for k in ("exposure", "whiteBalance", "colorAdjustments",
                          "shadowsMidtonesHighlights", "splitToning", "tonemapping",
                          "vignette", "fog", "lighting") if k in document]
    report.note(f"{layers['grading']} carries {', '.join(blocks)} (§8)")

    tonemapping = document.get("tonemapping")
    if isinstance(tonemapping, dict) and tonemapping.get("mode") == "External" \
            and tonemapping.get("hasLut"):
        report.warn(f"{layers['grading']}: tonemapping is External with a bound LUT, which is "
                    "NOT exported — the grading block is incomplete and a consumer replaying it "
                    "will not match the game (§8)")


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
    names: Optional[set] = None,
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
    parser = argparse.ArgumentParser(description="Validate a CS2 World Bundle (schema 2, frozen 2026-08-09).")
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
