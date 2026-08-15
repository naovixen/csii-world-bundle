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

from collections import namedtuple

Stage = namedtuple("Stage", "key title enabled units run")

BuildContext = namedtuple("BuildContext", "settings bundle level_dir derived_dir census region report jobs")


def all_stages():
    """The ordered stage list. Imported lazily so the UI process never loads numpy-heavy code."""
    from . import bake as _bake            # noqa: WPS433
    from . import validate as _validate    # noqa: WPS433
    stages = [_bake.STAGE]
    for name in ("terrain", "materials", "shapes", "networks", "derived", "instances", "lights", "cook"):
        try:
            module = __import__(f"{__name__}.{name}", fromlist=["STAGE"])
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
            continue
        stages.append(module.STAGE)
    stages.append(_validate.STAGE)
    return stages
