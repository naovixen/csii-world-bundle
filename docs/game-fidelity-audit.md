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

### 2.2 Documentation work-list (Phase 9 / audit amendments)

| # | Item |
|---|---|
| D1 | Consumer caveat: lane `tunnel` tag (`cmin<0`; 3,214 records) and segment `form=tunnel` (deep −2/−3·ElevationLimit; 309) are **different game predicates** — never equate them. One cross-reference sentence in §5B. |
| D2 | Document as deliberate exclusions: road-name label rendering, city-boundary fence, VFX placements (names + the `"VFX " + prefab` convention for consumers who substitute), backdrop water status. |
| D3 | Fix §4.3 grass wording: the game scatters a FoliageVFX layer *from the splatmap's grass channel* — consumer-side scatter from channel R is game-faithful. |
| D4 | Building-state consumer rules: under-construction buildings are *hidden* by the game (crane/fence entities ship as normal instances); condemned has no rendering consumer; destroyed pose/rubble not shipped. |
| D5 | §8c: clip lane reference is the mean of boundary y (game lerps laterally — band tilts on superelevated roads); spans >0.1 m with clip-count 0 draw nothing (§39 rule covers markings). Kind sub-toggles are destructive re-rips (unlike layer toggles). `InstanceWriter` stale compression comment. §7.2 lights example/position claims (with P8). |
| D6 | Water level-grid + one-cell dilation resample: deliberate deviation re-affirmed this pass. Backdrop terrain honest-omission log line documented (see 1a). |

### 2.3 Non-goals for user sign-off (N)

- VFX capture beyond doc note (P-side `vfx.json` possible; recommended: doc-only).
- Road-name labels / city-boundary fence as capture targets (recommended: doc-only, D2).
- Backdrop water simulation (heavier; sea-level + water params ship via P4 — recommended: defer, keep §29 open).
- Snow/wetness/wind/waves/season-live state: remain excluded (established contract).

### 2.4 Open verification (rides Phase 13 acceptance)

- **V1**: one in-game look at a storage yard / specialized-industry field to confirm Lot-painted
  areas arrive as `Surface` sub-areas (vanilla farm/forestry paint does; the areas query requires
  `Game.Areas.Surface`, and `LotPrefab` archetypes add `Lot`).
- The §38 clamp and §21 sync-extrapolation remain shader-side claims resting on the accepted
  in-game A/Bs; nothing reopened.

## 2.5 Post-rip findings (2026-08-10, the Phase 3 validation rip)

The rip that validated Phase 3 also surfaced two things no offline check could have caught.

### SPLAT-1 — the splat compositing rule is wrong, and it is why painted ground reads green (consumer-side, HIGH)

**Symptom** (user, in Blender): terrain painted a yellow sand colour near the map edges renders
as a lighter green.

**The data is not lost.** The producer ships the paint correctly: `extra1` is non-zero on 15.07%
of the splatmap and `extra2` on 12.76%, both at full strength, and those masks match
`user_splatmap.png`'s two channels texel-for-texel. All five ground textures ship
(`grass`/`dirt`/`rock`/`extra1`/`extra2`, plus normals).

**The consumer rule erases it.** Applying the documented rule (§8: composite base → G → R → B → A,
each at `opacity = saturate(weight · splatMultiplier)^splatPower`, published as 2.0 / 1.0) to the
shipped raster gives:

| visible ground | share of map |
|---|---|
| grass | **92.31%** |
| extra2 | 4.07% |
| rock | 2.82% |
| extra1 (the painted sand) | **0.81%** |
| dirt | **0.00% — never visible anywhere** |

The mechanism is arithmetic: `splatMultiplier = 2.0` makes any channel ≥ 128/255 fully opaque,
and the grass channel is ≥ 128 on **96.99%** of texels (≥ 240 on 88.53%). Since grass composites
*after* dirt, dirt can never show; the extras, mostly below 30, paint over grass at ~16% opacity
and vanish into it.

**Why the current reading of R must be wrong.** The grass channel averages 243.6/255 across the
whole map and sits at **249.6 below the waterline**, where there is certainly no grass. A channel
that is near-saturated underwater is not a grass *opacity*. Its own recorded provenance — "falls
monotonically with slope while every other channel is 0 on unpainted ground" — fits a
**grass-versus-rock blend factor** (`ground = lerp(rock, grass, R)`, rock being `1 − R` rather
than a residual of all four), which would leave dirt and the paint channels free to composite on
top. That is a hypothesis, not a verdict: `G` was never measured directly either (§8's own
provenance says "by elimination"), so the whole channel semantics need re-deriving against
in-game screenshots before any rule is written into the consumer guide.

This supersedes nothing in §33 — it *reopens* it. §33's rule was fitted from measurement and is
now falsified by a case it did not cover.

**RESOLVED the same day.** The hypothesis held: `R` falls monotonically with slope on unpainted
ground (254/255 at 0–5°, 5.9/255 at 40–90°), which is blend-weight behaviour, not opacity. The
rule that fits every measurement splits the channels by kind — `R`/`G` normalised with rock as
the residual of *those two alone*, then `B`/`A` composited over as opacities:

| model | painted `extra1` texels | whole map |
|---|---|---|
| all-four-as-opacities (shipped until now) | 0.81% paint | 92.31% grass, 0% dirt |
| all-four-as-partition | 47.7% paint vs 32.1% grass | 77.3% grass |
| **split by kind (adopted)** | **86% paint** (extra2 texels: 100%) | 71.8% grass, 14.1% dirt, 6.7%+5.3% paint, 2.1% rock |

Implemented in `tools/blender_import_bundle.py` (`_splat_ground_material`) and rewritten into
contract §8. The node graph was re-simulated offline and reproduces the table exactly.

### SPLAT-2 — the brown terrain was a view transform, not the blend (consumer-side, HIGH)

The visual check (user screenshots, in-game vs Blender, same orientation) showed the whole
landmass rendering **brown** in Blender against **green with sand beaches** in-game. Measured
over the land pixels of each: in-game hue **63.9°** sat 0.38 val 0.40; Blender hue **41.6°** sat
0.23 val 0.31.

That is not the blend rule. Composing the shipped splatmap against the shipped ground textures
offline predicts hue **75.6°** under the old rule and **70.9°** under the new one — both green,
both near the in-game 63.9°. Neither predicts 41.6°.

The cause is downstream of every pixel this project ships: `_GROUND_GAIN` (8.0) and
`_GROUND_SATURATION` (2.0) were calibrated by A/B renders that pin `view_transform = "Standard"`
(`tools/ab_render.py:33`), but the importer never pinned it, so an interactive session inherits
Blender's default — **AgX** since 4.0. AgX rolls bright colour off toward a desaturated warm
neutral, and an 8× gain lands the ground squarely in that rolloff: authored grass (sRGB
(67,74,53), hue 80°) comes out desaturated brown, and the tonal compression reads as the
"flipped lights and darks" the report described. Fixed by pinning Standard in the importer
(`_pin_view_transform`, called unconditionally including under `--keep`).

**Lesson for the consumer guide:** the bundle ships *albedo*, not scene-referred HDR. Any
consumer applying a presentation gain must state the display transform it was calibrated
against, or the same trap fires in their engine.

**SPLAT-1 itself is still unproven against the game** — the brown masked it. The next import
tests both at once: painted sand should read as sand *and* the landmass as green.

### WATER-2 — `rasters.water.seaLevel` is not the playable water datum (producer, LOW)

The new §P4 block captured `seaLevel: 15.0` while this map's actual water surface measures
508.6–528.3 m (median 521.0) and the game's own `kDefaultSeaLevel` is 511.7. `WaterSystem.SeaLevel`
feeds only `ResetBackdropWaterToSeaLevel` — backdrop/ocean water — so the field's semantics string
("WaterSystem.SeaLevel in world metres", implying the playable datum) is at best misleading and at
worst reading an uninitialised value. `terrain/water_level.png` remains the authoritative water
surface and is unaffected. Fix the semantics or drop the key; do not let a consumer seat water on it.

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
