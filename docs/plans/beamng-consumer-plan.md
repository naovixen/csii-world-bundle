# CS2 → BeamNG: the `cs2beamng` application + engine-neutral bake stage — plan

## Context

The producer (the C# mod) is finished and accepted (H1–H14, VALID against 15 schemas on the
2026-08-14 22:20 rip). `tools/csiibundle/` holds every reconstruction algorithm bpy-free, and
`tools/blender_import_bundle.py` is one adapter over it. The next step is the first real
downstream consumer: **a BeamNG.drive level builder** — CS2 city in, `levels/<name>/` out,
drivable in BeamNG.

User direction (this session, verbatim where it matters):
- The rip stays as it is; the BeamNG consumer is secondary to it and must be *implemented
  correctly*.
- **"Full focus on performance and integration within BeamNG."** 1M+ artifacts; anything that
  can be done in-game or out is to be considered; **a longer build for a more usable experience
  is preferred**; not "1 fps chug".
- **An application**: full progress visuals, expected times, **nothing hangs**; settings to tune
  foliage density and disable props / foliage / lane furniture etc. for weaker PCs;
  **Windows, Linux, macOS**; **modern visuals**.
- Flow: rip with the in-game mod → run the app, whose default bundle path is the ModsData export.

Why a two-stage producer (kept from the first draft): the rip's wall-clock is gated by one
serial writer thread (mesh stage ≈ 76 % of a 27-min rip); draping needs the *assembled* road
surface that only exists after the rip; so derived data comes from an **offline bake**
(`csiibundle.bake`, numpy) that runs once per rip into `<bundle>/derived/`, and every consumer
reads it. The app runs the bake as its first stages, with progress.

Facts established this session (verified, not assumed):
- BeamNG 0.39.2.1 installed at `G:\SteamLibrary\steamapps\common\BeamNG.drive`; userfolder
  `C:\Users\naomi\AppData\Local\BeamNG\BeamNG.drive\current\` (`levels/` present).
- Static meshes must be **COLLADA `.dae`** (no glTF). Level = `levels/<name>/info.json`,
  `main/**/items.level.json` (one JSON object per line: `class`, `name`, `persistentId`,
  `__parent`), `art/**/main.materials.json` v1.5 (`Stages[0].baseColorMap/normalMap/
  roughnessMap/metallicMap/opacityMap`; `.color.png`/`.normal.png`/`.data.png` suffixes select
  colour space; `colorPaletteMap` + `TSStatic.instanceColor/1/2` three-channel palette), forest
  = `art/forest/managedItemData.json` (`TSForestItemData`, `shapeFile`, `windScale`, `radius`…)
  + `forest/<type>.forest4.json` (line-delimited `{type,pos,rotationMatrix[9],scale}`, uniform
  scale) under one `Forest` object; `GroundCover` objects scatter billboard grass per terrain
  layer (`Types[]`, `layer`, `probability`, `sizeMin/Max`); terrain = `<name>.ter` v9 (`u8 ver,
  u32 size, u16[size²] heights, u8[size²] layer (255 = hole), u32 n, names`) + `.terrain.json`
  + `TerrainBlock`; `LevelInfo.visibleDistance`, fog; `WaterPlane`/`WaterBlock`;
  `PointLight`/`SpotLight`; `SpawnSphere`.
- **`.ter` orientation determined empirically** (3,800 DecalRoad nodes on `small_island`, 2 cm
  median error; probe script to be committed): `heights[row=(y−pos.y)/sq][col=(x−pos.x)/sq]`,
  row 0 = south, col 0 = west, `h = stored·maxHeight/65536 + position.z`. Bundle heightmap has
  row 0 = north → vertical flip.
- Shipped `.dae` convention (trailer + Juniper tree read this session): `<unit>`/`<up_axis>`
  honoured; `base00 → { bb_autobillboard25, nulldetail<px>, start01 → <name>_a400/_a200 (or
  `_L250/_L120/_L30`), Colmesh_<name>-1 }`; trailing integer = pixel-size threshold; materials
  by symbol = `mapTo`; last LOD may use `lod_vertcol`.
- The bundle: LH Y-up, `.glb` X-mirrored + reversed winding; `frames.zup_instance_matrix` →
  RH Z-up = BeamNG. `lodCap` hard-coded 0 — **only LOD 0 ships**. Full city: 1,226,046
  instances (697k trees, ~9k buildings, the rest props/fences/sub-objects), 5,575 meshes,
  8,259 network segment glbs (~19M verts, 404 MB), 641k lane polylines, 83k markings,
  25k areas, 50k clip volumes, 4096² heightmap @ 3.5 m on 14,336 m, 4,641 materials,
  3,544 PNG textures (1.9 GB). Bundle on disk is **not stale**.
- Machine: Python 3.13, numpy 2.5; no GUI libs installed; no `pyproject.toml` in the repo
  (tools import `csiibundle` via `sys.path`).
- BeamNG's shipped Lua (read on disk): `-compilemeshes` headless `.dae→.cdae` compiler
  (`lua/ge/extensions/util/compileMeshes.lua`, args `-compileMeshesCustomPaths`,
  `-compileMeshesNoCache`), `-LoadSingleLevelForTextureCache -testlevel <name>` texture-cache
  warmer, `-LoadSingleLevelForShaderCache`; per-level `mainLevel.lua` hook
  (`freeroam.lua:186`); `spawnPrefab`/`removePrefab` (`ge_utils.lua:749/814`) for runtime
  prefab load/unload; vanilla `Forest` object is minimal; `GroundCover` scatters billboards per
  terrain layer.

## Decisions (made; flagged, not asked)

| decision | why |
|---|---|
| **App = Python package `cs2beamng` in this repo** at `apps/cs2beamng/`, plus `tools/csiibundle/bake/`; one root `pyproject.toml` (`package-dir` maps `csiibundle` → `tools/csiibundle`, `cs2beamng` → `apps/cs2beamng`), `pip install -e .`, entry point `cs2beamng` (GUI) / `cs2beamng build …` (CLI). **CLAUDE.md scope + PLAN.md §9 amended.** | shares `csiibundle`, one install, same precedent as the Blender adapter. |
| **UI = local web UI**: stdlib `ThreadingHTTPServer` + Server-Sent Events, vanilla HTML/CSS/JS (dark, card-based, no build step, no CDN), shown in a **`pywebview` native window when available, else the default browser**. Pipeline runs in a **separate process** (`multiprocessing`) with a message queue; UI process never computes. | modern visuals with zero heavy deps; identical on Win/Linux/mac; a subprocess is what makes "nothing hangs" and Cancel true. Qt was the alternative — 200 MB dep, and packaging is where cross-platform Qt apps bleed. |
| **C# producer untouched** (optional LOD-rip toggle is the last phase, off by default). | rip time is the constraint. |
| Markings/decals **pre-draped as geometry** in `derived/`, not BeamNG `DecalRoad` projection. | user's ask; engine-neutral; 83k `DecalRoad`s is an untested load-time risk. |
| `derived/` = **consumer cache inside the bundle**, own `derived/derived.json`, staleness key = per-layer input fingerprints + `bakeVersion` + options; never rewrites producer files. | mixed-vintage bundles make `generatedAt` alone wrong; per-product deps let the app re-bake only what changed. |
| **Object model for performance**: trees + all uniform-scale props/fences/poi → **forest**; buildings (+ stack sections) and non-uniform-scale objects → **TSStatic** (static shape manager batches repeats); world-space layers (networks, derived) → **256 m tiles**, quadtree-split above ~300k verts; every shape gets `bb_autobillboard` imposter + generated LOD ladder + `nulldetail` cull. | see Performance research / strategy. |
| **Collision on for everything physical by default**, per-class simplified colliders (Colmesh from LOD ≥ 0.3 m thick, ColBox, trunk cylinders), exact for roads; **paint layers off by default with an option on**. | user direction ("BeamNG is all about collision"); the docs' rule against heavy collision is met by *simplified* colliders, not by removing them. |
| Numpy vertical ray-caster = **uniform 2-D grid + CSR cell→triangle index** over all network triangles, `drop_many` bulk API, behind the existing `ProjectionSurface` protocol. | O(pairs), fits memory; embree/Blender backend can swap in. |
| Tints: **hybrid** — TSStatics try `colorPaletteMap` + `instanceColor/1/2`; forest types get baked albedo variants per (material, colorSet), capped. | forest has no per-instance colour. |
| Lane furniture: rigid **level** tiles → forest; the rest baked to tiles behind toggles + vertex budget. | `rigid_tile_matrix` is a shear on any grade; forest can't express it. |
| Deterministic `persistentId` = uuid5(namespace, `<layer>:<id>:<n>`); rebuilds are diffs. | |

## Architecture

```
CS2 (in-game) ──rip (C#, unchanged)──► <bundle>/  (ModsData/Carto — the app's default input)
                                          │
        cs2beamng (app)  ── preflight ──► validate + census + estimates + settings
             │             ── build ────► worker process runs the STAGE PIPELINE:
             │                              1  bake (csiibundle.bake → <bundle>/derived/, skipped when fresh)
             │                              2  terrain · water · sky · spawn
             │                              3  materials + textures
             │                              4  shapes (.dae, LODs) · networks · derived tiles
             │                              5  instances → forest / TSStatic · lights · groundcover
             │                              6  validate level · report
             ▼
   <userfolder>/levels/<name>/   (or any --out)
```

- **Progress contract**: every stage declares `total` units up front (from the census) and
  calls `advance(n)` at least every ~0.5 s of work (long numpy ops are chunked); worker sends
  `{stage, done, total, rate, eta, msg}` + heartbeat; UI shows per-stage bars, overall bar,
  ETA (EMA of live rate; *initial* estimate from `~/.cs2beamng/history.json` per-stage rates ×
  this bundle's counts, or shipped defaults), elapsed, live counters, log tail, **Cancel**
  (worker checks a flag between chunks; partial output is removed or kept, user's choice).
  A missing heartbeat for 10 s shows "worker busy" — never a frozen window.
- **Settings** (persisted `~/.cs2beamng/settings.json`, exposed in UI + CLI): bundle path
  (default = ModsData export, per-OS detection incl. Proton prefix), output (default = BeamNG
  userfolder `levels/`, per-OS detection), level name/title, **region** (centre/radius or
  full), **foliage density 0–100 %** (deterministic per-instance hash so a lower density is a
  subset of a higher one; big/old trees survive first), **toggles**: trees, props, fences,
  lane furniture (per category), road markings, instance decals, areas, skirts, lights, water,
  groundcover grass, AI roads; **texture max size** (256/512/1024/full), **LOD generation** on/off
  + aggressiveness, imposter/cull px thresholds, **collision group** (per class on/off + exact /
  simplified / off preset, paint collision opt-in), tile size, terrain upsample ×2, forest
  shadows on/off, tree wind (synthesised vertex colours) on/off, worker count, keep-derived,
  cook-in-BeamNG on/off + exe path, experimental modes (streaming / district / buildings-in-
  forest) once they earn their place; **quality presets** Ultra/High/Medium/Low set the
  defaults for all of the above.
- **Cross-platform**: pure Python + numpy; `os.link` with copy fallback; `pathlib` everywhere;
  no shell-outs; `pywebview` optional extra; CI matrix (GitHub Actions win/linux/mac) runs the
  offline tests + a region build against `docs/example-bundle`.

## Performance research (2026-08-15) — what BeamNG itself does, and what big maps do

Sources: BeamNG docs *Performance and optimization*, *Props, buildings and prefabs*, *Asset
pipeline*, *Troubleshooting*, *TSStatic*, *Forest Editor*, *Cached Collada (.cdae)*; forum
threads (LOD/collision tutorial, "optimizing a map", load-time threads); the Steam "map modding
guide from scratch"; the vanilla level zips on this machine (counted, not quoted); Los Injurus /
Roane County release notes.

**Vanilla calibration (counted from the shipped zips):**

| level | forest items | forest types | TSStatic | DecalRoad | shape files |
|---|---:|---:|---:|---:|---:|
| Johnson Valley | **663,057** | 76 | 15,559 | 4,345 | 498 |
| Italy | 206,148 | 99 | 13,971 | 7,900 | 1,419 |
| West Coast USA | 133,363 | 143 | **22,067** | **12,752** | **2,608** |
| East Coast USA | 186,706 | 150 | 2,187 | 3,115 | 678 |

→ 697k trees + ~500k props in forest is vanilla-scale (Johnson Valley); ~9k buildings + a few
thousand non-uniform props as TSStatic is well under West Coast; **83k markings as DecalRoads
would be 6.5× the largest vanilla count — the pre-draped-tile decision stands**; our ~5.5k shape
files (+ states/variants) is 2–3× vanilla's largest — acceptable, watch load time. Los Injurus
(the biggest community city) needs 12 GB VRAM at high texture detail and 5+ min first load —
texture cap and load-time honesty matter.

**Engine mechanisms confirmed (with the shape convention read from vanilla `.dae` files):**
- Shape hierarchy `base00 → { bb_autobillboard<px>, nulldetail<px>, start01 → <name>_a<px>…,
  Colmesh_<name>-1 }`. `_a<px>` = pixel-size threshold at which that LOD is used (letter is
  arbitrary; vanilla uses `_a` and `_L`); **`bb_autobillboard<px>` makes the engine generate a
  billboard imposter for ANY shape** (cached in the level's temp folder; regenerated when the
  shape/materials change) — trees, props *and buildings*; `nulldetail<px>` culls the shape
  entirely below that size; the last real LOD conventionally uses material **`lod_vertcol`**
  (vertex colours, no textures) to cut binds; "each LOD should roughly halve polycount and
  material count".
- TSStatic without `dynamic`/ambient animation renders through the **static shape manager**,
  which batches repeated shapes — so identical building/prop shapes as TSStatic are instanced.
- Forest: `TSForestItemData` has `collidable`, wind fields (`windScale`, `rigidity`, …); wind
  deformation is driven by **vertex colours** (R up/down, B flutter, G variation, black static)
  — CS2 meshes have none, so `windScale 0` unless we synthesise colours from height.
- Docs' priority order when a scene is slow: (1) unique materials / draw calls, (2) vegetation
  density, (3) shadow-casting dynamic lights, (4) overdraw from transparency/decals ("~1 draw
  call per decal", "every DecalRoad layer costs"), (5) texture size/count, (6) **heavy collision
  meshes on frequently-touched objects**. Collision guidance: "use simple collision meshes for
  buildings, walls, props, barriers", "avoid Visible Mesh collision unless the object really
  needs it" (higher RAM), **Colmesh at least 0.3 m thick or vehicles clip/grab it at speed**,
  split large collision objects, test at speed.
- Terrain 512–8192 supported; `LevelInfo.visibleDistance` is the global far cull; textures are
  PNG-only inputs cooked to DDS with mips at first load; `.dae` is compiled to **`.cdae` v31
  (msgpack header + body, optional Zstandard)** on first load — the format is partially
  documented and vanilla levels ship `.cdae` directly (small_island: 182 `.cdae` vs 147 `.dae`).
- No user-facing occlusion/portal system beyond distance culling and shape detail sizes
  ("Zones" are gameplay areas).

## Out-of-the-box levers (2026-08-15 research — things map makers rarely use, or that are new)

Found by reading BeamNG's shipped Lua (`G:\…\BeamNG.drive\lua\ge\…`) and current tooling, not
just the modding docs:

| lever | what it is | how we use it | status |
|---|---|---|---|
| **BeamNG's own headless mesh compiler & cache warmer** | `BeamNG.drive.exe -compilemeshes -compileMeshesCustomPaths "levels/<name>/"` (`lua/ge/extensions/util/compileMeshes.lua`) compiles every `.dae` → `.cdae` with a `collada_cache`; `-LoadSingleLevelForTextureCache -testlevel <name>` loads a level for 600 frames to cook textures/DDS; `-LoadSingleLevelForShaderCache` likewise for shaders | optional post-build **"Cook in BeamNG"** stage: the app locates the exe (Win/Linux; Steam library detection), runs it headless, and shows progress by watching `.cdae`/cache files appear — turns the "long first load" into a visible app step, and makes the hand-written `.cdae` writer unnecessary | **adopt** (A5) |
| **Per-level Lua hook + runtime prefab streaming** | `levels/<name>/mainLevel.lua` is loaded by freeroam (`onClientPreStartMission`), and `spawnPrefab(name, file, pos, rot, scale)` / `removePrefab(name)` (`lua/ge/ge_utils.lua:749/814`) load and unload `.prefab.json` groups at runtime; used by missions/traffic for collidable objects | **tile streaming**: ship building/prop-heavy tiles as packed prefabs plus a small `mainLevel.lua` that spawns tiles within R of the player vehicle each N frames and removes tiles beyond R+hysteresis; forest/terrain/roads stay resident. Measure hitching; keep as an opt-in "Streaming" mode for machines that cannot hold the whole city | **experiment** (E1) |
| **Vulkan renderer** | BeamNG's Vulkan path is "up to 2× faster in CPU-bound scenarios" (draw-call bound — exactly a dense city); Linux native is Vulkan-only | post-build advice screen recommends Vulkan (with the known GPU-bound caveat) and links the docs | **adopt** (advice) |
| **Shared `assets/` folder + `.link` files** (0.37+) | one physical asset tree, levels reference it via `.link` files | **district mode**: emit N overlapping district levels (each vanilla-sized) that share one `assets/cs2_<city>/` tree — a way for weak PCs to "experience the map" a district at a time without duplicating 5 GB | **experiment** (E3) |
| **HLOD cell proxies via inverse detail sizes** | Torque picks the largest detail size ≤ current pixel size; a shape with an *empty* `_a<big>` detail and a real `_a<small>` proxy draws only when small on screen | per 1 km cell, merge all buildings into one heavily decimated `lod_vertcol` proxy shape visible only at distance while individual buildings `nulldetail` out — Unreal-style HLOD in an engine that has none | **experiment** (E2), after A3 numbers |
| **Buildings as forest items** | forest accepts any shape (`TSForestItemData` + `collidable`); most CS2 buildings are uniformly scaled | opt-in "buildings in forest" mode: instancing + imposters + forest cell culling for 9k buildings; loses `instanceColor` (use paint variants) | **experiment** (E4) |
| **Quadric decimation instead of vertex clustering** | `pyfqmr` / `fast-simplification` (pip wheels; wrappers of Fast-Quadric-Mesh-Simplification, ~4× faster than MeshLab), UV-preserving fork exists | optional dependency `[lods]`: quadric ladder when installed, numpy vertex clustering otherwise; same `decimate.lod_ladder` API | **adopt** (B1) |
| **Material dedup by texture set** | BeamNG's #1 cost is unique materials/draw calls; CS2 ships 4,641 materials, many sharing the same texture set with different scalar params | bake collapses materials whose resolved (textures, alpha mode, doubleSided) tuple is identical into one BeamNG material; report the reduction | **adopt** (B2) |
| **`GroundCover` for CS2's shipped-nowhere grass** | billboard scatter per terrain layer, near-free | already in the strategy | adopt |
| **Engine-generated imposters for every shape** (`bb_autobillboard`) | see Performance research | already in the strategy | adopt |

Not applicable / rejected: pre-cooking DDS ourselves (BeamNG cooks PNG itself and the warmer
above does it headless); occlusion/portals (none exposed); a hand-written `.cdae` writer (kept
only as a fallback if `-compilemeshes` proves unusable); texture atlasing across prefabs (CS2
UVs tile; skip).

## Performance strategy (the point of the exercise)

Levers, in the order they matter for a 1.2M-instance city, each with where it lives:

| lever | mechanism | where |
|---|---|---|
| **Instancing + imposters for the ~1.1M small things** | forest (`.forest4.json`) for every tree and every uniform-scale prop/fence/poi; forest types ≤ ~5k; `windScale 0` (optional synthesised wind vertex colours); forest shadows toggle | app stage 5 |
| **Imposter + LOD ladder + cull for EVERY shape** | each `.dae` gets `bb_autobillboard<px>` (engine-generated billboard, buildings included), `nulldetail<px>` sized by object radius (tiny props vanish at ~10 px, buildings at ~2 px), and a numpy vertex-clustering LOD ladder (`_a<px>` levels ≈ 100 % / 25 % / 6 %, last level as `lod_vertcol` with vertex colours sampled from albedo); authored LODs (P1) replace generated ones when present | bake product `lods` → `.dae` detail nodes |
| **Density & toggles & presets** | deterministic hash subset for foliage density; per-kind/per-category toggles; Ultra/High/Medium/Low presets; budgets from the census before build | preflight + stage 5 |
| **Draw-call & object budgets** | 256 m tiles for networks/markings/areas/skirts, one primitive per material per tile (83k markings → a few hundred draw calls); TSStatic ≤ ~25k (vanilla max 22k), adaptive split for fat tiles; per-1 km SimGroups | app stages 4–5 |
| **Texture memory** | max-size cap (box-filter downsample) at copy time; `.color/.normal/.data` suffixes so DDS cooking picks the right format; dedup by sha; per-preset defaults (Ultra full, High 1024, Medium 512, Low 256) | stage 3 |
| **Collision (all on by default — see policy)** | Colmesh from the ~25 % LOD for buildings/props (thickness-checked, ≥ 0.3 m or fall back to `ColBox`), `Visible Mesh Final` for network tiles + skirts (roads must be exact), forest `collidable true` with trunk Colmesh for trees, box collision for fence/furniture tiles | stages 4–5 |
| **Shadows/decals** | `castShadows false` + `decalType None` on paint tiles; forest shadow toggle; lights capped, non-shadowing | stages 4–5 |
| **In-game distance control** | `LevelInfo.visibleDistance` + fog from `grading.fog`; detail px thresholds | stage 2 |
| **Ground detail cheaply** | `GroundCover` billboard grass per splat layer (CS2 ships no grass instances by design) | stage 5, toggle |
| **Load time** | many medium files not few huge; `ndarray.tofile` DAE; **"Cook in BeamNG" stage** runs BeamNG's own headless `-compilemeshes` + texture-cache warmer with progress, so the first in-game load is not the cook; Vulkan advice | stage 4, A5 |
| **Build time is allowed to grow** | user preference; `--jobs` per tile/shape | pipeline |

Measured, not assumed: stage 6 emits `report.json` (object counts by class, forest items/types,
tris per layer, texture bytes, DAE bytes, budgets ok/exceeded) and the in-game checklist records
FPS at the docs' profiling viewpoints (spawn, fast drive, dense vegetation, dense urban, night
with lights, water, worst vista); levers are tuned against those numbers.

## Collision policy (BeamNG is about collision — everything physical collides by default)

| what | default | how | option |
|---|---|---|---|
| network tiles (roads, rails, bridges, tunnels) | **on** | `Visible Mesh Final` — the driving surface must be exact | none (always) |
| skirts | on | `Visible Mesh Final` | off |
| buildings + stack sections | **on** | `Colmesh_<name>-1` from the ~25 % LOD, thickness-checked (≥ 0.3 m; thin walls thickened by normal offset or `Visible Mesh Final` fallback per shape) | Visible Mesh / off |
| props / poi (TSStatic or forest) | **on** | Colmesh from LOD when the shape is ≥ 0.3 m in every axis, else `ColBox` of the bounds | off per kind |
| trees (forest) | **on** | `collidable true`, trunk-only Colmesh (cylinder from the trunk sub-mesh bounds) so canopies don't stop cars | full mesh / off |
| fences & lane furniture | **on** | forest items: ColBox; baked tiles: per-tile box strips ≥ 0.3 m thick | off |
| paint — markings, area surfaces, instance decals | **off** | these are projections lifted 8–30 mm above a surface that already collides; a collider there is a lip that catches wheels, and CS2 has none either | **on** (`Visible Mesh Final`) for users who want it |
| water | engine | `WaterBlock`/`WaterPlane` buoyancy is the engine's | — |

The UI exposes this as a "Collision" group with the defaults above and a global "Collision
detail: exact / simplified / off" preset; the report lists collision triangle totals per class.

## Phases

Each phase ends with its offline gate + commit; in-game loads are the user's. B* = bake side
(engine-neutral), A* = application, P* = optional producer.

### B0 — Contracts, packaging, settle the docs
- Root `pyproject.toml` (setuptools, `package-dir` mapping, `numpy` required, `pywebview`
  optional extra `[gui]`, console script `cs2beamng`); `apps/cs2beamng/` skeleton; keep the
  existing `sys.path` tests working.
- `docs/bundle-schema.md` §13 "Derived layers (bake, consumer cache)" + `docs/schema/
  derived.schema.json` (`bakeVersion, csiibundleSchema, bundle{sha256(bundle.json),
  generatedAt}, options{tile,diagonal,holes,region,lods}, inputs{layerKey: fingerprint},
  products{name:[layerKeys]}, tiles`), `derived/README.md`. **Confirm `validate_bundle.py` and
  the producer's Clean/Manifest stages ignore an unknown `derived/` dir** (read
  `RipOrchestratorSystem.CleanStaleOutputs`, `BundleWriter` scan); else default `--derived-dir`
  to a sibling.
- **Settle the lift-ladder disagreement** (`decals.LIFT_LADDER` / consumer-guide §5: area 0.020
  above marking 0.014 vs schema §8b prose: area below) — order is CONTRACT; keep the code's,
  fix the prose, changelog entry. Fix `lanegraph.py`'s stale `owner` docstring.
- `tools/beamng/probe_ter.py` (commit the orientation probe); amend CLAUDE.md, PLAN.md §9,
  `tools/README.md`; copy this plan to `docs/plans/beamng-consumer-plan.md`.
- **Gate:** `pip install -e .[gui]` works; `python -m csiibundle.bake docs/example-bundle`
  writes `derived.json` (zero products) and reports `STALE` after touching an input;
  `test_bundle_contract.py` green.

### B1 — `csiibundle` additions (bpy-free, each with a `tools/test_*.py`)
- `glb.py`: accept `COLOR_0`; `lod_level(node_name)`. New `glbwrite.py` (`write_glb`;
  parse∘write = id). `png.py`: `encode_gray/encode_rgba` (zlib; lift from
  `make_example_bundle.py`); `downsample_box(rgba, factor)`.
- `drape.py`: `TriangleSoupSurface(verts_zup, tris, cell=8.0)` (CSR grid; `drop/height` +
  `drop_many/height_many`), `drape_blocks(blocks, road, ground)`. Gate `test_soup_surface.py`:
  agrees with the *independent* oracle in `test_drape_pick.py` (its own vertical intersector)
  and with `HeightfieldSurface` on a meshed heightfield; scalar ≡ bulk.
- `lanes.py`: `part_source(nodes)` (numpy port of the adapter's `_load_lane_source`).
- `instances.py`: `skirt_rings(verts)` (bottom ring by geometry), `drop_skirt`, two-pass
  parent-flags for extension records; `density_keep(record, fraction)` (stable hash, weighted
  by size/state); `object_radius(mesh_bounds, scale)`.
- `terrain.py`: `hole_mask(bundle_dir)` → `(contain, deep_touch, band_count)` at face res.
- `splat.py`: `dominant_layer(...)` (argmax composite, roadbed own index).
- `water.py`: `bodies(levels, heights, cell, origin, downsample=8)` (BFS components).
- `materials.py`: `resolve(entry, id)` → engine-neutral resolved dict.
- New `decimate.py`: `lod_ladder(mesh, targets=(0.25, 0.06))` with two backends behind one
  API — quadric (`pyfqmr` / `fast-simplification`, optional extra `[lods]`, UV-preserving) when
  importable, else numpy `vertex_cluster` (grid snap, weld, drop degenerates, cluster on
  (pos, uv-island)); report tri ratios; `vertex_albedo(mesh, textures)` samples each vertex's
  albedo for the `lod_vertcol` last level.
- New `collision.py`: `thickness(verts, tris)` (min extent along principal axes / per-shell
  test), `colmesh_from_lod(mesh, min_thickness=0.3)` → mesh or `None`, `colbox(bounds)`,
  `trunk_cylinder(tree_mesh)` (from the trunk sub-mesh bounds), `wind_vertex_colours(mesh)`
  (height/radius heuristic → R/G/B per the BeamNG convention).
- **Gate:** all new tests green; existing unchanged.

### B2 — Bake: resolved materials, paint variants, LODs
- `bake/materials.py` → `derived/materials.json`, split mask maps
  (`<sha>_metallic/_roughness/_ao.png`), unpacked normals if any, `paint_variants.json` +
  tinted albedos (cap per prefab, report); **material dedup** — entries whose resolved
  (texture set, alpha mode, doubleSided, factors) tuple is identical collapse to one canonical
  material id (`derived/materials.json` carries the alias map; report the reduction).
- `bake/lods.py` → `derived/lods/<assetId>_LOD{1,2}.glb` for every mesh state (skipped when
  the producer shipped `_LOD<n>` nodes), `--jobs`.
- **Gate:** `test_bake_materials.py`; every LOD glb parses; tri-ratio report; wall-clock.

### B3 — Bake: draped markings + lane geometry
- `bake/markings.py`: soup surface from all `networks/*.glb` + heightfield fallback; decal parts
  via `lanes.bend_part` → `decals.station_*/lattice` → `drape.drape_blocks` with `LIFT_LADDER`;
  wear tint; **instance decals** draped (synthetic quad lattice, `half = decalHeight/2`, target
  by `decal_layers`); tiles; counters.
- `bake/lanegeometry.py`: rigid **level** tiles → `instances.jsonl`; rest baked; per-category
  toggles; vertex budget.
- **Gate:** counters within a few % of the Blender harness log on the standard region; Blender
  adapter gains `--derived` (imports `derived/markings/`) and the `ab_render.py`/scene-digest
  A/B is the oracle; full-city bake wall-clock recorded (fallbacks: `--jobs`, embree,
  Blender-injected surface).

### B4 — Bake: areas, skirts, clip cells
- `bake/areas.py` (`areas.build_pieces` over `HeightfieldSurface(diagonal)`), `bake/skirts.py`
  (`wants_skirt`, `__base.glb`, drop bottom ring), `bake/terrainmask.py` (`terrain.hole_mask` →
  `clip_cells.png` 0/128/255), `--diagonal nw_se|ne_sw|envelope` (default envelope until
  measured in-game).
- **Gate:** `test_clip_cut.py` semantics; skirt/area invariants; Blender `--derived` renders.

### A0 — Application shell: pipeline runner, progress, UI, settings, CLI
- `apps/cs2beamng/{__main__,cli,app}.py`; `pipeline/{runner,stage,progress,history,census}.py`
  (stage protocol `declare(total)/advance/log/check_cancel`; worker process + queue; heartbeat;
  ETA model; per-stage history); `settings.py` (schema, defaults, per-OS path detection for
  ModsData and BeamNG userfolder, validation); `server.py` (ThreadingHTTPServer, SSE `/events`,
  JSON API `/api/{census,settings,start,cancel,report,open-folder}`); `ui/` (`index.html`,
  `app.css`, `app.js`: preflight card, settings panel, stage list with bars + ETA + counters,
  log pane, results/report screen; dark theme; light via `prefers-color-scheme`); `window.py`
  (`pywebview` if importable, else `webbrowser.open`).
- `cs2beamng build <bundle> --out … [--settings file] [--set key=val …]` headless with a
  terminal progress renderer (same stage protocol).
- **Gate:** run against `docs/example-bundle` end-to-end with a stub "hello" stage; UI shows
  progress/ETA/cancel; cancel mid-stage returns within 1 s; kill the worker → UI reports it,
  window stays responsive; works from `python -m cs2beamng` on this machine (Windows) and via
  CI on Linux/mac (headless CLI).

### A1 — Terrain, water, sky, spawn → first loadable level
- `stages/terrain.py` (`csiibeamng/ter.py`): **lossless** heights (`maxHeight = 65536 ×
  elevationScale`, `position = [−mapSize/2, −mapSize/2, elevationOffset]`, rows flipped,
  `squareSize = 3.5`, `--terrain-upsample 2`), layer map = `splat.dominant_layer` + holes from
  `clip_cells.png` (`--holes contain|majority`), TerrainMaterials v1.5 from `terrain/surfaces/`
  + one `TerrainMaterialTextureSet`, roadbed material; `stages/water.py` (`water.bodies` →
  `WaterPlane`/`WaterBlock`s, appearance from `water.Appearance`); `stages/scene.py`
  (`LevelInfo` incl. `visibleDistance`/fog, `TimeOfDay`, `ScatterSky` from `colour.Lighting`,
  `SpawnSphere` on a `lanegraph.masters ∩ drivable` car lane, `info.json`).
- `stages/validate.py`: JSON-per-line, unique ids, files exist, `.ter` sanity, **`.ter` height
  under `OnGround` buildings ≈ `instances.ground_level` within 0.5 m**.
- **Gate:** validator green; **user in-game load #1** (terrain + water; region then full);
  optional Lua `getTerrainHeight` probe settles the diagonal.

### A2 — DAE writer, materials/textures, networks + derived tiles
- `csiibeamng/dae.py`: COLLADA 1.4.1 (`Z_UP`, `<unit meter="1">`, `(−gx, gz, gy)` local
  geometry, shared sources, one `<triangles material=sym>` per run; **vanilla hierarchy**
  `base00 → { bb_autobillboard<px>, nulldetail<px>, start01 → <name>_a<px> per LOD level,
  Colmesh_<name>-1 / ColBox }` with px thresholds sized by object radius; last level bound to
  `lod_vertcol` with `COLOR` source; `ndarray.tofile` formatting; ids prefixed). Verified
  against the shipped tree/trailer `.dae` structure read this session.
- `stages/materials.py`: `derived/materials.json` → v1.5 `main.materials.json` per art
  folder, textures hardlinked/copied with suffixes **after the max-size cap**, alpha/doubleSided/
  emissive/`colorPaletteMap`, paint-variant materials.
- `stages/networks.py` (segment glbs merged per tile, adaptive split, `Visible Mesh Final`,
  centreline-form via `networks.ribbon`); `stages/derived.py` (derived tiles → DAE + TSStatic;
  paint tiles `collisionType None` + `castShadows false` by default, `Visible Mesh Final` when
  the paint-collision option is on; skirts collide; furniture tiles get box-strip colliders;
  `lanegeometry/instances.jsonl` → forest with ColBox).
- **Gate:** `test_dae_writer.py` round-trip; validator (DAE parses, symbols ⊆ `mapTo`);
  **user in-game load #2** (region): roads drivable, markings clean, kerb normal polarity
  (else flip G), first-load time recorded.

### A3 — Instances → forest / TSStatic, lights, groundcover, density
- `stages/shapes.py` (every referenced mesh state → `.dae` with imposter + LOD ladder +
  `nulldetail` + collider per the collision policy (`collision.colmesh_from_lod` →
  thickness check → `ColBox` fallback; trees: trunk cylinder + optional wind vertex colours)
  + paint variants, `--jobs`); `stages/objects.py` (stream instances; `should_draw`;
  density_keep; forest vs TSStatic rule; `frames.to_zup_transform`; `instanceColor/1/2`;
  `collisionType` per class; forest `collidable`; spill to forest over budget; SimGroups per
  1 km); `stages/lights.py` (`lights.photometric_lights` → Point/SpotLight, non-shadowing,
  cap); `stages/groundcover.py` (`GroundCover` per splat layer with the bundle's ground
  textures as billboards, toggle).
- **Gate:** `report.json` vs budgets; validator; **user in-game load #3** (region → full):
  placement, tree states, tints, lights, grass; **FPS at three viewpoints** recorded → tune
  thresholds/density defaults; repeat with props/foliage/lane-furniture toggles off to confirm
  the low-end path.

### A4 — Performance tuning pass + AI roads (optional flag)
- Iterate the levers against A3's numbers (LOD ratios, cull px, tile size, forest shadows,
  texture cap defaults per "quality preset": Ultra/High/Medium/Low presets in the UI).
- `stages/airoads.py` (`--ai-roads`): hidden `road_invisible` `DecalRoad`s from
  `lanegraph.masters ∩ drivable(car)`; count measured in-game.
- **Gate:** presets documented with measured FPS/load-time on this machine; user sign-off.

### A5 — "Cook in BeamNG" stage (load time) + advice screen
- `stages/cook.py`: locate the BeamNG executable (settings override; Steam library detection
  via `libraryfolders.vdf` on Win/Linux; skip on macOS), run
  `BeamNG.drive.exe -compilemeshes -compileMeshesCustomPaths "levels/<name>/"` and then
  `-LoadSingleLevelForTextureCache -testlevel <name>` headless as subprocesses with the app's
  progress contract (progress = `.cdae` count / `.dae` count and cache-folder growth; timeout +
  cancel kill the process). Off by default until proven, then default on when the exe is found.
- Post-build **advice screen**: Vulkan recommendation, expected first-load time (measured
  cook time or estimate), settings that trade fidelity for FPS, where the level is.
- Fallback only if `-compilemeshes` proves unusable headless: `csiibeamng/cdae.py`, reader-first
  against the shipped v31 files, round-trip validated.
- **Gate:** cook runs on this machine end-to-end; first-load time with and without cook
  recorded; validator green.

### E — Experiments (each behind a flag; each is a measured comparison, kept only if it wins)
- **E1 tile streaming**: `stages/streaming.py` writes building/prop tiles as packed
  `.prefab.json` files + `levels/<name>/mainLevel.lua` (spawn within R of the player vehicle
  every N frames, remove beyond R + hysteresis, forest/terrain/roads resident); measure hitch
  (frame-time spikes) vs resident; ship as opt-in "Streaming" mode if hitching is bounded.
- **E2 HLOD cell proxies**: per-1 km merged `lod_vertcol` proxies with inverse detail sizes;
  measure FPS at the "worst vista" viewpoint.
- **E3 district mode**: N overlapping vanilla-sized district levels sharing one
  `assets/cs2_<city>/` tree via `.link` files.
- **E4 buildings-as-forest**: opt-in; compare FPS and load time against TSStatic.
- **E5 markings as `DecalRoad`s** (user question 2026-08-15: is draping right?): on one region,
  emit each marking as a `DecalRoad` (`overObjects`, CS2 texture, `textureLength` = repeat)
  instead of the pre-draped tile; compare FPS, load time and look at grazing angles / distance
  against the tile path. Texture-integration (unique-UV overlay atlas) is rejected on texel
  budget (~2.5 GB at 5 cm/texel) and the lack of a proven second-UV overlay stage; the tile
  path's 14 mm lift is invisible at eye height, and its far-distance shimmer is handled by a
  `nulldetail` cull on marking tiles.
- **Gate:** each experiment has a before/after table in `docs/plans/beamng-consumer-plan.md`;
  losers are removed, not left as dead flags.

### P1 — Producer (optional): LOD chain rip — gated by measured value
- `MeshRipSystem` follows `LodProperties` → `<assetId>_LOD<n>` (GltfWriter already names
  them); Settings toggle **`Rip LODs`, default off**; dialog reports the mesh-stage delta. Bake
  skips generated LODs when authored ones exist. Only if A4 shows generated LODs are visibly
  worse than the cost of the rip delta.

## Reuse map (call, don't rewrite)

`csiibundle.reader`, `frames`, `glb.parse_glb`, `instances.{mesh_candidates, growth_scale,
should_draw, wants_skirt, skirt_asset_id, is_paint, decal_layers, ground_level,
instance_matrix}`, `materials.{wire_plan, shader_only_kind, detect_normal_packing,
unpack_normal}`, `tints.{color_set, apply_reference}`, `lights.*`, `lanegraph.*`, `bezier`,
`lanes.{bend_part, rigid_tile_matrix, tile_count}`, `decals.{LIFT_LADDER, drape_lift,
station_count, lattice, quad_info}`, `drape.{ProjectionSurface, HeightfieldSurface,
drape_block}`, `terrain.clip_grid`, `areas.{build_pieces, memoised_sampler, surface_material}`,
`splat.Splat`, `png`, `water.Appearance`, `colour.{load, Lighting, Sky}`;
`tools/validate_bundle.py` (preflight), `tools/test_drape_pick.py` (oracle); adapter reference
behaviour: `_ProjectionSurface` (L1174), `_load_lane_source` (L1304), `build_lanes` (L1479),
`build_terrain` (L2265), `build_areas` (L2618), instance loop (L3866); `IO/Png.cs`.

## Budgets

- Bake full city ≤ ~15 min single process (markings dominant), RSS < 8 GB; builder full city
  allowed to be long (user preference) but must show ETA; report DAE/texture bytes.
- BeamNG (calibrated on vanilla maxima: 663k forest items, 22k TSStatics, 12.7k DecalRoads,
  2.6k shapes): TSStatic ≤ ~25k, forest items unbounded in practice, forest types ≤ ~5k, shape
  files ≤ ~10k, textures ≤ ~15k, lights ≤ 500 (default off), WaterBlocks ≤ 200, tiles ≤ ~1.5k
  per layer, hidden AI DecalRoads ≤ ~15k.

## Verification (end-to-end)

1. Offline, every phase: `tools/validate_bundle.py` (bundle unchanged), bake gates, Blender
   `--derived` A/B for markings/areas/skirts, `cs2beamng build docs/example-bundle` in CI on
   three OSes, `stages/validate.py` on every build (incl. the `.ter`-under-buildings check),
   UI hang/cancel tests (kill worker, long op, cancel).
2. In-game (user): loads #1–#3 with per-phase checklists; FPS at three viewpoints per preset;
   results logged in `docs/plans/beamng-consumer-plan.md`.
3. Staleness discipline unchanged: bundle changes only by re-rip; bake refuses an invalid
   bundle and reports `STALE`; app re-bakes only stale products.

## Risks & fallbacks

- Soup surface too slow → `--jobs`, embree (`trimesh`+`pyembree`), Blender-injected surface.
- Generated LODs ugly on foliage cards → forest imposters cover distance; LOD generation
  off for trees by default; P1 authored LODs.
- Pre-draped markings z-fight → material z-bias/translucent; `DecalRoad` experiment.
- COLLADA size/first-load → adaptive tiles, precision, no Colmesh; `.cdae` writer last.
- Palette tint semantics differ → baked variants for TSStatics too.
- `pywebview` missing/broken on a platform → browser fallback is the same UI.
- Object count stalls load → widen forest rule, presets.

---

## Log

| date | phase | result |
|---|---|---|
| 2026-08-15 | B0 | Framework landed: root `pyproject.toml` (`pip install -e .` → `csiibundle`, `cs2beamng`, scripts `cs2beamng` / `csiibundle-bake`), `csiibundle/bake/` (registry, `derived.json` manifest with per-product fingerprints, progress protocol, runner, CLI, `locate`), contract §13 + `docs/schema/derived.schema.json`, `tools/test_bake_manifest.py` (16 checks green), `tools/beamng/probe_ter.py` committed, lift-ladder prose and `lanegraph` docstring corrected, CLAUDE.md / PLAN.md §9 / tools/README.md amended. Confirmed `CleanStaleOutputs` and the manifest scan touch declared layers only, so `derived/` inside the bundle is safe. |
| 2026-08-15 | B1 | `csiibundle` additions landed, all bpy-free: `glb` reads `COLOR_0` + `lod_level`; new `glbwrite` (parse∘write = id, §2.2 mirror/winding); `png.encode_gray/encode_rgba/downsample_box`; `drape.TriangleSoupSurface` (CSR grid, `drop_many`/`height_many`) + `drape_blocks` — exact against a brute-force scan, the heightfield, and `test_drape_pick.py`'s intersector on 181 real segments; **full city: 7,289 segment meshes / 9.3M tris load 8 s, index 4 s, 1M drops 35 s**; `lanes.part_source`; `instances` skirt rings/drop/parent flags, `density_keep` (stable, monotone, state-weighted), `object_radius`; `terrain.hole_mask`; `splat.dominant_layer` (composite-share argmax + roadbed; 1.6 s full city); `water.bodies` (4-connected, 8× sim grid; 99 bodies on the reference city, the sea+river body spans 508–527 m → A1 splits by level band); `materials.resolve/resolve_all/texture_set_key` (**4,641 → 1,409 distinct texture sets**); new `decimate` (cluster backend; quadric via optional `pyfqmr`; 182k-tri building → 24 %/6 % in 11 s) and `collision` (PCA thickness, 0.3 m ColBox padding, trunk cylinder, wind colours). Gates: `test_glbwrite`, `test_soup_surface`, `test_lod_collision`, `test_b1_readers` + the whole existing suite green. |
| 2026-08-15 | B2 | Bake products `materials` and `lods` landed (`csiibundle/bake/{materials,lods}.py`, registry imports product modules lazily, `--opt key=value` passthrough). Full city `materials`: **7.5 min** — 4,641 → 1,409 texture sets (3,232 aliases), 845 mask maps split, 851 normals checked / 0 packed, paint variants for 3,800 prefabs (trees k-means to 4 of up to 2,422 distinct sets), 660 tinted tree albedos in linear light. `lods`: 427 prefabs of a 300 m region in 10 s on 6 workers, ratios 0.25 / 0.07, LOD2 carries vertex albedo. Gate `test_bake_products.py`. Decision recorded: tinted albedos baked for forest-bound kinds only (`paint_kinds`, default trees); buildings/props ride the engine palette path first. |
| 2026-08-15 | B3 | Bake products `markings` and `lanegeometry` landed (`bake/{roads,tiles,markings,lanegeometry}.py`). **Full city `markings`: 90 s** — road surface 8 s (7,289 meshes / 9.3M tris), bend 45 s (82,600 lanes → 82,181 blocks, 621 dropped by clip), 17,871 instance decals subdivided ≤ 2 m and draped by `decalLayers`, drape 14 s (1.81M queries: 1.50M draped, 325k grounded, 13.7k missed = 0.75 %), 1,114 tiles / 1.82M verts / 84 MB. Standard region: 11 s. **`lanegeometry`**: 35,895 lanes → 104,279 level rigid placements (64k uniform → forest-able) + 3,039 sheared tiles baked + 33.2M baked triangles (rails/sleepers 'lane' 20M, fences 11M, cables 2M) in 695 tiles / 1.4 GB, 2m22 — reported per category; the app's toggles/`lane_max_tris` act on it, A4 may decimate. Per-lane colorSet not applied to markings (recorded). Gate extended in `test_bake_products.py`. |
| 2026-08-15 | B4 | Bake products `areas`, `skirts`, `terrainmask` landed (+ `bake/ground.py` envelope heightfield: MAX for paint, MIN for skirts). Region: 86 areas → 30k conformed pieces in 17 s (paint sits 0.02 m above the heightfield, faces up — the Z-up face order IS the bundle-frame order, measured); 40 skirts placed with 7,057 bottom vertices dropped to the ground in 15 s. Full city `terrainmask`: 44 s — 6,923 contained faces (holes), 10,201 deep-touched, 573,459 samples under a shallow band (worst clamp 0.79 m). Two winding rules pinned by measurement: geometry that reaches the bundle frame through the §2.2 mirror (part sources, mirror-placed skirts) must reverse its face order; areas from `build_pieces` must not. `TileSink` clears its product's stale tiles first. Gate extended; **all seven products now bake**: full city ≈ 7.5 + ~4 + 1.5 + 2.4 + (areas full: not yet timed) + 0.7 min. |
| 2026-08-15 | A0 (scaffolding, session 1) | Written and smoke-tested headless, NOT yet the UI: `apps/cs2beamng/settings.py` (SCHEMA of every knob, presets ultra/high/medium/low, load/save `~/.cs2beamng/settings.json`, coerce/validate, per-OS bundle + BeamNG userfolder + exe detection), `pipeline/progress.py` (QueueProgress → queue messages, Heartbeat), `pipeline/history.py` (per-stage rate EMA + shipped defaults), `pipeline/census.py` (bundle counts, derived freshness, per-stage estimates), `pipeline/worker.py` (spawned process runs `stages.all_stages()`, reports stage/phase/adv/log/hb/done), `pipeline/runner.py` (`Runner` state with per-stage + overall ETA, cancel with hard-kill watchdog, dead-worker detection; `build_headless` terminal renderer), `stages/__init__.py` (Stage/BuildContext, lazy stage list), `stages/bake.py` (real: runs the csiibundle bake for the products the settings need), `stages/validate.py` (writes `cs2beamng_report.json`; defers to `beamng/validate_level` once A1 writes it). `cs2beamng build docs/example-bundle --out <dir>` runs green. The rest of A0 landed in session 2 (next row). |
| 2026-08-15 | A0 (**done**, session 2) | `server.py` (stdlib `ThreadingHTTPServer`, SSE `/events` — `state` every 0.5 s without the log + `log` deltas — JSON API `/api/{state,settings,preset,census,start,cancel,report,open,browse,quit}`, all token-gated: `X-Token` header / `?t=` for EventSource, malformed bodies → 400), `ui/{index.html,app.css,app.js}` (no build step, dark + `prefers-color-scheme` light: Bundle card with census, derived-cache freshness chips and expected time; Settings with presets and every SCHEMA knob grouped by `settings.GROUPS` incl. dependency dimming; Build card with overall bar/elapsed/remaining/expected/worker-heartbeat, planned stage list before Start, per-stage bars with phase + counters + ETA, cancel; Result card with per-stage table + validation problems + open-folder buttons; log pane), `window.py` (pywebview window with native folder picker and close-while-building confirm; browser fallback), `app.py:launch` (cancel + kill on exit), `__main__.py`, `settings.detected/schema_for_ui`, `stages.STAGE_MODULE_ENV` hook, `QueueProgress.stage_progress` + bake-stage proxy (stage fraction weighted by per-product estimates), `history.stage_estimate` (bake = Σ `bake/<product>`; per-product learning through `result["_history"]`), census total over the stages that exist, worker writes `cs2beamng_report.json` last, worker is **not** a daemon (the lods pool needs children) with `Runner.kill()`. Gate `tools/test_app_shell.py`, 61 checks (cancel in 0.05 s, hard-kill of a stubborn worker in ~2 s, dead worker → failed with `state()` never blocking, raising stage, child-pool stage, bake cancel raises `Cancelled`, token/API/SSE/400/501/409, headless build) + `.github/workflows/ci.yml` (win/linux/mac × py3.11/3.13). **Driven from a browser against the real bundle**: settings commit → census re-estimate (33 m → 16 m 30 s with a 200 m region), Build (materials 4,641 → 1,409 sets, 604 tinted albedos for 75 tree prefabs ≈ 5.7 min; region markings 7 tiles, lanegeometry 3, areas 5, skirts 5; terrainmask 44 s), Cancel on a real bake returned in 5 s, second build re-baked only `lods` (276 prefabs / 15 s on a pool) and reported all seven fresh; native window launch/quit verified. Three bugs the real run found and fixed: cancel ended as DONE (bake reports cancel as an outcome), estimate said 7 h 19 m (mixed units / learnt rate), daemon worker could not spawn the lods pool. Deferred: the B3 Blender `--derived` A/B oracle. |
| 2026-08-15 | A0 review pass | Two Sonnet reviewers + the finders of an aborted top-model `/code-review` (never again on the top tier — it hit the session limit) → 20 findings applied: **runner** — repeated cancel no longer postpones the hard-kill deadline; the reader thread is bound to its own queue/process (a stale thread cannot hijack the next build) and drives the watchdog itself (no caller has to poll `state()`); hard cancel is two-step (a `hard_cancel` event the worker's heartbeat thread honours by terminating its own pool children via `active_children()` and exiting, then `terminate()` 2 s later); `Runner.stop(grace)` is the one cancel→wait→kill path (app exit, Ctrl-C, headless `finally`); `kill()` joins outside the lock; hard-killed/crashed stages get `finishedAt`; a worker exit after a cancel request is "cancelled", not a crash. **worker** — ignores SIGINT (Ctrl-C reaches the child console-wide) so cancel always flows through the event; removes the previous `cs2beamng_report.json` at start so a killed build is never explained by an older report; heartbeat also exits on parent death (no orphan after a window crash). **server** — racing `/api/start`s → one 200, rest 409 (was 500); rejected POSTs close the connection (an unread body no longer poisons keep-alive); `stop()` idempotent across threads; `apply_preset` save errors reported; `report()` never falls back to a file once a build ran this session; planned-stage preview memoised and via public `Runner.plan()`. **census** — bundle size by `scandir` + cached per bundle.json mtime (was 1.3 s of `getsize` per settings change); `all_stages()` resolved once per process. **settings** — coercion errors name the key. **bake stage** — no `finish()` on a cancelled/failed bake (bar no longer flashes 100 %); per-product rates learnt only from full-city builds (a region build taught rates several × too fast). **window** — no native confirm inside `closing` (cocoa deadlock; double dialog after `/api/quit`): close-while-building cancels and closes; the page's Quit confirms. **UI** — Result card also on a page opened after the build ended; a failed Start restores the previous log; coercion errors highlight their field; stale `/api/settings` responses ignored (ordering token); log trimming only while following. Gate now 69 checks incl. repeated cancel, hard cancel with a live pool (children reaped, verified 0 stray processes), racing starts, keep-alive after a rejected POST. |
| 2026-08-15 | A1 (built; **awaiting in-game load #1**) | `beamng/{frame,ter,level,validate_level}.py` + `stages/{terrain,water,scene}.py` (commit 6762d0d). Terrain: lossless 4096² `.ter` (`maxHeight = 65536·elevationScale`, `position.z = elevationOffset`, row 0 south, `position = [originWorld.x, originWorld.z − 4095·3.5, offset]`, `squareSize 3.5`), 572,940 samples clamped to shallow bands + 680,728 roadbed samples sunk 0.15 m, 8 materials (`rock dirt grass extra1..4 roadbed` from `splat.dominant_layer` + surface id 1), 6,923 tunnel holes (`contain`; face → its SW sample's layer byte), TerrainMaterials v1.5 pointing base/macro/detail at the theme textures (`BASE_TILE_M 64`, `MACRO_TILE_M 24`, vanilla distances) — **3 s**. Water: 71 bodies → level bands (1 m) → components → `WaterBlock`s (top at `position.z`, extends down `scale.z`), the largest edge-touching band → `WaterPlane`; appearance from `rasters.water`; `csiibundle.water.coarse_grid` now decides wet per texel (¼ of texels wet ⇒ cell wet) because averaging land into shoreline cells made narrow rivers dry — **28 s** full city. Scene: `LevelInfo` (`view_distance` setting 4000 m default, fog `1/meanFreePath`), `TimeOfDay` (12:51, lat/lon), `ScatterSky` az 206.9° / el 63.7° from the sun vector (**assumption to verify in-game: BeamNG azimuth is clockwise from north**), `SpawnSphere` on the master car lane at the city's median (`heading_matrix` reproduces vanilla matrices exactly — vehicles face the sphere's +Y), hillshade preview, `info.json` — 5 s. **Validator green; physical check on the real city: 1,313 OnGround buildings, 99.5 % within 0.5 m, median \|Δz\| 1.4 cm, p95 3.2 cm** — orientation, origin and scale confirmed against real data. Gate `tools/test_a1_level.py` (36 checks) in CI. Full city built to `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\cs2_city` for load #1. |
