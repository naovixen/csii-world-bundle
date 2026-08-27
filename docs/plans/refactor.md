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
| 0 — Guardrails and baselines | **DONE** 2026-08-21 — see below |
| 1 — The three defects | **DONE** 2026-08-21 — one of the three was not live; see below |
| 2 — One implementation of every shared rule | **DONE** 2026-08-27 — five of its claims needed correcting; see below |
| 3 — One stage framework | not started |
| 4 — Split `csiibundle` and `tools/` | not started |
| 5 — The app and its UI | not started |
| 6 — The Blender adapter | not started |
| 7 — One rip-stage framework | **in progress** — extractions, the typed outcome, the frame loop and the report shape done; see below |
| 8 — The IO writer layer | not started |
| 9 — Split the rip systems | not started |
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

