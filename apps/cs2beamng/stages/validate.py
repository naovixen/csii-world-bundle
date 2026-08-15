"""Stage `validate` — check the level folder; its findings land in `cs2beamng_report.json`.

Structural checks a build cannot skip: every `items.level.json` line parses and carries a
class, `persistentId`s are unique, every `shapeName` / texture / `terrainFile` referenced
exists, every `.dae` parses as XML with `Z_UP`, `.ter` sizes agree with `.terrain.json`, exactly
one `TerrainBlock` / `LevelInfo` / spawn, and — the one strong physical check — sampled
`OnGround` buildings sit on the `.ter` within 0.5 m (`beamng.validate_level`). Until the writers
land (A1+), the folder may hold only the report. The worker writes `cs2beamng_report.json`
*after* this stage, so the file carries every stage's timing, this one's included.
"""

import os

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
    ctx.report["validation"] = {"problems": problems, "checks": checks}
    progress.end(f"{len(problems)} problem(s)")
    for p in problems[:20]:
        progress.log(f"validate: {p}")
    return {"problems": len(problems), "checks": checks}


STAGE = Stage("validate", "Validate level", enabled, units, run)
