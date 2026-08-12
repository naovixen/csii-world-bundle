# Handoff — 2026-08-12, session 3: the open-items sweep

Supersedes `docs/plans/2026-08-12-session-2-handoff.md` where they disagree. Read this, then
`docs/plans/2026-08-11-next-session-handoff.md` for the consumer hand-off effort's phase order.

**One-line status:** every item in the 2026-08-12 open table was already closed (FLARE, ALPHA,
DEADPASS) or is closed now (PROBE); of the 2026-08-11 table, GRADE-1, WATER-2, PILLARS, D1–D6,
the N sign-off, **V1 and P16-REG** are all closed too. Nothing from either table is outstanding.
The effort is back at its parked point, **Phase 5 — extract `tools/cs2bundle/`**.

## State of the world

- **The bundle is the 2026-08-12 09:39 full rip and every producer change in this session
  post-dates it.** `8d33983`, `946c9c6`, `dc1d06f` all touch `Systems/` or `IO/`. Nothing here
  is validated in-game; every producer change below is **built, awaiting re-rip**.
- Measurements taken against that bundle in this session are all facts about the *save* — what
  shape the entities have, how many tunnel lanes there are, which meshes nothing references —
  which is the one thing the staleness rule says it is still the right source for.
- `dotnet build -c Release` 0/0, deployed. `test_bundle_contract.py` **63/63** (was 58).
  `validate_bundle.py` on the full city **VALID, 120 warnings** (was 133).

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
