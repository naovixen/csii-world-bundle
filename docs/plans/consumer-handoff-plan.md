# Consumer hand-off — the plan

**Goal:** make the CS2 World Bundle consumable by someone who has never read this repo, so a
BeamNG level builder (or any other consumer) can be written against the bundle alone.

**Status 2026-08-14: H1–H13 are closed. The only thing left is H14, and it needs the user.**
`tools/csiibundle/` exists: **19 modules**, no `bpy` anywhere in it, and
`tools/blender_import_bundle.py` is an adapter over the package rather than the only copy of the
reconstruction. **Four coverage sweeps** followed H9, each asking a different question — layer,
field, render-rule, and the three defects visible by eye — and each found gaps the previous one
structurally could not. All are closed; §2's closing notes carry them. H10–H13 are §5.

**H14 is a user action: rip the city in-game, then acceptance.** §4 is what that rip has to
answer.

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
`COLLAPSED:` reporting nothing. **Five producer changes now post-date it**, and every one of them
rides H14's rip:

1. the `splitToning` capture with the valued `diagnostics.unread`, both added *because* of what
   this rip reported;
2. the **area normal-map rebuild** (§3, the black-areas fix) — visible in the bundle as
   `terrain/surfaces/area_*_n.png`, of which **20 of 35 are still two-channel packed on disk**;
   `tools/test_normal_packing.py` prints the count, so the next rip is checked against it rather
   than assumed;
3. **`boneIndex`** on a punctual light (H10) — the field that says a light's position is *not*
   object-frame;
4. the **asset-pack line** in the completion dialog (H13);
5. everything **H12** touched — four `warnings.jsonl` writers, every coordinate's precision, the
   shared object query, and network piece asset ids;
6. the **six producer fixes the `/code-review` pass found** (§5) — two of which move geometry:
   the roundabout at-grade branch, and `LowerBottomToTerrain` no longer suppressed on elevated
   compositions.

Everything else in the bundle is current.

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
| `tools/test_bundle_contract.py` | **88/88** (63 before H10) |
| `tools/validate_bundle.py docs/example-bundle` | VALID, 0 warnings — and the example now carries a record in **every** documented layer |
| `tools/validate_bundle.py <full city>` | VALID, **1 warning** on the 18:10 rip, against all **15** schemas (was 133 — see PILLARS-2). The one warning is the genuine parent advisory; the 119 declared gaps are a note. ~7 minutes |
| decal rows / png decode / splat extras / drape pick | pass |
| node fan / clip cut / normal packing / heightfield | pass |
| `python -c "import csiibundle"` under system Python | clean |

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
  materials.py §7 channel packing — the metallic/roughness glTF omits      (sweep) ✅
  water.py     §8 rasters.water — the ocean AND every swimming pool        (sweep) ✅
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
**299,972 trees carry an `attrs.growthScale`, 130,880 of them below 1.0** *(count corrected
2026-08-14: the earlier figure conflated "carries one" with "carries one below 1")* — a uniform scale that is NOT folded
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
| `attrs.elevation`, `attrs.decalLayers`, `attrs.decalHeight` | ground level is `pos[1] − elevation` and it is signed (970 records negative on the 08-12 rip, to −86.3 m); decal layers keep a road marking off the wall behind it |

**The area PBR block bit back: the surfaces rendered BLACK, and it was not the wiring.** Reading
`areaprefabs.json`'s `normalMap` was right; what it exposed is that **the area path never
rebuilt its two-channel normal maps**. A two-channel normal ships X and Y and lets the shader
reconstruct Z, and CS2's art arrives in both of the layouts Unity's `UnpackNormalmapRGorAG`
straddles — **AG** (X in alpha, red pinned white) and **RG** (X in red, blue pinned black).
`MaterialRipSystem.ReconstructTwoChannelNormal` has detected and rebuilt them since the
`661a441` mesh-pipeline work — it is what "each road is a different colour" turned out to be —
and it is called on the object path and on the ground-surface path. It was never called on the
area path. Measured on the bundle as it stands: **18 of 35 area normal maps are AG and 2 are
RG**, against 0 of 7 grounds and 0 of 40 objects sampled. Read as tangent RGB, AG gives
`normal.x = +1` — a shading normal lying flat along the surface, which on level ground faces 90°
away from any sun — and RG gives `normal.z = -1`, pointing into the surface. Both shade black,
which is exactly what was on screen: green terrain, and every driveway, lot, path and plaza in
the frame near-black.

Fixed at both ends, because they answer different questions:

| end | change | when it takes effect |
|---|---|---|
| **producer** (the real fix) | `TerrainRipSystem`'s `WriteAreaTexture` now runs the rebuild on `normalMap`, the one funnel all four area normal-map sites pass through; `ReconstructTwoChannelNormal` gained the RG signature beside the AG one it already had | **next re-rip** — contract §7.1 promises consumers ONE convention for every normal map in the bundle, and this is where the area half of that promise is kept |
| **consumer** (what un-blackens today's bundle) | `materials.detect_normal_packing` / `unpack_normal` classify a map from a strided sample using the producer's own thresholds, and the harness rebuilds a packed one in nodes — X from its channel, Y from green, Z reconstructed — and **warns** that it had to | immediately, and it is a no-op once a re-ripped bundle ships them unpacked |

**What is deliberately NOT guessed at.** 15 of the 35 area maps match neither signature: red is
near-constant at ~0.89 with real variation, alpha pinned, and no colour-space reading makes them
unit-length (raw |n| 1.257, sRGB→linear 1.199, linear→sRGB 1.414 — so it is not a gamma mangle).
They are left exactly as shipped, which renders them with a uniform ~40° normal tilt rather than
black. Inventing a layout for them would invent relief the game does not draw; the honest state
is "unclassified, recorded here".

Gates: `tools/test_normal_packing.py` — detection on maps whose layout is known by construction
(including the two negatives that would break working paths: a flat RGB map, and an RGB map with
data in its alpha, which is exactly what the seven grounds are), round-trip through each layout
within 8-bit quantisation, and the black itself asserted as `N·L` rather than by eye. Nine
standalone tests, validator VALID/1, 20 modules clean, `dotnet build` 0 warnings. On the area
region: **7 count lines identical** — no geometry moved — and the render goes from black lots to
sand, concrete and pavement at their own albedo.

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

## 4. The remaining phase

| phase | what it delivers |
|---|---|
| **H5–H9** | extract `tools/csiibundle/` — see §2 |
| **H10–H13** | schemas, consumer docs, the C# refactor, the producer polish — **all closed**, see §5 |
| **H14** | **Final re-rip (user action) + acceptance.** Validate against all 15 schemas; check every ⟳ change against real output; full Blender import for visual acceptance (A/B against pre-rip renders is *not* expected to match — the producer changed); fix measured-fact drift in the docs |

**What H14 must check.** The 18:10 rip already settled most of this list — `__teen`/`__elderly`
at 32 each, `fog` present, `COLLAPSED:` clean, tiling unchanged, validator VALID/1. What is left:

- **`splitToning`** — the block, and with it SPLAT-4. The prediction on record is that
  `highlights` is tinted toward yellow and `shadows` is at or near grey, because the residual is
  +6.9° on sand and +6.2° on pavement (both bright) against +1.2° on grass (dark), and split
  toning is the only operator in the chain that shifts hue by luminance. **Both ends tinted
  equally, or only the shadow end, falsifies it** — write the outcome down either way.
- `diagnostics.unread` now reports **values**, so read it again. The 18:10 rip reported names
  only and named **sixteen**: `Bloom`, `CloudLayer`, `DepthOfField`, `DiffusionProfileList`,
  `GlobalIllumination`, `HDShadowSettings`, `MotionBlur`, `ScreenSpaceAmbientOcclusion`,
  `ScreenSpaceReflection`, `ScreenSpaceRefraction`, **`SplitToning`**, `TerrainRendering`,
  `VisualEnvironment`, `VolumetricClouds`, `WaterRendering`, `WindVolumeComponent`. If split
  toning is not the answer, the colour-relevant ones to read next are `VisualEnvironment`,
  `GlobalIllumination`, `TerrainRendering` and `WaterRendering`.
- **The area normal-map rebuild.** `tools/test_normal_packing.py` prints how many of the 35
  `terrain/surfaces/area_*_n.png` are still two-channel packed. It is 20 on the 18:10 rip; it
  must be **0** after this one. The consumer-side unpack is a no-op once it is.
- **`boneIndex`** on `lights.json` (H10) — expect it on ~4 punctual entries, all bicycles and
  one workshop asset, and on none of the other 605.
- **The asset-pack line in the completion dialog** (H13) — expect *"89 prefab(s) in this city
  come from asset packs that are not enabled"*, or a smaller number if packs were enabled since.
- **Everything H12 touched**, which is where the real risk is: the four `warnings.jsonl`
  writers, the coordinate precision of every position and rotation in the bundle, the shared
  object query, and network piece asset ids. A clean `validate_bundle.py` covers most of it —
  the precisions are checked by the schemas, the query by the instance/mesh counts agreeing, the
  ids by the mesh-stem check. **`warnings.jsonl` is the one to read by eye**: 210 declarations,
  119 `mesh` and 91 `prefab`, and the merge must have kept every pass's lines.
- **The two review fixes that move geometry** (§5, the `/code-review` pass). The roundabout
  at-grade branch should *reduce* carved terrain at any roundabout on a trenched approach — look
  for terrain that used to be missing beside one. The `LowerBottomToTerrain` narrowing should
  put an elevated composition's abutment and retaining pieces back on the ground. Neither has a
  counter of its own; both are visible in a render and in nothing else.
- **The `COLLAPSED:` probe is now per-composition**, so it may report rows the 18:10 rip could
  not see. A new row is a finding, not a regression — read it before assuming the rip got worse.

---

## 5. Done: H10–H13 (2026-08-14)

### H10 — a schema for every layer

`docs/schema/` held **five** documents for a contract with **fifteen** layers, so ten layers
were specified in prose only. That is how both of the errors below survived: nothing ever forced
the question *what values does this field actually take*. The ten new documents — `lane`,
`laneprefabs`, `colors`, `lights`, `area`, `areaprefabs`, `clipvolume`, `warning`,
`surfaceindex`, `grading` — were derived from the **producer's own writers** and cross-checked
against the full-city rip, not written up from the prose.

Two field descriptions were wrong, and writing a schema is what found them:

| field | what the contract said | what it is |
|---|---|---|
| §5A `owner` | "the id of the `networks.jsonl` record this lane belongs to … absent for lanes owned by a node" | the generic ECS `Owner`. Of 677,566 lanes carrying one: **21.7%** resolve into `networks.jsonl`, **19.1%** into `instances.jsonl`, **59.2% into neither** (net nodes get no record, §5.1). 3,063 more carry none at all. Advisory grouping key |
| §7.2 `parentMesh` | the sub-mesh a punctual light's `position` is relative to | a **colour selector**. The full-city run rejected `parentMesh: -1001`, and a sweep of every reader of `Effect.m_ParentMesh` found three, none of which places a light. Positions are **object-frame** |

That second one produced the session's one producer change: what *does* move a light is
`m_BoneIndex` — `FindBoneIndex` rewrites position and rotation into the matched bone's bind-pose
space — and the bundle carried no trace of it, so a consumer could not tell which entries were
not object-frame. `boneIndex` now ships in exactly that case. ⟳

`csiibundle.lights` picked up both, plus a latent bug: the photometric discriminator was `type`,
which the producer writes only when the managed `LightEffect` resolves, so any punctual entry
whose photometry could not be read was **silently dropped from both lists**. It never fired on
this city (654 punctual, 654 with `type`), which is why it was worth fixing.

The validator loads all fifteen and gained three cross-file checks a schema cannot state:
`heightRanges` must run parallel to `triangles`; an area's `owner` must join to an instance
(§8b calls that join total, so a dangling one means a **mixed-vintage bundle**); and a §7.2 key
matching no mesh or glTF node name is reported — 184 of 3,620 palette keys and 86 of 1,186 light
keys, all for geometry the bundle deliberately excludes.

The example bundle now carries a record in **every** documented layer, including a third
instance whose mesh is deliberately absent and declared in `warnings.jsonl` — the §10 amnesty as
a live case rather than as prose. `warnings.jsonl` is schema-validated now, because
`_load_declared_missing` reads `assetId` and `reason` off it and a line the validator cannot
parse silently stops excusing anything.

**Gates:** 87/87 contract tests (24 new, one negative case per new schema); `validate_bundle`
VALID/0 on the example and **VALID/1** on the full city — the same single genuine parent
advisory as before, now with 680,629 lanes, 3,620 palettes, 1,186 light keys and `grading.json`
under schema. Full-city validation takes ~7 minutes. `dotnet build` 0/0.

### H11 — consumer docs

Four documents, each answering a different question, cross-linked from `README.md` and the top
of `bundle-schema.md` so a consumer author lands on the route rather than on the law:
`docs/consumer-guide.md` (the route), `docs/consumer-caveats.md` (what is true and surprising),
`docs/bundle-changelog.md` (which vintage of bundle you have, with a detection recipe per
entry), `tools/README.md` (what each tool is for). The guide's worked example is taken from
`docs/example-bundle`, so every number in it can be run.

### H12 — the C# refactor

Deliberately narrow, because nothing here can be *proved* before H14: three duplications where
the duplicated thing is a load-bearing invariant, plus one inconsistency that was a latent bug.
−437 lines and +136 across eight files, plus three new ones.

| what | why it mattered |
|---|---|
| `IO/WarningsFile.cs` — the §10 read-keep-rewrite protocol, **4 copies → 1** | both halves of the ownership rule have already been broken in opposite directions: truncating instead of merging made a meshes-first rip report 118 declarations and ship 87, and appending instead of replacing made a twice-ripped bundle declare every gap twice |
| `IO/BundlePrecision.cs` — the coordinate precisions, **5 copies → 1** | mm for positions, 6 dp for quaternions, both contract-visible. The later copies carried comments saying *"as `InstanceWriter` does"* and *"matching every other coordinate in the bundle"* — a constant whose copies describe each other wants to be one constant |
| `Systems/RipQueries.cs` — the world-object query, **2 copies → 1** | the instance and mesh passes carried byte-identical descriptors with the reasoning on only one. Exclusions are the dangerous half (a rule matrix has no row for absence), and if the copies drifted the two layers would describe different worlds with nothing to say so |
| `NetworkRipSystem.SanitizeId` → `MeshRipSystem.SanitizeAssetId` | **not purely a refactor.** The two sanitisers disagreed: the network one tested with `char.IsLetterOrDigit`, which is Unicode-aware, so a non-Latin letter came through and produced an id the contract's `^[A-Za-z0-9][A-Za-z0-9._-]*$` rejects. The same file already called the other one twice, so this was an inconsistency inside one system |

**One thing found and deliberately NOT fixed.** `AppearanceRipSystem` has a **third** object
query, and its doc comment claimed it "matches the instance rip". It does not: it excludes only
`Deleted`/`Temp`/`Marker`, while the instance and mesh passes also exclude `Moving`, `Creature`
and `Vehicle`. That superset is exactly where the validator's new note comes from — 184 of 3,620
`colors.json` keys and 86 of 1,186 `lights.json` keys match no mesh, almost all vehicles and tree
`*Child*` states. Harmless to a consumer, but it carries a **latent join hazard**:
`BuildAssetIds` disambiguates a shared name only against the set it is handed, so a name shared
between a building and a vehicle would be digest-suffixed in `lights.json` and left bare in
`meshes/`, and the punctual-light key would stop joining. **Not live** — zero keys in either
table are digest-suffixed today, against 7 mesh stems that are. Narrowing the query closes it
*and* drops 270 keys, which is an output change, so it waits for a rip that can prove it rather
than riding a refactor. The comment now states the difference instead of denying it.

**Gate:** `dotnet build` 0 warnings 0 errors, plus `/code-review` — whose findings are below,
and were substantial. Proof rides H14 — see §4 for what to look at.

### H13 — the asset-pack line in the completion dialog

A prefab whose asset was not loaded this session is not a producer failure and not a bundle
defect; the game itself drew those objects as nothing. But it is the **one shortfall a re-rip
fixes**, and it reached only `warnings.jsonl` and `areaprefabs.json`, which nobody opens after a
rip — 89 prefabs going unmentioned every time. Counted from the bundle on disk rather than from
the run, so the line stays true after a partial rip, and matched on the `unresolved_<index>`
assetId rather than on `kind: "prefab"`, because that kind also covers prefabs that resolved
fine and simply have no SubMesh buffer — enabling a pack cannot help those, and promising it
would be a cure that does not exist. ⟳

### The `/code-review` pass, and what it found

H12's second gate returned fourteen findings and **eleven were real**. Worth recording because
of what they were: two of them were mine from this session, and the rest were latent —
dormant-until-the-input-changes bugs that no gate in this repo was ever going to fire on.

**Producer, and the two that change geometry** — both ride H14:

| finding | what it was |
|---|---|
| `TerrainRipSystem.RoundaboutSecondPair` bailed on `!elevated && !tunnel` | the game's `AddNode` has a **third** branch: an at-grade side carrying `LowTransition` beside `Raised`/`Lowered`/`SoundBarrier` gets the same ring trim and has both bits **cleared**. Keeping them raised `cutTop` to `ElevationLimit*3` — 12 m — over an untrimmed ring, carving terrain the game leaves standing. The case is a roundabout on a road coming out of a trench |
| `elevatedAwayFromGround` suppressed `LowerBottomToTerrain` as well as `RaiseTopToTerrain` | the argument for the gate is about a TOP being dragged down; it says nothing about a bottom reaching up to meet the ground, and the game tags the two independently. An elevated composition's abutment and retaining pieces were shipping floating. Narrowed to `raiseTop` only — **and the gate is now labelled a producer CHOICE**, because `BatchMeshHelpers` conforms nothing on the CPU: it writes the class into a vertex `Color32.b` and the compiled shader decides. There is no managed counterpart to port |
| the RG normal-map test had only a negative signature | `lowBlue >= threshold && highRed < threshold` says "blue dark, red not white", which a **black placeholder normal** satisfies everywhere — and it was then "rebuilt" from `x = y = -1` into a uniform sideways normal, the exact defect the method repairs. Now requires red to carry something too, mirroring AG's `minA < 250`. Fixed in both the producer and `csiibundle` so the two still agree |
| `_groundSources` was never cleared between rips | rip, change theme, rip again: `sources.json` declared the previous theme's grounds, naming `.png` files no longer in the bundle |
| `sources.json` was the one unguarded write in `CaptureSurfaceTextures` | and `RipTerrain` has no outer try, so a locked file took splatmap, tiling, backdrop, clip volumes, areas and `rasters.json` with it — for a file the code itself calls "provenance only" |
| the `COLLAPSED:` probe keyed on `(piece, isNode)` | extents accumulate against the *current segment's* centre, so unioning across compositions unions different frames. A piece that collapses on the elevated tram deck and builds fine on 200 at-grade segments read as healthy — which is **exactly the §42 shape the probe was built for**. Now keyed by composition too |

**Consumer:**

| finding | what it was |
|---|---|
| `SHADER_ONLY_TEMPLATES` missed `Windows` and `Default` | so 12 of the 218 textureless materials fell through `if kind is None: continue` and rendered as **flat white slabs**: 7 window panes and 5 that are the city's fountains and a hot tub. `Windows` is now in the table (632 uses, 625 with maps, so only unbound panes reach it); the 5 `Default` ones are caught by CS2's own `_Wat_` name suffix, which **47 sibling materials corroborate**. All 218 now classify |
| `colour.smh_prepared` sliced `w` off with `[:3]` | dropping the game's `num = w * (sign(w) < 0 ? 1 : 4)` offset and its `max(x + num, 0)` clamp — verified against decompiled `ColorUtils` — while `explain()` claimed the step was an exact managed port. Dormant (all three `w` read 0.0 today), so no number moved |
| `areas.priority_lift` was `rank * 0.0005` uncapped | against the invariant stated four lines above it. At 21 distinct `rendererPriority` values the top surface reaches the `instance_decal` rung and paints a parking lot over its own bay markings. The catalogue has 13, so the 13-value spread is byte-identical; only ≥16 compress |
| two `TypeError`s in `probe_point.py` | both on a `channels.base: "unknown"` bundle, and both after the whole report is built and before a line of it prints |
| `decals.lattice` indexed the raw `stations` argument | after coercing everything else with `asarray`, so a caller passing a list crashed |
| `blender_import_bundle` fell back to `_SPLAT_CHANNELS_DEFAULT["R"]` for the base ground | which is `"dirt"`; the base's own default in the same dict is `"rock"`. It fires exactly when `channels.base` is `"unknown"` — so every cliff face and gorge wall, precisely where the base shows through, was painted with `dirt.png` |
| `validate_bundle._load_json_layer` returned a non-object to callers that `.items()` it | a truncation leaving a valid JSON array gave a traceback and **no verdict** in place of the INVALID the schema had already established |
| `test_lane_owner_resolving_nowhere_is_accepted` asserted on a substring the validator emits unconditionally | so it passed without its own mutation. Now asserts the count, with a companion test that the pristine example reads zero |

**Not findings:** the H12 dedup itself came back clean — the four warnings writers migrated with
their kind-ownership intact, and the taxiway drop survived. One cosmetic leftover (three
`/// <summary>` openers from a deleted method) fixed.

**Gate:** `dotnet build` 0 warnings 0 errors; **88/88** contract tests; every standalone test
passes with unchanged numbers (83,110 decal spans, clip cut ok, 5,872 node strips). Proof of the
producer half rides H14 — see §4.

---

## 6. Rules that have already cost something

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
