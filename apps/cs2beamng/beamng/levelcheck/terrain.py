"""The terrain: the `.ter` agrees with its sidecar, and the ground is where the bundle says.

The physical check is the one that catches a wrong orientation, a wrong origin or a wrong scale
in one go — a flipped terrain is metres off everywhere. It asks whether the `.ter` height under
sampled `OnGround` buildings equals `instances.ground_level` (`pos.y - attrs.elevation`) within
`GROUND_TOLERANCE`, which is sized to cover the roadbed sink and the shallow-band clamp.
"""

import json
import os

import numpy
from csiibundle import instances as instances_mod
from csiibundle import reader

from .. import ter as ter_mod

GROUND_TOLERANCE = 0.5
GROUND_SAMPLES = 3000
GROUND_PASS_FRACTION = 0.97
GROUND_MIN_SAMPLES = 50  # fewer buildings than this say nothing (the example bundle has 2)


def check_terrain(ctx):
    """The `.ter` reads, its layer indices have materials, and `.terrain.json` agrees."""
    # -- terrain ---------------------------------------------------------------------------
    block = (ctx.by_class.get("TerrainBlock") or [None])[0]
    if block is not None:
        path = ctx.resolve(block.get("terrainFile"))
        sidecar = os.path.join(ctx.level_dir, f"{ctx.level_name}.terrain.json")
        if path and os.path.isfile(path):
            try:
                size, heights, layers, names = ter_mod.read_ter(path)
                ctx.terrain = (size, heights, layers, names, block)
                ctx.checks["ter"] = {
                    "size": size,
                    "materials": names,
                    "holes": int((layers == ter_mod.HOLE).sum()),
                    "minStored": int(heights.min()),
                    "maxStored": int(heights.max()),
                }
                bad = numpy.unique(layers[(layers != ter_mod.HOLE) & (layers >= len(names))])
                if bad.size:
                    ctx.note(f".ter layer indices without a material: {bad.tolist()}")
                if os.path.isfile(sidecar):
                    with open(sidecar, encoding="utf-8") as handle:
                        side = json.load(handle)
                    if side.get("size") != size or side.get("materials") != names:
                        ctx.note(".terrain.json disagrees with the .ter (size or materials)")
                else:
                    ctx.note("no .terrain.json beside the .ter")
                if not block.get("maxHeight") or "position" not in block:
                    ctx.note("TerrainBlock lacks maxHeight/position")
            except (ValueError, OSError) as exc:
                ctx.note(f".ter unreadable: {exc}")


def check_ground(ctx):
    """The terrain under sampled OnGround buildings is where the bundle says it is."""
    # -- physical: terrain under OnGround buildings -----------------------------------------
    if ctx.terrain is not None and ctx.bundle_dir:
        result = _ground_check(ctx.terrain, ctx.bundle_dir, ctx.progress)
        ctx.checks["groundCheck"] = result
        if (
            result.get("sampled", 0) >= GROUND_MIN_SAMPLES
            and result["fraction"] < GROUND_PASS_FRACTION
        ):
            ctx.note(
                f"terrain under OnGround buildings: only {result['fraction']:.1%} within {GROUND_TOLERANCE} m "
                f"(median |dz| {result['medianAbs']:.2f} m over {result['sampled']} buildings) - orientation/origin/scale?"
            )


def _ground_check(terrain, bundle_dir, progress):
    _size, heights, _layers, _names, block = terrain
    position = [float(v) for v in block["position"]]
    square = float(block.get("squareSize", 1.0))
    max_height = float(block["maxHeight"])
    path = reader.layer_path(bundle_dir, "instances")
    if not path or not os.path.isfile(path):
        return {"sampled": 0}
    dz = []
    seen = 0
    for record in reader.read_jsonl(path):
        if record.get("kind") != "building":
            continue
        attrs = record.get("attrs") or {}
        flags = attrs.get("elevationFlags")
        if flags and list(flags) != ["OnGround"]:
            continue
        ground = instances_mod.ground_level(record)
        if ground is None:
            if flags:  # off the ground for a stated reason, no elevation given: skip
                continue
            ground = float(
                record["pos"][1]
            )  # §4.2: no elevation = sitting flat on the ground at pos
        seen += 1
        if seen > 200 and seen % 7 != 0:  # past the first 200: every 7th, spread over the file
            continue
        x, _y, z = record["pos"]
        h = ter_mod.sample_height(heights, max_height, position, square, float(x), float(z))
        if h is None:
            continue
        dz.append(h - ground)
        if len(dz) >= GROUND_SAMPLES:
            break
    if not dz:
        return {"sampled": 0}
    arr = numpy.abs(numpy.asarray(dz))
    return {
        # Which height this was measured against — see `ter.sample_height` for the deviation
        # from the triangulation BeamNG actually renders, and why it is under the tolerance.
        "sampler": "bilinear",
        "sampled": int(arr.size),
        "fraction": float((arr <= GROUND_TOLERANCE).mean()),
        "medianAbs": float(numpy.median(arr)),
        "p95Abs": float(numpy.percentile(arr, 95)),
        "meanSigned": float(numpy.mean(dz)),
    }
