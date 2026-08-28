"""The validation report, the per-run context every check takes, and the layer loaders."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from .schema import Validator, kind


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


class Context:
    """One validation run's shared state — the single argument every layer check takes.

    The core pass fills the join sets (`instance_ids`, `net_ids`, `mesh_names`) before the
    registry runs, and each layer check that counts records writes its count into `counts`
    for the manifest-counts comparison at the end. One signature for every check is what lets
    `validate_bundle.py` hold them as a registry instead of a call sequence with five shapes.
    """

    def __init__(
        self,
        bundle_dir: Path,
        layers: dict[str, str],
        schemas: dict[str, Validator],
        report: Report,
    ) -> None:
        self.bundle_dir = bundle_dir
        self.layers = layers
        self.schemas = schemas
        self.report = report
        # id -> every record seen so far is a §4.1 stack section (the one sanctioned duplicate)
        self.instance_ids: dict[int, bool] = {}
        self.net_ids: set = set()
        # Sub-mesh names, for the §7.2 appearance join: a palette or a procedural light is keyed
        # by the glTF node name inside the owning prefab's .glb, and a punctual light by the
        # prefab's own assetId, so both are looked up in this set. Object meshes only — a
        # colors.json key matching a network cross-section piece would be a coincidence, not a
        # join.
        self.mesh_names: set = set()
        self.counts: dict[str, int] = {}
        # Filled by the passes in `assets`, read by the counts/bounds checks at the end.
        self.manifest: dict[str, Any] = {}
        self.materials: dict[str, Any] = {}
        self.have_materials = False
        self.referenced_textures: set = set()
        self.instances: list[dict[str, Any]] = []
        self.networks: list[dict[str, Any]] = []
        self.mesh_files: list[Path] = []
        self.texture_files: list[Path] = []

    def layer_path(self, key: str) -> Path | None:
        rel = self.layers.get(key)
        return None if rel is None else self.bundle_dir / rel


def load_jsonl(path: Path, validator: Validator, label: str, report: Report) -> list[dict]:
    records: list[dict[str, Any]] = []
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
                report.error(f"{label}:{lineno}: expected a JSON object, got {kind(record)}")
                continue
            for err in validator.validate(record):
                report.error(f"{label}:{lineno}: {err}")
            records.append(record)
    return records


def load_json_layer(
    path: Path, validator: Validator, label: str, report: Report
) -> dict[str, Any] | None:
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
        report.error(f"{label}: expected a JSON object at the top level, got {kind(document)}")
        return None
    return document


def load_declared_missing(bundle_dir: Path) -> dict[str, str]:
    """Read warnings.jsonl and return {assetId: reason} for assets the producer could not deliver.

    Advisory and not schema-validated (§10), so this is deliberately forgiving: an unreadable or
    malformed file simply declares nothing, which leaves every gap an error rather than silently
    excusing it.
    """
    path = bundle_dir / "warnings.jsonl"
    declared: dict[str, str] = {}
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


def count_records(path: Path | None) -> int:
    """Record count of a JSON Lines layer — non-blank lines (blank lines are reported elsewhere)."""
    if path is None or not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def png_header(path: Path) -> tuple[int, int, int, int] | None:
    """Return (width, height, bit depth, colour type) from a PNG's IHDR, or None.

    Deliberately NOT `csiibundle.png.read_header`, which is the same walk and is what the three
    other callers in this repo now share. This validator's contract, stated at the top of
    `tools/validate_bundle.py`, is "the standard library, no pip installs, no producer source" —
    it is what a consumer author runs on a bundle they were handed, before they have any of
    this. That is worth one copy.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 29 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[24], data[25]
