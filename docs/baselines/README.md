# Phase-0 baselines

Captured 2026-08-21, before any refactor code moved. Refactor Phase 10 is the only phase allowed
to change a performance number, and these are the numbers it must compare against — same
instrument, before and after (`docs/plans/refactor.md`, principle 10).

| File | What it is |
| --- | --- |
| `bundle-2026-08-20.json` | sha256 + byte count for all 16,991 files of the ripped bundle, plus per-layer record counts. `python tools/bundle_manifest.py --compare <this> <after>` lists every difference. |
| `cs2_city_fine-report-2026-08-20.json` | the built level's own report — **its `settings` block is the input to every consumer-phase rebuild** (refactor Rule 6), and its `validation` block is the 0-problem baseline. |
| `cs2_city_fine-budget-2026-08-20.txt` | `cs2beamng budget` — resident MiB and triangles actually drawn. |
| `bake-example-cprofile-2026-08-21.txt` | a cProfile of the example-bundle bake. **Not a performance baseline** — see below. |
| `cs2_city_fine-report-after-phase1.json` | the same level rebuilt after refactor Phase 1 (`cook: true`, 16 m 03 s). Every validation check matches the baseline; `cs2beamng budget` came out **byte-identical**, which is why there is no "after" budget file. |
| `profile.md` | the numbers themselves, and what instrument produced each. |

## What the digest deliberately excludes

`derived/` is not hashed. It is the offline bake's output, so it is a function of the bundle
*and* of consumer code — hashing it into a producer baseline would report a consumer change as a
producer one. The vector exports (`GeoJSON/`, `GeoTIFF/`, `Shapefile/`, `Styles/`) are excluded
for the same reason in reverse: they are upstream Carto's output, which the refactor does not
touch.

## The bundle this describes

Ripped **2026-08-20 09:08:59–09:37:38** (28 min 39 s). No `Systems/ IO/ Domain/` commit landed
between that rip and this capture, so it is a valid pre-refactor golden. Every producer phase
after Phase 11 diffs a fresh rip against it; a difference that no phase declared is a defect.

## Why the cProfile is not a baseline

`docs/example-bundle` bakes in **0.375 s**, and half of that is `importlib`. Nothing in Phase
10's producer or consumer work-list is visible at that scale. Worse, the real bake runs its
products in worker **processes**, and `cProfile` only sees the parent — so profiling the real
bake this way would measure the coordinator and miss the work.

The instrument that does answer the question is the bake's own per-product seconds, which the
runner already reports and which `~/.cs2beamng/history.json` accumulates as units-per-second
across builds. `profile.md` records both.
