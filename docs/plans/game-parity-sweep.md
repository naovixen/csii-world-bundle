# Game-parity sweep — validate every area against CS2's own implementation

**Why this plan exists.** Between BeamNG loads #11 and #17 the terrain cut was "fixed" seven times
by reasoning about screenshots: containment scoring, a hole-rim snap, a sub-cut, a cliff lift, rim
skirts, a band lowering, a margin ring. Every one closed one artifact and opened the next. On
2026-08-17 Naomi stopped it — *"take a pause and validate against the game and replicate how it does
it exactly, this inventing and guessing has us going around in loops"* — and thirty minutes in the
decompiled `Game.Simulation.TerrainSystem` produced the actual rule (a stencil prism), showed the
**producer had matched it all along**, and replaced all seven inventions with one exact cut that
agrees with the game's own predicate at 99.98–100 % (audit §46).

That is the pattern to repeat everywhere. This plan is the checklist — the standing backlog of
areas, worked whenever there is room.

**It is also a per-phase obligation.** `docs/plans/beamng-consumer-plan.md` gives every phase from
**A3 onwards** a *parity gate* that must pass before that phase's in-game load: name and read the
game's counterpart system, write the rule matrix into `docs/cs2-api-audit.md`, build a samplable
predicate where the output admits one, count what stays approximated, and delete what the game's
rule replaces. Parity work a phase cannot finish lands here rather than being dropped.

## The method, per area

1. **Decompile first, read second.** `tools/decomp` (`decomp all Game <dir>` once, then ripgrep;
   `decomp type Game <FullTypeName>` for one system). Never conclude arithmetic from IL, from a
   field name, or from one of our own docs — three of our claims have failed a re-check, including
   one in this very sweep (§17's "no roadbed" was right, and re-verifying it took two minutes).
2. **Write the rule matrix**: game rule → our behaviour → verdict (`match` / `divergence` /
   `approximated` / `deliberate deviation`). One row per rule, in `docs/cs2-api-audit.md`,
   continuing the section numbering.
3. **Separate producer from consumer.** Ask "does the bundle carry what the game uses?" before
   "does the consumer use it right?". §46's answer was that the producer was already exact and
   every defect was consumer-side; the opposite is equally possible elsewhere.
4. **Build a predicate you can sample.** The strongest validation is a function that answers the
   game's own question at a point, compared with what we produce — see
   `tools/check_terrain_vs_game.py` (is this point inside a clip prism? vs does the level draw
   ground here?). Aim for one per area; a number beats a screenshot.
5. **Gate it.** A unit gate for the rule (`tools/test_terrainclip.py` is the model: it includes
   point-sample agreement with the game's predicate) and a counted report line for every
   approximation that remains.
6. **Delete what the rule replaces.** An approximation left in the tree beside the exact rule is
   the next session's confusion.

## The areas, in priority order

Priority is "how visible is the divergence" × "how likely we invented something".

### 1. Terrain materials and the splat — 1a/1b WORKED 2026-08-17 (audit §47, corrected by §48 and §49); two residuals open
- **Game:** `Game.Rendering.TerrainMaterialSystem` (`UpdateSplatmap`: height cascade + water +
  noise + user brush + `_SplatRockLimit`/`_SplatGrassLimit`; ground roster rock/dirt/grass + 4
  extras), `TerrainRenderSystem`, the terrain shader's own tier blend.
- **Known so far (2026-08-17):** the game paints **no roadbed** on terrain — the splatmap never
  sees net/lane/area data (verified: zero such references in the whole 1,505-line system). Our
  `surface_index == 1` stamp was an invention and drew a staircase of squares along every road
  edge; removed. BeamNG's own terrain shader adds macro/detail tiers as `(2t − 1)·factor`, so the
  same dark CS2 albedo in all three tiers rendered `extra2` at (0, 6, 0) black; tiers are now
  centred on mid-gray.
- **1a — WORKED, and the leading candidate was falsified (audit §47).** Three questions closed:
  the game's classifier is procedural but runs on the GPU and we rip its **output**, so there is
  nothing to port; the **argmax dither is NOT the checkerboard** (0.07 % of texels have their top
  two shares within 0.02; 0.148 % are isolated) and smoothing it would delete a mix the game
  actually paints — do not; the **height maps were the real gap** and the bundle already carried
  them (the alpha of `<ground>_normal.png`, measured), so both consumers were discarding CS2's own
  relief and filling `height{tier}Tex` with a flat 128. Fixed, gated at
  `tools/test_terrain_blend.py` 25/25.
- **OPEN 1a-residual — our layer texel is 1.75 m and BeamNG's constants assume ~0.5 m.** Rendering
  the ported shader over the real index raster reproduces load #19's photograph exactly. Every
  softening mechanism BeamNG has is sized in layer texels — domain warp ±0.5 texel at a 0.77-texel
  period, soft band 0.245 texel — and vanilla levels run `squareSize` 0.3–0.75 m against the
  **1.75 m we already ship** (`terrain_upsample = 2` is the default), so all of it renders
  2.3–5.8× oversize as metre-scale blobs of whole ground. No material setting reaches it
  (`TERRAIN_LAYER_WARP` is already on; `heightRange`/`warpAmp` are shader literals) and the `.ter`
  layer array shares the heightmap's grid, so the only lever is heightfield resolution — and
  `terrain_upsample` has **no headroom left**: already at 2, and refused on a fine rip because the
  fine map *is* 1.75 m. The next step is 0.875 m, the game's own next cascade slice, at a 16384²
  `.ter` — **768 MB against 192 MB today**. **ANSWERED IN §52 (2026-08-17), and it is now the one
  lever behind THREE of this plan's open items** (this blob residual, §50's grey fringe and
  §51.8's road clipping — all sized in layer texels or in height detail, all halving together).
  Supported but tight: 14336 / 0.875 = **16384 = exactly D3D12's max 2D texture dimension**, so it
  is the finest grid the API can express and nothing below it is reachable; no vanilla level goes
  above 4096 and we already ship 8192; `TerrainBlock` load is superlinear (0.8 s @4096 → 10.2 s
  @8192); and 768 MiB of terrain textures lands on an 8 GB RTX 3070 already at 6.7 GiB idle whose
  load #9 failure WAS VRAM exhaustion. It would be real data, not an upsample — `baseLod+2` is
  0.875 m over a quarter of the playable area, 16 camera windows instead of 4 (producer + re-rip);
  an upsample costs the same at runtime and buys two of the three. **Recommended: not now.** Meanwhile §48.4 ships the one
  deliberately NON-PARITY lever in the app, `terrain_blend_sharpen` (default 1.0 = faithful),
  which dissolves the blobs into sub-structural interlock rather than removing them. It is
  labelled as such in `settings.py`, under a `NOT PARITY` banner in `stages/terrain.py`, and in
  its own counted report line.
- **§48 corrected two of §47's own answers — read it before trusting the rows above.** (1) The
  tier ENCODING was doubling every deviation: BeamNG's `base + (2t − 1)·f` equals CS2's
  `lerp(base, T, f)` only when the tier is written `0.5 + (T − base)/2`, and we wrote
  `T − mean + 0.5`. That inflated the albedo's contrast 2.4× and clamped **15.8 % of `extra2` to
  black** — the green-black blotching of load #20. (2) Because a strength is then a lerp weight,
  the tier strengths ARE portable and §47's "no game quantity maps onto them" approximation is
  deleted: `detail = 1 − middle`, `macro = middle·(1 − far)`, both off the one ramp
  `[0, midStart, midEnd, farEnd]`.
- **1b — WORKED (audit §47.5), then REGRESSED and fixed properly in §48.3.** §47 put the game's
  tilings into a single pre-composited 40 m texture, which took a 10 m cut piece from 2.50 detail
  periods to 0.75 — the flat pale stepped slabs of load #20. The mesh now uses BeamNG's own
  `detailMap`/`detailScale`/`detailBaseColorMapStrength` (the same `blendDetail` arithmetic), so
  the near tier sits at the ground's exact period with no rounding at all.
- **§49 — the cut meshes took FOUR stacked defects to fix, and Naomi confirmed the look on load
  #26.** Each hid the next: the material sat in `art/terrains/*.materials.json`, where no vanilla
  level puts a plain `Material` (191 TerrainMaterial / 0 Material) so it never bound — flat grey;
  `*TexSize` is in LAYER TEXELS not metres, so **the whole terrain drew 1.75x too coarse** and the
  mesh's true-metre UVs were 1.75x off it; the textures carried no `.color`/`.data`/`.normal`
  suffix, so the albedo was decoded as linear (measured (174,179,166) against the terrain's
  (119,133,81), predicted (180,190,152)); and the mesh's base slot held the full-contrast albedo
  where the detail carries a deviation from the mean, doubling contrast and clamping **32.5 % of
  extra2 to black**. Gate `tools/test_terrain_blend.py` 47/47.
- **OPEN §49.6 — the polygon edges.** The last structural difference: terrain blends up to four
  grounds PER PIXEL, an ordinary mesh draws one per triangle, and 27 % of the rim is within two
  squares of a ground boundary. The faithful fix is verified in BeamNG's source and content but not
  built: `Stages` is a list, `defaultMat.hlsl` blends stage N over N-1 by `inLayer.opacity`, and
  vanilla ships 1,073 four-stage materials and 38 driving later stages from an `opacityMap`. Bake
  per-tile opacity masks from the same splat weights the terrain blends.
- **Rejected, and gone from the tree (§49.5):** `terrain_holes: majority` (over-cuts up to 1.75 m
  past the game's line — Naomi saw it) and `terrain_rim: lower` (lowering the rim to the band floor
  — *"that's a fidelity loss"*, and correct: the game hides with a stencil and never moves ground).
- **OPEN BUG §50 — the grey rim around every cutout. Proven, understood, unfixed.** BeamNG's
  terrain blends **material slot 0** into every pixel beside a hole: a `.ter` layer byte is the
  material index OR the 255 hole marker, so punching erases the material, and
  `matId = min(31, matId)` then reads past the material buffer onto texture-array slice 0. Proven
  in game by painting the seven terrain materials identifying colours — the grey came back as
  `rock`'s colour, which was index 0. We hit it ~35× harder than vanilla (34,566 m² of hole-adjacent
  terrain against Utah's 876 m²) because our holes are open corridors, not tunnel mouths. **Tried
  and reverted:** reordering the list so slot 0 is the commonest rim ground — right in the 55 % that
  is grass, worse everywhere else, *"looks awful in areas that are not grass"*. **Not tried:**
  padding the list to 32 so slot 31 exists and authoring it with a zero height map (an engine rule,
  `calcHeightBlend`, rather than a preference). ~10 % of the grey is genuine rock and would remain.
- **Corrected while chasing that bug:** §47's "the extras' `_TerrainExtra*HeightOffset` /
  `DirtOverride` are the one producer gap" is WRONG — all four scalars for all four extras have
  been in `rasters.terrainTiling.extras` since the tiling capture landed (extra4: `dirtOverride`
  0.48, `splatRange` 0.16, `blurDepth` 0.04, `heightOffset` 0.07). Consuming them is open consumer
  work, needs no re-rip, and is NOT what caused the grey.
- **HYPOTHESIS for area #4:** the ground **albedo**'s alpha (means 9–34/255) is a plausible
  smoothness, which would replace the `roughness` neutral 217. Nothing in the game's source names
  that channel — settle it there, from the shader, not from the correlation.

### 2. Terrain flattening, smoothing and the road interface — WORKED 2026-08-17 (audit §51); one producer residual open
- **Game:** `CullRoadsJob` (the `ShiftTerrain` sections, `m_MinOffset`/`m_MaxOffset`,
  `middleSize`, `NetUtils.GetTerrainSmoothingWidth`), `GetHeightData`.
- **Answered, and the suspicion was right twice over.** The game does not only clip at a road, it
  **grades**: `AddSegment` emits a `LaneSection` whose min/max offsets are the composition's own
  `(edgeHeight, surfaceHeight.min, edgeHeight)` triple, `DrawHeightAdjustments` renders those into
  `m_HeightmapCascade`, and `UpdateGPUReadback` reads that cascade back into `m_CPUHeights` —
  which is what `GetHeightData()` returns and the producer ships. For an at-grade road min == max,
  so the height is **assigned**, and `heightmap.png` arrives already graded. The apron is 8 m
  (`GetTerrainSmoothingWidth`; 20 m waterway, 100 m taxiway). **The answer to "should the consumer
  be adjusting it at all" is no.**
- **Both constants deleted.** `ROADBED_SINK = 0.15` moved 3.27M samples (10.0 km²), 29.8 % of them
  under no band at all; `clamp_shallow_bands` fired on **100 %** of covered samples — the band test
  requires `terrain > lane + bottom`, so clamping to `lane + bottom` always selects — landing them
  0.25 m below where CS2 grades. Together 3.28M samples moved, median 0.250 m, **2.4 million m³**
  dug out from under the city's roads: load #9's *"annoying to drive off the road"*.
- **Predicate:** `tools/check_roadgrade_vs_game.py` recovers `lane`/`cmin`/`cmax` per sample from
  the shipped bands (inverting the producer's own ±0.3 expansion) and scores the height against the
  game's profile window — **98.37 %** agreement on the 1.75 m grid (2,307,777 samples), 97.10 % on
  the 3.5 m base. Gate: `tools/test_roadgrade.py`.
- **Approximation that remains, counted:** the 1.17 % (fine grid) standing above the carriageway's
  own cross-section — pixels the game's stencil discards and a heightfield cannot.
  `clamp_carriageway` lowers exactly those onto `lane + top − 0.3`, the game's own number, only
  ever downward. The finer grid halves the residual (1.76 % → 1.17 %), now gated.
- **Load #21 (audit §51.7/§51.8): the residual is NOT a missing field, and no re-rip fixes it.**
  Naomi: *"curbs are much less annoying … large improvement. Small issues where the road now clips
  ever so slightly through the terrain where the roads are not flat / curved."* Measured:
  `m_MinHeightOffset` would remove 5 % of the excess volume (§51.5's naming of it is superseded);
  the residual rate is FLAT across sag/bend/crossfall, so curvature consumes the margin rather than
  causing the defect; and the carriageway interior already sits at p95 **−0.005 m** of the game's
  own `cmin`, i.e. exactly where CS2 graded it.
- **REJECTED, and gone from the tree (§51.8): the profile-aware ceiling.** Evaluating the game's
  lateral profile per sample takes the residual to zero on paper, but 85 % of what it moves
  (65,639 of 77,067 samples) lies inside `[cmin, cmax]` — heights CS2 legitimately writes somewhere
  on that cross-section — and the only thing calling them wrong is our estimate of the lateral
  position, which is weakest at the rim where the clamp bites. `cmax` is the tightest bound correct
  data cannot violate; the profile is tighter only if `u` is right. Shipping the triple does not
  help either: evaluating the profile still needs `u`.
- **OPEN — and §50 is NOT the blocker; that was my over-claim, corrected in §50.8.** The game
  STENCILS the shallow band, so there is no terrain under a road at all and nothing can clip
  through it, and we need no new data to cut shallow ones. But scoring §50's fix first (rather than
  building it) showed the route is worse than the artifact: 7.07 km² of new holes means §50's
  0.88 m fringe along **every road edge in the city**, and a punched face at a node is sky because
  the clip fan reaches past the road mesh — a second blocker §50 does not touch. **The one lever
  that reaches all three of these is terrain resolution** (the 1a-residual decision): BeamNG's warp
  is ±0.5 TEXEL, the asphalt→kerb ramp is smeared over ~one texel, and the blend constants are
  2.3–5.8× oversize — all three halve together at 0.875 m.

### 3. Network composition meshes — WORKED 2026-08-17 (audit §53); no geometry change, one residual open
- **Game:** `BatchMeshHelpers+GenerateBatchMeshJob.GenerateCompositionMesh`,
  `BatchDataHelpers.Calculate{Edge,Node,Orphan}Parameters`, `BatchInstanceSystem.UpdateNetInstances`,
  `NetProperty`, the vertex shader's Bernstein evaluation.
- **The port is exact, and that was re-derived rather than inherited.** Every branch of the
  per-vertex map, all six scale/bias pairs, both cut planes, the V-density accumulation, the
  `HalfLength` flip doubling (which squashes normals as well as positions), the height-bounds
  slicing, and all three curve builders control-point by control-point — **no arithmetic
  divergence**. §8's and §21's game-rule rows all still hold in the current code.
- **Three of the four listed deviations were wrong.** Terrain-conform flags and authored normals
  were already closed (§34/§38 and audit #40 P6) and the plan text had simply not caught up — the
  port reads `MeshNormal`/`MeshTangent`, applies `SmoothTopNormal`, and rotates by the Jacobian of
  the placing map, cofactor for the normal and plain for the tangent. **"Wind sway" never existed
  at all:** `NetProperty` hands a composition batch eight matrices, four syncs and three colours,
  and no wind. What survives: the roundabout `0x80` three-matrix blend (shader-side, counted), the
  orthonormalization choice (shader-side, unknowable from C#), and the two `UV0.zw` inputs below.
- **Hack removed:** `crossZ`/`crossZ2`/`crossZ3` were the game's `UV0.z` cross-anchor, computed
  correctly in all three branches and **assigned to nothing**; `UV0.w` was never computed. Both are
  now a comment naming what they are, and the `else if (roundabout)` branch is kept deliberately
  empty because being an `else if` is what stops a `HalfLength` piece in a roundabout from halving.
- **Predicate:** `tools/test_net_composition.py` gates the generator's `saturate` — a composition
  can neither overrun nor fall short of `m_Width`. Centre-band median **1.0000**, p95 1.0068, floor
  0.9982 over 449 straight segments, 96.2 % within 1 %; the excess at wider bands falls
  monotonically, which is the node flares (a different, wider composition) and not a mapping error.
  Carries its own negative control, per `test_node_fan.py`.
- **Cross-area finding, filed to #4:** the game scales the ENTIRE hanging-lane cubic by local wind
  SPEED, so in dead calm a wire is straight; our "calm-air drop" is really the shape at wind 1
  (comment corrected). And `_WindRangeLvlB`/`_WindElasticityLvlB` are authored material properties
  we do not ship.
- **OPEN, and deliberately not built:** every approximation counter (`_roundaboutParts`,
  `_pipelineCompositions`, `_rebuiltFramePieces`, …) is rip-report-only, so no offline pass can say
  whether the roundabout branch ever fired. Surfacing them in `warnings.jsonl` needs schema 3.

### 4. Materials: the CS2 shader → PBR mapping — WORKED 2026-08-17 (audit §54); consumer-only, no re-rip
- **Game:** `Colossal.AssetPipeline.Constants.Material`, `TextureImportAttribute.Mode`,
  `TerrainMaterialSystem.CreateFallbackNormalTexture`, `RenderedArea`, `DecalProperties`.
- **The shader cannot be read, and that is the finding.** Constants names the complete world-asset
  texture set but never a channel; the templates are Colossal's own (`Default`, `Windows`,
  `VegLeaves`, `NetCompositionMeshLit`, `DefaultDecal`), not stock HDRP Lit, so the HDRP packing is
  an assumption not an inheritance; the install ships no shader source; the modding toolchain is a
  stub. `_AmbientOcclusionMap` exists only on the **character** path.
- **`mask.G` IS NOT AMBIENT OCCLUSION — settled by measuring all 890 masks.** G mean **9/255** with
  91 % of texels exactly zero. R (mean 34, 82 % at an extreme) is metallic and A (mean 98,
  populated everywhere, uncorrelated with the rest) is smoothness, exactly as §7.1 had them. Of the
  18 masks clearing the old 96/255 gate, nine are uniformly white (a no-op) and nine are bimodal
  0/255 stencils; only **2 of 890 (0.22 %)** read as AO at all, both near-white. G is not the
  emissive mask either (IoU 0.009); G and B correlate at +0.64 with nothing else and their meaning
  is **recorded as unknown** rather than guessed a fourth time.
- **Deleted:** `AO_MIN_MEAN`/`_usable_ao` (a per-texture gate that did no useful work and read
  every mask off disk to decide), the `ambientOcclusionMap` binding, and the bake's `_ao.png`
  split (890 PNG writes; materials product version 1 → 2). The rule moved into
  `tools/csiibundle/materials.py` so both consumers get it.
- **Predicate:** `tools/test_mask_channels.py` — two structural clauses plus the measurement re-run
  against whatever bundle is on disk, with a negative control that must classify a synthesised AO
  map AS AO.
- **§47 corrected from source:** `CreateFallbackNormalTexture` builds the neutral ground map as
  RGBA **(128, 128, 255, 128)** — RGB is a genuine flat tangent normal, and the alpha's neutral is
  the **midpoint**, which is a height map and not smoothness. §47's correlation-based reading is
  confirmed.
- **OPEN, filed not built:** `_WindRangeLvlB`/`_WindElasticityLvlB` are authored material
  properties we never ship (animation only); `DecalProperties.m_RendererPriority` decides which of
  two overlapping decals wins and is shipped for areas but not for object decals; `RenderedArea`'s
  four mask/normal blend scalars are unswept.

### 5. Lighting, exposure and colour
- **Game:** HDRP volume stack, `LightingSystem`, the tonemapper, `grading.json`'s own source.
- **Ask:** our colour chain is documented as one derivation riding an OCIO view
  ([[colour-chain-lives-in-colour-model]]), and the ground albedo gain in the BeamNG app
  (`GRASS_TARGET_LUMINANCE = 0.16`, `MAX_GAIN = 6`) is a calibration, not a rule. What does the
  game's exposure actually do, and is the gain compensating for something we should be reading?

### 6. Water
- **Game:** `Game.Simulation.WaterSystem`, `WaterRenderSystem`, `WaterRenderSettingsPrefab`.
- **Ask:** levels, the shoreline dilation, waves, and whether the depth-based appearance we ship is
  the one the game computes.

### 7. Instances: trees, props, buildings
- **Game:** `BatchInstanceSystem`, `ObjectGeometrySystem`, tree growth/season state, `RenderedArea`.
- **Ask:** LOD selection thresholds, tree state → mesh choice (153,991 trees once drew as adults),
  sub-object composition, and the colour-variation path.

### 8. Areas and markings
- **Game:** area projection, `LaneMarkings`, the decal projector.
- **Status:** §8b/§39 settled the projection and the texture repeat; re-check the atlas rects and
  the lift ladder against source rather than against our own notes.

## Working agreements (from CLAUDE.md's non-negotiables)

- The game is the oracle; the Blender harness is a consumer with the same licence to be wrong.
- A rule with no counterpart in the game's source is a hack with a countdown on it.
- Every approximation is declared where it is made and counted in the report.
- A fix only one consumer gets is in the wrong place — shared rules live in `tools/csiibundle/`.
- Producer changes need a re-rip before they can be called validated; say "built, awaiting re-rip".
