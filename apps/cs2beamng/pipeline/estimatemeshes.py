"""What the meshes will weigh, calibrated on this machine's own past builds.

There is no way to know a mesh's cooked size without cooking it, so this reads the rolling
history of what the last builds actually produced and scales it by what these settings change."""

import os

from .. import settings as settings_mod
from . import history, meshindex
from .estimatebase import KIND_SECTION, TILE_GROUPS, dir_bytes, load_json

# -- the meshes, calibrated -----------------------------------------------------------------------


def _kind_groups(groups, selected, rates, _values):
    """Fill `groups` with one entry per placed kind, and return their bytes."""
    total = 0
    for kind, bucket in sorted(selected.items()):
        ratio = rates.get(f"size/cdae_per_source_{kind}") or 1.0
        size = int(bucket["bytes"] * ratio)
        groups[kind] = {
            "shapes": bucket["shapes"],
            "sourceBytes": bucket["bytes"],
            "bytes": size,
            "ratio": round(ratio, 3),
            "section": KIND_SECTION.get(kind, "world"),
        }
        total += size
    return total


def mesh_estimate(values, bundle, index, census=None):
    """What the level's `.cdae` files will weigh: the exact shape list, times measured ratios."""
    rates = history.load()
    map_size = float(((census or {}).get("bundle") or {}).get("mapSize") or 0) or _map_size(bundle)
    selected = meshindex.select(index, values)
    groups = {}
    total = 0
    total = _kind_groups(groups, selected, rates, values)
    scale = _region_scale(values, map_size)
    for key, parts, group in TILE_GROUPS:
        if not values.get(key, True):
            continue
        source = dir_bytes(os.path.join(bundle, *parts))
        if not source:
            continue
        ratio = rates.get(f"size/cdae_per_source_{group}") or 1.0
        size = int(source * scale * ratio)
        groups[group] = {
            "shapes": None,
            "sourceBytes": int(source * scale),
            "bytes": size,
            "ratio": round(ratio, 3),
            "section": "world",
        }
        total += size
    return {
        "bytes": total,
        "groups": groups,
        "basis": _mesh_basis(rates),
        "approximations": [
            {
                "what": "a cooked shape's bytes are its source glTF times a measured ratio, not the "
                "cooked file itself",
                "why": "the cook is BeamNG's own compiler and only running it says what it produces",
                "measured": "0.97 over the reference city's 4,272 object shapes, per kind 1.02 "
                "buildings / 0.65 props / 1.03 trees",
            },
            {
                "what": "a road, marking, area or foundation tile is sized from the bake product it "
                "is merged out of, scaled by how much of the map the region covers",
                "why": "how the merge divides into tiles is the build's own tiling, and a tile is "
                "one shape however many records went into it",
                "measured": "1.31 networks / 1.52 areas / 1.59 markings / 1.53 foundations",
            },
            {
                "what": "a recoloured prefab is counted once per colour the bake gave it, including "
                "the copies too thinly placed to be worth their own type",
                "why": "which copies survive is decided by `forest_min_items` against the item count "
                "per colour, which is the build's own count and not the bake's",
                "measured": "17 of 4,272 shapes and 5.9 MiB of 2,068 on the reference city (+5.7 % "
                "of the tree group, +0.3 % of all shapes)",
            },
            {
                "what": "trees standing in the road are still counted",
                "why": "the clearance rule is a spatial pass over the lane layer and it has never "
                "removed a whole shape",
                "measured": "1,806 of 1,276,060 records on the reference city, 0 shapes",
            },
        ],
    }


def stale_bake_note(census):
    """Both halves are sized from the bake's own output, so a bake these settings have moved past
    is a figure for the LAST one. The census already knows which products are stale; saying so
    here is the difference between an estimate that is behind and an estimate that is wrong."""
    products = ((census or {}).get("derived") or {}).get("products") or {}
    stale = sorted(
        name
        for name, info in products.items()
        if info.get("status") in ("stale", "absent")
        and name in ("materials", "lods", "areas", "markings", "skirts", "terraincut")
    )
    if not stale:
        return []
    return [
        {
            "what": f"the figures are for the last bake; {len(stale)} of its products "
            f"({', '.join(stale)}) will be rebuilt with these settings",
            "why": "both halves are sized from what the bake produced, and what the next bake "
            "produces is what the build will ship",
            "measured": None,
        }
    ]


def _region_scale(values, map_size):
    """How much of the map a region keeps — the square every stage that reads one tests."""
    region = settings_mod.region_of(values)
    if not region or not map_size:
        return 1.0
    radius = float(region["radius"])
    return min(1.0, 4.0 * radius * radius / (map_size * map_size))


def _map_size(bundle):
    manifest = load_json(os.path.join(bundle, "bundle.json")) or {}
    try:
        return float(manifest.get("mapSize") or 0)
    except (TypeError, ValueError):
        return 0.0


def _mesh_basis(rates):
    builds = int(rates.get("size/builds") or 0)
    if builds >= 1:
        return f"from your own last {builds} build{'s' if builds > 1 else ''}"
    return "from the reference city"


def terrain_file_bytes(values, _bundle):
    """The heightfield on disk: three bytes a sample plus a header.

    MEASURED on the reference level - an 8,192 grid wrote 201,326,743 bytes, which is
    8192 x 8192 x 3 + 151. It is not resident the way a shape is (BeamNG streams the `.ter`), so
    it is in the disk figure and out of the video memory one.
    """
    size = 4096 * max(1, int(values.get("terrain_upsample") or 1))
    return size * size * 3 + 151
