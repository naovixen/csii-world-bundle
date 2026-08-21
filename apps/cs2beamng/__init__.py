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


def _frozen_streams():
    """A windowed frozen build has no console, so `sys.stdout` is None and the first `print()`
    anywhere raises `AttributeError: 'NoneType' object has no attribute 'write'`.

    The pipeline prints — progress lines, stage summaries, the census — from the UI process and
    from every spawned worker, so this has to be fixed once, before any of them run, which is
    why it is at package import rather than in an entry point: a `spawn` child re-executes the
    exe and enters through multiprocessing's bootstrap, not through `cli.main`.

    The stream goes to a log file beside the settings, so a frozen build that dies still leaves
    something to read. Unfrozen, and in the console build, this does nothing at all.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    import os

    try:
        base = os.environ.get("CS2BEAMNG_HOME") or os.path.join(
            os.path.expanduser("~"), ".cs2beamng"
        )
        os.makedirs(base, exist_ok=True)
        handle = open(os.path.join(base, "cs2beamng.log"), "a", encoding="utf-8", errors="replace")
    except OSError:

        class _Null:  # last resort: swallow, never raise
            def write(self, _text):
                return 0

            def flush(self):
                pass

        handle = _Null()
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


_frozen_streams()
