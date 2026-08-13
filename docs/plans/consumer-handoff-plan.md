# Consumer hand-off — the plan

**Goal:** make the CS2 World Bundle consumable by someone who has never read this repo, so a
BeamNG level builder (or any other consumer) can be written against the bundle alone.

**Status 2026-08-12: H1–H9 are closed and nothing is open.** `tools/csiibundle/` exists: **17
modules**, no `bpy` anywhere in it, and `tools/blender_import_bundle.py` is down from 6,269 lines
to ~3,700 and is now an adapter over the package rather than the only copy of the
reconstruction. A completeness sweep after H9 found six layers of knowledge the harness held and
the package did not — see §2's closing note — and all six are now in it. Next is **H10**, schema
completion.

This file supersedes every dated handoff in this folder; they stay on disk as the record of how
each conclusion was reached. The contract itself lives in `docs/bundle-schema.md`, the
game-vs-bundle analysis in `docs/game-fidelity-audit.md` and `docs/cs2-api-audit.md`.

> **"Phase 5" means two different things.** `PLAN.md` numbers the *producer build* (its Phase 5 =
> Networks, finished 2026-08-05). This file numbers the *hand-off effort* and writes every phase
> as `H<n>`, so H5 is always the extraction step. Older documents use bare numbers; the mapping
> is at the bottom.

---

## 1. Where things stand

**The bundle on disk** is a full rip from **2026-08-12 18:10** — 8 stages, zero failures, zero
dropped pieces. Schema 2, 23 layers.

1,283,513 instances · **5,792 meshes** · 4,784 materials · 3,729 textures · 8,259 network segments
· 680,629 lanes · 119,302 lane-geometry records · 27,156 areas · 49,730 clip volumes.

**It validated P18, P17, P19 and PROBE-2** — `__teen` 32 + `__elderly` 32 (the mesh count's +64),
`grading.json` carrying `fog`, tiling unchanged because the fallback correctly did not fire, and
`COLLAPSED:` reporting nothing. **One producer change post-dates it**: the `splitToning` capture
and the valued `diagnostics.unread`, both added *because* of what this rip reported. Those two
ride the next rip; everything else in the bundle is current.

**Before trusting it, re-run the staleness check.** Producer changes make it a description of
old code, and only a rip in-game refreshes it:

```
git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' \
        --since='<bundle.json mtime>' -- Systems IO Domain
```

**Gates, all green:**

| gate | result |
|---|---|
| `dotnet build -c Release` | compiles clean, 0 warnings, 0 errors |
| `tools/test_bundle_contract.py` | 63/63 |
| `tools/validate_bundle.py docs/example-bundle` | VALID, 0 warnings |
| `tools/validate_bundle.py <full city>` | VALID, **1 warning** on the 18:10 rip (was 133 — see PILLARS-2). The one warning is the genuine parent advisory; the 119 declared gaps are a note |
| decal rows / png decode / splat extras / drape pick | pass |
| node fan / clip cut | pass (six windows) |

The deploy step fails `MSB3231` while CS2 is running — the compile still succeeds, but nothing
reaches the mods folder. Close the game before a build that must ship.

---

## 2. Done: H5–H9 — `tools/csiibundle/`

`tools/blender_import_bundle.py` was 6,269 lines, and roughly ten algorithms a consumer needs
were buried in it. H5–H9 move the engine-neutral half into a package that never imports `bpy`,
so a BeamNG builder can `import csiibundle` and the Blender script becomes a thin adapter over
it.

**Module layout**

```
tools/csiibundle/
  reader.py    manifest, layer paths, JSONL iteration, catalogue loaders    H5 ✅
  frames.py    instance_matrix, LH-Y-up conventions, RH-Z-up conversion     H5 ✅
  glb.py       GLB parsing, node flattening, degenerate-face policy         H5 ✅
  png.py       16-bit PNG decode                                            H5 ✅
  splat.py     channel map, packed-nibble decode, composite-over-base       H5 ✅
  colour.py    the colour derivation (tools/colour_model.py, less its Blender locator)  H5 ✅
  bezier.py    curve evaluation, cutting, arc tables   ✅
  lanes.py     GetTileCount port, clip windows, lane bend, hang drop   ✅
  decals.py    decal quads/rows/stations/lattice   ✅
  drape.py     ProjectionSurface protocol + a numpy heightfield raycaster   ✅
  terrain.py   clip rasterisation, hole snap, lattice, face generation   ✅
  areas.py     area triangle conformance   ✅
  networks.py  ribbon/mitre ring math   ✅
  tints.py     colorSet x colorMask as data + a CPU reference apply   ✅
  instances.py §4 placement: the __base skirt condition, the decal lift    (sweep) ✅
  lanegraph.py §5A lanes.jsonl — the lane GRAPH: paths, kinds, owners      (sweep) ✅
  lights.py    §4.3 photometry, and the two record shapes in one file      (sweep) ✅
```

**Order — one commit per step, A/B render gate on each**

| step | moves | also in the same commit | status |
|---|---|---|---|
| **H5** | reader, frames, glb, png, splat, colour | repoint `test_png_decode.py` to real imports | **done** |
| **H6** | bezier, lanes, decals | rewrote `test_decal_rows.py`, `test_clip_cut.py`, `test_drape_pick.py` | **done** |
| **H7** | terrain, areas | | **done** |
| **H8** | drape (harness keeps its BVH adapter) | added `HeightfieldSurface` + `test_heightfield.py` | **done** |
| **H9** | networks, tints, terrain lattice + face generation, area driver loop | three commits, as expected | **done** |

**The package is named `csiibundle`, not `cs2bundle`** — the earlier draft of this plan had it
wrong and every reference here is now the real name.

**The completeness sweep (post-H9).** The module list above was written before the extraction
and turned out to be incomplete: it enumerated the *algorithms* in the harness and missed
knowledge that was not algorithmic. Asking the question from the CONSUMER's end instead —
layer by layer, "can this be rebuilt from the package alone?" — found six gaps, all now closed:

| gap | why it mattered |
|---|---|
| `grading.json`'s **`lighting` block** was unparsed (`raw` only) | the illumination everything else is measured against: sun at **110,000 lux, 66°**, sky ambient tints, indirect multipliers, the in-game clock. The bundle's own semantics string says a consumer lighting the scene white misses the game's colour → `colour.Lighting` |
| **`lanes.jsonl` had no reader at all** | 680,629 records — the lane GRAPH (path, kind, length, owner). The layer a BeamNG builder or any simulation consumer wants most, and nothing in this repo had ever read it → new `lanegraph` |
| **`lights.json` was uninterpreted** | one file, **two record shapes**: 654 photometric lights vs 1,878 emissive colour slots (NeonSign, TrafficLight_Red, BrakeLight…). A consumer treating them uniformly makes 2,532 lights or none → new `lights` |
| the **§4 placement rules** lived in `main()` | the `__base` skirt condition (buildings with `elevationFlags` absent or `["OnGround"]` only) and the decal-kind lift → new `instances` |
| the **water surface** was harness-only | the dilated shoreline ring, ground-seated outer corners, and water NOT being filtered by the terrain cut → `terrain.water_surface` |
| the lane **`hang` cubic** was derived in the driver | `lanes.bend_part` took a finished cubic; the quadratic-to-cubic lift `(d0, (d0+h·L)·2/3, (d1+h·L)·2/3, d1)` was unreachable → `lanes.hang_curve` |

**The second sweep (field-level).** A repeat of the same layer-by-layer question would only
have confirmed the first sweep, so this one used a finer sieve: enumerate every FIELD the
producer actually ships across every layer, and check each against whether any consumer-side
code reads it. That found what the layer view could not.

**The headline: a `.glb` is one STATE of a prefab, and nothing consumed the other states.** The
producer ships `__teen`/`__elderly`/`__dead`/`__stump` and `__partial1`/`__partial2`/`__full`
derived meshes (§6.1), and `attrs.treeState`/`attrs.fullness` say which one each instance is in
— but no consumer-side code had ever joined on them. On the reference city that is **153,991 of
697,256 trees in a non-Adult state** (119,237 Elderly, 33,581 Teen) all drawing as healthy
adults: the consumer half of the very gap producer-side P18 closed in August. Separately,
**299,972 trees carry an `attrs.growthScale` below 1.0** — a uniform scale that is NOT folded
into `scale` (a tree record has no `scale` field at all), so every sapling drew at full height.
Both are now `csiibundle.instances` (`mesh_candidates`, `growth_scale`), and the harness uses
them. Measured on a 250 m downtown tree import: **420 of 2,229 trees swap mesh** (299 elderly,
121 teen), **1,101 take a growth scale** (0.719–1.272), and **0 positions move**.

Also closed: `lanes.jsonl`'s `limit`/`flags`/`secondary`/`curviness` (the first sweep's new
`lanegraph` documented only 5 of its 9 fields — 132,883 lanes carry a speed limit and 320,682
are CS2-derived rather than placed, which is a filter a consumer has to choose deliberately);
and `areas.heightRanges`, the game's own per-triangle prism band, which `areas.py` cited in its
docstring while ignoring the shipped values — 32.7% of area triangles are flat within tolerance
and conformance can skip them outright.

**The third sweep (render-rule level), and the biggest find of all three.** Asking "what does
the game do at draw time that we do not" turned up that **the `.glb` files carry no
metallic-roughness map at all**: measured, 1,449 of 1,449 glTF materials sampled have no
`metallicRoughnessTexture` and every one hard-codes `roughnessFactor` 0.9 — while **4,563 of the
bundle's 4,784 `materials.json` entries ship a real per-texel `mask`** whose R is metallic and
whose A is smoothness (§7.1). A glTF-only consumer therefore renders every window, metal roof,
wet carriageway and polished floor at one flat roughness. Sampled across 40 mask textures the
real values span metallic 0–1 (mean 0.21) and roughness 0–1 (mean 0.64), and **23 of 40 differ
from 0.9 by more than 0.1**. New `csiibundle.materials` carries the packing; the harness wires R
and A (and deliberately not G/B, whose two available readings disagree). 114 materials rig in
the standard region, geometry byte-identical, 18.6% of pixels changed.

**A fourth pass, on the three defects the user reported by eye** — white pools, no ocean
reflection, and "is the terrain missing something similar" — found all three to be the SAME
mechanism as the metallic gap, plus one more shipped-and-unread block:

| defect | cause | fix |
|---|---|---|
| **swimming pools render white** | a pool is two materials and the water one is `template:WaterGeneric` with **no maps at all** — CS2 draws water with a shader, so a glTF loader falls back to `baseColorFactor` `[1,1,1,1]`. Confirmed in the scene: the only textureless materials in a downtown import were both WaterGeneric at Base Color exactly (1,1,1,1) | `csiibundle.water` + `materials.shader_only_kind` |
| **every window pane too** | same mechanism, `DefaultGlassDoubleSided`, 109 materials, all textureless. 206 shader-only materials in total | same pass, given transmission |
| **ocean the wrong colour** | `rasters.water` ships `WaterRenderSettingsPrefab` verbatim — colour, scattering, absorption, smoothness, foam, waves — and **nothing read it**. The harness had three hard-coded numbers: (0.08, 0.22, 0.35) against the authored (0.366, 0.581, 0.6) | `water.Appearance`, driving the ocean AND the pools, because in game it is one shader |
| **ocean has no reflection** | not the water: a uniform grey world reflects as uniform grey. Raytracing was already on | new `--sky`, a physical sky at the bundle's OWN sun elevation/azimuth, **normalised** to the rig's 0.25 ambient (measured 14.28 mean, 57x) so the albedo identity survives |
| **terrain flat-lit** | all seven ground layers ship a `*_normal.png` and the splat blend read **none** — 0 normal maps wired, BSDF Normal input unlinked | all seven now composite in the same order, at the same opacities and tiling, as the albedo |

Also closed at this level:

| rule | why it is needed for 1:1 |
|---|---|
| `areaprefabs.json`'s **PBR block** — `normalMap`, `maskMap`, `metallic`, `smoothness` | before it shipped, pavement and plazas "rendered flat-lit with guessed specular" (audit #40 P11); 35 of 40 area prefabs carry a normal map and 23 a mask map, and the harness read none of them |
| **`rendererPriority`** | the game's overlap draw order — "without it a consumer has no rule for which of two overlapping areas paints on top". Rebuilt as geometry it becomes a rank-ordered lift inside the area rung: tiles +3.0 mm over sand +1.5 mm over grass +1.0 mm, all inside the ladder's 10 mm gap |
| **`attrs.state == "construction"` draws NOTHING** | `BatchInstanceSystem` skips the building; the crane and site fence are separate records the bundle already carries. Drawing it puts a finished tower on a building site |
| `attrs.elevation`, `attrs.decalLayers`, `attrs.decalHeight` | ground level is `pos[1] − elevation` and it is signed (29,610 records negative, to −86.3 m); decal layers keep a road marking off the wall behind it |

**One thing checked rather than assumed:** `materials.json`'s render fields look unread, but the
`.glb` files carry them and a glTF consumer gets them free. Verified rather than asserted —
across 117 material uses the `.glb`'s `alphaMode`/`doubleSided` agree with the table exactly (46
BLEND, 71 MASK, 47 double-sided). That layer is a convenience table whose unique content is
`colorMask` and `tint`; the note is now in `reader.load_materials` so nobody re-investigates.

Gates for the first sweep: every path A/B'd against the pre-sweep harness — standard region, full-map
terrain (water at stride 4), `--skirts` (102 placed), `--lanes`. In all four the **only** differing
line in the scene dump is `carto_sun`'s rotation matrix, by 6e-08, because the direction is now
normalised in float64 before truncation and comes out *closer* to unit (1.0000000031 against
1.0000000696). That moves **4 pixels of 2,560,000**, one of them by 8/255 — a shadow edge. Worth
recording: the pixel gate is more sensitive to a light's direction than to geometry.

**H5 as built.** `colour_model.py` stays on disk as the Blender/CLI adapter — the locator, the
`DEFAULT_BUNDLE` path, `REFERENCES` and `main()` — re-exporting the derivation from
`csiibundle.colour`, so `python tools/colour_model.py --explain` and `probe_point.py`'s
`import colour_model` both keep working unchanged. `test_splat_extras.py` was repointed too (it
regexed the same PNG decoders out of the harness) and gained a check 0 that scores
`Splat.extras` against its own reference decode, so the rest of that test is about the format
rather than about itself. Its `BUNDLE` constant and `test_png_decode.py`'s both still pointed at
the retired `Carto/spike/` subfolder, which meant the real-raster half of `test_png_decode` had
been silently skipping for months; fixed, and all five real rasters now decode PIL-exact.

**Running the gate.** Take the baseline *before* the first move, from the bundle as it stands,
and keep the same region and camera for every step:

```sh
BLENDER="G:/SteamLibrary/steamapps/common/Blender/blender.exe"
BUNDLE="C:/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto"

# import a region  (--center interchange|downtown|auto, or explicit X Z)
"$BLENDER" --background --factory-startup --python tools/blender_import_bundle.py -- \
    "$BUNDLE" --networks --terrain --lane-markings --center interchange --radius 350 \
    --save base.blend > base.log 2>&1

# render it — the camera args are the "aimed N viewport(s) at X … Y …" line from the log,
# NOT world coordinates: the harness has already converted them
"$BLENDER" --background base.blend --python tools/ab_render.py -- base.png <X> <Y> 528

python tools/ab_compare.py base.png step.png      # pixel diff — must be zero
python tools/ab_compare.py --counts base.log step.log   # log-count diff, secondary signal
```

`ab_compare` strips elapsed times before diffing counts, so timing jitter never fails a run.

**The pixel gate has a noise floor, and it is not zero.** Rendering *the same* `.blend` twice
gives `max=1` — EEVEE is not bit-deterministic — so "pixel-identical" cannot mean a zero diff
and a step reporting `max=1` or `2` has proved nothing on its own. Where the answer matters,
compare the **scene** instead of a render of it: dump every object's `matrix_world`, every mesh's
vertex/loop/UV digest and every `FLOAT4X4` attribute from both `.blend` files and diff those.
Every H5–H9 step did, and it is the reason those results are facts rather than hopes. The dump
script is thirty lines.

**THE STANDARD REGION DOES NOT EXERCISE EVERYTHING, and this bit almost every step.** The
interchange A/B was byte-identical for four consecutive steps that had *not yet been checked* on
the path they actually changed. What the region misses, and what each needs instead:

| path | why the region misses it | the run that covers it |
|---|---|---|
| rigid-tile instancing | `--lane-markings` skips roadside furniture | `--kinds none --lanes` |
| area conformance | no `--areas` | `--kinds building --areas --terrain --center downtown` |
| `ribbon` (§5 centerline form) | its four categories — pipe, cable, waterway, taxiway — are all **off by default**, and every in-region record is mesh form | `--kinds none --networks --net-categories all` |
| per-instance tints | `--kinds none` places no instances | `--kinds building,tree,prop` + dump `cs2_color*` |
| coarse quads, transition fans, the sub-cut | a region import is **stride 1** and native everywhere | a full-map `--kinds none --terrain` (stride 4) |
| `HeightfieldSurface` | the harness keeps its BVH and never calls it | `tools/test_heightfield.py` |

Before gating a step, ask which of these it touched, and run that one too.

**Rules for every step**

1. `csiibundle` never imports `bpy` at module scope — `python -c "import csiibundle"` under system
   Python is part of the gate. (`colour_model.py` is already clean except `blender_config_path()`,
   which is a Blender *locator*, not colour maths — leave it in the harness.)
2. **The A/B render must be pixel-identical to within the noise floor above**, and where it is
   not obviously so, the geometry dump must be *exactly* identical. A pure refactor that moves
   pixels is a bug. Investigate the diff; never widen the tolerance.
   *H5's one accepted exception:* 100 of 621 dumped lines differ, all of them object
   `matrix_world` rotation terms, by at most **2.4e-07** — two float32 ULPs, about 7 µm across a
   building. Zero mesh lines and zero structural lines differ. The cause is understood and
   one-directional: `frames.instance_matrix` composes in float64 and truncates once at
   assignment, where mathutils composed every intermediate in float32, so the new numbers are
   the *more* accurate ones. Anything larger than this, or any difference in a MESH line, is a
   real defect.
3. `test_decal_rows.py` and `test_png_decode.py` regex-extract functions from the harness and
   break the moment those functions move. Repoint them in the *same* commit.
4. Dependencies: package = stdlib + numpy. `validate_bundle.py` stays stdlib-only.
5. Import pattern, because Blender's `--python` does not add `tools/` to `sys.path`:
   `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` then `import csiibundle`.
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
| A hue error remained: +6.9° sand, +6.2° pavement, +1.2° grass, with light, tonemapper and working space all eliminated (SPLAT-4) | Fog was the suspect and the 18:10 rip **killed it** — fog is on at a 6.9 km mean free path but its tint is pure white, and white attenuation cannot shift hue. The same rip's new `diagnostics.unread` named **`SplitToning`**, a grading operator that tints shadows and highlights separately: the only one in the chain that shifts hue *by luminance*, which is how the residual is distributed (both errors are on bright surfaces, grass is dark and nearly exact). Captured now; the prediction it must satisfy is in H14 |

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
| **H5–H9** | extract `tools/csiibundle/` — see §2 |
| **H10** | **Schema completion.** Ten documented layers still have no JSON Schema: `lanes`, `laneprefabs`, `colors`, `lights`, `areas`, `areaprefabs`, `clipvolumes`, `warnings`, `terrain/surfaces/index`, and **`grading`** (now a declared layer, and it gained `fog`). Validator gains structural checks plus the cross-file joins, staying stdlib-only. Then extend `make_example_bundle.py` to exercise the layers it currently skips, and add negative cases per schema |
| **H11** | **Consumer docs.** `docs/consumer-guide.md` (layer tour, pipeline order and its two hard dependencies, must-implement tiers mapped to modules, the drape protocol, CONTRACT-vs-TUNED constants, a worked example tracing one instance, an LH-Y-up → RH-Z-up appendix); `docs/consumer-caveats.md`; `tools/README.md`; `docs/bundle-changelog.md` |
| **H12** | **C# refactor** of the ripper additions — behaviour-preserving, no output-shape change. Gate is a clean build plus `/code-review`; proof rides H14 |
| **H13** | **Producer polish.** One item left: the rip-completion dialog should say *"N prefabs come from asset packs that are not enabled — enable them and re-rip for full fidelity"*, derived from the `unresolved` flag and the 89 unresolved-prefab warnings. It already reports path, stage times and summaries |
| **H14** | **Final re-rip (user action) + acceptance.** Validate against all 15 schemas; check every ⟳ change against real output; full Blender import for visual acceptance (A/B against pre-rip renders is *not* expected to match — the producer changed); fix measured-fact drift in the docs |

**What H14 must check.** The 18:10 rip already settled most of this list — `__teen`/`__elderly`
at 32 each, `fog` present, `COLLAPSED:` clean, tiling unchanged, validator VALID/1. What is left:

- **`splitToning`** — the block, and with it SPLAT-4. The prediction on record is that
  `highlights` is tinted toward yellow and `shadows` is at or near grey, because the residual is
  +6.9° on sand and +6.2° on pavement (both bright) against +1.2° on grass (dark), and split
  toning is the only operator in the chain that shifts hue by luminance. **Both ends tinted
  equally, or only the shadow end, falsifies it** — write the outcome down either way.
- `diagnostics.unread` now reports values, so read it again: the fifteen remaining components
  arrive with their overridden parameters attached, and anything colour-relevant among them is
  the next lead if split toning is not the answer.
- Whatever H12's C# refactor touches.

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
| H5–H9 | 5–9 | Phases 3–7 | extract `csiibundle` |
| H10 | 10 | Phase 8 | schema completion |
| H11 | 11 | Phase 9 | consumer docs |
| H12 | 12 | Phase 10 | C# refactor |
| H13 | 13 | Phases 11 + 12 | producer polish |
| H14 | 14 | Phase 13 | final re-rip |

`PLAN.md`'s Phases 0–8 are a different sequence — the producer build, all finished. The approved
plan at `C:\Users\naomi\.claude\plans\encapsulated-questing-bunny.md` is still good for scope and
rationale; its status blocks are superseded by this file.
