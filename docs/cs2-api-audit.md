# CS2 API audit — what the ripper reads, and what it is missing

An audit of every Cities: Skylines II API the rip path touches, checked against the game's own
assemblies — originally with the retired `tools/mdump` IL walker, now with
[`tools/decomp`](../tools/decomp/README.md), which decompiles to real C# — plus a sweep for data
the game exposes that the bundle does not yet carry.

**Why this document exists.** The compiler proves a type and a field *exist*. It proves nothing
about what they *mean*, and a field read with the wrong meaning is a silent defect — it produces a
plausible number that is wrong. Every row below therefore records not just "resolves" but what the
game itself does with the value, taken from IL where the name alone was not enough.

**Authority.** The shipped assemblies are the authority, the CS2 modding wiki is corroboration.
Where they disagree, the assembly wins; where the assembly is ambiguous, the disagreement is
recorded rather than resolved by preference. See [§6](#6-what-the-wiki-confirms-and-what-it-does-not).

Audited against the game build under `CSII_MANAGEDPATH` as of 2026-08-05. Re-run the recipe in
[§7](#7-re-running-this-audit) after a game update.

---

## 1. Scope and result

| | |
|---|---|
| Type names taken from the rip path's `ComponentLookup<>` / `GetComponentData<>` / `HasBuffer<>` arguments | **76** — all resolve, all with the expected shape |
| Distinct `m_*` fields read off them | **79** — all present, all with the expected type |
| Managed classes and system APIs | `PrefabSystem`, `NameSystem`, `CityConfigurationSystem`, `TerrainSystem`, `WaterSystem`, `SurfaceAsset`, `VTSurfaceAsset`, `TextureAsset`, `BatchDataHelpers`, `MeshVertex/Normal/Tangent/UV0/Index.Unpack` — all resolve |
| Defects found | **4** (§4) — all fixed |
| Reachable data the bundle does not carry | **9 groups** (§5) — open |

Nothing the ripper currently reads is missing from the game or misspelled. Every defect found is a
*meaning* defect: a value read correctly and then described or combined wrongly.

---

## 2. The validated surface, by bundle layer

### 2.1 Instances — `instances.jsonl`

Query: `Object + Transform + PrefabRef`, excluding `Moving`, `Deleted`, `Temp`,
`Objects.Marker`, `Creature`, `Vehicle`.

| API | Read for | Verdict |
|---|---|---|
| `Game.Objects.Transform.m_Position / m_Rotation` | `pos`, `rot` | Confirmed. `float3` + `quaternion`, world space, no parent chain to compose. |
| `Game.Prefabs.PrefabRef.m_Prefab` | the prefab join | Confirmed. Points at the prefab **entity**, not `PrefabBase`; wiki agrees. |
| `Game.Common.Owner.m_Owner` | `parent` | Confirmed. Wiki: "used to denote the owner of an entity". |
| `Game.Objects.Attached.m_Parent` | `parent` fallback | Confirmed, and correctly used only as a fallback — `Attached` also carries `m_OldParent` and `m_CurvePosition`, i.e. it is a bind-to-network relation, not ownership. |
| `Game.Objects.Elevation.m_Elevation / m_Flags` | `attrs.elevation`, `attrs.elevationFlags` | Value was always correct; the contract's description of it was wrong, and `m_Flags` was dropped — see §4.1. |
| `Game.Objects.Color.m_Index / m_Value / m_SubColor` | `attrs.color` | Confirmed against the renderer. `BatchDataJob.UpdateObjectData` builds `float2(m_Index + 0.5f, m_Value * 0.003921569f)` — so `m_Value / 255` is exactly the game's own scaling, and `m_Index` is the palette row. |
| `Game.Objects.Tree.m_State / m_Growth` | `attrs.treeState`, `attrs.growth` | Confirmed. `TreeState` = `Teen 1, Adult 2, Elderly 4, Dead 8, Stump 16, Collected 32`; a sapling really is `0`, so omitting the key rather than writing `"0"` is right. `m_Growth` is a `byte`, so `/255` is right. |
| `Game.Objects.Stack.m_Range`, `Game.Prefabs.StackData` | stack expansion | Confirmed. Both overloads of `Game.Rendering.BatchDataHelpers.CalculateStackSubMeshData` match the two call sites signature-for-signature. |
| `Buildings.Building`, `Buildings.Abandoned`, `Buildings.Condemned`, `Common.Destroyed`, `Objects.UnderConstruction` | `attrs.state` | Confirmed as presence tests. Wiki confirms `Destroyed` = "destroyed structures, for example a collapsed building". |
| `Game.Tools.Temp`, `Game.Common.Deleted` | exclusion | Confirmed. Wiki: `Temp` = "about to be created/updated, but not finalized"; `Deleted` = "recently deleted or should be deleted". Excluding both is right. |
| `Game.Objects.Marker`, `Game.Net.Marker` | exclusion | Confirmed as empty tag components, which is what the exclusion in contract §4.3 assumes. |

`Game.Objects.SubObject` is confirmed to hold **only** `m_SubObject` (an `Entity`) — so
`InstanceRipSystem`'s claim that sub-objects need no recursion, because each is an ordinary entity
with its own world `Transform`, is correct against the type as shipped.

### 2.2 Meshes — `meshes/*.glb`

| API | Read for | Verdict |
|---|---|---|
| `Game.Prefabs.SubMesh.m_SubMesh / m_Position / m_Rotation / m_Flags` | node hierarchy, variant filter | Confirmed — **including the transform gate, which turns out not to matter. See §3.1.** |
| `Game.Prefabs.MeshVertex / MeshNormal / MeshTangent / MeshUV0 / MeshIndex` | geometry | Confirmed. Each is an `IBufferElementData` with a single field and static `Unpack` overloads — CPU-side ECS buffers on the prefab entity, which is why PLAN §6's GPU-readback risk never materialised. |
| `Game.Prefabs.MeshMaterial.m_StartIndex / m_IndexCount / m_StartVertex / m_VertexCount / m_MaterialIndex` | primitives | Confirmed. |
| `Game.Prefabs.MeshData.m_Bounds / m_State / m_SubMeshCount / m_IndexCount` | decal test, census | Confirmed. `MeshFlags.Decal = 0x1`. |
| `Game.Prefabs.SubMeshFlags` | the one-state filter | Confirmed bit for bit. All 19 `Require*` bits are covered by `RequireFlags`, and `OutlineOnly = 0x1000000` is excluded. |
| `Game.Prefabs.LodMesh.m_LodMesh`, `SharedMeshData.m_Mesh` | LODs, shared geometry | Confirmed. |
| `PrefabSystem.TryGetPrefab`, `NameSystem` | asset ids | Confirmed; wiki documents `TryGetPrefab(prefabEntity, out PrefabBase)` as the supported route. |
| `CityConfigurationSystem.leftHandTraffic` | traffic-side variant | Confirmed as a public property. |

### 2.3 Materials and textures

| API | Read for | Verdict |
|---|---|---|
| `SurfaceAsset`, `SurfaceAsset.vtSurfaceAsset`, `VTSurfaceAsset.Load` | the texture route | Confirmed; unchanged from the virtual-texturing findings, now recorded in [`tools/decomp/README.md`](../tools/decomp/README.md). |
| `SurfaceAsset.GetPreProcessedTextureGuid` | **avoided** | Still broken in this build: indexes with `stack * layer` where the flat index is `stack * 4 + layer`. The producer's own `PreProcessedGuid` is still required. |
| `_BaseColorMap`, `_NormalMap`, `_MaskMap`, `_EmissiveColorMap`, `_ControlMask` | the five maps | All confirmed present in `Game.dll`'s `#US` heap. `_ControlMask` was the gap — see §4.2 and §3.2. |
| `MaterialLibrary.MaterialDescription.m_Stacks[].m_PropertyNames` | texture cleanup, **and now the run's own map census** | Confirmed. This is the game's own data-driven list of what a decode binds; it is now also accumulated across the run and reported, so a map outside the contract's five shows up in the log instead of going unnoticed. |
| `Colossal.IO.AssetDatabase.TextureAsset` | PNG output | Confirmed. |

### 2.4 Networks and lanes

| API | Read for | Verdict |
|---|---|---|
| `Game.Net.Edge.m_Start / m_End`, `Node.m_Position / m_Rotation`, `ConnectedEdge.m_Edge` | topology | Confirmed. |
| `Game.Net.Curve.m_Bezier / m_Length` | centerlines, lane paths | Confirmed. `m_Length` is the game's own arc length, so publishing it rather than the polyline's length is right. |
| `Game.Net.EdgeGeometry.m_Start / m_End / m_Bounds`, `Start/EndNodeGeometry.m_Geometry` | the bend | Confirmed. |
| `Game.Net.Composition.m_Edge / m_StartNode / m_EndNode` | cross-section lookup | Confirmed. |
| `NetCompositionData` (`m_Width`, `m_MiddleOffset`, `m_SyncVertexOffsetsLeft/Right`, `m_Flags`, `m_State`) | assembly + vertex sync | Confirmed field for field. |
| `NetCompositionPiece` (`m_Piece`, `m_Offset`, `m_Size`, `m_SectionFlags`, `m_PieceFlags`) | pieces | Confirmed. |
| `NetPieceData`, `NetCompositionLane`, `NetCompositionMeshRef`, `NetGeometryData.m_DefaultWidth` | widths, lanes, meshes | Confirmed. |
| `RoadComposition` / `TrackComposition` / `PathwayComposition` / `WaterwayComposition` / `TaxiwayComposition` / `PipelineData` / `PowerLineData` / `FenceData` | `attrs.category`, `limit` | Confirmed. Reading category off the composition's components rather than the lane make-up is sound: each is a distinct component type, so a modded network carrying `RoadComposition` classifies as a road with no table lookup. |
| `Game.Net.Elevation.m_Elevation` | `attrs.elevation` | Present, type `float2`. **Averaged into one number — see §4.3.** |
| `Game.Net.Lane`, `CarLane`, `TrackLane`, `PedestrianLane`, `ParkingLane`, `ConnectionLane`, `SecondaryLane` | `lanes.jsonl` | Confirmed. `CarLane.m_SpeedLimit` (not `m_DefaultSpeedLimit`) is the effective limit, which is the right choice; both `CarLane` and `TrackLane` carry `m_Curviness`. |

---

## 3. Suspicions that measurement killed

Kept because a reasonable reading of the field names says otherwise, and because re-deriving them
after the next game update is cheaper than re-discovering them.

### 3.1 `SubMeshFlags.HasTransform` — the ripper is right to ignore it

`BatchDataSystem.BatchDataJob.UpdateObjectData` applies a sub-mesh's local transform only when

```
IL_0463 ldfld  Game.Prefabs.SubMesh::m_Flags
IL_0468 ldc.i4 2154496 (0x20E000)        # HasTransform | IsStackStart | IsStackMiddle | IsStackEnd
IL_046D and
IL_046E brfalse.s -> IL_04A8             # ...else use the object transform verbatim
```

`MeshRipSystem` applies `m_Position` / `m_Rotation` unconditionally, which reads like a defect. It is
not, and the reason is upstream: `ObjectInitializeSystem.InitializePrefab` sets `HasTransform`
**if and only if** `m_Position != float3.zero || m_Rotation != quaternion.identity`, and the
three-argument `SubMesh` constructor writes `float3.zero` and `quaternion.identity` explicitly. An
entry without the flag therefore carries an identity transform, and applying it is a no-op. Leave it
alone.

### 3.2 The `*Map` sweep was the wrong sweep — and it looked conclusive

Every `_*Map` / `_*Texture` / `_*Tex` string in `Game.dll` was enumerated first. Beyond the ripper's
four, the list is `_BaseHeightMap`, `_HeightMap`, `_Heightmap`, `_SnowMap`, `_SplatMap`,
`_Splatmap`, `_UserSplatmap`, `_WorldMap`, `_WorldspaceMaskMap`,
`_TerrainLegacy{Grass,Dirt,Rock}Texture`, `_DetailHeightTexture`, `_AnimationTexture`,
`_BrushTexture`, `_NoiseTex`, `_DepthFadeTex`, `_SeaPropagationTexture`, `_WaterTexture`,
`_WorldTexture` — terrain, water, snow and tool overlays, not one of them an object-surface map.
That reads like proof the four were complete. It is not: **`_ControlMask` does not end in `Map`**
(§4.2), and the sweep that would have found it is `strings Game '^_[A-Za-z0-9]+$'`, with no suffix
assumed.

This is the audit's own near-miss, kept because the shape of the mistake generalises: a regex over
the string heap answers the question you encoded, and an exhaustive-looking result over the wrong
population is more misleading than no result at all. The durable fix is not a better regex — it is
§4.2's report line, which makes the game enumerate its own maps every run.

### 3.3 An extra condition can be a bug all by itself

The buried utility node props — `GroundCableNode*`, and the water and sewage equivalents — kept
reaching the bundle despite a filter written to drop them. The filter asked for three things: the
object has an `Owner`, the owner carries `Game.Net.Marker`, and the owner's **prefab** carries
`PipelineData` or `PowerLineData`. The first two are right. The third is what let them through:

```
mdump find Game 'ComponentType::Read[A-Za-z]*<Game\.Net\.Marker>'
  -> Game.Prefabs.MarkerNet::GetArchetypeComponents          (twice: node archetype, edge archetype)
  -> Game.Rendering.GuideLinesSystem::OnCreate               (ReadOnly, a query)

mdump find Game 'ComponentType::Read[A-Za-z]*<Game\.Prefabs\.(MarkerNetData|TaxiwayData)>'
  -> Game.Prefabs.MarkerNet::GetPrefabComponents             -> MarkerNetData
  -> Game.Prefabs.TaxiwayPrefab::GetPrefabComponents         -> TaxiwayData
```

`Game.Net.Marker` has exactly one source in the whole game, and it is applied to the node and edge
archetypes alike — so an owner carrying it **is** a marker net, and the second condition was
already exact. `PipelineData` and `PowerLineData` come only from `PipelinePrefab` and
`PowerLinePrefab` (their `GetPrefabComponents`), and the marker nets a road lays along itself are
not those classes. The third condition could therefore only ever remove things the second had
already identified correctly, and it removed all of them.

The clause was there to spare taxiways, which are marker nets and yet are genuinely drawn.
`TaxiwayData` names that exception directly and has one source, so the exception is now stated as
itself rather than approximated by listing the network types that are not taxiways.

**The generalisable part is the shape, not the components.** An added condition intended to protect
one case silently deleted the filter's entire purpose, and nothing said so: the skip counter still
incremented, on the objects that were *not* the problem. The filter now names what it dropped, per
prefab, in the rip report — so "it ran" and "it caught the right things" stop being the same
observation.

### 3.4 `MeshMaterial.m_MaterialIndex` has two meanings, and the ripper reads the benign one

The ripper resolves a primitive's surface as `renderPrefab.GetSurfaceAsset(m_MaterialIndex)`, and
`GetSurfaceAsset(int)` is plain positional indexing into `m_SurfaceAssets`. That is only correct
while `m_MaterialIndex` is the authored *local* slot — and the game rewrites it.
`Game.Prefabs.MeshSystem.OnUpdate` resizes a processed prefab's `MeshMaterial` buffer to
`materialCount`, walks `surfaceAssets` **in order**, and stores per entry only
`m_MaterialIndex = GetMaterialIndex(surface)` — a **session-global** id from a
`Dictionary<MaterialKey,int>` shared across all prefabs (`ManagedBatchSystem.CreateBatch` and
`GenerateBatchMeshJob.GetMaterial` then compare these values across prefabs, which only works
because they are global). After that rewrite the entry's start/count ranges are zeroed and the
buffer position, not the stored index, is what maps to a surface.

Measured on the shipped bundle, the buffers the ripper reads are still in the authored state:
zero out-of-range fallbacks in `materials.json`, and road pieces resolve to the surfaces their
shading confirms (asphalt dark, kerbs light). So no change is needed today. The seam to check if a
future rip suddenly produces empty sub-meshes or wrong surfaces is exactly this: a buffer that has
been through `MeshSystem` has zeroed ranges and global indices, and positional resolution through
`GetSurfaceAsset(m_MaterialIndex)` stops being valid.

The same job settled a rendering question this audit had marked unrecoverable:
`GenerateBatchMeshJob.GenerateCompositionMesh` bakes each composition vertex's matrix selection
into its `Color32` — median pieces blend the middle matrices (4/5, from
`EdgeNodeGeometry.m_Middle` cut at 0.99) against the inner boundaries (2/3); side pieces reference
the corner connection curves (6/7, `BuildCurve` from the flare corners at composition width) under
flag bits (0x80) not yet decoded. The vertex-to-matrix mapping is therefore managed code, not
shader code; `docs/bundle-schema.md` §5.1 records what the producer now reproduces from it.

**And it needed a tool change to see.** `mdump`'s IL walker printed generic calls as
`ComponentType::ReadWrite<>`, dropping the one operand that carries the meaning, which made every
`GetArchetypeComponents` body unreadable — the place CS2 declares what a thing *is*. It now decodes
the `MethodSpec` signature, and `find` matches on the instantiation, which is what turned "who adds
this component?" into a single query.

---

## 4. Defects found — all fixed

Ordered by how wrong a consumer's output gets. Each is recorded with what was wrong and what
changed, because the finding is the reusable part: the next audit needs to know what was already
looked at and why the answer is what it is.

### 4.1 `attrs.elevation` does not mean what the contract says it means

**Contract §4.2:** `elevation` — "terrain height under the instance, m".
**What is written:** `Game.Objects.Elevation.m_Elevation`.
**What that is:** the object's height *above* the ground.
`ObjectToolBaseSystem.CreateDefinitionsJob.UpdateSubObjects` computes it as

```
TerrainUtils::SampleHeight(terrain, transform.m_Position)   -> t
... (water surface + PlaceableObjectData.m_PlacementOffset.y on the water path)
math::max(0, transform.m_Position.y - t)                    -> ObjectDefinition::m_Elevation
```

so it is `max(0, y − groundHeight)`, clamped at zero, and the component is simply absent for objects
sitting on the ground. A consumer told it is terrain height will read `0` for almost every object in
the city and conclude the whole map is at sea level.

Note `pos.y − elevation` *does* recover ground height for elevated objects, so the exported number is
useful — it is only the description that is wrong.

**Also dropped:** `Game.Objects.Elevation.m_Flags` (`ElevationFlags`: `Stacked 1`, `OnGround 2`,
`Lowered 4`, `OnAttachedParent 8`). `Lowered` is how the game marks something placed *below* ground,
which the clamped-at-zero magnitude cannot express.

**Fixed.** §4.2 now says what the number is, and states that ground level is `pos[1] − elevation`.
`InstanceRipSystem.AddAttributes` additionally emits `attrs.elevationFlags` as `ElevationFlags`
names. The `instance.schema.json` description was wrong in the same way and was corrected with it.

**Correction to this entry.** It first claimed the value is clamped at zero and so cannot express
"below ground". That is what `ObjectToolBaseSystem.CreateDefinitionsJob` does when it *builds a
placement definition*; it is not what the stored component holds. A real city disproves it
outright — of 517,398 records carrying the key, **29,610 are negative, down to −86.3 m**. Reading
one code path and generalising it to the data is the same class of mistake as §3.2's regex: the IL
answered the question asked of it, which was not the question that mattered. Measuring the shipped
bundle takes seconds and settles it.

### 4.2 `colors.json` could not be applied without `_ControlMask`, which was never exported

The bundle exports each render prefab's `ColorVariation` palette (three `ColorSet` channels) and, per
instance, which variation was chosen. **It did not export the texture that says which texels each
channel applies to.** Contract §7.2 said the three channels are the ones "the material's mask map
selects between" — that is not the `_MaskMap` the bundle carries. CS2 uses a separate `_ControlMask`
texture whose R, G and B are colour masks 1–3, multiplied over the base colour
([cslmodding.info](https://cslmodding.info/cs2/textures/)). `_ControlMask` is present in `Game.dll`'s
string heap; it was missed because the ripper's four map names, and the sweep in §3.2, are all `*Map`
suffixed names.

A consumer therefore had a palette and no way to apply it, and every building rendered in its base
colour — the exact failure `colors.json` was added to prevent.

**Fixed.** `_ControlMask` is now the contract's fifth map, `colorMask`, resolved through the same
`Take` path as the other four — so it takes whichever route (bound asset, rebound guid, VT stack,
surface VT data, live material) that surface's pixels actually need, and a surface without one
returns `null` silently rather than raising a warning. §7.1 gained a channel row and §7.2 now names
`colorMask` rather than "the material's mask map" as the selector.

Adding a key to the **closed** material object is not one of the changes §12 permits inside
`schema: 1`; it was amended in place rather than bumped to `schema: 2`, and the reasoning is
recorded in the contract's own §12.1 rather than here.

**And the diagnostic that would have caught it earlier.** `MaterialRipSystem` now accumulates every
property name the material library declares across the run and prints two lines in the report: how
many materials carried a colour mask, and which declared properties are *not* among the contract's
five. "Is there a map we are still not exporting?" is now answered by the run rather than by another
offline sweep — which is the actual root cause here. The map was not hidden; nobody had asked the
game.

### 4.3 Network `attrs.elevation` averages away the information

`Game.Net.Elevation.m_Elevation` is a `float2` — start and end. The producer wrote
`(x + y) * 0.5`. A segment climbing from ground level to an elevated section reported a number that
describes neither end, and a consumer deciding "is this a bridge" or cutting terrain got it wrong at
exactly the segments where it matters.

**Fixed.** `attrs.elevationStart` and `attrs.elevationEnd` carry what the game holds; `elevation`
keeps the mean so a consumer reading only it still works. Both are additive `attrs` keys and so
schema-1-compatible.

### 4.4 A decal does not say what it projects onto

Contract §4.2a told a consumer to project a decal's texture "down the volume's local −Y onto the
surface beneath it". The game is more specific than that: `MeshData.m_DecalLayer` is a
`Game.Rendering.DecalLayers` mask — `Terrain 1`, `Roads 2`, `Buildings 4`, `Vehicles 8`,
`Creatures 16`, `Other 32` — naming exactly which surfaces receive the projection. Without it a
consumer projects road grime onto the building behind it.

`InstanceRipSystem`'s per-prefab decal cache ORs `m_DecalLayer` across the prefab's decal meshes,
and a `kind: "decal"` record carries `attrs.decalLayers` as flag names.

**That was the wrong field, and the "fixed" above was never true in output.** `MeshData.m_DecalLayer`
is zero on every decal in a full city — all 17,370 of them — so `decalLayers` was absent from every
record while the code that reads it looked correct. The mask the renderer actually uses lives on the
managed `Game.Prefabs.DecalProperties` component, which `AssetImportPipeline.SetupDecalComponent`
attaches to the **`RenderPrefab`**: both `ManagedBatchSystem.CreateBatch` and
`RenderPrefabRenderer.Instance.SetDecalProperties` set `_DecalLayerMask` from
`DecalProperties.m_LayerMask`.

**Fixed properly.** The cache now also resolves each decal sub-mesh's `RenderPrefab` and ORs
`DecalProperties.m_LayerMask`, keeping the ECS field as a fallback in case a future build starts
populating it.

**The same component carries the marking's identity.** `DecalProperties.m_TextureArea` is a
`Bounds2` handed to the shader as `_TextureArea` (with `_MeshSize` from the volume bounds). Without
it a decal mesh is unidentifiable: `NA_RoadArrow_Forward`, `NA_RoadArrow_Forward_Left90` and
`NA_RoadArrow_Left180` are byte-identical boxes sharing one material, and their mesh UVs are a
default box unwrap. `MeshRipSystem.TryFlattenDecal` now bakes that rect across the volume's
footprint and ships a four-vertex quad; §4.2a is rewritten accordingly.

**Verify after a rip**, since none of this is checkable offline: `attrs.decalLayers` should be
present and `["Roads"]` for road markings, and `meshes/NA_RoadArrow_*.glb` should differ from each
other in their UVs.

---

## 5. Reachable and unread

Everything below was confirmed to exist and to be readable on the main thread from data the rip
already walks. Ordered by value per unit of work.

### 5.1 The documented `attrs` that are never produced — highest value, lowest risk

Contract §4.2 documents `name`, `category`, `zone`, `zoning`, `theme`, `brand`, `level`, `height`,
`storey`, `season`, `address`. `InstanceRipSystem.AddAttributes` writes **none** of them; it writes
`elevation`, `color`, `growth`, `treeState`, `state`. The contract permits this — a consumer must not
require any key — but PLAN §3 asks for them, and every source is validated:

| Attribute | Source | Notes |
|---|---|---|
| `name` | `NameSystem.GetName(entity, bool)` / `TryGetCustomName` | Already wrapped by Carto's `Domain/NameManager.cs`. `NetworkRipSystem.ResolveName` already handles the locale-key trap (`Assets.NAME[…]`) and is the pattern to copy. |
| `height` | `ObjectGeometryData.m_Bounds` / `m_Size` | **Per prefab, no mesh read.** PLAN §3 requires true height from mesh bounds rather than storey heuristics; this is that number, already computed by the game. Also carries `m_Pivot`, `m_LegSize`, `m_Layers`, `Objects.GeometryFlags`. |
| `level` | `SpawnableBuildingData.m_Level` (`byte`) | On the prefab. |
| `zone` | `SpawnableBuildingData.m_ZonePrefab` → `ZoneData.m_ZoneType` / `m_AreaType` / `m_ZoneFlags` | |
| `lotSize` | `BuildingData.m_LotSize` (`int2`) | Not currently in the contract; a new optional `attrs` key is schema-1-compatible. |
| `season` | `ClimateSystem.currentSeasonName` | Manifest-level, one string. |
| construction progress | `UnderConstruction.m_Progress` (`byte`), `m_Speed` | The bundle says `state: "construction"` and drops how far along. |
| building → road | `Buildings.Building.m_RoadEdge`, `m_CurvePosition` | The frontage link, and the natural join from a building to `networks.jsonl`. `m_Flags` (`Buildings.BuildingFlags`) is also unread. |

### 5.2 Phase 6 (terrain surfaces) is fully reachable — no unknowns left

The one part of the bundle described as "the meaningful upgrade over a bare heightmap" needs no
research:

| Piece | API | Notes |
|---|---|---|
| `terrain/surface_index.png` | `Game.Rendering.TerrainMaterialSystem.splatmap` | **A public `UnityEngine.Texture` property.** `SPLATMAP_SIZE = 4096`, `WORLD_SPLATMAP_SIZE = 1024`. `UserSplatmap` / `UserSplatmapWorld` hold the player-painted layer separately. |
| surface ids | `Game.Prefabs.TerrainMaterialType` (`None 0`, `Extra1 4` … `Extra4 7`) via `TerrainMaterialProperties.m_Type` on a `SurfacePrefab` | The enum names only `None` and `Extra1..4`; values 1–3 are the built-in grass/dirt/rock slots and are unnamed in metadata, so the mapping has to be confirmed against a real splatmap rather than assumed. |
| `terrain/surfaces/*.png` | `Game.Prefabs.Terrain.TerrainRenderSettingsPrefab.m_{Grass,Dirt,Rock,Extra1..4}{Diffuse,Normal}` | Each an `AssetReference<TextureAsset>` — the same `TextureAsset` type the material layer already converts to PNG. Tiling and blend ranges are on the same prefab (`m_Terrain*Tiling`, `*BlendMid/Far*`), so a consumer can reproduce the ground blend rather than guess it. |
| elevation / water | `TerrainSystem.GetHeightData(bool)` → `TerrainHeightData.heights` (`NativeArray<ushort>`, `resolution`, `scale`, `offset`); `WaterSystem.GetSurfaceData(out JobHandle)`; `TerrainUtils.SampleHeight`, `WaterUtils.SampleDepth` | Already used by Carto's `RasterSystem`. |

### 5.3 Phase 7 (manifest) — every field is a property read

`Game.UI.MapMetadataSystem` (this is `Instance.Map`) exposes `mapName`, `theme`, `latitude`,
`longitude`, `temperatureRange` (`Bounds1`), `cloudiness`, `precipitation`, `area`, `buildableLand`,
`surfaceWaterAvailability`, `groundWaterAvailability`. `CityConfigurationSystem` adds `cityName` and
`leftHandTraffic`; `ClimateSystem` adds `currentSeasonName`, `temperature`, `isRaining`, `isSnowing`.

### 5.4 Lane graph — cheap detail the drivable layer omits

`CarLane.m_CarriagewayGroup`, `m_LaneCrossCount`, `m_DefaultSpeedLimit` (versus the effective
`m_SpeedLimit` already exported), `m_AccessRestriction`, and the `blockageBounds` / `cautionBounds`
properties. `ConnectionLane.m_TrackTypes` / `m_RoadTypes` — the bundle classifies a lane as
`"connection"` without saying what may connect through it. `ParkingLane.m_ParkingFee`,
`m_FreeSpace`, and prefab-side `m_SlotSize` / `m_SlotAngle` (the geometry of a parking bay).
`Game.Net.SubLane.m_PathMethods` says which pathfinding methods may traverse a lane.

### 5.5 Colour variation detail that `colors.json` drops

Beyond `m_ColorSet` and `m_Probability`, `ColorVariation` carries `m_GroupID`, `m_SyncFlags`,
`m_ColorSourceType`, `m_ExternalChannel0..2`, and `m_HueRange` / `m_SaturationRange` /
`m_ValueRange` / `m_AlphaRange0..2`. The ranges are how CS2 randomises *within* a variation, so a
consumer re-colouring a city (as opposed to reproducing this one) cannot currently match the game's
own spread. Low priority while §4.2 is open — the palette is unusable either way.

### 5.6 Areas and districts have no layer at all

PLAN §3 lists areas/districts/zoning as a domain (polygon, name, category, density, theme, colour).
Contract v1 has no layer key for them, and nothing is exported. The data is plain ECS:
`Game.Areas.Area`, `Geometry`, `Triangle`, `Node` (the polygon), `District`, `Lot`, `Surface`,
`Space`, `Extractor`, `MapTile`, `SubArea`, plus `DistrictModifier`. Carto's existing `AreaSystem`
already walks most of it for the vector export. Adding a layer key is explicitly schema-1-compatible
(§12), so this needs no version bump.

### 5.7 Smaller items

- `MeshData.m_MinLod`, `m_ShadowLod`, `m_LodBias`, `m_TilingCount`, `m_AvailableTypes` (`MeshType`),
  `m_AvailableLayers` — LOD selection thresholds the bundle's `lodCap` cannot express.
- `SubMesh.m_RandomSeed` — how CS2 picks between otherwise-equal variants.
- `PlaceableObjectData.m_PlacementOffset`, `m_RotationSymmetry`, `Objects.PlacementFlags`.
- `Objects.Plant.m_Pollution`.
- `BuildingPropertyData` (residential unit count, allowed resources) and `BuildingExtensionData`
  (`m_Position`, `m_LotSize`, `m_HasUndergroundElements`) — semantics, not geometry.

---

## 6. What the wiki confirms, and what it does not

[The CS2 modding wiki](https://cs2.paradoxwikis.com/Modding) corroborates a small part of this
surface and is silent on the rest.

**Confirms:** `Owner` ("used to denote the owner of an entity"), `Temp` ("marks entities about to be
created/updated, but not finalized"), `Deleted`, `Destroyed` ("destroyed structures, for example a
collapsed building"), `Moving` ("the current velocity and rotation of an object"), `Edge` ("a road
segment between two nodes"), `Node` ("can be an intersection or control point"), `Lane` / `CarLane` /
`SubLane`, `Curve`, `Composition`, `EdgeGeometry`
([Common ECS Components](https://cs2.paradoxwikis.com/Common_ECS_Components),
[Systems and Components catalog](https://cs2.paradoxwikis.com/Systems_and_Components_catalog)). The
[PrefabSystem](https://cs2.paradoxwikis.com/PrefabSystem) page confirms
`TryGetPrefab(prefabEntity, out PrefabBase)` as the supported resolution route and warns that a name
may exist in two layers (`Game.Buildings.X` the ECS component versus `Game.Prefabs.X` the authoring
type) — a trap this codebase already avoids by fully qualifying both.

**Says nothing about:** `Transform`, `SubMesh`, `MeshVertex`/`MeshIndex`/`MeshMaterial`, `MeshData`,
`ColorVariation`, `ProceduralLight`, `Stack`/`StackData`, the whole `NetComposition*` family,
`SurfaceAsset`, or the virtual-texturing path — i.e. essentially everything the ripper depends on.
That is the standing justification for `tools/decomp`: for this project the wiki is a sanity check on
a handful of lifecycle components, not a reference.

**Disagrees on one thing — unresolved.** The asset-authoring side of the wiki and
[cslmodding.info](https://cslmodding.info/cs2/textures/) describe `MaskMap` as
**R** metallic · **G** glossy-coat · **B** unused · **A** glossiness. Contract §7.1 documents it as
**R** metallic · **G** ambient occlusion · **B** detail/emissive mask · **A** smoothness, which is
the stock HDRP Lit packing that CS2's shader names otherwise follow. Both agree that **A is
smoothness, not roughness**, which is the channel the contract warns about and the one most likely to
be got backwards. G and B are genuinely in dispute, and neither source settles it: CS2 ships its
shaders compiled inside asset bundles, so the packing is not recoverable from `Game.dll`. Treat §7.1's
G and B as unverified until a real texture is inspected, and do not "correct" either way on the
strength of a name.

---

## 7. Re-running this audit

Everything here is reproducible offline from `%CSII_MANAGEDPATH%` with no game launch, using
[`tools/decomp`](../tools/decomp/README.md). After a game update, re-run in this order; anything
that stops resolving is a breakage the compiler would also catch, and anything whose *value*
changes is one it would not.

```sh
cd tools/decomp && dotnet build -c Release
D=./bin/Release/net8.0/decomp.exe

# 0. Once per game version: a full source dump for ripgrep.
$D all Game "$TMP/game-src"

# 1. Every component and buffer the rip path reads still exists with the same fields, and every
#    flag value we branch on still has the same bits — decompile the type, read the fields and
#    enum literals directly.
$D type Game Game.Prefabs.NetCompositionData
$D type Game Game.Prefabs.NetPieceFlags        # ...and the other flag enums of §2

# 2. The behavioural claims — decompile the method named in each section and re-read the
#    condition in source. The load-bearing ones:
$D type Game Game.Prefabs.ObjectInitializeSystem       # §3.1 HasTransform gate
$D type Game Game.Rendering.BatchDataSystem            # §3.1 transform mask, §8 net parameters
$D type Game Game.Tools.ObjectToolBaseSystem           # §4.1 elevation formula
$D type Game Game.Rendering.BatchMeshHelpers           # §8 GenerateCompositionMesh
$D type Game Game.Rendering.BatchDataHelpers           # §8 matrices
$D type Game Game.Rendering.BatchInstanceSystem        # §8 sub-mesh selection
$D type Game Game.Prefabs.MeshSystem                   # §3.4 m_MaterialIndex rewrite

# 3. Shader property names — deliberately not `*Map`-suffixed (§4.2).
rg -o '"_[A-Za-z0-9]+"' "$TMP/game-src/Game" | sort -u

# 4. Who puts a component on an entity — the archetype question of §3.3.
rg 'ComponentType\.Read\w*<Game\.Net\.Marker>' "$TMP/game-src/Game"

# 5. Terrain and manifest surfaces, for the phases not yet built.
$D type Game Game.Rendering.TerrainMaterialSystem
$D type Game Game.Prefabs.Terrain.TerrainRenderSettingsPrefab
$D type Game Game.UI.MapMetadataSystem
```

**Why the tool changed.** The audit was first run with `tools/mdump`, an IL walker, and its two
traps (operand-less opcodes hidden by default; enum literals invisible to reference search) are
what §3.1 and §4.1 were fought through. The 1:1 network validation of §8 then showed IL reading
producing confident wrong answers where source reading did not — a misread scale factor and a
misassigned matrix — so mdump was retired for `tools/decomp`. The historical sections above keep
their original mdump command transcripts as records of how the findings were made.

## 8. Network assembly versus the game, rule by rule

A ground-up validation of `NetworkRipSystem`'s assembly against the game's own pipeline,
decompiled to C# on 2026-08-06 with `ICSharpCode.Decompiler` (the `ilspycmd` tool does not
install; a 20-line console app over the library does the job — see the recipe below). The
authorities are `Game.Rendering.BatchDataHelpers` (`CalculateEdgeParameters`,
`CalculateNodeParameters`, `CalculateOrphanParameters`, `BuildEdgeMatrix`, `BuildCurve`),
`Game.Rendering.BatchMeshHelpers+GenerateBatchMeshJob.GenerateCompositionMesh` (the per-vertex
matrix selection, baked into vertex colour), `Game.Rendering.BatchInstanceSystem.UpdateNetInstances`
(which sub-meshes an entity draws), and `Game.Prefabs.NetCompositionMeshRefSystem` (mesh dedup and
`m_Rotate`).

| # | Game rule | Producer | Verdict |
|---|---|---|---|
| 1 | Edge: matrices 0–3 = start/end Segment boundary curves; halves along the length; across = lerp left→right | Same curves, same halves, geometric bend | **match** |
| 2 | Node: matrices 0/2 and 1/3 = the two across-width halves; sync remap `m_CompositionSync0..3` | Same, incl. piecewise-linear sync | **match** |
| 3 | Median piece at a node: centre column (`\|x−offset.x\|<0.01`) rides `m_Middle`×0.99; other vertices proportional in their half's frame; `size.x==0` collapses to the offset | Same mapping; stretch fallback for pieces with no centre column and for median-less compositions (documented in schema §5.1) | **match** (fallbacks are producer additions) |
| 4 | Winding reverses on `Invert` XOR `FlipMesh`; `Invert` mirrors x, `FlipMesh` mirrors z | Same | **match** |
| 5 | Node one-off pieces draw only triangles whose vertices all sit at authored `z ≥ −0.01` | `GetFrontHalf`, same rule | **match** |
| 6 | `HalfLength` sections: front half stretched over the full run | Front half substituted after the tile count is taken from the full length | **match** |
| 7 | Tiled pieces: V = along × density (mean \|ΔV\|/\|Δz\| over non-negative-V triangles), continuous across tiles; negative V is an absolute-mapping marker | Per-tile V offset of `tile × VSpan`, same density, same negative-V exclusion | **match** (game replaces per-vertex V with the density; producer keeps authored V within a tile — equivalent for the linear strips pieces are) |
| 8 | `NetCompositionMeshRef.m_Rotate`: the edge draws as `RotatedEdge` — inverted curves, start↔end and left↔right swapped | Halves swapped and inverted the same way; count reported | **match** |
| 9 | Edge drawn only when `any(start+end length > 0.1)`; flare only when `any(half length > 0.05)` | Same gates | **match** |
| 10 | Orphan nodes draw two half-disc caps (`CalculateOrphanParameters`: quarter-circle béziers, 0.2761424 circle constant, world axes) | New collector reproduces the construction; outside connections and markers excluded as the game excludes them | **match** |
| 11 | `SkipBottomHalf` pieces keep the `z ≤ 0.01` half; on an edge that half maps onto the start Segment only | Same cut; edge placement over the first half of the run | **match** |
| 12 | `PreserveShape` pieces at a node ride the straight corner rails (matrices 6/7, `BuildCurve` from the flare corners at composition width) and stand at the rails' start at authored length (`t = (z + size.z/2)/width`) | Same rails, same parametrisation; at roundabouts the rails come from the left half's two corners, as the game builds them | **match** (any feathering between rail and flare is shader-side) |
| 13 | Roundabout compositions: `m_MiddleRadius > 0` matrices — middle split 0.99/0.01, halves against the island edge, three-matrix blends flagged 0x80 | Halves cover outer→inner as the standard mapping; the wedge fill covers inner→`m_Middle`→inner with the same 0.99 cut; rails use the left-half variant. The 0x80 blend weights live in the shader | **approximated** — surface coverage matches; the exact blend feathering does not |
| 14 | Normals/tangents: authored values carried through, rotated by the composition matrices in the shader | Rebuilt from faces after the bend | **deliberate deviation** — authored creases are approximated; crown and kerb shading can differ slightly from the game |
| 15 | Texture density, mapping offsets, `m_IndexFactor` batching terms | Not needed — the producer bakes geometry the shader would compute | **not applicable** |
| 16 | Piece x: authored + `m_Offset.x`, no width scaling in the game | Producer scales by `m_Size.x / NetPieceData.m_Width`; measured ≡ 1 on real pieces (`m_Size` comes from the mesh) | **benign** |
| 17 | `m_Offset.z` shifts a piece along its tile (used with `HalfLength`) | Ignored; `HalfLength` handled by the half-cut instead | **benign** (no other user of `m_Offset.z` observed) |

Every once-deferred rule is now reproduced or approximated regardless of whether the current city
exercises it — a producer must not shape itself to one save. The report still counts corner-rail
pieces, halved `SkipBottomHalf` pieces, roundabout parts and rotated node parts, so a rip states
how often each rule fired. Rotated *node* parts remain counted-only: the game routes them through
the edge parameters, which cannot be meant literally for a flare, and no measured city has
produced one yet.

**Tooling**: this validation is what retired the mdump IL walker — hand-reading the IL of
`CalculateNodeParameters` produced two wrong readings that decompiled source settled in minutes.
The decompiler is now the committed tool [`tools/decomp`](../tools/decomp/README.md); use it for
everything, and treat source as the only acceptable evidence for arithmetic.
