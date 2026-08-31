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


## 8. Residency, 2026-08-30 (second pass) — what the bytes are, and the levers that reach them

Research session after §7's 3,625 MiB level read 6.17 GB of a 6.88 GB GPU budget with Texture
Quality Low (`beamng.log`, 0.39.4, D3D12). Measured on the built level, the bundle, and Torque's
source; everything below is a number, not a guess.

### 8.1 Where the 3,625 MiB is

| | MiB | what it is |
| --- | ---: | --- |
| `art/shapes/cs2` | 1,983 | 4,031 shapes, **20.2 M top-level triangles at ~100 B each**; LOD 2 4 %, colmesh 2.4 M tris; LOD 1 already dropped |
| networks | 787 | 1,024 tiles, 9.3 M LOD-0 triangles at 63 B (three levels summed), the game's own cross-sections |
| markings / areas / terraincut | 218 | |
| textures | 402 dds + 235 png | at Low, /4 |

The top 120 custom assets in the bundle are 9.0 M LOD-0 triangles / 1,197 MiB of source.
`Kinsman_Plaza.cdae` is 37.7 MB for 176,587 triangles because its authored LOD 1 is a
**byte-identical copy of LOD 0** — 12 such levels on the city (also
`EU_ResidentialHighSignature03`), ~30 MiB in all.

**Welding is not a lever.** Objects: 14.8 M vertices for 9.0 M triangles over the top 120
meshes, of which 14.2 M are unique in (position, normal, uv) — 4 %. Network tiles: 5 %. CS2's
meshes are split on purpose.

### 8.2 What the card does with a file (Torque3D source, BeamNG docs)

* `TSMesh::__TSMeshVertexBase` is **48 bytes** (position, tangentW, normal, tangent, uv), full
  float, no packing; +4 B with vertex colour, +8 B with a second UV. Indices 16-bit on the GPU.
  One vertex buffer per shape spanning **every** detail level; the CPU copy is kept. So the
  ~1.7× file→VRAM ratio is arithmetic, `detailAdjust` (Mesh Quality) frees nothing, and a far
  level ADDS to the buffer.
* **BeamNG streams texture mips** — `documentation.beamng.com/modding/materials/texture_streaming`:
  0.37 on Vulkan, **0.39 on D3D12**, never D3D11; evicts unused high mips above ~50 % of the
  driver's budget, stops loading near ~90 %. The `getMemoryInfo` "budget" is that budget. So
  textures self-regulate and the meshes are what squeezes them — a higher Texture Quality
  needs mesh bytes gone, not texture bytes.
* Shadow Quality scales the PSSM texture 0.25/0.5/1/2/4 (Lowest→Ultra): ~16/64/256 MiB.
* The TSStatic page: `useInstanceRenderData` "should be used only when needed … for normal level
  props and buildings, it should be left false"; a static without it "can be rendered through
  the static shape manager", which batches repeated shapes. The level set it on all 16,428.
* Known Issues: "some levels use almost 4 GB VRAM to load just non-streamable data".
* **§7's experiment tested unloading, not never-loading.** `TSStatic::_createShape()` runs in
  `onAdd()`; a `Prefab` with `loadMode "Manual"` adds nothing until `:load()`. Untested, and
  the one thing that could take the whole map's bytes off the card at once.

### 8.3 Built (shapes v17, instances v12, derived v6, networks v6, terrain v3)

* **`authored_lod0_max_tris`** (`shapelods.authored_chain`): a shape whose LOD 0 is at or over
  N triangles ships the game's LOD 1 as its nearest level and leaves LOD 0 out; LOD 1 is then
  kept even under `authored_lod1_drop`, the collider still comes off it. Sized on the bundle:
  N = 20,000 → 189 shapes in the level, −6.28 M triangles ≈ **−599 MiB** of files; 50,000 → 63
  / −378; 5,000 → 591 / −854. Presets: performance and medium 20,000, low 5,000. Declared and
  counted in the report (`lod0Dropped`, `lod0DroppedTriangles`).
* An authored level **byte-identical to LOD 0** (sha1 of indices + positions after the same
  merge) is left out; LOD 0 keeps its band (`identicalLevelsDropped`).
* `useInstanceRenderData` only on a TSStatic that carries an `instanceColor`
  (`level.tsstatic(instanced=)`); road, paint, marking, area tiles ship it false.
* Terrain stage bumped to v3 so the one-file-per-image writer of 095455b actually runs — the
  level built after it still carried **57.2 MiB** of byte-identical tier PNGs (27 groups).
* `tools/beamng/cellprefab.py`: rewrites every `Objects/km_x_y` cell beyond a radius of the
  spawn into `prefabs/<cell>.prefab.json` with a `loadMode "Manual"` `Prefab` in its place
  (backup beside; `--restore`). The never-loading experiment, on the level as built.

Not built, with the reason: **BC1 for opaque colour** (−76 MiB of 152) would thread a format
through `partition`/`export_all`/the ledger key and into the spawned texture workers, for an
off-by-default option on the half that streams — dropped. **Decimating** the no-chain custom
assets and the road LOD 0 needs an attribute-aware simplifier with a position weld first
(cluster decimation made lattices); an offline experiment on `Kinsman_Plaza` and one net tile,
not a build.

### 8.4 The experiments Naomi runs (one session, no rebuild needed for E1/E2)

1. **E1 — never-loading.** `python tools/beamng/cellprefab.py <level_dir> --keep-radius 2000`,
   load, `dump(Engine.Render.getMemoryInfo())`, then
   `for _, n in ipairs(scenetree.findClassObjects('Prefab')) do scenetree.findObject(n):load() end`,
   `be:reloadCollision()`, read again. A jump on `load()` = lazy loading is real and a
   `stream_radius_m` cell layer (record §7's design, never unloading) is worth building.
   `--restore` afterwards.
2. **E2 — the settings delta.** Texture Quality Normal, Shadows High, dynamic reflections on;
   `getMemoryInfo`. That is the number the build must clear.
3. **E3** — FPS at the spawn on the rebuilt level (instance data off on 15,000 statics).

### 8.5 The rebuild — numbers

Rebuilt 2026-08-30 03:5x with `authored_lod0_max_tris 20000` on the §7 settings
(`docs/baselines/*-after-phase10-lod0drop.*`), validator 0 problems:

| | §7 build | this build | |
| --- | ---: | ---: | --- |
| meshes cs2 | 1,983 | **1,505** | 192 shapes ship the game's LOD 1 as their nearest level (10,352,647 LOD 0 triangles left out); 1 identical authored level dropped (the other copies sat on shapes A already reached) |
| networks / markings / areas / cut | 1,005 | 1,005 | untouched |
| textures .dds | 402 | 402 | |
| textures .png (terrain) | 235 | **156** | 55 orphaned per-tier copies swept (79 MiB); 53 files, one per distinct image |
| **RESIDENT TOTAL** | **3,625** | **3,069** | −556 MiB of files ≈ −0.9 GB on the card |

Top 100 shapes 862 → 568 MiB; `Kinsman_Plaza` 36.0 → 18.1 MiB. Triangles in view unchanged
(1.93 → 1.95 M cs2 at 8 km — LOD 1 draws at every distance for those 192). At 5,000 the
setting reaches ~590 shapes for roughly another 250 MiB; that is the `low` preset's value.

**Awaiting Naomi:** the look at the 192 buildings up close; E1 (`cellprefab.py`, §8.4) and E2's
`getMemoryInfo` at Normal textures / High shadows on this build. The 6.17 GB of §7 should read
~5.2–5.3 GB at Low before the settings go up.

### 8.6 E1 run 2026-08-30 — never-loading WORKS, and it is not where the bytes are

On the 3,069 MiB build with 87 cells beyond 2 km of the spawn as `loadMode "Manual"` prefabs
(`cellprefab.py`), Texture Quality Low: `usageBytes` **5.02 GB** at spawn, **5.77 GB** after
`prefab:load()` on every cell (`budgetBytes` 6.29 → 6.77). The far cells' shapes were not on the
card until asked for — §7's verdict covered unloading only. But the near-ring level is 5.02 GB
with 85 % of the city absent: the FIXED part (road tiles, markings/areas, forest types, the
8192² terrain, textures, the engine's own targets at 3440×1440) is ~5 GB, and object streaming
reaches 0.75. Next reading: a tiny vanilla level at the target settings, to split the engine's
baseline from the level's fixed part.

### 8.7 E2 and the baseline — the level costs ~3.5 GB at the target settings

Same day, separate sessions, settings changed from the main menu only (Mesh Normal, Terrain
Normal, **Texture Normal, Lighting Ultra, Shadows High**):

| | usage | budget | |
| --- | ---: | ---: | --- |
| `gridmap_v2` | 2.89 GB | 7.60 GB | engine + a small level — the baseline is ~2–2.5 GB at 3440×1440 |
| `cs2_city_fine` (3,069 MiB build) | **5.98 GB** | 6.78 GB | 30–40 fps, dips to 10; **88 % of budget**, where texture streaming stops loading (docs: ~90 %) — the dips |

So the level is ~3.5 GB on the card at these settings and needs ~1 GB more off to sit near
70 % with streaming steady. Buildings can give ~0.75 of that if the far cells never load
(§8.6); the rest is the fixed part — road tiles first (787 MiB of files ≈ 1.3 GB), forest
types (~0.5), markings/areas (~0.35), terrain. Also: 5.98 → 2.89 on the level switch, so
memory IS given back between levels on 0.39.4 — the "restart the game" lore does not hold.

**Phase 3, built (Naomi chose objects + tiles, 2.5 km):** `stream_radius_m` (0 = off;
performance preset 2,500). Every placing stage buckets by `level.cell_of` — `Objects/km_x_y`,
`Networks/networks_km_x_y`, `Markings/markings_km_x_y`, areas, skirts, lane furniture;
`terraincut` stays resident (a hole with nothing over it is a fall). `Level.stream_group`
writes a cell as `prefabs/<cell>.prefab.json` (root SimGroup, zero `groupPosition`, world
positions — vanilla's own dialect) with a `loadMode "Manual"` `Prefab` in its place;
`Level.write_main_lua` writes `mainLevel.lua`: each frame the nearest unloaded cell whose
square is within the radius of `core_camera.getPosition()` is `load()`ed (one per frame),
`be:reloadCollision()` at most every 5 s after a load, **nothing is ever unloaded** (§7).
`read_objects` reads the prefabs too, so the validator, the budget's in-view count and the
POI stage see the same objects as before. Versions: instances 13, derived 7, networks 7,
scene 5.

What it costs, declared: a road or building beyond 2.5 km is not drawn until approached
(today a building is culled at ~1.4 km anyway; a road was drawn to the horizon); a cell load
is a stall proportional to the cell, and the collision rebuild ~2 s — both while driving into
a new district. The far ring (§4.2) is what puts outlines back.

### 8.8 Cell streaming, built and withdrawn the same night — the limit is the main thread

Built as §8.7 describes and loaded three times (2026-08-30 late):

| build | what happened |
| --- | --- |
| 1 | the cell's root `SimGroup` shared the `Prefab`'s name → "did not create root SimGroup", cells parentless; and the cook had been skipped with the game open, so every tile was `Importing COLLADA` at runtime — a minute at 1 fps at spawn |
| 2 (cooked, `_root`) | spawn **5.07 GB** (5.98 resident), no errors; then a **4.5–5 s freeze every 30 s** while entering new ground — every one a `be:reloadCollision` + `Loaded Static Collision` (1.3 + 3.2 s, growing with what is loaded) |
| 3 (no rebuild) | **the car fell through a streamed bridge**; one manual `be:reloadCollision()` fixed it. So a prefab's `load()` does NOT register collision, and BeamNG rebuilds the whole static collision (2 GB, 45–51 M triangles) or nothing. And dense downtown cells stutter on load regardless — the load is a main-thread add |

Naomi's verdict: a no-go. Stalls interrupt play; smaller stalls are still stalls. Memory-wise it
worked exactly as §8.6 predicted (5.07 → 5.48 GB over a drive, against 5.98 resident), and
that is the record of what the mechanism is worth. **Withdrawn**: `stream_radius_m`,
`stream_collision_s`, `Level.stream_group`, the loader, the cell buckets on tiles — all out
(instances v15, derived v9, networks v9, scene v8); `level.cell_of` stays as the object
group naming; `tools/beamng/cellprefab.py` stays as the experiment. What could change the
verdict is an engine change (off-thread shape loading, incremental collision), not a builder
one.

**Where that leaves residency:** 3,069 MiB of files = 5.98 GB at Normal textures / Ultra
lighting / High shadows on the 8 GB card (88 % of the streaming budget: playable at 30–40 fps
with dips). The levers left are all "less city": `authored_lod0_max_tris 5000` (~−250 MiB),
`forest_rare_type_min` up (types mount, items do not), Shadows Normal instead of High
(−48 MiB) — and the region build, which is the one that fits with room at these settings.

### 8.9 Experiment G (decimating LOD 0) — closed without a lever; the renderer check; where it ends

`fast_simplification` (Fast-Quadric, position-only) after an exact position weld, on
`Kinsman_Plaza` (176,587 tris), `EU_ResidentialHighSignature03` (77,772) and the heaviest road
segment (128,114), 2026-08-30: the collapse stalls at 67–86 % of the triangles on the two
buildings whatever the target, and the surface it leaves is metres off the original (p95
two-sided distance 0.4–2.3 m even at a 14 % cut) — it collapses across the unwelded seams, the
same lattice failure the cluster decimator had. The metric (nearest-centroid + exact
point-triangle) also overstates on large triangles, so the numbers are an upper bound, but the
Blender-free reading is enough: a position-only quadric cannot be bounded to centimetres on
these meshes. An error-bounded, attribute-aware simplifier (meshoptimizer's `simplify` with
`target_error`) needs a C++ toolchain here (no wheel for this Python); **deferred**, library
uninstalled, nothing in the build depends on it.

Built instead: the validator reads the renderer out of `beamng.log` (`machine.graphics_api`)
and notes Direct3D 11 — no texture streaming there, and 0.39.1 falls back to it on small
cards; `resident.graphicsApi` / `textureStreaming` in the report; a README section.

**State at the end of the session (uncommitted, awaiting the one in-game validation):**
`cs2_city_fine` fully resident at **3,069 MiB** (from 3,625 at the session's start; 192
shapes start at the game's LOD 1, terrain orphans swept, instance data only on coloured
statics), validator 0 problems; measured 5.98 GB at Normal/Ultra/High, 30–40 fps. Streaming
withdrawn (§8.8). The levers that remain are all "less city": `authored_lod0_max_tris 5000`
(~−250 MiB), fewer forest types, Shadows Normal — or a `--region` for the district in play.

### 8.10 The draw-distance items closed out (2026-08-30, late)

§4.5's list, item by item:

| item | outcome |
| --- | --- |
| §2.4 Torque's detail size | still Naomi's one measurement; nothing here depends on it being either value |
| forest triangles in the budget | done earlier the same day |
| **the far ring** (§4.2) | **built**: `stages/farring.py`, `far_ring_metres` (default 2,500 m). Every building's silhouette (already computed per shape by `_far_level`) is carried in the shape result (`lods.farRing`), placed by the instances stage with the building's transform, merged per 512 m cell into one `lod_vertcol` shape (`art/shapes/farring/farring_<cx>_<cy>.dae`, `TSStatic` at the cell origin, no collision, no shadows). The hand-over is ONE distance for the level: a building whose detailed levels stop nearer than D keeps a per-building silhouette only up to D and is in the ring beyond; one that reaches past D (the towers) keeps its own silhouette to the game's cull and is not — a shape's thresholds are per shape and the shape stands in many cells, so a per-cell hand-over is not expressible. Cell start `group_detail_px(cell radius, D)`, null at the largest member's game cull × `far_detail_scale`, rounded up. Nothing drawn twice, nothing absent; the step from authored coarsest level to 1 m voxel hull now happens at D for everything (declared). Instances v16, shapes v19. |
| `decalBias` / fog / shadow distance with the view | already there since scene v4 (`decalBias` and `fogAtmosphereHeight` scale with `view_distance`); vanilla sets no `shadowDistance` and neither do we — closed |
| one material per LOD 2 | measured on the bundle: 1,528 of 1,615 LOD 2 levels already bind ONE material; the 87 that bind 2–4 would lose their texturing under a merge — not done, on purpose |
| `castShadows` by size | done (`shadow_min_radius`) |
| worked-example test for `authored_lod_limits` | `tools/test_a3_rules.py::test_authored_lod_limits_worked_example` — a 24 × 12 × 18 m house with a 6,000- and an 800-triangle level gives limits [142, 133, 103]: LOD 0 to 181 m, LOD 1 to 512 m, LOD 2 to 16 km; the coarsest level's limit is the mesh's own `m_MinLod`; a 0.5 bias lifts every limit by three. Plus the ring's two-box case. |

### 8.9 Locating the loader and the collision builder in the engine binary (step 2 of the DLL route)

Naomi asked whether an in-process C++ DLL (loaded ReShade-style as a proxy `dxgi.dll`; the
community's Volumetric Smoke mod is one) could make shape loading asynchronous. The gated first
step — *find* the functions without changing anything — ran 2026-08-30 with
`tools/beamng/locate_engine.py` (pefile only, no disassembler) on build 20972 (0.39.x,
`BeamNG.drive.x64.exe` 46 MB, 107,156 functions in `.pdata`, 12,762 RTTI class names, PDB
`c:\symbols\BeamNG.drive.x64.pdb` not shipped). Took an hour, not the week estimated: RTTI gives
every vtable, asserts carry `D:\jenkins\workspace\gameengine\...\*.cpp` paths, and the log
strings are `{}`-format literals. All anchors are strings, so the script survives updates.

| What | Where (VA, build 20972) | Evidence |
|---|---|---|
| `TSStatic` vtable | `0x141f6f3a0`, 78 slots, 17 override `SceneObject` | RTTI `.?AVTSStatic@engine@@` |
| `TSStatic::onAdd` | slot 16, `0x1409f6510`, 435 B | refs `engine::TSStatic::onAdd`, `TSStatic::onAdd() - Shape creation failed!`, `usePolysoup`, `scene\objects\staticMesh…cpp` |
| generic `Resource<TSShape>` loader wrapper | `0x1401bdcd0`, 673 B, **15 callers** (TSStatic, vehicles, GroundCover, …) | 2 hops below `onAdd` |
| `Resource2<TSShape>::create` (the parser) | `0x1402fc3a0`–`0x1402fe6c4`, 9 KB, monolithic | refs `cdae`/`gltf`/`Failed to load Collada shape (assimp)`, `Failed to find TSShapeConstructor for`, **`Importing CS script`, `exec("`** |
| bulk "Mesh Cache" loader | `0x1401d04a0`, 4.6 KB | `Mesh Cache rebuilt summary: {} of {} files freshly loaded`, `{} of {} meshes loaded -> {:0.2f} %` |
| static collision rebuild | `0x14019b3a0`–`0x14019ba98` | `Physics collision reloading...`, `generateCollision` (PhysicsPlugin), `world triangles: {}` |
| its callers | `PrefabV2::onFileChanged` (`0x1409c9c60`) and the Lua binding table fn `0x14055e8e0` (`reloadCollision`) | only two |
| `Prefab::load` Lua entry / `Prefab::_loadFile` | `0x1409c7740` / `0x1409c1660` | log strings |

What the map says about step 3 (making it async), before any code:

* The parser is one 9 KB function that, besides reading `.cdae`, resolves `TSShapeConstructor`
  scripts and **executes console scripts** (`exec(`, `Importing CS/DSO script`). A loader that
  calls into the script VM is main-thread by construction; detouring it onto a worker means
  re-implementing the cdae path *without* those side effects, blind.
* The loader wrapper is shared by 15 callers — vehicles, ground cover, everything. Any hook is
  global, not "level statics only".
* The collision builder has no incremental entry: the only two callers rebuild the world
  (`world triangles`/`world vertices` are whole-scene counters). Nothing to hook; it would have
  to be replaced.
* The engine already has a bulk shape pre-loader with a progress counter (the "Mesh Cache" path).
  Whether it runs threaded is unknown — but it points at the one cheap experiment left.

**The decisive Lua-only measurement (no DLL):** shapes never unload (§7), so a cell's second
`Prefab:load()` after an `:unload()` is a resource-cache hit — the parse is gone and only object
creation, scene insertion and material binding remain. If the second load stalls as long as the
first, the parse is not the cost and no async loader, hooked or native, would remove the stall.
If it is far shorter, the parse is the cost and the remaining question is the collision rebuild,
which the map shows has no incremental path. Either way that reading closes the DLL question
for a tenth of the effort of step 3; take it before writing a proxy DLL.

### 8.11 The first look at the ring (Naomi, 2026-08-30 late) — three fixes

1. **Boxes drawn together with the meshes.** A ring cell had ONE detail, and Torque draws a
   shape's largest detail for any pixel size above its threshold — so inside the hand-over the
   cell was drawn on top of the real buildings. Each cell now carries an empty near detail (a
   1 mm triangle 500 m underground) one pixel above the ring's start; the ring draws only beyond.
2. **Boxes up close on the small houses.** A house's detailed levels end at CS2's cull ÷
   `lod_detail_scale` — a few hundred metres at scale 10 — and the per-building box ran from
   there to the hand-over. A ring member now ships NO box: its coarsest authored level runs out
   to the hand-over (its null moves to D) and the ring takes over there. The band is drawn
   triangles (the game's LOD 2, median 499), not a box. Shapes v20.
3. **The rare-type drop is gone** (`forest_rare_type_min`, `forest_rare_keep_radius`,
   `drop_rare_types`): it left prop families half-built — the rarer members of a set missing
   from every instance of it — for 0.5 % of the forest. Every placed type ships. Instances v17.
4. **And the ring's own sizes were inverted** (caught by the instrument: 117 ring triangles in
   an 8 km view). A Torque detail's size is where it STARTS as the camera approaches, so the
   ring level's size is its far end (the cull) and the empty near detail's the hand-over.
   Right way round: 167,859 ring triangles in view at 8 km, 179 draw calls. Instances v18.
   Level: 3,103 MiB, 2,860 forest types (every placed type), validator 0 problems.

**The reading (2026-08-30, `tools/beamng/stall_probe.lua`, keep-radius 500, three largest
never-loaded cells, `os.clock` on the main thread):**

| cell | TSStatics | unique shapes | first `load()` | `unload()` | second `load()` | GPU Δ |
|---|---|---|---|---|---|---|
| km_1_0 | 957 | 136 | **2,906 ms** | 2 ms | **151 ms** | +81 MB |
| km_2_1 | 805 | 268 | **4,445 ms** | 3 ms | **195 ms** | +240 MB |
| km_1_-1 | 806 | 104 | **1,409 ms** | 4 ms | **133 ms** | +246 MB |

Then `be:reloadCollision()`: **1,370 ms** synchronous (313,003 instances, 5,170 unique meshes,
1,495 MB), followed by `staticCollision … Loaded Static Collision in 2.657s` (43.5 M tris,
1,783 MB) logged *after* the Lua call returned — that part runs off the main thread.

* The parse is **93–96 %** of the stall. Per unique shape it is **14–21 ms** (2,906/136,
  4,445/268, 1,409/104) — under one 30 fps frame. Object creation is ~0.15–0.25 ms per TSStatic.
* So an async loader is not what the parse needs: **Lua can amortise it below a frame** — mount
  each of an approaching cell's unique shapes one per frame (a hidden TSStatic, or a per-shape
  micro-prefab) from 2–3 km out, and the later cell `load()` is the 130–200 ms creation cost,
  itself divisible by smaller cells. No DLL, no engine change.
* The collision rebuild is the residue and the only true freeze: 1.37 s blocking per rebuild,
  no incremental entry in the binary (§8.9 table), and a DLL cannot add one. With a 3 km
  lookahead it fires once per ~3 km driven. Whether one 1.4 s freeze every few minutes of
  driving is acceptable is Naomi's call; the §8.8 verdict said no to stalls.

**DLL route: closed on the evidence.** The parse does not need it; the collision cannot use it.

### 8.12 The second look (Naomi, 2026-08-30) — the switch points were ours, not the game's

Three symptoms, three rules of ours:

| symptom | rule | fix (shapes v21) |
| --- | --- | --- |
| trees full at 20 m, bare after, full again at 100–200 m | LOD 0 → the bake's clustered LOD 1 (a canopy comes out as sticks) → the billboard (a picture of the full tree) | a tree ships LOD 0 and hands straight to the billboard where the generated level would have started — the game's own trees do exactly this (their LOD 1 is an impostor card) |
| building LOD "way too close", inconsistent between buildings, every switch visible | `lod_detail_scale` (10) multiplied the CHAIN switch points, not only the cull: a house the game keeps at LOD 0 to 181 m lost it at 18 m; no-chain buildings switched by the displacement rule instead | chain switch points are the game's limits, UNSCALED; `lod_detail_scale` moves only a chainless shape's null; this build at scale 1 for everything (props included) |
| gaps in a few buildings | generated (clustered) levels on buildings: the 66 LOD-0 promotions and the chainless ladders are the lattices | generated levels for PROPS only; a building is LOD 0 / its chain + the ring |

The first build of that (shapes v21) failed its own budget twice, and the instrument caught
both before a load: **1.65 billion** forest triangles in view (a tree with no generated level
had nothing for the billboard to take its switch size from, so LOD 0 ran to the cull) and **no
ring at all** with 9.1 M building triangles at 8 km (at the game's own switch points a house's
LOD 2 reaches 16 km, so "detail reaching past D" exempted every building). Shapes v22: a
tree's generated levels are loaded for their sizes only and the billboard replaces them all;
every building is a ring member, towers included.

### 8.13 The LOD audit — `tools/beamng/lod_audit.py`

"Validate everything in the LOD implementation is correct, this is the 3rd build" (Naomi).
The audit reads what the shapes stage recorded for every shape (detail sizes, null,
billboard, radius, the game's limits, ring membership) and the ring cells' thresholds from
the instances report, and walks the camera from 10 m to the cull for every one of them asking
Torque's own question - the detail with the largest size not above the projected pixel size -
then checks seven rules: R1 sizes strictly decreasing and above the null, the billboard
between; R2 a ring member drawn at every distance up to the hand-over and never beyond, its
cell beyond and never nearer, no billboard under the ring; R3 a tree is LOD 0 then the
billboard; R4 no generated level on a building; R5 chain switch sizes are the game's; R6 a
LOD-0-dropped shape starts smaller; R7 props' ladders. Exit 1 on any failure; it runs before
any load.

Its first run, on the third build, found four defects no report showed: (1) the recorded
ladder went stale after the clamp / imposter / lift steps, so report and audit read pre-clamp
sizes - the stats are now taken once from the final levels; (2) 1,028 props had their
coarsest level AND their null at 2 px at the game's own distances - `_above_null` lifts every
level strictly over the null and the billboard between; (3) ring members still carried a
runtime billboard (1 px under a 111 px null) - none now; (4) a ring member's null rounded UP,
so a 6 m house stopped at 1,940 m and its cell started at 2,500 m - a 560 m gap - the null
rounds DOWN now (an overlap is a hull inside a building at 2.5 km; a gap is a missing house).
Shapes v25.

Second and third runs (v26–v28): (5) lifting a mesh to `null+1` left a band `[null, null+1)`
where the null is chosen — 1,678–2,510 m of no warehouse — so the mesh keeps its size and the
null comes DOWN under it; (6) "the null at the hand-over" was the wrong model altogether: a
level is drawn for `p >= its size`, so a house left at 44 px with a null at 3 px vanishes at
181 m and the null is chosen from there to 2.5 km — a ring member's coarsest level now TAKES
the hand-over's pixel size, the null one under it (`shapewrite._far_level`); (7) the 2 px
floor stopped two 2.9 m sheds 616 m short of the ring — a 1 px coarsest level with no null is
allowed. The v27 build's shapes stage also failed with a KeyError and the audit read the
previous state — it now refuses to run without a finished report. Torque's selection rule is
pinned as `tools/test_a3_rules.py::test_torque_detail_selection`.

v28 audited clean and its budget said **32.4 M building triangles in view at 4 km**: with
`authored_lod1_drop` on, a house's LOD 2 (game start 16 km) fell under the hand-over and was
dropped, so LOD 0 ran to 2.5 km for every house. v29: the COARSEST level under the hand-over
covers the band to it - LOD 0 to where the game's LOD 1 ended (512 m), the game's LOD 2 from
there to the ring. Declared: in that band the game draws LOD 1; LOD 1 is the residency trade.

v29 audited clean at 23.5 M building triangles inside 4 km — per shape, the custom packs:
`USSW_BonusResidentialMedium01_L3_3x2` ×112 at 9,757 triangles each to 2.5 km, and its
siblings; their LOD biases keep CS2 drawing LOD 0 for kilometres. At the game's own switch
points this city is what CS2 draws, and BeamNG cannot draw it. **v30: one uniform
`lod_detail_scale` on every chain again** — what v20 got wrong was three scales (buildings
10, props 4, generated ladders their own), not the scaling — built at 3 for buildings and
props, trees at 10 (the billboard), ring at 2.5 km. And the level shown AT the hand-over is
the nearest one under it (the game's choice at that pixel size), not the coarsest.

**v30, audited clean, 2026-08-31:** 3,069 MiB resident; inside 4 km 5.78 M triangles (buildings
and props 3.63 M, roads 1.12 M, forest 0.67 M, ring 0.15 M) against 27 M unscaled and the §7
build's ~5 M. A house: LOD 0 to ~60 m, the game's LOD 2 to 2.5 km, then its cell's ring; the
custom packs' 10k-triangle LOD 0 to ~100 px; the airport its LOD 1 (57k) to 2.5 km; a tree LOD 0
to ~100-200 m then the billboard; props the same 3× uniform scale. Settings on the level:
`lod_detail_scale 3`, `lod_detail_scale_prop 3`, `lod_detail_scale_tree 10`, `far_ring_metres
2500`, `authored_lod0_max_tris 20000`, `authored_lod1_drop` on. Awaiting Naomi's look.

**Naomi's look at v30 (2026-08-31):** houses "perfect"; big city buildings keep full detail
too long (she asks ~0.66 of the distance); trees can switch nearer (~0.5) and their canopy
tops thin out at the crossover; 10–30 fps at Normal mesh/terrain/texture, High shadows, Ultra
lighting. Calibration read from the game: Mesh Quality is `$pref::TS::detailAdjust` —
Lowest 0.5, Low 0.75, **Normal 1.0**, High 1.5, Ultra 2.0 (`lua/ge/extensions/core/settings/
meshQuality.lua`); every threshold here assumes 1.0, so on Normal the level switches where
the build says. The tops: the imposter atlas is 256 × 128 for a whole tree (measured in
`temp/`), Torque's 64 px default cell. v31: `lod_detail_scale_large` (4.5, for buildings of
radius ≥ `lod_large_radius_m` 30 — OURS, declared: CS2's limits scale with size and keep a
tower sharp for kilometres), `lod_detail_scale_tree` 20, `tree_billboard_px` 128 written as the
billboard node's FCOLLADA user property `bb_dimension` with `bb_includePoles`.

**v31, audited clean (2026-08-31):** 5.65 M triangles inside 4 km (buildings and props
3.60 M, roads 1.12 M, forest 0.57 M, ring 0.15 M), 3,069 MiB. What the large scale changed:
`USSW_ResidentialHigh01_L3_5x4` (35 m radius, LOD 0 dropped) was its 20,596-triangle LOD 1
from the kerb to 2.5 km in v30 — LOD 1's size at 3× (18 px) sat under the hand-over, so it
was the level shown at it; at 4.5× it is 159 px (~290 m) and the 500-triangle LOD 2 covers
the band to the ring. Trees: LOD 0 to ~30 m then a 128 px-cell billboard. The audit now takes
the shape's kind from the build (`kind` in the shape result) rather than guessing it.
v32 (buildings 2x, large 3x): audit clean, 6.08 M triangles in 4 km, 3,069 MiB.
v33 (buildings 4x, large 6x, props 3x, trees 20x): audit clean, 5.41 M triangles in 4 km, 3,069 MiB. Naomi's pick after clarifying scale direction.

### 8.14 The FPS isolation sweep, and what still holds detail to the ring

Naomi at v33: 20–30 fps in the city; some tall hotels never change LOD; a tree crossover
where the mesh vanishes before the billboard shows.

* **The hotels are the 11 no-chain landmarks** (state query): `Five_Luxe_JBR_` 243k,
  `Kinsman_Plaza` 176k, `LuxuryTowerResidence` 92k, `Address_Beach_Resort_Dubai` 87k, three
  floating boats (chain, LOD 0 dropped), `Two_Seventy_Hennepin`, `Torre_Tetris`,
  `Amazon_Warehouse`, `HotelMondial1` — workshop assets whose LOD slots held copies of LOD 0,
  so nothing coarser exists; each holds its one level to the 2.5 km ring. The closed
  decimation experiment (§8.9) is the only path to a mid level for them; whether they matter
  is what the sweep is for.
* **`tools/beamng/cs2diag.lua`** (installed to the userfolder's `lua/ge/extensions/`):
  `extensions.load('cs2diag')` then `cs2diag.start()` — measures a baseline where you stand,
  hides one group at a time (Objects, Networks, Markings, Areas, Vegetation, FarRing,
  TerrainCut, LaneFurniture) for 8 s each, restores it, ranks the FPS gain per group, prints
  the summary and writes `cs2diag_report.json` (with `getMemoryInfo` before/after and a
  baseline-drift line so noise is visible). Run parked downtown and again overlooking the
  city.
* The tree crossover is unexplained until the sweep session: check the imposter atlas size in
  `temp/` (did `bb_dimension=128` take?) and whether the vanish band is the atlas being built
  on first sight (`Creating imposters` in the log at that moment).

**First sweep (Naomi, overlook at (2991, 154), v33, Normal/Ultra/High): the FPS is MEMORY.**
`usageBytes` 5.85 GB of a 6.09 GB driver budget — 96 %, 0.24 GB available — and the sweep
proves it the other way round: hiding buildings, vegetation, roads, markings each moved FPS by
under ±1 at a 13.4 fps baseline (hiding frees no residency), and the FPS jumped to ~27 mid-
sweep and STAYED for the closing baseline — the texture streamer settling, not the group that
happened to be hidden ("+14.1 without TerrainCut" is that artefact; drift +12.8 says so).
Note the budget itself was 6.09 GB this session against 6.78 before — something else on the
machine held ~0.7 GB. And the tree-billboard `bb_dimension=128` did NOT take: atlases rebuilt
this session (41 min old, `VFP_SylvesterPalmTree01…`) are still 256 × 128 — the FCOLLADA
user-properties route is ignored by BeamNG's importer; UNVERIFIED-FAILED, the harmless extra
stays in the .dae, the knob does nothing yet.

**The billboard cell, the real mechanism (2026-08-31):** vanilla GridMap's oak ships a
`TSShapeConstructor` companion (`untitled.cs`: `addImposter("0","0","0","0","64","0","0")`) —
so the atlas cell is set from a `.cs` beside the shape, not from the node. `shapewrite` now
writes `<tree>.cs` (`addImposter(switch px, 8 equator views, poles on, dim = tree_billboard_px)`)
for every tree with a billboard and drops the dae's `bb_autobillboard` node for those (one
billboard, one owner); `dae.sweep_orphans` sweeps `.cs` too. Shapes v32. Verify after Naomi's
next load: the rebuilt atlases in `temp/` should read 1024 x N (8 views x 128) instead of
256 x 128 — a per-tree VRAM cost, ~0.5 MB BC per type, ~130 MB over 253 tree shapes if all
rebuild, which the streamer can evict.

Same build carries Naomi's picks: `authored_lod0_max_tris` 5000 (637 shapes, 14.7 M LOD 0
triangles left out), `forest_rare_type_min` 3 / keep 5 m (955 one-and-two-item types dropped,
164 landmarks kept), `foliage_density` 33 (576k items, 1,890 types). Resident 2,911 MiB
before the .cs build; audit clean.

**The billboard saga's end (2026-08-31):** the `.cs` companions "disappeared" because the
engine's shape loader IMPORTS a `TSShapeConstructor` script and rewrites it as
`<shape>.dae.asset.json`, deleting the script as obsolete (`Resource2<TSShape>::create`,
watched live through a 20 s poll: 238 written by the shapes stage, converted one by one across
the compiler's run). The level now carries 238 asset companions with
`imposters: [{dimension: 128, equatorSteps: 8, includePoles: true, size: <switch px>}]` — and
v33 writes that file DIRECTLY (no .cs, no per-cook conversion), with `dae.sweep_orphans`
taught the double extension so a rebuild keeps them. Verify on Naomi's next load: rebuilt
atlases in temp/ should be larger than 256 x 128, and the canopy tops should survive the
crossover (poles are in the atlas now).

Level as handed over (force-built, audit clean, 2,911 MiB): `authored_lod0_max_tris` 5000,
buildings 4x / large 6x / props 3x / trees 20x, ring 2.5 km, rare floor 3 (keep 5 m),
foliage 33 %, billboards 128 px 8 views with poles.

**...and the asset.json route bakes ORANGE (2026-08-31, reverted in v34).** The size took (238
atlases rebuilt at 512 x 512) but Naomi's screenshot showed every distant tree flat orange, and
decoding a fresh atlas put the orange IN the atlas: opaque pixels mean RGB (215, 65, 4) —
BeamNG's no-material colour — while the same trees render normally up close. The imposter
bake under an asset-file config runs without the shape's materials; mechanism unknown, one
suspicion is the bake running earlier in the load than the material mount. v34 reverts to the
dae `bb_autobillboard` node (engine default bake, known good), `tree_billboard_px` marked
INERT in its own help text. The 11 no-chain landmarks stay as the one open visual item
(nothing coarser exists; the error-bounded decimator remains the only path).

**Correction, same hour (Naomi: "why revert something that worked?"):** the asset.json route is
vanilla's own — 611 `.dae.asset.json` ship in the game's levels, trees included
(`tree_aspen_blocker_a`: `equatorSteps 16, includePoles false, dimension 128`). The orange
bake came with OUR parameter choice (8 views, poles on); v34's revert was over-broad and was
stopped mid-build. v35 keeps the route with vanilla's tree parameters verbatim (16 views, no
poles, 128 px). If v35 still bakes orange, the parameters are exonerated and the suspect
becomes the bake's material context - then the revert is right.

**Verdict (2026-08-31, two in-game confirmations):** the asset.json imposter bakes orange for
this level's shapes even with vanilla's parameters verbatim — worse on the second look ("orange
blobs for a lot of trees"). The one delta left standing against vanilla: material LOCATION.
Vanilla defines a shape's materials in a `materials.json` beside the shape; ours live in
`art/cs2/main.materials.json`, and the asset-route bake runs at shape load, plausibly before
that file mounts — the node-route bake runs later and has never failed. v36: asset.json
imposters off for good, the dae `bb_autobillboard` node is the billboard (engine-default 64 px
cell — the soft-but-correct look), `tree_billboard_px` inert with the reason in its help. The
untested candidate fix, for another day: a shape-adjacent materials.json for the tree
materials only.

### 8.15 LODs for the no-chain landmarks — the MeshLab backend

"Investigate LODs for buildings that don't have LODs" (Naomi). The 11 landmarks hold their
single full mesh to the 2.5 km ring because clustering webs their facades and pyfqmr pulls
metres of error across the unwelded seams (§8.9). The investigation, all measured:

* `pymeshlab`'s `meshing_decimation_quadric_edge_collapse_with_texture` after a WELD
  (unwelded, every edge is a "boundary" and the collapse refuses: 176,587 -> 176,583) and
  with **`optimalplacement=False`** — a collapse lands ON an existing vertex, so the
  simplified surface lies on the original by construction (decimated->original Hausdorff
  max 0.0 m on the balcony tower, against 18 m with free-floating placement).
* Kinsman_Plaza: 176,587 -> 70,995 triangles at 0.7 cm mean error — a clean mid level,
  confirmed in a Blender render. LuxuryTowerResidence: usable. The perforated balcony
  facade (`EU_ResidentialHighSignature03`) still webs shut — visible in the render even
  where the metric is happy; its measured displacement is what defers it out.
* My centroid-kNN Hausdorff overstated on large triangles (a roof's own face lost to 48
  nearer window facets); MeshLab's `get_hausdorff_distance` replaced it in the probe.

Built: `csiibundle/decimatemeshlab.py` (the reducer behind `decimate.lod_ladder`'s interface;
OBJ round-trip because `pymeshlab.Mesh(w_tex_coords_matrix=...)` leaves the wedge texture
index unset; wedge UVs welded back per unique (vertex, uv); `pymeshlab` optional, extras
`[lods]`). `lodchoice.best_levels` enters it in the CONTEST for meshes >= 20,000 triangles —
judged, like every candidate, by measured two-sided displacement (lods product v5). The
consumer's building gate opens ONLY for shapes whose baked levels the meshlab backend won
(`_lod_backend` -> `ShapeJob.lod_backend` -> `lod_levels(meshlab_generated=)`, shapes v37);
every other building still refuses generated levels (§8.12's lattice rule). The switch
points stay displacement-gated, so a webbed level only ever shows where its error projects
under `lod_switch_error_px`.

First bake of §8.15 (lods v5, 4m38s): 2,599 baked, MeshLab won levels on 15 heavy assets —
`LuxuryTowerResidence` at 0.06 m displacement for 61k of 92k, `Address_Beach_Resort_Dubai`,
`Torre_Tetris`, `Two_Seventy_Hennepin`, `HotelMondial1` — and `Five_Luxe_JBR_` went to cluster
because cluster MEASURED better there (the contest doing its job). Two defects kept it out of
the level: the index records `backends` (per-level winners) and the consumer read `backend`
(gate never opened); and `Kinsman_Plaza`-class assets were counted among the 1,757 "authored"
skips because their LOD slots hold byte-copies of LOD 0 — the bake now digests the copies
(order-independent sorted corners, the consumer's own rule) and generates real levels for
them (lods v6, shapes v38).

Second bake (lods v6, shapes v38): 26 identical-copy assets now generate — Kinsman ships
44k + 43k instead of 176,587 (LOD 0 promoted at its measured sub-centimetre error), the
landmark ladders are real, 2,927 MiB, cs2 3.12 M triangles in 4 km, audit clean. One holdout
taught the last rule: `Five_Luxe_JBR_` lost the contest to cluster by 12 cm of p95 — and a
p95 cannot see a cluster lattice, the very defect that banned generated levels from
buildings. **lods v7: meshes of 20,000 triangles and up take the MeshLab backend outright**
(on-surface by construction); the contest remains for the small, which are never buildings.

### 8.16 Naomi's screenshots (2026-08-31 01:29) — the chimera level, the billboard's size, and the stadium

Six screenshots against the level on disk: buildings that never change LOD (the JBR beach
towers, the AON tower), buildings that only ever render coarse (the baseball stadium, harbour
warehouses), and a band where trees stop rendering between the mesh and the billboard. Three
distinct causes, none of them what the audit was watching:

1. **The level she looked at is a mid-session chimera.** The force build ran 20:37→00:35 and its
   shapes stage executed at 21:18 — from the working tree as it stood mid-§8.15, before either
   evening commit (947e37c at 23:32, a0bada1 at 00:36). Its state still shows Kinsman_Plaza as
   one 176,587-triangle level and 25 single-level ≥20k shapes; the §8.15 ladders never reached
   the disk she flew over. Its tree rows carry `imposterConstructor` (the v33 asset.json route)
   with **no `.dae.asset.json` and no `.cs` left beside the shapes** — the revert removed the
   configs but not the state. No new defect; the fix is the v39 rebuild from HEAD.
   `NewAonCenter` stays single-level by design: 12,727 triangles to the ring is under every
   threshold and costs nothing — CS2 keeps a landmark sharp for kilometres too.

2. **The billboard's size is its far stop, and we wrote the takeover size there.** Vanilla's
   own `pine_radiata_forest.dae` is the rule: meshes `a800/a350/a150` and `bb_autobillboard25`
   — the card's detail size sits one step over nothing, so it draws from where the meshes end
   all the way out. `_vegetation_imposter` instead gave the card the replaced level's switch
   size (SpruceTree01: mesh 655, bb 164, null 27), and Torque's selection then hands
   `[null, bb)` to the null: **the tree drew nothing from 99 m to 601 m** — the bare mid-range
   of every look since v30, previously blamed on the atlas cell size. `_imposter_thresholds`
   had the same defect one octave down for props (bb at the old null, final null at
   null/IMPOSTER_REACH — the declared 3× reach never drew). Both now write the billboard at
   `null + 1`; audit **R8** pins the adjacency (R1 allowed bb anywhere between null and mesh,
   which is exactly the defect) and `test_billboard_runs_to_the_null` pins the arithmetic.
   Trees now: LOD 0 to ~25 m, the card to ~1.8 km (the scaled cull × IMPOSTER_REACH).

3. **The `authored_lod0_max_tris` drop assumed the game's LOD 1 is a usable stand-in, and for
   the stadium it is not.** `Oracle_Park_by_Dmscopio` ships 927 of 147,867 triangles — 0.6 %,
   a smooth teal hull at the kerb. The distribution has a knee: below 5 % sit 7 shapes
   (Oracle Park plus EU railing/window-guard props, ~19 MiB of LOD 0 kept); the next ratio up
   is 6.5 %. `DEGENERATE_LOD1_RATIO = 0.05`: a shape whose authored LOD 1 is under 5 % of
   LOD 0 keeps LOD 0 (counted per shape, `lod0DropSkippedDegenerate`). The airport-class
   shapes (LOD 1 at 20–40 %) keep the drop — that trade is the declared one.

   Also hers to know: the flat-white warehouse look is `texture_max 256`, a residency setting,
   not a LOD; and `Baseball_Field__2_revised2` itself is a 12-triangle field plane in CS2 —
   the visible bowl is the separate Oracle_Park building above.

**The v39 rebuild (2026-08-31, 11m23s, validator 0 problems): 2,970 MiB**, budget at 4 km:
cs2 3,086,913, networks 1.11 M, forest 717 k, ring 146 k — **5,287,248 total** (v33 was 5.41 M
with none of the landmark ladders). `lod0Dropped` 630 (+7 degenerate keeps: Oracle Park 0.6 %,
Planetario 5.0 %, the EU railings/window guard). All 238 trees at `bb == null + 1`
(SpruceTree01: mesh to 25 m, card to 579 m). Kinsman 176k/44k/43k at 284 m / 1,132 m / ring;
Oracle Park LOD 0 to 684 m then its 927; Five_Luxe LOD 0 to 1,084 m then 65,778 — its MeshLab
level measured 1.47 m of displacement and the switch gate holds it out to where that projects
under 8 px, which is the gate doing its declared job on a hard facade, not the ~90 m the first
§8.15 bake note hoped. Audit **R1–R8 clean** after R4 learned the §8.15 gate (it reads the bake
index's `backends` — the same authority `stages/shapes._lod_backend` read — instead of banning
every generated level on a building; LOD 0 promotion gated the same way). One observation left
open: `LuxuryTowerResidence` ships two generated levels of 61,594 triangles each (MeshLab
stalled at the same count for both targets) — a few wasted MiB, not a defect the switch points
can show. **Awaiting Naomi's look**: the trees mid-range, the stadium up close, the beach
towers switching.
