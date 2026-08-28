# Full-scale refactor — producer, consumers, documentation

## Context

The repo has grown to ~118k lines of source and ~27k lines of markdown across a C# CS2 mod
(the producer) and two Python consumers, built feature-first over 420 commits. The architecture
is sound — both halves have real stage abstractions, every Python module has a docstring, JSONL
reading streams properly — but nothing has ever enforced size, consistency or duplication limits,
and there is no linter, formatter, type-checker, test runner or line-count gate anywhere in the
repo. The result, across **187 fork-owned files and 75,145 lines**: 57 files exceed the 400-line
ceiling, 248 functions exceed 60 lines (121 of them exceed 100), the same rules are implemented
two and three times across the consumers, and every "live plan" pointer in the documentation
points at a closed document.

Three audits (producer C#, consumer Python, docs/build/CI) measured this. They also turned up
**three live defects**, which is why the refactor starts by fixing them rather than moving code
around them.

The outcome this targets: every fork-owned file under 400 lines with one clear responsibility;
one implementation of every shared rule, in `csiibundle`; consistent naming and structure enforced
by CI rather than by discipline; measured performance that is equal or better; and a documentation
set of small, standalone, single-purpose docs that a new developer can actually start from.

### Decisions taken

- **Upstream Carto code is not touched.** ~30k lines across ~60 files (`NetworkSystem`,
  `POISystem`, `BuildingSystem`, `RouteSystem`, `AreaSystem`, `ZoningSystem`, `RasterSystem`,
  `SharedDataCollectionSystem`, `IO/GeoJson|Shapefile|GeoTiff|IO|Options|Enums|FieldInfo|
  BufferManager|TagManager`, most of `Domain/`, all of `Geodata/` and `Utils/`) are exempt from
  every rule below — including the 364-line `#if false` block in `NetworkSystem.cs:2963`, which
  stays. `Settings.cs`, `Mod.cs` and `Instance.cs` are **mixed**: they count as upstream, so only
  fork-added members inside them are in scope.
- **Feature work is frozen** for the duration. `docs/plans/a6-fidelity.md`, including A6.0 (the
  in-game GPU out-of-memory), waits.
- **The two `§NN` audits get split by subsystem**, with all 754 code references rewritten.

### Scope

**Every fork-owned file is in scope — 187 files, 75,145 lines.** The 400-line ceiling is one gate
among several and happens to bind on 57 of them; it is not the scope.

| | Files | Lines |
| --- | ---: | ---: |
| Fork C# — over 400 | 14 | 18,122 |
| Fork C# — under 400 | 21 | 3,034 |
| Python / JS / CSS — over 400 | 43 | 33,340 |
| Python / JS / CSS — under 400 | 109 | 20,649 |
| **Total** | **187** | **75,145** |

A further 21 files sit in the 300–400 band and cross the line under any growth.

**Small files are held to the same principles, and several carry the worst violations.** The
three duplicate JSONL writers are 92, 64 and 157 lines (Phase 8). `IO/BundlePrecision.cs` is 65
lines of which 36 are comment and 24 are code. `csiibundle/frames.py`, `beamng/frame.py` and
`bakeopts.py` are all small and all named in Phases 2 and 3. Every one of the 12 never-called
public functions and the permanently-false `hasattr` guard lives in a small module. Function
length (≤ 60), naming, type hints, comment discipline, dead code and duplication are enforced
across all 187 files; only the file-length gate distinguishes them.

## The principles this is judged against

These are the standard for the whole refactor. Every phase declares which it serves, every phase
is assessed against **all ten** before it lands, and Phase 12 writes them into the repo as
`docs/coding-standards.md` so they outlive this plan. Where a principle can be machine-checked it
is; where it cannot, the audit's named findings are the closing checklist — the principle is not
satisfied until its baseline list is empty or each survivor is a declared, counted exception.

| # | Principle | What it means here | Assessed by | Baseline today |
| --- | --- | --- | --- | --- |
| 1 | **DRY** | One implementation of every rule. Shared rules live in `csiibundle` or a shared C# helper; both consumers call the same function. | Named-findings checklist per phase; import-graph check | 4 transform impls, 3 PNG-decode wrappers, 5 IHDR parsers, 4 sRGB curves, 3 `resample_nearest` (one disagreeing by half a texel), 4 rip-run skeletons, 3 JSONL writers, 10 `GradingWriter` captures, 2 stage frameworks, 8 copies of the parallel-work block |
| 2 | **KISS** | Functions ≤ 60 lines, one level of abstraction each. No type carrying two mutually exclusive contracts. | `check_limits.py` | 248 functions > 60 (121 > 100); `BuildGameComposition()` 627, `main()` 522, `check()` 490; the `Stage` union at `RipOrchestratorSystem.cs:95` |
| 3 | **YAGNI** | No unused public API, no dead branches, no flags never flipped, no back-compat shims. | `ruff` F401/ARG + a callers-check script; named list | 12 never-called public functions, `BundleFile.OnDisk`, 2 unused `EntityQuery` fields, a permanently-false `hasattr`, `level_budget.py`, `Settings.SetDefaults`' 147 duplicate defaults |
| 4 | **SOLID — SRP** | One reason to change per file and per class. | `check_limits.py` + per-phase review against the audit's seam list | `settings.py` holds 6 responsibilities; `TerrainRipSystem` writes 3 non-terrain layers; `stages/materials.py` is a stage *and* the alias registry |
| 5 | **SOLID — DIP / ISP** | The engine-neutral layer must not know its consumers. A stage must not import another stage's privates. | Import-graph check in CI | `bake/ground.py:39` defaults to a BeamNG measurement; `colour.py:686` is a Blender-only method on a neutral class; `mapview.py:262` uses `stages.poi` privates; `estimate.py:458` documents a cycle instead of breaking it |
| 6 | **Separation of concerns** | Presentation out of pipeline, engine knowledge out of `csiibundle`, IO out of collection. | Import-graph check + review | `settings.py` is a view model every worker imports; `server.py` owns 9 caches and 5 background computations; 6 bundle layers bypass the IO layer entirely |
| 7 | **Meaningful naming** | One name per concept, no decompiler artifacts, no unexplained abbreviations. A glossary in `docs/coding-standards.md` is the arbiter. | `ruff` naming rules + glossary review | 8 names for ground height, 8 for "placed object", 5 for prefab id, `colour`/`color` inside one module, `num7`/`num8`/`num10` |
| 8 | **Self-explanatory code** | Comments carry rules and measurements, never restatement. Derivations live in docs; declared approximations stay in code. | Per-file comment-density measurement + the preservation list in Rule 4 | C# 24–31 % vs Python 7–10 %; `Instance.cs` 63 %; 22 copies of `/// Mod's logger.` |
| 9 | **Consistent structure** | One return-shape convention, one docstring style, one field convention, one JSON idiom, one test style. | `ruff` + review | 5 return shapes and no `@dataclass`; 4 docstring styles; 3 C# field conventions inside one `partial class`; 4 JSON idioms; **12 `check()` signatures across 32 definitions** with the argument order reversed between them (re-measured 2026-08-28; the earlier figure of 5 was low); 2 test styles and neither is pytest, 122 `test_*()` functions never collected, no JS test runner at all |
| 10 | **Efficiency, measured** | Vectorized over per-element loops; streamed/chunked over whole-file; no redundant passes. **Profile before optimizing, and never ship a regression.** | The Phase-0 profile table, re-run every phase | 1.27 M-record Python loops run 3–4× per build; `png.py` makes 6 copies of a raster; `write_objects()` runs 16× per build; zero Burst/jobs in the ripper; the network edge set walked 3× per rip |

**Performance is a gate on every phase, not only Phase 10.** Any phase whose build or rip timings
regress beyond run-to-run noise does not land until the regression is explained or reverted.

## Rules that govern every phase

1. **Behaviour-preserving.** No move changes what the producer writes or what a level build
   produces. A defect found mid-move is logged and fixed as its own commit, never folded in.
2. **Profile before optimizing.** Phase 0 records baselines; nothing lands as an optimization
   without a before/after number from the same instrument.
3. **The ceiling is a CI gate, not discipline.** Files ≤ 400 lines, functions ≤ 60, with an
   explicit committed exemption list (upstream + generated files) that doubles as the
   don't-touch boundary.
4. **Comment discipline is not knowledge deletion.** Both audits independently found that the
   dense comments are a *measured-game-rule archive*, not noise — ~20 blocks in the Python and
   ~10 in the C# are load-bearing (the `rotationMatrix` three-measurement derivation at
   `beamng/frame.py:16`, the `HEIGHT_SIGMA` algebra at `stages/terrain.py:393`, the
   diagonal-vs-bilinear measurement at `drape.py:88`, the `NOT PARITY` fence at
   `stages/terrain.py:445`). These move with their code verbatim. What gets deleted is boilerplate
   that restates the code — 22 copies of `/// Mod's logger.`, 17 of `/// The event triggered when
   the system instance is destroyed.`, and `Instance.cs`-style 63 %-comment density.
5. **Producer changes cannot be validated by us.** All producer work batches into one block and
   ends with a single request for an in-game re-rip, diffed against the Phase-0 golden manifest.
   Until then the wording is "built, awaiting in-game re-rip".
6. **Consumer changes end with a rebuild of `cs2_city_fine` in place**, `cook: true`, settings
   taken from its own `cs2beamng_report.json`, handed over before the commit.

---

## Phase 0 — Guardrails and baselines

**Principles served:** all ten — this phase is what makes them checkable.

No source moves. Everything after this depends on being able to prove nothing broke.

- **Golden baselines.** `tools/bundle_manifest.py` writes a sha256-per-file + per-layer
  record-count digest of the current bundle (a 6.5 GB copy is unnecessary; the digest is
  diffable and cheap). The bundle on disk is valid for this — ripped 2026-08-20 09:10–09:37 with
  no `Systems/ IO/ Domain/` commit since. Also capture the current level's
  `cs2beamng_report.json` and `cs2beamng budget` numbers.
- **`upstream-files.txt`** — derived once from git authorship, committed. The exemption list and
  the don't-touch boundary in one artifact.
- **Lint and format.** `ruff` (lint + format) configured in `pyproject.toml`; `.editorconfig`;
  C# analyzers (`Nullable`, `EnforceCodeStyleInBuild`, warnings-as-errors) scoped to fork files.
- **`tools/check_limits.py`** — fails on any non-exempt file > 400 lines or function > 60, across
  **all 187 fork files**. Wired into CI. Seeded with a shrinking allowlist so it goes green
  immediately and tightens each phase; entries are only ever removed. Today it would report 57
  files and 121 functions.
- **pytest.** A `conftest.py` with a `bundle_dir` fixture (replacing 35 `sys.path.insert`
  bootstraps and 20 hard-coded `%USERPROFILE%\…\ModsData\Carto` paths, 7 of which are unrunnable
  off Windows) and a `needs_rip` marker for the 15 tests that require a real rip. The 28
  hand-rolled `check()` helpers across 5 incompatible signatures — two with reversed argument
  order (`test_lod.py:20`, `test_navgraph_map.py:40`) — become plain asserts. **CI runs `pytest`
  once, so all 31 test files run instead of the 13 named in `ci.yml`.**
- **Producer gets its first automated check.** A `Carto.Tests` project links the five fork files
  that are already game-free — `IO/Png.cs`, `IO/BundleWriter.cs`, `IO/BundleManifest.cs`,
  `IO/BundleFile.cs`, `Domain/RipWarning.cs` — via `<Compile Include>`, so it builds without the
  game's assemblies and CI can run it. Extend to the `Unity.Mathematics`-only files
  (`GltfWriter`, `BundlePrecision`, `BaseSkirtBuilder`) if the NuGet package satisfies them.
  Add `Systems/`, `IO/`, `Domain/` to `ci.yml`'s `paths:` filter — today a producer change
  cannot fail any check, including compilation.
- **Baseline profile table.** Rip stage seconds from the last report; build stage seconds;
  `cProfile` over a bake; `level_budget` resident bytes and triangles. This is the only
  admissible evidence in Phase 10.

## Phase 1 — Fix the three defects the audit found

**Principles served:** 10 (a correctness precondition: every later move must be provably behaviour-preserving).

Each its own commit, with the evidence, before any code moves.

1. **The app never applies the §4.2a decal lift.** `csiibundle/instances.py:415 instance_matrix`
   exists precisely so this cannot be missed — its docstring says "a forgotten decal lift
   flickers" — and `apps/cs2beamng/stages/instances.py:311 _transform` reimplements the transform
   in plain Python without it. `grep placement_lift apps/` returns nothing. The same
   reimplementation also renormalises the quaternion, silently defeating the diagnostic property
   `frames.quat_matrix:87` deliberately preserves. Fix: the app calls the shared function.
2. **`bake/roads.py:28` still filters the region as a circle.** Every other stage uses a square;
   `stages/networks.py:74` carries the post-mortem of exactly this bug ("a square of buildings and
   a disc of roads, and the corners of the area had houses and no streets"). The fix landed on one
   side only, so on a region build the road *surface* markings and skirts drape against is still
   a disc.
3. **`NetworkRipSystem` can hang the main thread.** It uses a bounded(8) `BlockingCollection` with
   plain blocking `Add` at `:530, :700, :791, :1547` and has **zero** `IsFaulted` checks — the
   exact shape `MeshRipSystem.cs:744-751` documents as wrong ("a writer task that has faulted
   never will [make room] … the reader could block the main thread forever"). It also has no
   per-entity try/catch in `Advance`, so one throwing edge kills the stage. Adopt
   `MeshRipSystem.Enqueue`'s `TryAdd` + fault check.

Also fix here, being latent rather than live: `RipProgress.Pump()` is called only from
`MeshRipSystem.OnUpdate:134` and `NetworkRipSystem.OnUpdate:403`, so the progress indicator
freezes on any plan without those stages.

---

## Consumer (validates in ~7 min per rebuild — do this first)

## Phase 2 — One implementation of every shared rule

**Principles served:** 1 DRY, 3 YAGNI, 5 DIP/ISP, 6 separation of concerns.

CLAUDE.md's "a fix that only one consumer gets is in the wrong place" applied literally.

- **Transform math** — collapse onto `csiibundle/frames.py` + `instances.py`. Delete
  `stages/instances.py:311 _transform`/`_swap_yz`/`_quat_rows`, `stages/lights.py:328`'s import
  of those privates, and the nested-comprehension 3×3 product at `stages/lights.py:334`. The
  bare axis-swap exists in five places (`frames.py:151`, `frames.py:162` — dead and byte-identical
  to the previous, `beamng/frame.py:39`, `:44`, and inline at two stages).
- **PNG decode** — one wrapper with the PIL fallback, replacing three
  (`groundmap.py:74 read_rgb`, `beamng/materials.py:81 _load_any`, `stages/terrain.py:480
  _read_alpha`) and one IHDR reader replacing five.
- **sRGB transfer** — one pair. Today there are four: the piecewise curve in `colour.py:149`,
  a numpy re-derivation in `bake/materials.py:85`, plain `** 2.2` gamma doing the same job in
  `stages/terrain.py:529/645/665`, and a fourth in `probe_point.py:83`.
- **`resample_nearest`** — three copies with a **half-texel disagreement**: `reader.py:191` centres
  samples (`+ 0.5`, with a §8 justification), `bake/materials.py:117` and
  `measure_paint_factor.py:50` do not.
- **`_tint_multiplier` / `_best_factor`** — `measure_paint_factor.py:58,68` are verbatim copies of
  `bake/materials.py:375,385`.
- **Height sampling** — `beamng/ter.py:119 sample_height` is bilinear, and it is what
  `validate_level.py:900` uses to check that objects sit on the ground. `drape.py:88` states
  outright that bilinear "answers a different question from the one the rendered mesh answers, by
  a measured p95 of 9.4 cm at 3.5 m cells and 41 cm at a 14 m stride." Either the validator uses
  the diagonal-aware sample or the deviation is declared and counted.
- **"First mesh state on disk"** — three implementations (`stages/shapes.py:210`,
  `stages/instances.py:294`, `pipeline/meshindex.py:169`) walking the same candidates and citing
  the same §6.1.
- **Bundle location** — `bake/locate.py:36` handles Windows/Proton/macOS and is used by three
  modules; 20 other sites hard-code the path in two different spellings.
- **Engine knowledge out of the neutral layer** — `bake/ground.py:39 MEASURED_DIAGONAL = "ne_sw"`
  is a *BeamNG* measurement serving as the default for every consumer;
  `collision.py:25 MIN_THICKNESS` and `:157 wind_vertex_colours` are BeamNG conventions;
  `colour.py:686 Grading.write_ocio` is a Blender-only method on a neutral class. These become
  parameters supplied by the consumer, or move into the consumer.
- **Fix the inverted imports** — `pipeline/mapview.py:262-271` reaches into `stages.poi`'s
  privates; three stages import two other stages as libraries; `pipeline/estimate.py:458` has a
  comment acknowledging a cycle instead of breaking it.
- **Delete the dead surface (YAGNI), almost all of it in small modules.** Twelve public functions
  that nothing calls, in a package with exactly two consumers: `frames.py:162 zup_to_bundle_point`
  (body **byte-identical** to `bundle_to_zup_point:151`, kept "for readability at a call site"
  that does not exist), `instances.py:406 is_paint` (docstring: "so a future paint-like kind is
  added here once"), `areas.py:195 is_flat`, `decals.py:82 instance_priorities`,
  `lod.py:188 area_triangle_min_lod`, `materials.py:247 roughness_from_smoothness`,
  `tints.py:60 is_tintable`, `water.py:213 is_water_material`, `lanegraph.py:130 has_flag`,
  `reader.py:293 load_colors` + `:313 load_grading`, `bake/tiles.py:265 tile_of`,
  `beamng/dds.py:107 mip_count`. Plus `stages/scene.py:105`, where
  `hasattr(colour_mod, "blackbody")` is permanently false — `blackbody` does not exist — so the
  line is a constant wearing a function call. Also `validate_level.py:914-925`, two wrappers that
  exist only to un-privatise two functions, and the unused `ceiling` parameter threaded through
  three layers of the height protocol.
- **Promote the privates that three modules already share by importing them** —
  `bake/skirts.py:23` and `bake/lanegeometry.py:34` import `_in_region`, `_material_specs` and
  `_texture_uri_base` out of `bake/markings.py`; `_in_region` is separately reimplemented at
  `stages/navgraph.py:187`, and `_collision_policy` exists twice (`stages/instances.py:398`,
  `stages/shapes.py:768`).

## Phase 3 — One stage framework, not two

**Principles served:** 1 DRY, 2 KISS, 4 SRP, 9 consistent structure.

`stages/__init__.py:43 Stage` + `pipeline/worker.py:39` and `bake/__init__.py:43 Product` +
`bake/runner.py:88` are structurally isomorphic: both are
`(name, declared inputs, version, run(ctx, progress)) → stats`, both fingerprint inputs, both
persist after every unit, both catch `Cancelled` separately. `pipeline/state.py:6` already
acknowledges half of it and reuses the bake's fingerprint primitives. Unify the record, the
registry, the context, the outcome type and the option-declaration mechanism
(`Product.options` vs `Stage.settings_keys` solve the same problem in different vocabulary).

Then extract the copy-pasted bodies: the `run()` preamble/epilogue present in 11 stages, the
`ctx.shared` null-guard in 6, the **parallel-work block copy-pasted 8 times** (4 stages + 4 bake
products), and the TSStatic literal built by hand in 3 places. Collapse the stage-name string
lists that live in 5 places (`stages/__init__.py:87`, `stages/bake.py:98`,
`pipeline/history.py:19`, `server.py:63 CENSUS_KEYS`, `bake/__init__.py:59`).

## Phase 4 — Split `csiibundle` and `tools/` to ≤ 400

**Principles served:** 2 KISS, 4 SRP, 6 separation of concerns.

Seams named by the audit, not invented:

- `csiibundle/terrain.py` (1272) → clip rasterisation + morphology (join `terrainclip.py`, which
  already holds the prism half), lattice/mesh generation, carriageway rules, ground tiling, water,
  hole mask.
- `csiibundle/navgraph.py` (1138) → reading, centerline derivation, junction synthesis,
  stacked-road clearance, polyline maths (beside `bezier.py`), reporting.
- `csiibundle/colour.py` (874) → transfer functions, the HDRP grading port, OCIO. **This is the
  largest module in the repo with no test at all** — add one here.
- `bake/materials.py` (770), `decimate.py` (644), `instances.py` (573), `drape.py` (571),
  `glb.py` (503), `materials.py` (500), `lanes.py` (416).
- `tools/validate_bundle.py` (1372) → its JSON-Schema subset evaluator (`:57-252`) is a general
  library; `validate_bundle()` at `:415` is a **338-line function**; the `_check_*` family already
  has the right shape and becomes a registry.
- The four oversized test files, which split naturally under pytest.
- `tools/probe_point.py` (645) — drop its private colour, PNG and `Bundle` reimplementations.

## Phase 5 — The app and its UI

**Principles served:** 1 DRY, 2 KISS, 4 SRP, 6 separation of concerns, 9 consistent structure.

- **`settings.py` (1247) — the worst SRP violation in the repo.** One setting's facts are spread
  across **14 parallel dictionaries** all keyed by setting name (`SCHEMA` 79, `LABELS` 79,
  `BLURBS` 79, `DETAILS` 35, `UNITS` 23, `SUBSET_OF` 15, `CONTROLS` 9, `DRIVEN_BY` 5, `NEEDS` 3,
  `DRIVES` 3, `RANGES` 3, `CHOICE_SETS` 2, `ZERO_LABEL` 2, `PARENT_OFF` 1) — plus `SECTIONS` and
  `GROUPS`, two independent total partitions of the same key set kept in sync only by a test.
  Adding a setting means touching up to 16 tables and nothing catches a miss. Replace with one
  `Setting` record per key; derive every table. Then split: schema, view model, persistence and
  presets, coercion/validation (`validate:1092` and `region_of:1104` both parse the region
  string), and the OS discovery block (`:1112-1208`, including a Steam `.vdf` parser that
  duplicates `bake/locate.py`'s).
  Keep `state.Recorder`'s undeclared-key gate — it is a genuinely good design.
- **`server.py` (771)** — `AppServer` owns nine mutable cache fields and runs the mesh index, the
  cost estimate, the map render, the census and the stage plan on background threads. Split the
  HTTP shell (`_Handler`'s two `if/elif` chains over 23 routes → a route table) from the
  application-state cache. Move `pipeline/mapview.py` out of `pipeline/` — it is UI rendering.
- **`ui/app.js` (1666)** — one IIFE, 71 functions. Split into ES modules along the seams already
  visible: transport/SSE, settings rendering, the cost meter, the map/region/POI controls, census,
  log. `ui/app.css` (576) splits with them.
- **`stages/shapes.py` (1097), `beamng/validate_level.py` (925)** — the latter's `check()` is a
  **490-line function**; its helpers below it are already the natural per-check units, the same
  registry shape `validate_bundle.py` gets in Phase 4.
- **`stages/terrain.py` (904)** — the ~290-line texture-authoring block is an independent concern.
  The `# ---- NOT PARITY` fence at `:445-466` survives verbatim; it is the model for how every
  declared approximation should be marked.
- `stages/instances.py` (590), `pipeline/estimate.py` (576), `stages/materials.py` (539),
  `pipeline/runner.py` (488), `beamng/budget.py` (487), and the six files in the 400–450 band.

## Phase 6 — The Blender adapter

**Principles served:** 1 DRY, 2 KISS, 4 SRP, 6 separation of concerns.

`tools/blender_import_bundle.py` (4142) is the single largest file in the repo. It is already a
thin caller of `csiibundle` for frames and reading; what remains is Blender-specific and splits
into: material node-graph authoring (~700 lines), lane geometry (~600), terrain + splat (~900),
areas, scene/lighting/grading, the glTF fast-path importer, and the survey/statistics tool (which
is a separate tool, not an importer). `main()` is **522 lines**. `blender_import_level.py` (540)
loses its `read_dae()` — a COLLADA reader for the exact format `beamng/dae.py` writes, in a
different package with no shared schema — and stops putting `apps/` on `sys.path`.

---

## Producer (validates only by an in-game rip — one block, one rip)

## Phase 7 — One rip-stage framework

**Principles served:** 1 DRY, 2 KISS, 4 SRP, 9 consistent structure.

The run skeleton is copy-pasted four times across `InstanceRipSystem`, `LaneRipSystem`,
`MeshRipSystem`, `NetworkRipSystem` — `BeginRip`/`Advance`/`ProcessOne`/`Finish`/`Fail`/`Abandon`,
each with its own `*Run` state class. `Advance` differs between `LaneRipSystem.cs:224` and
`InstanceRipSystem.cs:498` only in a type name and two nouns, comments included verbatim; `Fail`
is near-byte-identical. There are **three different back-pressure schemes** for one problem, one
of which is the documented-wrong one (Phase 1). Extract `Systems/Rip/RipStage.cs` carrying the
frame budget, the bounded queue with `TryAdd` + fault check, the per-entity try/catch, the writer
task, progress reporting and pumping, warning declaration, and the completion-callback-or-dialog
shape repeated at 16 sites. Replace the `FAILED:` string protocol — asserted in
`RipOrchestratorSystem.cs:641` and produced independently at 12 sites, where a stage writing
"Failed" is silently a success — with a typed outcome. Split the `Stage` union
(`RipOrchestratorSystem.cs:95`), which carries two mutually exclusive contracts in one type with
no compiler help.

Extract the shared helpers: `Digest` (identical in `MeshRipSystem.cs:2378` and
`MaterialRipSystem.cs:1756`, the second's comment admitting the copy), `SanitizeAssetId` /
`BuildAssetIds` (a `public static` policy module used by five systems, living inside
`MeshRipSystem`), `DescribeComponents` (identical in two systems), and `Tessellate` (two divergent
sampling policies for the same bézier, `LaneRipSystem.cs:627` and `NetworkRipSystem.cs:2079`).

## Phase 8 — The IO writer layer

**Principles served:** 1 DRY, 3 YAGNI, 4 SRP, 6 separation of concerns, 9 consistent structure.

`InstanceWriter`, `LaneWriter` and `NetworkWriter` are the same class three times — their
docstrings say so (`LaneWriter.cs:12`: "The same shape as `NetworkWriter` and for the same
reason"). This is the exact signal `IO/BundlePrecision.cs:16` already acted on one level down
("A constant whose copies have to describe each other wants to be one constant"). Give them a
`JsonLinesWriter` base and one line-builder. Bring in the **six bundle layers that bypass the IO
layer entirely** and are written inline with `BundleFile.CreateText` + `JsonConvert`
(`districts.jsonl`, `areas.jsonl`, `clipvolumes.jsonl` in `TerrainRipSystem`; `laneprefabs.json`
in `NetworkRipSystem`; `materials.json`; `colors.json`/`lights.json`).

Collapse `GeometryAssetReader.ReadFloat3/2/4` (`:460/484/508`) — the same 20-line body three
times, the later two carrying `/// <inheritdoc>` as an acknowledgement — into one generic. Collapse
`GradingWriter`'s ten copies of one capture shape into a table, and move the multi-sentence
`["units"]` prose out of the shipped payload into the docs. Settle on one JSON idiom for fork code
(`BundleWriter` alone uses three) and rename its lowercase POCO properties.

Delete: `BundleFile.OnDisk` (`:30`, an identity function with zero callers — residue of the
reverted COMP-1 experiment), `MeshRipSystem`'s `_subMeshPrefabQuery` and `_placedBuildingQuery`
(declared and built, never read), and `NetworkRipSystem.SanitizeId:1827` (a one-line alias whose
own comment explains an indirection nobody needed).

## Phase 9 — Split the rip systems to ≤ 400

**Principles served:** 2 KISS, 4 SRP, 7 naming, 9 consistent structure.

By collaborator, not by slicing into `partial` — `partial` only where ECS demands it.

- **`TerrainRipSystem.cs` (3379)** — `RipTerrain()` is a **331-line** god-method sequencing 13
  unrelated captures (with duplicated `4b`/`4c` section labels at `:351/380` and `:361/385`).
  Split into: sequencing; `TerrainRasterCapture` (the seven `Capture*` methods); `RoadbedStamper`
  and `ClipVolumeCollector` (the `Stamp*` family — a self-contained CPU rasteriser over béziers
  with no terrain-writer dependency); `WaterLevelBuilder`; and **`AreaRipSystem`** —
  `RipSurfaceAreas` (461 lines) and `RipDistricts` write `areas.jsonl` and `districts.jsonl`
  and have nothing to do with terrain.
- **`MeshRipSystem.cs` (2390)** — run lifecycle, writer task, `SubMeshSelector` (the game's rule
  port), `PrefabMeshReader`, and the asset-id policy that Phase 7 extracts.
- **`NetworkRipSystem.cs` (2367) + `.GameMesh.cs` (1348)** — `Finish()` is ~290 lines of report
  building; `NetworkRun` is a ~70-field mutable bag mixing three stages; `RipLaneGeometry` +
  `GetLanePrefab` (~410 lines) are a structurally separate stage writing a separate layer, so they
  become `LaneGeometryRipSystem`. `BuildGameComposition()` at `.GameMesh.cs:561` is **627 lines**
  — the single longest method in the repo — and becomes a `CompositionMeshBuilder`. The two halves
  of this one `partial class` currently use **opposite field-naming conventions** (`_run` vs
  `m_ConformProbes`); settle on one.
- `InstanceRipSystem.cs` (1772), `MaterialRipSystem.cs` (1768), `BundleWriter.cs` (856),
  `RipOrchestratorSystem.cs` (808), `GradingWriter.cs` (666), `GltfWriter.cs` (652),
  `LaneRipSystem.cs` (641), `GeometryAssetReader.cs` (531), `AppearanceRipSystem.cs` (479),
  `TerrainRipSystem.FineCascade.cs` (465).
- **Fork logic inside upstream files moves out**, leaving the upstream files otherwise untouched:
  the six near-identical ripper button handlers and `Fit()` from `Settings.cs` into a fork-owned
  `Settings.Ripper.cs` partial and `IO/`. **Do not reorder `Settings.cs`'s property declarations**
  — `Settings.cs:494` records that settings load assigns in declaration order and a master's
  setter stomps its subs, so the order is load-bearing. Strip CJK from fork-added members only
  (155 lines in `Settings.cs`, 48 in `Instance.cs`, 20 in `Mod.cs` — where the fork's own
  additions are already inconsistent, `Mod.cs:61` being English-only next to bilingual siblings).
- Also fix here: `Settings.SetDefaults` (`:50-217`) duplicates 147 property initializers — two
  sources of truth for every default, with nothing enforcing agreement.

## Phase 10 — Performance, profile-guided

**Principles served:** 10 efficiency, measured.

Every item cites a Phase-0 baseline number and reports the after.

**Producer.** The ripper uses **zero jobs and zero Burst** — every one of the ten stages is a
serial main-thread walk, which directly contradicts CLAUDE.md's stated convention and costs
57.6 s for the instance walk alone (`InstanceRipSystem.cs:163`). Jobify the three genuinely pure
per-entity passes (the instance classification pre-pass, lane classification/tessellation, the
roadbed/clip rasterisation). Hoist `ComponentLookup` — only `InstanceRipSystem` does, and
`LaneRipSystem.ProcessOne` performs ~20 type resolutions **per lane** over hundreds of thousands
of lanes. Merge the three separate walks of the network edge set (`NetworkRipSystem._edgeQuery`,
`TerrainRipSystem.StampRoadbed`, `StampCutout` — walks 2 and 3 read the identical component chain
and differ only in a flag mask). Stop `Task.Run(...).Wait()` on the main thread
(`TerrainRipSystem.cs:447`, a 93-line lambda posted to the pool and immediately blocked on).
Cache the `Instance` service locator — 40 systems exposed as properties that call
`GetOrCreateSystemManaged<T>()` on **every access**, reached from rip paths at 30+ sites. Vectorize
`Png.EncodeGray16`'s per-sample byte swap and use Sub/Up filtering instead of filter 0 on the
smoothly-varying 16-bit heightmaps. Kill the per-record dictionary + boxing + `double[]`
allocations in the writers (tens of millions of short-lived objects at ~1 M instances).

**Consumer.** The 1.27 M-record instance layer is streamed and iterated in Python **3–4 times per
build** (`shapes.py:168`, `instances.py:152`, `meshindex.py:112`, `bake/skirts.py`), and
`stages/instances.py` does a pure-Python quaternion→matrix conversion plus a `json.dumps` per
record — a `(N,3)/(N,4)/(N,3)` problem that batched numpy does in a handful of ops. **`png.py`
makes ~6 full copies of a raster** (`:96` returns `bytearray(out.tobytes())`, discarding the
array it just built; `:141` and `:171` add two more `bytes()` copies) — peak RSS ~0.8 GiB per
8192² heightfield decode where ~0.3 GiB would do, and the array at `:78` is already the answer.
`beamng/level.py:103 write_objects()` rewrites the entire object tree and walks the whole `main/`
directory on every call, **16 times per build** (14,658 statics × 16). `bake/materials.py:633`
does a boolean mask + reduction per centre per k-means iteration where `numpy.bincount` is a
one-liner. `stages/lights.py:334` composes 3×3 rotations with nested comprehensions per light.

## Phase 11 — Comment, naming and structure consistency

**Principles served:** 1 DRY, 7 naming, 8 self-explanatory code, 9 consistent structure.

Applies to all 187 fork files, not just the split ones.

Delete boilerplate: 22 copies of `/// Mod's logger.`, 17 of the `Instance.Log` cross-reference,
17 + 16 of the `OnCreate`/`OnDestroy` event descriptions, `/// Close the file.`, and the
`Instance.cs` pattern of a four-line bilingual block on each of 40 one-line properties (63 %
comment density). Fork C# is 24–31 % comments against Python's 7–10 %; the gap is almost entirely
this.

Move derivation prose — `GradingWriter`'s 60 lines of provenance, `NetworkRipSystem.GameMesh.cs:596`'s
25 comment lines guarding a 3-line expression, `NetworkRipSystem.cs:660`'s 28 guarding four — into
the per-subsystem reference docs Phase 12 creates, leaving a one-line summary and a stable anchor.
**Declared approximations stay in the code** (`NetworkRipSystem.GameMesh.cs:596` "This is a
producer CHOICE, not a port", `stages/instances.py:236`'s `approximations` block,
`stages/terrain.py:445`'s NOT PARITY fence) — those are a CLAUDE.md non-negotiable.

Name things properly while there: `num7`, `num8`, `num10` in `NetworkRipSystem.GameMesh.cs:627-791`
are decompiler artifacts whose trailing comments name what the identifier should have been. Settle
the `colour`/`color` split that currently appears **inside the same module**, the six names in use
for "placed object" (`instance`/`record`/`placement`/`item`/`object`/`entry`/`row`/`shape`), the
five for "prefab id", the eight for ground height, and `center`/`centre`, which disagree between
the settings keys and the 183 uses in prose and locals.

Structure, likewise repo-wide:

- **One return-shape convention.** Five are in use — namedtuple (16 of them), plain tuple, dict
  with magic control keys smuggled in the payload (`_history`, `_norecord`, `_report`, `_tiles`),
  dict-of-lists, and classes. There is **no `@dataclass` anywhere in the consumer codebase**.
- **Type hints.** Coverage is inverted: the two most-depended-on packages have the least
  (`navgraph.py` 1 signature of 32, `glb.py` 1 of 14, `pipeline/state.py` 1 of 27,
  `settings.py` 3 of 25) while the standalone leaf tools are fully annotated. Annotate the public
  surface of `csiibundle` and `cs2beamng` at minimum, checked in CI.
- **Field and file conventions in C#.** Three field-naming conventions coexist (`_camelCase`
  implicit-private, `_camelCase` explicit-private, and `m_PascalCase` in `.GameMesh.cs`) — inside
  one `partial class`. No `#region` anywhere; instead three incompatible banner-comment styles.
  `Settings.Fit(...)` is applied at 7 call sites and skipped at 5. One logger idiom, not two.
- **Docstring style.** Four coexist; settle on one, with an explicit return-shape block where a
  function returns a dict.

### Magic strings and numbers, and the constants modules that end them

**The bundle contract is spelled as bare literals on both sides of its own seam.** `"bundle.json"`
appears in **25 Python files**, `"materials.json"` 31 times, `"instances.jsonl"` 16,
`"networks.jsonl"` 16, `"clipvolumes.jsonl"` 11 — **29 Python modules hard-code at least one
bundle filename**, and the producer repeats the same twenty names across `IO/` and `Systems/`
(four copies each of `instances.jsonl`, `networks.jsonl`, `materials.json`, `areas.jsonl`,
`clipvolumes.jsonl`, `lanemeshes.jsonl`, …). These names *are* the contract. Renaming a layer is
a repo-wide grep across two languages today, and nothing fails if the two halves disagree.

The other three families, all counted:

- **Closed string sets with no declaration.** `kind == "tree"` at 10 sites (and the set is
  wider than those sites know — "tree" is *all* planting, `"growth" in attrs` is the real tree
  test), `status == "baked"` 11, plus `"fresh"`, `"running"`, `"failed"`, `"cancelled"`;
  material `kind` in `"opacity"`/`"colormask"`/`"glass"`/`"trunk"`; progress `kind` in
  `"stage"`/`"stagep"`/`"phase"`/`"phase_end"`/`"log"`/`"hello"`. Every one of these is an enum
  that was never written down, and each is compared by literal in a different module from the
  one that produces it.
- **The engine's key vocabulary, inline.** `"class"` 40 times, `"persistentId"` 16,
  `"shapeName"`, `"materialTag0"`/`"materialTag1"`, `"internalName"`, `"__parent"` — BeamNG's
  JSON keys typed out at each use, in a package whose whole job is emitting them.
- **Magic numbers.** `ruff --select PLR2004` reports **648 magic-value comparisons repo-wide,
  197 of them inside the two shipped packages** (`csiibundle` + `cs2beamng`).

**The fix is dedicated constants modules at the boundaries, not a grab-bag `constants.py`** — a
shared dumping ground would breach principle 5 as fast as the literals breach principle 1:

- **`tools/csiibundle/bundlepaths.py`** — every filename and subdirectory the bundle contract
  names, and nothing else. `reader.py`, `bundlecheck`, the bake and both consumers resolve
  through it; after this phase **no module outside it opens a bundle path by literal**.
- **`IO/BundleNames.cs`** — the same list on the producer side, one `internal static class`,
  the single source for `BundleWriter`/`InstanceWriter`/`NetworkWriter`/`LaneWriter`/
  `TextureWriter`/`WarningsFile`.
- **The two lists are checked against each other**, the way `tools/gen_docs.py --check` already
  keeps the settings docs from drifting: a check asserts the C# and Python name sets are equal,
  so the two halves of the contract cannot diverge silently. This is the only new cross-language
  coupling the phase adds, and it is a *test*, not an import.
- **`apps/cs2beamng/beamng/keys.py`** — BeamNG's JSON key and class-name vocabulary, beside the
  writers that emit it.
- **Closed sets become enums** wherever they cross a file boundary: instance `kind`, material
  channel `kind`, run `status`, progress event `kind`. A `str`-valued `Enum` (**not** `StrEnum`
  — `pyproject.toml` targets 3.10), so the wire format on disk is byte-identical and Rule 1
  holds. Declaring them is also how the `"tree"` trap above stops being folklore.
- **Tuning numbers stay where their rule is derived.** A measured constant moves *only* if it is
  repeated or crosses a module boundary, and it moves to the module that owns the rule, named,
  with its derivation comment — never to a shared file. Hoisting `HEIGHT_SIGMA` away from the
  algebra that explains it (`stages/terrain.py:393`) would be the same defect as deleting the
  comment, which Rule 4 forbids.

**Gates, and the specs that prove them.** Enable `PLR2004` through the existing shrink-only
ledger (`tools/check_ruff_debt.py`, the pattern already running for `ARG`) so the 648 can fall but
never rise, with specs exempt. Add `tools/check_literals.py`: a bundle filename or a BeamNG key
spelled outside its constants module fails CI, in both languages. Three specs ship with the work,
because a gate with no spec is a gate nobody notices has stopped firing:

- `tests/python/tools/check_literals_spec.py` — the checker flags a planted literal and passes a
  clean tree, and `packaging/cs2beamng.spec` (PyInstaller's, not a test) is not swept up.
- `tests/python/csiibundle/bundlepaths_spec.py` — every name the contract lists resolves, and the
  set matches `docs/example-bundle` on disk.
- `tests/python/tools/bundlenames_parity_spec.py` — the cross-language check: `IO/BundleNames.cs`
  and `bundlepaths.py` name the same set. This is the only thing holding the two halves of the
  contract together, so it is a spec that runs on every CI leg, not a one-off script.

The enum conversions are proved by a spec per closed set asserting the **serialised value is
unchanged** (`str(Kind.TREE) == "tree"`), which is what makes them safe under Rule 1.

### File structure and file names

**Every split so far has invented its own naming idiom, and none of them is written down.** Three
were coined inside one week of Phases 4 and 9:

- `estimate.py` → `estimatebase` / `estimatemeshes` / `estimatescore` / `estimatetextures`
  (run-together suffixes naming the *split*, not the thing);
- `instances.py` → `instanceforest`, `materials.py` → `materialexport` / `materialledger`
  (run-together, but noun-phrase);
- C# partials `NetworkRipSystem.GameMesh.cs` and `TerrainRipSystem.FineCascade.cs` **and** a new
  `Systems/Rip/` subdirectory whose five files all re-state the directory in the filename
  (`Systems/Rip/RipWalk.cs`, `RipReport.cs`, `RipOutcome.cs`, `RipAssetIds.cs`,
  `RipDiagnostics.cs`) — two structuring mechanisms for one concern.

Repo-wide the same absence shows as:

- **No word-separation rule.** `map_lua.py`, `matbind.py`, `validate_level.py` and the
  `levelcheck/` package are four conventions for the same kind of thing inside one app.
- **Families spelled by prefix only.** `csiibundle` is **60 flat modules** carrying implicit
  packages: `nav*` (8), `lattice*` (3), `terrain*`/`clip*` (4+), `colour*` (3), `mesh*` (3).
- **Tests are not in `tests/`.** All **48** Python `test_*.py` live in `tools/`, mixed in with the
  library modules, the `check_*_vs_game.py` oracles and the `probe_*.py` / `ab_*.py` one-shots —
  including the tests for `apps/cs2beamng`. `tests/` holds only `Carto.Tests` (C#). (Phase 12's
  "no command runs all tests, 18 of 31 run nowhere" is **stale in its first half** and must be
  corrected there: `ci.yml:83` runs `python -m pytest -q` and `pyproject.toml:128` records the
  fix. What is left is *where* the files live and *what shape* they are — the section below.)

**The convention to settle, apply repo-wide, and write into `docs/coding-standards.md` (Phase 12):**

1. **One word-separation rule per language.** Python: a library module is a single lowercase word
   naming one concept (`matbind`, `navgraph`, `drape`); `verb_noun` is reserved for scripts and
   entry points (`check_limits.py`, `build_exe.py`); tests are `test_<module>.py`. C#: one type
   per file, file named for the type; partial-class files `Type.Aspect.cs`.
2. **A prefix family of ≥ 4 members becomes a package**, and the prefix moves into the directory
   and drops off the file: `csiibundle/nav/graph.py`, `nav/junction.py`, `nav/merge.py`, … The
   import-graph check that principle 5 already puts in CI decides which families are real (mostly
   internal edges) — a prefix that is merely alphabetical stays flat rather than becoming a
   package that lies about cohesion.
3. **A split names the thing, not the split.** `estimate*` becomes `pipeline/estimate/` with
   `base.py`, `meshes.py`, `score.py`, `textures.py`. `instanceforest` and `materialledger` are
   re-read for what they actually own and named for that.
4. **One structuring mechanism per C# concern** — partial-class suffixes *or* a subdirectory,
   never both — and **no file name repeats its directory**: `Systems/Rip/Walk.cs`, not
   `Systems/Rip/RipWalk.cs`.
5. **Tests move out of `tools/` into `tests/`**, mirroring the source tree — the shape and the
   naming are the next section's subject.
6. **One file, one exported concept, and the file name is that concept's glossary term** — the
   same glossary this phase settles for ground height, placed object, prefab id, `colour`/`color`
   and `centre`/`center`.

**How the moves land.** Every rename is `git mv` plus its import rewrite in one commit and nothing
else (Rule 1: behaviour-preserving, and a rename mixed with an edit is unreviewable). Each rename
also carries its **documentation references in the same commit** — the discipline Phase 12 applies
to the 754 `§NN` code references — because a moved file is exactly how the eight dangling reference
targets Phase 12 has to fix got there. `check_limits.py` grows the naming rules (per-directory
regex, the no-repeat-the-directory rule, tests-live-in-`tests/`) so the convention is a CI gate and
not discipline, per Rule 3 — with `tests/python/tools/check_limits_spec.py` covering the new rules,
including the `packaging/*.spec` exclusion.

**The renames are safe because the specs exist first.** A module is moved only once it has a spec
(next section) pinning its behaviour; the spec is written against the pre-move code, and the move
lands green. That ordering is what makes "behaviour-preserving" a measurement rather than a claim,
and it applies to the Phase 4 and Phase 9 splits already recorded as done — where a split product
has no spec, the spec is backfilled in this phase against today's behaviour.

### Test structure: one harness, one layout, `.spec` naming

Measured 2026-08-28; principle 9's baseline row is updated from these numbers:

- **48 Python test files, 132 collected tests — and 122 `test_*()` functions that are never
  collected.** They predate pytest and are internal helpers taking positional arguments, which is
  precisely why `pyproject.toml:137` has to set `python_functions = ["gate"]`. For 44 of the 48
  files that yields **one collected test per file**: a whole suite reports as a single pass/fail.
- **Two styles, not one.** 44 files are `gate()` scripts; four are `unittest.TestCase` subclasses
  (`ContractCase` in `test_bundle_contract.py`, `test_contract_assets.py`, `test_contract_ground.py`,
  `test_contract_lanes.py`) which pytest collects *only* because unittest classes bypass the
  `python_classes = []` override. Neither style is pytest.
- **Every file brings its own print-based mini-harness.** Twelve carry a module-global
  `FAILS`/`CHECKS` accumulator plus `section()`, and **`check()` is defined 32 times in 12 distinct
  signatures** with the argument order reversed between them: `check(cond, what)` (8),
  `check(ok, label)` (6), `check(ok, claim, detail="")` (3), `check(name, condition, detail="")` (2),
  `check(label, ok, detail="")`, `check(label, decoder_name, path)`, and six more. This is
  principle 1 as much as 9 — twelve copies of a test framework pytest already ships.
- **A failure is a `print()`, not an assertion.** `check()` appends to a global and the single
  `gate()` asserts at the end, so pytest can report *that* a file failed but never which claim,
  which line, or what the values were. Every debugging session starts by re-running the file by
  hand.
- **`pytest --collect-only` takes 48 s**, because collection imports modules that do real work.
- **The ES-module UI has no tests at all** — 9 modules under `apps/cs2beamng/ui/`, no
  `package.json`, no runner; CI's only JS coverage is `check_limits.py`'s line count.
- **C# has three test files** — `BundleFileTests.cs`, `JsonLinesWriterTests.cs`, `PngTests.cs`
  (xUnit) — for a fork of 187 files, of which `IO/` alone is 27.

**The structure to build.**

1. **`tests/` is the one test root, mirroring the source tree**, three language subtrees beside
   the existing `tests/Carto.Tests`:

   ```
   tests/python/csiibundle/drape_spec.py        ← tools/csiibundle/drape.py
   tests/python/cs2beamng/stages/terrain_spec.py ← apps/cs2beamng/stages/terrain.py
   tests/python/conftest.py                      ← tools/conftest.py, deleted there
   tests/js/settings.spec.js                     ← apps/cs2beamng/ui/settings.js
   tests/Carto.Tests/IO/BundleFileSpec.cs        ← IO/BundleFile.cs
   ```

   `tools/` keeps the library; the `probe_*.py` / `ab_*.py` one-shots go to `tools/probes/`; the
   `check_*_vs_game.py` oracles stay in `tools/` — they are instruments the game validates, not
   specs, and CLAUDE.md's rule that the game is the oracle is the reason they are separate.
2. **`.spec` is the naming convention, spelled the way each language permits.** JS takes it
   literally — `settings.spec.js`. C# takes `<Type>Spec.cs`, renaming the three `*Tests.cs`.
   **Python cannot take `drape.spec.py`**: a dot is not legal in a module name and pytest's
   default `prepend` import mode derives the module name from the basename, so the form is
   `drape_spec.py` with `python_files = ["*_spec.py"]`. One trap to encode in the gate:
   `packaging/cs2beamng.spec` is **PyInstaller's build spec**, not a test, and no `*spec*` glob
   may collect it.
3. **One spec per source module, named for it, and that mapping is a gate.** A new module with no
   spec fails CI, the same shape `check_limits.py` already has. Coverage is measured as
   *modules-with-a-spec*, not lines — a percentage nobody can read is not a gate.
4. **One harness: plain pytest.** Delete the twelve mini-harnesses and the twelve `check()`
   signatures. A claim becomes `def test_<claim>()` with a bare `assert`, which turns the 122
   orphaned functions into real, individually-reported tests. `python_functions` and
   `python_classes` overrides go away; `python_files` is the only one left. The four
   `unittest.TestCase` contract classes convert to pytest functions with a shared fixture.
5. **Tiers are markers, not filenames or `SKIPPED` lists.** `@pytest.mark.contract` (needs only
   `docs/example-bundle`), `@pytest.mark.build` (needs a built level on this machine — today
   spelled by the `test_estimate_build.py` split and by per-file `SKIPPED` accumulators),
   `@pytest.mark.game` (opt-in, the game-comparison instruments). CI runs the first two. Moving
   the heavy work behind fixtures is what takes the 48 s collection down.
6. **JS gets a runner**: `node --test` (built into Node 18+, zero dependencies, and the UI is
   plain ES modules — a bundler-shaped runner would be a dependency this repo has so far not
   needed), `tests/js/*.spec.js`, wired into `ci.yml` beside `pytest` and `dotnet test`.
   `check_limits.py` already counts `.js`, so the specs sit under the same 400/60 ceiling.
7. **C# specs mirror `IO/`, `Domain/`, `Systems/`** — and the boundary is declared, not implied:
   the rip systems cannot be unit-tested without the game assemblies, so the target is the pure
   surface (`IO/`, `Domain/`, `Systems/Rip/`'s helpers, the geometry math ported in
   `NetworkRipSystem.GameMesh.cs`). **What is untestable off-game is written down and counted**
   in `docs/testing.md`, the same discipline CLAUDE.md requires of a declared approximation.
   Producer specs are also the *only* producer work this plan can validate without a rip — Rule 5
   applies to everything else, and a spec that passes offline still says nothing about what the
   next in-game rip writes.
8. **Every phase lands its specs with it, and this phase backfills.** From here on a split that
   produces four modules produces four specs in the same commit. Phases 4 and 9 are already done,
   so their products (`estimate*`, `instanceforest`, `materialledger`, `Systems/Rip/*`,
   `TerrainRipSystem.FineCascade.cs`) are backfilled here, against today's behaviour, before the
   renames in the previous section move them.

**What this phase does not do.** It does not add coverage for behaviour that was never tested —
that is new work, not a refactor, and Rule 1 forbids it changing what the producer writes or a
build produces. The specs written here pin what the code does **today**, including its defects; a
defect a spec exposes is logged and fixed as its own commit, per Rule 1's second sentence.

## Phase 12 — Documentation

**Principles served:** 3 YAGNI, 7 naming, 8 self-explanatory code — and it writes principles 1-10 into the repo as `docs/coding-standards.md`.

Delete far more than is written.

- **`docs/plans/` (19 files, 8,948 lines) collapses.** 11 already carry a
  CLOSED/SUPERSEDED/ACCEPTED/BUILT banner and 86 dead checkboxes; five handoffs open by telling
  the reader to open a file outside the repo. Keep only durable content, moved into
  `docs/decisions/` as short standalone notes. One `docs/status.md` replaces every "live plan"
  pointer — today `PLAN.md:271`, `CLAUDE.md:15` and `apps/cs2beamng/README.md:8` all point at
  closed documents, and the last is five phases stale.
- **The two audits split by subsystem** with stable named anchors, and **all 754 `§NN` code
  references are rewritten in the same commits as their targets** — the code is currently pinned
  to the numbering of an 11,036-line pair of files. Retire the second `§` namespace (contract §
  vs audit §, no prefix rule today) and the six phase-letter namespaces (Phase N / H / A / B / P /
  D / E / W), which `PLAN.md:273` already records as having caused a misread.
- **New standalone single-purpose docs**, one job each, no cross-dependency:
  `setup.md` (prerequisites: the `Styles` submodule init that nothing mentions today but the mod
  build needs; `CSII_TOOLPATH` **and** `CSII_MANAGEDPATH`, both **User**-scoped as
  `Carto.csproj:22` literally requires; the .NET SDK and Blender versions, stated nowhere),
  `build-mod.md` (the `dotnet build -c Release` route to `Carto.dll` plus the three Burst
  natives, where `Mod.targets` deploys them, and how to build without deploying),
  `build-app.md` (`build_exe.py` → `cs2beamng.exe` and `cs2beamng-cli.exe`, the PyInstaller
  stub trap, the missing `packaging/cs2beamng.ico`, and the wheel), `running.md` (GUI, CLI,
  the bake, the Blender harness — including `gui --port`/`--no-open` and `CS2BEAMNG_HOME`,
  documented nowhere today), `commands.md` (every command with its flags, generated where
  possible — note the two `--region` shapes that differ between the app and the bake and are
  cross-referenced by neither), `testing.md` (there is currently **no command that runs all
  tests**, and 18 of 31 run nowhere), `releasing.md`, `architecture.md`, `contributing.md`.
- **`docs/coding-standards.md`** — principles 1–10 written into the repo as the standard for all
  future work, each with what it means here, the gate that enforces it, and the exemption rule for
  upstream code. Carries the naming glossary (one agreed term per concept: ground height, placed
  object, prefab id, `colour` vs `color`, `centre` vs `center`) and the comment rule — derivations
  in docs, declared approximations in code, nothing that restates the code. **Written so the gate
  config and the doc cannot drift**: `check_limits.py`'s thresholds and the `ruff` rule set are
  quoted from the config, the way `gen_docs.py` already does for settings.
- **Generate what can be generated.** `tools/gen_docs.py --check` is the one thing in the doc tree
  that cannot drift, gated in two workflows. Extend the pattern to the command reference and the
  stage/product lists.
- **Fix the counted drift:** "fifteen schemas" in 9 places (there are 17); `tools/README.md:22`
  "23 modules, ~8,000 lines" (27 + 17, 14,742); Python "≥3.11" vs `>=3.10` vs CI's 3.11/3.12/3.13;
  "eight stages" in 5 places (`RipOrchestratorSystem.BuildStages` builds **ten**); `tools/cs2bundle/`
  cited 13 times, a path that never existed; 8 dangling reference targets including two absolute
  paths outside the repo. Then delete `tools/level_budget.py`, a shim whose only stated reason to
  exist is that the docs still name the old path.
- **Single-source the version** — it lives in five places today (`Carto.csproj`'s
  `AssemblyVersion`/`FileVersion`/`Version` at 1.0.16, `PublishConfiguration.xml`'s `ModVersion`,
  and `cs2beamng/__init__.py:21` at 0.1.0), and no `vX.Y.Z` tag can satisfy both halves —
  `release_mod.ps1:57` enforces the tag against the mod version while `release.yml` names the
  artifact from the app version.
- Rewrite `CLAUDE.md` and `README.md` against what is then true, and retire `PLAN.md` (all phases
  accepted) into `docs/decisions/`.

---

## Verification

### How to verify a move — cheapest instrument first

**Binding from Phase 6 on.** Phase 5 spent over three hours waiting: fourteen full `pytest` runs
at ~10 minutes, three full-city rebuilds at ~16 minutes, and one more wasted on a crash. Almost
none of it bought anything. **Eight of its failures were a moved ADDRESS** — a `module.name` where
the module no longer has the name — and every one was found by whatever happened to be running:
five by a 3-second example build, one by the 10-minute suite, one by a **16-minute full-city
build**, and one by a person typing `cs2beamng budget`. A one-second question was answered
fourteen times by a ten-minute instrument.

The ladder, measured on this repo on 2026-08-28. **Run a rung only when everything above it is
clean, and never reach for a lower rung to answer a question a higher one can settle.**

| | Instrument | Cost | What only it can catch |
| --- | --- | --- | --- |
| 1 | `ruff check` | 2 s | unbound names, unused imports, syntax |
| 2 | `python tools/check_references.py` | **1.3 s** | `module.name` and `from ours import name` that no longer resolve; import errors; cycles |
| 3 | `python tools/check_shadows.py` | 1 s | a local that shadows an import (`tidy = tidy(...)`) |
| 4 | `python tools/check_limits.py` | 3 s | the size ceiling, and stale allowlist entries |
| 5 | `python -m pytest -q -m "not slow"` | **2 m 45 s** | 123 of the 132 tests |
| 6 | headless build of `docs/example-bundle` | 4 s | the pipeline end to end, minus cook and minus the real bundle |
| 7 | `python -m pytest -q` | 10 m 24 s | the nine real-bundle files — 78 % of the suite's time |
| 8 | rebuild `cs2_city_fine` (Rule 6) | 17 m | the cook, the real city, and residency |
| 9 | Naomi's in-game look | hers | everything a report cannot say |

**Rungs 1-4 are eleven seconds together and they are not optional.** Run them after every edit,
not after every landing. Rungs 1-3 exist because of Phase 5: `check_references` and
`check_shadows` were both written after the failures they would have caught, and between them
they cover every address break that phase produced.

**Rung 5 is the iteration loop.** `-m "not slow"` is a measured split, not a guess: nine files
are 494 s of the suite's 635 s and every one walks the real bundle or a real built level; the
other 39 come to 141 s. The list and the timings are in `tools/conftest.py`.

**Rung 7 runs once per landing, not once per fix.** Phase 5's pattern was: full run, find
failures, fix, full run again — twenty minutes to land one commit. Fix at rung 5 and spend rung 7
once, on the way out.

**Rung 8 runs once per PHASE, not once per landing**, unless a landing changes what the build
writes and the next landing would stack on top of it. Phase 5's mid-phase rebuild was worth it
for exactly that reason — the shapes split rewrites every `.dae` — and its second was the
closing gate. A third was wasted on a crash rungs 3 and 6 would have caught.

**What has no rung below 8, and must therefore be reasoned about rather than discovered.** The
cook stage does not run on the example bundle (it needs a BeamNG userfolder), so nothing under
rung 8 executes `stages/cook.py`. A change there is the one case where the expensive instrument
is the first honest one — say so in the commit rather than pretending a green rung 6 covered it.

**A command with no test is a command that will break silently.** `cs2beamng budget` was dead for
an hour and the whole suite stayed green, because it reaches the user through no test and no
stage. `test_a1_level` now `--help`s all six CLI entry points; anything new that a person invokes
directly gets the same one-line proof.

**The standing per-phase gate.** Every phase, without exception, answers all eleven before it
lands. The first five are mechanical; the rest are a review against the phase's diff, using the
baseline lists in the principles table as the closing checklist.

1. `ruff` clean and `ruff format` idempotent over every fork file — not only the ones touched.
2. `check_limits.py` clean; the phase's allowlist entries **removed, never added**.
3. `check_references.py` and `check_shadows.py` clean — the eleven-second rungs, run after every
   edit rather than at the end.
4. `pytest` green across all 48 test files (not the 31 of the plan's first draft; Phase 4's
   splits raised it); the `Carto.Tests` project green.
5. The import-graph check clean: no `csiibundle` → consumer reference, no stage → stage private,
   no cycle.
6. **DRY** — did this phase leave any rule implemented twice? Every item it claimed from the
   principle-1 baseline is gone, not merely relocated.
7. **YAGNI** — nothing was added "for later"; every symbol the phase introduces has a caller.
8. **SRP / separation** — each new file states one responsibility in one sentence. If that
   sentence needs an "and", the split is wrong.
9. **Naming** — every new or renamed symbol matches the `docs/coding-standards.md` glossary; no
   concept gained a second name.
10. **Comments** — comment density did not rise; every derivation the phase moved is intact and
   every declared approximation is still declared and still counted.
11. **Performance** — the phase's stage timings are re-measured on the Phase-0 instrument and are
    within noise or better. A regression blocks the phase.

Anything that cannot be satisfied is recorded as a declared, counted exception in the phase's
commit message — never left silent.

**Consumer phases.** Rebuild `cs2_city_fine` in place with `cook: true`, settings read from its own
`cs2beamng_report.json`, BeamNG closed. Diff the new report against the Phase-0 golden with
`ab_compare.py --counts`; `validate_level` at 0 problems; `cs2beamng budget` within noise of
baseline. Then hand it over for the in-game look **before** the commit.

**Producer phases.** Build only (`dotnet build -c Release`), `Carto.Tests` green, and the wording
stays "built, awaiting in-game re-rip". After Phase 11, one request for a full rip; diff its
digest against the current anchor manifest with `bundle_manifest.py` — per-layer record counts and
per-file hashes must match except where a phase declared a change. Read the rip report's warnings
and per-stage counts against the golden report.

**A digest diff compares producer code only across rips of an UNCHANGED SAVE.** This was written
into Phase 0 as an assumption and the 2026-08-27 re-rip broke it: the city had been played, so
+6,949 instances, +3,453 lanes, +290 areas and +11 meshes arrived that no producer commit could
account for, and terraforming rewrote 57 terrain-conforming road meshes on top. Nothing was wrong
with the rip — `VALID`, zero stage failures — but the instrument could no longer answer the
question it was built for. So the Phase 11 rip is only a gate if the save is held still from the
anchor rip to it; if it moves, the diff drops to what it can still do (record counts in the right
direction, no new warning classes, no stage failure) and says so. `docs/baselines/README.md`
carries the 08-27 diff and the one thing in it the save does not explain.

**Performance.** Phase 10 reports a table: rip stage seconds, build stage seconds, bake `cProfile`
totals, `level_budget` resident MiB and triangles drawn — before and after, same instrument.

**Docs.** `gen_docs.py --check` green; a link checker over `docs/` reporting zero dangling targets;
`grep -c '§' Systems IO Domain tools apps` reaching zero unresolved references; and the acid test
— follow `setup.md` through `build-mod.md` and `build-app.md` on the stated prerequisites alone
and confirm each documented command runs.

---

## Resuming this work

**Status: APPROVED 2026-08-20. In progress — see "Progress" at the end of this document.**

**This plan is the only record.** Three audit agents measured the repo (producer C#, consumer
Python, docs/build/CI); their raw reports are not persisted anywhere. Every number, file path and
line reference in this document came from them and from direct verification. Do not re-run the
audits — read this instead.

### What a future session must not re-litigate

- **Upstream Carto code is not touched at all** — not even its dead code. The boundary is
  authorship; Phase 0 commits it as `upstream-files.txt`. `Settings.cs`, `Mod.cs` and `Instance.cs`
  count as upstream; only fork-added members inside them are in scope.
- **Feature work is frozen**, including A6.0 (the in-game GPU out-of-memory) in
  `docs/plans/a6-fidelity.md`.
- **The `§NN` audits are split by subsystem** and all 754 code references rewritten with them.
- **Scope is all 187 fork-owned files / 75,145 lines**, not the 57 that breach the 400-line
  ceiling.
- **The verification ladder is not a suggestion.** Read "How to verify a move" before touching
  anything. The eleven-second rungs run after every EDIT; `-m "not slow"` is the loop; the full
  suite runs once per landing; the rebuild once per phase. Phase 5 ignored all of that and spent
  three hours proving it matters.

### Traps this plan already accounts for

- `Settings.cs` property **declaration order is load-bearing** (`Settings.cs:494`) — settings load
  assigns in declaration order and a master's setter stomps its subs. Do not reorder.
- The three defects in Phase 1 are **pre-existing and live**, not caused by this work. Fix them
  first so every later move is provably behaviour-preserving.
- The dense comments are a **measured-game-rule archive**, not noise. Both audits said so
  independently. Rule 4 lists the load-bearing blocks; they move verbatim or not at all.
- Producer work cannot be validated by an agent. It batches into one block ending in a single
  re-rip request; until then the wording is "built, awaiting in-game re-rip".
- Consumer work ends with a rebuild of `cs2_city_fine` handed over for an in-game look **before**
  the commit, per the standing workflow — **once per phase**, not once per landing, unless a
  landing changes what the build writes and the next would stack on top of it.
- **The cook stage has no cheap instrument.** It does not run on the example bundle (it needs a
  BeamNG userfolder), so nothing under the 17-minute rebuild executes `stages/cook.py`. A change
  there is the one place the expensive instrument is the first honest one.

---

## Progress

Each phase records here what landed and what it found, so this document stays the single record.

| Phase | Status |
| --- | --- |
| 0 — Guardrails and baselines | **DONE** 2026-08-21 — see below |
| 1 — The three defects | **DONE** 2026-08-21, in-game look verified 2026-08-27 |
| 2 — One implementation of every shared rule | **DONE** 2026-08-27, in-game look verified — five of its claims needed correcting; see below |
| 3 — One stage framework | **DONE** 2026-08-27 — 7 of 8 parallel blocks unified; the record merge refused, with reasons |
| 4 — Split `csiibundle` and `tools/` | **in progress** — `terrain`, `navgraph`, `colour`, `validate_bundle`, `make_example_bundle` split and validated, `probe_point` refused with reasons; see below |
| 5 — The app and its UI | **DONE** 2026-08-28, in-game look accepted — see below |
| 6 — The Blender adapter | not started |
| 7 — One rip-stage framework | **in progress** — extractions, the typed outcome, the frame loop and the report shape done; see below |
| 8 — The IO writer layer | **in progress** — the writers, the attribute readers and the grading guard; see below |
| 9 — Split the rip systems | **in progress** — the settings defaults and the field naming; the god-methods remain |
| 10 — Performance | not started |
| 11 — Comment, naming and structure | not started |
| 12 — Documentation | not started |

### The 2026-08-27 re-rip — the producer's first evidence

The four producer commits carried "built, awaiting in-game re-rip" from 2026-08-21 to 2026-08-27.
They are now ripped, and they are clean:

* nine stages, **zero failures in any of them** — including `Networks & lane geometry`, whose
  new per-entity catch and `run.Failed` counter (Phase 1, `e212516`) had never run in-game;
* `validate_bundle.py` **VALID**, one warning, the same advisory-`parent` warning the golden had;
* 27,154 network edges → 8,259 segments, identical to the golden's 8,259;
* the mod is provably HEAD — `Carto.dll` stamped 90 s before the last producer commit, and no
  `Systems/ IO/ Domain/` file touched since.

The full diff and the one unexplained item in it are in `docs/baselines/README.md`. The short
version: the save moved, which cost the golden its role as a producer oracle (see Verification),
and three prefab meshes changed in a way the save should not cause — not attributable to any of
the four commits, none of which touches geometry code, and not settleable from here because the
old bytes are gone. The next rip is the experiment.

### Phase 0 — DONE 2026-08-21

Commits: `156a90e` (the plan into the repo), `28d894d` (lint, format, gates), and this one.

**Built**

* `upstream-files.txt` — 92 upstream paths, derived from the tree at `43a05fb` (the last commit
  by Carto's author) intersected with HEAD. Both the exemption list and the don't-touch line.
* `tools/check_limits.py` + `tools/limits-allowlist.txt` — the 400-line / 60-line gate. Python
  by `ast`, C# and JS by brace matching over a string- and comment-blanked copy. **A stale
  allowlist entry is an error**, so the list can only shrink.
* `tools/check_ruff_debt.py` + `tools/ruff-debt.txt` — the same shrink-only property for the one
  ruff rule family (`ARG`) that Phases 2 and 3 own. 62 entries.
* **Added in Phase 5, after the failures that needed them** (all three in the `gates` CI job):
  `tools/check_references.py` (every `module.name` a shipped package writes still exists — 1.3 s
  over 193 modules), `tools/check_shadows.py` (a local that shadows an import), and the `slow`
  marker in `tools/conftest.py` that splits the suite 2 m 45 s / 10 m 24 s.
* `ruff` at 100 columns, whole tree formatted, `ruff check` clean. `.editorconfig` for both
  languages. C# analyzers with `TreatWarningsAsErrors`, upstream exempted per file the way
  Roslyn wants it.
* `tools/conftest.py` + `[tool.pytest.ini_options]` — `pytest` runs **all 31 test files** where
  CI ran 13 and 18 ran nowhere. 35 `sys.path` bootstraps in nine spellings deleted; a
  `bundle_dir` fixture over `csiibundle.bake.locate` (with a `CS2_BUNDLE_DIR` override) and a
  `needs_rip` marker that skips and says where it looked.
* `tests/Carto.Tests` — the producer's first automated check, 19 tests, linking the game-free
  sources by `<Compile Include>`.
* `docs/baselines/` — the golden bundle digest (16,991 files, 5.51 GB, per-layer counts), the
  level report and budget, and `profile.md`: the rip's ten stage timings, the build's measured
  rates, the level's resident MiB and drawn triangles, and what has no baseline and why.
* `ci.yml` — a `gates` job, a `mod` job, `pytest` once instead of thirteen commands, and
  `Systems/ IO/ Domain/ tests/` finally in the `paths:` filter.

**Found while building it** — each its own concern, all fixed here except where noted:

1. **The mod was compiling against the Python app's PyInstaller output.** `AssemblySearchPaths`
   puts `{CandidateAssemblyFiles}` first and the SDK's default globs swept `dist/`, so
   `netstandard` resolved out of `dist/cs2beamng/_internal/pythonnet/runtime/`. It built, which
   is why nobody noticed. Fixed by excluding the build outputs from the mod's globs and pinning
   the reference by `HintPath`.
2. **`ruff format` reformats fenced Python inside Markdown** and collapsed the aligned comment
   columns in `docs/consumer-guide.md`. Markdown is excluded; the docs were reverted.
3. **RUF005 turned numpy broadcasting into list concatenation.**
   `rng.normal(0, 1.5, (200, 3)) + [0, 4.5, 0]` became a list splat in
   `tools/test_lod_collision.py`. `test_lod_collision` caught it. The rule is retired with the
   reason in `pyproject.toml` — a cosmetic rule that rewrites `+` has no place here.
4. **`test_node_fan.py` failed its own negative control** on this city, and had done since
   before this work. The control hard-coded two segment `assetId`s from an older save; on the
   current city they carry no node strip in the window, so the gate was UNPROVEN and the file
   failed. It now falls back to discovering its own segments from the bundle, the way
   `dense_windows` already chose its clusters: 0.40 m → 17.83 m in the road-junction window.
5. **`test_terrain_blend.py` would have made CI permanently red.** It deliberately fails when
   no bundle is present ("a silent skip is how a gate stops being a gate") — correct intent,
   but CI never has a bundle. Split into a third outcome: `UNPROVEN` is printed by name, and
   **enforced as a failure the moment a bundle IS present**, which is when unmeasured means the
   rip did not carry the layer.
6. **The audit's "five game-free files" is three.** `BundleManifest.cs` takes an `Options`,
   which opens `using Carto.Utils`, which opens `Unity.Entities`, `Unity.Burst` and
   `Colossal.Mathematics`; `BundleWriter.cs` follows it out, taking a `BundleManifest`. And
   `Unity.Mathematics` **is not on NuGet**, so `GltfWriter`, `BundlePrecision` and
   `BaseSkirtBuilder` cannot be linked either. `Carto.Tests` covers `Png.cs`, `BundleFile.cs`
   and `RipWarning.cs`.
7. **`RipSurfaceAreas` is 135 lines, not the 461 the audit recorded** (`TerrainRipSystem.cs:903`
   to :1037). Phase 9's split still stands — it writes `areas.jsonl` and has nothing to do with
   terrain — but the number was wrong.

**The formatting was proved behaviour-preserving, not assumed to be.** `ruff format` touched 150
files including the whole bake. Baking `docs/example-bundle` at `5b7e212` (the commit before
Phase 0) and at HEAD, in two worktrees, gives 16 identical files out of 16 — the only difference
in `materials.json` is its own `generatedAt` timestamp, one second apart.

**Numbers that changed from the plan's**

`ruff format` expands dense one-liners, so the fork is **83,387 lines**, not 75,031, and **68**
files breach the 400-line ceiling, not 57. The file count is unchanged at 187, and the
pre-format count was exactly the audit's 57 / 14 / 43, which is how the boundary was confirmed
correct. The allowlist is seeded at 305 entries (68 files + 237 functions).

**Deliberate deferrals, each with an owner**

* `ARG` findings (62 ledger entries) — Phases 2 and 3.
* C# naming (`IDE1006`) stays a suggestion — Phase 11 promotes it to a warning.
* `IDE0005` (unnecessary using) cannot be a build gate: the SDK only runs it with
  `GenerateDocumentationFile`, which would light up `CS1591` across upstream Carto.
* Nullable stays off project-wide; it cannot be scoped by `.editorconfig` the way a diagnostic
  can, and fork files opt in one at a time as Phase 9 splits them.
* Seven test files are still module-level scripts whose body runs at import. Phase 4 owns them.

### Phase 1 — DONE 2026-08-21

Three defects, and the plan understated one of them by a factor of six.

**1. The app never applied the §4.2a decal lift — and that was LATENT, not live.**

The plan's claim is half right. `stages/instances.py:_transform` did reimplement the §2.2
composition in plain Python and did leave the lift out. But `placement.BAKED_KINDS` is
`("decal",)`, so **a decal record never reaches that function**: `bake/markings.py:250` places
decal quads itself, deliberately without the lift in the matrix — its comment says "bundle
frame, no lift: the drape sets the height" — and hands `decals.LIFT["instance_decal"]` to the
drape as `drape_eps`. Every record that does reach `_transform` (building, tree, prop) has a
lift of 0.0.

Measured rather than argued: rebuilding `cs2_city_fine` through the new code and matching 8,646
TSStatics to the bundle record at their own unambiguous (x, z) gives a worst vertical delta of
**0.000000000 m**. Nothing was sitting coplanar; nothing moved.

The fix stands anyway, because the duplicate transform is real and principle 1 owns it: the app
now calls `csiibundle.instances.instance_matrix`, and `frames.decompose` is new — engine-neutral,
returning the three fields a `TSStatic` needs from one matrix. The trap is closed rather than
survived: the day a decal does reach `_transform`, it arrives lifted.

What DID change is the quaternion. The copy renormalised; `frames.quat_matrix` deliberately does
not, because a stored non-unit quaternion is a signal about the producer. **6.9 % of records
store one**, off unity by ~2e-6, and it now arrives as a scale a hair off 1.0 instead of being
corrected away. Checked against 400,000 records before the change: position identical to 0.0,
rotation and scale within 3.8e-6 — which is exactly that effect — and no negative scales
anywhere.

**2. The region was a disc in SIX places, not one.** The plan named `bake/roads.py` and said
"every other stage uses a square". Counted:

| Disc | Square |
| --- | --- |
| `csiibundle/areas.py:348` — the surface areas | `beamng/placement.in_region` |
| `bake/areas.py` — the centre/radius threaded to it | `stages/derived._tile_intersects` |
| `bake/roads.py` — the road surface markings drape onto | `stages/water._intersects` |
| `bake/markings.py:_in_region` — and via imports, skirts and lane geometry | `pipeline/meshindex` |
| `bake/lods.py` — which meshes get LODs at all | `stages/networks` (fixed 2026-08-20) |
| `stages/navgraph._in_region` — the AI road graph | |

So a region build kept a square of buildings, props, trees, terrain tiles and road meshes and a
disc of everything painted on them. In the corners: streets with no markings, no skirts, no lane
geometry and no AI roads, and buildings whose LODs were never generated.

**21.5 % of a region build was in that gap** (4r² against πr²). Measured at centre (1399, 1153),
half-width 1000 m on the reference city: **16,786 more placed objects, 135 more road segments,
887 more surface areas**.

Fixed as principle 1 requires — one rule, in `csiibundle/region.py`, called by all eleven sites,
squares included so there is one rule rather than six that agree. `tools/test_region.py` asks
each real caller about the four corners of a box.

The cache had to be handled or the fix would not reach anyone with a region build already baked:
`region.tag()` records the shape on the region dict and `bake/runner.py` folds it into the option
fingerprint, so the five region-declaring products re-bake for a region and a **whole-bundle bake
is untouched** — there being no boundary, the two rules agree there. `stages/navgraph` goes to
version 4 for the same reason.

**3. `NetworkRipSystem` could wedge the main thread.** Confirmed. All four `Queue.Add` sites were
plain blocking adds on a bounded(8) queue with zero `IsFaulted` checks and no `Fail` path to
abort into — the exact shape `MeshRipSystem.cs:744` documents as wrong. `Advance` also had no
per-entity try/catch, so one throwing edge took the whole stage and every layer it writes.
`Enqueue` (TryAdd + fault check), `RipStep` (per-entity catch, counted and reported) and `Fail`
are all MeshRipSystem's, for MeshRipSystem's reasons. Built, awaiting in-game re-rip.

**Two of the four items were not what the plan said.** Defect 1 was latent, not live, and the
`Pump` item below was not a defect at all. That is now three plan claims out of four checked and
found overstated (the region was six discs, not one). **Measure before believing the plan** — its
numbers came from an audit that read code, not from running it.

**The rebuild (refactor Rule 6).** `cs2_city_fine` rebuilt in place, `cook: true`, settings read
from its own `cs2beamng_report.json`, BeamNG closed. **16 m 03 s**, status done, and the whole
bake re-forced first (2,540 s) because the E3–E7 commits of 2026-08-20 21:41–21:58 had already
changed `derived/` under the standing level.

Against the Phase-0 golden report, **every validation check is identical** — objects, classes,
forest items and types, cooked shapes, material files, navgraph, ground check, ter, spawn points,
all of it — and so are the budgets: DecalRoads 15,393, TSStatics 19,202, WaterBlocks 192, forest
items 903,371, forest types 3,105, shape files 7,879. `cs2beamng budget` is **byte-identical** to
the baseline: 4,256 MiB resident, 1,278,860 triangles.

`validate_level` reports **1 problem, the same 1 as the baseline**: the standing budget warning
that 4,256 MiB is in the unmeasured band. The only fields that moved are
`resident.budgetMiB`/`budgetPixels`/`budgetSource`, which are a THRESHOLD derived from the
resolution BeamNG last logged — and BeamNG was closed for the rebuild, so it fell back to the
1080p reference. A property of the machine, not of the level.

That is the expected result: this was a full-city build, `region` is None, and neither change
bites without a region. **The region fix is unproven in a built level until someone builds a
region** — the arithmetic and every caller are pinned by `tools/test_region.py`, but the picture
of a corner with markings in it is still owed.

**The fourth item was not a defect.** The plan recorded `RipProgress.Pump()` as latent —
"the progress indicator freezes on any plan without those stages". It does not: `Mod.cs`
registers every rip system unconditionally at `MainLoop` and none declares `RequireForUpdate`,
so `Pump` ran every frame regardless, twice. The **placement** was still wrong — `Pump` is what
runs `RunDeferred`'s queued work, the vanilla vector export included, and it was riding on an
accident of registration in two systems whose job it is not. It moved to `RipOrchestratorSystem`.

### Phase 2 — pre-flight, 2026-08-21

Every claim checked against the code before touching it, because two of the plan's own have
already turned out wrong (the region was six discs, not one; `RipProgress.Pump` was not a defect).

**Confirmed exactly as written**

* `frames.py:161` and `frames.py:167` are byte-identical (`bundle_to_zup_point` /
  `zup_to_bundle_point`), and the second has **zero callers**.
* **All thirteen** of the never-called public functions are never called — grepped one by one
  across the whole repo, callers = 0 for every name.
* `stages/scene.py:133 hasattr(colour_mod, "blackbody")` is permanently false: there is no
  `blackbody` anywhere in either package.
* Three PNG-decode wrappers (`groundmap.read_rgb`, `beamng/materials._load_any`,
  `stages/terrain._read_alpha`) and three `resample_nearest` (`reader.py:206`,
  `bake/materials.py:123 _resample_nearest`, `measure_paint_factor.py:50`).
* Three "first mesh state on disk" walks — `stages/shapes._state_suffix`,
  `stages/instances` (inline, cached by key), `pipeline/meshindex._state_stem`. **They do not
  return the same thing**: two return the suffix (`candidate[len(asset):]`), one the whole stem.
  The shared function returns the candidate; slicing stays at the call site.
* `bake/ground.py:41 MEASURED_DIAGONAL = "ne_sw"` is a BeamNG measurement defaulting for every
  consumer; `colour.py:752 Grading.write_ocio` opens "Write a **Blender** OCIO config".
* `pipeline/mapview.py:281` imports `stages.poi`; `stages/derived.py:48` and
  `stages/networks.py:39,40` import other stages as libraries.

**Wrong in the plan**

* **`pipeline/estimate.py` does not document a cycle.** The deferred
  `from ..beamng import validate_level` at `:506` is a lazy import with a comment about reading
  the budget "exactly the way the validator reads it" — and `validate_level` imports nothing
  from `pipeline`, so there is no cycle to break. It can simply be hoisted.
* **The hard-coded bundle path is in 9 non-test files, not 20** (`blender_import_bundle`,
  `check_roadgrade_vs_game`, `check_terrain_vs_game`, `colour_model`, `bake/locate`,
  `bake/__main__`, `probe_point`, `cli`, `modinstall`) — the 20 counted occurrences, and most
  of the rest are in tests, which `tools/conftest.py` already gives a fixture for.

**Already done, in Phase 1.** The `_in_region` promotion this phase lists could not wait: making
`bake/roads.py` a square while `bake/markings.py` stayed a disc would have left the very
inconsistency the defect is. `csiibundle/region.py` is that shared rule, and all eleven sites
call it.

### Phase 2 — DONE 2026-08-27

Every item on the list is done, and **five of its claims needed correcting**. The pattern from
Phase 1 held: the plan is a set of hypotheses, and checking each one before moving code found
work it had mis-sized in both directions.

**What collapsed**

| Rule | Was | Now |
| --- | --- | --- |
| Dead public surface | 13 functions, 0 callers each | deleted, plus `water.WATER_TEMPLATES` orphaned by one of them |
| PNG decode | 4 wrappers in 3 modules | `csiibundle/images.py` — `load_any`, `load_rgb`, `load_alpha` |
| PNG header | 4 IHDR readers | `png.read_header`, used by 2; 2 exempt (below) |
| sRGB transfer | 5 copies of the curve | `colour.srgb8_to_linear` / `linear_to_srgb8` + named constants |
| Tint multiplier | 3 (`_tint_multiplier`, `measure_paint_factor.multiplier`, `_uniform_multiplier`) | `tints.multiplier` / `tints.best_constant` |
| Texture resize | 2 byte-identical | `images.resize_nearest` |
| "First mesh state on disk" | 3 walks | `instances.first_mesh_state` |
| Bundle location | 4 hard-coded Windows paths + 2 env-var spellings | `locate.default_bundle_dir()`, `CS2_BUNDLE_DIR` |
| `ceiling=4000.0` | a parameter on 8 signatures, never once passed | `drape.HEIGHT_CEILING` |
| Engine knowledge | `MIN_THICKNESS`, `wind_vertex_colours` in the neutral package | `beamng/dae.py` |
| Material name binding | `stages/materials.py`, imported by 2 other stages | `beamng/matbind.py` |
| `LevelPaths` | `stages/derived.py`, imported by `stages/networks.py` | `beamng/level.py` |
| `_in_region`, `_material_specs`, `_texture_uri_base` | privates imported across `bake/` | `region.contains`, `tiles.material_specs`, `tiles.texture_uri_base` |
| `Grading.write_ocio` | a Blender method on the neutral class | `colour_model.write_ocio` |

**Proved rather than assumed.** The collapsed arithmetic is bit-identical, not
approximately-identical: `srgb8_to_linear`/`linear_to_srgb8` over all 256 bytes, a 200,001-point
float sweep and the 3x256x3 LUT that consumes them; `tints.multiplier`/`best_constant` over four
mask shapes including the zero-weight branch and 2,000 random scalar cases. And the whole bake:
`docs/example-bundle` baked from a worktree at `8e1cf11` and from this tree, same bundle, same
drive — **15 of 16 files byte-identical, the 16th differing only in its own `generatedAt`**.

(The first attempt at that comparison said a `lanegeometry` tile differed. It did not: the two
bakes wrote to different drives, and `texture_uri_base` returns `None` when no relative path
exists, so one glb carried a texture URI and the other carried none. The test was wrong, not the
code — worth recording because the failure looked exactly like a real regression.)

**Wrong in the plan**

* **`validate_bundle.py` and `probe_point.py` cannot share anything, and that is by contract.**
  The plan counts their PNG header reader and sRGB pair as duplication. `validate_bundle` states
  at the top: "Python 3.8+ and the standard library, no pip installs, no game, no producer
  source" — it is what a consumer author runs on a bundle before they have any of this.
  `probe_point` runs inside Blender, which ships numpy and nothing else, and its own comment
  says "a diagnostic that only works when the thing it is diagnosing imports cleanly is not much
  of a diagnostic". Both keep their copies, and both now say so where the copy is.
* **`stages/terrain.py`'s gamma-2.2 is not a fifth sRGB copy.** It is a different transfer
  function on BeamNG terrain textures, in five places. Merging it into the exact curve would
  change every terrain texture the app writes. `colour.py`'s constants block now says so, so a
  later pass does not "tidy" it — the same fence Phase 7 put around `Tessellate`.
* **The `resample_nearest` half-texel disagreement is a defect fix, not a move.** `reader`'s
  `+ 0.5` centres the sample and has a §8 justification; the texture resize does not and takes
  each block's corner. Adopting the better one changes what the bake writes, so Rule 1 says it
  gets its own commit. `images.resize_nearest` documents the disagreement and says not to settle
  it quietly.
* **The imports are not inverted.** `pipeline` importing `stages` is how `runner`, `worker` and
  `census` reach `all_stages`, which is correct layering. The defect in `mapview` was reaching
  into `stages.poi`'s **privates**; `_Network`/`_derive` are `Network`/`derive` now.
* **`MEASURED_DIAGONAL` could not move.** It is a BeamNG measurement defaulting for every
  consumer, as the plan says — but `cs2beamng` is the only consumer of this bake that ships
  terrain, so it is also the only value anybody asks for, and changing the neutral default would
  change what a bare `csiibundle-bake` writes. The literal `"ne_sw"` in `runner.DEFAULT_OPTIONS`
  now references the one constant, and both sites state whose measurement it is and that a
  second engine must override it.

**Two things the plan listed as dead code that were not**

* **`stages/scene.py`'s `hasattr(colour_mod, "blackbody")`** is a permanently-false branch, as
  the plan says. But deleting it silently would have dropped a rule the contract states:
  `colour.Light` says the sun's effective colour is the authored filter TIMES the blackbody of
  its colour temperature, and no consumer applies it because no `blackbody` exists anywhere.
  That is a gap, not dead weight. The fake call is gone and the gap is a **declared, counted
  approximation** — `sunColorTemperatureIgnored` in the scene report says when it actually bit.
* **`validate_level.render_target` / `light_manager`** are not "wrappers that exist only to
  un-privatise": they have six callers between `doctor`, `settings` and `estimate`. They ARE the
  public API, so the private originals took their names and the wrappers went. `render_target`
  then broke the 60-line ceiling under its new name — the shrink-only allowlist keys on the
  symbol — and was split rather than re-listed.

**One regression, caught by a test rather than by me.** Moving the material-name binding into
`beamng/materials.py` made `stages/derived` and `stages/networks` transitively reach a module
that reads `texture_max`, and `test_app_shell` checks that a stage declares every setting any
module it can REACH reads. Declaring it would have been wrong twice — neither stage reads it,
and both would then re-run whenever it changed. The binding is four settings-free functions, so
it got its own module, `beamng/matbind.py`, and the closure stayed honest. `beamng/materials.py`
imports `mapto` from it rather than the other way round.

**The height-sampling question, answered with a measurement.** The plan says of
`ter.sample_height`: "either the validator uses the diagonal-aware sample or the deviation is
declared and counted". Measured on `cs2_city_fine` (square size **1.75 m**) over 1,441 sampled
buildings, bilinear and the measured `ne_sw` triangulation disagree by a median and p95 of
**0.0000 m**, a maximum of **0.0154 m**, and **no** sample crosses the 0.5 m tolerance the check
applies — buildings sit on ground the game graded flat, which is where the two agree. The 9.4 cm
and 41 cm figures in `drape.py` are at 3.5 m and 14 m strides. So: declared and counted, with
the numbers, and `groundCheck.sampler` in the report now names which sampler produced them.

**One intended behaviour change, measured.** `stages/lights._world` imported `_quat_rows` and
`_swap_yz` out of `stages/instances` — a third and fourth copy of arithmetic `csiibundle.frames`
owns, and the pair Phase 1 had just removed from `_transform`. `frames.quat_matrix` does not
renormalise and `_quat_rows` did, so the written `rotationMatrix` moves: 630 of 654 light
rotations (96.3 %) store a non-unit quaternion, off by at most 1.5e-06 in the squared norm, and
**814 of the 5,886 matrix floats as written (13.8 %) change, by at most 2e-06** — a fifth of a
millimetre of aim at 100 m. Not renormalising is the deliberate choice, for the reason Phase 1
recorded: a producer shipping a non-unit quaternion should show up, not be corrected in one
consumer.

**New in `csiibundle`:** `images.py` — the picture loaders. `png.py` broke 400 lines when they
were added to it, and the gate was right that they do not belong there: `png.py` is the
standard-library decoder for the §8 rasters, which are numbers and must never touch PIL.
**New in the app:** `beamng/matbind.py`.

### Phase 3 — DONE 2026-08-27 (the record unification refused, with reasons)

**What landed**

* **`csiibundle/parallel.py` — `each()`.** Seven of the eight parallel blocks now run through one
  function: four bake products (`materials` ×4 uses, `lods`, `tiles`) and four `cs2beamng` stages
  (`derived`, `materials`, `networks`, `shapes`). `bake/materials._each` was already the
  abstraction; it was private to one product while seven other places re-derived it.

  It is one function because `QueueProgress` **subclasses** the bake's `Progress` — both sides
  already share `check()` and `advance(n)`, so this is one protocol, not two that resemble
  each other. The three rules it now owns in one place:

  * a pool is not always faster, so below a per-caller threshold the serial loop wins;
  * the serial path must behave exactly like the pooled one, or a bug appears at `jobs > 1` and
    vanishes under a debugger at `jobs = 1`;
  * cancellation is honoured between results — a pool that never calls `progress.check()` runs
    to completion however hard the user presses stop.

  Five keyword arguments, each a difference I verified rather than assumed: `ordered` (`tiles`
  and two stages need `imap`; the rest do not), `chunksize`, `min_parallel`, `spawn` (the stages
  need it — a forked worker inherits the parent's half-built level), and `advance` — because
  **`stages/networks` counts SEGMENTS and advances by however many a tile held**, so one per
  result would make its bar meaningless. Two stages also checked for cancellation only every
  25th item; every item now, which is strictly more responsive.

  **`bake/areas` is the eighth and stays as it is.** It passes `initializer`/`initargs` so the
  workers own the heightfield raster instead of each copying it — the change that made the areas
  conform parallelisable at all, per that module's own note. That is a memory strategy, not a
  loop, and a sixth knob to express it would cost more than the eight lines it saves.

* **`beamng/level.tsstatic()`** — the TSStatic literal was built by hand in `stages/derived`,
  `stages/instances` and `stages/networks`, three places to remember that `decalType` and
  `useInstanceRenderData` are not optional and that these objects are compared as TEXT between
  builds, so an unrounded float makes an unchanged object look changed. The three roundings
  (position 3, rotation 6, scale 5) turned out to agree, which is why one builder reproduces all
  three byte-for-byte — checked, not assumed.

* **`bake/terraincut` was in none of the estimate's tables, and that was a real gap.** The three
  places that name products hold genuinely different things — `PRODUCT_MODULES` is bake ORDER,
  `census.units` is how many units a product will do, `history.DEFAULT_RATES` is how fast — so
  they cannot be one list. But they enumerate one set, and `terraincut` was missing from two:
  it contributed nothing to the preflight's expected column and its **57.5 s** arrived as time
  nobody had promised. Both are filled in, the rate from the 2026-08-27 rebuild's own report, and
  `test_app_shell.test_bake_product_tables` now fails if a product is missing from any of the
  three. Verified by removing the rate again and watching it fail.

**Refused: unifying `Stage` with `Product`.**

The plan calls them "structurally isomorphic". They are not. `Stage` has eleven fields
(`key title enabled units run inputs settings_keys outputs shared_keys after version`) and
`Product` six (`name inputs path run version options`); `BuildContext` has nine and the bake's
`Context` five, overlapping in three. The shared core is `(name, inputs, run, version, options)`
and the rest is a stage's UI, ordering and cross-stage handover.

Merging them produces a record where half the fields are meaningless to half its users — which is
exactly what `RipOrchestratorSystem.Stage` was, and what **Phase 7 of this same plan told me to
split**, for "carrying two mutually exclusive contracts in one type with no compiler help". The
plan asks for opposite things in two phases. The split is the one that is right, so the merge is
not done and this is the record of why.

What the two registries genuinely share is the *fingerprint* logic, and `pipeline/state.py`
already imports the bake's primitives for it rather than copying them — the plan notes this
itself. That is the seam, and it is already sewn.

**The `ctx.shared` guard: a distinction, not duplication.** Five sites, two spellings.
`(ctx.shared or {})` builds a throwaway dict when `shared` is empty; `(ctx.shared if ctx.shared
is not None else {})` hands back the real one. The two sites calling `.setdefault(...)` —
`stages/derived` and `stages/networks`, both recording which materials they bound — need the
second, because `.setdefault` on a throwaway mutates nothing. Unifying them onto `or {}` would
silently stop both stages recording. Left alone deliberately.

### Phase 4 — DONE 2026-08-28 (in-game look accepted)

The three seams the audit sized biggest are done, each verified before moving on. All three are
behaviour-preserving by measurement, not by reading: the terrain split re-ran `test_clip_cut`'s
full gate against the real bundle (fuzz agrees on 4,000 quads, all six windows within budget),
the navgraph split re-ran `test_navgraph_map` (real graph build), and the colour split was
compared numerically against the pre-split code via `git stash` — `apply()`, both
`colour_management` forms, all matrices, `sources` and `warnings` bit-identical on the example
bundle.

**`terrain.py` (1,563) → seven modules**, per the audit's seams: `clipraster` (scanline fill +
mask morphology), `containment` (the deletion rule), `clipgrid` (`clipvolumes.jsonl` onto the
heightmap; the hole mask), `carriageway` (the ground-height rules), `lattice` + `latticefaces` +
`latticecut` (the corridor lattice, its three face families, the exact prism cut), and
`watersurface`; `ground_tiling` joined `splat.py` beside `GROUND_FALLBACK_COLOUR`. Two API
records replaced loose positional args — `clipgrid.Grid` (the geo frame that was seven scalars
on every signature) and `clipgrid.ClipGrid` (clip_grid's 7-tuple, now named; tuple-compatible).
`fill_clip_quad_rows` became the two span enumerators `spans_scalar`/`spans_vector`, and the
fuzz now forces BOTH on every quad instead of only exercising the vector path on tall ones —
strictly stronger. `contain_faces`' stale "returns four things" docstring tail (it returns
three) was dropped.

**`navgraph.py` (1,249) → eight modules**: `navroad` (the Road/Navgraph model), `navread`,
`navspine` (centerline derivation), `navjunction` (connectors + joints), `navmerge` (the
consumer's node merge; simple graph; components), `navclearance` (the stacked-deck clamp +
`stacked_coverage`), `navreport`, and `polyline` (generic polyline maths beside `bezier`).
`navgraph.py` keeps `build`/`summarise`/`Road`/the category constants — the public entry.
The externally-imported privates are public in their new homes (`navmerge.simple_graph`,
`navjunction.terminal_widths`), and two dead parameters died: `_connectors`' unused
`max_width`, and `build_lattice`'s unused `strip_geo`.

**`colour.py` (944) → four modules**: `colourspace` (transfer curves, Rec.709<->ACEScg, the
ported HDRP matrix operators), `colour` (the `Grading` port, `__init__` decomposed into
per-block parsers), `colourocio` (view picking, on-screen evaluation, the generated view
transform, the SMH LUT), `illumination` (sun/moon/sky/clock). And the module got the test the
plan ordered: `tools/test_colour.py`, 27 checks over the transfer pair (all 256 bytes
round-trip), the HDRP operators, the SMH asymmetry, the contrast algebra as a closed form, and
the example bundle's grading end to end. `colour_model.py` keeps its CLI surface, retargeted.

Also in this block, before any file moved: `each()`'s docstring had pushed it to 67 lines and
the check_limits gate was RED — Phase 3 landed without re-running it (the gate counts a def's
full span, docstring included). Trimmed and committed separately (`3497d23`).

**`validate_bundle.py` (1,614) → `tools/bundlecheck/` + a 260-line entry**, honouring its
stdlib-only contract package-wide (`bundlecheck` imports nothing but the standard library and
says so). `bundlecheck.schema` is the general JSON-Schema-subset library the plan named;
`report.Context` is the one blackboard every check takes — `check(ctx)` replaced the five
incompatible `_check_*` signatures, so `validate_bundle.py` runs them as the registry the plan
asked for (`polygons` §8b/§8c/§8d with the triangle rule written once instead of three times,
`auxlayers` §5A/§7.2/§10/grading, `rasters`, and the joined passes in `assets`). Verified two
ways: output byte-identical to the old validator on the example bundle AND on the full city
(old copy run via `git show`, no stash), and `test_bundle_contract`'s 88 mutation cases green.

**`make_example_bundle.py` (915) → `tools/examplebundle/` + a 72-line entry** (`payloads`,
`layers`, `ground`, `grading`, `manifest`). The gate that matters: regeneration is still
**byte-identical** to the committed `docs/example-bundle`.

**`probe_point.py` — the file split is REFUSED, with the reason now in its docstring**: the
documented way to run it is to paste the whole file into Blender's Scripting tab, so it stays
one file, allowlisted, as a declared exception (and Phase 2 had already ruled its PNG/sRGB
copies stay by contract). Its four over-60 functions were decomposed in place instead, and its
two calls onto the removed `Grading.on_screen`/`match_value` methods — the one breakage the
colour split caused anywhere — now route through `csiibundle.colourocio`.

**Test files, first block (2026-08-28).** Each split had to decompose its over-60 functions in
the same move — the shrink-only allowlist cannot re-admit an entry at a new path — and each was
re-run before and after with identical results:

* `test_clip_cut.py` (557) → the windows gate (260) + `test_clip_fuzz.py` (the scanline fuzz,
  no bundle needed) + `cliptruth.py` (the independent truth rasteriser — support, not a test).
  Same window numbers to the digit; the fuzz still agrees on the same 4,000 quads (the rng
  sequence was preserved) and now reports every disagreement instead of the first three.
* `test_terrain_blend.py` (622, a SCRIPT whose body ran at import — its own gate() docstring
  said "Phase 4 turns the body into real per-claim functions") → per-claim functions split as
  `test_terrain_blend.py` (the ported shader + HEIGHT_SIGMA + tier encoding) +
  `test_terrain_tiling.py` (the bundle-facing half). 19 + 28 = the original 47 checks.
* `test_a2_dae.py` (563) → the writer/frame half + `test_a2_materials.py` (material_v15,
  texture export PNG/DDS, the compiler's placeholder files). 26 + 32 = the original 58 checks,
  and one dead statement removed (an `export_texture` call guarded by a file-exists test that
  was always false).
* `test_lod.py` (418) → the LOD arithmetic (325) + `test_lod_displacement.py`.
* `test_drape_pick.py` (446) → the gate (decomposed `main`) + `drapetruth.py` (the raw-.glb ray
  target). Same sweep numbers.
* `check_instances_vs_game.py`'s 113-line sampler decomposed in place.

**Test files, second block (2026-08-28, while the A/B rebuild ran).** Same discipline —
decompose the over-60 functions in the move, re-run before and after:

* `test_bundle_contract.py` (986, one 949-line class) → `contractbase.py` (the pristine-copy
  base every case mutates) + four files by contract section: the manifest/records core,
  `test_contract_assets.py`, `test_contract_lanes.py`, `test_contract_ground.py`. All 88
  original cases pass across the four.
* `test_terrain_fine.py` (573) → the reader/hole-mask/fidelity half + `finecopy.py` (the
  fine-raster example copy) + `test_terrain_fine_stage.py` (the cs2beamng terrain stage, the
  terraincut ground meshes and the watertight seam). 13 + 13 checks, all green.
* `test_estimate.py` (567) → the arithmetic/selection tiers + `test_estimate_build.py` (the
  real-build tier, decomposed to per-claim helpers).
* `test_bake_products.py` (466) → the example-bundle half + `test_bake_products_real.py`
  (the real-bundle region bakes).

**The rebuild (refactor Rule 6), 2026-08-28.** The whole bake re-forced through the split
code (the first attempt was killed externally mid-`areas` and resumed with `--only` on the
four unfinished products), then `cs2_city_fine` rebuilt in place, `cook: true`, settings from
its own `cs2beamng_settings.json`, `--force`, 18m56s, status done. **Against the after-phase3
baseline report, all 79 validation-check fields are identical and the problems list is
identical** (the same 1: the standing budget-band warning) — objects, classes, forest items
and types, cooked shapes, navgraph (roads 15,393 / nodes 51,197 / endNodesMerged 18,703),
ground check, ter, spawn points, and the residency bytes to the decimal (3,482.6 MiB mesh +
790.1 MiB texture = 4,272.7 MiB). `cs2beamng budget` is **byte-identical** to the baseline
capture. `check_instances_vs_game` on the rebuilt level: 100 % on every exact question over
800 sampled records, 15,697 TSStatic colorSets and 892,332 forest paints. The fresh report is
`docs/baselines/cs2_city_fine-report-after-phase4a.json`. The in-game look remains owed to
Naomi before the phase closes.

**A defect found by the rebuild itself (2026-08-28), fixed as its own commit per Rule 1:**
`cs2beamng build --settings` read only a FLAT dict and silently applied nothing from the app's
own saved `cs2beamng_settings.json` (whose shape is `{"schema", "settings"}` — every key fell
outside SCHEMA), so the build ran on the app settings store instead and rebuilt whatever level
THAT named. The first A/B rebuild of this phase went to `cs2_city_network` because of it. The
reader now unwraps the saved shape and refuses a file from which nothing applied, instead of
proceeding silently.

**Second landing (2026-08-28): the whole `csiibundle` block is under the ceiling.** Every move
verified behaviour-preserving — the `materials` bake re-run on the example bundle after each
split of its code and compared identical mod `generatedAt`, and the targeted test sets green
after each module's split. New modules, all split on story seams with the measured-rule
docstrings moved verbatim (wide-use names re-exported from their old homes, the pattern the
navgraph split set):

* `bake/materials.py` 818 → 277 over four modules: `bake/materialmaps.py` (the per-file pool
  jobs + `CHUNK`), `bake/emissive.py` (the colour-slot join; its coverage story is the module
  docstring), `bake/paint.py` (the whole §7.2 pipeline — k-means, prefab materials, group-by-
  source-albedo, factor split, the three-pass tint orchestration), and `run()` decomposed into
  per-phase functions.
* `decimate.py` 692 → 368 + `displacement.py` (level_displacement / distance_to_surface / the
  triangle grid; `vertex_cluster` and `distance_to_surface` decomposed under 60). Retargeted
  `bake/lods.py` and `test_lod_displacement.py`; a latent shadowing defect surfaced in the
  move — `lods.py` had a local list named `displacement`, renamed.
* `drape.py` 613 → 200 + `projection.py` (the protocol + `HEIGHT_CEILING`),
  `heightsurface.py` (`HeightfieldSurface`), `meshsurface.py` (`TriangleSoupSurface`;
  `_query` decomposed). Both surface classes re-exported from `drape`.
* `instances.py` 601 → 357 + `skirt.py` (§4.1), `density.py`, `orphans.py` (self-contained on
  `reader`, no import cycle).
* `materials.py` 527 → 335 + `normalmaps.py` (packing detection) and `shaderonly.py` (the
  textureless tables + substitutes); `resolve` stays in place.
* `glb.py` 521 → 354 + `glbjson.py` (JSON-chunk sweeps) and `meshclean.py`
  (`clean_degenerate_polygons` decomposed; its policy is the module docstring).
* `lanes.py` 474 → 364 + `lanetile.py` (the rigid-tile gate + quaternion helpers;
  `rigid_tile_matrix` decomposed via `_straight_chord`).
* `bake/markings.py` 439 → 385 (`bake/markinglod.py`: the per-prefab/per-decal `m_MinLod`),
  `bake/areas.py` 433 → 334 (`bake/areasclip.py`: the A6.5 decalLayers clip, decomposed),
  `bake/terraincut.py` 401 → 400 (docstring rewrap).

`check_limits` is **clean** — 0 unallowlisted violations; the csiibundle allowlist is down to
19 entries (the deliberately-kept long functions: the product `run()`s, `bend_part`,
`_parse_glb_unguarded`, `resolve`, and kin), from 248 at the phase's start.

**Second-landing Rule-6 rebuild, verified (2026-08-28).** Full `bake --force` + `cs2_city_fine`
build ran clean in 16 m 52 s (7,883 shapes compiled, validate problems = the standing budget
advisory only). Against the pre-refactor after-phase3 report every build-stage count is
IDENTICAL; the only differing fields are environmental (the budget bands ride the rolling
build history and this run's beamng.log logged no resolution, so the estimate fell back to
reference 1080p) or recording artifacts (phase 3 served the materials product from cache and
logged no stats; `texturesReused` counts what the previous build left on disk). Baseline
captured as `docs/baselines/cs2_city_fine-report-after-phase4b.json`. Two notes on the
after-phase4a baseline this supersedes: its `estimate` block was the anomaly (prop 4,400 /
tree 533 shapes against phase 3's and this run's identical 2,852 / 270 — a snapshot from the
settings-bug turbulence), and its `tintedTextures: 0` vs this run's 56 is not a behaviour
delta — the stat counts textures NEWLY WRITTEN (an existing right-sized file is reused, the
encode being deterministic), and the behaviour-bearing split, 1,770 as-factor / 3,190
as-texture, is identical in both. The level is rebuilt in place and awaits the in-game look.

**Third landing (2026-08-28): the phase's work list is drained.**

* The two vs-game probes are **declared single-file exceptions**, not splits (`bbe7980`):
  `check_roadgrade_vs_game.py`'s nine numbered sections are one linear derivation, each
  consuming the previous one's rasters, and `check_instances_vs_game.py` is read-the-level,
  ask-the-five-questions, print-the-score. Both declare it in their docstring and ride the
  shrink-only allowlist; every function in both is already under 60.
* **The A3 gate is three files** (`e7d6eb6`): the stage tests stay in `test_a3_objects.py`
  (1,315 → 640), the writer rules move to `test_a3_rules.py` and the classification rules
  plus real-city sweeps to `test_a3_city.py`, tally shared via `a3common` (whose `start()`
  reset keeps one pytest process from leaking failures across files). The five moved over-60
  tests are decomposed per claim. 188 checks before, 76+74+38 = 188 after.
* **The A0 gate is three files** (`e24747b`): the shell proper stays in `test_app_shell.py`
  (1,692 → 1,036; its four big tests keep their allowlist keys), the A4 incremental build
  moves to `test_app_incremental.py` and the delivery features to `test_app_features.py`,
  harness shared via `shellcommon.run_gate`. The moved over-60 tests are decomposed per
  claim. 232 checks before, 131+69+32 = 232 after.

With that, every seam this phase named is closed: the csiibundle modules, `validate_bundle`
(→ `bundlecheck`), all four oversized test files, and `probe_point`. What stays on the
allowlist is the durable tail this phase never claimed — the C# producer files (later
phases), the app and its stages (Phase 5), the Blender adapter, and the deliberately-kept
long functions (`run()`s, `bend_part`, `_parse_glb_unguarded`, `resolve`, and kin), every one
under the shrink-only rule. The phase's last gate — Naomi's in-game look at the rebuilt
`cs2_city_fine` (second-landing rebuild, verified against after-phase3) — was **accepted
2026-08-28**, which closes the phase. Phase 5 (the app and its UI) has not been started.

### Phase 9 — in progress, 2026-08-27

**Landed**

* **"Reset to defaults" did not reset `RipTextureSize`.** `Settings.cs` states every default
  twice — inline on the auto-property and again in `SetDefaults()`, which is what the options
  screen's Reset button calls — with nothing keeping the two in step. All 147 pairs checked: none
  disagree on a value, but `RipTextureSize` had an inline `= 1024` and **no `SetDefaults` line**,
  so Reset restored every other ripper setting and left the texture size wherever the user had
  put it. Fixed, and `tools/test_settings_defaults.py` now fails on either failure mode — a
  missing assignment or a disagreeing value. Verified by reintroducing each and watching it fail.
* **The six fork-declared `m_PascalCase` fields are `_camelCase`**, which is what `.editorconfig`
  codifies for this repo. 41 renames, compile-checked.

**Wrong in the plan**

* **The two halves of `NetworkRipSystem` do not use "opposite field-naming conventions".** Almost
  every `m_Name` in those files is the GAME's — `m_Bezier`, `m_Edge`, `m_StartNode` are CS2's own
  ECS component fields, read and not ours to rename. Only **six** declarations were the fork's,
  four in `.GameMesh.cs` and two in a nested struct in `NetworkRipSystem.cs`.
* **`SetDefaults` has 17 assignments with no inline initializer, and that is correct.** They are
  the ripper's `RipObjects` family, which are full properties over backing fields — a master's
  setter writes all its subs and a sub recomputes its master. A property with a hand-written
  setter cannot carry an inline initializer at all. This is the same mechanism behind the plan's
  own warning that `Settings.cs`'s declaration order is load-bearing.

**Not done.** The two god-methods — `RipTerrain` (332 lines, and its duplicated `4b`/`4c` section
labels are real, at `:351/380` and `:361/385`) and `BuildGameComposition` (628, the longest in the
repo) — plus the `AreaRipSystem` extraction. `RipTerrain`'s section 5 alone is 176 lines and hands
about fifteen locals to the write pass, so it needs a captured-state record rather than a
parameter list. That is a focused block of work, not something to leave half-finished.

### Phase 8 — in progress, 2026-08-27

**Landed**

* `IO/JsonLinesWriter.cs` — the base `InstanceWriter`, `LaneWriter` and `NetworkWriter` were three
  copies of. Their own docstrings admitted it (`LaneWriter`: "The same shape as `NetworkWriter`
  and for the same reason"). The stream, the count, opening through `BundleFile`, serialising with
  no formatting and disposing move up; what stays is the one thing that differs, which is which
  fields a record contributes.

  **And it is testable, where the three copies were not.** It needs only `BundleFile` and
  Newtonsoft, both already in the test project, so it links — five new tests take the suite from
  19 to 24 and pin the invariant all three writers depend on: `Count` counts a line that was
  written and not one that was skipped. `LaneWriter` drops a path of fewer than two points, and a
  bundle whose manifest counts that record has a manifest disagreeing with its own layer. Also
  pinned: field order (the layers are diffed between rips), an empty layer is an empty file and
  not a missing one (§10 distinguishes those), and no BOM / no CR.

* `GeometryAssetReader.ReadAttribute<TPacked, TValue>` — `ReadFloat3`/`ReadFloat2`/`ReadFloat4`
  were twenty-one lines three times, and the second and third carried
  `<inheritdoc cref="ReadFloat3"/>` as an admission. What they share is not the shape but three
  rules: ask for the attribute's format, treat dimension zero as "no such attribute" rather than
  an error, and treat a native throw the same way so a bad prefab costs its own mesh and not the
  rip. `select` runs per vertex — a delegate call on ~20.7 M triangles, which is well under a
  tenth of a second inside a stage that takes 1,683.

* `GradingWriter.Applied<T>` — "null, or carried but switched off, means the game is not applying
  it" was written at ten sites. Six were the plain guard, two add a condition of their own
  (`SplitToning.IsActive()`, `Fog.enabled.value`) which stays beside the call because it is
  theirs, and two are positive-form checks inside `CaptureLighting`. All ten now ask one function.

**Wrong in the plan**

* **The `["units"]` prose cannot leave the payload: it is `required` in the frozen schema.**
  `docs/schema/grading.schema.json` lists `"units"` in the `required` array of every block that
  has one, so removing it would fail `validate_bundle.py` on every bundle ever ripped. Schema 2 is
  the frozen contract (CLAUDE.md, and the 2026-08-09 freeze). Moving the prose to the docs is a
  schema change with a version bump behind it, not a tidy-up — and worth doing on its own terms,
  since the strings are a real share of `grading.json`.
* **Three of the four deletions were already done**, in `a4acd61`: `BundleFile.OnDisk`,
  `MeshRipSystem`'s two dead queries, and `NetworkRipSystem.SanitizeId` — which was **not** dead,
  as that commit recorded.

**Not done yet:** the six layers that bypass the IO layer (`districts.jsonl`, `areas.jsonl`,
`clipvolumes.jsonl`, `laneprefabs.json`, `materials.json`, `colors.json`/`lights.json`, written
inline with `BundleFile.CreateText` + `JsonConvert`), and settling `BundleWriter` on one JSON
idiom.

### Phase 7 — in progress, 2026-08-21

Started out of order, and deliberately: the Phase 1 consumer rebuild is a multi-hour block during
which the Python tree has to stay still, and the two halves of this repo have independent gates.

**Landed** (`a4acd61`, `32855e1`, `303715e`)

* `Systems/Rip/RipAssetIds.cs` — what an `assetId` is, out of `MeshRipSystem`, where it was
  `public static` and five systems reached for it. `Digest` came with it, deleting the
  byte-identical copy in `MaterialRipSystem` whose own comment admitted it.
* `Systems/Rip/RipDiagnostics.cs` — `DescribeComponents`, byte-identical in two systems.
* `Systems/Rip/RipOutcome.cs` — a stage says whether it failed. The `FAILED:` prefix was written
  by twelve sites and read by one, `StartsWith("FAILED", Ordinal)`; a stage writing `"Failed"` —
  which all five already write to their logs — was silently a success.
* The `Stage` union is two types. `SyncStage` and `AsyncStage`, and `StartStage` switches on the
  type rather than on a null check standing in for one.
* `Systems/Rip/RipWalk.cs` — the frame loop. `InstanceRipSystem.Advance` and
  `LaneRipSystem.Advance` were byte-identical for **57 lines**, comments included; 114 lines of
  duplicated loop are 46 of call site. The tuning stays per-stage: the instance walk's 12 ms
  against the mesh rip's 6 is a measured choice with its reason recorded, and a shared constant
  would have thrown that away.

**Landed 2026-08-27**

* `Systems/Rip/RipReport.cs` — the completion-callback-or-dialog shape, which the plan counted at
  16 sites and is 8: a success path and a failure path in each of the four walking stages. Every
  copy carried the same two try/catch blocks for the same two reasons (a callback that throws must
  not take the stage down; `ShowMessageDialog` throws when the UI bindings are not up). **99 lines
  out of the four systems, 71 back in as call sites.**

  What the extraction had to NOT do is more interesting than what it did. The three stages
  disagree about which audience gets which text, and a naive collapse would have silently changed
  two of them:

  | | to the orchestrator | to the modal |
  | --- | --- | --- |
  | instance, lane | the **full** report | one line |
  | mesh | the same summary | the same summary |
  | network | one line | the **full** report |

  So `dialogText` is a separate argument, passed explicitly at all eight sites, and the network
  one carries a comment saying it inverts the other two on purpose. Settling that disagreement
  changes what a rip reports and is not a move.

  One thing the extraction DID settle: the dialog text now goes through `Settings.Fit`, which
  caps it at 18 lines and points at the log for the rest. The network rip and the orchestrator
  already fitted theirs; the mesh rip handed its full report straight to a modal that cannot
  scroll. That is a behaviour change, declared here.

* **Five stale doc comments**, all describing a protocol `32855e1` had already deleted:
  "hand a summary with the `FAILED` prefix, which is what the orchestrator counts a stage failure
  by" (three sites) and "the orchestrator counts a stage failed by its summary's FAILED prefix"
  (two). It counts a typed `RipOutcome.Failed` now. The word FAILED is still in the summary text,
  because a person reads it — the comments now say which is which.

**Still not done.** `MeshRipSystem` and `NetworkRipSystem` keep their own frame loops, and on
inspection that is right rather than pending:

* `NetworkRipSystem.Advance` walks **three** lists — edges, then orphan nodes, then lane geometry
  — under one shared frame budget, each with its own index and its own back-pressure check.
  `RipWalk.Advance` is a single-list loop; a "sequence of lists" abstraction to fit it would be
  more machinery than the 40 lines it would save.
* `MeshRipSystem.Advance` is the single-list shape, but it reports `Phase.Write` progress on every
  frame of the DRAIN, which `RipWalk` does not, and its per-item catch is inside `ReadOne` rather
  than in the loop. Fitting it needs a ninth parameter on a function that already takes eight —
  the point at which a parameter list is a design smell rather than a shared rule.

**Not done, and not for lack of time: `Tessellate` should not be unified yet.**

The plan lists it as "two divergent sampling policies for the same bézier". They are divergent,
and the divergence is not accidental:

| | `LaneRipSystem.cs:616` | `NetworkRipSystem.cs:2190` |
| --- | --- | --- |
| samples | `Samples = 8`, fixed | `ceil(length / TessellationGap)`, adaptive |
| straight curves | sampled 8 times anyway | short-circuited to two points |
| duplicate points | skipped at `1e-6` | skipped by `Append` |

The network one is better on both counts. But adopting it for lanes changes `lanes.jsonl` — a
**676,863-record layer** — so it is a producer output change, not a refactor, and Rule 1 forbids
folding it into a move. It needs its own commit, its own measurement of what the record count and
the file size do, and an answer to whether 8 points was ever a considered number. Left as it is,
named here so the next pass does not "tidy" it by accident.


### Phase 5 — in progress, 2026-08-28

Three of the phase's named targets are done. **This is the first phase whose headline numbers
the plan got exactly right** — `settings.py`'s sixteen tables are 79/79/79/35/23/15/9/5/3/3/3/2/2/1
plus `SECTIONS` and `GROUPS`, counted — and two of its supporting claims were still wrong.

**`settings.py` (1,724) → a 17-module package** (`ecbdcbc`). One `Setting` record per key,
every table derived. `label` and `blurb` are required arguments, so the failure mode that
mattered — a key with no `LABELS` entry raising `KeyError` on the settings page, at render time,
in front of the user — is a `TypeError` at import. The 79 declarations are grouped as the screen
groups them (`paths world collision textures navigation tuning build driven`); `sections.py`
stays the placement authority, `screen.py` is the view model, `values.py` the coercion,
`store`/`presets` the persistence, `discovery` the per-OS paths.

Proved by a 37-observable before/after snapshot (every table, `schema_for_ui`, `coerce` over
14 probes x 79 keys, `validate`/`region_of` over 11 dicts, every preset applied and diffed).
**Two entries differ and both are intended:**

* `region_of` no longer raises. It parsed the region string and `validate` parsed it again, and
  the two disagreed: `"1 2 -3"` was a printed problem to one and a region of radius **-3** to the
  other; `"nonsense"` was a printed problem to one and a `ValueError` out of the cost meter to
  the other. One `parse_region`. Every build path already refuses to run with validate problems,
  so the only reachable caller is the settings page's estimate.
* `schema_for_ui` order. All 79 entries content-identical; the sequence is the screen's own,
  which is what `docs/settings.md`'s header has always promised and did not deliver — the
  Advanced section was listed in SCHEMA order, not the order it is drawn in. Both generated
  artifacts are pure reorders, verified by sorting.

2,346 lines against 1,724; the 622 are one field per line where sixteen tables held the same
facts positionally.

**`server.py` (834) → `server/`** (`75b85de`): `routes` (the shell), `app` (`AppServer`),
`caches` (`BundleCaches`), and the map in four. The route table is not cosmetic — the token check
was a statement in the MIDDLE of the GET chain, so whether a new route was authorised depended on
where it was pasted, and `/api/start` is the one route answering `(payload, status)` where every
other branch called `self._json(x)`. Both are columns of the table now. `pipeline/mapview.py`
(444) moved out of `pipeline/` as the plan asked and split four ways, the fourth
(`mapstore`) falling out of a real cycle: `mapview.build` calls `mappoints`, and `mappoints.snap`
needs the cache key and stored document `mapview` owned.

**`validate_level.py` (1,009) → `levelcheck/` + `machine.py`** (`836632d`). `check()` is **549
lines, not the plan's 490**, and the file held a second thing the plan does not mention: a model
of the MACHINE (`resident_budget_mib`, `render_target`, `light_manager`,
`resident_failure_mib`), which has nine callers across `settings.discovery`, `doctor` and
`estimate` — **none of which is validating a level**. Twelve `check(ctx)` functions over a
`LevelContext`, the Phase-4 registry shape, every one under 60 lines. Proved by running the old
`check()` and the new one against the built `cs2_city_fine`: problems list identical, every check
measurement identical.

The section label at `:261` was wrong and had been: "collisionType must match what the shape
actually carries" headed 198 lines of which 38 are about collisionType, the other 160 being the
material and shape-geometry checks under no heading at all.

**Wrong in the plan**

* **The Steam `.vdf` parser duplicates nothing.** `bake/locate.py` has no `.vdf` parser and needs
  none: it finds the CS2 *bundle*, which sits at a fixed path under a Steam root, so where CS2 is
  INSTALLED never comes up. Only BeamNG's executable can be on a second drive. What the two did
  share is the list of Steam roots, in three copies; that is `locate.steam_roots_linux` now
  (`26b44b1`), which adds `~/.steam/root` to two of them — a declared Linux-only change.
* **`GROUPS` is not a partition to reconcile — it is dead** (`458c8a4`). `schema_for_ui`'s
  `group` and `settings_payload`'s `groups` are read by nothing; the UI's `sec.group` is a
  SECTION's band, the same word for a different thing. Its only reader was the test asserting it
  partitioned SCHEMA, and that check could not fail without the stricter `section_of` partition
  at `test_app_shell.py:285-304` failing first.
* The region parse is in **four** places, not two: `tools/check_instances_vs_game.py` carried a
  private copy the plan never counted.
* 19 routes, not 23.

**A regression this work introduced, and how it was found** (`e7ee137`). The settings split left
`detected()`'s `from .beamng import machine` one level too shallow, and the block's blanket
`except Exception` — which exists because a missing BeamNG log must not blank the settings page —
caught the `ImportError` and set `budget` to None. That is indistinguishable from "BeamNG has
never been run here", so nothing failed and no test could have caught it; a machine with no
BeamNG is a legitimate answer. Found while repointing the callers at `beamng.machine`. The guard
now carries `budgetError`, because one that hides its own bugs inside the condition it was
written for is not a guard.

**`stages/shapes.py` (1,312) → seven modules** (`419948f`), the largest stage and the one with
the most over-length functions: six of them, 794 lines between them. `shapeparts` exists because
of a cycle rather than a wish — `lod_levels` reads the bake's LOD glbs through `_collect`, which
the shape writer also uses. **`ShapeJob` is the consumer's second `@dataclass`**: `shape_job` took
a 14-element tuple and the two places building one, forty lines apart, each listed all fourteen
in order with nothing checking they agreed. `lod_levels`' `if`/`elif` repeated eight of its nine
conditions (the promote branch and the kept-small branch differ only in the triangle floor), so
the eight are one named condition now.

**`stages/terrain.py` (1,049) → five modules** (`ceed967`), which the plan sized as "the ~290-line
texture-authoring block" and is four subjects: the stage, the textures, the tiling and the
materials. The `# ---- NOT PARITY` fence survives verbatim with the constants it fences, as does
every other measured rule the file carries. `terrain_materials` was 226 lines and `_ground_mesh_maps`
68; both decompose along banners the code already had. Two module-attribute shadows that ruff
caught and a different structure would have hidden: `albedo_gain = albedo_gain(...)` and
`= terrain_materials(...)`, where a local took the imported function's name.

**`ui/app.js` (1,667) → nine ES modules, `ui/app.css` (576) → five sheets** (`653e642`). One IIFE,
71 functions sharing one closure, so every function could reach every other and nothing said which
were meant to. **`connectEvents` could not stay in `core`** — it calls three renderers, so the
module every other one imports was importing the screen; it is in `app.js` with the rest of the
wiring and `core` now imports nothing. All 303 CSS rule blocks are byte-identical, checked by
parsing both and comparing the sets.

Verified by **driving the browser**, because no test runs this code: the first split loaded and
died on `buildControl is not defined`, a real import the scanner missed because it blanked
template literals and `${buildControl(x)}` inside one is code. After the fix the page renders all
six cards, the cost meter, every settings section, the reference page, the map with its road
network and the POI suggestion chips. `test_app_shell` now asks for every static module rather
than `app.js` alone — a missing one is a blank screen and the old check passed straight through it.

**The 400-650 band is done** — fifteen more files across three landings (`f9c4c4a`, `a989fbd`,
`3c2df72`), each split along a section banner it already carried: `estimate` into four,
`instances`, `materials`, `runner`, `poi`, `beamng/materials`, `tilelod`, `networks`, `budget`,
`worker`, `dae`, `cook` and `state`. **Every `apps/cs2beamng` module is now under the ceiling**;
what is left on the allowlist is the C# producer (Phases 7-9), the Blender adapter (Phase 6), and
the exceptions Phase 4 declared.

**Three findings worth keeping from that band.**

* **Six more import cycles, all the same shape.** A module that composes two others cannot also
  be what they share, so `estimatebase`, `shapeparts` and `mapstore` exist; and where the
  dependency was genuinely one-way the function moved to the side that owns it
  (`write_ladder_index` to `cooktidy`, `payload_path` back to `state`).
* **A split changes a module's ADDRESS, and the linter cannot see it.** `beamng/materials` kept
  five names and lost nine to `textureplan`; `tilelod` kept three and lost eight; `state`,
  `dae` and `cook` the same. Every time ruff was clean and a build died —
  `tilelod.new_stats`, `bm.dds_mod`, `state.diff_shared`, `dae.glb_to_local`,
  `cook._write_ladder_index`. **`bm.dds_mod` should never have existed**: one module reaching
  through another for its import. What caught all five was a headless build of the example
  bundle, three seconds a time, after every fix.
* **The full suite caught the one that mattered most.** `worker.py` scores every finished build
  through `estimate_mod.LEARN_FLOOR_MIB`/`.score`/`.observe`, and after the estimate split those
  three pointed at nothing. Five of the six failures that run were tests reaching for old
  addresses; the sixth was a production path.

#### The closing Rule-6 rebuild (2026-08-28)

`cs2_city_fine` rebuilt in place, `cook: true`, `--force`, 16 m 42 s, `status: done`, 7,883
shapes compiled. Against `after-phase5a` — the baseline Naomi's accepted in-game look was taken
on — **671 report fields are identical, every validation check is identical, and the problems
list is byte-identical** (the one standing budget-band advisory). Resident 4,272.7 MiB and
1,292,894 triangles drawn, both unchanged. All 27 differing fields are inside the `estimate`
block, which is the meter's rolling calibration on the last builds' own measurements — a build
moves it by design. Baseline captured as `cs2_city_fine-report-after-phase5b.json`.

**The rebuild found the seventh and eighth address breaks, and neither was reachable any other
way.** `cook.run` did `tidy = tidy(...)`, an `UnboundLocalError` the moment the split made
`tidy` an import — and the cook stage does not run on the example bundle, so only a real
16-minute city build could reach it. Then `cs2beamng budget` was dead: `main` had moved to
`budgetreport` and the command reached the user through nothing else — no test, no stage — so
the whole suite stayed green over it.

Both classes are now gated. `tools/check_shadows.py` walks the AST for a local that shadows an
import (ruff's F823 catches only some shapes of it; this phase produced seven), and
`test_a1_level` `--help`s all six CLI entry points, which is enough to prove their imports.

**Naomi's in-game look at the rebuilt level was accepted 2026-08-28, which closes the phase.**

### Phase 5 — DONE 2026-08-28

Every `apps/cs2beamng` file is under the 400-line ceiling and every function in the app is under
60. The allowlist went **222 -> 187**; what remains on it is the C# producer (Phases 7-9), the
Blender adapter (Phase 6), and the exceptions Phase 4 declared. Twenty-four commits, seven of
which are defects found on the way and fixed as their own commit per Rule 1.

**What landed**

* `settings.py` (1,724) -> a 17-module package, one `Setting` record per key, every table derived.
* `server.py` (834) -> `server/`: the route table, `AppServer`, `BundleCaches`, and the map in
  four (`mapview` / `mapimage` / `mappoints` / `mapstore`).
* `validate_level.py` (1,009) -> `levelcheck/`, twelve `check(ctx)` functions over one context,
  plus `beamng/machine.py` — the model of the MACHINE, which is what most of that file's callers
  actually wanted and which the plan never mentioned.
* `stages/shapes.py` (1,312) -> seven modules and the consumer's second `@dataclass`.
* `stages/terrain.py` (1,049) -> five, with the `NOT PARITY` fence intact.
* `ui/app.js` (1,667) -> nine ES modules; `ui/app.css` (576) -> five sheets, all 303 rule blocks
  byte-identical.
* The 400-650 band, fifteen files, in three landings.
* Seven defects fixed as their own commits, including one this work introduced (the settings
  page's target-machine line, blanked by an import one level too shallow behind a broad `except`).

**What the phase cost, and what the plan now does about it.** Fourteen full `pytest` runs at ~10
minutes, three full-city rebuilds at ~16 minutes, and a fourth wasted on a crash — over three
hours of waiting, most of it buying nothing. **Eight of the phase's failures were a moved
ADDRESS**, a one-second question answered fourteen times by a ten-minute instrument. Verification
now has a measured ladder (see "How to verify a move" above), two new one-second gates
(`check_references`, `check_shadows`) that between them catch every address break this phase
produced, and a `slow` marker that makes the iteration loop 2 m 45 s instead of 10 m 24 s.

**Three findings about the work itself, kept because they will recur in Phases 6-12.**

* **A split changes a module's ADDRESS, and nothing in the language notices.**
  `beamng/materials.py` kept five names and lost nine; `tilelod` kept three and lost eight. Every
  caller still parsed and still linted clean. This is the single most common failure of the whole
  phase and it now has a 1.3-second gate.
* **The gates themselves have blind spots, and the way to find one is to reintroduce the bug.**
  `check_limits` read `git ls-files` — tracked files only — so a new file was invisible until
  committed, which is precisely backwards for a refactor. It reported clean on a commit with
  three over-limit functions. Every gate written since has been proved by putting the failure
  back and watching it fail.
* **The plan's numbers are hypotheses.** Phase 5 is the first whose headline count was exactly
  right (`settings.py`'s sixteen tables), and four of its supporting claims were still wrong: the
  `.vdf` parser duplicates nothing, `GROUPS` is dead rather than a partition to reconcile, the
  region parse was in four places not two, and there are 19 routes not 23. — `stages/instances.py`,
`pipeline/estimate.py`, `stages/materials.py`, `pipeline/runner.py`, `beamng/budget.py`,
`pipeline/worker.py`, `beamng/dae.py`, `stages/cook.py`, `pipeline/state.py`,
`beamng/tilelod.py`, `stages/poi.py`, `beamng/materials.py`, `stages/lights.py`,
`stages/networks.py`. Then the Rule-6 rebuild of `cs2_city_fine` and the in-game look.

**The stage-to-stage private import is closed** (`9c840d5`): `stages/lights.py` imported
`_transform` out of `stages/instances.py` and so did `check_instances_vs_game`. Phase 2 removed
the `_quat_rows`/`_swap_yz` imports from that same call site and left this one, so the standing
gate has been reporting clean against a breach it could not see. It is `beamng.frame.record_transform`
now — "a bundle record as the three fields a BeamNG SceneObject stores" was never the instance
stage's fact.

**A flaw in the size gate itself, found by it failing to fail** (`3330f94`).
`check_limits.fork_sources()` listed `git ls-files` — TRACKED files only — so a file written but
not yet `git add`ed was invisible. Running the gate before committing a split, which is exactly
the moment it is for, read the old files and skipped every new one. **The `levelcheck` commit
(`836632d`) says "every one is under 60 lines" and three were not** (`check_info` 86,
`check_materials` 105, `check_budgets` 61); the gate had never read those files. Fixed with
`--others --exclude-standard`, which took the file count 298 → 315 — the size of the blind spot
on this tree alone — and the three functions are decomposed. Nothing else in the phase was
affected: the settings and server packages are clean under the fixed gate.

**The Rule-6 rebuild for these landings is still owed**, and every commit above says so. The
shapes split is the one that most needs it: it writes every `.dae` in the level.

#### The Rule-6 rebuild for Phase 5's first block, verified (2026-08-28)

`cs2_city_fine` rebuilt in place, `cook: true`, `--force`, settings from its own
`cs2beamng_settings.json`, BeamNG closed. Against
`docs/baselines/cs2_city_fine-report-after-phase4b.json`, **645 report fields are identical and
every build-bearing number is unchanged**:

| | after-phase4b | after this block |
| --- | ---: | ---: |
| objects | 35,019 | 35,019 |
| cooked shapes | 7,883 | 7,883 |
| forest items / types | 908,841 / 3,104 | 908,841 / 3,104 |
| navgraph roads / nodes / merged | 15,393 / 51,197 / 18,703 | identical |
| resident mesh + texture | 3,482.6 + 790.1 = 4,272.7 MiB | identical to the decimal |
| missing files, unbound materials, oversize geometries, invalid details | 0 | 0 |
| `classes`, `ter`, `groundCheck`, `collisionTypeMismatch` | | all identical |

That is the proof the shapes split needed: it rewrites every `.dae` in the level, and the level
came out the same.

**76 fields differ and every one is accounted for.** The budget band moved
(`budgetPixels` 2,073,600 → 4,953,600, `budgetSource` "REFERENCE 1080p, no resolution logged" →
"resolution from beamng.log"), which is a property of the machine and not of the level — the
level mounts the same 4,273 MiB either way, and the single validation problem is the same
standing budget-band advisory with the same mounted figure and a different threshold beside it.
The cost estimate's predicted bytes ride that band and the last bake. `materials` was served from
cache so it logged no stats, and `texturesReused` counts what the previous build left on disk —
both recording artifacts Phase 4 saw as well. The one genuinely new field is `daeParseErrors: 0`,
declared in `836632d`.

`cs2beamng budget`: 4,273 MiB resident, 1,292,894 triangles drawn inside the view distance.
Baseline captured as `docs/baselines/cs2_city_fine-report-after-phase5a.json`. **Naomi's in-game
look at the rebuilt level was accepted 2026-08-28**, which closes the Rule-6 gate for this block:
the settings package, the server package, the levelcheck registry and the seven-way shapes split
are all validated in a running game, not only against a report.
