# Ground-up validation of the ripper against the game's own implementation

Validate every piece of fork-added ripper code against CS2's decompiled source, the way the
network layer was validated on 2026-08-06 (`docs/cs2-api-audit.md` §8 is the exemplar and the
format to follow). The goal is threefold: **1:1 fidelity** (find rules the game applies that we
don't, or apply differently), **performance** (find work the game proves unnecessary), and
**hack removal** (find producer inventions — guesses, synthetic fallbacks, tuned constants —
that decompiled source can replace with the game's own rule).

## Scope

Fork-added code only — the parent Carto vector export (GeoJSON/Shapefile/GeoTIFF systems) is out
of scope. `Systems/NetworkRipSystem.cs` is already validated (§8); do not redo it, but do apply
anything you find that contradicts it. Validate, in this order:

1. **`Systems/MeshRipSystem.cs`** against `Game.Prefabs.MeshSystem`,
   `Game.Rendering.BatchInstanceSystem.UpdateObjectInstances` (sub-mesh/variant selection,
   `Require*` flags, layer masks), `Game.Rendering.BatchDataSystem.BatchDataJob.UpdateObjectData`
   (transforms, stacks via `CalculateStackSubMeshData`), and the LOD path (`LodMesh`,
   `MeshData.m_MinLod/m_LodBias`). Known latent trap to close out: §3.4 —
   `MeshMaterial.m_MaterialIndex` is a *global* library index after `MeshSystem.OnUpdate`
   processes an entity, and our positional use is only safe while we read authored buffers.
   Establish *which* entity's buffer we read and whether the game ever rewrites it, and make the
   resolution robust either way. Also validate decal flattening against
   `Game.Prefabs.DecalProperties` / `AssetImportPipeline.SetupDecalComponent`.
2. **`Systems/InstanceRipSystem.cs`** against the game's culling/instancing
   (`PreCullingData` flags, `BatchInstanceSystem`), ownership (`Owner` vs `Attached` semantics),
   stack expansion, `Game.Objects.Color` packing, tree state/growth scaling, and the
   marker/creature/vehicle exclusions.
3. **`Systems/MaterialRipSystem.cs`** against `SurfaceAsset.SyncToUnityMaterial` (which maps a
   decode actually binds, and under which keywords), the material template semantics
   (`GetTemplateMaterial`), alpha keyword handling, and the VT routes. The five-map contract and
   the decal `alphaMode: "blend"` override are recent — re-check both against source rather than
   trusting the session that wrote them.
4. **`Systems/LaneRipSystem.cs`** against `BatchInstanceSystem.UpdateLaneInstances` and the lane
   prefab data (lane sub-mesh flags include `RequireEditor` and left/right-hand-traffic bits —
   check we handle the traffic-side selection the way the game does).
5. **`Systems/AppearanceRipSystem.cs`** (colors.json / lights.json) against `ColorVariation`
   consumption in the renderer and `ProceduralLight` usage.
6. **`IO/GltfWriter.cs`, `IO/TextureWriter.cs`, `IO/Png.cs`** — no game counterpart; validate
   against their specs (glTF 2.0, PNG) and against the bundle contract instead, and look for
   perf wins in the hot paths (these dominate write time).

## Method — follow it in this order, per system

1. **Decompile first.** `tools/decomp` (see its README): `decomp type Game <FullTypeName>` for
   the counterpart system; `decomp all Game <dir>` once, then ripgrep for field readers/writers
   and archetype declarations. Never conclude anything about arithmetic from IL or from memory;
   both have produced confident wrong answers in this repo.
2. **Read the shipped bundle before theorising** (`ModsData/Carto/spike/` — see the
   `check-the-shipped-bundle-first` memory). Measure the current behaviour: counts, joins,
   per-primitive stats. The offline render harness technique (top-down textured render of a
   `.glb`, lit render with stored normals, up/down area histograms) reproduces Blender-visible
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
5. **Performance**: the rip walks the main thread on a frame budget. Compare our per-entity work
   against what the game proves is enough (lookups it caches, data it precomputes, buffers it
   reads once). Candidates: repeated `EntityManager` component reads per entity, per-vertex work
   that PieceTiling-style per-distinct-value caching could hoist, texture decode paths. Measure
   before and after with the rip report's own timings; per the `prefer-simple-over-speculative`
   memory, revert anything that doesn't measurably help, and never add main-thread cost for
   something the harness can do.
6. **Record**: extend `docs/cs2-api-audit.md` with one matrix section per system (§9, §10, …),
   update `docs/bundle-schema.md` §5.x where producer behaviour changes, update the memory files.
   Build with `dotnet build -c Release` after each system. Do not commit — the user validates
   with an in-game rip plus Blender first, then accepts.

## Constraints

- The bundle contract (`docs/bundle-schema.md`, schema 1) is frozen; behaviour changes must stay
  schema-compatible (additive `attrs`, new files, corrected geometry are fine; changed key
  meanings are not — see contract §12).
- Decide at design forks and flag the decision in the summary; don't block on questions
  (`decide-dont-ask-on-best-practice` memory).
- Normals in the network layer are a documented deliberate deviation (face-rebuilt vs the game's
  transported authored normals). If this pass builds authored-normal transport for object meshes
  anyway, revisit that deviation with it.
