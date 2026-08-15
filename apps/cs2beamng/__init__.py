"""`cs2beamng` — turn a CS2 World Bundle into a BeamNG.drive level.

The application half of the CS2 → BeamNG pipeline. `csiibundle` reads the bundle and bakes the
engine-neutral derivatives (`csiibundle.bake`); this package is the BeamNG adapter and the user
interface around it:

* `pipeline/` — the stage runner: a worker process, a progress contract with ETAs, cancel,
  history-calibrated estimates. Nothing here computes; it schedules and reports.
* `stages/`   — the build stages in order: bake, terrain/water/sky, materials/textures,
  shapes/networks/derived tiles, instances (forest / TSStatic) + lights + groundcover, validate.
* `beamng/`   — the format writers: `.ter`, COLLADA `.dae`, `items.level.json`, forest files,
  materials v1.5, `info.json`; and the level-folder model.
* `ui/`       — the local web UI (HTML/CSS/JS, no build step) served by `server.py` and shown
  in a native window (`pywebview`) or the default browser.
* `cli.py`    — `cs2beamng` (GUI) and `cs2beamng build ...` (headless).

Cross-platform by construction: pure Python + numpy, no shell-outs except the optional
"cook in BeamNG" stage, `pathlib`, hardlink-with-copy-fallback.
"""

__version__ = "0.1.0"
