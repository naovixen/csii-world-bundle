# Phase-0 profile — the numbers Phase 10 must beat, and the instrument that produced each

Captured 2026-08-21, before any refactor code moved. Refactor principle 10 is *efficiency,
measured*: nothing lands as an optimization without a before and an after from the **same
instrument**, and any phase whose timings regress beyond run-to-run noise does not land.

Machine: Windows 11, the same one every number below was taken on. A number taken elsewhere is
not comparable to these and must not be reported as an improvement.

---

## 1. The rip — 1,719 s (28 min 39 s)

Instrument: the mod's own stage timings, `…/Logs/Carto.log`, rip of **2026-08-20 09:08:59 →
09:37:38**. This is the rip that produced the golden bundle.

| Stage | Seconds | Units | Per unit |
| --- | ---: | ---: | --- |
| Clean | 0.0 | — | |
| Instances | 72.3 | 1,302,892 placed objects | 18,020 objects/s |
| Appearance | 0.8 | — | |
| Meshes, materials & textures | **1,298.2** | 4,298 prefabs | 3.3 prefabs/s |
| Lane graph | 46.3 | 677,006 lanes | 14,622 lanes/s |
| Terrain (fine cascade) | 1.1 | 8192² @ 1.75 m, 4 readbacks | |
| Networks & lane geometry | 73.6 | 27,067 edges + 79 orphan nodes | |
| Terrain & water | 71.4 | — | |
| Manifest | 155.4 | 16,991 files | 109 files/s |

**75 % of the rip is one stage.** `Meshes, materials & textures` is where Phase 10's producer
work has to pay off or not pay off at all; the instance walk it names (57.6 s at
`InstanceRipSystem.cs:163`) is 72.3 s here and 4 % of the total. Nothing in the ripper uses
Burst or a job — every one of these is a serial main-thread walk.

The log line says `9 stage(s)` and then names **ten**. That disagreement is a Phase 12 item
(the docs say "eight" in five places); noted here because a reader comparing a future log to
this table will otherwise think a stage disappeared.

## 2. The bundle — 16,991 files, 5.51 GB

Instrument: `python tools/bundle_manifest.py`, digest in `bundle-2026-08-20.json`.

| Layer | Records |
| --- | ---: |
| instances.jsonl | 1,276,060 |
| lanes.jsonl | 676,863 |
| lanemeshes.jsonl | 119,202 |
| clipvolumes.jsonl | 49,730 |
| areas.jsonl | 26,855 |
| networks.jsonl | 8,259 |
| warnings.jsonl | 210 |
| districts.jsonl | 0 |

Every count matches `bundle.json`'s own declared `counts` block exactly, which is itself the
first thing a fresh rip's digest should be checked against.

`instances.jsonl` alone is 326 MB and 1.28 M records. Phase 10 records that the consumer streams
and iterates it **3–4 times per build**; that is the figure to re-measure, and the record count
here is its denominator.

## 3. The build — measured rates, not one run's seconds

Instrument: the app's own history, `~/.cs2beamng/history.json`, accumulated over 5 builds.

Seconds are the wrong unit to freeze here: a build's stage times depend on which stages the
incremental runner skipped, and the most recent `cs2_city_fine` report (2026-08-20 13:29, in
this directory) is a 44 s run in which ten of fourteen stages were skipped as "inputs and
settings unchanged". Units per second survive that.

| Stage / product | Units per second |
| --- | ---: |
| terrain | 1,004,719 |
| bake (overall) | 303,294 |
| scene | 90,523 |
| water | 74.6 |
| lights | 51,305 |
| groundcover | 48,987 |
| bake/skirts | 27,826 |
| navgraph | 24,001 |
| instances | 17,315 |
| derived | 17,004 |
| shapes | 10,620 |
| bake/lanegeometry | 662 |
| bake/markings | 318 |
| materials | 278.5 |
| bake/areas | 210 |
| networks | 123 |
| bake/materials | 102 |
| bake/terrainmask | 28.3 |
| bake/lods | 20.3 |
| validate | 0.41 |

`validate` at 0.41 units/s and `water` at 74.6 are the two whose "unit" is a whole level rather
than a record — read them as seconds-per-level inverted, not as throughput.

### The bake, in seconds

`python -m csiibundle.bake --force` over the whole 5.51 GB bundle, 2026-08-21, **2,540 s
(42 min 20 s)**. Not a clean-room measurement — other work (a `pytest` run, `dotnet build`) shared
the machine — so treat these as the right order of magnitude and re-measure on a quiet box before
claiming a Phase 10 win from them.

| Product | Seconds | Share |
| --- | ---: | ---: |
| lods | 1,575.4 | 62 % |
| materials | 435.2 | 17 % |
| areas | 154.6 | 6 % |
| lanegeometry | 144.5 | 6 % |
| markings | 87.5 | 3 % |
| terrainmask | 75.3 | 3 % |
| terraincut | 55.9 | 2 % |
| skirts | 11.6 | 0.5 % |

**Two products are 79 % of the bake.** `lods` generates the LOD ladders and `materials` walks
every material and every instance; nothing else is close. That is where a bake optimization has
to land, and `skirts` — which Phase 10 does not mention — is already 11.6 s and cannot repay
attention.

## 4. The level — 4,256 MiB resident, 1,278,860 triangles drawn

Instrument: `cs2beamng budget`, full output in `cs2_city_fine-budget-2026-08-20.txt`.
4,000 m view from (1399, 1153).

| Resident | MiB |
| --- | ---: |
| meshes cs2 | 2,002 |
| meshes networks | 787 |
| meshes areas | 383 |
| meshes skirts | 158 |
| meshes markings | 130 |
| meshes terraincut | 6 |
| **meshes total** | **3,466** |
| textures .dds | 554 |
| textures .png | 235 |
| **RESIDENT TOTAL** | **4,256** |

| Triangles inside the view distance | | Statics |
| --- | ---: | ---: |
| networks | 793,547 | 532 |
| areas | 343,191 | 523 |
| markings | 63,718 | 574 |
| skirts | 55,697 | 418 |
| cs2 | 15,019 | 12,352 |
| terraincut | 7,688 | 36 |
| **TOTAL** | **1,278,860** | |

Against vanilla's heaviest level, `west_coast_usa`: 3.0× the shape files, 0.9× the TSStatics,
6.8× the forest items, 10.5× the materials. 7,879 of 7,879 shapes cooked.

## 5. What has no baseline, and why

* **A cProfile of the real bake.** The bake runs its products in worker **processes**;
  `cProfile` sees only the parent, so it would measure the coordinator and miss the work. The
  example-bundle profile in this directory runs in 0.375 s — half of it `importlib` — and is a
  smoke test, not a measurement. Phase 10's bake instrument is §3's per-product seconds.
* **A full cold `cs2beamng build` in seconds.** No such run exists since the golden rip. The
  first consumer phase (Phase 2) rebuilds `cs2_city_fine` in place with `cook: true` as its
  gate, and that run's report is the one to add here.

## 6. Gate timings, so the guardrails themselves stay cheap

| Command | Seconds |
| --- | ---: |
| `ruff check` | < 1 |
| `ruff format --check` | ~1 |
| `python tools/check_limits.py` | ~4 |
| `python tools/check_ruff_debt.py` | ~2 |
| `dotnet test tests/Carto.Tests` | ~1 (19 tests, 42 ms of test time) |
| `dotnet build -c Release` (the mod) | ~40 |
| `pytest tools` (bundle present, quiet machine) | 584 |
| `pytest tools` (bundle present, sharing the box with a level build) | 12,890 |
| `pytest tools` (no bundle — what CI sees) | 62 |

The gap between the first two is not overhead: with a bundle on disk the suite measures the real
5.5 GB city, which is the point of it. The third is a warning — the same suite took **3 h 35 m**
while a `cs2beamng build` and several `dotnet build`s shared the machine. Every number in this
file is only comparable to one taken on a box doing nothing else.

`pytest` collection alone is ~43 s because seven test files are still module-level scripts whose
body runs at **import**, so collecting them runs them. Phase 4 turns those into real per-claim
functions and that cost goes away.
