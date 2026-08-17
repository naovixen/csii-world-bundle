# csii-world-bundle — Cities: Skylines II world ripper (fork of Carto)

A fork of the **Carto** Cities: Skylines II mod that extends it from a *vector geodata*
exporter into a **full world ripper**: it walks the loaded city in-engine and dumps the real
**meshes, materials/textures, per-instance transforms, and every useful attribute** of
buildings, networks, props, trees, terrain and water into a single, well-documented,
engine-neutral **CS2 World Bundle** on disk.

The mod is the **producer** of that bundle and nothing more. Consumers read the bundle through
its documented contract and **must not be coupled into the mod**; the mod builds and runs
without any of them. Two consumers do live in this repo, beside the mod, as of 2026-08-15:
the Blender adapter (`tools/blender_import_bundle.py`) and the **`cs2beamng` BeamNG.drive level
builder application** (`apps/cs2beamng/`), both built on the engine-neutral `tools/csiibundle/`
package and its offline **bake** stage (`csiibundle.bake` → `<bundle>/derived/`, contract §13).
The live plan for that work is `docs/plans/beamng-consumer-plan.md` — **every phase from A3 on
carries a parity gate** (read the game's counterpart system, write the rule matrix, sample a
predicate, count what stays approximated) before its in-game load; `pip install -e .` installs
both Python packages (`pyproject.toml` at the root maps them from `tools/` and `apps/`).

## The spec
`@PLAN.md` is the authoritative spec. It defines the extraction inventory, the bundle
contract (schema + formats), the concrete fork points in the Carto source, and the phased
build with acceptance tests. Follow its phases in order; re-read the relevant section instead
of guessing. **Freeze the bundle schema (Phase 0) before writing extractors** — it is the
seam everything depends on.

## What this is / isn't
- **Local & personal use only.** This rips assets from the user's own copy of CS2 for
  interoperability with software they own. **Never** add features that publish, upload, or
  redistribute ripped meshes/textures, and never commit ripped game assets into this repo.
- It is a **CS2 mod** (runs inside the game). Unlike the parent Python-side tools, it *does*
  touch game data — that is the whole point — but only reads it.
- Keep Carto's existing **vanilla vector/raster export intact**. The ripper is an *additive*
  export path alongside GeoJSON/Shapefile/GeoTIFF, not a replacement.

## Stack & setup
- **C# / .NET SDK-style project** (`Carto.csproj`, `Carto.sln`), built against the
  **Cities: Skylines II Modding Toolchain**. The csproj imports
  `$(CSII_TOOLPATH)\Mod.props` + `Mod.targets`, so the `CSII_TOOLPATH` user env var must
  point at the installed toolchain (set up once via the in-game "Modding Toolchain" or the
  Paradox mod SDK). Game assemblies (`Game`, `Colossal.*`, `Unity.Entities`,
  `UnityEngine.CoreModule`, `Unity.Burst/Collections/Mathematics`) are referenced with
  `<Private>False`.
- Available bundled libs you may use: **Newtonsoft.Json** (JSON), **ICSharpCode.SharpZipLib**
  (zip). `AllowUnsafeBlocks` is on (needed for native mesh buffer reads).
- **Build:** `dotnet build -c Release` (or open `Carto.sln` in VS 2022). `Mod.targets`
  deploys the DLL to the CS2 mods folder automatically.
- **Run/test:** launch CS2, load a save or the map editor, open **Options → Carto → Ripper**,
  and press **Rip World**. The bundle lands directly in
  `…\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto\` — one bundle per
  install, no per-city subfolder, beside the vector export's `GeoJSON/`, `GeoTIFF/`,
  `Shapefile/` and `Styles/` folders. A full city takes ~28 min; unchecking **Rip Objects** and
  **Rip Terrain** gives a ~5 min networks-only rip, and files whose stage did not run are left
  alone, so the bundle goes **mixed-vintage** rather than broken.
- **Only the user can produce a bundle.** Ripping needs the game running with a city loaded;
  you cannot do it. See the staleness non-negotiable below before using one.

## Layout (parent Carto + ripper additions)
- `Systems/` — one `GameSystemBase` per domain (`BuildingSystem`, `NetworkSystem`,
  `AreaSystem`, `POISystem`, `RouteSystem`, `ZoningSystem`, `RasterSystem`,
  `SharedDataCollectionSystem`). **New — the ripper's collectors:** `InstanceRipSystem`,
  `MeshRipSystem`, `MaterialRipSystem`, `AppearanceRipSystem`, `NetworkRipSystem` (+ the
  `.GameMesh` partial that ports the game's composition-mesh generator), `LaneRipSystem`,
  `TerrainRipSystem`, and `RipOrchestratorSystem`, which sequences all eight stages behind the
  one **Rip World** button.
- `IO/` — writers (`GeoJson`, `Shapefile`, `GeoTiff`, `IO`, `Options`, `Enums`, `FieldInfo`).
  **New:** `GltfWriter`, `BundleWriter`, `BundleManifest`, `BundleFile`, `InstanceWriter`,
  `NetworkWriter`, `LaneWriter`, `TextureWriter` (DDS/RenderTexture → PNG), `Png` (16-bit
  rasters), `GeometryAssetReader` (the native mesh decode), `BaseSkirtBuilder`,
  `GradingWriter`, `RipProgress`.
- `Domain/` — data records (`BuildingStat`, `NetworkStat`, `POI`, `Theme`, …). **New:**
  `InstanceRecord`, `MeshAsset`, `MaterialAsset`, `NetworkRecord`, `LaneRecord`,
  `LaneGeometryRecord`, `PrefabMesh`, `RipWarning`.
- `Geodata/` — CRS/projection math (PROJ port). Reuse for the manifest's geo-referencing.
- `Utils/`, `Locale/`, `Settings.cs`, `Mod.cs`, `Instance.cs` (service-locator exposing
  `Instance.Prefab`, `Instance.Terrain`, `Instance.Water`, `Instance.Map`, …).

## Conventions (match the existing code)
- Follow Carto's established patterns: `EntityQuery` + Burst `IJobEntity`/`IJob` collectors,
  `ComponentLookup<T>`/`BufferLookup<T>`, `NativeList`/`NativeArray` with explicit `Dispose`,
  managed writing off the main thread via `Task.Run`. Resolve prefabs with
  `Instance.Prefab.TryGetPrefab(...)`. Comments in ripper code (fork-added files) are
  **English-only** — the bilingual EN/中文 style is upstream Carto's; keep it in upstream files,
  never add it to ours (stripped repo-wide 2026-08-09).
- **Dedupe by prefab.** Rip each unique mesh/material **once**; instances only reference them
  by `assetId`. A city has thousands of trees but a handful of tree prefabs.
- Gate each phase on its PLAN acceptance test. **Commit after each passing phase** with a
  clear message; show the diff.

## Non-negotiables
- **The bundle on disk goes stale the moment producer code changes.**
  `…\ModsData\Carto\` is the output of *one* rip at *one* point in time, not a live view of
  what the producer would emit today. Any change under `Systems/`, `IO/` or `Domain/` makes those
  bytes a description of the **old** code, and only the user can refresh them by ripping in-game.
  Therefore:
  - **Never import the stale bundle and reason about the result as if it reflected your change.**
    The import will succeed and look plausible; every conclusion about the new code is fiction.
  - **Never call a producer change validated** because the harness ran clean — it ran on old bytes.
    Say "built, awaiting in-game re-rip", the convention the audit notes already use.
  - **Ask for a rip and wait.** Do not substitute the nearest available artifact.
  - **Check staleness before trusting it, every time:** compare the bundle's mtime against
    `git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' --since='<mtime>' -- Systems IO Domain`.
    Name any commit it lists when stating what the bundle can and cannot answer.
  - **That check tells you where to look, not what to conclude — it gives false positives.**
    The mod deploys on `dotnet build` (via `Mod.targets`), not on commit, so the running mod can
    hold uncommitted work and a commit dated *after* a rip may already have been live during it.
    Confirm against something the rip itself emitted — a log line, or a field's presence in the
    output — before calling a layer stale. (2026-08-07: `8985b73` looked like it post-dated the
    rip, but the rip's own log already printed that commit's `CaptureSplatmap` lines.)
  - It is still the right source for *what shape the entities in a real city have* — a fact about
    the save, not about the code. It is the wrong source for *did my change work*.
- **When output looks wrong, find the game's rule before writing a fix.** The game's own
  implementation is decompilable (`tools/decomp`; `decomp all Game <dir>` then ripgrep) and it is
  the only authority on what the output should be. Between BeamNG loads #11 and #17 the terrain cut
  was "fixed" seven times — containment scoring, a rim snap, a sub-cut, a cliff lift, rim skirts, a
  band lowering, a margin ring — and every one of them was a guess that closed one artifact and
  opened the next. Thirty minutes in `Game.Simulation.TerrainSystem` gave the actual rule (a stencil
  prism, audit §46), showed the producer had matched it all along, and replaced all seven with one
  exact cut. So:
  - **Do not invent a rule in a consumer.** If a consumer needs to decide something the game also
    decides, read how the game decides it and port that. A rule with no counterpart in the game's
    source is a hack with a countdown on it, however reasonable it looks.
  - **Every approximation is declared and counted** in the code that makes it and in the rip/build
    report — what it approximates, why an exact answer is not available, and how often it fires.
    An undeclared approximation is indistinguishable from a bug once its artifact appears.
  - **The Blender harness is not the oracle; the game is.** The harness is a consumer with the same
    licence to be wrong, and on 2026-08-17 it carried the very defect a screenshot was blaming on
    the app. Compare a consumer against the game's rule (`tools/check_terrain_vs_game.py`
    samples points and asks the game's own predicate), not against the other consumer.
  - **A fix that only one consumer gets is in the wrong place.** Shared rules live in
    `tools/csiibundle/`; both consumers call the same function.
- **Prove GPU mesh readback FIRST** (PLAN Phase 1 spike). Reading real vertex/index buffers at
  runtime is the single highest risk; if it's infeasible, the fallback is offline
  `.assets`/asset-DB extraction keyed by prefab name — decide before building everything else.
- Freeze and version the **bundle schema** before writing extractors; write a JSON Schema +
  example bundle so a consumer can be built in parallel without reading this code.
- Keep the mod **additive and non-destructive**: never write into the game install, only into
  Carto's `ModsData` output folder. No network access.
- Do not couple to any specific downstream engine. The bundle is engine-neutral.
