#!/usr/bin/env python3
"""Generate the hand-authored example CS2 World Bundle (schema 2, frozen 2026-08-09).

The example is the executable half of the Phase-0 schema freeze: it is a tiny but
*complete* bundle (one cube mesh, one texture, one material, three instances, one
centerline network segment, a manifest) that `tools/validate_bundle.py` accepts.
A consumer can be built and smoke-tested against it with no game and no mod.

**Every layer the contract documents appears here** (H10, 2026-08-14), so a consumer author
meets each one before meeting a real city — including the three that are easy to skip and
expensive to get wrong: the lane GRAPH (§5A), the two record shapes sharing `lights.json`
(§7.2), and the §10 declared-gap amnesty, which the example exercises for real by placing an
instance whose mesh is deliberately absent and declared.

Everything here is deterministic — re-running rewrites byte-identical files. The layer
authors live beside it in `tools/examplebundle/`.

Usage:  python tools/make_example_bundle.py [output_dir]
        (default output_dir: docs/example-bundle)
"""

from __future__ import annotations

import sys
from pathlib import Path

from examplebundle import grading, ground, layers, manifest
from examplebundle.payloads import ASSET_ID

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "example-bundle"


def build(out_dir: Path) -> None:
    tex_rel, tex_hash, glb_size = layers.write_assets(out_dir)
    instances = layers.write_instances(out_dir)
    networks = layers.write_networks(out_dir)
    graph_lanes = layers.write_lane_graph(out_dir, networks)
    lane_records = layers.write_lane_geometry(out_dir, tex_rel)
    layers.write_colors(out_dir)
    layers.write_lights(out_dir)
    layers.write_warnings(out_dir)
    grading.write_grading(out_dir)
    rasters = ground.write_terrain(out_dir)
    area_record = ground.write_areas(out_dir, instances)
    clip_volumes = ground.write_clip_volumes(out_dir, networks, area_record)

    counts = {
        "meshes": 1,
        "materials": 1,
        "textures": 1,
        "instances": len(instances),
        "networkSegments": len(networks),
        "warnings": 1,
        "lanes": len(graph_lanes),
        "laneGeometry": len(lane_records),
        "areas": 1,
        "clipVolumes": len(clip_volumes),
    }
    bounds = manifest.bundle_bounds(instances, networks, area_record, clip_volumes)
    manifest.write_manifest(out_dir, bounds, counts, rasters)

    print(f"wrote example bundle to {out_dir}")
    print(f"  texture sha256 {tex_hash}")
    print(f"  {ASSET_ID}.glb {glb_size} bytes")


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT)
