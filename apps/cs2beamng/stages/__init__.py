"""The build stages, in order. Each is a `Stage(key, title, enabled, units, run)`:

* `enabled(settings)` — whether this build runs it (a toggle, a preset, an absent layer);
* `units(census, settings)` — the denominator for its ETA before it starts (the census's
  numbers), or None;
* `run(ctx, progress)` — the work, reporting through the bake's `Progress` protocol (begin /
  advance / log / end / check) and returning a dict that lands in `report.json`.

`ctx` is a `BuildContext`: settings, bundle dir, level dir, derived dir, census, region, and a
`report` dict every stage may add to. Stages are independent files under this package; the
worker (`pipeline/worker.py`) is what sequences them.
"""

import importlib
import os
from collections import namedtuple

Stage = namedtuple("Stage", "key title enabled units run")

BuildContext = namedtuple("BuildContext", "settings bundle level_dir derived_dir census region report jobs shared",
                          defaults=(None,))


def level_of(ctx):
    """The one `beamng.level.Level` every stage of a build appends to (kept in `ctx.shared`).
    Each stage writes the accumulated object tree when it is done, so the level folder is
    consistent after every stage — a cancelled build is a smaller level, not a broken one."""
    from ..beamng.level import Level  # noqa: WPS433

    shared = ctx.shared if ctx.shared is not None else {}
    if "level" not in shared:
        shared["level"] = Level(ctx.level_dir, ctx.settings["level_name"], ctx.settings.get("level_title") or ctx.settings["level_name"])
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
    from . import bake as _bake            # noqa: WPS433
    from . import validate as _validate    # noqa: WPS433
    stages = [_bake.STAGE]
    # materials runs AFTER every stage that binds a material, so it exports only what is used
    for name in ("terrain", "water", "scene", "shapes", "networks", "derived", "instances", "lights", "materials", "cook"):
        try:
            module = __import__(f"{__name__}.{name}", fromlist=["STAGE"])
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
            continue
        stages.append(module.STAGE)
    stages.append(_validate.STAGE)
    return stages
