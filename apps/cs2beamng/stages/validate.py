"""Stage `validate` — check the level folder and write `report.json`.

Structural checks a build cannot skip: every `items.level.json` line parses and carries a
class, `persistentId`s are unique, every `shapeName` / texture / `terrainFile` referenced
exists, every `.dae` parses as XML with `Z_UP`, `.ter` sizes agree with `.terrain.json`, exactly
one `TerrainBlock` / `LevelInfo` / spawn, and — the one strong physical check — sampled
`OnGround` buildings sit on the `.ter` within 0.5 m (`beamng.validate_level`). Until the writers
land (A1+), the folder may hold only `info.json`; the report still records what ran.
"""

import json
import os
import time

from . import Stage


def enabled(settings):
    return True


def units(census, settings):
    return 1


def run(ctx, progress):
    progress.begin("validate: level folder", total=None, unit="")
    level = ctx.level_dir
    problems = []
    checks = {}
    if not os.path.isdir(level):
        problems.append(f"level directory missing: {level}")
    else:
        try:
            from ..beamng import validate_level
        except ImportError:
            validate_level = None                       # the level validator lands in A1
        if validate_level is not None:
            checks, problems = validate_level.check(level, ctx.bundle, progress)
        else:
            checks = {"note": "level validator not built yet (A1)"}
    report = dict(ctx.report)
    report["validation"] = {"problems": problems, "checks": checks}
    report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    os.makedirs(level, exist_ok=True)
    with open(os.path.join(level, "cs2beamng_report.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=1, sort_keys=True, default=str)
    progress.end(f"{len(problems)} problem(s)")
    for p in problems[:20]:
        progress.log(f"validate: {p}")
    return {"problems": len(problems), "checks": checks}


STAGE = Stage("validate", "Validate level and write report", enabled, units, run)
