"""Printing what a level costs, and the `cs2beamng budget` command."""

import argparse
import json
import sys

from .budget import measure, measure_vanilla

MIB = 1024.0 * 1024.0

# Measured from the shipped levels (2026-08-18, BeamNG 0.39) with `--vanilla`. `west_coast_usa` is
# the big open-world one and the fair comparison for a city; the maxima are over every level in
# `content/levels/`. THEIRS, not ours — re-measure rather than edit.
VANILLA = {
    "west_coast_usa": {
        "shapeFiles": 2608,
        "tsStatics": 22067,
        "forestTypes": 226,
        "forestItems": 133375,
        "materials": 586,
        "visibleDistance": 15667,
    },
    "italy": {
        "shapeFiles": 1419,
        "tsStatics": 13971,
        "forestTypes": 208,
        "forestItems": 206152,
        "materials": 171,
        "visibleDistance": 25000,
    },
    "johnson_valley": {
        "shapeFiles": 498,
        "tsStatics": 15559,
        "forestTypes": 78,
        "forestItems": 663057,
        "materials": 171,
        "visibleDistance": 24500,
    },
}


def report(result, compare=None):
    compare = compare or VANILLA
    lines = []
    add = lines.append
    add(
        f"level {result['level']} — {result['viewDistance']:.0f} m view from "
        f"({result['spawn'][0]:.0f}, {result['spawn'][1]:.0f})"
    )
    add("")
    _resident(result, add)
    _heaviest_shapes(result, add)
    _triangles(result, add)
    _counts(result, compare, add)
    return "\n".join(lines)


def _resident(result, add):
    add("  resident                    MiB")
    for group, value in sorted(result["meshBytes"].items(), key=lambda kv: -kv[1]):
        add(f"    meshes {group:<20} {value / MIB:8.0f}")
    add(f"    meshes {'TOTAL':<20} {result['meshBytesTotal'] / MIB:8.0f}")
    for ext, value in sorted(result["textureBytes"].items(), key=lambda kv: -kv[1]):
        add(f"    textures {ext:<18} {value / MIB:8.0f}")
    add(f"    {'RESIDENT TOTAL':<27}{result['residentTotal'] / MIB:8.0f}")
    add("")


def _heaviest_shapes(result, add):
    top100 = result.get("shapeBytesTop100") or 0
    if top100:
        share = 100.0 * top100 / max(1, result["meshBytesTotal"])
        add("  the heaviest shapes — a shape is mounted WHOLE for one instance in view")
        add(
            f"    top 100 of {result['shapeFiles']:,} shape files{top100 / MIB:16.0f} MiB "
            f"({share:.0f} % of all mesh bytes)"
        )
        for value, name in (result.get("heaviestShapes") or [])[:8]:
            add(f"    {name[:44]:<44}{value / MIB:8.1f} MiB")
        add("")


def _triangles(result, add):
    add("  triangles inside the view distance")
    for group, value in sorted(result["trianglesInView"].items(), key=lambda kv: -kv[1]):
        add(f"    {group:<27}{value:>12,}  ({result['staticsInView'].get(group, 0)} statics)")
    add(f"    {'TOTAL':<27}{result['trianglesInViewTotal']:>12,}")
    if result.get("unknownLadders"):
        add("    NOT COUNTED — the `.dae` sources were dropped, so these groups' detail ladders")
        add(f"    could not be read: {', '.join(result['unknownLadders'])}. Build with `keep_dae`")
        add("    to measure them.")
    add("")


def _counts(result, compare, add):
    reference = compare.get("west_coast_usa") or {}
    add("  counts                        this   west_coast_usa")
    for key, label in (
        ("shapeFiles", "shape files"),
        ("tsStatics", "TSStatics"),
        ("forestTypes", "forest types"),
        ("forestItems", "forest items"),
        ("materials", "materials"),
    ):
        theirs = reference.get(key)
        ratio = f"{result[key] / theirs:5.1f}x" if theirs else "    —"
        add(f"    {label:<22}{result[key]:>10,}{(theirs or 0):>12,}  {ratio}")
    add(f"    {'collision triangles':<22}{result['collisionTriangles']:>10,}  (per shape file)")
    placed = result.get("collisionTrianglesPlaced")
    if placed is not None:
        by_group = ", ".join(
            f"{g} {n:,}"
            for g, n in sorted(
                result.get("collisionPlacedByGroup", {}).items(), key=lambda kv: -kv[1]
            )
        )
        add(f"    {'collision placed':<22}{placed:>10,}  ({by_group})")
        add(
            f"    {'collision instances':<22}{result.get('collisionPlacedInstances', 0):>10,}  "
            "(objects and forest items with any collision)"
        )
    add(f"    {'cooked shapes':<22}{result['cooked']:>10,} of {result['shapeFiles']:,}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cs2beamng budget", description=__doc__.split("\n\n")[0])
    parser.add_argument("level", nargs="?", help="built level directory")
    parser.add_argument("--view", type=float, default=4000.0, help="view distance in metres")
    parser.add_argument(
        "--spawn", default=None, help="X,Y to measure from (default: the SpawnSphere)"
    )
    parser.add_argument("--screen-height", type=float, default=1080.0)
    parser.add_argument("--json", default=None, help="also write the numbers here")
    parser.add_argument(
        "--vanilla", default=None, help="BeamNG install to re-measure the maxima from"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.vanilla:
        rows = measure_vanilla(args.vanilla)
        for name, row in sorted(rows.items()):
            print(f"{name:28s} " + "  ".join(f"{k}={v}" for k, v in row.items()))
        return 0
    if not args.level:
        parser.error("a level directory is required (or --vanilla)")
    spawn = None
    if args.spawn:
        parts = args.spawn.replace(",", " ").split()
        spawn = (float(parts[0]), float(parts[1]))
    result = measure(args.level, args.view, spawn, args.screen_height)
    print(report(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, indent=1, sort_keys=True)
    return 0
