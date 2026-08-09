# Implementation Plan — Carto-Ripper: the CS2 World Bundle exporter

A build spec for extending **Carto** (a Cities: Skylines II mod that exports vector/raster
geodata) into a **full world ripper**. Where vanilla Carto walks the loaded city and keeps
only 2D footprints + a heightmap, this fork keeps **everything geometrically and semantically
useful**: real meshes, materials/textures, per-instance transforms, and the complete
attribute set — written to a single, versioned, engine-neutral **CS2 World Bundle**.

The mod is a **producer** of that bundle. Any consumer (BeamNG level builder, Blender
importer, another game) reads the bundle through the documented contract in §4 and is **out of
scope for this repo**. The bundle is the entire interface — design it so a consumer can be
built against the schema alone, without reading this mod's code.

---

## 1. Definition of Done

Loading a save (or the map editor) and triggering **Rip World** produces:

```
ModsData/Carto/<CityName>/
  bundle.json          # manifest: version, frame, CRS, extent, counts, layer index
  meshes/*.glb         # unique meshes, deduped by prefab (geometry + LODs + material refs)
  textures/*.png       # unique textures, deduped by content hash
  materials.json       # material table (maps, tints, flags) referenced by meshes
  instances.jsonl      # one line per placed object: assetId + world transform + attributes
  terrain/*.tif        # elevation (+ optional world-elevation surround)
  terrain/surface_index.png + surfaces/*.png   # per-cell ground material splat + textures
  water/*.tif          # water depth / level
  networks/*.glb + networks.jsonl              # road/rail segment meshes + attributes
  vector/*             # (unchanged) vanilla Carto GeoJSON/Shapefile, still emitted
```

Such that:
1. Every `assetId` referenced in `instances.jsonl` / `networks.jsonl` resolves to a file in
   `meshes/`, and every texture referenced by `materials.json` resolves in `textures/`.
2. Opening any `meshes/*.glb` in Blender shows a correct, textured mesh.
3. Instance transforms reproduce the in-game layout (spot-check positions against the game).
4. `bundle.json` fully specifies the coordinate frame + geo-referencing, so a consumer needs
   **no game and no this-mod code** to place everything correctly.

Success is measured **per phase** by the acceptance tests in §7, against real cities.

---

## 2. Why fork Carto (not start fresh)

Carto already does the hard 60%: it runs inside CS2, enumerates the world by domain via ECS
queries, resolves prefabs, computes a metric projection + origin, and streams data to disk off
the main thread. We reuse all of it and add geometry/material capture beside the existing
attribute capture.

Concretely, Carto already gives us (see `Instance.cs`, the service-locator):
- `Instance.Prefab` (`PrefabSystem`) — resolve an entity's prefab: `TryGetPrefab(prefabData, out T)`.
- `Instance.Terrain` (`TerrainSystem`), `Instance.Water` (`WaterSystem`),
  `Instance.Map` (`MapMetadataSystem`) — terrain height, water, map size/theme/north/lat-lon.
- Per-domain collectors (`BuildingSystem`, `NetworkSystem`, `AreaSystem`, `POISystem`,
  `RouteSystem`, `ZoningSystem`, `RasterSystem`) already produce `*Stat` records carrying
  `entity`, `prefab`, `elevation`, `category`, zoning, theme, etc.
- The per-instance transform is already read in these collectors via
  `ComponentLookup<Game.Objects.Transform>` (position `m_Position`, rotation `m_Rotation`) +
  `ComponentLookup<PrefabRef>`. **That is exactly the join we need** (instance → transform →
  prefab → mesh).
- `Geodata/` (a Burst PROJ port) + `Options.GetTMProjection()/GetTMCoord()` give the CRS,
  origin and center to geo-reference the bundle.
- `IO/` writers + `Options`/`Enums` (`Property`, `Feature`, `System`) — the attribute model.
  Note `IO.AvailablePropertyTable` already enumerates a rich set (buildings expose
  `Height`, `Storey`, `Value`, `Theme`, `Level`, `Zone`, …; networks expose `Form`, `Lane`,
  `Width`, `Direction`, `Limit`, `Length`, `Elevation`, …).

---

## 3. Extraction inventory — maximise what we draw out

Rip **everything a downstream renderer could plausibly use**, deduped by prefab. Capture-once
(mesh/material) vs per-instance (transform/attributes) is the core distinction.

| Domain | Geometry (dedup, once per prefab) | Per-instance | Attributes to carry |
|--------|-----------------------------------|--------------|---------------------|
| **Buildings** | main mesh + **sub-meshes** + **sub-buildings** (wings/extensions/upgrades) + LODs; attached **sub-objects/props** (`SubObject` buffer) as child meshes | `Transform` (pos/rot), ground `Elevation`, entity id | Name, Asset (prefab), Category, Zone, Zoning, Theme, Brand, Level, Height*/Storey*, footprint, state (construction/abandoned/destroyed) |
| **Networks** (road/rail/path) | generated **segment mesh** (surface, markings) + node/junction mesh; else cross-section (`NetCompositionData`) + centerline for consumer-side extrusion | per-edge geometry, elevation | Width, Lane count, Direction, **Form** (Normal/Elevated/Tunnel/Bridge), Limit, Length, Category, Asset |
| **Trees / plants** | mesh per species/prefab (+ LOD, seasonal variants if available) | `Transform`, growth/age state, season | Prefab, category |
| **Props / decorations / fences** | mesh per prefab | `Transform` | Prefab, category |

**Building 1:1 fidelity (a hard requirement — this is the pivot's whole point):** every real
building in the city must map to exactly one placed asset with its real geometry, replacing the
old footprint→box→guessed-height derivation. To guarantee it:
1. **Enumerate real building entities**, not footprints. **Do not** inherit any lossy filter
   (e.g. the old "drop footprints > 50,000 m²" / "drop extractors & landfill") — those existed
   only to paper over the footprint approach and are what made counts diverge.
2. **Recurse `m_subBuildings` and `m_subMeshes`**, not just decorative sub-objects: a building
   with wings, extensions, or an upgrade tier is several sub-parts; the whole set is one
   logical building and all of it must be ripped, or large buildings map partially.
3. **True height from the mesh bounds** — never derive height from `Level`/storey heuristics.
   `Height`/`Storey` attrs are carried as metadata only.
4. **Area-based "buildings"** (parks, extractors, landfills) have no single mesh — represent
   them faithfully as their sub-object props + area polygon, **not** as a synthetic box.

**Foliage & prop completeness (must-have, not best-effort):** capture **every** tree/prop
instance, which means enumerating three sources, not one:
1. **Standalone entities** — free-standing trees/props/wild vegetation placed on the map
   (`Game.Objects.Tree` + `Transform` + `PrefabRef`). Direct enumeration.
2. **Sub-objects** — street trees on roads and landscaping on lots are stored in the owner's
   **`Game.Objects.SubObject` buffer** (on net segments/lanes and on buildings), each with its
   own local transform. **Recurse these** and bake to world space via the owner's `Transform`,
   or the majority of a city's trees are missed.
3. **Ground detail / grass** — rendered as a terrain **surface/detail layer**, not discrete
   instances; it is captured via `terrain/surface_index.png` + ground textures (§4), **not**
   `instances.jsonl`. Document this so a consumer doesn't expect per-blade instances.

**Positions are low-risk and always obtainable.** A per-instance `Transform` is plain ECS
component data — unlike mesh geometry (§6, the GPU-readback unknown), it carries no feasibility
risk. Therefore full tree/prop *placement* (position + rotation + which prefab) is guaranteed
even if mesh readback needs the offline fallback; in that case a stand-in mesh is substituted
per `assetId` while every instance still lands in the right place.
| **Terrain** | — | — | Heightmap raster (reuse `RasterSystem`/`GeoTiff`); optional world-elevation surround; **surface material splat** + ground textures (grass/dirt/cliff/sand) |
| **Water** | — | — | Depth/level raster; shoreline |
| **Areas / districts / zoning** | — | polygon | Name, category, density, theme, color |
| **POIs / routes** | (marker mesh if any) | point / polyline | Type, name, address; route color/stops/transport |
| **Global / environment** | — | — | Map name, map size, **coordinate frame + CRS + origin + north**, climate/theme, time-of-day/season at export, lat/lon anchor |

\* `Height`/`Storey`/`Value` are enumerated in `IO.AvailablePropertyTable` but marked
"not available in 1.0 release" in `BuildingSystem` — with real meshes we get true height from
the mesh bounds, so treat these as best-effort metadata, not the source of truth.

Guiding rule: **for every entity, capture its prefab-resolved geometry+material once and its
per-instance transform+semantics; plus the global environment layers.** When in doubt, include
the attribute — it's cheap and a consumer can ignore it.

---

## 4. The CS2 World Bundle contract (the deliverable)

This section is the seam. Version it (frozen at `"schema": 2` since 2026-08-09 — the sketch
below predates the freeze; `docs/bundle-schema.md` is authoritative where they differ) and ship
a JSON Schema + example bundle so a consumer can be built against it with no access to this code.

### Coordinate frame (state it once, precisely)
- Positions/meshes in **CS2 world metres**. CS2/Unity is **left-handed, Y-up** (X east, Y up,
  Z north). Record this literally in the manifest so consumers can convert to their own
  convention (e.g. Z-up right-handed) deterministically.
- `bundle.json` carries: `origin` (world→geo anchor), `mapSize`, `north` offset, the CRS +
  Transverse-Mercator params from Carto's `Options`/`Geodata`, and `bounds`. Terrain rasters
  are geo-referenced by the same frame, so heightmap and meshes line up.

### `bundle.json` (manifest)
```jsonc
{
  "schema": 2,
  "generator": "carto-ripper <version>",
  "city": "<name>",
  "frame": { "units": "m", "handedness": "left", "up": "Y",
             "axes": "X=east,Y=up,Z=north" },
  "geo": { "crs": "EPSG:326xx | custom-TM", "tmParams": {…},
           "originLatLon": [lat, lon], "north": <deg> },
  "bounds": { "min": [x,y,z], "max": [x,y,z] },
  "mapSize": <m>,
  "counts": { "meshes": N, "materials": N, "textures": N,
              "instances": N, "networkSegments": N },
  "layers": {                     // relative paths, presence = availability
     "instances": "instances.jsonl", "networks": "networks.jsonl",
     "materials": "materials.json",
     "terrain": "terrain/elevation.tif",
     "surfaceIndex": "terrain/surface_index.png",
     "water": "water/depth.tif",
     "vector": "vector/" }
}
```

### `instances.jsonl` (one JSON object per line — streamable, huge-city friendly)
```jsonc
{ "assetId": "eu_residential_low_01", "kind": "building",
  "pos": [x,y,z], "rot": [qx,qy,qz,qw], "scale": [1,1,1],
  "attrs": { "category":"Residential", "zone":"…", "theme":"…",
             "level":3, "elevation":42.7, "state":"normal" } }
```
`assetId` = stable, sanitised prefab name → `meshes/<assetId>.glb`. `kind` ∈
`building|prop|tree|fence|decal|poi`. `networks.jsonl` is analogous with road attrs and either
an `assetId` (segment mesh) or an inline centerline + width for consumer-side extrusion.

### Meshes — glTF 2.0 binary (`.glb`)
One `.glb` per unique prefab. glTF carries geometry (positions/normals/UV/tangents/vertex
colors), submeshes (as primitives/nodes), LODs (glTF `MSFT_lod` or a documented naming
convention), and material references. Prefer **referencing** textures by relative path into
`textures/` (dedup) over embedding, to avoid re-storing shared atlases per mesh.

### Materials — `materials.json`
```jsonc
{ "eu_res_01_mat": { "baseColor": "textures/<hash>.png",
                     "normal": "textures/<hash>.png",
                     "mask": "textures/<hash>.png",      // CS2 control/MADS map
                     "emissive": "textures/<hash>.png",
                     "tint": [r,g,b,a], "flags": ["…"] } }
```
Map CS2's PBR material params to a small, documented set. Consumers approximate as needed.

### Textures — `textures/<sha256>.png`
Deduped by content hash. Convert CS2 texture surfaces (DDS / RenderTexture) to lossless PNG.
Keep channel semantics documented (CS2 packs roughness/metallic/AO into a mask map).

### Terrain / water / surfaces
- `terrain/elevation.tif` — reuse Carto's `RasterSystem` + `GeoTiff` writer (already exists).
- `terrain/surface_index.png` — per-cell ground-material id + `terrain/surfaces/*.png` for the
  referenced ground textures (grass/dirt/cliff/sand). **New raster** — the meaningful terrain
  upgrade over a bare heightmap.
- `water/depth.tif` — from `WaterSystem`.

### `vector/` — unchanged
The existing GeoJSON/Shapefile/GeoTIFF export still runs and lands here, so nothing is lost.

---

## 5. Concrete fork points (in this cloned Carto source)

- **`Mod.cs` / `Settings.cs` / UI** — add a **Rip World** feature toggle + a `RipOptions`
  block (which layers, LOD cap, texture size cap) alongside the existing Feature/Property tabs.
- **`IO/IO.cs` `Export(Options)`** — the single entry point Carto already exposes. Add an
  additive branch: after (or beside) the vector/raster export, invoke the ripper pipeline.
- **`IO/Options.cs` + `IO/Enums.cs`** — extend with rip options; reuse
  `GetTMProjection()/GetTMCoord()` for the manifest frame.
- **`Systems/*System.cs`** — reuse the existing `*Stat` collectors' `entity`+`prefab`+
  `Transform` joins. `BuildingSystem.CollectBoundariesJob` (uses `transformLookup` +
  `prefabRefLookup`) is the template for `InstanceRipSystem`. `NetworkSystem` for segments,
  `RasterSystem` for terrain surfaces.
- **New `Systems/MeshRipSystem.cs`** — given the set of unique prefab entities seen across all
  domains, resolve each to render data and export geometry (see §6). Dedup by prefab entity.
- **New `Systems/MaterialRipSystem.cs`** — resolve materials/textures for those prefabs; dedup
  textures by hash.
- **New `IO/GltfWriter.cs`, `IO/BundleWriter.cs`, `IO/TextureWriter.cs`,
  `IO/BundleManifest.cs`** — the bundle serializers (Newtonsoft.Json for JSON/JSONL;
  hand-written or minimal glTF writer; SharpZipLib already available if a zipped bundle is
  wanted).
- **New `Domain/MeshAsset.cs`, `MaterialAsset.cs`, `InstanceRecord.cs`** — the records.

Keep all new code additive; do not modify the vector export's behaviour.

---

## 6. Key CS2 / ECS APIs (and the central unknown)

- **Instances → transform → prefab:** query `Game.Objects.Transform` (`m_Position` float3,
  `m_Rotation` quaternion) + `Game.Prefabs.PrefabRef` (`m_Prefab`). Trees also carry
  `Game.Objects.Tree` (growth/state); buildings carry a `Game.Objects.SubObject` buffer for
  attached props.
- **Prefab → geometry:** resolve managed prefab via `Instance.Prefab.TryGetPrefab(...)`. Render
  data lives on the prefab entity as `Game.Prefabs` components — a `SubMesh` buffer, mesh/
  material data (`RenderPrefab`, mesh + material references). Vertex/index/UV data ultimately
  sits in the render/mesh systems (`Game.Rendering`).
- **THE HIGH-RISK UNKNOWN — vertex/index readback.** CS2 meshes may be resident only in GPU
  buffers, not trivially CPU-readable at runtime. **Resolve this in Phase 1 before anything
  else.** Options, in preference order:
  1. Read managed CPU-side mesh buffers if the prefab exposes them (`AllowUnsafeBlocks` is on
     for native reads).
  2. `AsyncGPUReadback` / `Mesh.isReadable` copy from the render buffers.
  3. **Fallback:** offline extraction from `Cities_Data/sharedassets*.assets` (UABE-style,
     keyed by prefab name) — geometry offline, transforms/attrs from the runtime bundle,
     joined by `assetId`. Community tools (UABE, CRP importer) prove this works.
- **Terrain/water/surfaces:** `TerrainSystem` height data → the GeoTIFF path Carto already has;
  `WaterSystem` depth; the ground-surface material grid via the render/terrain material system
  → `surface_index.png`.
- **Frame/geo:** `MapMetadataSystem` (map size, theme, lat/lon, north) + Carto `Geodata`/
  `Options` for CRS.

---

## 7. Phase-by-phase build plan (gate each on its acceptance test)

**Phase 0 — Bundle schema freeze.** Write `docs/bundle-schema.md` + a JSON Schema + a tiny
hand-authored **example bundle** (one cube mesh, one texture, two instances, a stub manifest).
*Accept:* schema validates; the example round-trips through a standalone validator; a consumer
could be written from this alone.

**Phase 1 — Mesh-readback feasibility spike (highest risk).** For a single known building
prefab, get real vertices/indices/UVs at runtime and write one valid `.glb`.
*Accept:* the `.glb` opens in Blender as the correct shape. If runtime readback fails, wire the
offline `.assets` fallback and re-accept. **Do not proceed until one path works.**

**Phase 2 — Instance enumeration.** `InstanceRipSystem`: walk buildings/props/trees **and
recurse `SubObject` buffers** (street trees on roads, lot landscaping) → `instances.jsonl`
with correct world transforms + attributes, deduped prefab set recorded.
*Accept:* instance count matches in-game object count within tolerance **including sub-object
trees** (verify a road with street trees yields one instance per tree); spot-checked positions
match; every referenced `assetId` is in the dedup set. This phase carries **no** mesh-readback
risk — positions come out regardless of Phase 1's outcome.

**Phase 3 — Mesh dump (all prefabs).** `MeshRipSystem`: export every unique prefab mesh (+
submeshes, LODs, sub-objects) to `meshes/*.glb`, deduped.
*Accept:* N unique prefabs → N `.glb`; a scene assembled from `instances.jsonl` + meshes (in
Blender via a throwaway importer script) visually reconstructs the city greybox-with-shapes.
**Building 1:1 check:** the number of building instances equals the in-game building count
(no drops), and a multi-part building (extension/wing/upgrade) reconstructs whole, not partial.

**Phase 4 — Materials & textures.** `MaterialRipSystem` + `TextureWriter`: DDS/RenderTexture →
PNG, deduped; `materials.json`; meshes reference materials/textures.
*Accept:* Blender shows textured buildings; every material/texture ref resolves; no duplicate
textures by hash.

**Phase 5 — Networks.** Rip road/rail segment + junction meshes (fallback: cross-section +
centerline) → `networks/*.glb` + `networks.jsonl` with Width/Form/Direction/Lane/Limit.
*Accept:* road surfaces reconstruct; elevated/tunnel distinguished by `Form`; centerline
fallback present where a segment mesh couldn't be read.

**Phase 6 — Terrain, water & surfaces.** Elevation + water GeoTIFFs (reuse existing) +
`surface_index.png` + ground textures.
*Accept:* rasters geo-reference to the same frame as the meshes; surface splat indexes valid
ground textures.

**Phase 7 — Manifest & packaging.** `BundleWriter` assembles `bundle.json` (frame, geo, counts,
layer index) and lays out the folder; optional zipped bundle.
*Accept:* a fresh consumer, given only the bundle + §4, resolves every reference and places
everything correctly; validator passes on a real city end-to-end.

**Phase 8 — Scale & robustness.** Big city (dense, many trees): dedup ratios, memory, export
time; stream `instances.jsonl`, cap LODs/texture sizes via options; don't stall the game.
Extended (2026-08-09) with the UX consolidation: one asynchronous **Rip World** button
orchestrating every stage with the manifest always last, a dedicated ripper tab with coarse
layer selection, and progress through the game's notification system — see
`docs/plans/2026-08-09-phase-8-scale-and-ux.md` for the full scope and the texture-cap
decision (single-choice cap stays; multi-size shipping rejected as redundant — mip chains).
*Accept:* a large save exports from the one button without OOM or lockup; counts and dedup
ratios sane; validator passes end-to-end.

Phases 0 and 1 are the gate. Everything after can iterate.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| **GPU-only mesh readback** | Phase-1 spike decides the path; offline `.assets` fallback keyed by prefab name, joined to runtime instances |
| CS2/Unity version drift breaks component layout | Pin against the current game build; isolate render-data access behind one class; log + skip unknown prefabs rather than crash |
| Networks are generated meshes, not prefabs | Rip generated segment meshes; cross-section (`NetCompositionData`) + centerline fallback for consumer-side extrusion |
| Huge cities (10⁵+ trees) → memory/time | Dedup by prefab; stream `instances.jsonl`; option caps (LOD, texture size); off-main-thread writing like Carto already does |
| CS2 PBR material model ≠ consumer's | Export raw maps + documented channel semantics; let consumers approximate |
| Coordinate/axis confusion downstream | State handedness/up/axes literally in `bundle.json`; ship an example + validator |
| **Redistribution of ripped copyrighted assets** | Local/personal only; never upload; never commit ripped assets; document the boundary in CLAUDE.md and the README |
| Breaking Carto's existing export | Keep the ripper strictly additive; regression-check the vanilla vector/raster output |

---

## 9. Non-goals (keep this repo focused)
- **No downstream conversion.** Turning the bundle into a BeamNG level / Blender scene / other
  game is a separate consumer project. This repo stops at a correct, documented bundle.
- **No redistribution/publishing** of ripped assets — ever.
- **No replacement** of Carto's vector export; the ripper sits beside it.

---

## 10. Reference documentation
- Carto data model & API: `github.com/taipei-native/Carto/wiki` (this repo is a fork).
- CS2 modding: `cs2.paradoxwikis.com` (PrefabSystem, Asset Pipeline, Surfaces), the CS2
  Modding Toolchain docs.
- Prefab/mesh access & asset dumping: `cslmodding.info/dump/`; offline tooling: UABE, the CRP
  asset importer (prove `.assets` mesh/texture extraction).
- glTF 2.0 spec (mesh interchange), `MSFT_lod` extension.

### One-line summary
A fork of Carto that, beside its existing geodata export, walks the loaded CS2 city in-engine
and rips real meshes + materials/textures + per-instance transforms + full attributes —
deduped by prefab — into a single versioned, engine-neutral **CS2 World Bundle**, so any
downstream consumer can rebuild the city with accurate geometry from the bundle alone.
