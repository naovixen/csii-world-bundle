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

**From A3 onwards every phase carries a PARITY GATE, before the in-game load.** A2 ended with a
week lost to it: the terrain cut was "fixed" seven times by reasoning about screenshots
(containment scoring, a rim snap, a sub-cut, a cliff lift, rim skirts, a band lowering, a margin
ring), each fix causing the next artifact, until the game's own implementation was read and gave
the rule in half an hour — a stencil prism — and showed the producer had matched it all along
(audit §46). The parity gate is what stops that recurring, and it is not satisfied by "it looks
right in Blender": the harness is a consumer with the same licence to be wrong, and it was
carrying the very defect a screenshot was blaming on the app.

For each phase, before it is called done:

1. **Name the game's counterpart system and read it** — `tools/decomp`, not IL, not our own docs
   (three of our documented claims have failed a re-check). For A3 that is `BatchInstanceSystem`
   / `ObjectGeometrySystem` / the tree state and colour-variation paths; for A4 the LOD and
   culling thresholds; for A5 nothing (it is a build step, not a fidelity one) — say so explicitly
   rather than skipping the question.
2. **Write the rule matrix** into `docs/cs2-api-audit.md`: game rule → our behaviour → verdict
   (`match` / `divergence` / `approximated` / `deliberate deviation`), one row per rule.
3. **Build a predicate you can sample** where the phase's output admits one, and report the
   agreement as a number — `tools/check_terrain_vs_game.py` is the model (it asks the game's own
   question at random points and compares it with what the level draws). A number beats a
   screenshot, and it is what makes a regression visible later.
4. **Declare and count every approximation** that remains, in the code that makes it *and* in the
   build report. **No new tuned constant ships without either a source citation or a counted
   approximation beside it** — an undeclared approximation is indistinguishable from a bug the
   moment its artifact appears.
5. **Delete what the game's rule replaces.** An approximation left in the tree beside the exact
   rule is the next session's confusion.

`docs/plans/game-parity-sweep.md` carries the same method plus the standing backlog of areas
(including the two the terrain still owes: the ground-boundary dither and ground-mesh material
parity). A phase's parity work goes in that backlog if it cannot be finished inside the phase —
it does not get dropped.

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
- **Parity gate (before the load):** read `BatchInstanceSystem.UpdateObjectInstances`,
  `ObjectGeometrySystem` and the tree growth/season + colour-variation paths; matrix rows for LOD
  selection, which mesh state a tree draws, sub-object composition and how `instanceColor*` is
  derived. Sampleable predicate: for N random instances, does the state/LOD/tint we place match
  what the game would choose for that entity? Anything left approximated is counted in
  `report.json`.
- **Gate:** `report.json` vs budgets; validator; **user in-game load #3** (region → full):
  placement, tree states, tints, lights, grass; **FPS at three viewpoints** recorded → tune
  thresholds/density defaults; repeat with props/foliage/lane-furniture toggles off to confirm
  the low-end path.

### A4 — Performance tuning pass + AI roads (optional flag)
- Iterate the levers against A3's numbers (LOD ratios, cull px, tile size, forest shadows,
  texture cap defaults per "quality preset": Ultra/High/Medium/Low presets in the UI).
- `stages/airoads.py` (`--ai-roads`): hidden `road_invisible` `DecalRoad`s from
  `lanegraph.masters ∩ drivable(car)`; count measured in-game.
- **Parity gate:** the tuning levers are OURS (BeamNG-side), so the matrix here records which
  numbers are engine tuning and which are fidelity — and no fidelity number moves to buy FPS
  without saying so in the preset table. Where the game has its own answer (LOD switch distances,
  cull thresholds, forest density), read it and state the divergence rather than tuning past it.
- **Gate:** presets documented with measured FPS/load-time on this machine; user sign-off.

### A5 — "Cook in BeamNG" stage (load time) + advice screen
- `stages/cook.py` — **built 2026-08-16 (pulled forward after load #9's 210 s stall)**: locates
  the engine binary (`beamng_exe` or the Steam libraries; `Bin64/BeamNG.drive.x64.exe`, the
  root exe is a launcher that returns at once), runs `-headless -nosteam -compilemeshes
  -compileMeshesCustomPaths "levels/<name>/" -compileMeshesNoCheck -compileMeshesNoCache`
  as a subprocess with the app's progress contract (progress = `.cdae` count beside the
  `.dae`s; startup grace 180 s, stall 600 s, cancel kills), skips with a reason when no exe /
  not under a userfolder / BeamNG already running. Default on. `-LoadSingleLevelForTextureCache`
  is NOT in 0.39's shipped Lua (`extensions/test/singleLevel.lua` absent) — textures still cook
  on the first load, on demand, ~2 min on the full city and no stall.
- Post-build **advice screen**: Vulkan recommendation, expected first-load time (measured
  cook time or estimate), settings that trade fidelity for FPS, where the level is.
- Fallback only if `-compilemeshes` proves unusable headless: `csiibeamng/cdae.py`, reader-first
  against the shipped v31 files, round-trip validated.
- **Parity gate:** none — this phase changes no geometry, material or placement, only how long
  the first load takes. Recorded here so the question is answered rather than skipped.
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
  losers are removed, not left as dead flags. An experiment that changes what is DRAWN (E5
  markings as `DecalRoad`s, E4 buildings-as-forest) also carries the parity check for that layer —
  a faster wrong picture is still wrong.

### P1 — Producer (optional): LOD chain rip — gated by measured value
- `MeshRipSystem` follows `LodProperties` → `<assetId>_LOD<n>` (GltfWriter already names
  them); Settings toggle **`Rip LODs`, default off**; dialog reports the mesh-stage delta. Bake
  skips generated LODs when authored ones exist. Only if A4 shows generated LODs are visibly
  worse than the cost of the rip delta.
- **Parity gate:** a PRODUCER change, so it needs the stricter form — matrix against
  `LodProperties`/the game's own LOD selection, and "built, awaiting in-game re-rip" until Naomi
  has ripped. A producer change cannot be validated against the bundle on disk (CLAUDE.md's
  staleness non-negotiable).

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
| 2026-08-15/16 | A1 **ACCEPTED (in-game loads #1–#5)** | Loads by Naomi, each fixing one thing (commits 50a08bb, 7d2a8ea, 274d5ae, 9cfcc00): **(1)** dark at load — BeamNG `TimeOfDay.time` is `(hour/24 − 0.5) % 1`, **noon = 0** (`environment.lua`), the naive fraction put 12:51 at 01:20; `tod_hour` setting added. **(2)** untextured — v1.5 TerrainMaterial needs all 15 `*Tex` slots (5 maps × 3 tiers); an absent slot logs `Missing Terrain texture: /`; neutral AO/height/roughness maps written. **(3)** purple/green per-pixel noise close-up — NOT textures (BeamNG's cooked BC7 cache decoded fine) but a **name collision**: terrain material internal names are a global, case-insensitive registry and loose `levels/` folders are always mounted, so the stale `cs2_city` folder's `grass` shadowed `cs2_city_a1`'s (and Small Island's `Grass` got ours: `dont have required size of 2048`); fixed by prefixing every internal name and texture with the level name; one file per tier per map like vanilla; explicit tier sizes (base 128 m, macro 40, detail 4). **(4)** near ground black, mid splotchy — CS2 ground albedo ≈ 0.06 linear vs BeamNG's ~0.2: linear gain calibrated on grass to 0.16 (×2.57 here), `terrain_brightness` setting; macro/detail strengths 0.6/0.85 since our base tier is a 128 m smear. **Also**: `Level.copy_file` hard-linked bundle PNGs into the level and a later in-place RGB rewrite went through the link — the bundle's `terrain/surfaces/*.png` lost their alpha (RGB intact; a terrain-only re-rip restores); hard-linking removed. FPS ≈ 100 with terrain + water. Water plane/blocks and spawn correct. Docs consulted: documentation.beamng.com terrain (`*TexSize` = world metres; size mismatch → warning material). |
| 2026-08-16 | A2 **ACCEPTED (in-game loads #6–#8, region)** | `beamng/dae.py` (COLLADA 1.4.1, vanilla `base00 → {bb_autobillboard, nulldetail, start01 → _a<px>, Colmesh}` hierarchy, one geometry per Mesh, shared-offset `<triangles>` per material run, `ndarray.tofile`; `glb_to_local = (−gx, gz, gy)` keeps winding; `glb_uvs` flips V), `beamng/materials.py` (v1.5 objects; textures capped + suffixed `.color/.normal/.data`, RGB unless needed), `stages/networks.py` (7,289 segment glbs → 256 m tiles, adaptive quarter split > 300k verts, tile-local coordinates, `Visible Mesh Final`), `stages/derived.py` (bake tiles per product with the collision policy: paint off, skirts/furniture on), `stages/materials.py` LAST — exports only what this build bound (region: 13 canonical materials, 62 textures; full city would be 1,409/5,446, ~2 GB at the 1024 cap). Three loads fixed three format facts: **(1)** detail size must be > 0 — `_a0` tiles gave "Shape with invalid detail levels" (collision built, nothing drawn) → `_a2`; **(2)** v1.5 opacity comes from **`opacityMap`** (+`opacityFactor`), never the albedo alpha — decals drew as opaque white boxes → the alpha is exported as `_o.data.png`; **(3)** materials export after the tile stages. Roads, kerbs, lane lines, decals, areas, skirts, furniture render and collide; region level 166 MB, builds in 1 m 15 s. Gate `tools/test_a2_dae.py` (28) in CI; validator checks DAE material symbols ⊆ mapTo and detail sizes. |
| 2026-08-16 | A2 full-city feedback (load #9, `cs2_city_network`: terrain + networks + markings + lane furniture, areas/skirts off) | Naomi's six screenshots (01:46–01:52) + `beamng.log` + BeamNG's `temp/` cache read before touching code. **What the evidence says:** (1) *road surfaces as RGB static, kerbs/tunnel walls navy-and-black* — NOT the export: `Road_0c1d…`/`RoadParts_fd2f…` JSON, PNGs and even BeamNG's cooked `.dds` are byte-identical to the accepted region build, and BeamNG's own missing-texture/material cards are orange "NO TEXTURE"/"NO MATERIAL" (decoded from `gameengine.zip`), not noise. The static is uninitialised GPU texture memory; the log shows BeamNG walking every level material 240→400 s after load and the LAST two it processed — `RoadParts` (392 s) and `Road` (394–400 s, its ao/rough/metal maps 3 s apart where every other material took 0.3 s) — are exactly the two that draw broken (navy top + black side = a black base colour reading fully metallic and reflecting sky/ground). Most consistent with **VRAM exhaustion on an 8 GB RTX 3070 at 3440×1440**: this level is 2,832 shapes / 1.6 GB of `.cdae` (lane furniture alone 970 MB of DAE, 13.1 M triangles of `FenceResidentialPieceHigh02`), 800 MB static collision, a 4096² terrain, plus whatever else held VRAM (CS2 was open for a rip an hour later). **Test before anything else: close CS2/other GPU apps, reload; then rebuild with lane furniture off.** (2) *blocky terrain cut-outs with black beneath at deep roads/tunnels* — the hole test itself is game-faithful (`over ∈ (bottom, top)`; the tunnel shell IS exported: `Road` + `RoadParts` walls to +15 m + `TunnelTopEnding`), the black is the underside of the heightfield seen through 3.5 m holes and the shell drawn with the broken `RoadParts`; `contain` also kept the mouth face standing across the road. (3) *terrain spikes through the road on cross-slopes* — samples inside a shallow band's footprint but above its roof (up to 0.79 m over the floor) were never clamped; and the ring sample outside the band rose through the kerb between samples. (4) *step off every road edge* — MEASURED on the 2026-08-16 rip: CS2's own grading puts ground under a road at **lane − 0.25 m** (p25 = p50 = p75) and the ring at −0.06/0.00, and the stage was lowering all 573k band samples to the band floor `lane − 0.5` and sinking the apron 0.15 more: a second trench that climbed back at the kerb. (5) *first load stalls* — `Level loaded in 217.9 s: objects 210.6 s` = 2.1 GB of COLLADA parsed to `.cdae` (2,832 files, cached in `temp/`), then 260 textures cooked on demand; second loads are fast. (6) *kerbs at junctions "jutting/invisible"* — the segment meshes' winding agrees with their normals (0.1 % of 302k faces opposed) and the crop shows the black kerb faces of the same `RoadParts` failure; re-judge after (1). **Built:** `csiibundle.terrain.clip_span/clip_grid/hole_mask` now also record `band_lane` (carriageway height, incl. samples above the roof by < `CLIP_SHALLOW_REACH` = 4 m) and `deep_lane` (road under every deep-band footprint sample); bake **terrainmask v2** writes `shallow_lane.png` + `deep_lane.png` (per-product `version` in the bake registry/manifest so only it re-bakes); `stages/terrain.py`: ground under a shallow band capped at `lane − 0.25` (the game's plane; only lowers), 8-ring capped at the lane, apron sink removed, holes default **`touch`** (clears the mouth; `contain`/`none` remain), and **rim walls** — a vertical quad from every hole-edge down to `deep_lane − 1 m`, rock-textured, facing the hole, with collision, tiled as `holewalls_<tx>_<ty>.dae` under `HoleWalls` (full city: 11,702 holes, 6,686 quads, 162 tiles; the wall's road height is the punched face's own `hole_lane.png` — recorded per face inside `contain_faces` because a portal strip too short to cover one cell centre still punches faces, and authored clip AREAS carve too and needed it: 1,253 rim edges had no height until then); `stages/cook.py` (A5 pulled forward): `BeamNG.drive.x64.exe -headless -nosteam -compilemeshes -compileMeshesCustomPaths levels/<name>/ -compileMeshesNoCheck -compileMeshesNoCache` after the build, progress = `.cdae` appearing beside each `.dae`, skips (never fails) when no exe / not under a userfolder / BeamNG running; `dae.write_shape` deletes a stale beside-`.cdae`; setting `cook` default on. (`-LoadSingleLevelForTextureCache` is not in 0.39's Lua — textures still cook on first load, ~2 min, no stall.) Gates: `test_clip_cut` ok, `test_b1_readers` ok, `test_a2_dae` 28/28, `test_a1_level` 39/39 (real bundle: 99.7 % under buildings within 0.5 m, holes + walls checked), `test_app_shell` 70/70. Full city rebuilt headless with the new code (`cs2_city_network`, settings switched to `terrain_holes=touch`, `cook=true`): 18 m 22 s cold (bake 15 m — Naomi had wiped `derived/` and re-ripped 02:19–02:53), 7 m 30 s warm incl. **cook 3,001 shapes → `.cdae` in 3 m 19 s, exit 0**; validator 0 problems. **Awaiting load #10** — first with CS2 closed. |
| 2026-08-16 | **Load #10 regressed → c426641 REVERTED except the cook stage** (`cs2_city_network`, first load with CS2 closed) | Naomi's seven screenshots (04:45–04:53) vs load #9's: textures now draw (the GPU-state diagnosis held — the road texture was the one improvement), but every tunnel mouth is walled shut (the rim walls stand across the portal face: a hole edge is a hole edge), sunken roads sit in a black moat (`touch` punched the whole trench footprint), the terrain still pokes through carriageways, hole rims are saw-toothed, and junctions z-fight. Decision (Naomi): revert the commit, keep A5. **Reverted:** `stages/terrain.py` (band-floor clamp + 0.15 m apron sink, `contain` holes, no walls), `csiibundle.terrain` (`band_lane`/`deep_lane`/`face_lane`, `CLIP_SHALLOW_REACH`), bake terrainmask back to v1 (per-product `version` plumbing reverted with it; `derived/terrain/` force re-baked so the cache matches the code), the Blender adapter/tests, `terrain_holes` back to `contain|majority|none` (Naomi's saved `touch` reset to `contain`). **Kept:** `stages/cook.py`, `dae.write_shape`'s stale-`.cdae` removal, `cook` default on. **Decal boxes found and fixed while at it** (crosswalks, the `45`s and lane lines drew as light opaque boxes in BOTH loads, while arrows blended): the headless compiler runs without a level, never sees `art/cs2/main.materials.json`, and BeamNG's COLLADA importer wrote an auto-generated `main.materials.json` beside the first shape of each directory (`art/shapes/{markings,networks,lanegeometry,holewalls}/`, 04:00–04:02) with opaque `diffuseColor 0.8` placeholders for that shape's names — `LaneMarkingsEUNA`, `LaneLine`, `CarLane`, `TrainRailsBase`, `RoadParts`, `TrainSubwayTrack`, `Powerline_Cables`, `holewall`. Loose files under `levels/` are all mounted, so those shadowed ours; `NA_RoadArrow` was not among them, which is why the arrows were right. The materials export itself is correct (opacity maps carry the alpha; the atlases decoded fine). Fix: `cook` removes every `*.materials.json` under `art/shapes/` before and after compiling (also on cancel/stall); `validate_level` fails on a materials file under `art/shapes/` or a name defined twice (`strayMaterialFiles`, `duplicateMaterials`) — run on the load #10 folder it reports 4 files / 8 shadowed names; the worker clears `art/shapes/` and `art/textures/` at build start (a removed stage's tiles, a region change or a texture-cap change no longer leave orphans that BeamNG mounts and the compiler compiles). New oracle `tools/blender_import_level.py`: imports the LEVEL (`.ter` with holes + `.dae` tiles through Blender's COLLADA importer + the level's v1.5 materials with opacity) for a region, so a build can be looked at before a load; `tools/oblique_render.py` renders any saved `.blend`. Gates: `test_a2_dae` 36/36 (8 new), `test_a1_level` 36/36, `test_clip_cut`, `test_b1_readers` ok. Full city rebuilt headless with the reverted code — see the row below for the Blender comparison and load #11. |
| 2026-08-16 | Rebuild + Blender oracle after the revert (**awaiting load #11**) | Full city rebuilt headless with the reverted code: **6 m 57 s** (terrain 14 s, networks 54 s, derived 69 s, materials 4 s, cook 2,839 shapes 3 m 09 s exit 0 — it wrote 3 placeholder materials files again and the stage removed them — validate 0 problems); only `art/cs2/` and `art/terrains/main.materials.json` remain. Blender, level vs bundle at two regions: **(a) roundabout over the sunken twin highway (2936, 2240) — the load #10 shots 045054/045305**: the level draws continuous ground either side of the trench with the retaining walls flush at their tops (no moat), the roundabout and ramps whole, crosswalks as stripes and the `70 70`/`55`/`30` decals as digits — no boxes; **(b) the hillside twin-bore portal at (482, 6255)** (sandy hills, the 044835 kind): the mouth is open (no walls), the road runs under the rock-cut mesh; the one visible difference to the bundle's lattice is the KNOWN `contain` limitation — 3.5 m heightfield squares beside a cut slope from the rim into the carriageway where the lattice cuts clean ("terrain through the road", present in #9 and #10 too). `touch` fixed that but dug the moats, so the middle rule is still to be designed (punch a touched face only where its kept corners stand above the carriageway, and only inside the band's own walls); not attempted this session. Renders under `E:/carto-out/probe/` (`ref_*` = bundle, `lvl_*` = level). |
| 2026-08-16 | **Terrain/road interface validated against the game; the fine cascade raster (producer) and the 60k-vertex geometry cap (consumer) — awaiting re-rip + load #11** (audit §45) | Naomi's load-#10 ask: validate against the decomp, fix where the CS2 World Bundle producer diverges, carry to the app, A/B in headless Blender. **Classified first:** the torn junctions are BeamNG-only (the bundle at (2936, 2240) renders clean) and sit in the tiles whose single COLLADA geometry passes 65,535 vertices (`net_11_8` = 94,001; the accepted region's largest tile was 62,798; 59 city tiles up to 167k) — BeamNG's `.cdae` keeps them whole with 32-bit indices (`tools/beamng/probe_cdae.py`), so the wrap is in the renderer's per-mesh index buffer; the poke-through is a **producer gap**: `Game.Simulation.TerrainSystem` renders the terrain near the camera from the height cascade's finer slices (4 slices, all 4096²; slice `baseLod+1` = 1.75 m/texel over a 7168 m camera window, +2 = 0.875 m), each re-drawn as base heightmap + lot/lane/area grading rasterised at that texel size, while `GetHeightData()` — the bundle's `heightmap.png` — is slice `baseLod` at 3.5 m; kerbs at nodes are game-true (rails carry `m_EdgeHeights`). **Built:** producer `Systems/TerrainRipSystem.FineCascade.cs` — a new stage before Networks steers the game's own cascade over the map (2×2 windows, camera pivot straight down with the pivot→transform offset calibrated first because the pitch is clamped, a kick to the far corner when the quarter-window hysteresis would swallow a move), waits for the slice to land + render, reads it back synchronously → `terrain/heightmap_fine.png` 8192² @ 1.75 m + `rasters.terrainFine` + `layers.terrainFine` (schema 2 additive; `warnings.jsonl` `kind: terrainFine` when it could not run; the network conform samples it); consumers `reader.load_heightfield` (declared+missing = CONTRACT VIOLATION, undeclared = base map) used by the Blender lattice, `drape.HeightfieldSurface`, `terrain.hole_mask`, the bake ground/terrainmask/areas/skirts/markings inputs, and the app terrain stage (8192 `.ter` when fine, `terrain_upsample` skipped); cell-tuned constants made metre-tuned (`contain_samples(cell)`, corridor `ceil(10.5 m/cell)`); `dae.split_mesh` caps every `<geometry>` at 60,000 vertices in `networks` + `derived` (nodes keep the trailing `_a<px>`), the validator flags oversize geometries. **A/B (headless Blender):** interchange + downtown-areas top-down `PASS max=1`, full-map scene digest identical, the split tile through the level oracle `PASS max=0`; gates `test_a2_dae` 44/44, `test_terrain_fine` 14/14 (new), `test_a1_level` 36/36, `test_app_shell` 70/70, clip-cut/readers/bake green, `validate_bundle` VALID/1, `dotnet build` clean. **Next:** Naomi rips (terrain + networks is enough) → `test_terrain_fine.py` fidelity numbers + base-vs-fine renders → rebuild `cs2_city_network` → load #11 (shards gone, terrain meets the kerbs). |
| 2026-08-16 | **Load #11 → #12: shards fixed, textures cook once, deep-band teeth → `terraincut` (the Blender cut in-game); 8192 hung; load #12 feedback: gaps beside trench walls, teeth at a trench mouth** (audit §45.7) | Load #11 (4096 + split tiles): junction shards GONE, roads level, decals draw; the cook worked (2,839 TSStatics in 41.6 s via .cdae); the 30 s stalls were BeamNG cooking 302 textures on first load because every build cleared+rewrote `art/textures` → `Level.write_bytes_if_changed` (mtime kept, BeamNG's cooked cache survives), orphans swept, worker no longer clears textures. **8192 terrain** loaded (TerrainBlock 9.5 s) then hung the game after the world was up (main thread not responding, 9.2 GB) — BeamNG's stated ceiling, no vanilla level > 4096 → `terrain_upsample` 1 = 4096 base map (default) / 2 = 8192 fine; bake `ground` option follows (now a PER-PRODUCT option key, not a shaping option — a first cut made every product stale and cost a 20 min re-bake). Per-product versions are back (`Product.version/options`; `terrainmask` v2 adds `deep_floor/deep_footprint`). Teeth in trenches/portals = heightfield keep/punch of whole faces vs the game's per-pixel discard: (1) `terrain.lower_deep_bands` (in-band samples under the carriageway; kept beside a portal roof) — better but teeth stayed in mouths; (2) **`terraincut` bake product**: the harness lattice (containment delete, rim snap, sub-cut) as ground meshes for deep-touched faces + 1 cell, heightfield punched there, seam watertight via the ONE shared `terrain.ground_heights` (float32, quantised) — level oracle == harness render at the sunken road. Ground-mesh materials `<level>_ground_{grass,dirt,rock}` from a macro×detail composite (UV 40 m, terraincut v2). Load #12 (4096 + terraincut): much better; remaining: gaps beside deep trench walls (present in the harness too — the cut-edge residual), teeth at a trench MOUTH where a shallow band meets steep ground (present in the coarse harness, gone with the fine raster → the 8192 experiment: `cs2_city_8192`, lane furniture off, being built). Gates: test_terrain_fine 25/25, test_a2_dae 46/46, test_a1_level 36/36, test_app_shell 70/70. |
| 2026-08-16 | **Load #13 (`cs2_city_8192`, 8192 + terraincut, furniture off) → pre-cooked DDS textures, rim skirts, paint erosion; level `cs2_city_fine` rebuilt + cooked, awaiting load #14** | Naomi: 8192 loaded (slow, looked hung), <1 fps for ~1 min after load, some textures still missing, no more jutty bits at the tunnel, a gap beside the terrain (screenshots did not arrive). Diagnosis from `beamng.log`: level loaded in 48.7 s (TerrainBlock 10.2 s at 8192 vs 0.8 s at 4096; TSStatic 33 s / 2,326 shapes) — then `TextureCooker … started importing` for ALL 190 textures between 81 s and 290 s (`temp/levels/<name>/art/textures/` cache is keyed on the level NAME, and the name was new): the sub-1 fps minute. Vanilla levels ship `.dds` and never enter the cooker (their temp cache holds only imposters). **Fix: `beamng/dds.py`** — textures go out pre-compressed + mipmapped in exactly the formats the cooker produced (read back from its output: colour BC7 sRGB 99, normal BC5 83, grey data BC4 80, RGB data BC7 98; DX10 header, mips to 1×1, `0xa1007`/`0x401008`); block encoder `etcpak` (optional dep `textures`; BC7 mean err 0.02/255 at 0.16 s per 1024², BC5 0.67 max 16 — Pillow's DXT put 90-187/255 outliers into contrast blocks); 190 textures in 3 s, 128 MB (== the cooked cache), setting `texture_dds` (PNG fallback). Blender loads them (OIIO). **The gap beside the wall**: cross-section at the 1031/5921 trench — the clip prism side (where the terraincut rim snaps) is 0.22 m OUTSIDE the retaining wall's outer face (game: `m_WidthOffset`), nothing below → sky through the slot at every wall foot (harness too). **terraincut v3/v4: rim skirts** — a `ground:rock` strip from every once-used lattice edge of the tile's faces down to the deep band floor − 1 m (`deep_floor` 3×3 min), none on roof rims (portal iso-edge; a curtain there would cross the bore), sub-cut T-junctions resolved (a sub-edge on a longer once-used edge is covered; the long edge keeps only its uncovered parts) — 158k tris city-wide. Oracle: the sawtooth of sky at the wall foot is closed. Also: `.ter` deep-band lowering now only moves samples whose four squares are all punched (a kept square must keep the height the meshes were built with), and the fine-grid roadbed PAINT is eroded by one sample (nearest resample bled it half a base cell onto the trench banks — a dark sawtooth of road paint on the rim; the sink is unchanged, the meshes share it). The remaining oracle "spikes" behind trench walls are a rendering artefact of steep facets — ray-cast profiles along the wall at 6 offsets are smooth (±0.3 m staircase ripple), no geometry stands above the bank. `terrain_upsample` default → 2 (8192). Gates: test_a2_dae 58/58 (DDS block), test_app_shell 70/70, test_terrain_fine 25/25. |
| 2026-08-16 | **Load #14 (`cs2_city_fine`) → tunnels closed, pale squares, black walls: terraincut v5, AO skip, shader-matched ground composite; rebuilt + cooked, awaiting load #15** | Naomi's screenshots (Steam `userdata/…/760/remote/284160/screenshots`): (1) tunnels closed off — the v3/v4 skirts hung curtains across the bore (the `deep_footprint & ~inside` roof test does not fire at the mouth ring, whose samples ARE inside the band); v5 skirts only where the rim midpoint is within 0.75 cell of the referenced strip's side polyline (`_near_prism_side`, refs + strip_geo) — WRONG: `cut9_portal.png` still had curtains in the road centre (a node fan's radial curve passes a polyline-distance test); **v6**: a rim gets a skirt only where `deep_footprint` is on exactly one side of it (`_footprint_one_sided`, probes 0.75 cell either side; the footprint runs on into the hill along the tube, so a mouth/roof rim has it on both) — verified by listing every face in the mouth zone: nothing but the tube below 557 m for |across| < 8 m (`cut10_portal.png`); Naomi then circled two more: a rock strip at the right wall's end (a skirt hung from a rim edge climbing the cliff, 557→580 m; **v8**: no skirt when EITHER end is more than 13.5 m above the floor — a lateral rim never rises above the roof) and a sky triangle in the hillside over the portal (the sub-cut patch's centre-sample rule: on a 26 m-per-cell cliff a dropped sub-quad reaches ~1.6 m above the roof and a kept one as far below; `terrain.build_lattice` now resolves CLIFF sub-patches (corner range > 4 m, `SUBCUT_CLIFF_RANGE_M`) exactly — keep a sub-quad if any corner is above the roof, lift its in-band corners onto the roof iso (`band_heights_at`), the one sanctioned upward move: on a near-vertical patch it stays on the surface; gentler faces keep the centre rule). Mouth lattice: 0 faces below the roof for |across| < 8; `cut12_portal_close.png` clean; (2) 'light green squares' = `ground:grass` faces (lattice slope class) in a rock-painted hillside, and every ground mesh 2-4× brighter than the heightfield: the terrain shader (`shaders/common/terrain/terrain.h.hlsl` `calculateValue`) does `base + (macro·2−1)·mF + (detail·2−1)·dF` on the encoded texel values (mF 0.39, dF 0.85→0.3 over 50 m) — a darkening overlay a plain mesh material never got; v5 binds `ground:<layer>` from `splat.dominant_layer` per face (materials `ground:{grass,dirt,rock,extra1-4}`, one mesh material per painted layer) and the composite applies the same overlay at ~60 m (grass mean 103→69/93/19, matching the screenshot's terrain grass ≈ 70/115/40); (3) black wall faces + black tunnel interiors (present since load #9): the bake's `_ao.png` = mask G channel, all-zero on 67 % of masks (mean 11.5/255) — bound as `ambientOcclusionMap` it zeroed ambient; the materials stage now binds AO only when its mean ≥ 96/255 (24 of the build's maps dropped). Contract §7.1's 'G = ambient occlusion' is a hypothesis to re-check against the decompiled shader inputs. Rebuilt `cs2_city_fine` (4m27s, cook 2,326, validator 0). Still open: the ring's staircase silhouette above trench walls (geometry is smooth; facets + heightfield staircase), TSStatic load time, lane furniture at 8192. |
| 2026-08-16 | **Load #15 (`cs2_city_fine`) → pillars at portals, pale jagged ring: portal-wall cap, terraincut v9 (no ring, no snap, heightfield normals); rebuilt, awaiting load #16** | Screenshots 22:31-22:34: (1) 50-70 m concrete pillars at trench/portal ends = `TunnelTopEnding` (RaiseTopToTerrain) pieces the producer lifted to a 70 m cliff face at the mouth (seg_2059357 at 1092/6490: top 603 m over a 534 m road; also seg_2059568, seg_262549) — the game's terrain stands vertical there and hides them, our heightfield slopes over 1.75 m and does not; the networks stage now caps TunnelTopEnding vertices at the segment level + 13.5 m (`_cap_portal_walls`: nothing above the deep band's roof needs closing); (2) the pale, jagged, faceted band along every trench wall was the terraincut margin ring — 1.75-3.5 m of mesh beyond the wall in a material the terrain shader never matches — so **v9 removes the ring**: the mesh is exactly the band-touched faces (`CORRIDOR_MARGIN_CELLS` 0), whose outer corners are raster samples shared with kept heightfield squares, hence no lateral snap (`build_lattice(snap=False)`; the sub-cut draws the hole edge at 1/8 cell), and every vertex carries the raster's own central-difference normal through the tile sink (`TileSink.add(normals=)`, `weld` averages) so the seam has no shading step; visible mesh beyond a wall is now 0-1.75 m. Colour parity with the terrain shader remains approximate (per-layer materials + the encoded-space overlay). |
| 2026-08-17 | **A2 SIGNED OFF — and the parity sweep runs before A3** | Nineteen in-game loads from the region build (#6-#8, which accepted A2 itself) through the full city (#9-#19). What the feedback rounds bought, all now committed: the 60,000-vertex per-geometry cap (load #10's torn junctions were BeamNG's renderer wrapping per-mesh index buffers past 65,535 — `.cdae` keeps them whole, proven against the shipped files); pre-cooked **DDS** textures in the cooker's own formats, which removed the sub-1 fps first minute (zero `TextureCooker` lines since) together with `write_bytes_if_changed`, since rewriting an identical texture had been invalidating BeamNG's cache every build; the **cook** stage (A5 pulled forward) and its removal of the placeholder `*.materials.json` BeamNG's importer writes beside the first shape of a directory, which had been shadowing our decal materials; the producer's **`layers.terrainFine`** capture of the game's own 1.75 m cascade slice, which is the ground the player actually sees; per-product bake versions; and the **level oracle** (`tools/blender_import_level.py`), which reads what BeamNG reads so a build can be judged without a load. The terrain cut ends the phase matching the game's own stencil rule exactly (§46, 99.98-100 % against the game's predicate) after that rule was finally read rather than guessed at. **Not carried into A3:** the ground-boundary checkerboard and ground-mesh material parity — written up as sweep items 1a/1b with their evidence, to be cleared by the parity sweep before A3 starts. |
| 2026-08-17 | **The consumer-side gap closing gutted — `terraincut` v10 is the Blender lattice verbatim; awaiting load #17** (audit §45.8) | Naomi after load #16: the invented tunnel cut / gap closing "caused artifacts poking up to the sky and texture not matching the terrain — gut it and implement it as closely to how it looks on Blender as possible, as that implementation works flawlessly". **Removed:** the rim skirts (v3–v8: curtains across tunnel bores at #14, rock fins off cliffs, three successive tests to stop them drawing), the v9 margin ring + `snap=False` (the pale jagged band along every trench wall at #15 — mesh beyond the wall in a material the terrain shader never matches), `terrain.lower_deep_bands` with `terrainmask` v2's `deep_floor.png`/`deep_footprint.png` (the 12 m spikes behind trench walls at #12/#13, and the `_samples_fully_punched` gate that patched them), `networks._cap_portal_walls` (it hid producer-true `TunnelTopEnding` geometry), and the fine-grid roadbed paint erosion. **Ships:** `terraincut` v10 = `terrain.build_lattice` **unchanged** (containment delete, hole-rim snap, sub-cut, cliff sub-patch resolve), handing the consumer the faces it changed — every face a deep band touches plus every face with a corner the snap moved (a heightfield square still holding that vertex at its raster position would tear), and nothing else; the mesh's outer boundary is unmoved raster samples at `terrain.ground_heights` heights, so the seam is watertight by construction. `terrainmask` v3 drops the deep rasters; the terrain stage punches and paints from the same rule the mesh binds; ground materials are `doubleSided` (a cliff sub-patch is near-vertical). Full city 8192: 45,927 faces punched, 10,463 meshed → 83 tiles / 134,455 triangles, 288 s. **Measured against the harness** (level oracle vs bundle harness, ray-cast profiles on a 0.5 m grid at the 1031/5921 trench and the 1092/6490 portal): holes agree to 6 columns of 58,081, median |Δh| **0.005 m** (quantum 0.0625), and 1,552 of the 1,553 columns over 0.5 m are cliff columns where a vertical ray cannot compare two triangulations — the one flat outlier is a 30 m cliff in the source raster where the `.ter` is bit-identical and only the diagonal differs. **Two invariants gated** (`test_terrain_fine`, exact triangle/face SAT, not centroids): no punched face left without ground (city-wide 1 of 45,927; the harness shows sky there too) and a watertight seam (12,512 vertices, median 3.9e-6 m = glTF float32 noise, max 3.0e-5 m, **none over a millimetre** — after two quantisation fixes the gate found, both the bake rounding in float32 where the consumer rounds in float64: the vertex heights, then the shallow-band floor, which has to reproduce `terrainmask`'s whole PNG round trip; `terraincut` v12, and remember a bake product's `version` must be bumped or the re-bake reports `fresh`). 5,527 faces overlap the kept heightfield in plan: that is the snap folding the rim, which the harness does too, and probes show only the heightfield visible from above. Gates: test_terrain_fine 26/26, test_a2_dae 58/58, test_a1_level 36/36, test_app_shell 70/70, test_bake_products, test_clip_cut, test_b1_readers. Still approximate: ground-mesh colour vs the three-tier terrain shader. The `TunnelTopEnding` pillars are producer-true and stay until fixed in the producer. |
| 2026-08-17 | **Load #17 → validated against the game and replicated exactly: `terrainclip` + `terraincut` v13; awaiting load #18** (audit §46) | Naomi: *"take a pause and validate against the game and replicate how it does it exactly, this inventing and guessing has us going around in loops"*. **Validated from `Game.Simulation.TerrainSystem`:** a clip volume is emitted per lane section with `GeometryFlags.ClipTerrain` from the composition's own `m_WidthOffset`-trimmed curves; the band is the edge/surface heights + `m_ClipHeightOffset`, raised to `ElevationLimit*3` for a Lowered side or a tunnel transition, then `±0.3` VERTICALLY in `GenerateClipDataJob` which also raises the curves to the band floor; the volume is a **closed prism mesh** instanced per section and rendered into a **stencil** (`RenderClipAreas`, `TerrainShadowUseStencilClip`), dilated toward the camera by `tan(fov/2)*0.002` so no sliver survives. **The producer already matches all of that line for line** — every artifact of loads #11–#17 was a CONSUMER approximation of the per-pixel discard. **Built:** `csiibundle/terrainclip.py` — the exact cut by convex subtraction (on one triangle the prism's side walls, floor and roof are all affine in x/z, so the discarded part is an intersection of half-planes); `terraincut` **v13** reads `clipvolumes.jsonl` directly and cuts every square a deep band reaches, replacing the containment scoring, the rim snap, the sub-cut and (removed from the shared harness too, proven by experiment to be the NEEDLES) the cliff lift; `_punch_mask` now takes `corridor_faces.png` as the whole hole set — unioning it with the old scored `clip_cells.png` punched **8,132** squares the exact cut keeps, each a gap with no mesh under it. **Also:** terrain macro/detail tiers are now CENTRED on mid-gray (`_centre_to_mid`) — BeamNG's `calculateValue` adds them as `(2t−1)·factor`, so putting the same dark CS2 albedo in all three tiers applied its darkness three times and `extra2` rendered **(0,6,0) black** and grass a crushed (41,77,0); centred, every ground renders at its own albedo. **Measured against the game's own predicate** (sample points, 'inside a deep prism at the terrain's height?' vs 'does the level draw ground here?'): the trench 99.825 % → **99.98 %**, the portal → **100.00 %**, ground drawn where the game discards **0**, open-sky columns per 80 m window **65 → 3**. Full city 43,043 squares cut, 188,693 triangles, 134 s (was 288 s). Seam still exact (30,305 vertices, max 3.2e-5 m = glTF float32). Gates: test_terrainclip (new, incl. point-sample agreement), test_terrain_fine 26/26, test_a2_dae 58/58, test_a1_level 36/36, test_app_shell 70/70, test_clip_cut, test_bake_products, test_b1_readers. `cs2_city_fine` rebuilt + cooked (2,255 shapes, exit 0, validator 0). Open: the terrain material-index boundary is a hard checkerboard (argmax per sample, no blend); the Blender harness still uses the scored cut and is now the less accurate consumer. |
| 2026-08-16 | **NEXT: A3** | Instances → forest (`.forest4.json` + `managedItemData.json`) for trees and uniform-scale props / fences / poi (`lanegeometry/instances.jsonl` placements too), TSStatic for buildings + non-uniform objects with the LOD ladder from `derived/lods/` (`_a<px>` levels, `lod_vertcol`, `bb_autobillboard`, `nulldetail`) and Colmesh/ColBox from `csiibundle.collision`; hybrid tints (`colorPaletteMap` + `instanceColor/1/2`, forest baked variants); density/toggles/budgets; lights (off by default); groundcover. Then A4 perf pass (incl. the areas bake: 75 min single-threaded full city), A5 cook. |
