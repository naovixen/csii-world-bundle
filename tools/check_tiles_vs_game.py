"""Does a built level stop drawing each tile where CS2 would?

The A4 parity predicate (plan §Part 3, audit §63). The A3 predicate
(`check_instances_vs_game.py`) asks that question of the 1.2M placed objects; this one asks it of
the **merged world tiles** — roads, lane markings, painted areas, roadside furniture — which A3
shipped at one always-on detail level, so a 256 m tile only fell below its 2 px threshold at about
85 km and every one of 9.27M network triangles was live inside a 4 km view distance.

Every question has an answer in the game's own implementation:

| question | the game's rule |
|---|---|
| how far is a road segment drawn? | `NetCompositionHelpers.CalculateMinLod` → `CalculateLodLimit(GetRenderingSize(float2(m_Width, Size(m_HeightRange))), mean piece bias)`, and `PreCullingSystem` culls an edge on the **min** of its three compositions |
| how far is a lane marking / fence drawn? | `NetLaneGeometryData.m_MinLod` — `NetInitializeSystem` takes the min over the prefab's meshes of `CalculateLodLimit(GetRenderingSize(Size(bounds).xy), m_LodBias)` |
| how far is painted ground drawn? | `Game.Areas.GeometrySystem` per triangle: `CalculateLodLimit(GetRenderingSize(float3(4·d1, nodeDistance, 4·d2)))`, where `d1 ≤ d2` are the two nearest distances from the triangle's centre to the area's own boundary |
| and then? | `CalculateLod(d²) >= m_MinLod`, i.e. drawn out to `2^((128 − L)/6) · levelOfDetail · 540 / tan(fov/2)` |

Usage:

    python tools/check_tiles_vs_game.py <bundle> <level_dir> [--samples N]

The shipped answer is read **back off the `.dae` on disk** (its `nulldetail<px>` node and its
`_a<px>` detail nodes), never from the build report. The expected answer is recomputed from the
bake's own tiles, whose primitive material names carry the game's per-primitive limit
(`csiibundle.bake.tiles.MIN_LOD_MARK`) — so this compares the level against the GAME's rule and
not against the build that produced it.

Exits non-zero if any tile's shipped threshold disagrees with the recomputed one by more than
one pixel of rounding.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from csiibundle import glb, lod  # noqa: E402
from csiibundle.bake.locate import default_bundle_dir  # noqa: E402
from csiibundle.bake.tiles import split_material_name  # noqa: E402

try:
    from cs2beamng.beamng import tilelod
except ImportError:
    tilelod = None

# The bake products whose tiles carry the game's per-primitive limit. `terraincut` deliberately
# does not: it IS the ground where the heightfield is punched, and the game never distance-culls
# terrain. `skirts` does not either — a building foundation has no counterpart in CS2 at all.
MARKED_PRODUCTS = ("markings", "lanegeometry", "areas")
UNMARKED_PRODUCTS = ("skirts", "terraincut")

NULL_RE = re.compile(r"^nulldetail(\d+)$")
DETAIL_RE = re.compile(r"_a(\d+)$")
COLLADA_NS = "{http://www.collada.org/2005/11/COLLADASchema}"


def shipped_thresholds(dae_path):
    """`(null_px, [detail px, ...])` read out of a shipped `.dae`'s visual scene."""
    null_px = None
    details = []
    try:
        for _event, element in ET.iterparse(dae_path, events=("start",)):
            if not element.tag.endswith("}node"):
                continue
            name = element.get("id") or ""
            m = NULL_RE.match(name)
            if m:
                null_px = int(m.group(1))
                continue
            m = DETAIL_RE.search(name)
            if m and not name.startswith("Colmesh_"):
                details.append(int(m.group(1)))
    except (OSError, ET.ParseError) as exc:
        return None, [], repr(exc)
    return null_px, sorted(set(details), reverse=True), None


def expected_from_tile(glb_path, detail_scale, level_of_detail):
    """The game's answer for one baked tile: per-triangle limits off the primitive material
    names, the tile's own bounding-sphere radius, and the ladder those two produce."""
    nodes = glb.parse_glb(glb_path)
    if not nodes:
        return None
    lo = hi = None
    limits = []
    for node in nodes:
        tris = numpy.asarray(node["tris"], dtype=numpy.int64)
        if len(tris) == 0:
            continue
        matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
        world = numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
        lo = world.min(axis=0) if lo is None else numpy.minimum(lo, world.min(axis=0))
        hi = world.max(axis=0) if hi is None else numpy.maximum(hi, world.max(axis=0))
        for name, count in node["material_runs"]:
            _base, _variant, min_lod = split_material_name(name)
            limits.extend([min_lod] * int(count))
    if lo is None or not limits:
        return None
    radius = float(numpy.linalg.norm(hi - lo) * 0.5)
    distances = tilelod.cull_distances(limits, level_of_detail)
    ladder = tilelod.tile_levels(radius, distances, detail_scale)
    return {
        "radius": radius,
        "ladder": ladder,
        "triangles": len(limits),
        "markedTriangles": sum(1 for v in limits if v is not None),
    }


def check_product(product, bundle, level_dir, derived, detail_scale, level_of_detail, samples):
    folder = os.path.join(derived, product)
    shapes = os.path.join(level_dir, "art", "shapes", product)
    if not os.path.isdir(folder) or not os.path.isdir(shapes):
        return None
    names = sorted(f for f in os.listdir(folder) if f.endswith(".glb"))
    if samples and len(names) > samples:
        step = max(1, len(names) // samples)
        names = names[::step][:samples]
    rows = []
    for name in names:
        stem = os.path.splitext(name)[0]
        dae = os.path.join(shapes, stem + ".dae")
        if not os.path.isfile(dae):
            continue
        expected = expected_from_tile(os.path.join(folder, name), detail_scale, level_of_detail)
        if expected is None:
            continue
        null_px, details, error = shipped_thresholds(dae)
        if error:
            rows.append({"tile": stem, "error": error})
            continue
        want_null = expected["ladder"]["nullPx"]
        want_levels = len(expected["ladder"]["levels"]) or 1
        rows.append(
            {
                "tile": stem,
                "shippedNull": null_px,
                "expectedNull": want_null,
                "shippedLevels": len(details),
                "expectedLevels": want_levels,
                "gameNullMetres": expected["ladder"]["gameNullMetres"],
                "triangles": expected["triangles"],
                "marked": expected["markedTriangles"],
                "keptFar": 1.0 - (expected["ladder"].get("culledFraction") or 0.0),
            }
        )
    return rows


def summarise(product, rows):
    total = len(rows)
    if not total:
        return {"tiles": 0}
    agree_null = 0
    agree_levels = 0
    disagreements = []
    metres = []
    marked = kept = 0
    triangles = 0
    for row in rows:
        if row.get("error"):
            disagreements.append(f"{row['tile']}: {row['error']}")
            continue
        want, got = row["expectedNull"], row["shippedNull"]
        ok_null = (want is None and got is None) or (
            want is not None and got is not None and abs(want - got) <= 1
        )
        agree_null += int(ok_null)
        agree_levels += int(row["shippedLevels"] == row["expectedLevels"])
        if not ok_null:
            disagreements.append(f"{row['tile']}: nulldetail {got} but the game's rule says {want}")
        if row["gameNullMetres"] is not None:
            metres.append(row["gameNullMetres"])
        marked += row["marked"]
        triangles += row["triangles"]
        kept += row["keptFar"]
    metres.sort()
    return {
        "tiles": total,
        "nullDetailAgreement": round(agree_null / total, 6),
        "levelCountAgreement": round(agree_levels / total, 6),
        "trianglesCarryingTheGameLimit": round(marked / max(1, triangles), 4),
        "gameNullMetresMedian": round(metres[len(metres) // 2], 1) if metres else None,
        "meanTrianglesKeptAtTheFarLevel": round(kept / total, 4),
        "disagreements": disagreements[:10],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", nargs="?", default=None)
    parser.add_argument("level", nargs="?", default=None)
    parser.add_argument("--derived", default=None)
    parser.add_argument("--samples", type=int, default=200, help="tiles per product (0 = all)")
    parser.add_argument(
        "--detail-scale",
        type=float,
        default=None,
        help="lod_detail_scale (default: read the level's own build report)",
    )
    args = parser.parse_args(argv)
    if tilelod is None:
        print("cs2beamng is not importable (pip install -e .)", file=sys.stderr)
        return 2
    bundle = os.path.abspath(args.bundle or default_bundle_dir())
    if not args.level:
        print("a level directory is required", file=sys.stderr)
        return 2
    level_dir = os.path.abspath(args.level)
    derived = os.path.abspath(args.derived or os.path.join(bundle, "derived"))
    detail_scale = args.detail_scale
    if detail_scale is None:
        detail_scale = 6.0
        report = os.path.join(level_dir, "cs2beamng_report.json")
        if os.path.isfile(report):
            with open(report, encoding="utf-8") as handle:
                detail_scale = float(
                    ((json.load(handle).get("settings") or {}).get("lod_detail_scale")) or 6.0
                )
    level_of_detail = lod.DEFAULT_LEVEL_OF_DETAIL

    print(f"bundle {bundle}")
    print(f"level  {level_dir}")
    print(f"lod_detail_scale {detail_scale}, levelOfDetail {level_of_detail}\n")
    failures = 0
    out = {}
    for product in MARKED_PRODUCTS:
        rows = check_product(
            product, bundle, level_dir, derived, detail_scale, level_of_detail, args.samples or 0
        )
        if rows is None:
            print(f"{product}: not built")
            continue
        summary = summarise(product, rows)
        out[product] = summary
        print(f"{product}: {summary['tiles']} tiles sampled")
        print(
            f"  nulldetail agrees with the game's rule   {summary['nullDetailAgreement'] * 100:.2f} %"
        )
        print(
            f"  LOD level count agrees                   {summary['levelCountAgreement'] * 100:.2f} %"
        )
        print(
            f"  triangles carrying a game limit          {summary['trianglesCarryingTheGameLimit'] * 100:.2f} %"
        )
        print(f"  CS2 draws the median tile to             {summary['gameNullMetresMedian']} m")
        print(
            f"  the far level keeps                      {summary['meanTrianglesKeptAtTheFarLevel'] * 100:.1f} % of its triangles"
        )
        for line in summary["disagreements"]:
            print(f"  DISAGREE {line}")
        failures += len(summary["disagreements"])
        print()
    for product in UNMARKED_PRODUCTS:
        shapes = os.path.join(level_dir, "art", "shapes", product)
        if os.path.isdir(shapes):
            print(
                f"{product}: deliberately unlimited — {'the ground where the heightfield is punched' if product == 'terraincut' else 'a foundation skirt has no counterpart in CS2'}"
            )
    print()
    if failures:
        print(f"{failures} tile(s) disagree with the game's rule")
        return 1
    print("every sampled tile stops drawing where the game's own rule says it should")
    return 0


if __name__ == "__main__":
    sys.exit(main())
