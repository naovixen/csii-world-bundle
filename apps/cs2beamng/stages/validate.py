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
            checks, problems = validate_level.check(level, ctx.bundle, progress, ctx.settings)
        else:
            checks = {"note": "level validator not built yet (A1)"}
    progress.end(f"{len(problems)} problem(s)")
    for p in problems[:20]:
        progress.log(f"validate: {p}")
    # `_report` is merged into the report by the worker and travels in the replay payload, so a
    # SKIPPED validate still leaves its verdict in `cs2beamng_report.json` — writing straight to
    # `ctx.report` did not (see `pipeline/worker._merge_report`).
    return {"problems": len(problems), "checks": checks,
            "_report": {"validation": {"problems": problems, "checks": checks}}}


# This stage's input is THE LEVEL FOLDER, not the bundle: it is fresh exactly when nothing the
# build wrote has moved since the run that produced the verdict now in the report. That is a
# stat walk (`state.level_fingerprint`) and it is self-verifying, which "every earlier stage was
# skipped" would not have been - the bake and the cook are outside the skip mechanism entirely,
# and a hand-edited level file is outside all of it.
# version 2: the resident-bytes budget, and the verdict survives a skip
# version 3: that budget is derived from the card and the render resolution (A6.0)
# version 4: the level is checked against the LIGHTING PATH the user's BeamNG is set to - a local
#            light is shaded only by the deferred renderer, and under `Basic Lighting` every one
#            of them costs load time and scene memory and lights nothing (A6.9, audit §82)
# version 5: a shape whose `.dae` source was dropped counts as PRESENT when its cooked `.cdae` is
#            there, which is what the engine actually loads (A8/P7)
# version 6: `map.lua`'s own node build is run over the level's OWN `DecalRoad`s and the build
#            FAILS on a triangle - the shape that reaches map.lua:459's unguarded `lanes`
#            compare and takes the game down before the level opens (A7.5, audit §85)
STAGE = Stage("validate", "Validate level", enabled, units, run, inputs=("level:",),
               settings_keys=("gpu_vram_mib", "render_width", "render_height"), version=6)
