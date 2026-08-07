# CS2 storage audit — what the game stores, what we ship, and what the delta is

**Phase A of** `docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md`.
**Subject:** the full-city bundle ripped 2026-08-07, at
`…/ModsData/Carto/spike/`, and the `Carto.log` of the rip that produced it.
**Status:** measurement only. Nothing here changes the producer or the contract; the ranked
table in §9 is what later phases are chosen from.

## How to read this

Each domain section answers the same four questions:

1. **What does the game store** as the authoritative description?
2. **What do we ship** instead?
3. Is the delta **information** (we add something real) or **representation** (same facts, more
   bytes)?
4. What is the **smallest lossless encoding**, and what does a consumer have to do to use it?

Every claim carries a number and the command that produced it. Estimates are labelled by
provenance: `measured` (a sample was actually re-encoded and extrapolated by a stated rule),
`arithmetic` (computed from format widths), or `unmeasured`. Two sources are used throughout —
the decompiled game for what it stores, the shipped bundle for what is actually in it — because
a decompile is a claim about code, not about data.

## 0. Baseline

### Environment

```
BUNDLE="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto/spike"
LOG="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/Logs/Carto.log"
```

`ls "$BUNDLE"` lists `areaprefabs.json`, `areas.jsonl`, `colors.json`, `instances.jsonl`,
`lanemeshes/`, `lanemeshes.jsonl`, `laneprefabs.json`, `lanes.jsonl`, `lights.json`,
`materials.json`, `meshes/`, `networks/`, `networks.jsonl`, `terrain/`, `textures/`,
`warnings.jsonl`. `head -1 "$LOG"` is
`[2026-08-07 11:50:23,206] [INFO]  Mod instance loaded. 模組實例載入完成。`.
`python -c "import PIL, numpy, zlib, lzma, struct, json; print('deps ok')"` prints `deps ok`
(Python 3.13.5, PIL 12.3, numpy 2.5).

### Byte-exact census

Command:

```
python "$SCRATCH/audit/baseline.py" "$BUNDLE"
```

(`baseline.py`: walks each top-level entry, sums `st_size` over every file beneath it.)

| entry | bytes | MiB | share | files |
|---|---:|---:|---:|---:|
| textures/ | 1,979,015,958 | 1887.3 | 40.4% | 3,510 |
| meshes/ | 1,800,230,540 | 1716.8 | 36.8% | 5,634 |
| networks/ | 424,469,220 | 404.8 | 8.7% | 7,248 |
| instances.jsonl | 324,202,611 | 309.2 | 6.6% | 1 |
| lanes.jsonl | 242,145,498 | 230.9 | 4.9% | 1 |
| terrain/ | 72,050,547 | 68.7 | 1.5% | 41 |
| lanemeshes.jsonl | 28,063,057 | 26.8 | 0.6% | 1 |
| areas.jsonl | 10,296,261 | 9.8 | 0.2% | 1 |
| colors.json | 5,795,684 | 5.5 | 0.1% | 1 |
| lanemeshes/ | 2,738,524 | 2.6 | 0.1% | 71 |
| networks.jsonl | 2,446,606 | 2.3 | 0.0% | 1 |
| materials.json | 2,120,933 | 2.0 | 0.0% | 1 |
| lights.json | 563,330 | 0.5 | 0.0% | 1 |
| laneprefabs.json | 25,433 | 0.0 | 0.0% | 1 |
| warnings.jsonl | 22,959 | 0.0 | 0.0% | 1 |
| areaprefabs.json | 7,369 | 0.0 | 0.0% | 1 |
| **TOTAL** | **4,894,194,530** | **4667.5** | **100.0%** | |

`4,894,194,530` bytes is the number every later "share of total" percentage in this document
divides by.

### Networks count reconciliation

Three numbers were reported for `networks/` before this task: 7,248 (files on disk), 8,259
(a log line), 7,380 (the design spec's estimate table). Commands:

```
ls "$BUNDLE/networks" | wc -l                                     # -> 7248
wc -l < "$BUNDLE/networks.jsonl"                                  # -> 8259
grep -c '"assetId":"seg_' "$BUNDLE/networks.jsonl"                # -> 7248
grep -E 'segment\(s\) to networks.jsonl' "$LOG"                   # -> "Wrote 8259 segment(s) to networks.jsonl in 65.8 s"
```

`Carto.log` itself states the split explicitly, two lines below the write line:

```
Wrote 8259 segment(s) to networks.jsonl in 65.8 s
  mesh form: 7248 (8591923 triangles, 404.8 MiB of glTF in networks/)
  centerline form: 1011 (4026 point(s))
```

The rip records every network segment in `networks.jsonl` (**8,259** records — this is the
correct count of "how many network segments the city has"). Of those, **7,248** carry an
`assetId` pointing at a baked `networks/seg_<id>.glb` mesh, and that number equals both the file
count on disk and the count of `"assetId":"seg_` matches — those three are the same measurement
taken three ways and agree exactly. The remaining **1,011** records have no `assetId` field at
all (confirmed: `grep -v '"assetId":"seg_' "$BUNDLE/networks.jsonl" | grep -c '"assetId"'` →
`0`) — they are written in "centerline form": geometry only as the `centerline` point list plus
`attrs`, with no baked mesh. Breaking those 1,011 down by `attrs.category`
(`E:\cs2-carto-ripper\.worktrees\audit-bundle-representation\.superpowers\sdd\2026-08-08-bundle-representation-audit\measure\audit\no-mesh-records.jsonl`,
extracted with `grep -v '"assetId":"seg_' "$BUNDLE/networks.jsonl" > no-mesh-records.jsonl`):

| category | records without mesh | records with mesh | total |
|---|---:|---:|---:|
| cable | 693 | 0 | 693 |
| pipe | 105 | 0 | 105 |
| taxiway | 91 | 0 | 91 |
| waterway | 81 | 0 | 81 |
| pathway | 41 | 207 | 248 |
| road | 0 | 5,725 | 5,725 |
| highway | 0 | 943 | 943 |
| train | 0 | 313 | 313 |
| tram | 0 | 60 | 60 |

`cable`, `pipe`, `taxiway` and `waterway` are entirely mesh-less — every segment in those four
categories is centerline-only, 100% of the time. `pathway` is split: 207 of 248 have a mesh, 41
do not. All 41 mesh-less `pathway` records share `attrs.form == "elevated"` and
`attrs.asset == "LeisurePier01Small01"`, but 11 *other* `LeisurePier01Small01` segments in the
same rip *do* have a mesh, and segment length does not separate the two groups (meshed lengths
run 10.3–52.1 m, mesh-less lengths run 8.4–70.7 m, fully overlapping) — so for this one asset the
mesh/no-mesh split is not explained by category, form, prefab name, or length alone; it is
recorded here as `unmeasured` and left for whichever later task inspects piers.

7,380 (the design spec's estimate table, `docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md:21`)
matches none of the three measured values (7,248 files; 8,259 records; 7,248 records with a
`seg_` `assetId`) and is superseded by this section's measured figures.

## 1. Textures

## 2. Meshes

## 3. Networks

## 4. `instances.jsonl`

## 5. `lanes.jsonl`

## 6. Markings and decals

## 7. Terrain

## 8. Rip time, by phase

Every timestamped log line in `Carto.log` (`grep -nE '^\[2026' "$LOG"`) plus the two explicit
in-line durations the rip logs for its heaviest steps. The rip ran as one uninterrupted mod
lifecycle from `Mod instance loaded` to `Mod instance had been disposed of`; there is no log
line marking a separate "rip triggered" event, so the first phase's bracket start is the mod
load banner itself — if the user left the mod idle after load before triggering the rip, that
idle time is included in the Instances bracket below and this is *not* separable from the log
alone.

| # | phase | start | end | seconds | type |
|---|---|---|---|---:|---|
| 1 | Instances (collect + write `instances.jsonl`, `colors.json`, `lights.json`; mesh-phase progress-indicator setup) | 11:50:23.206 | 11:55:54.823 | 331.617 | bracket |
| 2 | Meshes (mesh export + concurrent VT texture/material readback for mesh prefabs) | 11:55:54.823 | 12:15:31.929 | 1177.106 | bracket |
| 3 | Lanes + Networks (lane collect/write, network topology, network segment mesh bake, network materials/textures) | 12:15:31.929 | 12:23:17.290 | 465.361 | bracket |
| 4 | Terrain | 12:23:17.290 | 12:23:24.328 | 7.038 | bracket |
| — | **TOTAL** (mod loaded → mod disposed) | 11:50:23.206 | 12:23:24.328 | **1981.122** | bracket |

Rows 1–4 sum to 1981.122 s, matching the total bracket exactly (318.236 + 13.381 + 1177.106 +
465.361 + 7.038 = 1981.122; the 13.381 s between `colors.json`/`lights.json` being written and
the first mesh-phase progress line is folded into row 1's end boundary).

Within those brackets, four durations are logged explicitly by the rip itself and are
`instrumented`, not bracket-derived — but each is a *sub-step* of its row, not the whole row, so
they do not replace the bracket and are not additive with each other:

| instrumented sub-step | seconds | type | which row it falls inside |
|---|---:|---|---|
| Mesh writing: `Wrote 4198 .glb, skipped 120, failed 0, in 1178.3 s` | 1178.3 | instrumented | row 2 (1177.106 s bracket — the two agree to within 1.2 s) |
| Mesh-pass texture readback: `Texture bytes: 1853.6 MiB; readback held the main thread 347.1 s` | 347.1 | instrumented | row 2, concurrent with the 1178.3 s above, not sequential |
| Network segment mesh baking: `Wrote 8259 segment(s) to networks.jsonl in 65.8 s` | 65.8 | instrumented | row 3 (465.361 s bracket; 465.361 − 65.8 − 9.5 = 390.061 s of row 3 is lane collection, lane write, network edge topology, and everything else not covered by an explicit duration) |
| Network-pass texture readback: `Texture bytes: 54.9 MiB; readback held the main thread 9.5 s` | 9.5 | instrumented | row 3, concurrent with other row-3 work |

## 9. Ranked candidates
