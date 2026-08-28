"""The example bundle's bounds and its `bundle.json` manifest."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .payloads import HALF, HEIGHT


def bundle_bounds(instances, networks, area_record, clip_volumes) -> dict:
    """Bounds enclosing every placed mesh. The cube reaches HALF*sqrt(2) horizontally under an
    arbitrary yaw, so the footprint is padded by that instead of by HALF."""
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
    for node in area_record["nodes"]:
        pts.append(tuple(node))
    for volume in clip_volumes:
        for side in ("left", "right"):
            for x, _, z in volume.get(side, []):
                pts.append((x, volume["bottom"], z))
                pts.append((x, volume.get("top", volume["bottom"]), z))

    return {
        "min": [round(min(p[i] for p in pts), 6) for i in range(3)],
        "max": [round(max(p[i] for p in pts), 6) for i in range(3)],
    }


def _frame_and_geo() -> dict:
    """The manifest's frame and geo-referencing blocks."""
    return {
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
    }


def _layers_block() -> dict:
    return {
        "instances": "instances.jsonl",
        "networks": "networks.jsonl",
        "lanes": "lanes.jsonl",
        "laneGeometry": "lanemeshes.jsonl",
        "lanePrefabs": "laneprefabs.json",
        "materials": "materials.json",
        "colors": "colors.json",
        "lights": "lights.json",
        "meshes": "meshes/",
        "laneMeshes": "lanemeshes/",
        "textures": "textures/",
        "terrain": "terrain/heightmap.png",
        "surfaceIndex": "terrain/surface_index.png",
        "surfaces": "terrain/surfaces/",
        "water": "terrain/water_level.png",
        "areas": "areas.jsonl",
        "areaPrefabs": "areaprefabs.json",
        "clipVolumes": "clipvolumes.jsonl",
        "grading": "grading.json",
        "warnings": "warnings.jsonl",
    }


def write_manifest(out_dir: Path, bounds: dict, counts: dict, rasters: dict) -> None:
    manifest = {
        "schema": 2,
        "generator": "csii-world-bundle example bundle generator",
        "generatedAt": "2026-01-01T00:00:00Z",
        "game": {"name": "Cities: Skylines II", "version": "1.5.7f1", "mode": "Game"},
        "city": "Example City",
        "map": "Example Map",
    }
    manifest.update(_frame_and_geo())
    manifest["bounds"] = bounds
    manifest["mapSize"] = 14336.0
    manifest["counts"] = counts
    manifest["layers"] = _layers_block()
    manifest["rasters"] = rasters
    manifest["ripOptions"] = {
        "meshSource": "runtime",
        "lodCap": 0,
        "textureSizeCap": 2048,
        "includeSubObjects": True,
        "includeVector": False,
    }
    (out_dir / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
