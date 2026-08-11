# Next-session handoff — consumer hand-off effort (written 2026-08-11)

## Start here

Read `C:\Users\naomi\.claude\plans\encapsulated-questing-bunny.md` (the approved plan, with a
"Where we are" block at the top), then this file for what changed since. The phase list below
supersedes the plan file's numbering where they disagree.

**One-line status:** Phases 1–4 are done and validated by a real rip; the effort is now at
**Phase 5 — extract `tools/cs2bundle/`**, which is the first of the remaining consumer-facing
phases.

## State of the world, verified 2026-08-11

- **Bundle on disk is fresh and trustworthy.** `…\ModsData\Carto\bundle.json` mtime
  **2026-08-11 00:17**. Two producer commits carry later timestamps (`3292288` 01:08,
  `c21145d` 01:15) — that is the documented false positive: the mod deploys on
  `dotnet build`, not on commit. Confirmed against what the rip itself emitted:
  `bundle.json` carries `channels.R = "dirt"`, the packed-nibble `B`/`A` entries and the
  `blend` string, and `terrain/surfaces/sources.json` shows
  `extra4 <- Sand_temperate_01_Basecolor`. The rip **does** reflect HEAD's producer.
- **Validator: VALID, 0 errors, 133 warnings** on the full city. All 133 are the known
  bridge-pillar class ("mesh is not referenced by any instance") — pillars are referenced by
  networks, not instances. Phase 10 item.
- Full-city counts: 1,283,514 instances · 5,728 meshes · 4,705 materials · 3,582 textures ·
  8,259 network segments · 680,629 lanes · 119,302 lane-geometry records · 27,156 areas ·
  210 warnings.
- `dotnet build -c Release` 0/0; `test_bundle_contract.py` 58/58; `validate_bundle.py` on
  `docs/example-bundle` VALID; `test_decal_rows.py` and `test_png_decode.py` pass.

## What was settled (do not re-derive)

The terrain-colour investigation is **closed**. Memory files
`splat-channels-r-is-dirt.md`, `cs2-colour-grading-layer.md`,
`colour-chain-lives-in-colour-model.md`, `blender-premultiplies-colour-by-alpha.md` hold the
detail. The load-bearing facts:

- `R = dirt`, `G = grass` (they were inverted). Never re-derive channel identity from slope —
  rock is R's own residual, so every soil weight falls with slope together.
- `B` and `A` each pack **two 4-bit weights**: `extra1:extra2` and `extra3:extra4`. Decode
  `(ch & 15)` / `(ch >> 4)` and **normalise by 15, not 255**. Before this, `extra3`/`extra4`
  shipped no texture at all — `extra4` is Sand, which is what the user paints beaches with.
- The blend is a **composite over `base` in channel order**, never a partition;
  `baseWeight = max(0, 1 − min(1, R/255·splatMultiplier))` — the residual of **R alone**.
- The importer reproduces shipped albedo **1:1**. `_GROUND_GAIN`/`_GROUND_SATURATION` are
  deleted — **do not re-add gain, saturation or tint knobs**. The game's real colour grading
  ships in `grading.json` and is applied consumer-side via `tools/colour_model.py` + an OCIO
  view.

## Open items, each with a phase

| # | item | severity | phase |
|---|---|---|---|
| GRADE-1 | **`grading.json` is written but never declared in `bundle.json`'s `layers`.** Verified: the file exists (6,767 bytes, keys `exposure`/`whiteBalance`/`colorAdjustments`/`shadowsMidtonesHighlights`/`tonemapping`/`vignette`/`lighting`) but `layers` has no grading key, and contract §1 says a consumer reads every path from `layers`. Add a `LayerDef` in `IO/BundleWriter.cs` + the schema's `layers` enum + a §-entry in `docs/bundle-schema.md`. | **HIGH — a consumer cannot find the layer** | 13 (producer) |
| P16-REG | Instance walk 80.5 s vs its 57.6 s baseline — the wave-2 semantic attrs cost more than the lookup hoist saved. Measure which reads dominate before changing anything. | MED | 13 |
| WATER-2 | `rasters.water.seaLevel` captured 15.0 against a measured 521 m water surface; the field drives backdrop water only, so its "world metres" semantics string is wrong. Fix the string or drop the key. | LOW | 13 |
| V1 | Do Lot-painted areas (storage yards, specialised industry) arrive as `Surface` sub-areas? Needs one in-game look. | LOW | 14 |
| PILLARS | 133 validator warnings: bridge pillars unreferenced by instances. Decide whether the validator should exempt network-owned pillars the way it exempts `__base`/`__dead`/`__stack_*`. | LOW | 10 |
| D1–D6 | Documentation debts from audit #40 §2.2 (tunnel-tag distinction, deliberate exclusions, grass wording, building-state consumer rules, §8c lane-reference tilt). | doc | 11 |
| N items | VFX / road-name labels / city-boundary fence / backdrop water as documented non-goals — **needs user sign-off**. | doc | 11 |

Also unvalidated-by-eye: the four painted grounds now ship, but nobody has confirmed in Blender
that painted beaches finally read as sand. Worth one look before Phase 5 builds on it.

## Phase order from here

**5–9 extract `tools/cs2bundle/`** → **10 schema completion** → **11 consumer docs** →
**12 C# refactor** → **13 producer polish (GRADE-1, P16-REG, WATER-2)** → **14 final re-rip**.

### Phase 5–9 — extract `tools/cs2bundle/` (next)

Goal: move the engine-neutral reconstruction logic out of `tools/blender_import_bundle.py`
(now ~5,600 lines) into a bpy-free package both Blender and a future BeamNG builder import.
Module layout, extraction order and per-step gates are in the plan file. Two amendments since
it was written:

- `tools/colour_model.py` (849 lines, already bpy-free) is a **new extraction candidate** and
  arguably the cleanest one — it is the whole colour derivation and belongs in `cs2bundle`.
- The splat module must implement the **packed-nibble decode and the composite-over-base
  blend**, not the superseded rules.

Hard rules: `cs2bundle` never imports `bpy` (assert with `python -c "import cs2bundle"` under
system Python every step); the Blender script stays green at every step; A/B render gate per
step — a pure refactor must be **pixel-identical**, so investigate any nonzero diff rather than
widening tolerance. `test_decal_rows.py` and `test_png_decode.py` regex-extract functions from
the harness and must be repointed in the **same commit** as the move that breaks them.

## Method notes worth keeping

- **Drive the real pipeline; don't reason about it.** The colour investigation cost three wrong
  diagnoses argued from static analysis. It closed only when the shader was executed headlessly
  (`blender --background`, emission tapped off each node) and measured. Blender 5.2 lives at
  `G:\SteamLibrary\steamapps\common\Blender\blender.exe`; probe scripts are in the session
  scratchpad.
- **Test channel identity against physical facts**, not curve fits: "grass cannot grow on a
  lakebed" settled what slope correlation never could.
- Two self-inflicted traps hit this week: Blender's factory startup ships a **cube at the
  origin** that occludes a test plane, and **PIL premultiplies RGBA by alpha on resize**, which
  silently collapsed the splatmap's paint channel. Both produced confident wrong readings.
- Don't round-trip UTF-8 files through PowerShell `Get-Content`/`Set-Content` — it
  double-encodes em-dashes and adds a BOM (it corrupted `MEMORY.md`; repaired).

## Suggested opening prompt for the next session

> Continue the consumer hand-off effort in `E:\csii-world-bundle`. Read
> `docs/plans/2026-08-11-next-session-handoff.md` first, then the approved plan at
> `C:\Users\naomi\.claude\plans\encapsulated-questing-bunny.md`. The bundle at
> `…\ModsData\Carto` is fresh (2026-08-11 00:17) and reflects HEAD's producer — validator VALID
> with 0 errors — so it is safe to measure against.
>
> Start **Phase 5: extract `tools/cs2bundle/`**, following the module layout and extraction
> order in the plan, plus `tools/colour_model.py` as an additional module. One step at a time,
> each gated by the A/B render (expect pixel-identical), the repointed harness tests, and a
> bpy-free import check; commit per passing step.
>
> Before starting, fix **GRADE-1**: `grading.json` is written but never declared in
> `bundle.json`'s `layers`, so a consumer following contract §1 cannot find it. That is a small
> producer change — build it, say "built, awaiting re-rip", and fold it into the Phase 14 rip.
