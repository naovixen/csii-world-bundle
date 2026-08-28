"""A JSON Schema (draft 2020-12) subset evaluator — the general half of the validator.

Knows nothing about bundles: one `Validator` per schema document, local `$ref` into its own
`$defs`, and a hard error (`SchemaError`) on any keyword outside the supported subset, so a
schema author cannot silently use a keyword this evaluator would ignore.
"""

from __future__ import annotations

import json
import re
from typing import Any


class SchemaError(Exception):
    """Raised when a schema itself uses a keyword this evaluator does not implement."""


SUPPORTED = {
    "$schema",
    "$id",
    "$defs",
    "$ref",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "patternProperties",
    "propertyNames",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "oneOf",
    "anyOf",
    "allOf",
    "not",
}


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_ok(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return is_int(value) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return is_num(value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise SchemaError(f"unknown type name {name!r}")


def kind(value: Any) -> str:
    """The JSON type name of a Python value, for error messages."""
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
    if is_int(value):
        return "integer"
    return "number"


class Validator:
    """Evaluates one schema document (with local $ref into its own $defs)."""

    def __init__(self, schema: dict[str, Any], name: str) -> None:
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

    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise SchemaError(f"{self.name}: only local $ref is supported, got {ref!r}")
        node: Any = self.root
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[token]
        return node

    def validate(self, instance: Any) -> list[str]:
        errors: list[str] = []
        self._apply(self.root, instance, "$", errors)
        return errors

    def _apply(self, schema: Any, value: Any, path: str, errors: list[str]) -> None:
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
                errors.append(f"{path}: expected type {'|'.join(names)}, got {kind(value)}")
                return

        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

        if isinstance(value, str):
            self._apply_string(schema, value, path, errors)
        if is_num(value):
            self._apply_number(schema, value, path, errors)
        if isinstance(value, list):
            self._apply_array(schema, value, path, errors)
        if isinstance(value, dict):
            self._apply_object(schema, value, path, errors)

        self._apply_combinators(schema, value, path, errors)

        if "not" in schema:
            sub: list[str] = []
            self._apply(schema["not"], value, path, sub)
            if not sub:
                errors.append(f"{path}: value must NOT match {json.dumps(schema['not'])}")

    def _apply_combinators(
        self, schema: dict[str, Any], value: Any, path: str, errors: list[str]
    ) -> None:
        for key, combine in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "one")):
            if key not in schema:
                continue
            passing = []
            branch_errors: list[str] = []
            for i, branch in enumerate(schema[key]):
                sub: list[str] = []
                self._apply(branch, value, path, sub)
                if sub:
                    label = (
                        branch.get("title", f"branch {i}")
                        if isinstance(branch, dict)
                        else f"branch {i}"
                    )
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
                    errors.append(
                        f"{path}: matched {len(passing)} oneOf branches, expected exactly 1"
                    )

    def _apply_string(
        self, schema: dict[str, Any], value: str, path: str, errors: list[str]
    ) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")

    def _apply_number(
        self, schema: dict[str, Any], value: float, path: str, errors: list[str]
    ) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: {value} >= exclusiveMaximum {schema['exclusiveMaximum']}")

    def _apply_array(
        self, schema: dict[str, Any], value: list[Any], path: str, errors: list[str]
    ) -> None:
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: has {len(value)} items, minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: has {len(value)} items, maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(
            value
        ):
            errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(value):
                self._apply(schema["items"], item, f"{path}[{i}]", errors)

    def _apply_object(
        self, schema: dict[str, Any], value: dict[str, Any], path: str, errors: list[str]
    ) -> None:
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
