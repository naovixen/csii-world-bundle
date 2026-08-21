"""The build stages, in order. Each is a `Stage(key, title, enabled, units, run, ...)`:

* `enabled(settings)` — whether this build runs it (a toggle, a preset, an absent layer);
* `units(census, settings)` — the denominator for its ETA before it starts (the census's
  numbers), or None;
* `run(ctx, progress)` — the work, reporting through the bake's `Progress` protocol (begin /
  advance / log / end / check) and returning a dict that lands in `report.json`.

`ctx` is a `BuildContext`: settings, bundle dir, level dir, derived dir, census, region, and a
`report` dict every stage may add to. Stages are independent files under this package; the
worker (`pipeline/worker.py`) is what sequences them.

## The incremental declarations (A4)

Four more fields say what this stage is a function OF, so the worker can skip it when nothing
it reads has changed (`pipeline/state.py` has the whole mechanism and its three guards):

* `inputs` — `"layer:<manifestKey>"` / `"derived:<relpath>"`. **`None` means "always run"**,
  which is the default: a stage opts in to being skippable by declaring what it reads.
* `settings_keys` — every setting whose value changes its output, including the ones it reads
  through a helper (`beamng.placement` reads `trees`/`props`/`buildings`/the density sliders)
  and `region` when it uses `ctx.region`. Reading an undeclared one is caught at runtime and
  by `tools/test_app_shell.py`; the stage then re-runs rather than shipping stale bytes.
* `outputs` — level-relative paths whose absence forces a re-run.
* `shared_keys` — what it publishes into `ctx.shared` for later stages, replayed on a skip.
* `after` — earlier stages this one consumes the OUTPUT of, through `ctx.shared` or the level
  folder. `materials` exports only the textures the shapes/tiles bound, so a change in `shapes`
  must re-run it even though none of its own declared inputs moved. The signature carries those
  stages' signatures, so an upstream change propagates exactly one hop per declaration.
* `version` — bump when the stage's own code would produce different bytes for the same inputs.

A stage that reads the built LEVEL rather than the bundle declares the single input `"level:"`
(the validator does): its fingerprint is a stat walk of the level folder, minus the two files
the build writes about itself. `"shapes:"` is the narrower one for a stage that is a function of
the written shapes alone (the cook is): a stat walk of `art/shapes/**/*.{dae,cdae}`, which does
not move when a texture or the terrain does.
"""

import importlib
import os
from collections import namedtuple

Stage = namedtuple(
    "Stage",
    "key title enabled units run inputs settings_keys outputs shared_keys after version",
    defaults=(None, (), (), (), (), 1),
)

BuildContext = namedtuple(
    "BuildContext",
    "settings bundle level_dir derived_dir census region report jobs shared",
    defaults=(None,),
)


def level_of(ctx):
    """The one `beamng.level.Level` every stage of a build appends to (kept in `ctx.shared`).
    Each stage writes the accumulated object tree when it is done, so the level folder is
    consistent after every stage — a cancelled build is a smaller level, not a broken one."""
    from ..beamng.level import Level

    shared = ctx.shared if ctx.shared is not None else {}
    if "level" not in shared:
        shared["level"] = Level(
            ctx.level_dir,
            ctx.settings["level_name"],
            ctx.settings.get("level_title") or ctx.settings["level_name"],
        )
        shared["level"].derived_dir = ctx.derived_dir
    return shared["level"]


# Tests (and only tests) point this at a module whose `all_stages()` returns stub stages; the
# variable is read in the worker process too, which is what makes hang/cancel tests possible.
STAGE_MODULE_ENV = "CS2BEAMNG_STAGE_MODULE"


_CACHE = {}


def all_stages():
    """The ordered stage list. Imported lazily so the UI process never loads numpy-heavy code;
    resolved once per process (the census, the runner and the state preview all ask)."""
    override = os.environ.get(STAGE_MODULE_ENV) or ""
    if override in _CACHE:
        return list(_CACHE[override])
    stages = _resolve(override)
    _CACHE[override] = stages
    return list(stages)


def _resolve(override):
    if override:
        return importlib.import_module(override).all_stages()
    from . import bake as _bake
    from . import validate as _validate

    stages = [_bake.STAGE]
    # materials runs AFTER every stage that binds a material, so it exports only what is used
    # `navgraph` and `poi` come after `scene` (they read and extend the `info.json` it writes)
    # and `poi` after `navgraph` (it snaps onto the DecalRoads the navgraph stage shipped)
    for name in (
        "terrain",
        "water",
        "scene",
        "shapes",
        "networks",
        "derived",
        "instances",
        "lights",
        "navgraph",
        "poi",
        "groundcover",
        "materials",
        "cook",
    ):
        try:
            module = __import__(f"{__name__}.{name}", fromlist=["STAGE"])
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
            continue
        stages.append(module.STAGE)
    stages.append(_validate.STAGE)
    return stages
