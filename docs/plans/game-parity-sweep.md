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
  `.ter` — **768 MB against 192 MB today**. **Decide whether that trade is worth making — it is a
  size/memory question for Naomi, not a parity question.** Meanwhile §48.4 ships the one
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
- **Filed, not built:** the extras' `_TerrainExtra*HeightOffset` / `DirtOverride` are the one
  producer gap §47 found; they change a paint's softness, not its place.
- **HYPOTHESIS for area #4:** the ground **albedo**'s alpha (means 9–34/255) is a plausible
  smoothness, which would replace the `roughness` neutral 217. Nothing in the game's source names
  that channel — settle it there, from the shader, not from the correlation.

### 2. Terrain flattening, smoothing and the road interface
- **Game:** `CullRoadsJob` (the `ShiftTerrain` sections, `m_MinOffset`/`m_MaxOffset`,
  `middleSize`, `NetUtils.GetTerrainSmoothingWidth`), `GetHeightData`.
- **Ask:** our `ground_heights` is a 0.15 m roadbed sink plus a shallow-band clamp — both TUNED
  CONSTANTS with no counterpart in the game. What does the game actually do to the heightmap under
  and beside a road, at what width, with what falloff? The shipped `heightmap.png` is already
  flattened, so the question is whether the consumer should be adjusting it at all.
- **Suspect:** `ROADBED_SINK = 0.15` is documented as "TUNED" and a "FALLBACK … it guesses a
  constant against a road surface it never looks at". That is exactly the shape of the inventions
  §46 removed.

### 3. Network composition meshes
- **Game:** `NetCompositionHelpers.GenerateCompositionMesh`, `CalculateEdgeParameters` /
  `CalculateNodeParameters` / `CalculateOrphanParameters`, the vertex shader's Bernstein evaluation.
- **Status:** ported wholesale and audited at §8/§45, but the deviations list in
  `NetworkRipSystem.GameMesh.cs`'s header (wind sway, terrain-conform flags, rebuilt normals, the
  roundabout three-matrix blend) should be re-derived rather than trusted, and the roundabout port
  is on record as **unexercised by the reference save**.
- **Ask specifically:** authored normals. Object meshes now transport them; networks still rebuild
  per face (a documented deliberate deviation). Can the parameter-space bend carry them?

### 4. Materials: the CS2 shader → PBR mapping
- **Game:** the surface shaders' own inputs, `TerrainMaterialPropertiesData`, decal properties.
- **Ask:** contract §7.1 says mask G = ambient occlusion; that is a HYPOTHESIS and it is already
  suspect — 67 % of masks have an all-zero G, which blacked out walls and tunnels until the
  materials stage stopped binding AO below a mean of 96/255. Read the shader's own channel use and
  settle it. Then: metallic/roughness packing, `colorMask`/`colorSet` tinting, alpha modes,
  emissive, and what a decal's blend mode really is.

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
