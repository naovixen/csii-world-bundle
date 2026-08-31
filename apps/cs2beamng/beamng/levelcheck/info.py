"""`info.json`: the spawns it names exist, and the navgraph will not crash the load.

Two silent failures and one fatal one. A `spawnPoints` entry whose `objectname` does not resolve
is dropped by `core/levels.lua:531` with a log line and nothing else; a broken `preview` is worse
than silent, because `levels.lua:147` substitutes the LEVEL preview, so it ships a plausible
wrong thumbnail (A7.4). And a navgraph `map.lua` cannot build kills the LOAD, so the level is
asked that question here rather than by the game (A7.5).
"""

import json
import os

from .. import map_lua


def check_info(ctx):
    """`info.json` parses, its spawns and previews resolve, and its roads make a legal graph."""
    info_path = os.path.join(ctx.level_dir, "info.json")
    if not os.path.isfile(info_path):
        ctx.note("no info.json")
        return
    with open(info_path, encoding="utf-8") as handle:
        try:
            info = json.load(handle)
        except ValueError:
            ctx.note("info.json is not JSON")
            return
    if not info:
        return
    _check_spawns(ctx, info)
    _check_navgraph(ctx, info)


def _check_spawns(ctx, info):
    """The default spawn, and every quick-travel point's object and preview.

    `core/levels.lua:531` logs an error and drops a `spawnPoints` entry whose `objectname` does
    not resolve, so a typo costs the POI silently. Its `preview` is worse than silent —
    `levels.lua:147` substitutes the LEVEL preview through `imageExistsDefault`, so a broken path
    ships a plausible wrong thumbnail rather than an error (A7.4).
    """
    spawn_names = {o["name"] for o in ctx.by_class.get("SpawnSphere", [])}
    default = info.get("defaultSpawnPointName")
    if default and default not in spawn_names:
        ctx.note(f"info.json defaultSpawnPointName {default!r} is not a SpawnSphere")
    points = info.get("spawnPoints") or []
    ctx.checks["spawnPoints"] = len(points)
    for entry in points if isinstance(points, list) else []:
        if not isinstance(entry, dict):
            ctx.note("info.json spawnPoints holds a non-object entry")
            continue
        name = entry.get("objectname")
        if name is None:
            continue  # levels.lua's own auto-inserted default carries none
        if name not in spawn_names:
            ctx.note(
                f"info.json spawnPoints objectname {name!r} is not a SpawnSphere - "
                f"the quick-travel point is dropped with a log line and nothing else"
            )
        preview = entry.get("preview")
        if preview and not os.path.isfile(os.path.join(ctx.level_dir, *str(preview).split("/"))):
            ctx.note(
                f"info.json spawnPoints preview {preview!r} does not exist - BeamNG "
                f"silently substitutes the level preview for it"
            )


def _check_navgraph(ctx, info):
    """The navgraph is what the minimap, the route line and the ground markers all read."""
    roads = ctx.by_class.get("DecalRoad", [])
    drivable = [r for r in roads if float(r.get("drivability") or 0) > 0]
    ctx.checks["navgraph"] = {
        "decalRoads": len(roads),
        "drivable": len(drivable),
        "nodes": sum(len(r.get("nodes") or ()) for r in drivable),
    }
    if not drivable:
        ctx.note(
            "no DecalRoad with drivability > 0: `map.getMap()` will be EMPTY, so the "
            "minimap draws nothing, a waypoint produces no route and the ground-marker "
            "arrows never appear (audit A7.0). Build with `ai_roads true`"
        )
        return
    # ...and having roads is not the same as having a graph map.lua can build. It crashes the
    # LOAD on a configuration the builder must never emit (audit A7.5), so the level is asked
    # the question here rather than the game.
    rules = info.get("roadRules") if isinstance(info.get("roadRules"), dict) else {}
    map_stats, risks = map_lua.check(drivable, right_hand_drive=bool(rules.get("rightHandDrive")))
    ctx.checks["navgraph"].update(map_stats)
    if risks:
        example = risks[0]
        # FATAL: the stage's own changelog promised the build FAILS on this class since v6,
        # and it only ever noted it - a level with a map.lua:459 crash shipped as `done` (E3)
        ctx.fatal(
            f"{len(risks)} navgraph node{'s' if len(risks) != 1 else ''} take"
            f"{'' if len(risks) != 1 else 's'} map.lua's optimiser into its "
            f"unguarded lane compare (map.lua:459): the load dies with "
            f"'attempt to compare nil with string' and the level never opens. "
            f"First is {example['node']} between {example['between'][0]} and "
            f"{example['between'][1]} - road {example['road']}, chord "
            f"{example['chord']}"
        )
    elif map_stats["chords"]:
        ctx.note(
            f"{map_stats['chords']} navgraph nodes sit on a triangle (a degree-2 "
            f"node whose two neighbours are already linked). None of them ties "
            f"map.lua's ten edge fields today, so none crashes the load, but the "
            f"emitted graph is meant to be simple and this says it is not"
        )
