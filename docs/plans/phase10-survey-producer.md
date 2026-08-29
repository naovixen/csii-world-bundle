# Phase 10 survey — producer (C#)

Survey of the rip systems' hot paths, 2026-08-29, against the 2026-08-29 rip log (28 min 12 s:
mesh stage 1,304.6 s of which texture readback held the main thread 411.8 s; Manifest 144.5 s;
Networks & lane geometry 74.7 s; Terrain & water 68.5 s; Instances 54.8 s for 1,310,692 objects;
Lane graph 42.3 s for 680,772 lanes). Line numbers are HEAD `53e0b9d`. No file was modified by
the survey.

**Headline: the mesh stage's 1,305 s is a single-writer-thread bottleneck, not read cost.**
4,356 glb (1,761 MiB) + 4,102 PNG (2,033 MiB) = ~3.8 GiB all pushed through one background task,
fed by a queue of 8, while the main thread blocks on it.

## 1. Rip loop scheduling, budgets, main/background split

`RipOrchestratorSystem.OnUpdate` (`Systems/RipOrchestratorSystem.cs:202-214`) is a state machine:
pump progress, and if `_stageDone` advance. Stages are `SyncStage` (main thread, via
`RipProgress.RunDeferred`) or `AsyncStage` (started, reports via callback). Stage list at
`Systems/RipOrchestratorSystem.Stages.cs:21-278`.

Per-frame budgets — three loops, three constants:

| Stage | Budget | Queue cap | Loop |
| --- | --- | --- | --- |
| Mesh | 6 ms (`MeshRipSystem.cs:117`) | **8** (`:125`) | `MeshRipSystem.Walk.cs:35-76` (own loop) |
| Instances | 12 ms (`InstanceRipSystem.Run.cs:142`) | 8192 (`:148`) | `Rip/RipWalk.cs:79-137` |
| Lanes | 12 ms (`LaneRipSystem.cs:188`) | 8192 (`:193`) | `Rip/RipWalk.cs` |
| Networks | 12 ms (`NetworkRipSystem.cs:216`) | **8** (`:198`) | `NetworkRipSystem.Walk.cs:36-95` (own loop) |
| Terrain | none — one synchronous `SyncStage` | — | — |

Every `.Wait()` / `Task.Run` in fork files:

| Site | Verdict |
| --- | --- |
| `MeshRipSystem.cs:241` `run.Writer = Task.Run(() => WriteQueued(run))` | the one writer thread |
| `MeshRipSystem.cs:307` `run.Writer.Wait()` | guarded by `Walk.cs:75 if (run.Writer.IsCompleted)` — no-op |
| `InstanceRipSystem.cs:366` `Task.Run` / `Finish.cs:35` `.Wait()` | guarded by `RipWalk.cs:136` |
| `LaneRipSystem.cs:220` `Task.Run` / `Finish.cs:34` `.Wait()` | guarded |
| `NetworkRipSystem.cs:383` `Task.Run(() => Encode(run))` | encoder thread |
| `NetworkRipSystem.Assemble.cs:81-82` `CompleteAdding(); run.Encoder?.Wait();` | **unguarded** — `Walk.cs:94 Finish(run)` runs the frame the walk ends, so the main thread blocks until the encoder drains its remaining ≤ 8 items (each a full glb write) |
| **`TerrainRipSystem.Write.cs:51-63`** `Task writer = Task.Run(...); writer.Wait();` | **pure pessimisation.** Nothing runs concurrently: every raster encode + `File.WriteAllBytes` + `clipvolumes.jsonl` (~50k records) happens on a worker while the main thread sleeps on it. Same work plus a thread hop. Inside the 68 s "Terrain & water" stage |
| `RipOrchestratorSystem.Stages.cs:120` (Clean), `:249` (Manifest) | genuinely async |
| `IO/GeoTiff.cs:334-338` `Task.Run(...); .Wait()` | same anti-pattern as terrain (upstream file — not touched) |

No `.Result` anywhere. No `Parallel.*`, no `IJobEntity`, no Burst in any rip system.

## 2. Mesh stage — where the 1,305 s goes

Per prefab, on the main thread (`MeshRipSystem.Walk.cs:83 ReadOne` → `Assemble.cs:38
BuildPrefabMesh`):

1. `GetBuffer<SubMesh>` + copy to `SubMesh[]` (`Assemble.cs:60-61`).
2. `SelectSubMeshes` (`Variants.cs:122-178`) — per entry, `EntityManager.HasComponent<MeshData>` +
   `GetComponentData<MeshData>`.
3. Per selected part → `ReadMesh` (`Read.cs:286`):
   * `Instance.Prefab.TryGetPrefab` (`Read.cs:293`) — service-locator hit;
   * `asset.ObtainMeshes(true)` (`Read.cs:67`) loads the geometry asset off disk, then
     `asset.ReleaseMeshes()` in `finally` (`:160`);
   * `GeometryAssetReader.ReadAll` (`GeometryAssetReader.cs:107`) → per mesh `Read`
     (`.Attributes.cs:31`): `NativeArray<MeshVertex>(Allocator.Temp)` + per-vertex copy loop
     (`:53`), same for indices (`:65`), then three `ReadAttribute<,>` calls each allocating a
     `NativeArray` + a managed `TValue[]` and running a **delegate call per vertex**
     (`GeometryAssetReader.cs:212 result[i] = select(buffer[i])`). ~20.7 M triangles per rip;
   * a **second full bounds pass** over vertices at `.Attributes.cs:117-124` — then
     `GltfWriter.Primitives.cs:45-54` computes min/max again on the writer thread. The first is
     dead work for anything going to glb;
   * `AssignMaterials` (`SubObjects.cs:42`) → `Instance.MaterialRip.Resolve` per primitive —
     **this is where texture readback fires** (§3).
4. `BuildStateVariants` (`Assemble.cs:87-102`) re-runs `SelectSubMeshes` + `AssembleParts`
   (**including `ReadMesh` again**) per variant. A tree with `__dead` + `__stump` re-reads and
   re-decodes the same geometry asset 3×.
5. `ReadOne` then enqueues prefab + skirt + N variants + M stack parts + rest — all between the
   loop's single capacity check (`Walk.cs:55`).

glb serialisation and write: on the writer thread (`Write.cs:30-70` → `GltfWriter.Write` →
`BundleFile.WriteAllBytes` → `File.WriteAllBytes`). Synchronous file IO, but not on the main
thread. **Parallelism across prefabs: none.** One reader (main thread), one writer task.

**The main thread blocks on the writer.** `RipQueue.Enqueue` (`Systems/Rip/RipQueue.cs:48-55`)
does `queue.TryAdd(item, 200)` in a loop. With `QueueCapacity = 8` and one writer doing PNG
deflate + SHA-256 + 3.8 GiB of `File.WriteAllBytes`, the main thread parks here constantly.
1,305 s ÷ 4,304 prefabs = 303 ms/prefab against a 6 ms budget — the budget is irrelevant; the
stage is writer-gated.

Per-prefab allocation churn on the writer thread (`GltfWriter.Build`, `IO/GltfWriter.cs:95-243`):
the entire glTF JSON is `List<object>` of `Dictionary<string, object>` — ~10 dictionaries per
primitive, boxed ints/floats, `new[] { min.x, min.y, min.z }` per accessor (`Primitives.cs:63-64`),
then `JsonConvert.SerializeObject` (`GltfWriter.cs:227`), then `Pad` (`:279-288`) copies the whole
BIN chunk, then `glbStream.ToArray()` (`:242`) copies it again, then `File.WriteAllBytes`. Three
full copies of the payload per mesh.

String concat in the hot path: `$"{assetId}_mat{i}"` per primitive at `Read.cs:377`,
`GeometryAssetReader.cs:148`, `.Attributes.cs:108`, `SubObjects.cs:60`,
`GltfWriter.Primitives.cs:129`. `IO/GltfWriter.cs:204` `$"csii-world-bundle {Instance.Version}"`
→ `Instance.Version` is `Assembly.GetCustomAttribute<AssemblyFileVersionAttribute>()`
(`Instance.cs:271`) — **a reflection call per .glb, 4,356×**. `BundleFile.EnsureDirectory`
(`IO/BundleFile.cs:70-74`) does a `Directory.Exists` syscall on every one of the 4,356 writes.

## 3. Texture readback — 411.8 s on the main thread

Route (`MaterialRipSystem.cs:117-152 Take`): `Claim(key)` → `ReadBack` / `ReadBackVirtual` /
`ReadBackLive` → `Capture` → `Blit`.

`Blit` (`MaterialRipSystem.Capture.cs:238-270`) — **not** `AsyncGPUReadback`:

```csharp
Graphics.Blit(source, target);
RenderTexture.active = target;
readback = new Texture2D(width, height, TextureFormat.RGBA32, false, linear: !srgb);
readback.ReadPixels(new Rect(0,0,width,height), 0, 0, false);   // full pipeline stall
readback.Apply(false, false);
byte[] raw = readback.GetRawTextureData();
```

`Texture2D.ReadPixels` is a synchronous GPU→CPU stall. `AsyncGPUReadback` is used elsewhere in
this repo (`TerrainRipSystem.Backdrop.cs:61-62`, `FineCascade.cs:269`,
`SharedDataCollectionSystem.cs:1163`), so the API is not the obstacle. This is essentially all of
the 411.8 s. Then per texture, still on the main thread:

* `Blit` allocates a `Texture2D` and destroys it every call (`:250`, `:268`) — 4,102 allocs/frees;
* `GetRawTextureData()` returns a copy, then another full copy for the row flip (`:258-260`) —
  2 × 2,032 MiB of memcpy;
* `ReconstructTwoChannelNormal` (`Capture.cs:177-225`) — normal maps only, two full passes over
  every texel, the second with `Mathf.Sqrt` + 2 `Mathf.RoundToInt` per texel.

Encoding and write: on the mesh writer thread (`TextureWriter.Write`, `IO/TextureWriter.cs:121-155`)
— own encoder, not `ImageConversion.EncodeToPNG` (correct: Unity refuses BC-compressed sources,
and this one is deterministic).

**Content-hash dedupe runs AFTER encoding** (`TextureWriter.cs:132-142`):

```csharp
byte[] png = Png.EncodeRgba(...);      // full deflate
string hash = Sha256(png);              // full hash
if (!_hashes.TryAdd(hash, true)) { Deduped++; return; }   // too late
```

A duplicate image still pays the full deflate + SHA-256. The ordering is also architecturally
load-bearing: because the bundle path is the hash of the encoded PNG, the glb naming a texture
cannot be serialised until that texture is encoded — which is why there is one queue and one
writer (`MeshRipSystem.Run.cs:38-45`, `MaterialRipSystem.Describe.cs:28-32`). Hashing the raw RGBA
pixels instead would break that coupling and let PNG encoding fan out across cores. There is an
earlier dedupe by asset key — `Claim` (`TextureWriter.cs:107`), called at
`MaterialRipSystem.cs:115` — which prevents re-reading the same asset.

## 4. Manifest — 145 s

**It does not SHA-256 anything.** The only `SHA256` in the codebase is
`IO/TextureWriter.cs:170-178`. The 145 s is:

1. `ReadMeshBounds` (`IO/BundleWriter.Bounds.cs:23-48`) — for each of ~12k glb: open, seek to the
   JSON chunk (`BundleWriter.cs:285-307`), `Encoding.UTF8.GetString`, `JObject.Parse`
   (`Bounds.cs:55`), then walk every mesh/primitive/accessor as `JToken`s. Newtonsoft
   LINQ-to-JSON over 12k documents dominates. Single-threaded.
2. Full record re-read — `ScanPlacements` deserialises all 1.31 M instances into `PlacementRow`
   objects with `double[]` fields (`Scan.cs:58-98`), 680k lanes (`ScanPointRows`), 27k networks,
   ~50k clip volumes.
3. `Png.DecodeGray16` of `heightmap.png` and `water_level.png` in full (`Scan.cs:268`, `:290`)
   just to find min/max.
4. Layer sizing (`BundleWriter.cs:239-241`) — `Directory.GetFiles(path, "*", AllDirectories)` +
   `new FileInfo(file).Length` per file, ~17k stat calls.

Every number it recomputes is already known at write time: `GltfWriter.Build` computes the exact
accessor min/max it later re-parses (`Primitives.cs:45-54`), `MeshRipSystem` tracks per-file sizes
(`run.Sizes`, `Run.cs:144`), the writers count records (`JsonLinesWriter.Count`). Failing sidecars,
`ReadMeshBounds` is embarrassingly parallel.

## 5. InstanceRipSystem — 55 s / 1.31 M objects

Walk shape: `_objectQuery.ToEntityArray(Allocator.Persistent)` (`InstanceRipSystem.cs:304`) then
an index loop (`RipWalk.cs:103-123`). No chunk iteration, no `IJobEntity`, no Burst.

Hoisted lookups: good — 13 `ComponentLookup`/`BufferLookup` fields (`InstanceRipSystem.cs:173-185`),
refreshed per slice (`:223-244`). But `AddAttributes` still goes to `EntityManager` ~12× per
entity (`InstanceRipSystem.Attributes.cs`): `GrowthScaleData` (`:133,135`),
`Destroyed`/`Abandoned`/`Condemned`/`UnderConstruction` (`:156-159`), `SpawnableBuildingData`
(`:166,168`), `Renter` buffer (`:186,188`), `CompanyData` (`:192,194`), `CurrentDistrict`
(`:214,215`), `Quantity` (`:256,258`). `state` fires on every building and `Quantity` is probed
on every entity.

Two full pre-pass walks before the slice loop:

* `InstanceRipSystem.cs:331-355` — 1.31 M iterations of `IsUtilityMarkerSubObject` +
  `IsVehicleCargoSubObject` (each several `EntityManager.HasComponent` + a `SubMesh` buffer scan,
  `SubObjects.cs:128-218`), synchronous, in `BeginRip`, on one frame.
* `MeshRipSystem.Collect.cs:57-101` walks the identical 1.31 M query again and recomputes the
  identical two exclusion tests — the `NativeBitArray` cache the instance run built (`Run.cs:42`)
  is not shared. `RipAssetIds.BuildAssetIds` also runs twice over the full prefab set
  (`InstanceRipSystem.cs:360`, `Collect.cs:110`).

Per-entity: `ClassifyKind` (`Classify.cs:27-39`) is lookup-based; `DecalOf` is cached per prefab;
`Instance.Name.GetPrefabName` at `Attributes.cs:195` (brand) is uncached and per-entity;
`:220-221` (address) per building, uncached.

Writer (`IO/InstanceWriter.cs:36-52`, on the writer task): a `Dictionary<string, object>` per
record with boxed ints, `BundlePrecision.Vector`/`Rotation` (`IO/BundlePrecision.cs:38-56`)
allocating a fresh `double[]` per call — 1.31 M × 2 arrays minimum, plus one `double[3]` per
attribute value; `record.Attributes` is `Dictionary<string, object>` with every scalar boxed
(`Attributes.cs:40, 64, 68, 119, 125, 139, 169, 258`). JSON is Newtonsoft:
`JsonLinesWriter.WriteLine` (`IO/JsonLinesWriter.cs:64`) does
`JsonConvert.SerializeObject(line, Formatting.None)` — a reflection-based walk over a boxed
dictionary, 1.31 M times, allocating each whole line as a string. `RecordWriter`
(`IO/RecordWriter.cs:38`) is the same for areas/districts/clipvolumes.
`Stopwatch.Elapsed.TotalMilliseconds` is read once per entity (`RipWalk.cs:122`).

## 6. LaneRipSystem.ProcessOne — per-lane EntityManager calls

`LaneRipSystem` declares zero `ComponentLookup` fields. Every probe is a raw `EntityManager` call:

| # | Site | Call |
| --- | --- | --- |
| 1-2 | `LaneRipSystem.cs:278` | `Exists`, `HasComponent<Curve>` |
| 3 | `:285` | `GetComponentData<Curve>` |
| 4-5 | `:287-289` | `HasComponent<PrefabRef>`, `GetComponentData<PrefabRef>` |
| (6-7) | `:294-295` | `HasBuffer<SubMesh>` + `GetBuffer<SubMesh>` — prefab-cache miss only |
| 6-10 | `Lane.cs:28-32` (`Classify`) | `HasComponent<CarLane|TrackLane|PedestrianLane|ParkingLane|ConnectionLane>` — up to 5 |
| 11 | `:313` | `HasComponent<SecondaryLane>` |
| 12-13 | `:319-321` | `HasComponent<Owner>`, `GetComponentData<Owner>` |
| 14-18 | `Lane.cs:45-71` (`AddLaneData`) | `HasComponent<CarLane>` … + one `GetComponentData` — up to 5 |

Typical 15, worst 18, minimum 13, zero hoisted — ~10 M EntityManager resolutions per rip. Four
are redundant every lane: `Classify` and `AddLaneData` both probe the same four lane types.

**The real hot spot is the flags string.** `Lane.cs:50, 59, 65, 71`: `lane.m_Flags.ToString()`
on a `[Flags]` enum → reflection into the enum's name table, a comma-joined string, then
`AddFlags` (`Lane.cs:83-92`) does `.Split(',')` + `.Trim()` + `List.Add` per name. Same pattern
at `InstanceRipSystem.Attributes.cs:47` (`elevation.m_Flags.ToString()` → `FlagNames` → `Split`
→ `ToArray`) and `:149` (`tree.m_State.ToString()`). A static flag→`string[]` table built once
per enum type would eliminate it.

## 7. Terrain / network edge walks

Within `TerrainRipSystem`: two full walks of `_edgeQuery` (+ two of `_orphanQuery`), not three —
`TerrainRipSystem.Roadbed.cs:50` (`StampRoadbed`) + `:104` from `CaptureRoadbed`
(`TerrainRipSystem.cs:250`); `TerrainRipSystem.Clip.cs:58` (`StampCutout`) + `:144` + `:206`
(`_clipAreaQuery`) from `CollectClipVolumes` (`TerrainRipSystem.cs:252`); plus
`TerrainRipSystem.Areas.cs:55` (`_districtQuery`) and `:153` (`_surfaceAreaQuery`). Across the
whole rip: three, since `NetworkRipSystem.cs:316` walks its own `_edgeQuery`.

The two terrain walks read a nearly identical chain per edge — `PrefabRef → NetGeometryData`
(flags gate), `Composition → {m_Edge, m_StartNode, m_EndNode} → NetCompositionData →
TerrainComposition`, `EdgeGeometry`, `StartNodeGeometry`, `EndNodeGeometry` (`Roadbed.cs:57-96`
vs `Clip.cs:65-136`). The only differences are the flag gate (`FlattenTerrain|ClipTerrain` vs
`ClipTerrain`) and `TryGetGroundComposition` vs `TryGetClipBand`; both funnel into the same
`StampSegment`/`StampNode` rasteriser (`TerrainRipSystem.Strips.cs`). `Clip.cs` calls
`TryGetClipBand` twice per node (`:101`/`:105`, `:120`/`:124`), each re-reading
`NetCompositionData` + `TerrainComposition` from the same entity.

## 8. `Instance.` service locator

Every one of these is `World.DefaultGameObjectInjectionWorld.GetOrCreateSystemManaged<T>()` on
every access (`Instance.cs:31-289`). Only `Log`, `Name`, `Rb`, `Rsa`, `Xtm`, `Zcc`, `Settings`
are cached. `Prefab`, `City`, `Terrain`, `Water`, `MaterialRip`, `MeshRip`, `Camera`, `Shared`,
`Simulation` are not. `Instance.Version` (`:271`) is `Assembly.GetCustomAttribute<>()`.

In-loop call sites (~35): `MeshRipSystem.Read.cs:293` (per sub-mesh part);
`MeshRipSystem.SubObjects.cs:85` (per part + `Collect.cs:145` per work item);
`MeshRipSystem.SubObjects.cs:64, 68, 71` `Instance.MaterialRip` ×3 per primitive;
`MeshRipSystem.Collect.cs:256, 268, 272, 273, 278, 279`; `MeshRipSystem.SubMesh.cs:55`
`Instance.City.leftHandTraffic` per `SelectSubMeshes` call; `Rip/RipAssetIds.cs:116`;
`InstanceRipSystem.Attributes.cs:195, 220, 221`; `NetworkRipSystem.Piece.cs:92, 158`,
`GameMesh.cs:122, 253`, `LanePrefabs.cs:118, 205`, `Composition.cs:304`, `Attributes.cs:103, 220`,
`Orphans.cs:95`; `NetworkRipSystem.Conform.cs:37, 43`; **`IO/GltfWriter.cs:204` `Instance.Version`
per .glb**; `TerrainRipSystem.Splat.cs:103, 104, 263, 264`, `Appearance.cs:67, 186`,
`Backdrop.cs:54, 82, 83`. The pattern to follow exists at `AreaSystem.cs:56`,
`BuildingSystem.cs:66`, `RasterSystem.cs:37`: `static readonly PrefabSystem _prefab = Instance.Prefab;`.

## 9. `IO/Png.cs` encoders

`EncodeGray16` (`Png.cs:91-114`) — a per-sample byte-splitting loop, two bounds-checked stores +
multiply-add index per sample; a 4096² heightmap is 16.7 M iterations. `EncodeRgba` (`:35-53`)
and `EncodeGray8` (`:65-79`) use `Buffer.BlockCopy` per row but all three allocate a full
`(stride+1) × height` staging buffer to interleave a zero filter byte per row — +2,032 MiB of
allocation and memcpy across the texture stage. Feeding the deflater row-by-row would remove the
copy. Filter type 0 on every scanline (`:48, :73, :103`), a documented determinism choice
(`:17-19`). Deflate level `DEFAULT_COMPRESSION` (= 6) at `:155`; `IO/BundleFile.cs:14-21` records
gzip-9 on the glb path was reverted (COMP-1) for the same single-writer reason, and the argument
was not carried over to PNG. `Deflate` allocates a 64 KiB scratch per call (`:160`) and a final
`output.ToArray()` copy (`:168`).

## 10. Other redundant / quadratic work

1. Bounds computed twice per mesh — `GeometryAssetReader.Attributes.cs:117-124` (main thread)
   then `GltfWriter.Primitives.cs:45-54` (writer thread).
2. `ReadMesh` re-invoked per state variant (`Assemble.cs:96`) — each call redoes
   `ObtainMeshes`/`ReleaseMeshes` and full attribute decode of the same asset.
3. The 1.31 M object query is walked twice (`InstanceRipSystem.cs:331`,
   `MeshRipSystem.Collect.cs:64`) with the same exclusion predicates recomputed.
4. `RipAssetIds.BuildAssetIds` runs twice over the full prefab set.
5. `ResolveBaseMaterial` calls `_meshSettingsQuery.CalculateEntityCount()` per Base-flagged part
   (`Collect.cs:263`) — a chunk-count scan for a singleton.
6. Double dictionary lookup idiom `TryGetValue; dict[k] = v + 1` — `InstanceRipSystem.Walk.cs:82-83,
   100-101, 201-202, 208-209`, `LaneRipSystem.cs:302-303, 332-333`,
   `NetworkRipSystem.Walk.cs:302-303, 306-307`, `MeshRipSystem.Collect.cs:84-85`.
7. `NetworkRipSystem.Walk.cs:301, 305`: `ToString()` on an already-string boxed object per edge.
8. `GltfWriter.Primitives.cs:124-140` builds a `List<string>` + `Dictionary<string, List<>>` per
   mesh keyed on freshly-concatenated strings.
9. `BaseSkirtBuilder.CollectBaseLines` (`IO/BaseSkirtBuilder.cs:143-231`) — two full passes over
   every triangle, the first only to fill a `HashSet<float4>` of excluded edges, then two
   `Contains` per candidate edge (`:184-185`). Per Base-flagged part, on the writer thread.
10. `Domain/NameManager.GetPrefabName` (`:84-97`) — no cache; called per entity in several paths.
11. `Utils.IOUtils.CombinePath` + `$"{prefab.AssetId}.glb"` per write (`Write.cs:56, 113`).
12. `MaterialRipSystem.cs:220-224` — 7 `string.Contains` after a `ToLowerInvariant()` per surface.

## Ranked wins

| # | Change | Target |
| --- | --- | --- |
| 1 | Replace `ReadPixels` with `AsyncGPUReadback` in `Capture.cs:238-270` | ~410 s of main-thread stall |
| 2 | Hash raw pixels (not the PNG) so the texture path is known pre-encode → fan PNG encoding across a task pool; raise `QueueCapacity` | breaks the 1-writer gate on ~1,300 s |
| 3 | Drop deflate to level 1-3 in `Png.cs:155`; feed the deflater per row (no staging buffer) | writer throughput + 2 GiB alloc |
| 4 | Delete `Task.Run(...).Wait()` at `TerrainRipSystem.Write.cs:51-63` | free |
| 5 | Merge `StampRoadbed` + `StampCutout` into one edge walk; hoist the doubled `TryGetClipBand` | ~half of terrain's edge cost |
| 6 | Precompute flag-name tables; kill `m_Flags.ToString()` + `Split` in `Lane.cs:50,59,65,71` and `Attributes.cs:47,149` | large slice of the 42 s lane stage |
| 7 | Add `ComponentLookup`s to `LaneRipSystem`; merge `Classify`/`AddLaneData` probes | ~10 M → ~4 M lookups |
| 8 | Cache `Instance.Prefab`/`City`/`MaterialRip`/`Version` in `static readonly` fields | ~35 in-loop sites |
| 9 | Emit per-stage sidecars (bounds, counts) at write time; parallelise `ReadMeshBounds` | ~145 s manifest |
| 10 | Share the exclusion bitarrays + asset-id table between instance and mesh rips | one 1.31 M walk |
| 11 | Hand-write JSON lines instead of `JsonConvert.SerializeObject(Dictionary<string,object>)` | instance/lane writer alloc |
| 12 | Drop the dead bounds pass at `GeometryAssetReader.Attributes.cs:117-124`; cache `ReadMesh` per mesh entity across state variants | mesh read |
