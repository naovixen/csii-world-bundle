# Consumer hand-off — the consolidated plan (living document)

**This file is the single authority for the consumer hand-off effort.** It replaces the approved
plan file and every dated handoff listed under [Appendix A](#appendix-a--what-this-replaced).
Where it disagrees with any of them, this file wins. The dated files stay on disk as the record
of *how* each conclusion was reached; each now carries a SUPERSEDED banner pointing here.

These four remain authoritative in their own domains and are **not** replaced:

| document | authority over |
|---|---|
| `docs/bundle-schema.md` | the bundle contract (schema 2). The normative text. |
| `PLAN.md` | the producer build (its Phases 0–8, all accepted). Its §4 sketch predates the freeze. |
| `docs/cs2-api-audit.md` | how the producer compares to the decompiled game, §1–§44. |
| `docs/game-fidelity-audit.md` | the 2026-08-09 1:1 coverage pass and its P/D/N work-list. |

---

## Read this first: the two "Phase 5"s

`PLAN.md` numbers the **producer build** (Phase 5 = Networks, accepted 2026-08-05). This file
numbers the **consumer hand-off effort** (Phase 5 = extract `tools/cs2bundle/`, not started).
They are unrelated sequences that happen to share integers, and the collision has already caused
one misread. **In this file every phase is written `H<n>`.** "H5" is always the extraction step;
a bare "Phase 5" in an older document means whichever sequence that document was numbering.

The H-numbers are the 2026-08-10 renumbering, preserved digit-for-digit so every existing
reference in `docs/`, `MEMORY.md` and the git log still resolves. The plan file's body used an
*earlier* numbering; the map is in [Appendix B](#appendix-b--phase-number-map).

---

## Status — 2026-08-12

**Closed: H1, H2, H3, H4.** All four were re-examined this session against the current tree and
the current bundle rather than against their own status labels; the verdict and its evidence are
in [Part 1](#part-1--closed-phases-h1h4-re-verified-2026-08-12). Three status claims were stale
and are corrected in `docs/game-fidelity-audit.md`.

**Next: H5** — extract `tools/cs2bundle/`. Nothing blocks it.

### The bundle on disk

Full rip **2026-08-12 15:13**, 1,666 s, all 8 stages, **zero** `FAILED`, **zero** `DROPPED:`.
`git log --since='2026-08-12 15:13' -- Systems IO Domain` lists nothing, so no producer commit
post-dates it: **safe to measure against, all layers.** Re-run that command before trusting this
paragraph — it is true as written and goes false the moment the producer changes.

`schema` 2 · **23** layers · 1,283,514 instances · 5,728 meshes · 4,714 materials · 3,589
textures · 8,259 network segments (7,289 mesh-form + 970 centerline-only) · 680,629 lanes ·
119,302 lane-geometry records · 27,156 areas · 49,730 clip volumes · 210 declared warnings.

### Gates, run 2026-08-12

| gate | result |
|---|---|
| `dotnet build -c Release` | **compiles clean** (0 warnings, 0 errors, `bin/Release/net48/Carto.dll` produced). The deploy copy fails `MSB3231` while CS2 holds the DLL — close the game before a build that must ship. |
| `tools/test_bundle_contract.py` | 63/63 OK |
| `tools/validate_bundle.py docs/example-bundle` | VALID, 0 warnings |
| `tools/test_png_decode.py` | PASS (16-bit gray, 8-bit RGBA) |
| `tools/test_decal_rows.py` | ok — 83,110 spans, 2 buried, worst overshoot 6.4 mm |
| `tools/test_splat_extras.py` | OK |
| `tools/validate_bundle.py` on the full city | VALID, **120 warnings** (recorded from the 15:13 run; ~10 min, not re-run today) |
| `tools/test_node_fan.py`, `tools/test_clip_cut.py` | green on the 15:13 rip (session-3 record) |

---

## Part 1 — closed phases H1–H4, re-verified 2026-08-12

### H1 — full game-fidelity audit → `docs/game-fidelity-audit.md`

**Holds.** The classification is sound and no later session has falsified a *verdict* in it. What
was stale is the status text around it, corrected this session:

1. **§2.1's "None of it is validated ... awaiting re-rip" was true when written and is now
   false.** Every P-item is validated — see H3 below.
2. **§4's "Grass: scatter from splatmap R is game-faithful" repeats the pre-2026-08-10 channel
   map.** `R` is dirt; grass is `G`. D3 was paid in the contract but this bullet still carried the
   old channel, in the very list that H11's consumer guide is told to feed from.
3. **Its counts are the 2026-08-09 12:39 rip's** (1,266,276 instances, 5,589 meshes, 22 layers,
   1,899 light entries). Four rips later they read as in Status above. Now dated in place.

One structural caveat worth carrying forward: the §1a table's "Captured" verdicts were measured
before **§42.6** established that `MeshVertex` and friends are a *render cache*, so a piece's
geometry was resident-or-not per session. Those verdicts were therefore true of that rip, not
necessarily of every rip. `0eeb24f` made the read deterministic (`RenderPrefab.geometryAsset`
first), and the 15:13 rip reports zero `DROPPED:` — so the table's claims are now true by
construction rather than by luck. This is why "two rips before any 'layer X is missing' verdict"
became a standing rule.

### H2 — repo & label hygiene

**Holds, re-swept this session.**

- **English-only comments:** all 94 fork-added files re-scanned for CJK — **zero** hits in any
  added `.cs` or `.py`. The remaining CJK is upstream Carto's own bilingual style (65 files:
  `Domain/`, `Utils/`, `Geodata/`, upstream `IO/` and `Systems/`, the `Locale/` payloads), which
  is where the convention says it belongs.
- **Version labels:** the five schema `$id`s are de-versioned; `validate_bundle.py` and
  `make_example_bundle.py` docstrings say "schema 2, frozen 2026-08-09"; `bundle.schema.json`
  pins `"const": 2`; README's ripper section describes the shipped one-button rip.
- **Repo state:** working tree clean, no tracked `__pycache__`, no stale-file candidates left
  from the original sweep.
- **New debt found, and it is documentation:** ten plan files, five of them superseded, plus an
  out-of-repo plan file carrying two conflicting numberings and a stale "Where we are". That is
  what this consolidation pays off (Appendix A).

### H3 — audit gap-close (P1–P16) — **now validated, not merely built**

Built in `095322a`, `b549bbe`, `4d6959d` and labelled "built, awaiting re-rip". Four rips have
happened since. Measured against the 15:13 bundle this session:

| item | assertion | measured |
|---|---|---|
| P1 | mesh cap default 0 (All), capped prefabs declared | `RipMeshLimit = 0`; **5,728** meshes shipped |
| P3 | clip volumes are closed prisms — a `top` field | **49,730 / 49,730** carry `top` |
| P4 | water appearance params | `rasters.water` carries **24** keys incl. `waterColor`, `scatteringColor`, `absorptionDistance`, `smoothnessClose/Distant`, `seaLevel` |
| P5 | terrain tiling/blend params | `rasters.terrainTiling` with per-layer `tiling`/`metresPerRepeat`/`lodBlend` for dirt, grass, rock + the four `extras` blocks |
| P6 | authored normals **and tangents** through the network bend | **200/200** sampled `networks/*.glb` carry `NORMAL` + `TANGENT` |
| P7 | dead/stump tree variant meshes | **32** `__dead`, **24** `__stump` files |
| P8 | lights photometry read off the `Effect` buffer | **654** placed emitters carry `range`, `temperature` *and* `position` (2,532 entries total; the rest are procedural emissive, which has no photometry) |
| P9 | stockpile fullness | `attrs.fullness` on 1,351 of a 200k sample; 28 fullness-variant meshes |
| P10 | road wear on lane geometry | **11,310 / 50,000** sampled lane-geometry records carry `wear` |
| P11 | area material completeness | `areaprefabs.json` carries `normalMap`, `maskMap`, `metallic`, `smoothness`, `edgeFadeRange`, `edgeNoise`, `rendererPriority` — 21 fields over 40 prefabs |
| P12 | §4.2 attrs produced | `theme`, `level`, `zone`, `zoning`, `brand`, `address`, `state`, `elevationFlags`, `fullness`, `colorSet` all present; `name` only where custom-named. `height`/`storey`/`category`/`season` are documented **derive-instead** in §4.2, not silently missing |
| **decalRole** | marking/wear split (predicted 38/5) | `laneprefabs.json`: **38 marking / 5 wear** — the prediction to the digit. The harness reads the key and the assetId substring heuristic is deleted, replaced by a `CONTRACT VIOLATION` check |
| 2d | extra-layer tiling params | `terrainTiling.extras` ships `dirtOverride`/`splatRange`/`blurDepth`/`heightOffset` per extra |

Not directly observable in a bundle, and honestly labelled as such: **P2** (writer-task fault
detection), **P13** (churn/per-entity warning persistence — the paths exist; this rip produced no
network-stage or churn warnings, so they are unexercised rather than proven), **P14**
(orchestrator/manifest correctness), **P15** (clip-band gates — they change band *values*, not a
labelled field), **P16** (perf), **4b** (elevated conform gate — built at
`NetworkRipSystem.GameMesh.cs:559`, dormant in this city by construction). The indirect evidence
is four consecutive full rips at 8/8 stages with zero `FAILED`.

**P16-REG is closed and struck. Do not re-open it.**

### H4 — SPLAT-1 and the terrain colour chain

**Closed**, with one residual carried forward. The channel map is `R`=dirt, `G`=grass, `B`/`A`
each a **bit-packed nibble pair** (`extra1:extra2`, `extra3:extra4`, decode `(ch & 15)` and
`(ch >> 4)`, normalise by **15**), composited **over `base` in channel order** with
`baseWeight = max(0, 1 − min(1, R/255·splatMultiplier))`. The importer reproduces shipped albedo
1:1; `_GROUND_GAIN`, `_GROUND_SATURATION` and `_GROUND_BRIGHTNESS` are deleted — **do not re-add
gain, saturation or tint knobs.** The colour derivation lives once in `tools/colour_model.py`,
grading in **ACEScg** on an OCIO view.

Still open: **SPLAT-4**, a hue error of +6.9° (sand) and +6.2° (pavement) against +1.2° on grass.
Every cheap explanation is dead by measurement — the illumination is white
(`lighting.sun.color = (1,1,1)`, `useColorTemperature` false, achromatic vignette), ACES 1.3 is
already the best of Blender's view transforms, and the fitted per-channel gain was refused as
three measurements with two free parameters. It is a warm-surface-specific hue shift, not a
brightness or saturation defect, and it does not block anything.

---

## Part 2 — the remaining phases

### H5–H9 — extract `tools/cs2bundle/` (NEXT)

Move the engine-neutral reconstruction logic out of `tools/blender_import_bundle.py` (**6,269
lines**) into a bpy-free package that both Blender and a future BeamNG builder import.

**Package layout** (unchanged from the approved plan except where noted):

```
tools/cs2bundle/
  __init__.py   SCHEMA_VERSION = 2, re-exports, docstring = API map
  reader.py     Bundle: manifest, layer path resolution, JSONL iteration, catalogue loaders
  frames.py     instance_matrix, LH-Y-up conventions + RH-Z-up conversion helpers
  glb.py        GLB array parsing, node flattening, clean_mesh_arrays() degenerate-face policy
  png.py        _unfilter / _decode_png_*
  splat.py      channel map, packed-nibble decode, ordered composite-over-base
  colour.py     NEW — tools/colour_model.py (849 lines) less blender_config_path()
  bezier.py     _bezier_point_vec, _cut_bezier, _arc_table_vec, _curve_length
  lanes.py      _lane_tile_count (GetTileCount port), clip windows, _bend_lane_part, _hang_drop_vec
  decals.py     _decal_* quad/rows/stations/lattice, _DECAL_* constants
  drape.py      ProjectionSurface protocol + batching/miss/lift; HeightfieldSurface (numpy)
  terrain.py    _fill_clip_quad/_clip_grid, _snap_hole_boundary, lattice helpers, face generation
  areas.py      _conform_area_triangle + the pure conform loop
  networks.py   ribbon/mitre ring math
  tints.py      tint rule as data + CPU reference apply
```

**Extraction order — one commit per step, A/B render gate each:**

- **H5 — pure leaves:** `reader`, `frames`, `glb`, `png`, `splat`, `colour`. Same commit: repoint
  `test_png_decode.py` to real imports (delete the regex-exec).
- **H6 — lane block:** `bezier`, `lanes`, `decals`. Same commit: rewrite `test_decal_rows.py` to
  import `cs2bundle` directly.
- **H7 — terrain/clip/areas pure parts:** `terrain`, `areas`.
- **H8 — drape protocol:** algorithmic body to `cs2bundle.drape`; the harness keeps a ~15-line BVH
  adapter. Add `HeightfieldSurface`; only swap it into the harness's area drape if A/B stays
  identical, else ship it consumer-facing only.
- **H9 — coupled geometry (riskiest, may be 3 commits):** network ring math, terrain/water face
  generation, the area driver loop. What remains in `blender_import_bundle.py` should read as a
  thin bpy adapter over `cs2bundle`.

**Amendments accumulated since the plan was approved — build these, not the superseded rules:**

- `colour.py` is a **new module and the cleanest single extraction**. It is bpy-free at import
  time; the one `import bpy` sits inside `blender_config_path()` (a Blender *locator*, not colour
  maths). Leave that function in the harness or take it as a consumer-supplied hook.
- `splat.py` implements the **packed-nibble decode and composite-over-base** rule (H4 above), not
  the partition rule the plan file described.
- `terrain.py` implements the **clip-cut containment invariant**: only a **deep** band may carve;
  a shallow band flattens and never deletes. The hole-rim snap moves **sideways, never upward**.
- `decals.py` reads **`decalRole`** — the substring heuristic is already gone, do not resurrect it.
- The marking-lift-is-a-kerb hypothesis is **falsified and reverted**; segment meshes are
  single-layer shells. Run `tools/test_drape_pick.py` before re-deriving anything in that area.

**Hard rules, every step:**

1. `cs2bundle` never imports `bpy` at module scope — `python -c "import cs2bundle"` under system
   Python is part of every gate.
2. The Blender script stays green at every step.
3. **A/B render must be pixel-identical.** A pure refactor that moves pixels is a bug; investigate
   the diff, never widen the tolerance.
4. `test_decal_rows.py` / `test_png_decode.py` regex-extract functions from the harness and break
   the instant those functions move — repoint them **in the same commit** as the move.
5. Full gate set now: `ab_render`/`ab_compare`, `test_decal_rows`, `test_png_decode`,
   `test_splat_extras`, `test_drape_pick`, `test_clip_cut` (six windows), `test_node_fan`,
   `test_bundle_contract`, plus the bpy-free import check.
6. Dependencies: package = stdlib + numpy; `validate_bundle.py` stays **stdlib-only**.
7. Import pattern for harness and tests:
   `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` then `import cs2bundle` —
   Blender's `--python` does not add `tools/` to `sys.path`.

Tag every constant **CONTRACT** (lift-ladder order, tile-count −0.0001 epsilon, N = L/max(1,size.z),
zero-instance decal rule, quaternion `[x,y,z,w]`, `diag(-1,1,1)` un-mirror) or **TUNED** (lift
magnitudes, tolerances; `_DECAL_DIP_FRACTION` and `_AREA_CONFORM_TOLERANCE` must stay fractions of
their lifts). Module docstrings cite the `bundle-schema.md` § they implement.

### H10 — schema completion (2 commits)

**H10a — the missing schemas + validator.** Ten documented layers still have no JSON Schema:
`lanes.jsonl`, `laneprefabs.json`, `colors.json`, `lights.json`, `areas.jsonl`,
`areaprefabs.json`, `clipvolumes.jsonl`, `warnings.jsonl`, `terrain/surfaces/index.json`, and
**`grading.json`** — the tenth, added because GRADE-1 made grading a declared layer on
2026-08-12. Draft 2020-12, de-versioned `$id`s. `laneprefabs` includes `decal`/`decalRole`;
`clipvolumes` includes `top`. Written from the normative prose, then shape-checked against the
shipped bundle (legitimate: shape, not producer behaviour). `validate_bundle.py` gains structural
validation of all ten plus the cross-file joins (lanes→laneprefabs, part assetId→laneMeshes/,
areas→areaprefabs, `owner`→instances, colorSet/lights refs, surfaces index→files, warnings
well-formed) — staying stdlib-only.

**H10b — example bundle + tests.** `make_example_bundle.py` gains a mesh-form `networks/` record,
`lanes.jsonl` + `laneprefabs.json` with one marking-role and one wear-role decal part,
`colors.json`, `lights.json`, a tiny splatmap, `warnings.jsonl`. Regenerate `docs/example-bundle/`.
`test_bundle_contract.py` gains negative cases per new schema in its existing style.

**Also decide here — PILLARS-2:** the 120 remaining validator warnings are *every one* a gap the
producer already declared in `warnings.jsonl`. Should a declared gap still warn? It is a decision,
not an investigation.

Gate: validator VALID on the regenerated example bundle **and** on the shipped bundle; contract
tests green.

### H11 — consumer docs (commit)

- **`docs/consumer-guide.md`** (engine-neutral): layer tour; pipeline order and its two hard
  dependencies (lanes need the built road surface as a drape target; areas need built terrain +
  cell size); ranked must-implement tiers, each mapped to a `cs2bundle` module and a spec §; the
  drape protocol (what you supply vs what `HeightfieldSurface` gives free); the CONTRACT-vs-TUNED
  table; consumer duties the harness skips (§4.1 skirt `v=0` UV ring drop); "no LOD story —
  generate your own"; a worked numeric example tracing one instance from the example bundle; an
  appendix converting LH Y-up → RH Z-up with the worked example redone.
- **`docs/consumer-caveats.md`** — the read-before-writing-a-reader list: lift-ladder order,
  tile-count epsilon, zero-instance stubs, `decalRole`, junction interiors are decal parts, snow
  channel ignored, degenerate-face policy, 16-bit big-endian PNGs, clip-volume open-interval
  semantics, **splat is an opacity composite with packed nibbles, not a partition**, the total
  `owner` join (a lot's surfaces belong under its building), stack expansions are placed only via
  `__stack_first/mid/last`, `warnings.jsonl`. Cross-link the spec; do not duplicate normative text.
- **`tools/README.md`** — per-script run lines and purpose, `cs2bundle` overview and import
  pattern, dependency policy.
- **`docs/bundle-changelog.md`** — schema 1→2 history from the §12.1 amendments plus this
  effort's additive changes (`decalRole`, `top`, `terrainTiling`, water params, `grading` layer).
- README final links.

Facts to fold in, all settled and none of them re-derivable from the code alone: D1–D6 as paid,
the three signed-off non-goals (VFX, road-name labels, city-boundary fence), the corrected grass
channel, the colour chain (`grading.json` + ACEScg + OCIO view), `seaLevel` is a **sea datum and
not a water surface**, and `layers.worldWater` is reserved but has never been emitted.

### H12 — C# refactor of the ripper additions (behaviour-preserving)

Scope: fork-added C# — `Systems/{Mesh,Material,Instance,Network,Lane,Terrain,Appearance}RipSystem`,
`RipOrchestratorSystem`, the ripper parts of `IO/` and `Domain/`. Single-responsibility splits,
cohesive units into their own files, consistent naming, long methods and magic numbers dealt with,
Carto's ECS/job patterns kept intact. **No output-shape change.**

Per the staleness rule this **cannot** be validated against the bundle on disk: the gate is
`dotnet build -c Release` green plus a `/code-review` pass on the diff, and the proof rides H14.
Placed before H13 so the producer changes land on clean code.

### H13 — producer polish (build-only; "built, awaiting re-rip")

Everything the old Phase 13 carried is already paid — **GRADE-1** (`8d33983`), **WATER-2**
(`946c9c6`), **P16-REG** (closed and struck). What is actually left:

1. **PROBE-2** — drop the `COLLAPSED:` probe's `Size.y` clause. `NetPieceData.m_HeightRange` is an
   *authored* band defaulting to `Bounds1(0f, 3f)`, so every flat surface piece whose author left
   the default is flagged: 9 rows per rip, all noise, none of them tripping the two real clauses
   (`pinnedAcross`, `max(extent.x, extent.z) < 0.05`). Two-line change. **Needs a yes** — it
   weakens a diagnostic, and nothing depends on it.
2. **Rip-completion advisory.** The end-of-rip dialog already names the output path, per-stage
   times and summaries. What the original UX scope asked for and is still missing: an explicit
   "**N prefabs from asset packs that are not enabled — enable them and re-rip for full
   fidelity**" line, derived from the `unresolved` flag the area catalogue already carries and the
   89 `managed prefab did not resolve` warnings, naming the packs where derivable.
3. Anything H10/H11 turns up that is genuinely producer-side.

Gate: `dotnet build -c Release` green; validator and tests on the **example** bundle only.

### H14 — final re-rip (USER ACTION) + acceptance (commit)

The user rips in-game; then: validator on the fresh bundle against **all fifteen** schemas; every
H12/H13 change checked against real output; full Blender import for visual acceptance (A/B against
pre-rip renders is **not** expected to match — the producer changed; the gate is validator + eyes
+ log counts); measured-fact drift fixed in the docs; changelog touch-up.

Two things to look at while the city is loaded, both cheap and neither worth a rip of its own:

- **Item 4a — elevated deck gaps.** Recorded 2026-08-11 as "suspects dead, cause not found", with
  the producer exonerated because imported meshes matched their `.glb` triangle-for-triangle. That
  measurement is equally consistent with the `.glb` itself missing a piece — which is exactly what
  §42.6 later proved was happening intermittently, and fixed. **Most likely already closed by
  `0eeb24f`.** Confirm with one look at an elevated interchange; do not re-investigate from
  scratch.
- **Painted beaches** — the four painted grounds have shipped since 2026-08-10 but nobody has
  confirmed in Blender that a painted beach now reads as sand.

---

## Part 3 — the open-items table

Everything genuinely open, in one place. Nothing else is outstanding.

| # | item | kind | where it is paid |
|---|---|---|---|
| PROBE-2 | Drop the `COLLAPSED:` probe's `Size.y` clause — 9 false positives per rip, no defect it can uniquely catch. | needs a yes | H13 |
| PILLARS-2 | Should the 120 warnings — every one a gap the producer already declared — still warn? | decision | H10 |
| SPLAT-4 | Warm-surface hue residual (+6.9° sand, +6.2° pavement, +1.2° grass). Light, tonemapper choice and working space are all eliminated. | consumer, open | unscheduled — blocks nothing |
| 4a | Elevated deck gaps: probably closed by the `0eeb24f` residency fix, never confirmed. | verify | H14 |
| beaches | Painted sand unconfirmed by eye since the extras fix. | verify | H14 |
| §29 | Backdrop water simulation. Explicitly **not** part of the D2 sign-off. | deferred | — |

---

## Part 4 — standing rules

The ones that have each been paid for at least once. The full method notes live in the superseded
handoffs (Appendix A) and in `docs/cs2-api-audit.md`.

**On the bundle**

- **The bundle goes stale the moment producer code changes.** Only the user can refresh it. Check
  `git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' --since='<bundle mtime>' -- Systems IO Domain`
  every time, and remember the check gives **false positives** — the mod deploys on `dotnet build`,
  not on commit — so confirm against something the rip itself emitted before calling a layer stale.
- **Never call a producer change validated because a harness ran clean.** It ran on old bytes.
  Say "built, awaiting re-rip".
- **Mixed-vintage bundles lie.** A partial rip leaves the untouched stages' files at their old
  vintage. State which layer came from which rip, or do not draw the conclusion.
- **The rip log answers perf questions for free**, and there is one log per rip — it is rewritten
  each session. Capture what you need before the next rip starts.

**On evidence**

- **A doc's claim about the game is a hypothesis with a citation.** Three of our own audit claims
  died to a thirty-second re-check in a single session. The ones that age worst were written
  before a later finding moved the ground under them.
- **A "feeds only X" claim is a claim about every reader** and takes a sweep of every reader:
  `decomp all Game <dir>` then `rg <symbol>`. Use `tools/decomp` (real C#), never IL-guessing.
- **Drive the real pipeline; don't reason about it.** Blender lives at
  `G:\SteamLibrary\steamapps\common\Blender\blender.exe` and renders headless — look at the PNG
  before theorising about a visual defect.
- **Probe every new gate by reverting the fix and checking that it fails.** A test whose
  exemptions are derived from the rule under test is vacuous.
- **Two rips before any "layer X is missing" verdict** — or read the `DROPPED:` line, which now
  answers it in one.

**On process**

- Commit per passing step, show the diff, gate each phase on its acceptance test.
- Decide at a best-practice fork and build; flag contract changes in the summary rather than
  blocking on them.
- No legacy fallbacks during development: when the format changes, delete the old reader.
  Missing new-format data is an error, not a fallback trigger.
- Don't round-trip UTF-8 files through PowerShell `Get-Content`/`Set-Content` — it double-encodes
  em-dashes and adds a BOM.

---

## Appendix A — what this replaced

| file | status |
|---|---|
| `C:\Users\naomi\.claude\plans\encapsulated-questing-bunny.md` | the approved plan. Its scope and rationale stand; its "Where we are" block and both numberings are superseded here. |
| `docs/plans/2026-08-11-next-session-handoff.md` | superseded — its open table is fully closed. |
| `docs/plans/2026-08-12-next-session-handoff.md` | superseded — FLARE/ALPHA/DEADPASS/PROBE all closed. |
| `docs/plans/2026-08-12-session-2-handoff.md` | superseded. |
| `docs/plans/2026-08-12-session-3-handoff.md` | superseded — its findings are folded into Parts 1–3. |
| `docs/plans/2026-08-10-terrain-colour-handoff.md` | superseded by H4. |
| `docs/plans/2026-08-11-post-audit-visual-regressions.md` | worked and closed except item 4a (→ H14). |
| `docs/plans/2026-08-08-phase-b-build-order.md`, `2026-08-08-bundle-representation-audit.md`, `2026-08-07-import-performance-refactor.md`, `2026-08-09-phase-8-scale-and-ux.md` | historical records of completed work; not superseded, not live. |

All of them stay on disk. Each live-plan file above now opens with a banner pointing here.

## Appendix B — phase-number map

| this file | 2026-08-10 renumbering | plan file's body | what it is |
|---|---|---|---|
| H1 | 1 | Phase 1 | game-fidelity audit — **done** |
| H2 | 2 | Phase 2 | repo & label hygiene — **done** |
| H3 | 3 | — (added post-audit) | P1–P16 gap-close — **done, validated** |
| H4 | 4 | — (added 2026-08-10) | SPLAT-1 channel semantics — **done** |
| H5–H9 | 5–9 | Phases 3–7 | extract `tools/cs2bundle/` — **next** |
| H10 | 10 | Phase 8 | schema completion |
| H11 | 11 | Phase 9 | consumer docs |
| H12 | 12 | Phase 10 | C# refactor |
| H13 | 13 | Phases 11 + 12 | producer polish |
| H14 | 14 | Phase 13 | final re-rip + acceptance |

`PLAN.md`'s Phases 0–8 are a **different sequence** — the producer build, all accepted.
