# Game-fidelity audit — the 1:1 coverage question, answered (2026-08-09)

**Question.** Before the consumer hand-off effort begins: does the bundle capture everything the
game renders and stores for the built environment — a 1:1 relationship — and if not, what exactly
is missing?

**Method.** Five parallel investigations following the `validate-vs-game` process: (1) a
game-side completeness sweep enumerating every draw path in the decompiled `Game.Rendering.*` /
`Game.Effects.*` and every simulation state with a rendering consumer, classified against the
shipped bundle; (2–3) independent line-by-line re-audits of the terrain/clip and network producer
deltas landed since the §21–§28 delta pass (the code beside audit §30–§39, which those sections
described but never independently verified); (4) the never-audited Phase 7/8 infrastructure
(orchestrator, pre-clean, manifest, settings surface); (5) the Phase 8 threading/frame-budgeting
changes. Authority: a fresh decompile of `Game.dll` (4,336 types) and
`Colossal.IO.AssetDatabase.dll` via `tools/decomp`; measurements against the shipped bundle
(`ModsData/Carto`, ripped 2026-08-09 12:39 — **after** the last producer commit `3b9494d` 11:34,
so for once the bytes on disk reflect HEAD and measuring them is legitimate).

**Verdict.** For a static, daytime, 1:1 recreation: **no always-visible content class is
missing.** Everything the game persistently draws — objects, runtime-composed net meshes, lane
geometry with cable sag, base skirts, stacked objects, surface areas, clip volumes, terrain,
water shape, resolved per-instance color — is captured, verified by sampling the shipped bundle,
and every one of the 22 bundle layers traces to a named game system (no producer invention
found). The gaps are: two cheap parameter blocks that decide how every water/ground pixel *looks*
(water appearance, terrain tiling), one state-variant hole (dead trees render healthy), one
latent producer bug (lights.json photometry), one clip-volume divergence (area prisms are closed
in-game, unbounded in the bundle), a default-settings trap that would make a fresh install's rip
invalid, and a tail of cheap attributes and documentation debts. All are enumerated below with
dispositions.

Section numbering in brackets [§n] refers to `docs/cs2-api-audit.md`.

---

## 1. Master classification — what the game draws/stores vs the bundle

Ranks: **A** = visible in every screenshot · **B** = visible in common cases · **C** =
dynamic/simulation state a static rip reasonably omits · **D** = invisible in a static daytime
scene.

### 1a. Rendered world geometry

| Content class | Game system | Bundle layer | Verdict | Rank |
|---|---|---|---|---|
| Object meshes (buildings, props, trees, decals) | `BatchInstanceSystem.UpdateObjectInstances` | `instances.jsonl` + `meshes/` (1,266,276 / 5,589) | **Captured** [§9–§10, §25] | A |
| Net edge+node meshes (runtime-composed; never a baked prefab) | net composition, `colossal_CompositionMatrix0..7` | `networks.jsonl` + `networks/` (7,236 baked + 1,023 centerline) | **Captured** [§8, §21] | A |
| Lane geometry: markings, curbs, hanging cables (sag = `m_Hanging·dist·4/3`, matches `LaneSystem`), fences | `MeshType.Lane`, `HangingLane` | `lanemeshes.jsonl` + `lanemeshes/` + `laneprefabs.json` | **Captured** [§26, §32, §39] | A |
| Building base skirts (`MeshFlags.Base`) | `BatchMeshHelpers.AddBaseLines` | `meshes/*__base.glb` | **Captured** [§27] | A |
| Stacked objects (start/mid/end tiling) | `CalculateStackSubMeshData` | `meshes/*__stack_*.glb` + `scale` (7,003) | **Captured** | A |
| Surface areas (terrain-projected decals) | `AreaRenderSystem`, `RenderedArea` | `areas.jsonl` (26,441) + `areaprefabs.json` | **Captured** [§30, §36]; PBR completeness → GAP 7; Lot-paint → V1 | A |
| Clip volumes (tunnel-portal stencil) | `GenerateClipDataJob` → `TerrainSurface.RenderClipAreas` | `clipvolumes.jsonl` (49,724) | **Captured** [§20, §8c]; area-prism bounds → P3 | A |
| Terrain: heights, splat, user splat, surface index, ground textures | `TerrainSystem`, `TerrainMaterialSystem` | `terrain/*` | **Captured** [§17, §22, §33]; tiling params → GAP 4 | A |
| Backdrop terrain | `TerrainSystem.worldHeightmap` | `terrain/world_heightmap.png` (conditional) | **Captured-when-present.** Absent from this rip because the map has no backdrop heightmap resident — the rip's own log line ("World terrain: no backdrop heightmap resident; layer omitted") confirms an honest omission, not a failure | B |
| Backdrop water (horizon sea) | `WaterSystem.simulateBackdrop` | — | **Deliberately deferred, documented** [§29] | B/C |
| Water surface | `WaterSystem` | `terrain/water_level.png` | **Captured**; appearance params → GAP 3 | A |
| Zoning cell grid | `MeshType.Zone`, gated on `activeTool.requireZones` | — | **Correctly excluded** (never drawn in normal play; gate measured) | D |
| City boundary fence (quad-strip along owned-tile edges, drawn every frame) | `CityBoundaryMeshSystem` | — | **Undocumented exclusion** → D2 (document; a recreation doesn't want map furniture) | C |
| Road-name labels (world-space text on roads, on by default) | `AggregateRenderSystem` | names ship in `networks.jsonl attrs.name` (6,732); label *rendering* not | **Undocumented exclusion** → D2 | C |
| Ambient grass scatter | `VegetationRenderSystem`: one `FoliageVFX` graph scattered **from the splatmap's grass channel**, zero per-instance data | `terrain/splatmap.png` R channel ships the scatter input | **Excluded, documented** [§4.3] — doc wording to fix (D3): a consumer scattering its own grass from channel R is game-faithful, not an invention | C |
| VFX (smokestack plumes, spillways, fountains, fire) | `Game.Effects.VFXSystem`; `VisualEffectAsset` itself unrippable | — | **Undocumented exclusion** → D2 (name+placement could ship; recommend doc-only) | C |
| Punctual HDRP lights (street lamps, floodlights) | `LightEffect` on **EffectPrefab**, placed by `EffectSource.EffectSettings` | `lights.json` — partially | **Latent producer bug** → P8 (GAP 1) | C (night) |
| Emissive material lights (windows, neon) | `ProceduralEmissiveSystem`, `ProceduralLight` | `lights.json` (1,899, fields match 1:1) + `emissive` maps (971) | **Captured** | C (night) |
| Overlays: icons, routes, guides, infoview tints, gizmos | all tool/infoview-gated (early-out measured) | — | **Correctly excluded** [§29] | D |

### 1b. Simulation state that shapes the visible city

| State | Game side | Bundle | Verdict | Rank |
|---|---|---|---|---|
| Transform, elevation, flags | `Game.Objects.Transform`/`Elevation` | `pos/rot`, `attrs.elevation*` | **Captured** | A |
| Per-instance color pick (seed-driven, incl. season blend, brand channels) | `MeshColorSystem` | `attrs.colorSet` = the game's own resolved answer (1,191,199) + `colors.json` | **Captured** — recording the resolved value sidesteps the bit-exact seed trap | A |
| Tree age state + growth | `Tree.m_State/m_Growth` | `attrs.treeState/growth/growthScale`; one mesh state shipped [§6.1] | **Partial**: 1,173 Dead trees have no mesh to swap to → P7 (GAP 5) | B |
| Stockpile fullness (garbage/coal/log piles) | `Quantity.m_Fullness` + `RequireEmpty..Full` submeshes | — | **Partial gap** → P9 (GAP 6) | C |
| Left/right-hand traffic furniture | city `m_LeftHandTraffic` | correct side exported [§6.1] | **Captured** | A |
| Building states (construction/condemned/abandoned/destroyed) | `attrs.state`; game *hides* under-construction and spawns crane/fence (real entities → captured); condemned has **no rendering consumer at all**; destroyed = collapse pose + rubble (0 in city, no rubble meshes exist) | `attrs.state` | **Captured** as flags; consumer rules to document (D4) | C |
| Road wear tint on lane decals | `LaneCondition.m_Wear` → `colossal_CurveDeterioration = wear/10` (normal view; do not confuse with infoview-only `NetCondition.m_Wear`) | — | **Gap** → P10 (GAP 8) | C |
| Brand signs/colors; household yard props | placeholder resolution spawns real entities | captured by construction (rip walks placed entities) | **Captured** | B |
| Snow/wetness; wind sway; water waves/flow | `SnowSystem`, `WindTextureSystem`, flow textures | — | **Deliberately excluded, documented** [§29] — "the bundle is the dry, built world" | C |
| Animated-prop pose (turbines, cranes, clocks) | `ProceduralBone`, posed from live sim | rest-pose mesh (authored, valid) | **Captured as rest pose**; live pose unreproducible from a save | C |
| Season / time-of-day / sun | `TimeData`, `PlanetarySystem` lat/lon | baked into resolved colorSet; `geo.originLatLon` ships | **Captured in effect**; season label → GAP 2 | D |
| Outside connections | tag + sign props (real entities) | captured by construction | **Captured** | B |

### 1c. Per-prefab asset data

| Class | Game side | Bundle | Verdict | Rank |
|---|---|---|---|---|
| LODs | `LodProperties.m_LodMeshes`; LOD0 **is** the authored/highest mesh by construction (`AssetImportPipeline.SetupLODs`) | LOD0 only | **Captured — lossless by construction**; consumers generate their own LODs | D |
| Mesh state variants | `SubMeshFlags` enum | one documented state [§6.1] | **Partial, documented**; only visible losses = Dead/Stump (P7) and fullness (P9) — no rubble or partially-built meshes exist in the game at all | — |
| Material params | `SurfaceAsset` + template | `materials.json` (template-resolved) [§11, §24] | **Captured** | A |
| Decal prefab data | `DecalProperties` (TextureArea, priority, layers — nothing else exists) | UVs baked [§19], `decalLayers`, `decalHeight`; priority approximated by lift ladder | **Captured** | D |
| Area material PBR | `RenderedArea` normal/mask maps, metallic/smoothness, edge fade | diffuse+color+uvScale only | **Gap** → P11 (GAP 7) | C |
| Terrain render settings | `TerrainRenderSettingsPrefab` (per-layer near/mid/far tiling 2400/1600/40, LOD blend, triplanar) | only `splatMultiplier`/`splatPower` | **Gap** → P5 (GAP 4) | B |
| Water render settings | `WaterRenderSettingsPrefab` (`m_WaterColor`, scattering, absorption, smoothness) + `WaterSystem.SeaLevel` | — | **Gap** → P4 (GAP 3) | B |
| Skeleton/animation | `ProceduralAnimationProperties` | rest-pose vertices | adequate (see 1b) | C |

---

## 2. Findings and dispositions

Disposition key: **P# = producer work** (Phase 12 of the hand-off plan; one final re-rip covers
all), **D# = documentation** (Phase 9 / audit-doc amendments), **R# = refactor-adjacent** (Phase
10/12 mechanical), **N = documented non-goal** (user sign-off).

### 2.1 Producer work-list

**STATUS 2026-08-09: every P item below is implemented and building clean, in three commits —
`095322a` (P1, P2, P13-half, F4, P3, P15-terrain, P4, P5, T-F20), `b549bbe` (P14, I-F7/8/9/11,
P8, P12, P9, P10) and `4d6959d` (P6, P7, P11, P13-network, P16, GAP 6, N-F2/N-F3).** None of it
is validated: per the staleness rule, producer changes can only be proven by an in-game re-rip,
and every claim here is "built, awaiting re-rip". The live assertions to read in the next rip's
report and log: the `decalRole` marking/wear split (predicted 38/5 on the last city's
catalogue), the authored-normals piece counts (`_rebuiltFramePieces` should be 0) and their lit
A/B render — a sign error in the frame would show as inverted shading that no offline check
here can rule out — the new `surfaceless`/`waterway` clip-band classes, `lights.json` entries
finally carrying `range`/`temperature`, and the instance-walk stage time against its 57.6 s
baseline.


| # | Finding | Source | Severity |
|---|---|---|---|
| P1 | **Default `RipMeshLimit = 50` makes a fresh install's one-button rip invalid**: the cap silently drops all but the 50 most-used prefab meshes (this city: 5,539 of 5,589) with no warnings.jsonl declaration, while instances still reference them. The 50 was a Phase-3 dev-measurement default that leaked into the Phase-8 full-city button. Fix both halves: default 0 (All), *and* declare capped-off prefabs in warnings.jsonl so any capped rip stays contract-valid. (`Settings.cs:882`, `MeshRipSystem.cs:599`) | infra F1 | **critical** |
| P2 | Writer-task fault wedges the rip silently (queue never drains, orchestrator waits forever) or silently loses queued records without failing the stage. Check `WriterTask.IsFaulted` in `Advance`; `FAILED` summary from `Finish`. Same wedge pre-exists in `MeshRipSystem` (624/644). Related: abort-timeout dispose can truncate a line (fix removes trigger); `_generation` fence not volatile. (`InstanceRipSystem.cs:405-443`, `LaneRipSystem.cs:238-276`) | threading F1/F3/F8 | medium |
| P3 | **Clip-area volumes are closed prisms in-game** — `GenerateAreaClipMeshJob` caps bottom `min(0, aggMin(heightRange))−0.3` *and top* `max(0, aggMax)+0.3`; producer ships bottom-only ("caps nothing above" misread the job). Over-cuts everything above; under-cuts terrain dipping >0.3 below. Data already in hand (Triangle buffer); additive `top` field. (`TerrainRipSystem.cs:1950-2015`) | terrain F3 | medium |
| P4 | Ship water appearance params: `WaterRenderSettingsPrefab` (`m_WaterColor`, `m_WaterScatteringColor`, `m_AbsorptionDistance`, `m_SmoothnessClose/Distant`) + `WaterSystem.SeaLevel`. One managed prefab read; every water pixel's look depends on it. | GAP 3 | B-rank gap |
| P5 | Ship terrain tiling/blend params: `TerrainRenderSettingsPrefab` per-layer near/mid/far tiling, LOD blend ranges, triplanar, extra-layer params — the §39 lesson (never make a consumer guess metres-per-repeat) repeated on terrain. Additive block beside `splatMultiplier`/`splatPower`. | GAP 4 / terrain F15 | B-rank gap |
| P6 | **Authored normals + tangents through the network bend — implement.** Feasibility proven: the shader receives only the bézier matrices, so the CPU port can rotate authored `MeshNormal`/`MeshTangent` identically (frame = [across, up, along], inverse-transpose; `SmoothTopNormal` override via existing height bounds; flip incl. tangent `w`). Cost ≈ 10–25 s of a 1,413 s rip; retires §8 row 14 (the last big deliberate deviation) and ships TANGENTs for normal-mapped roads. Count the orthonormalization choice as an approximation. Add `SmoothTopNormal` to the GameMesh header deviations list either way (network F3). | network feasibility verdict | high value |
| P7 | Dead/stump tree variant meshes: export `RequireDead`/`RequireStump` submeshes as `assetId__dead.glb`/`__stump.glb` (same pattern as `__stack_*`); 1,173 Dead trees currently render healthy. | GAP 5 | B-rank gap |
| P8 | **lights.json photometry bug**: `AppearanceRipSystem.cs:211` reads `LightEffectData` off the sub-mesh entity; the game keeps it on the EffectPrefab, joined via the object prefab's `Effect` buffer (`EffectSource.EffectSettings` carries position/rotation/intensity/parentMesh). Measured: 0 of 1,899 entries carry the documented `range`/`temperature`. Walk the `Effect` buffer, join `LightEffectData`+`EffectColorData`, emit placed emitters (position exists — §7.2's "CS2 does not store a position" is wrong for HDRP lights and needs the doc fix too). | GAP 1 | latent bug |
| P9 | Stockpile fullness: ship `attrs.fullness` (+ variant meshes via the P7 mechanism where authored). | GAP 6 | C |
| P10 | Road wear: one field on lane-geometry records from `LaneCondition.m_Wear` (feeds `colossal_CurveDeterioration`, visible in normal view). | GAP 8 | C |
| P11 | Area material completeness: extend `areaprefabs.json` with `RenderedArea`'s normal/mask maps (existing texture path), metallic/smoothness, edge fade/noise, and `m_RendererPriority` (consumers z-fight overlapping areas without it). | GAP 7 / terrain F14 | C |
| P12 | §4.2 documented attrs never produced (`name, category, zone, zoning, theme, brand, level, height, storey, season, address` — zero occurrences in 1.27M records). Sources were validated in §5.1; produce them (they are cheap reads) rather than strike, `height` optional (derivable). | GAP 2 | D (contract honesty) |
| P13 | Mid-rip churn + per-entity failures must reach warnings.jsonl (contract §10) — the frame-sliced walk spans ~58 s of live simulation (97 stale hits in the acceptance rip). Also: network stage warnings are **never persisted** (report-only today; give `NetworkRipSystem` the mesh-pass ownership-merge write); `WriteWarnings` gating bug; silent conform-failure path. | threading F2/F9, infra F5, network F2 | medium |
| P14 | Orchestrator/manifest correctness batch: pre-clean `bundle.json` (stale-manifest-on-abort) + atomic manifest write; materials/textures pre-clean pair rule (orphaning under partial configs); vector stage writes outside the bundle (`vector/` layer can never exist — point it at the bundle or amend §9); lane layers contribute nothing to `bounds` (lane-only rip cannot produce a manifest); Appearance stage silently skipped when all kind toggles are off; `FAILED` prefix detection holes; `ripOptions` records live settings not the plan's. | infra F2/F3/F4/F6/F7/F8/F9 | medium batch |
| P15 | Small game-rule gates: `HasSurface==0` branch in `TryGetClipBand`; `Waterway` merge-layer in the elevated test (both clip and roadbed gates — canal beds currently land as roadbed); OverrideMaterial keyed on `m_Version` not material-null; kind-filter union rule for mesh pass (unreachable at defaults). | terrain F1/F2/F13b, threading F4 | low |
| P16 | Perf (measure before/after per `prefer-simple-over-speculative`): hoist `ComponentLookup`/`BufferLookup` in the instance walk (the 57.6 s pass is ~all main-thread component reads; exclusion probes run twice per entity ≈ 2.6M redundant); move `WriteClipVolumes` (~50k records) and area-PNG encodes off the main thread. | threading F5, terrain F20 | perf |

**P16-REG is closed and struck, 2026-08-12.** The instance-walk regression follow-up (80.5 s
against a 57.6 s baseline) is not being pursued. The 2026-08-12 09:39 rip's own log reads
**65.0 s**, so the regression was +7.4 s rather than +22.9 s, on a **1,717 s** rip whose mesh
stage alone is 1,313.2 s — the walk is 3.8% of the rip and the regression 0.4% of it. Measuring
which reads dominate would mean adding per-read instrumentation to the main thread of that walk,
which is the trade `prefer-simple-over-speculative` exists to refuse. **Do not re-open it**;
if perf ever matters again, the mesh stage is 76% of the rip and is where to look. *(P16's other
two halves — `WriteClipVolumes` and the area-PNG encodes off the main thread — are untouched and
unaffected.)*

### 2.2 Documentation work-list (Phase 9 / audit amendments)

| # | Item | status |
|---|---|---|
| D1 | Consumer caveat: lane `tunnel` tag (`cmin<0`; 3,214 records) and segment `form=tunnel` (deep −2/−3·ElevationLimit; 309) are **different game predicates** — never equate them. One cross-reference sentence in §5B. | **PAID** 2026-08-12, §5B. Both predicates re-checked against the producer and both counts re-measured on the 08-12 rip: 3,214 / 309, to the digit. |
| D2 | Document as deliberate exclusions: road-name label rendering, city-boundary fence, VFX placements (names + the `"VFX " + prefab` convention for consumers who substitute), backdrop water status. | **PAID** 2026-08-12, §4.3, after the user signed off the §2.3 recommendation: all three are **permanent** non-goals, not deferrals. No `vfx.json`. The `"VFX " + prefab.name` convention is real and is the game's own (`VFXSystem` line 146) — documented as an anchor for a consumer substituting its own effects, not as a layer. Backdrop water status is recorded under D6 instead (`layers.worldWater` reserved, never emitted). |
| D3 | Fix §4.3 grass wording: the game scatters a FoliageVFX layer *from the splatmap's grass channel* — consumer-side scatter from channel R is game-faithful. | **PAID** 2026-08-12, §4.3 — **but not as written.** "Channel R" is the pre-2026-08-10 channel map; R is dirt and grass is G. The §4.3 text names the *grass* channel and sends the consumer to `rasters.splatmap.channels` for its index rather than hard-coding one. |
| D4 | Building-state consumer rules: under-construction buildings are *hidden* by the game (crane/fence entities ship as normal instances); condemned has no rendering consumer; destroyed pose/rubble not shipped. | **PAID** 2026-08-12, §4.2, as a per-value table. All three claims re-verified in `BatchInstanceSystem`'s render branch (the `UnderConstruction` skip and its `m_NewPrefab != Entity.Null` upgrade exception; `Destroyed.m_Cleared >= 0`; `Condemned` has no reader in the rendering namespace at all). |
| D5 | §8c: clip lane reference is the mean of boundary y (game lerps laterally — band tilts on superelevated roads); spans >0.1 m with clip-count 0 draw nothing (§39 rule covers markings). Kind sub-toggles are destructive re-rips (unlike layer toggles). `InstanceWriter` stale compression comment. §7.2 lights example/position claims (with P8). | **PAID** 2026-08-12 — four parts, two of them not as written. §8c: the lane reference is documented as a per-quad constant that does **not** tilt, which is provable from the producer's own stamp; the parenthetical "game lerps laterally" is **not** repeated, because that pass is a GPU rasterisation and is not readable from the managed assemblies. §7.2: the flat "there is no position, because CS2 does not store one" now splits procedural (true) from punctual (false — `EffectSource.EffectSettings` carries one). Kind-toggle destructiveness: §1. The `InstanceWriter` comment was **already current** — it names COMP-1 and its revert; nothing to do. |
| D6 | Water level-grid + one-cell dilation resample: deliberate deviation re-affirmed this pass. Backdrop terrain honest-omission log line documented (see 1a). | **PAID** 2026-08-12 — half of it already was. The water level-grid deviation and the one-cell dilation were fully described in §8 before this pass; only the backdrop half was owed, and §8 now states that `worldTerrain`'s absence is a residency fact with a named report line. Recorded alongside it: `layers.worldWater` is reserved in the schema and **no producer path has ever emitted it**. |

### 2.3 Non-goals — SIGNED OFF 2026-08-12

The user took the recommendation on all three open items. They are **permanent** non-goals now,
written into the contract at §4.3, not deferrals to revisit — a future schema version does not
add them.

- **VFX**: doc-only. No `vfx.json`, no placements. A `VisualEffectAsset` is a compiled VFX
  graph with nothing to rip, and coordinates for an effect the bundle cannot describe are not
  worth a layer. ~~P-side `vfx.json` possible~~ — declined.
- **Road-name labels**: doc-only. The names ship (`networks.jsonl attrs.name`); the rendering
  is locale + font-atlas output, not city data.
- **City-boundary fence**: doc-only. Map furniture, drawn from the tile grid by
  `CityBoundaryMeshSystem`; a recreation does not want it.
- **Backdrop water simulation**: NOT part of this sign-off and still deferred; §29 stays open.
  Sea level and the water appearance params ship via P4 (see WATER-2 for what `seaLevel` is and
  is not), and `layers.worldWater` is reserved in the schema but has never been emitted (D6).
- **Snow/wetness/wind/waves/season-live state**: remain excluded (established contract).

### 2.4 Open verification (rides Phase 13 acceptance)

- **V1**: one in-game look at a storage yard / specialized-industry field to confirm Lot-painted
  areas arrive as `Surface` sub-areas (vanilla farm/forestry paint does; the areas query requires
  `Game.Areas.Surface`, and `LotPrefab` archetypes add `Lot`).
- The §38 clamp and §21 sync-extrapolation remain shader-side claims resting on the accepted
  in-game A/Bs; nothing reopened.

## 2.5 Post-rip findings (2026-08-10, the Phase 3 validation rip)

The rip that validated Phase 3 also surfaced two things no offline check could have caught.

### SPLAT — the terrain splat channel map (producer + consumer, CRITICAL, RESOLVED)

Two real defects, found 2026-08-10 and fixed at both ends. Everything else investigated alongside
them (view transform, light rig, presentation gain) turned out to be presentation tuning and has
been **removed** rather than kept — see the closing note.

**1. `R` and `G` were swapped.** The bundle called the dirt channel "grass", so composited in
order dirt blanketed the landmass and grass was painted onto the lakebed: the terrain rendered
brown. `R` had been called grass because it "falls monotonically with slope" — but rock is `R`'s
own residual, so **both** soil weights fall with slope together and that observation never
distinguished them. `G` was then assigned *by elimination*, the one inference never measured, and
it was the wrong one. Settled on where a ground can physically exist:

| | flat land 0–5° | 45–90° cliff | under ≥1 m water |
|---|---|---|---|
| `R` | 251.6 | 9.4 | **249.9** (rises with depth) |
| `G` | 201.7 | 0.5 | **0.0** |

`G` is identically zero on every lakebed and cliff — a vegetation mask. `R` blankets the seabed —
sediment. So **`R` = dirt, `G` = grass**, rock the residual of `R` alone.

**2. `B` and `A` are bit-packed, and two of four painted grounds were never shipped.** They are
not two extra weights but **four**, as 4-bit nibbles, two per channel — how the game fits four
paintable extras into an `R8G8_UNorm` user splatmap:

| splat | nibble | extra | paint tool (game locale) |
|---|---|---|---|
| B | low / high | extra1 / extra2 | 01 Dry / 02 Lush |
| A | low / **high** | extra3 / **extra4** | 03 Dirt / **04 Sand** |

`CountPaintableExtras` had read the target's *component count*, got 2, and concluded extra3/extra4
"can carry no weight anywhere" — so the producer shipped **no texture for either** and the contract
declared them unrepresentable. A texture format cannot reveal a packing convention. Ground painted
**Sand** was read as an extra2 opacity and drawn with the forest-floor texture.

Confirmed four ways: `TerrainMaterialSystem.PackToBAChannels` writes four brush components at bit
offsets 0/4/8/12 across exactly these two bytes; `Locale.cok` names the tools Dry / Lush / Dirt /
**Sand**; each nibble is a separate coherent brush-shaped region with extra4's tracing the
**shorelines**; and a probe of a painted beach reads `A = 240 = 0xF0` — extra4 at 15/15. The
re-rip then named the asset outright: **`extra4 <- Sand_temperate_01_Basecolor`**.

**Blending.** Composite over `base` in order — `R` (dirt), `G` (grass), then the four unpacked
extras. Soil channels at `saturate(w / 255 · splatMultiplier)^splatPower`, each extra at
`saturate(nibble / 15 · splatMultiplier)^splatPower`. **Not a partition** — `R` and `G` are both
near-saturated on flat land and sum past 255, so normalising turns every meadow half brown. Sample
the packed channels **point/nearest**; interpolating a packed byte is meaningless, and the game's
own user splatmap is `filterMode = FilterMode.Point`.

**Also added:** `terrain/surfaces/sources.json` records, per ground, the source `texture.name` and
the shader global it was bound as. Without it a bundle could say "this is extra2" but never
"extra2 was captured from *this asset*", so a ground that *renders* wrong could not be told from
one *captured* wrong. It settled the sand question on its first rip.

**Presentation is deliberately not calibrated.** Several attempts were made to match the game's
on-screen colour with a ground gain, a saturation multiplier, a warm light rig and a specular
override. All are **removed**. Each was fitted to a reference that turned out to be the wrong
patch of ground — the worst of them tuned a rig to make *rock* match an in-game *beach* — and a
curve fit cannot tell you that you are fitting the wrong thing. The importer reproduces the
shipped albedo 1:1 under a plain white sun; `--colour` replays the game's own grading, and
`tools/probe_point.py` exists so that any future comparison names the coordinate at both ends.

### SPLAT-3 — the "4x saturation gap" was a measurement artifact (consumer, RESOLVED)

Recorded at length because the *shape* of the error is the lesson. A painted-sand texel was
measured at saturation 0.101 against an in-game eyedropper of 0.442, and the gap was attributed to
the game tonemapping with ACES "which Blender has no view transform for". Both halves were wrong.

* **Blender 5.2 ships ACES.** Its view transforms are `Standard, ACES 1.3, ACES 2.0, Khronos PBR
  Neutral, AgX, Filmic, Filmic Log, False Color, Raw`. The premise that an OCIO config had to be
  installed to test the leading hypothesis was false; it is one assignment.
* **The 0.101 was not the same quantity as the 0.442.** It came from a 64x64 render of an 8 m
  patch, *averaged* — and averaging chroma across texels always desaturates — while the reference
  is a single eyedropper pixel. It was also measured through a chain that applied roughly half the
  grading: saturation on the albedo (before the contrast rather than after), the contrast in the
  *compositor* as a linear node (so invisible in the viewport, where the comparison was made), the
  shadows/midtones/highlights not at all, and then AgX, which desaturates further.

Applied whole, in order, on final radiance, with an ACES view transform, the shipped albedo lands
**within 0.5% on red and green** and the entire residual is one channel:

| | R | G | B |
|---|---|---|---|
| required / delivered, green-normalised | 1.005 | 1.000 | **0.674** |
| the same with SMH also replayed | 0.999 | 1.000 | **0.727** |
| with no grading applied at all | 1.174 | 1.000 | 0.514 |

So `grading.json` does the great majority of the work, the unreplayed SMH accounts for about a
sixth of what is left, and what remains is a ~27% blue excess — a *light colour*, not a saturation
deficit. Two candidate explanations remain and are distinguished by measurement, not argument:
the game's sun/sky is warmer than the white rig the importer ships (the bundle does not currently
capture it), or HDRP's ACES differs from Blender's ACES 1.3 by more than the percent-level.

Ruled out cheaply along the way, both by measurement: the sand texture cannot supply a saturated
eyedropper pixel (per-texel saturation p99 = 0.225, and **0.00%** of its texels reach 0.442), and
the reference coordinate is plain terrain with extra4 at opacity 1.000 — not an area surface.

Where it now lives: `tools/colour_model.py` holds the derivation once, and both the importer and
the probe import it. `colour_model.py --check` prints the residual above against the recorded
in-game reference, so the number is reproducible rather than remembered.

### SPLAT-4 — the residual is the LIGHT, measured against three in-game surfaces (open)

Three in-game screenshots (2026-08-10), each a full-frame crop of one surface, compared against
the model per texel with exposure freed per shot — auto-exposure differs between shots and the
bundle cannot supply it, so hue and saturation are the quantities under test:

| shot | rendering | in-game | model | residual R, G, B |
|---|---|---|---|---|
| SAND | extra4 at opacity 1.000 | `#ac995e` | `#ac986d` | 0.982, 1.000, **0.722** |
| GRASS | grass at opacity 1.000 | `#515417` | `#4e551b` | 1.117, 1.000, **0.765** |
| NEUTRAL | area `Pavement_Surface_02` | `#b5a882` | `#b0a992` | 1.060, 1.000, **0.783** |

(with SMH replayed; without it the blue column reads 0.615 / 0.635 / 0.681.)

**The neutral surface settles the question.** `Pavement_Surface_02`'s albedo is `#5a5a59` — R 90,
G 90, B 89, neutral to within one code value — and it renders in-game at `#b5a882`, saturation
0.278 at hue 45. No property of a neutral albedo can make it warm beige. The chroma is added by
the illumination or by a late display step, which is what the blue residual predicted from the
sand point alone.

**One light colour accounts for most of it.** Fitting a single per-channel gain BEFORE the tonemap
across all three surfaces at once (the residuals above are read *after* ACES, which is non-linear,
so a constant input gain is not a constant output ratio) removes **88%** of the chromatic error;
a white light is the baseline. What survives is ±14% and **surface-dependent** — it changes sign
between grass and the other two — so it is not a light. That part is consistent with HDRP's ACES
differing from Blender's ACES 1.3, which is where blue is known to vary between implementations.

**Do not adopt the fitted gain.** It is three measurements with two free parameters, which is
exactly the shape of the withdrawn 3.1x saturation. Its value is as a *prediction*: capture the
game's actual sun and sky colour in `GradingWriter` and see whether it lands there. Agreement
confirms it; disagreement means the residual was never the light.

**2026-08-11, the capture rip: the prediction was WRONG and the light hypothesis is dead.**
`lighting.sun.color` is `(1, 1, 1)` with `useColorTemperature` **false**, sky zenith and horizon
tints `(1, 1, 1)`, indirect multipliers 1.0, moon off, sun at 66° elevation at 12:01. The
illumination is white. The fitted gain had predicted R x1.05, B x0.87. Vignette is 0.25 but pure
black, a linear achromatic multiply, so it cannot shift chroma either. Every view transform
Blender ships was then run against all three surfaces: ACES 1.3 is already the best by a wide
margin, so the tonemapper *choice* is not it either.

**What the same measurements did settle: the working space.** Grading in ACEScg rather than linear
Rec.709 — HDRP's documented HDR path — cut the residual error from 0.29 to 0.18 and took grass
from 23% out on blue to within 5%. `colour_model` had been grading in Rec.709 on the strength of a
check that measured the difference at "under 2%"; that check compared pre-tonemap values on one
surface, and was wrong. Saturation now lands within 0.06 on all three surfaces. What remains is a
**hue** error concentrated on the two warm surfaces (+6.9 deg sand, +6.2 deg pavement) while grass
is nearly exact (+1.2 deg) — a different shape from the flat blue deficit, and still open.

**SMH should be replayed, and can be.** `ColorUtils.PrepareShadowsMidtonesHighlights` is managed
code: it puts the authored values through `Mathf.GammaToLinearSpace` first, so this bundle's
shadows `(1, 0.979, 0.9239)` are really `(1, 0.953, 0.836)` — nearly twice the blue correction a
naive reading gives. Replaying it closes about a third of the blue gap. Only the luminance blend
across the three ranges is HLSL, and it is a weighted per-channel gain, so the honest way to carry
it is a shaper plus a 3D LUT in the generated OCIO config rather than a matrix.

### SPLAT-5 — the terrain was drawn at a tenth of its albedo (consumer, CRITICAL, FIXED)

Found 2026-08-11 while chasing "the viewport is very dark and none of the colours come through",
and it is the largest fidelity defect this importer has had. Blender premultiplies an Image
Texture node's **`Color` output by its alpha** when the image loads with the default `STRAIGHT`
alpha mode. CS2 packs *data* into the alpha channel of its ground base-colour maps — it is not
opacity — and that data is mostly near zero:

| ground | extra1 | extra2 | extra3 | extra4 | grass | dirt | rock |
|---|---|---|---|---|---|---|---|
| mean alpha | 0.037 | 0.064 | 0.036 | 0.104 | 0.036 | 0.133 | 0.096 |

**Not one texel** in any of the seven is fully opaque. Measured at the sand reference point, the
terrain rendered `#24211d` against a shipped albedo of `#777066` — an **11x** deficit, with the
hue intact, which is why it read as "dark" rather than "wrong colour".

Isolated by elimination, each step ruling out the next: face normals all point up; disabling
shadows changes nothing; hiding the water changes nothing; replacing the material with a flat
Principled of the same albedo renders it correctly; routing the splat blend straight to an
Emission reproduces the deficit; rendering the `extra4` image node alone gives linear
(0.0191, 0.0168, 0.0138) against a file mean of (0.1848, 0.1629, 0.1334) — a uniform **0.1034x**,
which is exactly that texture's mean alpha.

Fixed by loading with `alpha_mode = "CHANNEL_PACKED"`, which leaves `Color` unmodified and keeps
`Alpha` available for materials that genuinely cut out. **Scope: only the textures the importer
loads itself** — the terrain grounds, the splatmap, the area surfaces. Blender's glTF importer
already channel-packs its own, so object, network and prop textures were never affected; that was
checked rather than assumed.

After the fix the sand point renders `#736c61` against `#777066` (within 4/255), and across the
region **0.0%** of pixels fall below display value 0.10, against 44.6% before.

**This is also the answer to two things that were previously misdiagnosed.** `_GROUND_BRIGHTNESS
= 3.09` was a 3x gain compensating for a 10x darkening — which is why it never quite worked, and
why deleting it appeared to make things worse. And the Phase 1 acceptance test passed against a
*synthetic control plane* carrying a hardcoded albedo, which dodged the texture path entirely; a
test that never touches the code under test cannot fail. The acceptance now runs on real terrain
with the real material: hue +0.4, saturation +0.018 against the model.

### WATER-2 — `rasters.water.seaLevel` is not the playable water datum (producer, LOW)

The new §P4 block captured `seaLevel: 15.0` while this map's actual water surface measures
508.6–528.3 m (median 521.0) and the game's own `kDefaultSeaLevel` is 511.7. The field's semantics
string ("WaterSystem.SeaLevel in world metres") reads as the playable datum, which it is not.
`terrain/water_level.png` remains the authoritative water surface and is unaffected. Fix the
semantics or drop the key; do not let a consumer seat water on it.

**CLOSED 2026-08-12 — semantics fixed, and this entry's own reasoning corrected.** The claim above
that `SeaLevel` "feeds only `ResetBackdropWaterToSeaLevel` — backdrop/ocean water" is **wrong**, and
was reached without sweeping the field's readers. It has five: the backdrop reset
(`WaterSystem.UpdateSeaLevel`), the `seaLevel` uniform bound into the water compute shader on every
`EvaporateStep`, `EvaluateSeaPropagation` and `RestoreWaterInSea`, `WaterRenderSystem`'s
`m_LastCameraLocation.y - SeaLevel` depth term that drives `smoothnessRange`, `Viewer`'s
`maxDistanceToSeaLevel` culling plane, and the editor's sea-level slider
(`WaterPanelSystem.kSeaLevel`). So it is a live, load-bearing simulation and render parameter — the
**sea datum** — and not the uninitialised value this entry also floated.

That does not rescue the original reading: a sea datum is still not a water surface, and on a map
whose water is all inland the two are hundreds of metres apart, which is exactly the 15.0 observed.
`15.0` is what `UpgradeToNewWaterSystem` left behind — `min(m_Height)` over the map's
constant-depth sources. The key stays, the string now says what it is and what it is not
(`TerrainRipSystem`, `docs/bundle-schema.md` §8, `bundle.schema.json`), and the standing
instruction — never seat water on it, read the raster — is unchanged.

The method note: a "feeds only X" claim is a claim about *every* reader, so it takes a sweep of
every reader. `decomp all Game <dir>` + `rg SeaLevel` is thirty seconds and would have caught it.

---

## 3. Re-verified clean this pass (do not re-audit without cause)

Network: conform port verbatim-matches `BatchMeshHelpers` (height bounds, classification order,
epsilons, terrain source identity, sample math); tunnel-lane tagging is a verbatim
`IsNetOwnerTunnel` port; composition layer skip fired 0 times on the full city (tunnels
preserved); lane world-bit skip = 12 pipe/cable parts; 0.1 m gates = 263 lanes, matches game.
Terrain: clip band arithmetic across all measured band classes; TERRAIN-2 extras gate (drop
provably weightless, format-gated, counted); splat channel map + multiplier/power; OverrideMaterial
port + unresolved flag + clip-area exclusion (renderer-end traced, 260 records = map-editor Clip
Surfaces); water decode ≡ `WaterUtils.ToWorldSpace`; `GetHeightData` road-flattening holds;
half-texel scanline fix confirmed. Threading: no mutation-after-handoff, no double-visit, 0
duplicate ids measured over 1.27M records, clean JSONL hygiene on all normal paths; record order
unchanged. Infra: pre-clean root hard-wired to `ModsData/Carto` (cannot hit user directories);
manifest scan-the-disk honesty holds; `BundleFile` hygiene centralisation correct.

## 4. Consumer-relevant facts surfaced (feed the Phase 9 guide)

- Wire sag formula and `laneprefabs.json` fields; the tunnel-tag distinction (D1).
- LOD0 is the authored mesh by construction — consumers generate LOD chains, nothing was lost.
- Resolved `colorSet` is the game's own answer — consumers must not re-derive from seeds.
- Grass: scatter from splatmap R is game-faithful (D3).
- Building-state rules (D4). Clip lane-reference tilt (D5).
