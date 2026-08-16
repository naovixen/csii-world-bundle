# cs2beamng — CS2 World Bundle → BeamNG.drive level

The application half of the CS2 → BeamNG pipeline. It reads a **CS2 World Bundle** (the output
of the ripper mod in this repo), bakes the engine-neutral derivatives it needs
(`csiibundle.bake` → `<bundle>/derived/`), and writes a BeamNG.drive level. Everything long
runs in a worker **process** with live progress, ETA and Cancel; the UI never computes.

Status (2026-08-16): A0 (shell), A1 (terrain, water, sky, spawn) and A2 (roads/networks,
markings, areas, skirts, lane furniture, materials + textures) of
`docs/plans/beamng-consumer-plan.md` are built and **accepted in-game** (region). The
pipeline runs **bake → terrain → water → scene → networks → derived → materials → cook →
validate**: a lossless 4096² `.ter` with one material per ground and tunnel holes (`contain`),
water by level band, sky/time from the rip's lighting, a spawn on a road, every network
segment as 256 m COLLADA tiles with exact collision, the bake's draped/conformed tiles, v1.5
materials with capped textures, and BeamNG's own headless `-compilemeshes` over the shapes so
the first load does not parse 2 GB of COLLADA (the cook stage — A5, pulled forward). The
terrain/road-interface rewrite tried after load #9 (ground at `lane − 0.25`, `touch` holes, rim
walls) regressed load #10 (walled tunnel mouths, moats beside sunken roads) and was reverted
2026-08-16; the plan log has the details. Buildings, props, trees (the 1.2 M instances) land
in A3; performance tuning A4.

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
  estimates), `terrain`, `water`, `scene`, `networks` (segment meshes → tiles), `derived`
  (bake tiles → shapes), `materials` (runs after every binder: exports only the materials this
  build bound), `cook` (BeamNG `-compilemeshes` headless; skips with a reason when BeamNG is
  running, not found, or the level is outside a userfolder; removes the placeholder
  `main.materials.json` files the compiler writes beside the shapes — they shadow the real
  materials in-game), `validate` (fails on a materials file under `art/shapes/` or a material
  name defined twice); A3+ add `shapes`,
  `instances`, `lights`. Every stage appends to one
  `beamng.level.Level` (`stages.level_of(ctx)`) and rewrites the object tree when done.
* `beamng/` — the format layer: `frame` (bundle→BeamNG axes, heading matrices), `ter` (v9
  `.ter` read/write, orientation contract, `sample_height`), `dae` (COLLADA writer with the
  vanilla shape hierarchy), `materials` (v1.5 objects + texture export), `level` (SimGroup
  tree, materials, `info.json`, deterministic persistentIds), `validate_level` (structure,
  material bindings, detail sizes, the terrain-under-buildings check).
* `ui/` — `index.html`, `app.css`, `app.js`: no build step, no CDN; dark by default, light via
  `prefers-color-scheme`.

## Tests

`python tools/test_a2_dae.py` — the COLLADA writer round trip, hierarchy, frame/winding/UV
conventions, v1.5 material rules (opacityMap, alphaTest, palette), texture export.
`python tools/test_a1_level.py` — `.ter` identity + orientation, `heading_matrix` vs a
vanilla spawn, the level tree, validator faults, a headless example build, the water band
split on a synthetic river, and (real bundle present) the terrain-under-buildings check.
`python tools/test_app_shell.py` — settings schema/groups, runner (cancel within a chunk,
hard-kill of a worker that ignores cancel, dead worker, raising stage), server (token, API,
SSE), and a headless build of `docs/example-bundle`. Stub stages come from
`tools/cs2beamng_stub_stages.py` via `CS2BEAMNG_STAGE_MODULE`. CI runs it on Windows, Linux and
macOS (`.github/workflows/ci.yml`).
