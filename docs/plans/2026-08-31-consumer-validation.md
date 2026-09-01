# Consumer validation pass — 2026-08-31

One question, asked after Phase 10 closed: **does the BeamNG consumer contain bugs that cause
visual or performance defects in the built level**, judged against BeamNG's own behaviour, the
pitfalls this repo has recorded, and performance best practice. Method: the engine's own log of
the 02:54 load of v39 (the only oracle for "what BeamNG did with our files"), vanilla level
definitions unzipped from the game, three code audits over the shape/LOD, material/texture/
terrain and network/navgraph/pipeline paths — every finding then re-read in the source and,
where possible, measured against the level state or the real bundle — and the repo's own
instruments. The working plan with the full ranked tables lives outside the repo
(`~/.claude/plans/your-job-is-to-inherited-peacock.md`); this is the record of what was found
and what landed.

## What the engine's log said

188 × `E|engine::WaterObject::initTextures| Texture missing:` (one per water piece: no
`depthGradientTex`, which every vanilla WaterBlock names) and `LevelInfo dont have defined
globalEnviromentMap` (vehicle reflections on the default sky). Static collision 53.8 M tris /
2,193 MB after Phase 10 (was 89 M / 3,720 MB). 226 tree imposters rebuilt at load after every
shape recompile — the engine's own rule, not a defect.

## Findings that were live in the level, and what landed

| # | Defect | Fix (all built, tested, level rebuilt, validator 0, audit clean) |
|---|---|---|
| A1 | A merged tile's `nulldetail` sat `detail_scale`× ABOVE its coarsest detail wherever the game's own cull was inside the view distance — every markings tile (5,794 m vs 8,000): Torque handed the band from the null's size up to LOD 0 to the null, and the tile vanished mid-range. | `tilelod.tile_levels` lowers the null under the coarsest level (declared, counted: `nullLoweredTiles` 1,114 markings / 360 areas / 173 networks); `dae.Shape` refuses any shape whose null or billboard is not under its coarsest detail (the validator cannot see it on a cooked level). |
| A2 | `WaterBlock` without `depthGradientTex`. | `stages/water` names vanilla's `depthcolor_ramp_b.png` (a game asset); log error gone by construction. |
| A3 | `clipgrid._bin_deep` skipped shallow *strips* but binned every `area` clip record; all 266 of the bundle's are shallow (median top 0.3 m), so each could delete the faces under it while `deep_touch` stayed False. Measured: contained faces 35,464 → 27,297 (8,167 faces, ~25,000 m²). Reach: the `terrain_cut: false` heightfield path and the lattice consumer — with the exact cut on, the shipped level's holes come from `corridor_faces.png` (36,502, unchanged). | per-record carve test; `areaShallow` counted in the terrainmask product (v5). |
| A4 | The 20 m box-collider guard never ran on the THIN path: `Baseball_Field__2_revised2` (138 m plane) shipped a padded slab with a 20 cm step round the outfield; a 30 m roundabout and a 28 m harbour connection the same. | thin ∧ oversized → no collider (`oversizedThin`, 3 shapes, in `NO_COLLIDER` and the report); `BOX_COLLIDER_MAX_METRES` got its first test. |
| A5 | The generated ladder's coarsest level sat at 1.5 × null, so every generated-ladder shape stopped at D_null / 1.5 while the report read D_null: `lod_detail_scale` 4 behaved as 6 and `ourCullMedian` was 1.5× optimistic on the 96 % of shapes without a billboard. | floor = null + 1 (a Torque detail's size is its stop, as `bb = null + 1` already said); `stopPx` recorded per shape and read by the report. Side effect caught by the audit (R3) and fixed: `_vegetation_imposter` tested the generated level's size, not LOD 0's, and 14 trees lost their card. |
| C7 | `LevelInfo` without `globalEnviromentMap`. | the engine's default cubemap named explicitly. |

## Latent guards (byte-identical on today's bundle; the defect returns silently without them)

B1 `plan_canonical` looked the bake's rebuilt normal up by material name in a table keyed by
path (could never hit) — reads `derived.normalRgb` now; B2 a packed (AG/RG) normal met at export
is rebuilt to RGB (`normalsUnpacked` counted; the current bundle's 29/7/40 normals are all RGB
because the producer rebuilds them); B3 the texture ledger key carries
`TEXTURE_PIPELINE_VERSION`, so an encoder fix cannot report "unchanged" and ship nothing; B4 the
building gate for generated levels is per LEVEL (`backends[i] == "meshlab"`), audit R4 reads the
same list; B5 far-ring cells go through `split_mesh` (the 60k-vertex shard cap) and the report's
duplicate `cells` key is `cellCount`; B6 `poi._patch_info` returns `(0, 0)` (a bare 0 killed the
build on a truncated `info.json`); B7 poi reads the navgraph's DecalRoads from the build's own
tree, never from disk (`roadNetworkSource` reported); B8 skirts obey `should_draw` and need
`buildings` on; B9 a segment with no mesh file is counted (`missingMesh`); B10 terrain textures
fail the stage instead of shipping a raw copy in the wrong encoding, encode failures are
separated from missing sources and fail the materials stage, a corrupt derived materials table
raises; B11 an explicit RGB neutral normal (128,128,255) instead of the height neutral; B12 a
ground with no textures is noted; B13 zip-slip guard in `modinstall.member_dest`; B14 the
`instanceColor` gamma question is declared in the instances report with its one-load test
recipe; `plant` added to `NO_COLLIDER` (a TSStatic plant asked for a Colmesh it did not have).

## Runtime cost

C1 network tiles decimate without boundary pins (a network tile shares no vertex with a
neighbour): far level 3.83 → 2.81 M triangles, networks in view 1.11 → 0.81 M — real, but
short of the 5× the pin count suggested; the cluster decimator's ratio search is the next
limit. C2 the ground-mesh base map is written at 16 px (it carried 16 pixels of information at
2048²; ~28 MiB of cooked VRAM over the grounds). C3 runtime billboards are trees only (238;
the 5 prop cards are gone; audit R9). C4 generated levels ship no vertex-colour stream.

## Instruments

E1 `tools/level_budget.py` imported a moved symbol and could not run. E2 `keep_dae: false` —
the shipping configuration — blinded four validator checks; `levelcheck.shapes.check_cooked_shapes`
now asks the shapes stage's own record (4,016 shapes, 11 s): null under the coarsest level,
bindings against the level's `mapTo` set, collider vs `collisionType`. E3 a `fatal` class of
problem (the map.lua:459 crash) raises out of `validate` — v6's changelog had promised it. E4
`gameCullMetres` carries the authored LOD bias `gamePx` already had. E5 `budget.detail_at`
selects as `lod_audit.select` does (null and billboard in `shape_ladders.json`; pinned equal
over 2,100 cases). E7 two stale comments. The estimator's ground-texture ratio is scored and
learnt like the mesh ratios (it had drifted 32 %).

## The level now (v43 shapes, 16:36 build)

Validator 0 problems; audit R1–R9 clean; budget inside 4 km: cs2 3.14 M, forest 0.82 M,
networks 0.81 M, ring 0.15 M, areas 0.12 M, markings 0.09 M — 5.13 M total. Draw distances by
`stopPx`: buildings 2,499 m (the ring hand-over), props 298 m, trees 196 m to the card.

## Open, deliberately

* **Naomi's look**: markings tiles at 40–120 m (no more pop), a shoreline (depth gradient),
  the baseball field edge (no kerb), props' vanish distance against v40 (1.5× nearer than the
  old effective scale — raise `lod_detail_scale_prop` if wanted), roads at 2–4 km.
* C5 network `castShadows` (Phase 10 declined; refine to ground-form-only with a `cs2diag`
  number). C6 sRGB-correct mips and renormalised normal mips (a visual change on every texture;
  lands with B3's key after a look). B14 needs one load to measure.
* D1–D6 build-pipeline items (silhouette payload through the pipe, per-variant re-parse,
  splat/water float64, texture double decodes, alias pickling, write_materials churn) — only
  if a build is felt to be slow.
* The markings tiles now draw to the game's own cull, not `detail_scale`× nearer — declared in
  `tilelod`; the alternative (pull the anchor in) would have pulled LOD 0 in with it.

## Follow-up, same day ("do the rest")

**Wave 1 — build performance, verified byte-identical on a forced rebuild (0 of 18,585 art/ +
main/ files changed size):** the ring silhouettes ride per-shape `.npz` files
(`cs2beamng_state/farring/`, 1,170 files / 1.4 MiB compressed) instead of Python lists through
the pool pipe and `shapes.state.json` (15.4 of its 19.7 MiB; the JSON shrinks on the next real
shapes run — a replayed stage keeps its recorded rows, which `farring.Ring.add` still accepts
inline); a one-entry parse memo covers a prefab's paint variants; the water stage builds its
coarse grid once and hoists the band index; one decode per texture source and a header-first
`_has_alpha`; `parallel.each` grew `initializer`/`initargs` and the alias table / materials
catalogue stopped riding every argument tuple (networks, derived, bake lods);
`write_materials` byte-compares; the navgraph progress overshoot, the census `terrain_cut`
key and the second `networks.jsonl` pass in navread are gone. The estimator's ground-texture
ratio is now measured per build (`groundShippedMiB` → `estimatescore.observe`); the default
and its test moved to the measured 0.965 (the `> 1.0` assertion described the pre-Phase-10
world). **Declined with a measurement:** rewriting `splat.dominant_layer` to a running-best
(saves ~1 s and ~400 MB transient) flips 20 of 16.8 M texels on float near-ties — a
byte-visible change for a negligible win.

**Wave 2 — the two look-affecting items:** C5 — a network tile of flat carriageway (every
segment `form: normal`) stops casting shadows; any tile holding an `elevated` or `tunnel`
segment keeps the pass. On the reference city that is **650 of 1,024 tiles out of the shadow
pass, 374 kept** (the Phase 10 decline, refined per segment). C6 — sRGB albedo mips are
averaged in linear light (a black/white checker's first mip is now code 188, not 128) and BC5
normal mips are renormalised as vectors (no more flattened relief at distance), behind
`TEXTURE_PIPELINE_VERSION 3`, so all 11,063 textures re-encoded exactly once. Build green:
validator 0, audit R1–R9 clean.

**Still open:** B14 (one in-game load: a known-`colorSet` building vs its CS2 colour), the
C1 residual (the network far level stops at 2.81 M against a 0.8 M target — the cluster
decimator's per-run floor, not the pins), and the D-items declined above. The look items for
Naomi are unchanged, plus: bridge shadows still present / flat roads no longer self-shadowing,
and distant texture colour (mips are slightly brighter and truer now).

## Naomi's first look at the pass (2026-08-31 evening) — three reports, three fixes

Her load's log was clean (the 188 water errors GONE - A2 works), `LevelInfo` ships 8 km, and
the instrument showed everything drawn at 8 km - so all three were render-side:

1. **"Square fog covering the borders" / "view distance cut in half"** - vanilla's
   `depthcolor_ramp_b.png` (the A2 ramp) has a PALE TEAL shallow end, and the sea's coastal
   level-band WaterBlocks (only the largest band becomes the infinite plane; the rest are
   literal boxes along the coast) went from sampling a missing (black) ramp to milky teal:
   fog-like squares along the map borders that walled off the view across the bay. Fix: the
   ramp is OURS now - `stages/water._depth_ramp` writes `art/water/depthcolor_ramp.png` as
   Beer-Lambert over `depthGradientMax` from the bundle's own scattering colour and
   `absorptionDistance` (water v3). The A2 no-missing-texture fix stays.
2. **"Black patches under houses"** - the A1 fix made the mid/far bands of paint tiles VISIBLE
   (they used to vanish where the null tied a level), and what showed there was the
   vertex-CLUSTERED far level of alpha-masked paint. Paint is never decimated now: a
   markings/areas tile's levels are the game's own culled subsets, exact (`PAINT_PRODUCTS`,
   derived v12).
3. **"A large field just grey"** - a modded terrain-painting surface (`TTP Moss/Grass`,
   `Terrain01/02`, `I8MOSS01F`): texture-less BY DESIGN (CS2 paints the terrain's own ground
   through them; there is nothing to rip), so it fell to the grey placeholder. Surfaced BY NAME
   onto the bundle's own ground albedo (moss/grass -> grass, terrain/soil -> dirt, materials
   v10, counted as `areaGroundFallbacks`); `Missing Area` and unknown names keep the grey,
   which is the game's own fallback colour.

## Reverted (2026-09-01 02:16)

Everything after the Wave-3 build — the dock "fixes" (areasclip straddler split, the concrete
stand-in, the producer's `MinTerrainHeightOverCell`) and water v4–v7 (phantom-body drop, box
refinement, enclosed drop, the no-plane flood-safe cover) — is reverted (91d3d9e, d9ee679,
d8a6eb7) at Naomi's word: "this has NOTHING to do with the water. It's created some weird
blocks of water in the ocean. These things are the AREAS. a black reflective surface and the
dock is a grey concrete area." Water is back to v3 (our depth ramp, WaterPlane sea, 200-block
budget); the mod DLL is rebuilt and deployed on the reverted producer.

The lesson is the diagnosis, not the code: the black squares were never confirmed to be water
objects before two nights of water theory. Next step, if she wants one: identify the object
class at the screenshot position from the level's own `items.level.json` (an area tile with a
black reflective material is a MATERIAL symptom — a missing/black albedo with specular), and
touch nothing else until that is confirmed.
