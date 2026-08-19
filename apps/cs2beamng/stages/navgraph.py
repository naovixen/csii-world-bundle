"""Stage `navgraph` — the AI road network, as invisible `DecalRoad`s, and the level's road rules.

**This is what makes the minimap draw, a waypoint route and the ground-marker arrows appear.**
All three read one structure — `map.getMap().nodes`, BeamNG's AI navgraph — and it is built from
exactly three things (`lua/ge/map.lua:567–915`, `loadJsonDecalMap`):

* `scenetree.findClassObjects('BeamNGWaypoint')`;
* `scenetree.findClassObjects('DecalRoad')`, kept where `drivability > 0`;
* `<levelDir>/map.json`, whose segments must name waypoints that already exist as scene objects.

Our roads are `TSStatic` meshes in `Networks/`. `map.lua` has never looked at a TSStatic, so
before this stage the graph was empty and `ui/apps/minimap/roads.lua:17` had nothing to draw —
the minimap app draws roads as VECTORS FROM THE NAVGRAPH, not from an image, which is why no
amount of texture work would have fixed it (audit A7.0).

## The rule, and it is vanilla's

`west_coast_usa` ships 12,752 `DecalRoad`s, of which 1,299 are pure AI helpers on the level-local
material `road_invisible` — a transparent DDS with `alphaTest`/`alphaRef 127`/`castShadows false`
— mostly under `MissionGroup/AIWaypointsGroup/`. So a navgraph of a few thousand invisible roads
is not a deviation; it is what a BeamNG level is. Every field this stage writes is copied from
one of those roads, and four readings of `map.lua` decide the rest:

1. **`useSubdivisions: false` makes our polyline the navgraph 1:1.** In that branch
   (map.lua:721) each node becomes one map node at `getNodePosition(i)` with
   `radius = getNodeWidth(i) * 0.5`. With subdivisions on, the engine instead resamples the
   road's EDGE geometry and Douglas-Peuckers it, which we do not want to be at the mercy of.
2. **`speedLimit` is m/s, not km/h** (`createSpeedLimits` uses `baseValue = 19.444 -- 70 km/h`,
   and vanilla roads carry `"speedLimit":"11.18"` = 25 mph). CS2's `attrs.limit` is km/h, so it
   is divided by 3.6. `<= 0` means "auto-calculate" and is therefore never written.
3. **`autoLanes: false` + `lanesLeft`/`lanesRight` give the lane string**, and BeamNG derives
   `oneWay` from it and applies `rules.rightHandDrive` itself (map.lua:626). So the counts go out
   and the engine chooses the side.
4. **The node merge is 3-D and radius-sized.** `mergeOverlappingNodes` (map.lua:1188)
   broad-phases in 2-D but tests `squaredDistance < square(max(r1, r2))` in 3-D, so a 24 m road's
   node reaches 12 m downwards and a bridge deck can weld into the street beneath it. That is why
   `west_coast_usa`'s overpasses are `autoJunction: false`, and why every road this stage emits
   from an `elevated` or `tunnel` segment carries it too. `navgraph_max_width` clamps the radius
   on top of that; `tools/check_navgraph_vs_game.py` counts what would still weld.

## The road rules

`map.lua:148` reads `info.json`'s `roadRules` — and `rightHandDrive` there means the STEERING
WHEEL is on the right, i.e. traffic drives on the LEFT (map.lua:630 puts the `+` forward lanes on
the left of the string when it is set, and `automation_test_track` is the one vanilla level that
sets it). CS2's `Instance.City.leftHandTraffic` is the same bit, and A7 W3 adds it to
`bundle.json` — so on a rip that carries it, it is read. On one that does not, it is **measured**
rather than assumed: the lanes running with a two-way segment sit to one side of the ones running
against it, and `csiibundle.navgraph` calibrates which side that is against CS2's own
`TurnRight`/`TurnLeft` lane flags. The vote goes in the report either way (5,735 to 1 on the
reference city, so this is not a close call being papered over).
"""

import os
import time

import numpy

from csiibundle import navgraph as navgraph_mod, reader

from ..beamng import frame
from . import Stage, level_of

STAGE_KEY = "navgraph"
GROUP = "AIWaypointsGroup"          # vanilla's own name for it
MATERIAL = "road_invisible"

# Copied verbatim from `west_coast_usa`'s AI-helper roads. Nothing here is tuned by us: they are
# the fields a vanilla navgraph road carries, and `useSubdivisions` is the load-bearing one.
VANILLA_FIELDS = {
    "improvedSpline": True,
    "useSubdivisions": False,
    "autoLanes": False,
    "renderPriority": 1,
    "detail": 0.2,
    "distanceFade": [200, 100],
    "startEndFade": [20, 20],
    "textureLength": 100,
    "hiddenInNavi": False,
}

# `attrs.category` -> BeamNG `drivability`. 1.0 is a full road; the AI prefers a higher value.
# Only what a car drives on is in the graph at all; `pathway`, `tram`, `train`, `waterway`,
# `cable` and `pipe` are not roads and never enter it.
DRIVABILITY = {"road": 1.0, "highway": 1.0, "taxiway": 0.3}


def enabled(settings):
    return bool(settings.get("ai_roads", True))


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("lanes") or 1)


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    categories = list(navgraph_mod.DEFAULT_CATEGORIES)
    if settings.get("navgraph_taxiways"):
        categories.extend(navgraph_mod.OPTIONAL_CATEGORIES)
    max_width = float(settings.get("navgraph_max_width") or 0) or None

    progress.begin("navgraph: build", total=units(ctx.census, settings), unit="lanes")
    graph = navgraph_mod.build(ctx.bundle, categories=tuple(categories), max_width=max_width,
                               progress=progress)
    for note in graph.notes:                # every approximation is counted where it is made
        progress.log(note)
    progress.end(f"{graph.stats['roads']:,} roads, {graph.stats['nodes']:,} nodes, "
                 f"{graph.stats['components']} components "
                 f"(largest {graph.stats['largestComponentFraction']:.1%})")

    # -- the material the roads bind ---------------------------------------------------------
    progress.begin("navgraph: material", total=None, unit="")
    _write_invisible_material(level)
    progress.end(f"art/road/main.materials.json + {MATERIAL}")

    # -- the roads ---------------------------------------------------------------------------
    progress.begin("navgraph: roads", total=len(graph.roads), unit="roads")
    region = ctx.region
    emitted = 0
    dropped = 0
    for i, road in enumerate(graph.roads):
        if i % 2000 == 0:
            progress.advance(2000)
            progress.check()
        nodes = [list(frame.to_beamng(x, y, z)) + [round(float(w), 3)] for x, y, z, w in road.nodes]
        if region is not None and not _in_region(road, region):
            dropped += 1
            continue
        obj = dict(VANILLA_FIELDS)
        obj.update({
            "name": road.key, "class": "DecalRoad", "material": MATERIAL,
            "position": [round(v, 4) for v in nodes[0][:3]],
            "nodes": [[round(v, 4) for v in n] for n in nodes],
            "drivability": DRIVABILITY.get(road.category, 1.0),
            "oneWay": bool(road.oneway),
            "lanesLeft": int(road.lanes_left),
            "lanesRight": int(road.lanes_right),
            # reading (4): an elevated road or a tunnel must not have its INTERIOR nodes welded
            # into whatever it crosses. Its two end nodes still merge exactly, which is what
            # keeps it attached to its own ramps.
            "autoJunction": not road.elevated,
        })
        # A DYNAMIC FIELD, not a DecalRoad property: Torque's `SimObject::setDataField` keeps
        # anything it does not recognise as one, so this rides along without the engine caring.
        # It is CS2's own street name for this road, and it is what the `poi` stage names a
        # derived fast-travel point after - the alternative was a sidecar file for one string.
        if road.name:
            obj["cs2Street"] = road.name
        if road.speed_kph:
            obj["speedLimit"] = round(float(road.speed_kph) / 3.6, 3)     # km/h -> m/s
        level.add([GROUP], obj)
        emitted += 1
    progress.advance(len(graph.roads) % 2000)

    # -- the road rules ----------------------------------------------------------------------
    left_hand, source = _traffic_side(ctx.bundle, graph)
    _patch_info(level, {"roadRules": {"rightHandDrive": bool(left_hand)}})
    level.write_objects()
    progress.end(f"{emitted:,} DecalRoads in {GROUP}"
                 + (f", {dropped:,} outside the region" if dropped else ""))
    progress.log(f"navgraph: traffic drives on the {'LEFT' if left_hand else 'RIGHT'} "
                 f"({source}) - info.json roadRules.rightHandDrive = {bool(left_hand)}")

    shared = ctx.shared if ctx.shared is not None else {}
    shared["navgraph"] = {"roads": emitted, "nodes": sum(len(r.nodes) for r in graph.roads),
                          "stats": graph.stats}
    return {"roads": emitted, "droppedOutsideRegion": dropped, "leftHandTraffic": bool(left_hand),
            "trafficSideSource": source, "stats": graph.stats, "notes": graph.notes,
            "seconds": round(time.monotonic() - started, 1)}


def _in_region(road, region):
    """A road is kept when any node is inside the build region (bundle-frame X/Z circle)."""
    cx, cz, r = region["centerX"], region["centerZ"], region["radius"]
    for x, _y, z, _w in road.nodes:
        if (x - cx) ** 2 + (z - cz) ** 2 <= r * r:
            return True
    return False


def _write_invisible_material(level):
    """`art/road/main.materials.json` + a 4x4 transparent PNG, exactly vanilla's shape.

    `alphaTest` with `alphaRef 127` is what makes the road draw nothing at all rather than a
    transparent quad that still costs a blend, and `castShadows: false` keeps 17,000 invisible
    ribbons out of the shadow pass. Both are copied from `west_coast_usa`'s own `road_invisible`.
    """
    from csiibundle import png

    texture = "art/road/invisible.png"
    level.write_bytes_if_changed(
        png.encode_rgba(numpy.zeros((4, 4, 4), dtype=numpy.uint8)), *texture.split("/"))
    level.write_materials("road", {MATERIAL: {
        "name": MATERIAL, "mapTo": MATERIAL, "class": "Material",
        "persistentId": level.pid("material:" + MATERIAL),
        "Stages": [{"colorMap": level.game_path(texture).lstrip("/"), "useAnisotropic": True},
                   {"useAnisotropic": None}, {"useAnisotropic": None}, {"useAnisotropic": None}],
        "alphaRef": 127, "alphaTest": True, "annotation": "STREET", "castShadows": False,
        "materialTag0": "RoadAndPath", "materialTag1": "beamng", "specularStrength0": "0",
    }})


def _traffic_side(bundle_dir, graph):
    """`(left_hand, source)` — which side traffic drives on.

    The producer reads `Instance.City.leftHandTraffic` (`Systems/NetworkSystem.cs:896`) and A7 W3
    puts it in `bundle.json`; where a rip carries it, that is the answer and nothing is inferred.

    Where it does not, `csiibundle.navgraph` MEASURES it — the lanes running with a two-way
    segment sit to one side of the ones running against it, and which side that is IS the rule.
    Its sign convention is calibrated against CS2's own `TurnRight`/`TurnLeft` lane flags rather
    than assumed. On the reference city the vote is 5,735 to 1, so this is not a close call that
    a consumer is papering over; the counts go in the report either way.
    """
    manifest = reader.load_manifest(bundle_dir) or {}
    city = manifest.get("city") if isinstance(manifest.get("city"), dict) else {}
    for holder, label in ((manifest, "bundle.json"), (city, "bundle.json city")):
        if "leftHandTraffic" in holder:
            return bool(holder["leftHandTraffic"]), f"{label} leftHandTraffic"
    stats = graph.stats
    right, left = stats.get("forwardLanesRight", 0), stats.get("forwardLanesLeft", 0)
    if right + left == 0:
        return False, "no bundle field and no two-way segment to measure; assumed right-hand"
    return bool(stats.get("leftHandTraffic")), (
        f"MEASURED from the lane layer, {right:,} two-way segments put the with-flow lanes right "
        f"and {left:,} put them left - the producer does not ship leftHandTraffic on this rip")


def _patch_info(level, extra):
    """Merge `extra` into the level's existing `info.json` rather than rewriting it.

    `scene` writes that file first and owns most of it; this stage and `poi` only add fields to
    it, so a read-modify-write is the honest operation. A missing file is not created here — a
    build without `scene` has bigger problems than its road rules.
    """
    import json

    path = level.path("info.json")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as handle:
        try:
            info = json.load(handle)
        except ValueError:
            return False
    info.update(extra)
    level.write_info(info)
    return True


INPUTS = ("layer:lanes", "layer:networks")
SETTINGS = ("ai_roads", "navgraph_max_width", "navgraph_taxiways", "region")
OUTPUTS = ("art/road/main.materials.json",)

STAGE = Stage(STAGE_KEY, "AI navgraph (minimap and routing)", enabled, units, run, inputs=INPUTS,
              settings_keys=SETTINGS, outputs=OUTPUTS, shared_keys=("navgraph",),
              after=("scene",), version=1)
