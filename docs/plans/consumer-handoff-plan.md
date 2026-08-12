# Consumer hand-off — the plan

**Goal:** make the CS2 World Bundle consumable by someone who has never read this repo, so a
BeamNG level builder (or any other consumer) can be written against the bundle alone.

**Status 2026-08-12: phases H1–H4 are closed and nothing is open.** Next step is **H5**, the
first of five refactor steps that pull the reconstruction logic out of the Blender script into a
reusable package.

This file supersedes every dated handoff in this folder; they stay on disk as the record of how
each conclusion was reached. The contract itself lives in `docs/bundle-schema.md`, the
game-vs-bundle analysis in `docs/game-fidelity-audit.md` and `docs/cs2-api-audit.md`.

> **"Phase 5" means two different things.** `PLAN.md` numbers the *producer build* (its Phase 5 =
> Networks, finished 2026-08-05). This file numbers the *hand-off effort* and writes every phase
> as `H<n>`, so H5 is always the extraction step. Older documents use bare numbers; the mapping
> is at the bottom.

---

## 1. Where things stand

**The bundle on disk** is a full rip from **2026-08-12 15:13** — 1,666 s, 8 stages, zero
failures, zero dropped pieces. Schema 2, 23 layers.

1,283,514 instances · 5,728 meshes · 4,714 materials · 3,589 textures · 8,259 network segments
(7,289 with geometry, 970 centerline-only) · 680,629 lanes · 119,302 lane-geometry records ·
27,156 areas · 49,730 clip volumes.

**Before trusting it, re-run the staleness check.** Producer changes make it a description of
old code, and only a rip in-game refreshes it:

```
git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' \
        --since='<bundle.json mtime>' -- Systems IO Domain
```

As of this writing that lists **this session's producer commits**, so the bundle predates them:
it is still the right source for *what a real city contains*, and the wrong source for *did this
session's changes work*. They are verified at H14.

**Gates, all green:**

| gate | result |
|---|---|
| `dotnet build -c Release` | compiles clean, 0 warnings, 0 errors |
| `tools/test_bundle_contract.py` | 63/63 |
| `tools/validate_bundle.py docs/example-bundle` | VALID, 0 warnings |
| `tools/validate_bundle.py <full city>` | VALID, **1 warning** (was 133 — see PILLARS-2) |
| decal rows / png decode / splat extras / drape pick | pass |
| node fan / clip cut | pass (six windows) |

The deploy step fails `MSB3231` while CS2 is running — the compile still succeeds, but nothing
reaches the mods folder. Close the game before a build that must ship.

---

## 2. Next: H5 — extract `tools/cs2bundle/`

`tools/blender_import_bundle.py` is 6,269 lines, and roughly ten algorithms a consumer needs are
buried in it. H5–H9 move the engine-neutral half into a package that never imports `bpy`, so a
BeamNG builder can `import cs2bundle` and the Blender script becomes a thin adapter over it.

**Module layout**

```
tools/cs2bundle/
  reader.py    manifest, layer paths, JSONL iteration, catalogue loaders
  frames.py    instance_matrix, LH-Y-up conventions, RH-Z-up conversion
  glb.py       GLB parsing, node flattening, degenerate-face policy
  png.py       16-bit PNG decode
  splat.py     channel map, packed-nibble decode, composite-over-base
  colour.py    the colour derivation (tools/colour_model.py, less its Blender locator)
  bezier.py    curve evaluation, cutting, arc tables
  lanes.py     GetTileCount port, clip windows, lane bend, hang drop
  decals.py    decal quads/rows/stations/lattice
  drape.py     ProjectionSurface protocol + a numpy heightfield raycaster
  terrain.py   clip rasterisation, hole snap, lattice, face generation
  areas.py     area triangle conformance
  networks.py  ribbon/mitre ring math
  tints.py     colorSet x colorMask as data + a CPU reference apply
```

**Order — one commit per step, A/B render gate on each**

| step | moves | also in the same commit |
|---|---|---|
| **H5** | reader, frames, glb, png, splat, colour | repoint `test_png_decode.py` to real imports |
| H6 | bezier, lanes, decals | rewrite `test_decal_rows.py` to import the package |
| H7 | terrain, areas | |
| H8 | drape (harness keeps a ~15-line BVH adapter) | add `HeightfieldSurface` |
| H9 | networks, face generation, area driver loop | riskiest; may be 3 commits |

**Rules for every step**

1. `cs2bundle` never imports `bpy` at module scope — `python -c "import cs2bundle"` under system
   Python is part of the gate. (`colour_model.py` is already clean except `blender_config_path()`,
   which is a Blender *locator*, not colour maths — leave it in the harness.)
2. **The A/B render must be pixel-identical.** A pure refactor that moves pixels is a bug.
   Investigate the diff; never widen the tolerance.
3. `test_decal_rows.py` and `test_png_decode.py` regex-extract functions from the harness and
   break the moment those functions move. Repoint them in the *same* commit.
4. Dependencies: package = stdlib + numpy. `validate_bundle.py` stays stdlib-only.
5. Import pattern, because Blender's `--python` does not add `tools/` to `sys.path`:
   `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` then `import cs2bundle`.
6. Tag each constant `CONTRACT` (lift-ladder order, tile-count −0.0001 epsilon,
   N = L/max(1,size.z), zero-instance decal rule, quaternion `[x,y,z,w]`, `diag(-1,1,1)`) or
   `TUNED` (lift magnitudes, tolerances). Module docstrings cite the spec § they implement.

**Build the current rules, not the superseded ones.** Three have changed since the module list
was first written: `splat.py` decodes **packed nibbles** and composites over base (it is not a
partition); `terrain.py` obeys the containment invariant — only a **deep** band carves, a shallow
one flattens and never deletes, and the hole-rim snap moves sideways, never upward; `decals.py`
reads **`decalRole`** from the bundle (the assetId substring heuristic is gone, do not revive it).
The "marking lift is a kerb" theory is falsified and reverted — run `tools/test_drape_pick.py`
before re-deriving anything there.

---

## 3. What was wrong, and what was done

Everything below is closed. Producer changes are marked **⟳** — they compile and are correct
against the game's decompiled source, but no producer change can be *proven* without an in-game
rip, which happens at H14.

### H1 — does the bundle capture what the game draws?

An audit answered this on 2026-08-09 (`docs/game-fidelity-audit.md` §1–§4) and was re-run on
2026-08-12 (§5) after the producer changed by 5,252 lines.

| problem | what was done |
|---|---|
| 16 gaps found by the first audit (P1–P16): a default mesh cap that silently dropped 5,539 of 5,589 meshes, clip volumes missing their top cap, no water or terrain appearance params, no authored normals on roads, dead trees rendering healthy, broken light photometry, missing attrs | All built, and all **verified in the shipped rip** this session — e.g. 49,730/49,730 clip volumes carry `top`, 200/200 sampled network meshes carry `NORMAL`+`TANGENT`, 654 lights carry range/temperature/position, `decalRole` split 38/5 exactly as predicted |
| **Two of the five tree states were never exported.** The game draws a different mesh for Teen, Adult, Elderly, Dead and Stump; only three shipped. 119,237 Elderly and 33,581 Teen trees — 59.5% of stated trees — drew as adults, while `__stump` shipped for a state no tree here is in | ⟳ `__teen` and `__elderly` now export through the same mechanism (`MeshRipSystem`). Contract §6.1 and the validator updated. No `__child`: the game has no Child case — a child is a Teen below growth 128, scaled, which `growthScale` already carries |
| **`Fog` was never captured.** The game's own `Climate.*Properties` set names eight weather-driven volume components; `GradingWriter` read five. `Fog` carries an **HDR tint** and an attenuation distance, sitting between every shipped surface and the camera | ⟳ Captured as `grading.json` → `fog`. Also added `diagnostics.unread`, which names any volume component the scene overrode and the writer skips — so the *next* blind spot shows up in a rip instead of needing a decompiler |
| Terrain tiling ignored the game's fallback settings prefab for a layer with no texture | ⟳ Fixed: resolved from the public `NewMapSettingsData` singleton, per layer, exactly where the game branches |
| Every exclusion added since the last audit was unverified. Exclusions are the dangerous class — a rule matrix has no row for absence | All re-derived at the renderer end and counted: marker networks (91 taxiways keep their record and lose 27,648 triangles the game never draws), the 142,268-lane part exclusion (matches term for term), unpainted areas (266). The hidden-piece gate turned out to be redundant — the game pre-filters the buffer the producer reads — so its revert was right |
| Nothing said what a rip taken in the *map editor* omits | Documented (§4.3): the editor draws `RequireEditor` parts, the producer always excludes them, and a bundle is always the played world |

### H2 — repo hygiene

| problem | what was done |
|---|---|
| Four dead members in fork-added C#, one of them (`IsMarkerNetwork`) still asserting in its doc comment that taxiways "are genuinely drawn" — the exact claim the audit had falsified | 99 lines removed; build clean |
| Three docs pointed at `ModsData/Carto/<name>/`, a directory the code has never used — including the staleness rule's own example, in the file every session reads first | Fixed in `CLAUDE.md` (twice) and `PLAN.md`. `CLAUDE.md`'s layout section listed 3 rip systems and 4 writers; it now lists all 8 and all 14 |
| `Documents/Related APIs.md` kept being re-raised as a deletion candidate | Decided: **keep**. It is upstream Carto's own file, from before the fork point, documenting the parent mod's API surface. It never claimed to cover the ripper |
| Non-English comments, schema `$id` versions, docstring versions, README | Re-checked: all clean. Zero CJK in any of the 94 fork-added files |

### H3 — the audit gap-close

Built in `095322a`, `b549bbe`, `4d6959d`; measured against the 15:13 rip this session and all
present in real output. The items that cannot be seen in a bundle — writer-fault detection,
warning persistence, orchestrator correctness, the clip-band gates, perf — are evidenced only by
four consecutive full rips at 8/8 stages with zero failures, and are labelled that way rather
than counted as proven.

### H4 — terrain colour

| problem | what was done |
|---|---|
| The terrain rendered brown: `R` and `G` were swapped, so dirt blanketed the landmass and grass was painted on the lakebed | Channel map corrected — `R`=dirt, `G`=grass, and `B`/`A` each pack **two 4-bit weights** (`extra1:extra2`, `extra3:extra4`; decode `& 15` and `>> 4`, normalise by **15**). Before this, two paint channels shipped no texture at all |
| The terrain drew at a tenth of its albedo | Blender premultiplies an image's colour by its alpha, and CS2 packs *data* into alpha. Fixed with `CHANNEL_PACKED`. The importer now reproduces shipped albedo 1:1 — **do not re-add gain, saturation or tint knobs** |
| The on-screen palette did not match, and three explanations were wrong | The chain lives once in `tools/colour_model.py`, grading in ACEScg on an OCIO view. Warm-light and tonemapper-choice hypotheses were both refuted by measurement |
| A hue error remained: +6.9° sand, +6.2° pavement, +1.2° grass, with light, tonemapper and working space all eliminated (SPLAT-4) | The three reference shots were taken at **different camera distances**, and `Fog` — tinted, distance-dependent — was never in evidence because the bundle did not carry it. It does now (see H1). Re-test when the H14 rip lands the number |

### Decisions taken this session

| item | decision |
|---|---|
| **PROBE-2** — the `COLLAPSED:` probe's `Size.y` clause flagged 9 healthy pieces per rip | ⟳ **Dropped.** `m_Size.y` comes from `m_HeightRange`, an *authored* field defaulting to `(0, 3)` — a declared band, not the geometry's thickness — so every flat surface piece whose author left the default tripped it. The two clauses that test what a piece actually built remain |
| **PILLARS-2** — 119 of the full city's 120 validator warnings were assets the producer had *already declared* in `warnings.jsonl` | **Declared gaps no longer warn**; they are counted in one note naming the reasons. Warning again for something the bundle already declared made the count a function of the producer's honesty and buried every other class. An *undeclared* missing mesh is still an error |
| **§29 backdrop water** — deferred for months | **Permanent non-goal**, documented in §8. The backdrop is seeded to sea level and then simulated: the surface is the `seaLevel` datum the bundle already ships, the ground under it is `worldTerrain` when resident, and what is left is transient flow — the same class as waves, excluded for the same reason. A consumer draws a plane at `seaLevel` |
| **Item 4a** — elevated deck gaps, "cause not found" | **Closed.** Rendered an interchange from the shipped bundle: the deck is continuous across the whole span including the river crossing. The original measurements ("meshes match their .glb triangle-for-triangle") were equally consistent with the `.glb` itself missing a piece, which is what the residency fix `0eeb24f` repaired |
| **Painted beaches** — shipped since 2026-08-10, never looked at | **Confirmed.** Rendered the strongest painted-sand region: a broad sand band reads as sand between grass inland and water offshore |

---

## 4. The remaining phases

| phase | what it delivers |
|---|---|
| **H5–H9** | extract `tools/cs2bundle/` — see §2 |
| **H10** | **Schema completion.** Ten documented layers still have no JSON Schema: `lanes`, `laneprefabs`, `colors`, `lights`, `areas`, `areaprefabs`, `clipvolumes`, `warnings`, `terrain/surfaces/index`, and **`grading`** (now a declared layer, and it gained `fog`). Validator gains structural checks plus the cross-file joins, staying stdlib-only. Then extend `make_example_bundle.py` to exercise the layers it currently skips, and add negative cases per schema |
| **H11** | **Consumer docs.** `docs/consumer-guide.md` (layer tour, pipeline order and its two hard dependencies, must-implement tiers mapped to modules, the drape protocol, CONTRACT-vs-TUNED constants, a worked example tracing one instance, an LH-Y-up → RH-Z-up appendix); `docs/consumer-caveats.md`; `tools/README.md`; `docs/bundle-changelog.md` |
| **H12** | **C# refactor** of the ripper additions — behaviour-preserving, no output-shape change. Gate is a clean build plus `/code-review`; proof rides H14 |
| **H13** | **Producer polish.** One item left: the rip-completion dialog should say *"N prefabs come from asset packs that are not enabled — enable them and re-rip for full fidelity"*, derived from the `unresolved` flag and the 89 unresolved-prefab warnings. It already reports path, stage times and summaries |
| **H14** | **Final re-rip (user action) + acceptance.** Validate against all 15 schemas; check every ⟳ change against real output; full Blender import for visual acceptance (A/B against pre-rip renders is *not* expected to match — the producer changed); fix measured-fact drift in the docs |

**What H14 must check**, because it is the only place these can be proven:

- `__teen` / `__elderly` mesh counts and the tree-variant report line.
- `grading.json` carries `fog` (or omits it because fog is off), and what `diagnostics.unread`
  names.
- **SPLAT-4**: replay the hue residual with the fog tint applied. If it closes, the four-month
  colour investigation is finished; if it does not, the residual is not the atmosphere either.
- `COLLAPSED:` reports no rows, or real ones.
- Validator warning count (expect ~1, not 120).
- Terrain tiling still reports the same values (the fallback branch should not fire on this map).

---

## 5. Rules that have already cost something

- **The bundle goes stale the instant producer code changes**, and only the user can refresh it.
  The staleness check gives false positives — the mod deploys on `dotnet build`, not on commit —
  so confirm against something the rip itself emitted before calling a layer stale.
- **Never call a producer change validated because a harness ran clean.** It ran on old bytes.
- **A partial rip leaves untouched stages at their old vintage.** Say which layer came from which
  rip, or do not draw the conclusion.
- **A claim about the game is a hypothesis with a citation.** Several of this project's own audit
  claims died to a thirty-second re-check. "Feeds only X" is a claim about *every* reader and
  takes a sweep of every reader: `decomp all Game <dir>`, then ripgrep. Use `tools/decomp` — real
  C# — never IL-guessing.
- **Drive the real pipeline instead of reasoning about it.** Blender is at
  `G:\SteamLibrary\steamapps\common\Blender\blender.exe` and renders headless; aim the camera at
  the coordinates the import log prints, not at world coordinates.
- **Probe every new gate by reverting the fix and checking that it fails.** A test whose
  exemptions derive from the rule under test proves nothing.
- **The rip log answers perf questions for free**, and there is one log per rip — it is rewritten
  each session.
- Don't round-trip UTF-8 files through PowerShell `Get-Content`/`Set-Content`; it double-encodes
  and adds a BOM.

---

## Appendix — phase numbers in older documents

| here | 2026-08-10 renumbering | the original plan file | what it is |
|---|---|---|---|
| H1 | 1 | Phase 1 | fidelity audit |
| H2 | 2 | Phase 2 | repo hygiene |
| H3 | 3 | — | audit gap-close |
| H4 | 4 | — | terrain colour |
| H5–H9 | 5–9 | Phases 3–7 | extract `cs2bundle` |
| H10 | 10 | Phase 8 | schema completion |
| H11 | 11 | Phase 9 | consumer docs |
| H12 | 12 | Phase 10 | C# refactor |
| H13 | 13 | Phases 11 + 12 | producer polish |
| H14 | 14 | Phase 13 | final re-rip |

`PLAN.md`'s Phases 0–8 are a different sequence — the producer build, all finished. The approved
plan at `C:\Users\naomi\.claude\plans\encapsulated-questing-bunny.md` is still good for scope and
rationale; its status blocks are superseded by this file.
