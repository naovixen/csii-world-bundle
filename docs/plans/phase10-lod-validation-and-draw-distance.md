# Phase 10 — the LOD chain validated against the game, the blank-material box, and the draw-distance study

Written 2026-08-29 (evening), on the 2026-08-29 21:29 rip (`lodCap 2`, 1,983 chains) and the
`cs2_city_fine` level built from it at 22:42 (`lod_detail_scale 10`, `view_distance 4000`,
`imposters` on, `imposter_min_metres 0` → 3,000 m). Every number below names its instrument.
The game's source is the 2026-08-29 decompile of `Game.dll` (`tools/decomp all Game`).

Three questions were asked: **is the authored-chain port right**, **do the level's performance
mechanisms work together**, and **what would it take to see the whole map** — silhouettes of
buildings and landscape from the far shore — without the pop-in the current ladders produce.
A fourth arrived mid-way: the "2d square box" on some buildings (Naomi's two 22:56 / 22:59
screenshots).

---

## 1. The box on the beach towers — a blank material, not a LOD

**What it is.** The three `Alvear_Tower_Buenos_Aires` instances (a custom asset; two on the
beach 400 m apart, one inland — the exact placement of the two blue slabs in the 22:56 shot and
the white one in the 22:59 shot). Its authored LOD 1 (19,773 triangles, upright, same node
transform as LOD 0) binds a material the bundle names
`TorreAlvear_LOD.001_3e4b7a6c…` — a Blender `.001` suffix that survived the asset author's
export. `dae.sanitize` allowed `.`, so the level wrote that string, byte-identical, as the
`.dae` material, the `materials.json` key, its `name` and its `mapTo` (checked in the cooked
`.cdae` and in `art/cs2/main.materials.json`). BeamNG still drew the level with the engine's
blank placeholder: a glossy white/sky-blue slab 72 × 196 m — the "square box", with the
neighbouring tower reflected in it. A material is a named `SimObject` and a `.` is not a valid
name character; nothing else in the level's 8,799 material references fails to resolve (a
byte-scan of every `.cdae` against the materials file: **exactly one** miss, this one; the
other dotted material in the bundle, `OraclePark_LOD.003_…`, is not placed).

**Ruled out first, for the record:** a leaked impostor level (no `_LOD<n>` node in the bundle
with ≤ 6 triangles is wider than 8 m; the eight ≤ 12-triangle levels are all roof AC units and
window guards with normals and UVs — authored boxes, not cards), and BeamNG's runtime
`bb_autobillboard` (the 190 shapes that carry one all cull past 2.4 km; the Alvear's billboard
band starts at 23 px = 8 km on a 1080p 45° reference, further still under Torque's real
projection — §2.4 — so it cannot be the thing standing 500 m off the beach).

**Fixed** (`apps/cs2beamng/beamng/dae.py`): `sanitize` no longer admits `.`; the shape and the
materials file go through the same function, so both sides rename together. Rebuilt in place
(the build is running as this is written; the Alvear `.dae` changes bytes, its `.cdae` is
re-cooked, `main.materials.json` is rewritten). **Naomi's look, 2026-08-30: "no more squares" — accepted.**

**Also removed, on the game's rule:** `bb_autobillboard` on every shape that ships an authored
chain (`shapewrite._imposter_thresholds`). CS2 draws the coarsest authored level right up to
the cull and has no impostor for these prefabs (`RequiredBatchesSystem`, §2.1); LOD 2 is 4.5 %
of LOD 0 city-wide, so the billboard bought nothing a 40-triangle level does not already give,
and a runtime imposter atlas is the one thing BeamNG builds per shape at play time and never
frees (`beamng-imposters-are-built-at-runtime`). 190 shapes lose the node; the imposter stays
a declared consumer addition for the generated ladder only. This is the second change in the
rebuild.

---

## 2. The authored chain, rule by rule against the game

Port: `csiibundle/lodchain.py`, `stages/shapelods.authored_chain`, `stages/shapeparts.split_lod_nodes`,
`Systems/MeshRipSystem.Lods.cs`. Game: `RequiredBatchesSystem.InitializeBatchGroup`
(`Game.Rendering.RequiredBatchesSystem.cs:482-712`), `MeshSystem` (`:451-504`, `:768`),
`RenderingUtils.GetRenderingSize(float3, float)` (`:142`), `LodProperties`, `LodMesh`.

| # | Game rule (decompiled) | Producer / consumer | Verdict |
| --- | --- | --- | --- |
| 1 | `MeshSystem` fills `LodMesh` from `LodProperties.m_LodMeshes`, **sorted richest first by `indexCount`** (`:465-489`), null entries logged and dropped | `ReadLodChain` walks the buffer in order and numbers levels 1.. as it goes | match |
| 2 | `MeshData.m_LodBias = LodProperties.m_Bias`, then `-= log2(sqrt(clamp(surfaceArea / (csum(size·size.yzx)·2), 1e-6, 1)))` (`:459, :497-501`) | `lod.lod_bias(size, area, authored_bias)` — same expression; `m_Bias` rides on the part node's `extras.lodBias` | match |
| 3 | `x = meshData.m_MinLod`, then `x = min(x, 255 − lodCount)` (`:537, :622`) | `authored_lod_limits`: `x = min(mesh_min_lod(...), 255 − k)` | match |
| 4 | Walk `lodCount−1 … 0` (coarsest first); each level's batches take `m_MinLod = x` **before** `x` is raised (`:625-704`) | `for j in range(k, 0, −1): limits[j] = x; …raise x` | match |
| 5 | `x = clamp(CalculateLodLimit(GetRenderingSize(groupSize, meshSize, indexCount, stack), bias) + 1, x + 1, 255 − num5)` (`:695`) | `x = min(max(calculate_lod_limit(rendering_size_indexed(group_size, index_count), bias) + 1, x + 1), 255 − (j − 1))` | match |
| 6 | `GetRenderingSize(float3 size, float indexCount) = csum(size) · 0.57735 · rsqrt(indexCount)`; the `StackDirection.None` overload sizes by the **group's** box, not the level's (`:186-206`) | `rendering_size_indexed(group_size, …)` — group box, verbatim constant | match |
| 7 | `indexCount` for a level = `meshData3.m_IndexCount`, **except** a `Decal` or `Impostor` level, which is sized as `LOD 0's indexCount × 0.25` (`:670`) | impostor levels are left out of the bundle (producer, `34bbda0`; parser skip for the loaded bundle). With the impostor gone the level above it is given the limit the impostor would have had — i.e. our coarsest mesh runs out to where the game would be drawing its card. | **approximated, counted** (`ImpostorLevelsSkipped` in the rip report; every vanilla tree, per the 2026-08-29 build) |
| 8 | LOD 0's batches take the final `x` (`:698-711`) | `limits[0] = x` | match |
| 9 | `StackX/Y/Z` overloads (`:168-206`) | not ported; declared in the docstring. No object in the reference bundle is stacked at the chain (the flags ride on `MeshFlags`, which the bundle does not carry) | declared gap |
| 10 | Level `j` drawn while `limits[j] ≤ lod < limits[j−1]`; `lod` from `CalculateLod(d², lodParameters)` with `lodFactor = levelOfDetail · 540 / tan(fov/2)` | `cull_distance(limit)` inverts it at `levelOfDetail 0.7` (the game's High); `limit_px` converts to a Torque detail size | match, with the 1/6-octave quantisation the lod index has (the last band runs 12.25 % past `cull_distance`) — declared in `lod.cull_distance` |
| 11 | `lodCrossFade` (`RenderingSystem.PrepareRendering`): a 255-step dither over ~0.25 s at every LOD change (`m_LodTimer += dt · 1020`), on for every preset but Very Low | BeamNG has no LOD cross-fade for TSStatic (Torque's only fade is the imposter band) | **cannot be ported** — this is the pop the viewer sees at every switch, and it is why §4 aligns the far switch with the fog |
| 12 | `m_ShadowLod` per level: `valueToClamp = max(valueToClamp, x)` with `m_ShadowBias = bias + shadowBias − 1.5·areaTerm − thinTerm` — shadows stop **sooner** than the mesh for sparse/thin meshes | BeamNG: `castShadows` is per object, no distance; the shadow range is `ScatterSky.shadowDistance` alone | declared gap (the level turns `castShadows` off per group, not per distance) |
| 13 | `PreCullingSystem.RelativeCullingJob`: a sub-object is culled at its **owner's** distance | not ported (per-instance ownership vs per-shape thresholds; audit §70) | declared gap, unchanged |
| 14 | The consumer's `lod_detail_scale` multiplies every threshold | game has no such factor | deliberate deviation, declared (`shapes.py` header) |
| 15 | Producer: `LodProperties.m_Bias` read from the render prefab | `mesh.LodBias`, on the part node's extras | match |
| 16 | Producer: each level read from its own render prefab's geometry asset with its own materials | `TryReadGeometryAsset(lodPrefab)` + `AssignMaterials` | match — and it is what shipped the dotted material name of §1 |

**Verdict: the port is exact where the game has a rule, and every place it is not is declared.**
Two things it did not know, found here:

* **The producer's numbering skips a level.** `level = mesh.Lods.Count + 1` after an impostor is
  skipped keeps the numbering dense, so a chain `[mesh, impostor, mesh]` comes out `_LOD1, _LOD2`
  and the consumer's limits walk treats it as a two-level chain. That is the right thing for the
  rule in row 7 (the game's arithmetic never sees the missing level), and it is recorded so nobody
  "fixes" it into sparse numbering.
* **`test_lod_authored.py` tests monotonicity, not the game's numbers.** It should pin one worked
  example against the decompiled arithmetic (a 60 k-index LOD 0 with 15 k / 4 k levels at a
  known box gives limits the game would give). Added below as an item; not blocking.

### 2.4 The one thing that is not validated: Torque's detail size

Everything above converts a game distance into a Torque detail size through
`lod.screen_px`: *the projected height in pixels of the bounding-sphere radius*,
`r · (H/2) / (d · tan(fov/2))`. Torque's own source says something else
(`tsShapeInstance.cpp`, `setDetailFromDistance`, GarageGames development branch):

```
const F32 pixelScale = (viewport.extent.x / viewport.extent.y) * 2;
F32 pixelRadius = (mShape->mRadius / scaledDistance) * state->getWorldToScreenScale().y * pixelScale;
F32 pixelSize   = pixelRadius * smDetailAdjust;
```

with `worldToScreenScale.y = H / (2·tan(fov_v/2))` (`sceneRenderState.cpp`). So the number a
detail's `_a<px>` is compared against is **`2 × aspect ×` our `screen_px`** — 3.56× at 16:9,
4.78× on Naomi's 3440 × 1440. If BeamNG kept this (its engine is closed; the docs only say
"the number in screen pixels that determines the next LOD switch"), then every threshold in
every level we have built fires **3.6–4.8× further away** than the report states:
`lod_detail_scale 10` behaves like ~2.8 (16:9) or ~2.1 (21:9), buildings are drawn to 2–3× the
distance the budget says, and the "in view" table undercounts:

| `cs2_city_fine`, 4 km view, same instrument | as reported (`screen_px`) | if Torque's factor holds, 16:9 | 21:9 (Naomi's) |
| --- | ---: | ---: | ---: |
| building triangles drawn | 1,811,105 | 5,219,163 | 7,190,150 |
| all groups | 3,085,705 | 8,524,541 | 12,257,981 |

(`cs2beamng budget --screen-height 3840 / 6880` — scaling the reference height is the same
thing as scaling the projected size.) This would also explain "buildings are the FPS cost"
surviving every ladder change, and it makes `detailAdjust` — BeamNG's Mesh Quality slider —
the multiplier the user is already turning.

**It needs one in-game measurement and nothing else.** In the free camera, note the distance at
which a named shape vanishes (the editor's object inspector gives the camera-to-object
distance). Predictions for `Alvear_Tower_Buenos_Aires` (`nulldetail8`, radius 144 m,
reference 1080 p / 45°):

| formula | vanishes at |
| --- | ---: |
| `screen_px` (what every report assumes) | ≈ 23.5 km — never, inside a 4 km view |
| Torque's, 16:9 | ≈ 84 km |

Both are past the view distance for a tower, so use a house: `NA_ResidentialLow…` shapes with
`nulldetail` in the 20–40 px range vanish at ~400–700 m under `screen_px` and at 1.5–3 km
under Torque's; the difference is unmistakable from the road. Concretely, `EU_ResidentialLow01_L1_2x2`
(radius 6.1 m, `nulldetail15`) vanishes at **≈ 520 m** under `screen_px` and at **≈ 1,860 m** under
Torque's formula at 16:9. Until it is measured, **every
distance in this document is quoted in `screen_px` terms**, and the fix, if it is needed, is
one constant in `lod.screen_px` / `lodchain.limit_px` / `budget.screen_px` (a declared
16:9 assumption — the aspect is the viewer's, not the level's) with every threshold in the
level moving 3.56× nearer.

---

## 3. The other performance mechanisms, as they stand together

Measured on the 22:42 level unless stated.

| mechanism | state | interaction / defect |
| --- | --- | --- |
| **Authored chain** (1,159 of 1,168 buildings) | switches at the game's limits × `lod_detail_scale`; LOD 1 = 41 % of LOD 0's triangles, LOD 2 = 4.5 % | **Residency, not triangles, is what it costs**: BeamNG mounts a shape whole (every level) once one instance is in view; CS2 streams levels on demand under `BatchMeshSystem.memoryBudget` (2,048 MiB on High). The chain's LOD 1s are 1,747 MiB raw. This is why the a8 P2 remainder (LOD 0 bytes on the top 100 shapes) is a residency lever and not a draw one. |
| **Generated ladder** (props, trees, 9 buildings) | displacement-deferred, one direction; vegetation exempt | sound; unchanged |
| **Runtime imposters** | 190 shapes, all big buildings, band `null_px → null_px/3` | removed for chain shapes (§1); the remaining candidates are non-chain shapes past 3 km, i.e. none on this city |
| **Cull (`nulldetail`)** | game's `m_MinLod` × `lod_detail_scale` (10 on this build) | §2.4 decides what it really is; at scale 10 the median building vanishes at 1,440 m and the median prop at 268 m (`screen_px`; the report's `drawDistance`, against the game's own 13,008 m and 1,024 m) — the pop-in at the horizon is this cull, not the LOD switches |
| **Network / marking / area tiles** | exact subsets by the game's per-primitive cull; roads never cull inside 41 km | at 14 km every one of the 1,024 road tiles is drawn (1.01 M triangles) — as CS2 does; no change needed |
| **Colliders** | 2,000-triangle cap, clustered | unrelated to draw; fine |
| **Forest** | 904,656 items / 1,269 types, cell-culled by the engine (2 km buckets, 200-item quadtree cells), per-item detail by the same pixel rule; `castShadows` on | **the budget instrument does not count forest triangles at all** — vegetation is BeamNG's own #2 slow-scene cause and is invisible to every "in view" table in this phase. Item below. |
| **Materials** | 5,483 (B2 dedupe landed) | 9.4× vanilla; every material on a detail is a draw call. LOD 2 levels should carry ONE material (the game's do carry their own baked atlas — 2,882 textures bound only at LOD 2 — which is exactly the far-LOD atlas §4 wants, already in the bundle) |
| **Textures** | DDS 556 MiB, PNG 235 MiB (terrain) | first-load cook stall only |
| **`LevelInfo`** | `visibleDistance 4000`, `fogDensity 1/meanFreePath`, `nearClip 0.1`, **`decalBias` not set** | `decalBias` "should be tuned to the visibleDistance" (BeamNG docs); the 15,393 navgraph DecalRoads are invisible so it does not show, but a longer view needs it set with the distance |
| **Shadows** | `ScatterSky` defaults; `castShadows` off on markings/areas/skirts | `shadowDistance` / `numSplits` / `logWeight` untouched — the far ring must not cast |

**Instrument gaps** (fix before ranking any further draw-side work — the Phase 0 lesson again):
(1) forest triangles in view are not measured; (2) `screen_px` may be 2·aspect off (§2.4);
(3) `budget` counts a static as drawn without a frustum — it is a 360° figure, which is the
right conservative number for residency and a 4–6× overstatement for one frame.

---

## 4. What it takes to see the whole map

The map is 14.3 km across. CS2 itself draws a 30 m house to ~9 km and a tower to the far edge
(`levelOfDetail 0.7`: a mesh is drawn until its rendering size is 1.43 px tall at 1080p), and
it fades every switch. Our level, at `lod_detail_scale 10`, culls the median building at
1,440 m and the biggest tower at ~6 km, has no fade, and at 4 km draws 3.09 M triangles.

### 4.1 What the instrument says raising the view costs today

`cs2beamng budget` on the 22:42 level, spawn (1404, 1168), all groups:

| view | 4 km | 8 km | 14 km | 14 km, coarsest level ×4 further | 14 km, coarsest level to CS2's own cull (×10) |
| --- | ---: | ---: | ---: | ---: | ---: |
| buildings/props (`cs2`) | 1,811,105 | 1,927,453 | 1,927,453 | 4,836,254 | 5,443,379 |
| networks | 793,090 | 1,008,147 | 1,014,398 | 1,014,398 | 1,014,398 |
| all groups | 3,085,705 | 3,480,918 | 3,487,700 | 6,396,556 | 7,003,681 |

Two readings. **Raising `view_distance` alone buys nothing**: 4 → 14 km adds 0.4 M triangles
because the ladders have already culled everything past ~1 km — the horizon stays empty, which
is the pop-in. **Letting every building's coarsest authored level run to CS2's own cull** puts
the whole city's silhouettes on screen for +3.5 M triangles — CS2 draws more than that — but it
does it with ~15,000 TSStatics each mounting its whole shape (5 GB resident already at 4 km) and
each a draw call per material. That is the wrong shape for the cost, and it is what §4.2 fixes.

### 4.2 The far ring: one merged silhouette mesh per cell (HLOD)

The technique every engine and every modding scene converges on (Unreal HLOD "Merged/Simplified
Mesh" layers with a cell size and a loading range; Skyrim's DynDOLOD `.BTO` meshes per 4×4 /
8×8 / 16×16 cells with objects admitted to a ring by size; Assassin's-Creed-style block
impostors; the BlockMap paper's vertical prisms). For this builder it is:

* **Per cell (256–512 m), one `.dae`** holding every building in the cell as a **silhouette
  proxy**: a footprint prism extruded to bbox height (~10 triangles, `lod_vertcol` colours from
  the albedo mean per face), or the game's coarsest authored level decimated to ≤ 50 triangles
  with its LOD-2 atlas colours baked to vertex colour. **Not the authored LOD 2 as it is**: that
  level is a median 499 triangles (p90 800; 1.21 M over the 1,099 chain meshes above 2,000
  triangles) — fine enough that drawing it everywhere is the +3.5 M of §4.1 — and it is what the
  per-building shape already draws out to its cull. One material per cell.
* **The per-building TSStatic keeps its ladder and its cull; the cell mesh starts where the
  building's `nulldetail` stops** — its own `_a<px>` computed from the *cell's* radius so the two
  hand over at the same distance (`lod.group_detail_px`, which the network tiles already use for
  exactly this conversion), and its own `nulldetail` at the game's cull for the largest member.
  No building is ever drawn twice; no building is ever absent.
* **A second ring for landmarks** (radius > 60 m: the 190 imposter candidates are the list) in
  1–2 km cells so a tower's outline survives to the far shore while a house does not.
* **Cost, estimated from the level:** 8,730 building instances × 10–50 triangles ≈ 0.1–0.4 M
  triangles for the whole map at ~60 cells = 60 draw calls and a few MiB — against +3.5 M
  triangles and 15,000 whole-shape statics for the same silhouettes done per building (§4.1). `castShadows` off; collision
  none (the per-building collider is already there, and out of range anyway).
* **Where it lives:** the bake (`csiibundle.bake`) — a `farcells` product beside `skirts`, since
  both consumers want it and it is a rule with a game counterpart (the game draws LOD 2 to the
  cull; we draw the same LOD 2, merged). The Blender adapter gets it as a collection.
* **Error rule, declared:** the ring switches where the coarsest authored level would have
  been drawn anyway, so its geometric error is the game's own. For prism fallbacks the
  Hausdorff distance to LOD 0 is measured and the switch pushed out until it projects under
  `lod_switch_error_px` (the existing knob) — the same one-direction rule as the generated ladder.

### 4.3 Hide the switches under the fog

There is no cross-fade (row 11), so the far ring's hand-over has to happen below the contrast
threshold. `LevelInfo.fogDensity` is already CS2's `1/meanFreePath` (6.9 km on this map, i.e.
transmittance 0.56 at 4 km — far too little to hide a switch). Set the far-ring hand-over at the
distance where `exp(−βd)` has pulled building-vs-sky contrast under ~5 % — with the game's fog
that is ~20 km, past the map — so either accept a visible hand-over at LOD 2 (it is the game's
own mesh, so the pop is the game's) or ship a `fogDensityOffset` / height fog a little denser
than the game's. **Recommendation:** keep the game's fog, and take the hand-over pop; it is one
switch per building at ~700 m instead of a disappearance.

### 4.4 Lua and custom culling — assessed, declined

Researched: level extensions (`levels/<name>/mainLevel.lua`, `onUpdate`/`onPreRender`),
`scenetree.findClassObjects("TSStatic")` + `obj:setHidden`, `Prefab loadMode="Manual"`,
`Zone`/`Portal`/`OcclusionVolume`, `$pref::TS::detailAdjust` / `skipRenderDLs` /
`smallestVisiblePixelSize`.

* **Distance culling in Lua duplicates what Torque already does per shape at C++ speed**
  (`setDetailFromDistance` on every TSStatic, forest cells against the frustum). Hiding
  thousands of objects per frame from Lua is a binding call each, with no published numbers,
  and `setHidden` leaves collision resident anyway. A rule with no counterpart in the game —
  CS2 has no occlusion culling and its distance cull is the mesh rule we already port.
* **Occlusion**: BeamNG documents no `Zone`/`Portal`/`OcclusionVolume`; the forum record is
  shadows and terrain vanishing inside zones and forest not occluded. Terrain occlusion
  (`$Scene::disableTerrainOcclusion` off) is the one that is on already and free.
* **`Prefab loadMode="Manual"`** is the only real streaming primitive; worth it only if
  residency still overflows after the far ring, since the ring is what lets the per-building
  shapes drop out of view (and out of memory) past their cull.
* **`$pref::TS::detailAdjust`** is the user's Mesh Quality slider; we should not set it from the
  level. The level's job is to make thresholds mean what they say (§2.4).

### 4.5 The rest, ranked

1. **§2.4 — measure Torque's detail size in-game** (one number, ten minutes). Everything else
   is sized wrong by 3.6–4.8× until it is known.
2. **Forest triangles into the budget instrument** (`budget.measure`: walk `forest/*.forest4.json`
   per type against the type's ladder in `shape_ladders.json`). Vegetation is unmeasured and it
   is BeamNG's own second-worst case.
3. **The far ring** (§4.2) — the change that makes the map visible from the other side.
4. **`view_distance` with `decalBias`, `fogAtmosphereHeight`, `ScatterSky.shadowDistance`,
   `fadeStartDistance` set together** in the `scene` stage once the ring exists: 8 km for
   `high`, 14 km for `ultra`; the ring makes the far half cost ~1 M triangles.
5. **One material per LOD 2 level** in the shape stage where the game's level binds several
   (draw calls); pack the LOD-2-only atlases (2,882 textures at the 64 px cap) per cell.
6. **`castShadows` by size**: BeamNG's guidance and the game's own `m_ShadowLod` both stop small
   things' shadows first; TSStatics under ~3 m radius and the far ring cast none.
7. **A worked-example test for `authored_lod_limits`** against the decompiled arithmetic.

---

## 5. Sources consulted (beyond the decompile)

Torque3D source (GarageGames development branch): `ts/tsShapeInstance.cpp`,
`scene/sceneRenderState.{h,cpp}`, `ts/tsLastDetail.cpp`, `ts/loader/tsShapeLoader.cpp`,
`forest/forestDataFile.h`, `forest/forestCell.h`, `T3D/tsStatic.cpp`, `T3D/occlusionVolume.cpp`.
BeamNG documentation: TSStatic, LevelInfo, Forest (object and format), TerrainBlock, Prefab,
SimGroup, level_creation §5/§8/§11/§12, texture streaming, extensions; forum threads on
`detailAdjust`, zones/occlusion, show/hide from Lua, `mainLevel.lua`. Unreal HLOD (overview and
World Partition), Simplygon HLOD/aggregation, DynDOLOD (Object LOD, Performance, Terminology),
MSFS scenery LOD rules, Nanite (Karis 2021), BlockMap (Cignoni et al.). Full URL lists are in
the two research transcripts of this session; the claims used here are the ones quoted above.

---

## 6. Built 2026-08-30 — the ranked list, and the stadium

Everything in §4.5 that did not need the in-game measurement, plus the stadium report, in one
change set (awaiting the rebuild and Naomi's look):

* **The far level (§4.2, revised).** Not a merged cell: BeamNG loads every shape at level load
  whatever is in view, so a merged ring saved no residency, only draw calls — and a merged cell
  cannot hand over per building without drawing the proxy inside the still-drawn mesh. Instead
  **each building shape gets one more detail level**: a closed voxel silhouette of its coarsest
  level (`csiibundle.silhouette` — occupancy sampled from the triangles, filled from the outside
  with the ground sealed, exposed faces greedily merged, coloured per face by the material that
  put the voxel there), on BeamNG's untextured `lod_vertcol`, starting exactly where the
  shape's own null was and running to **CS2's own cull** × `far_detail_scale` (default 1). The
  hand-over is per shape and exact, no building is drawn twice, and holes cannot exist (the
  lattice problem of every decimator). A box is 12 triangles; the Alvear tower 82, the airport
  20, a 2×2 house 52. Buildings only — CS2 culls a prop at ~1 km itself.
* **`view_distance`** default and `high` 8,000 m, `ultra` 14,000, `performance` 6,000; `LevelInfo.decalBias`
  scales with it (declared approximation from the documented 0.0005 @ 2,000 m pair).
* **Forest triangles are in the budget instrument** (`cs2beamng budget`: per item, the type's
  ladder at `radius × scale`, the same pixel rule Torque's `Forest` applies).
* **`castShadows: false` on TSStatics under `shadow_min_radius`** (3 m): CS2's per-level
  shadow LOD stops small things' shadows first; BeamNG has only the switch.
* **The worked example** in `tools/test_lod_authored.py`: one chain computed by hand from the
  decompiled arithmetic (`[138, 132, 100]`), pinned.

**The baseball ground's missing scaffolding / light platforms** (Naomi, 2026-08-30) is the
rare-forest-type rule, not the rip: `Baseball_Field__2_revised2` is a custom asset whose
building mesh is a 12-triangle field plane, and everything standing on it is a separate prop —
`OracleParkSportLightBig` (1 placed), `…Small` (2), `OracleParkScreen` (1), `OracleParkSculpture`
(1) — all in the bundle, all uniform-scale props, all forest types with fewer than 10 members,
all dropped by `forest_rare_type_min`. So were 245 other large one-offs (695 items): three
harbour cranes (117 m radius), the marina piers, the 57 m pylons, the container piles, the
yachts. **Fixed by `forest_rare_keep_radius`** (default 5 m): a rare type whose shape radius is
at least that is kept; the small tail (1,576 types) still goes. Counted in the report as
`forestRareTypesDropped.keptLarge`.

Not done: §2.4's in-game measurement (only Naomi can take it).

**Then the forest, once the instrument could see it** (the first 2026-08-30 rebuild, 8 km view):
forest **9,197,185** triangles in view over 892,042 items — against buildings 1.93 M, roads 1.6 M,
areas 0.68 M. The game's own rule for a tree past LOD 0 is a **card** (every vanilla tree's
authored LOD 1 is `MeshFlags.Impostor`, the level the producer skips), and BeamNG's
`bb_autobillboard` is the same mechanism, batched per forest cell. So every vegetation shape's
coarsest generated level is now the engine billboard at the same switch size
(`shapewrite._vegetation_imposter`): `OakTree01` draws 11,295 triangles to 35 m, 3,013 to 141 m,
a card to 870 m. 292 shapes get an atlas — vanilla's 82 are all vegetation too — and buildings
keep none (§1). The budget instrument counts a billboard as no triangles (it is two, batched);
the forest's remaining cost is its LOD 1 inside 141 m.

**The rebuild, 2026-08-30 01:2x** (`docs/baselines/*-after-phase10-far.*`; 11 m 05 s, all 4,275
object shapes rewritten and cooked): 1,170 building shapes carry a far silhouette, 244 vegetation
shapes a billboard, 284 large rare forest types kept (773 items; the stadium's three floodlight
types among them), 6,973 small statics without shadows, `visibleDistance 8000`. Validator: the
one residency advisory (5,071 MiB). At 8 km from spawn, `screen_px` terms:

| | first 2026-08-30 build | **this build** |
| --- | ---: | ---: |
| forest | 9,197,185 | **1,381,661** |
| buildings/props (`cs2`) | 1,927,453 | 1,938,355 (+ the silhouettes) |
| networks / areas / skirts / markings | 1.60 M / 0.68 M / 0.14 M / 0.11 M | unchanged |
| **all groups** | **13,670,302** | **5,865,680** |

Built with the far null ROUNDED (`round`); the committed code rounds it UP (`ceil`, never past
the game's cull) after `test_a3_objects` caught a house drawn 24 % past CS2's distance — the
level on disk draws silhouettes up to one pixel-size further than the next build will.
**Awaiting Naomi's look.**

## 7. Residency, 2026-08-30 — the constraint was never triangles

Naomi's load of the 01:03 build: **8 fps wherever the camera pointed**, unmoved by
`theForest.hidden`, `detailAdjust 0.1` or `visibleDistance 4000`; Task Manager: dedicated GPU
memory 7.8 / 8 GB, **shared GPU memory 6.9 GB**, GPU utilisation 16 %. The card was paging.
5,071 MiB of level files had become 14.2 GB of GPU memory (a ~2.8× expansion: Torque's vertex
buffers, per-instance data, collision, imposters, shadow maps, the 3440×1440 targets, the
engine's own baseline). BeamNG loads every level of every shape at level start and streams
nothing, so the LOD chain that cut drawn triangles ADDED bytes. Vanilla's biggest level is
1,375 MiB of files.

Cut, in two builds (`docs/baselines/*-after-phase10-residency.*`):

| | files MiB | what |
| --- | ---: | --- |
| 01:03 build | 5,056 | the far levels + billboards on the chain level |
| `authored_lod1_drop`, textures 256/128 | 4,120 | the game's LOD 1 left out where a LOD 2 exists (LOD 2 takes its band; collider still from LOD 1); −767 object, −197 texture |
| `area_conform_cm 5`, `skirts off`, forest 60/70 | **3,625** | areas 380 → 84; skirts 166 → 0; forest 905k → 752k items; validator 0 problems |

Dropped without building: terrain textures as DDS — vanilla ships its terrain textures as PNG
too (italy, small_island, west_coast_usa), so the terrain packer handles them natively and the
conversion buys nothing. Still resident at 3,625: object meshes 1,983 (the top 100 custom
assets' LOD 0 dominate), road tiles 787 (the game's exact cross-sections, never culled).

**Streaming (§4.4 revisited).** Everything a distance-streamed near ring needs is in the game
and used by its own Lua: `Prefab` with `loadMode "Manual"`, `prefab:load()`/`:unload()`
/`:isLoaded()`, `be:reloadCollision()` once per batch (collision is NOT rebuilt on load),
`levels/<level>/mainLevel.lua` with `onUpdate`, `core_camera.getPosition()`,
`Engine.Render.getMemoryInfo()`. The `.prefab.json` is the items.level.json dialect with a root
`SimGroup` and prefab-relative positions. Nobody has built it; the one unknown that decides
whether it is worth building is whether unloading frees the shapes' GPU memory (Torque
refcounts `Resource<TSShape>`; BeamNG may cache). The experiment is on the level as built:
delete every `km_x_y` SimGroup past 2 km of the camera and read `getMemoryInfo` before and
after.

**The streaming experiment, run 2026-08-30 03:xx — streaming is out.** On the 3,625 MiB level
(`Engine.Render.getMemoryInfo`: usage 6.17 GB of a 6.88 GB budget with Texture Quality Low,
i.e. ~1.7× the files), Naomi deleted the `km_x_y` object SimGroups further than 2 km from the
camera — 87, then 8 more from elsewhere: 95 of 102 cells, the city visibly gone — and usage read
6.10 → 6.20 → 6.17 GB over the next three minutes. `purgeResources` does not exist in BeamNG.
**Once a shape is loaded its GPU memory is never released**; unloading prefabs would free
instance data and draw calls, not residency. The lever is the level's file bytes (×~1.7 on the
card) and, when a city does not fit, a region build. Recorded so nobody builds the prefab layer.

