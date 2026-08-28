"""The example bundle's ground — the §8 rasters, §8b surface areas, §8c clip volumes."""

from __future__ import annotations

import json
from pathlib import Path

from .payloads import make_checker_png, make_gray_png

# One small grid shared by every raster: 32x32 cells at 4 m cover x 80..208, z -300..-428 —
# the whole example scene. Rows run toward -Z (row 0 is the northernmost, z = -300), columns
# toward +X.
GRID = 32
CELL_M = 4.0
ORIGIN = (80.0, 0.0, -300.0)  # top-left cell centre
ELEVATION_SCALE = 0.001  # worldY = value * 0.001 m


def _sample_grids() -> tuple[list[int], list[int], list[int]]:
    """`(heights, surface ids, water levels)`, row-major over the example grid."""
    heights: list[int] = []
    surface: list[int] = []
    levels: list[int] = []
    for row in range(GRID):
        z = ORIGIN[2] - row * CELL_M
        for col in range(GRID):
            x = ORIGIN[0] + col * CELL_M
            # A gentle west-east slope around the 12 m the instances stand at...
            world_y = 11.0 + (x - ORIGIN[0]) * 0.02
            # ...flattened to exactly 12 m under the example road (z -368..-352, §5's
            # segment), which is what a road-flattened band looks like in a real rip.
            on_road = -368.0 <= z <= -352.0
            if on_road:
                world_y = 12.0
            # The road's eastern end dives under a covering bulge — the shape a tunnel
            # portal or sunken trench has in a real rip: the game keeps the covering
            # terrain in the heightmap and hides it per-frame, so the bundle records the
            # bulge here and ships the clip volume (§8c) that opens it. A consumer that
            # ignores the clip layer walls the road off.
            cut = on_road and x >= 188.0
            if cut:
                world_y = 18.0
            heights.append(round(world_y / ELEVATION_SCALE))
            surface.append(1 if on_road else 0)
            # A pond in the north-east corner. The water layer stores the SURFACE LEVEL
            # in the heightmap's own encoding (0 = dry): a still pond is one constant
            # level wherever there is water, and the ground plane cuts the shoreline.
            # 15.32 m is the pond-centre ground (13.32 m) plus its 2 m depth.
            in_pond = (abs(x - 196.0) + abs(z - -308.0)) < 20.0
            levels.append(round(15.32 / ELEVATION_SCALE) if in_pond else 0)
    return heights, surface, levels


def write_terrain(out_dir: Path) -> dict:
    """The §8 rasters and the surfaces catalogue; returns the manifest's `rasters` block."""
    heights, surface, levels = _sample_grids()
    terrain_dir = out_dir / "terrain"
    (terrain_dir / "surfaces").mkdir(parents=True, exist_ok=True)
    (terrain_dir / "heightmap.png").write_bytes(make_gray_png(heights, GRID, GRID, 16))
    (terrain_dir / "surface_index.png").write_bytes(make_gray_png(surface, GRID, GRID, 8))
    (terrain_dir / "water_level.png").write_bytes(make_gray_png(levels, GRID, GRID, 16))

    grass_png = make_checker_png(16, 4, (96, 132, 72, 255), (84, 118, 64, 255))
    dirt_png = make_checker_png(16, 4, (128, 108, 88, 255), (112, 94, 76, 255))
    (terrain_dir / "surfaces" / "grass.png").write_bytes(grass_png)
    (terrain_dir / "surfaces" / "dirt.png").write_bytes(dirt_png)
    surfaces_index = {
        "0": {"name": "natural", "texture": "terrain/surfaces/grass.png"},
        "1": {"name": "roadbed", "texture": "terrain/surfaces/dirt.png"},
    }
    (terrain_dir / "surfaces" / "index.json").write_text(
        json.dumps(surfaces_index, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "resolution": CELL_M,
        "size": [GRID, GRID],
        "originWorld": list(ORIGIN),
        "elevationUnits": "m",
        "elevationScale": ELEVATION_SCALE,
        "elevationOffset": 0.0,
    }


def write_areas(out_dir: Path, instances: list[dict]) -> dict:
    """Surface areas (contract §8b): a concrete pad beside the road.

    The painted ground of meshless prefabs (parking lots, driveways). One quad, two
    triangles, indices into its own nodes; the prefab key resolves in areaprefabs.json.
    """
    area_record = {
        "id": 9001,
        "prefab": "example_concrete_pad",
        "owner": instances[0]["id"],
        "nodes": [
            [100.0, 12.0, -344.0],
            [112.0, 12.0, -344.0],
            [112.0, 12.0, -336.0],
            [100.0, 12.0, -336.0],
        ],
        "triangles": [[0, 1, 2], [0, 2, 3]],
    }
    (out_dir / "areas.jsonl").write_text(
        json.dumps(area_record, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    # The catalogue carries the four keys the game pushes for every version — and
    # `rendererPriority` above all, because without it a consumer has no rule for which of two
    # overlapping areas paints on top. `uvScale` is the authored rate: uv = world position ×
    # uvScale, so 0.2 means one repeat every 5 m. Tiling is world-planar, never UV-mapped.
    (out_dir / "terrain" / "surfaces" / "area_example_concrete_pad.png").write_bytes(
        make_checker_png(16, 4, (168, 166, 162, 255), (152, 150, 147, 255))
    )
    (out_dir / "areaprefabs.json").write_text(
        json.dumps(
            {
                "example_concrete_pad": {
                    "name": "Example Concrete Pad",
                    "color": [0.42, 0.4, 0.38, 1.0],
                    "texture": "terrain/surfaces/area_example_concrete_pad.png",
                    "uvScale": 0.2,
                    "heightOffset": 0.0,
                    "expandAmount": 0.5,
                    "roundness": 0.0,
                    "rendererPriority": 0,
                    "decalLayers": ["Terrain"],
                    "metallic": 0.0,
                    "smoothness": 0.35,
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return area_record


def write_clip_volumes(out_dir: Path, networks: list[dict], area_record: dict) -> list[dict]:
    """Clip volumes (contract §8c): what opens the tunnel portal.

    One edge strip: the two boundary Béziers straddling the whole road at the lane height,
    with the 30 m band a portal gets. It deliberately spans the ENTIRE road, not just the
    covered part — the band test is what decides, exactly as in the game. West of x=188 the
    terrain sits at the lane, 0 m over it, so nothing is cut; from x=188 east it bulges to
    18 m, 6 m over the lane and inside the band, so those cells open.

    Plus one area record: an authored clip surface. It carries its own polygon (schema 2 —
    the game never *paints* a clip surface, so areas.jsonl does not carry it; the pad here
    reuses the same footprint only for the example's economy), and `top` is absent because
    an authored clip surface hides whatever covers it, however high.
    """
    clip_volumes = [
        {
            "id": networks[0]["id"],
            "kind": "edge",
            "left": [
                [96.0, 12.0, -368.0],
                [132.0, 12.0, -368.0],
                [168.0, 12.0, -368.0],
                [206.0, 12.0, -368.0],
            ],
            "right": [
                [96.0, 12.0, -352.0],
                [132.0, 12.0, -352.0],
                [168.0, 12.0, -352.0],
                [206.0, 12.0, -352.0],
            ],
            "bottom": 0.5,
            "top": 30.3,
        },
        {
            "id": area_record["id"],
            "kind": "area",
            "area": area_record["id"],
            "bottom": 0.5,
            "nodes": area_record["nodes"],
            "triangles": area_record["triangles"],
        },
    ]
    (out_dir / "clipvolumes.jsonl").write_text(
        "".join(json.dumps(v, separators=(",", ":")) + "\n" for v in clip_volumes),
        encoding="utf-8",
    )
    return clip_volumes
