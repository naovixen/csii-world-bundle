# Game-parity sweep — validate every area against CS2's own implementation

**Why this plan exists.** Between BeamNG loads #11 and #17 the terrain cut was "fixed" seven times
by reasoning about screenshots: containment scoring, a hole-rim snap, a sub-cut, a cliff lift, rim
skirts, a band lowering, a margin ring. Every one closed one artifact and opened the next. On
2026-08-17 Naomi stopped it — *"take a pause and validate against the game and replicate how it does
it exactly, this inventing and guessing has us going around in loops"* — and thirty minutes in the
decompiled `Game.Simulation.TerrainSystem` produced the actual rule (a stencil prism), showed the
**producer had matched it all along**, and replaced all seven inventions with one exact cut that
agrees with the game's own predicate at 99.98–100 % (audit §46).

That is the pattern to repeat everywhere. This plan is the checklist.

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

### 1. Terrain materials and the splat — IN PROGRESS
- **Game:** `Game.Rendering.TerrainMaterialSystem` (`UpdateSplatmap`: height cascade + water +
  noise + user brush + `_SplatRockLimit`/`_SplatGrassLimit`; ground roster rock/dirt/grass + 4
  extras), `TerrainRenderSystem`, the terrain shader's own tier blend.
- **Known so far (2026-08-17):** the game paints **no roadbed** on terrain — the splatmap never
  sees net/lane/area data (verified: zero such references in the whole 1,505-line system). Our
  `surface_index == 1` stamp was an invention and drew a staircase of squares along every road
  edge; removed. BeamNG's own terrain shader adds macro/detail tiers as `(2t − 1)·factor`, so the
  same dark CS2 albedo in all three tiers rendered `extra2` at (0, 6, 0) black; tiers are now
  centred on mid-gray.
- **OPEN 1a — the boundary dither (load #19, confirmed by Naomi).** The per-sample material INDEX
  is `splat.dominant_layer`'s argmax, but the game blends the six grounds continuously per pixel.
  Where two weights are near-equal the argmax flips sample to sample and BeamNG draws a literal
  checkerboard of whole squares, each fully one ground. Read the composite the game actually
  renders (`COMPOSITE_ORDER` and the terrain shader's own blend) and decide what a
  one-index-per-sample engine should store — the argmax of a SMOOTHED weight field is the obvious
  candidate, but measure it against a render before shipping it.
  A second, concrete suspect for the same symptom: BeamNG blends adjacent terrain materials using
  their **height maps**, and we ship a flat 128 gray for every material at every tier
  (`NEUTRAL_MAPS`, `height{tier}Tex`) — there is nothing for the blend to work with, where every
  vanilla level ships real ones. Check what BeamNG's terrain shader does with `heightTex` before
  assuming which of the two causes the checkerboard; it may be both.
  Also open: `_SplatRockLimit`/`_SplatGrassLimit`/noise mean the game's classification is partly
  procedural and height/slope-driven — does the shipped splatmap already bake that in, or are we
  missing the classifier?
- **OPEN 1b — the ground meshes do not look like the terrain beside them (load #19).** Every
  `terraincut` piece wears `<level>_ground_<layer>`, a single texture at
  `terraincut.GROUND_UV_M` = 40 m per repeat composited from the ground's tiers; the terrain beside
  it renders base+macro+detail with distance attenuation and a 4 m detail tier. Close to the camera
  the mesh is a flat pale blur against richly textured ground, which is the "square blocks around
  every cutout" Naomi photographed — the cut geometry is right, the material is not. Options to
  weigh AFTER reading how BeamNG composes the terrain tiers: give the mesh material its own
  detail-scale second UV layer and a matching blend, or drive the mesh from the same three textures
  at the same three tilings. This is presentation parity between two of OUR materials, so the
  game's role here is only to say what the terrain should look like.

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
