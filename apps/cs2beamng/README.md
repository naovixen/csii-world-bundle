# cs2beamng — CS2 World Bundle → BeamNG.drive level

The application half of the CS2 → BeamNG pipeline. It reads a **CS2 World Bundle** (the output
of the ripper mod in this repo), bakes the engine-neutral derivatives it needs
(`csiibundle.bake` → `<bundle>/derived/`), and writes a BeamNG.drive level. Everything long
runs in a worker **process** with live progress, ETA and Cancel; the UI never computes.

Status (2026-08-15): A0 (shell) and A1 (terrain, water, sky, spawn) of
`docs/plans/beamng-consumer-plan.md` are built — the pipeline runs **bake → terrain → water →
scene → validate** and produces a level BeamNG can load: a lossless 4096² `.ter` with one
material per ground and tunnel holes, water planes/blocks by level band, sky/time from the
rip's lighting, a spawn on a road, preview and `info.json`. Meshes, materials, roads and the
1.2 M instances land in A2–A5.

## Install & run

```
pip install -e .            # from the repo root; installs csiibundle + cs2beamng
pip install -e .[gui]       # + pywebview for a native window (else: your default browser)

cs2beamng                   # the application
cs2beamng gui --browser     # force the browser
python -m cs2beamng         # the same

cs2beamng build [bundle] --out DIR [--set key=value ...] [--settings file.json]
```

Headless `build` uses the same runner and stages as the UI, with a terminal renderer; exit
code 0 on success, 1 on failure/cancel, 2 for bad settings. Ctrl-C cancels cleanly.

## Where things are

| what | where |
|---|---|
| settings (every knob, presets, per-OS detection) | `settings.py`, persisted at `~/.cs2beamng/settings.json` (`CS2BEAMNG_HOME` overrides) |
| stage rate history (drives the first ETA) | `~/.cs2beamng/history.json`, seeded from `pipeline/history.py` |
| the build report | `<levels>/<level_name>/cs2beamng_report.json` — settings, per-stage seconds/results, validation |
| the derived cache | `<bundle>/derived/` (contract §13); re-baked per product only when its inputs changed |
| local UI server | `server.py`: `http://127.0.0.1:<port>/?t=<token>` — token-gated JSON API + SSE `/events` |

## Architecture (short)

```
UI (browser / pywebview)  ──HTTP+SSE──►  server.py  ──►  pipeline/runner.py  ──queue──►  worker process
        never computes                    ThreadingHTTPServer      state, ETA, cancel        stages/*.py
```

* `pipeline/` — `Runner` (spawns the worker, folds messages into a JSON state with per-stage
  and overall ETA, cancel with a hard-kill watchdog, dead-worker detection), `worker.py` (runs
  `stages.all_stages()` in order, writes the report), `progress.py` (queue-backed `Progress`
  incl. heartbeat and optional stage-level progress), `census.py` (bundle counts + derived
  freshness + estimates), `history.py`.
* `stages/` — `Stage(key, title, enabled, units, run)`; `bake` (runs `csiibundle.bake` for
  the products the settings need and reports a stage-level fraction from per-product
  estimates), `terrain`, `water`, `scene`, `validate`; A2+ add `materials`, `shapes`,
  `networks`, `derived`, `instances`, `lights`, `cook`. Every stage appends to one
  `beamng.level.Level` (`stages.level_of(ctx)`) and rewrites the object tree when done.
* `beamng/` — the format layer: `frame` (bundle→BeamNG axes, heading matrices), `ter` (v9
  `.ter` read/write, orientation contract, `sample_height`), `level` (SimGroup tree,
  materials, `info.json`, deterministic persistentIds), `validate_level` (structure + the
  terrain-under-buildings check).
* `ui/` — `index.html`, `app.css`, `app.js`: no build step, no CDN; dark by default, light via
  `prefers-color-scheme`.

## Tests

`python tools/test_a1_level.py` — `.ter` identity + orientation, `heading_matrix` vs a
vanilla spawn, the level tree, validator faults, a headless example build, the water band
split on a synthetic river, and (real bundle present) the terrain-under-buildings check.
`python tools/test_app_shell.py` — settings schema/groups, runner (cancel within a chunk,
hard-kill of a worker that ignores cancel, dead worker, raising stage), server (token, API,
SSE), and a headless build of `docs/example-bundle`. Stub stages come from
`tools/cs2beamng_stub_stages.py` via `CS2BEAMNG_STAGE_MODULE`. CI runs it on Windows, Linux and
macOS (`.github/workflows/ci.yml`).
