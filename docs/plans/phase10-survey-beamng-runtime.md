# Phase 10 survey — the built level in BeamNG (runtime cost)

Measured 2026-08-29 against the level on disk at
`C:\Users\naomi\AppData\Local\BeamNG\BeamNG.drive\current\levels\cs2_city_fine` (the
after-phase5b build: 7,883 shapes, 3,482.6 MiB `.cdae`, matching
`docs/baselines/cs2_city_fine-report-after-phase5b.json`). `keep_dae: false`, so all geometry
figures come from `shape_ladders.json` + the `.cdae` sizes. No file was modified by the survey.

## 0. The headline — the baseline's triangle table is wrong, and it is the number the phase steers by

* `apps/cs2beamng/beamng/budget.py:334-346` `radius_hint()` returns **a nominal 2.0 m for every
  object shape** ("An object shape is small and its exact radius does not change which detail it
  picks inside a view distance"). That premise is false: Torque picks the detail by projected pixel
  size, and pixel size is linear in radius.
* Worked example, `art/shapes/cs2/USSW_ResidentialHigh01_L5_5x4.dae`, ladder
  `{74: 81836, 30: 20700, 19: 4736}`:

  | radius | 50 m | 200 m | 500 m | 1000 m |
  | --- | ---: | ---: | ---: | ---: |
  | 2 m (what the tool uses) | 20,700 | **0** | **0** | **0** |
  | 20 m (what it is) | 81,836 | 81,836 | 20,700 | 4,736 |

* Recomputing `measure()` over the real level from the real spawn (1404, 1168), 4,000 m view,
  varying only the cs2 radius:

  | cs2 radius | cs2 tris in view | ALL groups total |
  | --- | ---: | ---: |
  | 2 m (baseline) | 18,439 | 1,292,894 |
  | 8 m | 355,972 | 1,630,427 |
  | 15 m | 1,422,883 | 2,697,338 |
  | 25 m | 3,469,086 | 4,743,541 |

* The build report's own `drawDistance.building.ourCullMedian = 1,330 m` is only reachable with a
  radius an order of magnitude above 2 m.
* **Assessment:** the baseline line `cs2 15,019 (12,352 statics)` is an instrument artefact, not a
  measurement. Buildings are almost certainly the *largest* draw item in the level, not the
  smallest. This is exactly `docs/plans/a8-performance.md:314-318` (P8 item 1) — filed, never
  closed. **Fix the instrument before ranking any draw-side work.**

## 1. CS2 prop/building instances, LODs, and what "Visible Mesh Final" means

**Placement — good, no duplication.**

* `apps/cs2beamng/stages/instances.py:330-338` — one `TSStatic` per instance, `shapeName` pointing
  at a shared `art/shapes/cs2/<name>.dae`. Measured: 15,697 TSStatics referencing 4,275 distinct
  cs2 shapes (plus 1,747 tile statics and 1,870 `None`-collision paint tiles). No per-instance
  `.dae`. The de-duplication key is `(assetId, mesh state, paint variant)`
  (`apps/cs2beamng/stages/shapes.py:133-185`).
* `apps/cs2beamng/beamng/level.py:244-256` `tsstatic()` sets `useInstanceRenderData: True`
  **unconditionally**, including on the 1,747 network/skirt/terraincut tiles and 1,861 paint tiles
  that never carry an `instanceColor`. Worth checking whether that defeats the static shape
  manager's batching for tiles.

**LOD levels are per shape — yes.**

* `apps/cs2beamng/stages/shapelods.py:144-194` `lod_levels()` builds `[(px, [Mesh])]` per shape:
  LOD 0 + the bake's LOD1/LOD2 from `derived/lods/<source>_LOD{1,2}.glb`.
* `apps/cs2beamng/stages/shapelods.py:235-258` `_promote_lod0()` — A8/P2: LOD 1 replaces LOD 0
  where measured p95 displacement ≤ `lod0_max_error` (0.10 m). This build: **397 promoted, 2,941
  held back** by `lod0_min_triangles = 1000` (`shapelods.py:121`).
* Ladder ceiling `LADDER_RATIOS = (1.5, 6.0, 24.0)` (`shapelods.py:31`); the measured-displacement
  rule may only *defer* a level, never advance it (`shapelods.py:19-30`) — hard-won, do not touch.

**"Visible Mesh Final" and collision — this is where the cost is.**

* `apps/cs2beamng/stages/instances.py:54-58`: cs2 statics use `collisionType = "Collision Mesh"`
  (the `Colmesh_` node), **not** the full-detail mesh. Good.
* `apps/cs2beamng/beamng/tilemesh.py:10-14`: `Visible Mesh Final` rasterises **detail 0** —
  "BeamNG's resource checker measures visible-mesh collision at `setCurrentDetail(0)`". So
  networks / skirts / terraincut collide against their **undecimated LOD 0**.
* Separate simplified collision mesh — `apps/cs2beamng/stages/shapecollider.py:25-102`:
  * `kind == "tree"` and `is_tree` → trunk cylinder (`_trunk`, ~28 tris). **157 shapes.**
  * `kind == "tree"` and not a tree (PlantObject) → **no collider**. 77 shapes.
  * otherwise: LOD 1 hull via `collision.colmesh_from_lod` when `len(tris) <= colmesh_max_tris`
    (**20,000**, set at `stages/shapes.py:113`) or the footprint is > `BOX_COLLIDER_MAX_METRES`
    = 20 m. **2,506 `mesh` + 30 `mesh_oversized`.**
  * else a padded `colbox`. **1,311 shapes.**
  * `no_collide` (light-bearing prefabs, `collision_lights: false`) → **194 `lightNone`.**
* **The 20,000 cap is the defect.** "Simplified" only ever kicks in above 20k triangles, which
  almost nothing reaches. Measured colmesh per cs2 shape: **median 58, p90 2,257, max 59,389.**
  `USSW_ResidentialHigh01_L5_5x4` ships a **20,700-triangle** collider; `MedicalUniversity01`
  53,463.
* **World collision triangles, measured per instance (not per shape file):**

  | source | triangles |
  | --- | ---: |
  | cs2 TSStatics (15,688 × their colmesh) | **25,122,936** |
  | tiles on `Visible Mesh Final` (networks 9.27 M + skirts 1.67 M + terraincut 0.08 M) | **11,014,445** |
  | collidable forest (370,800 items over 2,327 types) | **35,937,382** |
  | **TOTAL** | **72,074,763** |

  `docs/cs2-api-audit.md:5408` corroborates from the engine's own log: `Loaded Static Collision …
  Tris: 89,096,868, Memory: 3720.81MB` and `collisionCache 7349 unique meshes, 1,035,444 instances
  … 3062.80mb`.
* **The reported figure is `4,327,133`** — `apps/cs2beamng/beamng/budget.py:79-81` counts
  `Colmesh_` triangles **per shape file**, ignoring instance count entirely, and never counts
  `Visible Mesh Final` tiles at all. A 17× understatement.
* `apps/cs2beamng/beamng/levelcheck/collision.py:53-55` gates on `path.lower().endswith(".dae")` —
  with `keep_dae: false` there are no `.dae`, so `collisionTypeMismatch` reports `0/0` on
  **every** shape. A blind check.

**Do small props get collision?**

* TSStatic props: yes, unconditionally (`collision_props: true`), with an LOD1-hull colmesh.
* Forest props: `apps/cs2beamng/stages/instanceforest.py:151-178` `_is_collidable()` — dropped for
  height < 0.5 m (`FOREST_COLLIDABLE_MIN_HEIGHT`), footprint < 0.8 m
  (`FOREST_COLLIDABLE_MIN_FOOTPRINT`), plants, and no-collider shapes. This build: 85 `tooShort`,
  425 `tooThin`, 77 `plant`, 190 `noCollider` → **2,327 of 3,104 types still collide, 370,800
  items.** The survivors are the problem:

  | collision tris | colmesh | items | type |
  | ---: | ---: | ---: | --- |
  | 1,008,700 | 524 | 1,925 | `CommercialACMachineRoof05_v0` |
  | 744,124 | 2,108 | 353 | `Hammock02_v0` |
  | 678,561 | 1,651 | 411 | `HangingChair02_v0` |
  | 652,916 | 781 | 836 | `ShippingContainer01_v0` |
  | 609,600 | 1,524 | 400 | `Hammock01_v0` |
  | 584,416 | **28** | 20,872 | `OakTree01_v0` (trunk cylinder — correct) |
  | 549,432 | 117 | 4,696 | `ACMachineWindows02_v0` |
  | 498,150 | 243 | 2,050 | `GardenBench02_v0` |

  Trees are fine. **Rooftop AC units, hammocks and hanging chairs carry 500-2,100-triangle
  colliders each.**

## 2. Networks — 8,259 records → 7,289 segments → 1,024 tiles

* `apps/cs2beamng/stages/networks.py:88-94` — bucketed by `floor(pos/tile)` of the segment origin,
  one bucket per segment; `stages/networklod.py split_tile` quarters a bucket past
  `SPLIT_VERTS = 300_000` (`networks.py:45`). Result: 1,024 tiles, 787.1 MiB.
* **No per-tile duplication** — a segment lands in exactly one bucket. **No prefab-level
  duplication either**: 7,289 segment records, 7,289 distinct `assetId`s; every segment is its own
  assembled mesh. Merging into tiles is the right call.
* LOD ladder: `apps/cs2beamng/beamng/tilelod.py:99-239` + `beamng/tilemesh.py:105-159`, 3 levels
  per tile, decimated to `TILE_TARGETS = (0.25, 0.06)` (`tilemesh.py:21`) with `boundary_pins`
  holding the seam (`tilelod.py:70-85`). **Content is identical across all three levels**
  (`tilesNeverCulled = 28`, `meanTrianglesKeptAtFarLevel = 1.0`): CS2 never culls a road inside
  4 km, so levels differ only by decimation.
* **The road meshes ARE the collision meshes at full LOD 0 count.** `stages/networks.py:135`
  `collision="Visible Mesh Final"` → 9,266,577 collision triangles over 1,024 tiles. The `.dae`
  carries no `Colmesh_` node (`networks.py:241` `collision=[]`).
* `castShadows` is **not** disabled for networks (`stages/derived.py:53-62` disables it only for
  markings and areas) — 1,024 road tiles in the shadow pass.

## 3. Areas — 27k areas, 747 tiles, 386.7 MiB, 8.04 M triangles (LOD0 6.26 M)

* Built per area (`tools/csiibundle/bake/areas.py:261-280` → `TileSink.add()`; geometry in
  `tools/csiibundle/areas.py:294+ build_pieces()`).
* **Triangle density is set by the conform tolerance, and it is tight.**
  `tools/csiibundle/areas.py:54-63`: `AREA_LIFT = decals.LIFT["area"] = 0.020 m`,
  `CONFORM_TOLERANCE = AREA_LIFT * 0.5 = 0.010 m`, `MAX_DEPTH = 12`, `PIECES_PER_CELL = 256`,
  `PIECE_BUDGET = (64, 65536)`. `conform_triangle` (`areas.py:195-250`) 4-way splits until the
  ground is within **1 cm** of the piece: **6.26 M LOD0 triangles for 26,855 areas ≈ 233
  tris/area** of flat paint.
* **Not a prism.** The docstring (`areas.py:1-14`) is explicit that CS2 renders each triangle as a
  prism spanning `m_HeightRange` and *paints the terrain inside it as a decal*
  (`RenderedArea.m_DecalLayerMask = DecalLayers.Terrain`). We rebuild it as thin geometry lifted
  20 mm — a faithful *look*, not a faithful *cost*.
* **Collision: off** (`stages/derived.py:56`, `collision_paint: false` → `None`). Correct.
  `castShadows: False` set (`derived.py:165-166`). Good.
* **Drawn as opaque geometry, not decals.** All 40 `cs2_city_fine_area_*` materials are opaque.
  No z-fighting (the 20 mm lift), but 747 opaque `TSStatic`s of full-coverage geometry over a
  terrain already drawn — pure overdraw.
* Alternative: **terrain layers** — the level already runs 7; the high-coverage surfaces
  (concrete/asphalt) are exactly what a splat layer is for and would cost 0 triangles. A
  `DecalRoad` cannot express an arbitrary polygon; groundcover is billboards.

## 4. Skirts — 641 tiles, 159.7 MiB, LOD0 1,671,542 triangles, collision ON

* `tools/csiibundle/skirt.py:1-10` — the §4.1 `<assetId>__base.glb` foundation band, a 1 m plinth
  round a building's silhouette, its bottom ring dropped onto the terrain.
* **Per instance, then merged per tile** (`tools/csiibundle/bake/skirts.py:75-131`): for every
  on-ground building the prefab's band is transformed to world, dropped, and the vertices copied
  into the tile sink. Genuine, by-design vertex duplication: the drop-to-ground makes each instance
  geometrically distinct. 1.67 M LOD0 triangles for a 1 m skirting board.
* **Collision on, `Visible Mesh Final`** (`stages/derived.py:57`, `collision_skirts: true`) →
  1,671,542 collision triangles. `docs/plans/a8-performance.md:289-291` recommends keeping
  collision; it does not require keeping it *exact*.
* Never culled (`tilesNeverCulled: 641`). `castShadows` on (`derived.py:57`).

## 5. Markings — 1,114 tiles, 130.1 MiB, LOD0 1,608,736 triangles

* **Meshes, not DecalRoads** (`tools/csiibundle/bake/markings.py:1-25`), one `TSStatic` per tile
  (`stages/derived.py:55`). Deliberate: `docs/plans/beamng-consumer-plan.md:141-142` — "83k
  markings as DecalRoads would be 6.5× the largest vanilla count".
* Collision none, `castShadows: False`. Both correct. These ARE culled
  (`gameNullMetresMedian 5,794 m`, `meanTrianglesKeptAtFarLevel 0.6251`).
* The 15,393 `DecalRoad`s in the level are the AI navgraph (`art/road/invisible.png`,
  `stages/navgraph.py:213-249`), against vanilla's 12,752 and a 40,000 limit. Cheap; leave alone.

## 6. Textures — 542.8 MiB DDS + 235.2 MiB PNG

* **The DDS half is correct.** `apps/cs2beamng/beamng/dds.py:1-20` writes BC7/BC5/BC4, DX10
  header, full mip chain (`dds.py:150-165`). Caps per kind (`beamng/materials.py:45-51`).
* **Which textures stay PNG:** everything written outside `textureplan.export_texture`:
  `art/terrains` **235.2 MiB PNG (104 files)**; `art/groundcover` 5; `art/road` 1. The terrain path
  never touches the DDS encoder — `stages/terraintextures.py:150-162, 190-193, 245-262` and
  `stages/terraintiling.py:106-150` call `png.encode_rgb` / `png.encode_gray` directly.
  24 × 2048², 80 × 1024²; as BC with mips ~213 MiB.
* Caveat, already in `docs/cs2-api-audit.md:5455-5460`: PNG-on-disk ≈ BC-in-VRAM here, so
  converting is a **first-load cook-stall** win (190 textures cooking between 81 s and 290 s of one
  load, `docs/plans/beamng-consumer-plan.md:575`), not a residency win.
* **57.1 MiB of the 235.2 is byte-identical duplication** (hash-checked): `<ground>_base_nm.png` =
  `_detail_nm.png` = `_macro_nm.png` for all 8 grounds; `_detail_b` = `_macro_b` for 7;
  `_base_h` = `_detail_h` = `_macro_h` for 7; `neutral_{ao,height,roughness}_{base,detail,macro}`
  ×3 each. Cause: `stages/terraintextures.py:172` `_height_texture()` and
  `terraintiling.py:106-115` `_mesh_normal_maps()` are called once per tier with the same source.
  Three texture objects in VRAM for one image.
* DDS duplication is minor: 7,743 files / 7,271 unique = **21.4 MiB**; one `_metallic.data.dds`
  written 103 times under 103 names. Dedup keys on the source stem
  (`beamng/textureplan.py:152-163`).

## 7. Materials — 6,144, 10.5× vanilla, and 1,240 of them are byte-identical

* From `art/cs2/main.materials.json` (6,123 entries): **1,573 canonical** CS2 materials; **4,509
  paint-variant materials** (`__v<key>`, `stages/shapes.py:290-313` `_material_tables()` and
  `stages/materials.py:282-320`; `variantsAsTexture 2,785` / `variantsAsFactor 1,724`); 40 area +
  7 ground + 8 terrain + 5 groundcover + 1 road. **The 10.5× is the paint-variant system, ~3× on its
  own** (`paint_variants_tree: 2`, `paint_variants_prop: 1`).
* **`beamng/materials.py` does NOT dedupe identical definitions.** `material_v15()`
  (`beamng/materials.py:75-168`) builds one object per name. Hash-checking ignoring
  `name`/`mapTo`/`persistentId`: **324 duplicate groups, 1,240 redundant materials**, worst 42-way
  (one advertising board). **1,154 of the 1,240 are paint-variant materials** whose baked tint
  collapsed to the same texture/factor.
* `validation.checks.duplicateMaterials: 0` is a *name* check (`levelcheck/materials.py:104-113`).
* `docs/plans/beamng-consumer-plan.md:190` already has this as **B2 "Material dedup by texture set
  — adopt"** ("BeamNG's #1 cost is unique materials/draw calls"). **Never built.**

## 8. Forest — 908,841 items, 3,104 types, 6.8× vanilla

* **Forest items, not TSStatics** (`stages/instances.py:148-157` → `forest/<type>.forest4.json`;
  `stages/instanceforest.py:181-223` → `TSForestItemData` per type). BeamNG instances these.
* Split by `beamng/placement.wants_forest` — uniform-scale trees and props go to forest, buildings
  and non-uniform-scale objects to TSStatic (only TSStatic carries `instanceColor`).
* **908,841 items / 3,104 types**, `forest/` is 138.5 MiB of JSONL parsed line by line at load.
  Vanilla's largest is 133,375 items / 226 types (`beamng/budgetreport.py:11-38`).
* **The type tail is the residency cost, and it is still open.** The 3,104 types mount
  **299.4 MiB of `.cdae`** — a `TSForestItemData` loads its shape whole for one item or ten
  thousand. `docs/plans/a8-performance.md:190-220`: "2,457 of 3,118 types — 79 % — exist to draw
  2.5 % of the foliage, and they cost 346 MiB of the 442." The proposed `forest_rare_type_min`
  predicate was **deferred, never built** (`a8-performance.md:362-380`). `forest_min_items: 10`
  merges paint variants only, and this build merged **0**.
* `forest_shadows: true` → 908,841 items in the shadow pass.

## 9. Lights — zero in this build

`settings.lights: false`. If on: 214 prefabs over 142,421 instances = 176,799 lights, capped by
`lights_max`; `CAST_SHADOWS = False` on every light (`stages/lights.py:56-58`). **Not a current
cost. Do not spend the phase here.**

## 10. What the budget model flags, and the known-open runtime items

`beamng/budget.py` blind spots: (1) `:334-346` `radius_hint = 2.0`; (2) `:79-81`
`collisionTriangles` per shape file, ignoring instances and `Visible Mesh Final` tiles — 4.3 M
reported vs 72 M real; (3) `:261-267` textures counted as file bytes, so PNG and BC7 are summed as
commensurate. What it does well: `heaviestShapes` / `shapeBytesTop100` (`:293-296`) — top 100 of
7,879 shapes = 959 MiB = 28 % of mesh bytes. `levelcheck/collision.py:53` is dead under
`keep_dae: false`.

| item | where | status |
| --- | --- | --- |
| Runtime imposters | `docs/cs2-api-audit.md:5399-5445` | CLOSED — `imposter_min_metres`; 190 of 4,275 shapes = 4.4 % |
| Static collision 3,720 MB / 89.1 M tris, 3,062 MB forest | `docs/cs2-api-audit.md:5440-5445` | **OPEN** — never re-measured |
| Oversized box colliders | `stages/shapecollider.py:48-57` | CLOSED — `BOX_COLLIDER_MAX_METRES = 20` |
| P8.1 — object drawn-triangle count is not measuring objects | `a8-performance.md:314-318` | **OPEN** — §0 |
| P8.2 — nothing has measured system RAM | `a8-performance.md:319-321` | OPEN |
| P3 Extras — `forest_rare_type_min` (N=10 → 241 MiB) | `a8-performance.md:362-380` | **OPEN, deferred** |
| P2 rest — decimate LOD 0 on the top 100 shapes (964 MiB) | `a8-performance.md:382-385` | OPEN |
| B2 material dedup by texture set | `beamng-consumer-plan.md:190` | **"adopt", never built** |
| Terrain ships `.png`, cooked at first load | `a8-performance.md:309-311` | OPEN — 235 MiB |
| BeamNG's slow-scene order: (1) unique materials, (2) vegetation, (3) shadow lights, (4) overdraw, (5) textures, (6) heavy collision on touched objects | `beamng-consumer-plan.md:154-160` | our level is worst on (1) and (6) |
| BeamNG guidance: simple collision for buildings/props; avoid Visible Mesh collision | `beamng-consumer-plan.md:158-160` | contradicted by the 20k cap and by skirts |

## Ranked wins

0. **Fix `radius_hint` before ranking anything draw-side** (`beamng/budget.py:334-346`). Read the
   radius from the shape's own bounds (`stages/shapewrite.py:60` computes it; `_result()` stores
   `radius`; write it into `shape_ladders.json`).
1. **Cap the object collision mesh: `colmesh_max_tris: 20000` → ~500-2,000**
   (`stages/shapes.py:113`, `stages/shapecollider.py:58-62`). 25.1 M collision triangles from cs2
   statics alone; BeamNG's own item (6); system RAM and BVH build time. The `colbox` fallback and
   the ≤ 20 m guard already exist. Risk: coarser contact on facades — invisible at driving speed.
2. **Take the collidable forest types that should never have collided**
   (`stages/instanceforest.py:151-178`): hammocks, hanging chairs, rooftop AC units, benches —
   ~36 M triangles. Needs a third rung (a colmesh-triangle ceiling or a "not reachable by a
   vehicle" test). Trees are already correct at 28 triangles.
3. **Build B2 — dedupe identical material definitions** (`beamng/materials.py`). 1,240 of 6,123
   byte-identical apart from their name. Zero visual risk.
4. **Give skirts a cheap collider, or none** (`stages/derived.py:57`): 1.67 M `Visible Mesh Final`
   triangles on tiles never culled. A per-tile box or the decimated LOD 1 does the same job.
5. **Turn off `castShadows` on network and skirt tiles** (`stages/networks.py:130-138`,
   `stages/derived.py:57,61`): 1,024 + 641 + 82 tiles of ground-flat geometry in the shadow pass.
6. **Loosen the areas conform tolerance and/or move high-coverage surfaces to terrain layers**
   (`csiibundle/areas.py:58`): 6.26 M LOD0 triangles for flat paint CS2 draws as a terrain decal.
7. **Ship terrain textures as DDS, and stop writing the same image three times**
   (`stages/terraintextures.py:172, 190-193`, `stages/terraintiling.py:106-115`): 57.1 MiB of
   duplication, three VRAM objects per ground; a first-load stall fix.
8. **Build the deferred `forest_rare_type_min` (N = 10)**: 241 MiB for 0.4 % of the forest.
9. **Re-measure, then decide about LOD 0 on the top 100 shapes** (`shapelods.py:95-121`) — only
   after #0 says what those shapes actually draw; the road-sign history says do not take it
   casually.

**Not worth the phase:** lights (zero placed), DecalRoads (15,393 invisible AI ribbons), imposters
(gated in A5), network tiling (no duplication found), DDS duplication (21.4 MiB).
