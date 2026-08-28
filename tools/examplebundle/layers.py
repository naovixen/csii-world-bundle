"""The example bundle's record layers — assets, instances, networks, lanes, appearance, gaps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .payloads import (
    ASSET_ID,
    HALF,
    HEIGHT,
    MATERIAL_ID,
    MISSING_ASSET_ID,
    make_checker_png,
    make_cube_glb,
    yaw_quat,
)


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def write_assets(out_dir: Path) -> tuple[str, str, int]:
    """The mesh, its texture and materials.json: `(texture rel path, its sha256, glb bytes)`."""
    (out_dir / "meshes").mkdir(parents=True, exist_ok=True)
    (out_dir / "textures").mkdir(parents=True, exist_ok=True)

    png = make_checker_png()
    tex_hash = hashlib.sha256(png).hexdigest()
    tex_rel = f"textures/{tex_hash}.png"
    (out_dir / tex_rel).write_bytes(png)

    glb = make_cube_glb(f"../{tex_rel}")
    (out_dir / "meshes" / f"{ASSET_ID}.glb").write_bytes(glb)

    materials = {
        MATERIAL_ID: {
            "baseColor": tex_rel,
            "tint": [1.0, 1.0, 1.0, 1.0],
            "alphaMode": "opaque",
            "doubleSided": False,
            "flags": [],
        }
    }
    (out_dir / "materials.json").write_text(
        json.dumps(materials, indent=2) + "\n", encoding="utf-8"
    )
    return tex_rel, tex_hash, len(glb)


def _example_buildings() -> list[dict]:
    return [
        {
            "id": 1001,
            "assetId": ASSET_ID,
            "kind": "building",
            "pos": [120.0, 12.5, -340.0],
            "rot": [0.0, 0.0, 0.0, 1.0],
            "scale": [1.0, 1.0, 1.0],
            "attrs": {
                "name": "Example Building A",
                "category": "Residential",
                "zone": "Residential Low Density",
                "theme": "European",
                "level": 3,
                "elevation": 12.5,
                "height": 4.0,
                "state": "normal",
            },
        },
        {
            "id": 1002,
            "assetId": ASSET_ID,
            "kind": "building",
            "pos": [160.0, 12.5, -340.0],
            "rot": yaw_quat(90.0),
            "attrs": {
                "name": "Example Building B",
                "category": "Residential",
                "zone": "Residential Low Density",
                "theme": "European",
                "level": 1,
                "elevation": 12.5,
                "height": 4.0,
                "state": "normal",
            },
        },
    ]


def write_instances(out_dir: Path) -> list[dict]:
    instances = _example_buildings()
    # The §10 declared-gap amnesty, exercised rather than described: this instance's
    # prefab has NO .glb, which would normally be a hard error — and is one when it is
    # undeclared. warnings.jsonl declares it, so the validator counts it in a note and the
    # placement survives for a consumer to stand something in for. Dropping the record
    # instead would lose the placement, which §4.1 requires to outlive the geometry.
    instances.append(
        {
            "id": 1003,
            "assetId": MISSING_ASSET_ID,
            "kind": "prop",
            "pos": [140.0, 12.0, -336.0],
            "rot": [0.0, 0.0, 0.0, 1.0],
            "attrs": {"name": "Example Bench (mesh undeliverable)", "category": "Street Furniture"},
        }
    )
    _write_jsonl(out_dir / "instances.jsonl", instances)
    return instances


def write_networks(out_dir: Path) -> list[dict]:
    networks = [
        {
            "id": 2001,
            "kind": "segment",
            "centerline": [[100.0, 12.0, -360.0], [140.0, 12.0, -360.0], [180.0, 12.2, -360.0]],
            "width": 16.0,
            "attrs": {
                "name": "Example Road",
                "asset": "Medium Road",
                "category": "road",
                "form": "normal",
                "lanes": 4,
                "direction": "twoway",
                "limit": 50.0,
                "length": 80.0,
                "width": 16.0,
                "elevation": 0.0,
            },
        }
    ]
    _write_jsonl(out_dir / "networks.jsonl", networks)
    return networks


def write_lane_graph(out_dir: Path, networks: list[dict]) -> list[dict]:
    """The lane graph (contract §5A): what makes the road drivable.

    networks.jsonl is the road SURFACE; this is the road GRAPH. Two car lanes running
    opposite ways along the example segment, and a footway beside it. `owner` is the generic
    ECS owner and merely happens to resolve to the segment here — a real bundle's owners are
    mostly net NODES, which get no record at all (§5.1), so a consumer must treat it as an
    advisory grouping key rather than a foreign key.
    """
    graph_lanes = [
        {
            "id": 4001,
            "kind": "car",
            "owner": networks[0]["id"],
            "path": [[100.0, 12.0, -362.0], [140.0, 12.0, -362.0], [180.0, 12.2, -362.0]],
            "length": 80.02,
            "limit": 50.0,
            "curviness": 0.004,
            "flags": ["Twoway"],
        },
        {
            "id": 4002,
            "kind": "car",
            "owner": networks[0]["id"],
            "path": [[180.0, 12.2, -358.0], [140.0, 12.0, -358.0], [100.0, 12.0, -358.0]],
            "length": 80.02,
            "limit": 50.0,
            "curviness": 0.004,
            "flags": ["Twoway"],
            "secondary": True,
        },
        {
            "id": 4003,
            "kind": "pedestrian",
            "owner": networks[0]["id"],
            "path": [[100.0, 12.0, -353.0], [180.0, 12.2, -353.0]],
            "length": 80.0,
        },
    ]
    _write_jsonl(out_dir / "lanes.jsonl", graph_lanes)
    return graph_lanes


def write_lane_geometry(out_dir: Path, tex_rel: str) -> list[dict]:
    """Lane geometry (contract §5B v2): a fence along a gentle curve.

    The cube stands in for a straight fence part; the record's parameters are what a
    consumer's bend has to consume: curve, spans (one hole), a width scale and paint.
    """
    (out_dir / "lanemeshes").mkdir(exist_ok=True)
    (out_dir / "lanemeshes" / f"{ASSET_ID}_fence.glb").write_bytes(
        make_cube_glb(f"../textures/{Path(tex_rel).name}")
    )

    lane_catalogue = {
        "Example_Fence": {
            "category": "fence",
            "hanging": 0.0,
            "parts": [
                {
                    "assetId": f"{ASSET_ID}_fence",
                    "pos": [0.0, 0.0, 0.0],
                    "rot": [0.0, 0.0, 0.0, 1.0],
                    "inverted": False,
                    "tilingCount": 0,
                    "geometryTiling": True,
                    "boundsMin": [-HALF, 0.0, -HALF],
                    "boundsMax": [HALF, HEIGHT, HALF],
                }
            ],
        }
    }
    (out_dir / "laneprefabs.json").write_text(
        json.dumps(lane_catalogue, indent=2) + "\n", encoding="utf-8"
    )

    lanes = [
        {
            "id": 3001,
            "prefab": "Example_Fence",
            "category": "fence",
            "owner": 2001,
            "curve": [
                [100.0, 12.0, -352.0],
                [126.0, 12.0, -352.0],
                [154.0, 12.0, -350.0],
                [180.0, 12.2, -350.0],
            ],
            "parts": [0],
            "spans": [[0.0, 0.45], [0.6, 1.0]],
            "widthScale": [1.0, 1.0],
            "colorSet": [[0.35, 0.3, 0.25, 1.0], [1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0]],
        }
    ]
    _write_jsonl(out_dir / "lanemeshes.jsonl", lanes)
    return lanes


def write_colors(out_dir: Path) -> None:
    """`colors.json` (contract §7.2), keyed by the SUB-MESH assetId — the glTF node name inside
    the owning prefab's .glb, which for this one-node cube is the prefab name itself. It is the
    prefab's POSSIBLE variations with their weights; what a given instance was actually drawn
    with is its own attrs.colorSet. Applying it is a masked MULTIPLY through the material's
    colorMask map, never a replacement."""
    document = {
        ASSET_ID: [
            {
                "channels": [
                    [0.82, 0.74, 0.62, 1.0],
                    [0.35, 0.33, 0.31, 1.0],
                    [1.0, 1.0, 1.0, 1.0],
                ],
                "probability": 70,
            },
            {
                "channels": [
                    [0.62, 0.66, 0.72, 1.0],
                    [0.28, 0.3, 0.34, 1.0],
                    [1.0, 1.0, 1.0, 1.0],
                ],
                "probability": 30,
            },
        ]
    }
    (out_dir / "colors.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def write_lights(out_dir: Path) -> None:
    """`lights.json` — ONE FILE, TWO RECORD SHAPES, keyed the same way and placed by OPPOSITE
    rules. The procedural entry has no position because the light IS the emissive material: put
    it at the centroid of the primitive using that slot. The punctual entry carries a real
    local position and must use it — falling back to the centroid would move every streetlamp
    to the middle of its pole. A consumer that treats them uniformly makes both or neither."""
    document = {
        ASSET_ID: [
            {
                "slot": 0,
                "color": [1.0, 0.87, 0.68, 1.0],
                "colorOff": [0.0, 0.0, 0.0, 1.0],
                "purpose": "DecorativeLight",
                "responseSpeed": 0.5,
            },
            {
                "kind": "punctual",
                "effect": "Example Wall Lamp",
                "position": [0.0, 3.2, 2.1],
                "rotation": [0.0, 0.0, 0.0, 1.0],
                "parentMesh": -1,
                "range": 12.0,
                "finalColor": [3.2, 2.78, 2.18, 1.0],
                "type": "Spot",
                "color": [1.0, 0.87, 0.68, 1.0],
                "intensity": 2000.0,
                "intensityUnit": "Lumen",
                "temperature": 2700.0,
                "spotShape": "Cone",
                "spotAngle": 100.0,
                "innerSpotPercentage": 60.0,
            },
        ]
    }
    (out_dir / "lights.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def write_warnings(out_dir: Path) -> None:
    """warnings.jsonl (contract §10): the producer declaring a gap it could not close."""
    (out_dir / "warnings.jsonl").write_text(
        json.dumps(
            {"kind": "mesh", "assetId": MISSING_ASSET_ID, "reason": "mesh not readable at runtime"},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
