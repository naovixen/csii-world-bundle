# Handoff — 2026-08-12, session 3: the open-items sweep

Supersedes `docs/plans/2026-08-12-session-2-handoff.md` where they disagree. Read this, then
`docs/plans/2026-08-11-next-session-handoff.md` for the consumer hand-off effort's phase order.

**One-line status:** every item in the 2026-08-12 open table was already closed (FLARE, ALPHA,
DEADPASS) or is closed now (PROBE); of the 2026-08-11 table, GRADE-1, WATER-2, PILLARS, D1–D6,
the N sign-off, **V1 and P16-REG** are all closed too. Nothing from either table is outstanding.
The effort is back at its parked point, **Phase 5 — extract `tools/cs2bundle/`**.

## State of the world

- **The bundle is the 2026-08-12 15:13 full rip and it carries every producer change in this
  session.** `git log --since='2026-08-12 15:13' -- Systems IO Domain` lists nothing. Safe to
  measure against, all layers.
- All eight stages, **1,666 s**, **zero** `FAILED`, **zero** `DROPPED:`.
- `dotnet build -c Release` 0/0, deployed. `test_bundle_contract.py` **63/63** (was 58).
  `validate_bundle.py` on the full city **VALID, 120 warnings** (was 133).

## The re-rip: DONE and green (full rip 2026-08-12 15:13)

Every producer change in this session is validated in-game.

- **GRADE-1, both halves.** `bundle.json` carries `"grading": "grading.json"` among **23**
  layers (was 22); `schema` still 2. The ordering fix shows in the timestamps —
  `grading.json` **15:10:12**, `bundle.json` **15:13:06**: the file exists before the scan that
  indexes it, which is precisely what was broken.
- **WATER-2.** The new `semantics` string shipped, naming all five things `SeaLevel` drives and
  stating that it is not the water surface. Value still `15.0` against
  `ArchipelagoWaterRenderSettings` — unchanged and expected; the fix was the description.
- **PROBE.** `COLLAPSED:` still reports with its `Name|Where` labels intact, so re-keying by
  `(piece entity, isNode)` preserved the report exactly. Stage times: Instances 63.1 s, mesh
  stage 1,253.0 s, total 1,666 s (was 1,717) — **do not attribute that to this change**; it is
  one sample either side of run-to-run variance measured in tens of seconds.
- **PILLARS.** VALID, **120 warnings, zero of them orphan-mesh** — the exemption confirmed
  against fresh producer output rather than a synthetic bundle. The 120 split exactly as
  predicted: 89 + 28 + 2 assets the producer declared in `warnings.jsonl`, plus the 1 parent
  advisory.
- Networks identical to the previous rip **to the digit**: 7,289 mesh form / 970 centerline /
  9,266,577 triangles, `Centerline-only: 91`.
- Gates: `test_node_fan` PASS (5,872 strips, worst 0.93 m; negative control 0.20 → 11.64 m),
  `test_clip_cut` green on all six windows, contract 63/63, decal rows / png decode / splat
  extras / drape pick all pass.
- Incidental: the log printed `World terrain: no backdrop heightmap resident; layer omitted` —
  the exact residency case documented under D6 this session, live in this rip.

### One new finding — the `COLLAPSED:` probe's third clause is a false-positive generator

The probe reported **9 rows, and all nine are noise.** Every one trips
`(Size.y > 0.5f) && (extent.y < 0.05f)`, and that clause misreads its own input:
`NetCompositionHelpers` sets `m_Size.y` from `NetPieceData.m_HeightRange.max - min`, which
`NetPiecePrefab` declares as an **authored** field defaulting to `Bounds1(0f, 3f)`. It is a
declared band, not the geometry's thickness. So every flat surface piece whose author left the
default gets flagged — which is why the list is gravel shoulders, public-transport lanes and
tram tracks, reporting large built footprints like `120.99 x 0.01 x 94.23 m`. Manifestly built,
and flat because a lane surface is flat.

The other two clauses — `pinnedAcross` and `max(extent.x, extent.z) < 0.05` — are the real
collapse tests, and none of the nine trip either. **Recommend dropping the `Size.y` clause and
keeping those two.** Not done unasked: it weakens a diagnostic, and nothing depends on it.

## What closed

| item | outcome |
|---|---|
| **PROBE** | The `COLLAPSED:` probe **stays permanently**, and no longer charges per placement. |
| **GRADE-1** | `grading.json` is declared as `layers.grading`; schema stays 2; the write order that made the key unreachable is fixed. |
| **WATER-2** | `seaLevel`'s semantics corrected — and the audit entry's own reasoning was wrong and is corrected with it. |
| **PILLARS** | Not the class it was recorded as. Stack expansions are exempt from the orphan warning; 133 → **120**. |
| **D1, D3–D6** | Paid, three of them not as written. |
| **D2 / N items** | Signed off by you: VFX, road-name labels and the city-boundary fence are **permanent** non-goals. |
| **V1** | **Yes** — lot-painted ground does arrive as `Surface` sub-areas. Confirmed in-game and by a total `owner` join. |
| **P16-REG** | Closed and struck at your instruction. Do not re-open. |

### GRADE-1 (`8d33983`) — two faults, not one

`grading.json` had shipped since 2026-08-10 and was never in `layers`, so a consumer obeying §1
could not find it. The recorded reason was that `layers` is a closed object and a new key would
force schema 3. **§12 says the opposite in its own words** — "add a new layer key" is on its
list of within-version changes. The closure protects a consumer that would *silently drop* a
value (the `colorMask` case); the layer index is presence-based and a consumer that does not
implement grading already behaves correctly. `additionalProperties: false` stays as the
producer-side typo guard it actually is. **Schema stays 2**, reasoned out in the new §12.3.

The second fault made the first moot: `GradingWriter.Write` ran **after**
`BundleWriter.WriteManifest`, whose index is a directory scan. A first rip would have declared
every layer but this one; a second would have declared the *previous* rip's file. Grading is
written first now.

### WATER-2 (`946c9c6`) — the entry's reasoning was wrong, its conclusion was not

The audit said `WaterSystem.SeaLevel` "feeds only `ResetBackdropWaterToSeaLevel`" and might be
uninitialised. A sweep of every reader finds **five**: the backdrop reset, the `seaLevel`
uniform bound into the water compute shader on every `EvaporateStep` / `EvaluateSeaPropagation`
/ `RestoreWaterInSea`, `WaterRenderSystem`'s `cameraY - SeaLevel` term behind `smoothnessRange`,
`Viewer`'s `maxDistanceToSeaLevel` culling plane, and the editor's sea-level slider. It is a
live, load-bearing **sea datum**.

The conclusion survives anyway: a sea datum is not a water surface, and on an all-inland map the
two are hundreds of metres apart — which is the 15.0 against 508.6–528.3 m that opened the item.
`15.0` is what `UpgradeToNewWaterSystem` leaves behind, `min(m_Height)` over the constant-depth
sources. The string now states both halves.

### PILLARS (`2eabc09`) — the premise was wrong twice

"133 warnings, every one the known bridge-pillar class ... pillars are referenced by networks,
not instances" is wrong on both counts. The real split of the 133:

| count | class |
|---|---|
| 89 | assetId has no mesh — "none of N sub-mesh(es) yielded geometry", **declared in `warnings.jsonl`** |
| 28 | same, "empty SubMesh buffer" |
| 2 | same, "no SubMesh buffer" |
| 13 | mesh is not referenced by any instance |
| 1 | aggregated advisory: parents that resolve to no exported record |

The 13 are **not** network-owned. They are §4.1 **stack expansions**: a stacked prefab is placed
only through `X__stack_first/mid/last`, never through `X`, which orphans the bare mesh, its
`__base` skirt, and any section this city's pillars were not tall enough to need.
`Vertical_Pillar` is the type case — 1,296 `__stack_first` + 6,706 `__stack_mid` records and not
one reference to `Vertical_Pillar` itself. The validator exempts the stack family now, as it
already exempted skirts and tree-state variants.

**The remaining 120 are all the producer's own declared gaps** — every one of them is an asset
the producer could not deliver and said so in `warnings.jsonl`, which is the layer designed for
exactly that. Deciding whether *those* should still warn is a separate, real question.

### D1–D6 — three were owed differently than recorded

- **D1** §5B — lane `tunnel` and segment `form: "tunnel"` are different predicates
  (`cmin(Elevation) < 0` vs `CompositionFlags.General.Tunnel`); 3,214 against 309, re-measured.
- **D3** §4.3 — **not as written.** The audit said "scatter from channel R is game-faithful";
  R is dirt, and that wording predates the 2026-08-10 channel fix. §4.3 names the *grass*
  channel and sends the reader to `rasters.splatmap.channels` rather than hard-coding an index.
- **D4** §4.2 — a per-value table, every rule re-verified in `BatchInstanceSystem`'s render
  branch (including the `m_NewPrefab != Entity.Null` upgrade exception, which means "under
  construction" does not always mean "not drawn").
- **D5** — four parts, two not as written. The §8c lane reference is a per-quad constant that
  does **not** tilt (provable from the producer's own stamp); the audit's "the game lerps
  laterally" is **not** repeated, because that pass is a GPU rasterisation and is not readable
  from the managed assemblies. §7.2's flat "CS2 does not store a position" splits into
  procedural (true) and punctual (false). The `InstanceWriter` comment was already current.
- **D6** — the water half was already fully documented; only the backdrop half was owed.
  Recorded alongside it: **`layers.worldWater` is reserved in the schema and no producer path
  has ever emitted it.**

### PROBE (`dc1d06f`) — it stays, at a cost that no longer scales

`COLLAPSED:` reports a piece's **authored** size against the extent it built. The authored size
never ships, so unlike the reach probe FLARE retired, no offline gate can ask the question at
all. What it cost was the fair objection: it sat in the per-placement loop of the mesh stage —
the 1,313 s stage — resolving a prefab name and interpolating a string per placement to learn a
name it needs once per piece. Keyed by `(piece entity, isNode)` now.

### V1 — closed, and stronger than it was asked

**Yes.** Confirmed in-game by the user, and corroborated on the 09:39 rip: **27,047 of 27,156
areas carry an `owner`, and every single one resolves to a `kind: "building"` record — none
dangling.** So `owner` is a hard join, not a hint, and a consumer should group a lot's surfaces
under its building rather than scattering them loose on the terrain. The 109 without an owner
are the *other* case and equally real: ground the player painted directly, belonging to no lot,
almost all asset-pack brushes (`G87_More_Concrete_*`, `Water_Mirror_*`,
`Farming_Surfaces___Decals_*`, `Terrain_Surfaces_Pack_*`). §8b says all of this now.

### P16-REG — closed and struck, do not re-open

The instance-walk regression is not being pursued, by the user's instruction and with the
numbers to support it. The 09:39 rip's own log reads **65.0 s**, not the 80.5 s the item opened
with, against a 57.6 s baseline. Stage times from that log: Clean 0.0, **Instances 65.0**,
Appearance 0.7, *Meshes, materials & textures 1,313.2*, Lane graph 46.8, Networks & lane
geometry 81.8, Terrain & water 66.8, Manifest 143.0; total 1,717 s. The walk is 3.8% of the rip
and the regression 0.4%. Measuring which reads dominate would mean main-thread instrumentation
inside that walk. If perf ever matters again, the mesh stage is 76% of the rip.

## Still open

| # | item | what it needs |
|---|---|---|
| PROBE-2 | Drop the `COLLAPSED:` probe's `Size.y` clause? It flags 9 healthy flat surface pieces per rip and can flag no defect the other two clauses miss. | A yes. Two-line producer change, rides the next rip. |
| PILLARS-2 | Should the 120 remaining warnings — every one a gap the producer already declared in `warnings.jsonl` — still warn? | A decision. Not raised before because the class was mis-attributed. |
| §29 | Backdrop water simulation | Still deferred; explicitly **not** part of the D2 sign-off. |
| Phase 5 | extract `tools/cs2bundle/` | The effort's actual next step. |

## Method notes earned this session

- **A "feeds only X" claim is a claim about every reader, and takes a sweep of every reader.**
  WATER-2's premise died to `decomp all Game <dir>` + `rg SeaLevel`, thirty seconds of work that
  the original entry did not do. The same shape caught PILLARS ("every one the pillar class")
  and D3 ("channel R"). **A doc's claim about the game is a hypothesis with a citation, not a
  fact** — and the ones that age worst are the ones written before a later finding moved the
  ground under them (D3 predates the R=dirt fix; §7.2's "no position" predates the P8 split).
- **Do not repeat a claim you cannot verify, even from your own audit.** D5 asserted the game
  lerps the clip band laterally. That pass is GPU. What is provable is the producer's own
  constant, which is what §8c now says, with the unreadability stated rather than papered over.
- **Every gate added this session was shown to fail with its fix reverted** — the schema key
  (`$.layers: unexpected property 'grading'`), and all three stack-exemption gates, one of which
  exists specifically to stop the exemption becoming a blanket amnesty.
- **The rip log answers perf questions for free**, and it is one rip per file — the 09:39
  timings were still on disk only because no rip has run since. Read it before instrumenting
  anything.
- The full validator takes **~10 minutes** on this city. Run it once, to a file, in the
  background; do not run it twice in one command.
