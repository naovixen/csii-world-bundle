#!/usr/bin/env python3
"""Standalone validator for a CS2 World Bundle (schema 2, frozen 2026-08-09).

Standalone means exactly that: Python 3.8+ and the standard library, no pip installs, no
game, no producer source. It is the reference check for the Phase-0 contract, and the thing a
consumer author runs to confirm a bundle they were handed is well-formed. The implementation
lives beside it in `tools/bundlecheck/`, which honours the same contract.

It does two jobs:

  1. Structural — validates every declared layer against docs/schema/*.schema.json (sixteen
     documents as of 2026-08-14, one per layer), using a small built-in evaluator for the JSON
     Schema subset those files use (`bundlecheck.schema`).
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
import json
import sys
from pathlib import Path

from bundlecheck import assets, auxlayers, polygons, rasters
from bundlecheck.report import Context, Report, count_records
from bundlecheck.schema import SchemaError, Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_DIR = REPO_ROOT / "docs" / "schema"

BOUNDS_TOLERANCE = 1e-6

# One schema document per layer of the contract. `lanemesh` is §5B's parameter record and
# `laneprefabs` its catalogue; `lane` is §5A's graph, a different file. Every name here must
# exist in the schema directory — a missing one is a setup error, not a bundle error.
SCHEMA_NAMES = (
    "bundle",
    "instance",
    "network",
    "materials",
    "lane",
    "lanemesh",
    "laneprefabs",
    "colors",
    "lights",
    "area",
    "areaprefabs",
    "clipvolume",
    "district",
    "warning",
    "surfaceindex",
    "grading",
)

# The layer checks that need no join beyond what `Context` carries, in report order. One
# `check(ctx)` signature each — the registry the `_check_*` call sequence used to be.
LAYER_CHECKS = (
    polygons.check_areas,
    polygons.check_districts,
    polygons.check_clip_volumes,
    auxlayers.check_lanes,
    auxlayers.check_appearance,
    auxlayers.check_warnings,
    auxlayers.check_grading,
)


def _load_schemas(schema_dir: Path) -> dict[str, Validator]:
    schemas = {}
    for name in SCHEMA_NAMES:
        path = schema_dir / f"{name}.schema.json"
        schemas[name] = Validator(json.loads(path.read_text(encoding="utf-8")), path.name)
    return schemas


def _check_layer_files(ctx: Context) -> None:
    """Every declared layer must exist, as the right kind of thing."""
    for key, rel in ctx.layers.items():
        target = ctx.bundle_dir / rel
        if rel.endswith("/"):
            if not target.is_dir():
                ctx.report.error(f"layers.{key}: directory {rel!r} does not exist")
        elif not target.is_file():
            ctx.report.error(f"layers.{key}: file {rel!r} does not exist")


def _check_counts(ctx: Context) -> None:
    """Manifest counts against what the bundle actually contains."""
    counts = ctx.manifest["counts"]
    report = ctx.report
    for key, actual in (
        ("meshes", len(ctx.mesh_files)),
        ("materials", len(ctx.materials)),
        ("textures", len(ctx.texture_files)),
        ("instances", len(ctx.instances)),
        ("networkSegments", len(ctx.networks)),
    ):
        if counts[key] != actual:
            report.error(
                f"bundle.json counts.{key} is {counts[key]}, but the bundle contains {actual}"
            )

    # The record layers added after the freeze carry a count whenever their layer is declared
    # (§3); a declared layer with no count is itself an error, so truncation detection cannot
    # silently lapse.
    for key, layer_key, actual in (
        ("lanes", "lanes", count_records(ctx.layer_path("lanes"))),
        ("laneGeometry", "laneGeometry", ctx.counts["laneGeometry"]),
        ("areas", "areas", ctx.counts["areas"]),
        ("districts", "districts", ctx.counts["districts"]),
        ("clipVolumes", "clipVolumes", ctx.counts["clipVolumes"]),
    ):
        if layer_key in ctx.layers:
            if key not in counts:
                report.error(
                    f"bundle.json: layers.{layer_key} is declared but counts.{key} is missing"
                )
            elif counts[key] != actual:
                report.error(
                    f"bundle.json counts.{key} is {counts[key]}, but the bundle contains {actual}"
                )
        elif key in counts and counts[key] != 0:
            report.error(
                f"bundle.json counts.{key} is {counts[key]}, but no layers.{layer_key} is declared"
            )

    # counts.warnings has been optional since the freeze — checked only when stated.
    if "warnings" in counts:
        actual = count_records(ctx.layer_path("warnings"))
        if counts["warnings"] != actual:
            report.error(
                f"bundle.json counts.warnings is {counts['warnings']}, "
                f"but the bundle contains {actual}"
            )


def _check_bounds(ctx: Context) -> None:
    """Every instance position and network point must lie inside the manifest bounds."""
    report = ctx.report
    lo = ctx.manifest["bounds"]["min"]
    hi = ctx.manifest["bounds"]["max"]
    for axis in range(3):
        if lo[axis] > hi[axis]:
            report.error(f"bundle.json bounds: min[{axis}] > max[{axis}]")
    points: list[tuple[str, list[float]]] = [
        (f"instances.jsonl line {i + 1}", r["pos"]) for i, r in enumerate(ctx.instances)
    ]
    for i, record in enumerate(ctx.networks):
        if "pos" in record:
            points.append((f"networks.jsonl line {i + 1}", record["pos"]))
        for j, point in enumerate(record.get("centerline", [])):
            points.append((f"networks.jsonl line {i + 1} centerline[{j}]", point))
    for label, point in points:
        for axis in range(3):
            if (
                point[axis] < lo[axis] - BOUNDS_TOLERANCE
                or point[axis] > hi[axis] + BOUNDS_TOLERANCE
            ):
                report.error(
                    f"{label}: axis {axis} value {point[axis]} lies outside bundle.json bounds"
                )


def validate_bundle(bundle_dir: Path, schema_dir: Path) -> Report:
    report = Report()
    schemas = _load_schemas(schema_dir)

    manifest_path = bundle_dir / "bundle.json"
    if not manifest_path.is_file():
        report.error("bundle.json is missing at the bundle root")
        return report
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for err in schemas["bundle"].validate(manifest):
        report.error(f"bundle.json: {err}")
    if report.errors:
        return report

    ctx = Context(bundle_dir, manifest["layers"], schemas, report)
    ctx.manifest = manifest
    _check_layer_files(ctx)
    assets.check_materials(ctx)
    assets.check_instances(ctx)
    assets.check_networks(ctx)
    assets.check_lane_geometry(ctx)
    assets.scan_meshes(ctx)
    assets.check_unreferenced(ctx)
    for check in LAYER_CHECKS:
        check(ctx)
    _check_counts(ctx)
    _check_bounds(ctx)
    rasters.check_rasters(ctx)

    report.note(f"schema {manifest['schema']}, generator {manifest['generator']!r}")
    report.note(f"{len(ctx.instances)} instances, {len(ctx.networks)} network records")
    report.note(
        f"{len(ctx.mesh_files)} meshes, {len(ctx.materials)} materials, "
        f"{len(ctx.texture_files)} textures"
    )
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a CS2 World Bundle (schema 2, frozen 2026-08-09)."
    )
    parser.add_argument(
        "bundle", type=Path, help="path to the bundle directory (the one holding bundle.json)"
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        help="directory holding *.schema.json",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print only errors and the verdict"
    )
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
