"""Stage `poi` — fast-travel points: the big map's "Quicktravel Points" group.

The contract is exact and we already emitted half of it. `core/levels.lua:531`
(`onGetRawPoiListForLevel`) walks `info.json`'s `spawnPoints`, and for every entry whose
`objectname` `scenetree.findObject` resolves it emits a POI with
`icon = "poi_fasttravel_round_orange"` and `quickTravelPosRotFunction = getSpawnPointPosRot`;
`freeroam/bigMapPoiProvider.lua` groups those under **`type_spawnPoint = "Quicktravel Points"`**.
So the whole feature is data a level ships — no new file format and no Lua — and the level
already shipped ONE such point, the spawn.

`west_coast_usa` ships nine:

    {"translationId": "levels.west_coast_usa.spawnpoints.industrial",
     "description": "...", "objectname": "spawns_industrial", "preview": "spawns_industrial.jpg"}

`objectname` must name a real `SpawnSphere`, and the quick-travel rotation is
`quat(0,0,1,0) * spawnPoint:getRotation()` — the same convention `frame.heading_matrix` already
encodes for `spawn_city`. **`preview` is not validated by the engine**: `levels.lua:147` runs it
through `imageExistsDefault` and silently substitutes the level preview, so a broken path shows
as a plausible wrong thumbnail rather than an error. `validate_level` checks it for that reason.

## Two tiers, and the first one is the answer to the actual request

**Tier 1 — a user-authored list (`poi_file`).** "The keys bottom-left", "the east city beach",
"the town left of the lake" are Naomi's names for her own city and no algorithm will produce
them. The file is a small JSON list; ten of these is ten lines and it is the whole feature:

    [{"name": "Vice City Beach", "at": [4820, 1660], "description": "East city beach"}]

`at` is **BeamNG-frame X/Y** by default — what you can read off the big map — and a
`"frame": "bundle"` on the entry (or on a wrapping object) switches it to bundle X/Z. A third
element is accepted and ignored: the point is snapped onto the road network either way.

**Tier 2 — a derived default set**, so a fresh build is not empty. The navgraph this level
already shipped is gridded, the densest cells are taken with a minimum separation between them,
and each is named by the **most common street name** among the roads in it (6,732 of the
reference city's segments carry `attrs.name`). Capped at `poi_max` — vanilla ships 3 to 9 —
because a big map with forty markers is not a feature.

**Tier 3 — CS2's own districts**, which is the faithful answer and the one the game agrees with.
A district is a polygon the player drew and a label the player typed, and it is the only PLACE
name a CS2 city contains. The producer now ships them as `districts.jsonl` (contract §8d), so
where a rip carries that layer its names are used ahead of anything derived. **A bundle ripped
before 2026-08-19 does not have it**, and this stage then falls through to tier 2 and says so —
it is a producer change, and no result from an older bundle can stand in for one.

The three tiers compose: authored points first, then one per district, then derived cells to fill
up to `poi_max`. See `docs/plans/a7-navigation.md` A7.4.

**The snap is a placement rule for new points and NOT a correction to the existing spawn.**
A7.3 records that the "spawn inside a building" report was the building rotation (§78), not the
spawn picker, and the picker is deliberately left alone.
"""

import itertools
import json
import math
import os
import re
import time

import numpy
from csiibundle import reader

from ..beamng import frame
from ..beamng.level import read_objects
from . import Stage, level_of

STAGE_KEY = "poi"
SPAWN_LIFT = 0.5
# Vanilla ships 3 (`italy`) to 9 (`west_coast_usa`) quick-travel points. A derived set is capped
# on the same scale: the big map is a menu, not an index.
DERIVED_CELL = 512.0  # metres; the grid the derived seeds are picked on
DERIVED_SEPARATION = 1800.0  # metres between two derived points
SLUG_RE = re.compile(r"[^a-z0-9]+")


def enabled(settings):
    return bool(settings.get("pois", True))


def units(census, settings):
    return None


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings

    progress.begin("poi: road network", total=None, unit="")
    network = _road_network(ctx)
    progress.end(
        f"{len(network.points):,} road points"
        + ("" if len(network.points) else " - nothing to snap to")
    )
    if not len(network.points):
        return {"points": 0, "note": "no road network to place points on", "seconds": 0.0}

    cap = int(settings.get("poi_max") or 8)
    wanted = []
    authored = _read_poi_file(settings.get("poi_file"), progress)
    wanted.extend(authored)
    districts = []
    if len(wanted) < cap:
        districts = _read_districts(ctx.bundle, progress)[: cap - len(wanted)]
        wanted.extend(districts)
    derived = []
    if len(wanted) < cap:
        derived = _derive(network, cap - len(wanted), [p["at_beamng"] for p in wanted])
        wanted.extend(derived)

    progress.begin("poi: place", total=len(wanted) or None, unit="points")
    existing = {
        obj["name"]
        for _rel, _n, obj in read_objects(level.dir)
        if obj and obj.get("class") == "SpawnSphere"
    }
    entries = []
    placed = 0
    used = set(existing)
    for point in wanted:
        snapped = network.snap(point["at_beamng"])
        if snapped is None:
            continue
        position, heading = snapped
        name = _unique(f"spawn_{_slug(point['name'])}", used)
        level.add(
            ["PlayerDropPoints"],
            {
                "name": name,
                "class": "SpawnSphere",
                "position": [round(v, 3) for v in position],
                "rotationMatrix": [round(v, 6) for v in frame.heading_matrix(*heading)],
                "autoplaceOnSpawn": "0",
                "dataBlock": "SpawnSphereMarker",
                "enabled": "1",
                "homingCount": "0",
                "indoorWeight": "1",
                "isAIControlled": "0",
                "lockCount": "0",
                "outdoorWeight": "1",
                "radius": 5,
                "sphereWeight": "1",
            },
        )
        entry = {"translationId": point["name"], "objectname": name}
        if point.get("description"):
            entry["description"] = point["description"]
        entries.append(entry)
        placed += 1
        progress.advance(1)
    level.write_objects()
    progress.end(
        f"{placed} quick-travel point(s): {len(authored)} authored, "
        f"{len(districts)} from CS2 districts, {len(derived)} derived"
    )

    added, replaced = _patch_info(level, entries)
    if replaced:
        progress.log(
            f"poi: replaced {replaced} quick-travel entr(y/ies) left in info.json by an "
            f"earlier build - their SpawnSpheres are not in this level"
        )
    for entry in entries:
        progress.log(f"poi: {entry['translationId']!r} -> {entry['objectname']}")
    if not authored and not districts and derived:
        progress.log(
            "poi: every point is DERIVED (densest road cells, named by their commonest "
            "street) - this bundle ships no districts.jsonl (contract §8d, producer "
            "2026-08-19), so CS2's own place names are not available to it. Point "
            "`poi_file` at a JSON list to name your own, or re-rip"
        )
    return {
        "points": placed,
        "authored": len(authored),
        "districts": len(districts),
        "derived": len(derived),
        "infoSpawnPoints": added,
        "names": [e["translationId"] for e in entries],
        "seconds": round(time.monotonic() - started, 1),
    }


# -- the road network to snap onto ---------------------------------------------------------------


class _Network:
    """The drivable points a POI may be placed on, with their local heading.

    Read back out of the LEVEL's own `DecalRoad`s where the `navgraph` stage put them — the thing
    that shipped, not the thing the build meant to ship (audit §80). Where there are none it
    falls back to the bundle's own `car` lanes, so `pois` works with `ai_roads` off.
    """

    def __init__(self, points, headings):
        self.points = numpy.asarray(points, dtype=numpy.float64).reshape(-1, 3)
        self.headings = numpy.asarray(headings, dtype=numpy.float64).reshape(-1, 2)
        self._tree = None

    def snap(self, xy):
        if not len(self.points):
            return None
        if self._tree is None:
            from scipy.spatial import cKDTree

            self._tree = cKDTree(self.points[:, :2])
        _d, i = self._tree.query(numpy.asarray(xy, dtype=numpy.float64)[:2])
        p = self.points[int(i)]
        return (float(p[0]), float(p[1]), float(p[2]) + SPAWN_LIFT), tuple(self.headings[int(i)])


def _road_network(ctx):
    points, headings, names = [], [], []
    for _rel, _n, obj in read_objects(level_dir_of(ctx)):
        if not obj or obj.get("class") != "DecalRoad":
            continue
        nodes = obj.get("nodes") or []
        for a, b in itertools.pairwise(nodes):
            points.append((0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]), 0.5 * (a[2] + b[2])))
            headings.append((b[0] - a[0], b[1] - a[1]))
            # the CS2 street name the `navgraph` stage rode along on the road, NOT the object's
            # own `seg_<id>` name - a fast-travel point called "seg_2331850" is worse than none
            names.append(obj.get("cs2Street"))
    if points:
        network = _Network(points, headings)
        network.labels = names
        return network
    return _lane_network(ctx)


def _lane_network(ctx):
    """The fallback: `car` lanes owned by a `road`/`highway` segment, straight from the bundle."""
    from csiibundle import navgraph as navgraph_mod

    segments = {}
    path = reader.layer_path(ctx.bundle, "networks")
    if path:
        for record in reader.read_jsonl(path):
            a = record.get("attrs") or {}
            if a.get("category") in navgraph_mod.DEFAULT_CATEGORIES:
                segments[record["id"]] = a.get("name")
    points, headings, names = [], [], []
    lanes_path = reader.layer_path(ctx.bundle, "lanes")
    if lanes_path and segments:
        for lane in reader.read_jsonl(lanes_path):
            if lane.get("kind") != "car" or lane.get("owner") not in segments:
                continue
            if "SideConnection" in (lane.get("flags") or ()):
                continue
            p = lane.get("path") or []
            if len(p) < 2:
                continue
            i = (len(p) - 1) // 2
            a, b = p[i], p[i + 1]
            ax, ay, az = frame.to_beamng(*a)
            bx, by, _bz = frame.to_beamng(*b)
            points.append((0.5 * (ax + bx), 0.5 * (ay + by), az))
            headings.append((bx - ax, by - ay))
            names.append(segments[lane["owner"]])
    network = _Network(points, headings)
    network.labels = names
    return network


def level_dir_of(ctx):
    return ctx.level_dir


# -- tier 1: the authored list -------------------------------------------------------------------


def _read_poi_file(path, progress):
    """`[{name, at_beamng, description}]` from the user's JSON list, or `[]`."""
    if not path:
        return []
    if not os.path.isfile(path):
        progress.log(f"poi: poi_file {path!r} does not exist - no authored points")
        return []
    with open(path, encoding="utf-8") as handle:
        try:
            doc = json.load(handle)
        except ValueError as exc:
            progress.log(f"poi: poi_file {path!r} is not JSON ({exc}) - no authored points")
            return []
    default_frame = "beamng"
    if isinstance(doc, dict):
        default_frame = str(doc.get("frame") or default_frame)
        doc = doc.get("points") or doc.get("pois") or []
    out = []
    for entry in doc if isinstance(doc, list) else []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("at"):
            progress.log(f"poi: skipping malformed entry {entry!r} (needs `name` and `at`)")
            continue
        at = [float(v) for v in entry["at"]]
        which = str(entry.get("frame") or default_frame).lower()
        if which.startswith("bundle"):
            # bundle X/Z (east/north) -> BeamNG X/Y; the height is irrelevant, the snap sets it
            at = [at[0], at[-1] if len(at) > 2 else at[1]]
        out.append(
            {
                "name": str(entry["name"]),
                "at_beamng": (at[0], at[1]),
                "description": entry.get("description"),
            }
        )
    if out:
        progress.log(f"poi: {len(out)} authored point(s) from {path}")
    return out


# -- tier 3: CS2's own districts -----------------------------------------------------------------


def _read_districts(bundle_dir, progress):
    """`[{name, at_beamng, description}]` from `districts.jsonl` (§8d), largest district first.

    The centroid the producer ships is the polygon's NODE mean and is inside the district for any
    convex-ish shape; the snap moves it onto the road network anyway, so a concave district ends
    up on the nearest road to its middle rather than outside itself. Districts with no label are
    skipped — an unnamed marker on the big map is worse than no marker.
    """
    path = reader.layer_path(bundle_dir, "districts")
    if not path or not os.path.isfile(path):
        return []
    out = []
    unnamed = 0
    for record in reader.read_jsonl(path):
        name = (record.get("name") or "").strip()
        centroid = record.get("centroid")
        if not centroid or len(centroid) < 3:
            continue
        if not name:
            unnamed += 1
            continue
        x, y, _z = frame.to_beamng(centroid[0], centroid[1], centroid[2])
        out.append(
            {
                "name": name,
                "at_beamng": (x, y),
                "area": len(record.get("nodes") or ()),
                "description": f"{name} district",
            }
        )
    out.sort(key=lambda p: -p["area"])
    if out or unnamed:
        progress.log(
            f"poi: {len(out)} named CS2 district(s) from districts.jsonl"
            + (f", {unnamed} unnamed skipped" if unnamed else "")
        )
    return out


# -- tier 2: the derived set ---------------------------------------------------------------------


def _derive(network, want, avoid):
    """The densest road cells, at least `DERIVED_SEPARATION` apart, named by their streets."""
    if want <= 0 or not len(network.points):
        return []
    labels = getattr(network, "labels", None) or [None] * len(network.points)
    cells = {}
    for i, point in enumerate(network.points):
        key = (int(math.floor(point[0] / DERIVED_CELL)), int(math.floor(point[1] / DERIVED_CELL)))
        bucket = cells.setdefault(key, {"n": 0, "sum": numpy.zeros(2), "names": {}})
        bucket["n"] += 1
        bucket["sum"] += point[:2]
        name = labels[i]
        if name:
            bucket["names"][name] = bucket["names"].get(name, 0) + 1
    ordered = sorted(cells.values(), key=lambda b: -b["n"])
    taken = [numpy.asarray(a, dtype=numpy.float64) for a in avoid]
    out = []
    for bucket in ordered:
        if len(out) >= want:
            break
        centre = bucket["sum"] / bucket["n"]
        if any(float(numpy.linalg.norm(centre - t)) < DERIVED_SEPARATION for t in taken):
            continue
        if not bucket["names"]:
            continue
        name = max(bucket["names"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        taken.append(centre)
        out.append(
            {
                "name": name,
                "at_beamng": (float(centre[0]), float(centre[1])),
                "description": f"{bucket['n']:,} road segments around {name}",
            }
        )
    return out


# -- info.json -----------------------------------------------------------------------------------


def _patch_info(level, entries):
    """REPLACE the quick-travel entries in `info.json`, keeping the `scene` stage's own spawn.

    Appending was wrong and the level caught it on the first real build: the `scene` stage
    rewrites `info.json` whole when it runs, but a build where it is SKIPPED leaves the previous
    build's file in place — so this stage appended a second set beside the first, and the six
    `SpawnSphere`s behind the older set were gone (the object tree is rewritten per build, so a
    stage's objects are only what THIS run added). `levels.lua:544` logs an error and drops such
    an entry, which is silent in the big map.

    This stage owns every `spawnPoints` entry except the default spawn, so the list is rebuilt
    rather than extended: the entry naming `defaultSpawnPointName` survives, everything else is
    replaced by what this run actually placed. That makes a stale entry impossible rather than
    detectable, and it is self-healing on a level built by an older version.
    """
    path = level.path("info.json")
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        try:
            info = json.load(handle)
        except ValueError:
            return 0
    default = info.get("defaultSpawnPointName")
    kept = [
        e
        for e in (info.get("spawnPoints") or [])
        if isinstance(e, dict) and e.get("objectname") and e["objectname"] == default
    ]
    dropped = len(info.get("spawnPoints") or []) - len(kept)
    info["spawnPoints"] = kept + [e for e in entries if e["objectname"] != default]
    level.write_info(info)
    return len(info["spawnPoints"]) - len(kept), dropped


def _slug(text):
    return SLUG_RE.sub("_", str(text).lower()).strip("_") or "point"


def _unique(name, used):
    candidate = name
    n = 2
    while candidate in used:
        candidate = f"{name}_{n}"
        n += 1
    used.add(candidate)
    return candidate


# NOT `level:`. This stage reads the DecalRoads the `navgraph` stage wrote, but it also WRITES to
# the level (its spawn spheres and `info.json`), so a level fingerprint taken before it ran never
# matches the one taken after and the stage could never be skipped. `after` is the right
# mechanism and carries the navgraph stage's own signature one hop, which is exactly the
# dependency: if the roads changed, this re-runs.
INPUTS = ("layer:lanes", "layer:networks")
SETTINGS = ("pois", "poi_file", "poi_max", "ai_roads", "region")
OUTPUTS = ("info.json",)

# version 2: `info.json`'s spawnPoints are REPLACED, not appended to. Appending left six entries
# naming SpawnSpheres a later build no longer placed, which `levels.lua` drops with a log line and
# nothing visible - caught by this repo's own level validator on the first real build.
STAGE = Stage(
    STAGE_KEY,
    "Fast-travel points",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    after=("scene", "navgraph"),
    version=2,
)
