"""Where the selected instances are — the pre-pass behind `--center auto/downtown/interchange`.

The map origin is not the city: a save's buildings can sit kilometres off (0, 0), so a radius
around a guessed point mostly finds empty ground, which looks exactly like a broken import.
Every run surveys before it places anything.

The only module of this package that never touches `bpy`, deliberately — it is arithmetic over
`instances.jsonl` and `networks.jsonl`, so it is the one part of the adapter a test can reach
without Blender.
"""

import math
import os

from csiibundle.reader import read_jsonl


def skyline_weight(record):
    """How much a building counts towards "this cell is downtown".

    The square of its height, so that a cluster of towers outranks a larger number of houses:
    five 60 m towers score 18,000 against a hundred 4 m houses' 1,600, where a plain count puts
    the houses twenty to one. Downtown is what makes a useful test region — it is where the tall
    meshes, the dense props and every road category are — and a plain densest-cell search finds
    suburbia instead, because that is where the object count is.
    """
    height = record.get("attrs", {}).get("height")
    try:
        height = float(height)
    except (TypeError, ValueError):
        height = 0.0
    # A building with no height attribute still counts as one building rather than nothing.
    return max(height, 1.0) ** 2


def interchange(instances_path, networks_path, cell=250.0):
    """The tallest place that also has a motorway junction in it.

    Downtown and the motorway are usually not the same place, and looking at either alone leaves
    half the interesting geometry out of frame: junction flares and elevated forms are on the
    motorway, tall meshes and dense props are downtown. This scores only those cells that hold
    highway or elevated network records, by the building skyline over their 3x3 neighbourhood —
    a junction cell often holds no buildings itself, but the one next to it does.

    Returns (x, z) or None if the bundle has no highway records.
    """
    if not os.path.isfile(networks_path):
        return None

    junction_cells = set()
    for record in read_jsonl(networks_path):
        attrs = record.get("attrs", {})
        if attrs.get("category") != "highway" and attrs.get("form") != "elevated":
            continue
        where = record["pos"] if "pos" in record else record.get("centerline", [[0, 0, 0]])[0]
        junction_cells.add((math.floor(where[0] / cell), math.floor(where[2] / cell)))

    if not junction_cells:
        return None

    skyline = {}
    for record in read_jsonl(instances_path):
        if record.get("kind") != "building":
            continue
        x, _, z = record["pos"]
        key = (math.floor(x / cell), math.floor(z / cell))
        skyline[key] = skyline.get(key, 0.0) + skyline_weight(record)

    def neighbourhood(key):
        cx, cz = key
        return sum(skyline.get((cx + dx, cz + dz), 0.0) for dx in (-1, 0, 1) for dz in (-1, 0, 1))

    best = max(junction_cells, key=neighbourhood)
    if neighbourhood(best) <= 0:
        return None
    return (best[0] * cell + cell / 2, best[1] * cell + cell / 2)


def locate(instances_path, kinds, cell=250.0, weight=None):
    """Where the selected kinds actually are: bounds, centroid, and the densest cell.

    A pre-pass over instances.jsonl, which costs a few seconds on a full city and saves the far
    more expensive mistake of importing a patch of empty ground and concluding the bundle is
    broken. Returns None when the selection is empty.

    `weight` scores each record's contribution to the densest cell; the default counts them.
    """
    count = 0
    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    sum_x = sum_z = 0.0
    grid = {}

    for record in read_jsonl(instances_path):
        if record.get("kind") not in kinds:
            continue
        x, _, z = record["pos"]
        count += 1
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_z, max_z = min(min_z, z), max(max_z, z)
        sum_x += x
        sum_z += z
        cell_key = (math.floor(x / cell), math.floor(z / cell))
        grid[cell_key] = grid.get(cell_key, 0) + (weight(record) if weight else 1)

    if count == 0:
        return None

    densest = max(grid, key=grid.get)
    return {
        "count": count,
        "bounds": (min_x, max_x, min_z, max_z),
        "centroid": (sum_x / count, sum_z / count),
        "densest": (densest[0] * cell + cell / 2, densest[1] * cell + cell / 2),
        "densest_count": grid[densest],
    }
