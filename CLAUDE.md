# csii-world-bundle — Cities: Skylines II world ripper (fork of Carto)

A fork of the **Carto** Cities: Skylines II mod that extends it from a *vector geodata*
exporter into a **full world ripper**: it walks the loaded city in-engine and dumps the real
**meshes, materials/textures, per-instance transforms, and every useful attribute** of
buildings, networks, props, trees, terrain and water into a single, well-documented,
engine-neutral **CS2 World Bundle** on disk.

This mod is the **producer** of that bundle and nothing more. Downstream consumers (e.g. a
BeamNG level builder, a Blender importer) are **out of scope for this repo** — they read the
bundle through its documented contract and must not be coupled into the mod. Keep this repo
buildable and useful on its own.

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
- **Prove GPU mesh readback FIRST** (PLAN Phase 1 spike). Reading real vertex/index buffers at
  runtime is the single highest risk; if it's infeasible, the fallback is offline
  `.assets`/asset-DB extraction keyed by prefab name — decide before building everything else.
- Freeze and version the **bundle schema** before writing extractors; write a JSON Schema +
  example bundle so a consumer can be built in parallel without reading this code.
- Keep the mod **additive and non-destructive**: never write into the game install, only into
  Carto's `ModsData` output folder. No network access.
- Do not couple to any specific downstream engine. The bundle is engine-neutral.
