# Ground-up validation of the ripper against the game's own implementation

Validate the fork-added ripper code against CS2's decompiled source, the way the network layer
was validated in `docs/cs2-api-audit.md` §8 and the full surface in §9–§15 (those sections are
the exemplar and the format to follow). The goal is threefold: **1:1 fidelity** (find rules the
game applies that we don't, or apply differently), **performance** (find work the game proves
unnecessary), and **hack removal** (find producer inventions — guesses, synthetic fallbacks,
tuned constants — that decompiled source can replace with the game's own rule).

## Scope

Fork-added code only — the parent Carto vector export (GeoJSON/Shapefile/GeoTIFF systems) is out
of scope. Two ground-up passes are already on record: **§8** (networks, 2026-08-06) and
**§9–§15** (the full-surface pass, 2026-08-06, commit `9b9776a`). **This run validates
everything changed or added since `9b9776a`** — `git diff --stat 9b9776a..HEAD -- Systems/ IO/
Domain/` is the authoritative file list; regenerate it rather than trusting the list below if
they disagree. Code unchanged since that pass (`Systems/AppearanceRipSystem.cs` §13,
`IO/TextureWriter.cs` §14) is out of scope; do not redo it, but do apply anything you find that
contradicts its verdicts.

§16–§20 are targeted investigations settled from source *during* the development being audited
(junction interior, terrain/roadbed, the wedge-paver resolution, lane-marking atlas rects,
terrain cutouts). Do not re-litigate their conclusions — but the code that landed them was
written *beside* those sections and has never been independently audited, and several fixes
still await a re-rip. Auditing the landed code against the decompiled source (not merely
against the section tables) **is** in scope.

Validate, in this order:

1. **`Systems/NetworkRipSystem.cs` + `Systems/NetworkRipSystem.GameMesh.cs`** — the layer was
   rewritten since §8: the game's `GenerateCompositionMesh` pipeline is now ported wholesale
   (parameter-space vertices, `BuildEdgeMatrix` bézier matrix pairs, the vertex shader's
   Bernstein evaluation done on the CPU), plus the zero-width intersection-piece fix (§18
   resolution — one line, in a method that was already misread once) and the junction ground
   floors. §8's matrix describes the *pre-rewrite* producer: re-audit the port line-by-line
   against the decompiled generator (`CalculateEdgeParameters` / `CalculateNodeParameters` /
   `CalculateOrphanParameters` and the generator's vertex path), re-verify each §8 rule still
   holds in the new code path, and check the deviations list in the GameMesh.cs header (wind
   sway, terrain-conform flags, rebuilt normals, roundabout three-matrix blend) is still
   complete and still counted.
2. **`Systems/TerrainRipSystem.cs`** — entirely new (Phase 6, ~1,500 lines, never audited).
   Validate against `Game.Simulation.TerrainSystem` (height cascade, `GetHeightData`
   road-flattening — §17.1), `TerrainMaterialSystem`/`TerrainRenderSystem` (splat computation,
   no-roadbed fact — §17.2), the `CullRoadsJob` flattening walk behind `StampRoadbed` (§17.3 —
   width offsets, per-node strip sets, orphan caps, smoothing aprons, elevated/tunnel clamps),
   `GenerateClipDataJob` + `TerrainSurface.RenderClipAreas` behind `StampCutout` (§20 — clip
   bands, the `ElevationLimit·3` portal/trench raises, `ClipArea` triangles), and the water
   level/surface reads against `Game.Simulation.WaterSystem`.
3. **`IO/GeometryAssetReader.cs` + the `Systems/MeshRipSystem.cs` delta** — the native
   `GeometryAsset` decode path (attribute formats: float32/float16/normalised integers,
   `MeshNormal.Unpack`) against `Colossal.IO.AssetDatabase.GeometryAsset`'s own accessors; the
   `asset.data.IsValid` gate and Unity-mesh fallback (§17.4); and `TryFlattenDecal`'s
   authored-UV → `m_TextureArea` affine rescale (§19) against `DecalProperties`,
   `ManagedBatchSystem.CreateBatch` and `AssetImportPipeline.SetupDecalComponent`.
4. **`Systems/MaterialRipSystem.cs` delta** — the VT crash fence (marker files around
   `VTSurfaceAsset.Load`): confirm from source that the fenced call is genuinely the only
   native-crash surface in the decode path and the marker protocol cannot convict the wrong
   surface; the DXT5nm two-channel normal-map reconstruction against the shader's unpack; the
   changed texture `Capture` path.
5. **`Systems/InstanceRipSystem.cs` delta** — the Pipeline/SubPipeline utility-marker exclusion
   (§17.5) against `BatchInstanceSystem.UpdateObjectInstances`' layer replacement, and the
   vehicle-cargo exclusion (`IsVehicleCargoSubObject` — cargo riding an agent is not built
   environment) against how the game parents and draws vehicle cargo.
6. **`Systems/LaneRipSystem.cs` delta + `Domain/LaneGeometryRecord.cs` + `IO/NetworkWriter.cs`**
   — the lanemeshes-layer changes since §12, including how lane records now carry the §19
   marking crops through to `lanemeshes/`.
7. **`IO/BaseSkirtBuilder.cs`** — new; no game counterpart. Validate against the bundle
   contract and against the terrain layer instead: the skirt must agree with
   `terrain/heightmap.png` at the edges it wraps.
8. **`IO/Png.cs` (16-bit rasters) and `IO/GltfWriter.cs` deltas** — no game counterpart;
   validate against their specs (PNG, glTF 2.0) and the bundle contract, and look for perf wins
   (these are in the hot write path).

## Method — follow it in this order, per system

1. **Decompile first.** `tools/decomp` (see its README): `decomp type Game <FullTypeName>` for
   the counterpart system; `decomp all Game <dir>` once, then ripgrep for field readers/writers
   and archetype declarations. Never conclude anything about arithmetic from IL or from memory;
   both have produced confident wrong answers in this repo.
2. **Read the shipped bundle before theorising** (`ModsData/Carto/spike/` — see the
   `check-the-shipped-bundle-first` memory), but note which fixes postdate it: the shipped rip
   predates the §18 wedge fix, §19 UV rescale and the cutout layer, so measured defects may
   already be fixed in code — check the diff before filing them. Measure current behaviour:
   counts, joins, per-primitive stats. The offline render harness technique (top-down textured
   render of a `.glb`, lit render with stored normals, up/down area histograms, pixel-ratio
   comparison against in-game screenshots — see the §18 method note) reproduces Blender-visible
   defects in seconds — rebuild those throwaway scripts as needed.
3. **Build the rule-by-rule matrix**: game rule → producer behaviour → verdict
   (`match` / `divergence` / `approximated` / `deliberate deviation` / `benign`). Every
   divergence gets either a fix or a counted, reported approximation — *implement rules even if
   the current city doesn't exercise them*; a producer must not shape itself to one save. Wire a
   counter into the rip report for anything approximated so a rip states how often it fired.
4. **Hunt the hacks**: grep the fork-added code for `fallback`, `guess`, `synthetic`,
   `approximat`, `heuristic`, `magic`, tuned constants, and comments admitting uncertainty. For
   each, ask whether the decompiled source names the real rule. Also check `warnings.jsonl`
   categories in the shipped bundle — every warning class is a place we punted.
4b. **Sweep for reachable-and-unread data, per domain** (the original audit's §5, re-run every
   pass — skipping it is how the splatmap gap survived §22, see §22a): for each audited system,
   list the public data the game exposes in that domain (properties, global shader bindings,
   render targets, buffers) and ask which of it the bundle does not carry. Anywhere the
   producer or a consumer *reconstructs* something (classification, blending, placement), ask
   whether the game holds the reconstructed quantity directly. And compare rendered output
   against an in-game screenshot for each layer — an appearance mismatch is a finding even
   when every implemented rule matches, because it points at data the design never shipped.
4c. **Exclusions are guilty until proven hidden and counted against the shipped bundle**
    (the §21 row 3 tunnel regression is the case study — skipping this is how every
    underground road silently lost its walls). A fix that makes the producer *stop* emitting
    something is unlike other fixes: the rule matrix has no row for absence, and its wired
    counter has never been seen firing when the fix lands. Before any new skip/gate:
    (1) trace the "never rendered" claim to the **renderer end** — the camera/batch layer
    selection (`CommonUtils.GetBoundsMask` names the world set `Default|Moving|Tunnel|Marker`;
    `ManagedBatchSystem` binds layers to cameras) — never to a flag name, a precedent verdict,
    or a layer *substitution* ("replaces Default with X" means it draws on X, not that it
    vanishes); (2) run the gate's predicate against the last full-city bundle and state in
    the audit row **what it would have dropped, by class** — "all `form=tunnel` networks" is
    a conviction (or a deliberate, flagged contract change), "a few hundred pipe runs under a
    city of thousands of edges" is what the Pipeline gates legitimately look like. A verdict
    inherited from an earlier section ("§12.2 already does this") is not evidence for either
    half — re-derive both, per the VertexSync lesson.
5. **Performance**: the rip walks the main thread on a frame budget. Compare our per-entity work
   against what the game proves is enough (lookups it caches, data it precomputes, buffers it
   reads once). Candidates: repeated `EntityManager` component reads per entity, per-vertex work
   that per-distinct-value caching could hoist, the raster stamping loops in `TerrainRipSystem`,
   texture decode paths. Measure before and after with the rip report's own timings; per the
   `prefer-simple-over-speculative` memory, revert anything that doesn't measurably help, and
   never add main-thread cost for something the harness can do.
6. **Record**: extend `docs/cs2-api-audit.md` with one matrix section per system, continuing the
   numbering after §20 (§21, §22, …); where a finding amends an existing section (e.g. a §8 rule
   the rewrite changed), annotate that section in place the way the §18 resolution did. Update
   `docs/bundle-schema.md` §5.x where producer behaviour changes, update the memory files.
   Build with `dotnet build -c Release` after each system. Do not commit — the user validates
   with an in-game rip plus Blender first, then accepts.

## Constraints

- The bundle contract (`docs/bundle-schema.md`, schema 1) is frozen; behaviour changes must stay
  schema-compatible (additive `attrs`, new files, corrected geometry are fine; changed key
  meanings are not — see contract §12).
- Decide at design forks and flag the decision in the summary; don't block on questions
  (`decide-dont-ask-on-best-practice` memory).
- Network normals are still a documented deliberate deviation (face-rebuilt vs the game's
  transported authored normals — §8 row 14, restated in the GameMesh.cs header). Object meshes
  now *do* transport authored normals via `GeometryAssetReader`, so the machinery exists:
  assess whether the port can carry authored normals through the parameter-space bend, and
  revisit the deviation if it can — otherwise re-affirm it explicitly in the new section.
