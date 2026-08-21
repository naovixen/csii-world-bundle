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
| 9 | **Consistent structure** | One return-shape convention, one docstring style, one field convention, one JSON idiom, one test style. | `ruff` + review | 5 return shapes and no `@dataclass`; 4 docstring styles; 3 C# field conventions inside one `partial class`; 4 JSON idioms; 5 `check()` signatures, two with reversed argument order |
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

**Principles served:** 7 naming, 8 self-explanatory code, 9 consistent structure.

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

**The standing per-phase gate.** Every phase, without exception, answers all ten before it lands.
The first four are mechanical; the rest are a review against the phase's diff, using the baseline
lists in the principles table as the closing checklist.

1. `ruff` clean and `ruff format` idempotent over every fork file — not only the ones touched.
2. `check_limits.py` clean; the phase's allowlist entries **removed, never added**.
3. `pytest` green across all 31 test files; the `Carto.Tests` project green.
4. The import-graph check clean: no `csiibundle` → consumer reference, no stage → stage private,
   no cycle.
5. **DRY** — did this phase leave any rule implemented twice? Every item it claimed from the
   principle-1 baseline is gone, not merely relocated.
6. **YAGNI** — nothing was added "for later"; every symbol the phase introduces has a caller.
7. **SRP / separation** — each new file states one responsibility in one sentence. If that
   sentence needs an "and", the split is wrong.
8. **Naming** — every new or renamed symbol matches the `docs/coding-standards.md` glossary; no
   concept gained a second name.
9. **Comments** — comment density did not rise; every derivation the phase moved is intact and
   every declared approximation is still declared and still counted.
10. **Performance** — the phase's stage timings are re-measured on the Phase-0 instrument and are
    within noise or better. A regression blocks the phase.

Anything that cannot be satisfied is recorded as a declared, counted exception in the phase's
commit message — never left silent.

**Consumer phases.** Rebuild `cs2_city_fine` in place with `cook: true`, settings read from its own
`cs2beamng_report.json`, BeamNG closed. Diff the new report against the Phase-0 golden with
`ab_compare.py --counts`; `validate_level` at 0 problems; `cs2beamng budget` within noise of
baseline. Then hand it over for the in-game look **before** the commit.

**Producer phases.** Build only (`dotnet build -c Release`), `Carto.Tests` green, and the wording
stays "built, awaiting in-game re-rip". After Phase 11, one request for a full rip; diff its
digest against the Phase-0 golden manifest with `bundle_manifest.py` — per-layer record counts and
per-file hashes must match except where a phase declared a change. Read the rip report's warnings
and per-stage counts against the golden report.

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
  the commit, per the standing workflow.

---

## Progress

Each phase records here what landed and what it found, so this document stays the single record.

| Phase | Status |
| --- | --- |
| 0 — Guardrails and baselines | not started |
| 1 — The three defects | not started |
| 2 — One implementation of every shared rule | not started |
| 3 — One stage framework | not started |
| 4 — Split `csiibundle` and `tools/` | not started |
| 5 — The app and its UI | not started |
| 6 — The Blender adapter | not started |
| 7 — One rip-stage framework | not started |
| 8 — The IO writer layer | not started |
| 9 — Split the rip systems | not started |
| 10 — Performance | not started |
| 11 — Comment, naming and structure | not started |
| 12 — Documentation | not started |
