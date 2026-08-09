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
| `Game.Objects.Color.m_Index / m_Value / m_SubColor` | ~~`attrs.color`~~ **no longer read** | **This row was wrong about what the value means, and §10.3 retires it.** The scaling observation was correct but the palette is the *infoview* palette: `ObjectColorSystem` is the component's only writer and fills it from the active info mode (`InfoviewUtils.GetColor`), so the exported "paint choice" was UI-overlay state that depended on which infoview was last open. The real paint is the instance's `MeshColor` buffer — see §10.3. |
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

Measured on the shipped bundle, the outputs looked authored — zero out-of-range fallbacks in
`materials.json`, road pieces resolving to the surfaces their shading confirms — and this entry
originally concluded the buffers had not yet been rewritten. **§9.2 corrects that conclusion from
source.** The rewrite happens exactly once, at prefab creation (`MeshSystem`'s query is
`MeshData + PrefabData + (Created|Deleted)`), so every `MeshMaterial` buffer a rip can ever see
has already been through it; nothing in the game writes authored ranges into ECS at all. The
measured cleanliness has a structural explanation instead: object render prefabs never carry a
`MeshMaterial` buffer (only net pieces and the baked composition-mesh archetype do), and the
ripper's ranges come from the geometry *asset*, whose sub-mesh ordinals the game itself resolves
positionally. The ECS fallback now uses the buffer position — the only meaning the slot has —
rather than the stored global id (§9.2).

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
| 2 | Node: matrices 0/2 and 1/3 = the two across-width halves; sync remap `m_CompositionSync0..3` | Same, incl. piecewise-linear sync | **match** *(§21 row 12 later falsified the remap's coordinate handling — right-half knots were flipped and end regions translated instead of scaling; fixed there)* |
| 3 | Median piece at a node: centre column (`\|x−offset.x\|<0.01`, **`dx` computed before the zero-width pin**) rides `m_Middle`×0.99; other vertices proportional in their half's frame; `size.x==0` pins only the lerp coordinate, so a zero-width `* Intersection Piece 0` becomes a three-rail surface paving the centre wedge (§18) | Same mapping since f7cba22. The earlier port — and an earlier revision of this very row — read the pin as "all vertices are centre column" and collapsed the piece to a zero-area ribbon, opening a three-cusped hole at every junction of every road class; the "stretch fallback" it also claimed never survived into the GameMesh port | **match** (was the §18 divergence; this row itself was the misreading that cost the round-6 hunt) |
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

> **§21 annotation (2026-08-07):** the "Producer" column above describes the pre-rewrite
> assembly; the network layer has since been replaced by the wholesale port of the game's
> generator (`NetworkRipSystem.GameMesh.cs`), re-audited line-by-line in §21. Every *game rule*
> row above re-verified against the port; rows 5/7's producer descriptions are superseded by the
> port's exact mapping. The "rotated node parts" counter is gone: `NetCompositionMeshRefSystem.
> TryFindComposition` only attempts the mirrored mesh match when the Node general flag is absent,
> so a rotated node composition is impossible by construction, not merely unobserved.

**Tooling**: this validation is what retired the mdump IL walker — hand-reading the IL of
`CalculateNodeParameters` produced two wrong readings that decompiled source settled in minutes.
The decompiler is now the committed tool [`tools/decomp`](../tools/decomp/README.md); use it for
everything, and treat source as the only acceptable evidence for arithmetic.

## 9. Object meshes versus the game, rule by rule

A ground-up validation of `MeshRipSystem` (with `GeometryAssetReader` and the parts of
`GltfWriter` it feeds) against the game's own object pipeline, decompiled to C# on 2026-08-06.
Authorities: `Game.Rendering.BatchInstanceSystem.UpdateObjectInstances` (which sub-meshes an
entity draws), `Game.Rendering.BatchDataSystem.UpdateObjectData` (transforms, stacks, tree
scale, the placeholder stretch), `Game.Rendering.BatchDataHelpers` (the `Calculate*SubMeshData`
family), `Game.Prefabs.MeshSystem` (the `MeshMaterial` rewrite), `Game.Prefabs.RenderPrefab`
(`ObtainMesh`, the material-index flattening), `Game.Rendering.ManagedBatchSystem.CreateBatch`
(surface binding), and `Game.Prefabs.ObjectInitializeSystem` / `ObjectGeometryPrefab`
(archetypes, sub-mesh flags at authoring).

| # | Game rule | Producer | Verdict |
|---|---|---|---|
| 1 | A sub-mesh draws when `(m_Flags & state) == m_Flags` — a full-subset test over *all* flags. Baseline state is `DefaultMissingMesh \| HasTransform \|` traffic side; per-instance components add tree/stack/net-object/quantity bits | `ExportsSubMesh`: `Require*` bits ⊆ documented state, `OutlineOnly` excluded, `IsStack*`/`HasTransform`/`DefaultMissingMesh` ignored | **match** for the documented one-state export. The producer's fixed `RequireSafe\|RequireAdult\|RequireEmpty\|RequireClear\|RequireForward` is exactly what the game's per-instance calculators produce for the common case (no `Quantity` fullness, `NetObject` clear+forward, adult tree). `ObjectState` has no `Safe`/`LevelCrossing` cases, so those two bits cannot occur on object sub-meshes — harmless in the state |
| 2 | The meshes of one geometry asset are simultaneous parts; the game creates one batch per flattened (mesh, sub-mesh) pair and binds `GetSurfaceAsset(flatOrdinal)` (`RenderPrefab.ObtainMesh` defines the flattening; LODs are separate render prefabs behind `LodProperties`) | Was: richest mesh wins, sub-mesh ordinal local to it | **divergence — fixed.** `GeometryAssetReader.ReadAll` now merges every mesh of the asset with flat material ordinals; the Unity-mesh fallback merges the same way. Report counts multi-mesh prefabs. The earlier "every object mesh measures one primitive, nothing visible changes" reading of the shipped bundle was circular — the bundle was ripped by the one-mesh code. Offline parsing of the game's own `.cok` archives shows vanilla trees are the mass-market case: every tree stage is `meshCount = 2` (trunk + leaf cards), which is why every tree in the pre-fix bundle exported dead. §15 has the file-level evidence |
| 3 | `MeshMaterial` buffers: only writer is `MeshSystem.OnUpdate` at prefab `Created` — resizes to one entry per surface *in order*, stores a session-global library id, leaves ranges uninitialised. The game resolves surfaces from this buffer by *position* (`CreateBatch` matches the global id, then `GetSurfaceAsset(position)`). Only net-piece prefabs and the baked composition archetype carry the buffer at all | Was: `m_MaterialIndex` used positionally (§3.4's "benign" reading) | **latent divergence — fixed.** The ECS fallback now uses the buffer position; garbage ranges are dropped by the existing range checks. Unreachable today (objects have no such buffer; pieces read through the geometry asset), but the fallback no longer depends on that |
| 4 | `SubMeshGroup`/`MeshGroup` — per-instance random variant selection (`MeshGroupSystem`, `PseudoRandomSeed.kMeshGroup`) | Not handled | **benign.** `SubMeshGroup` entries are created only on the `CharacterGroup` path (`ObjectInitializeSystem.InitializePrefab`) and the buffer only exists when a mesh is a `CharacterGroup` — creatures, which the bundle excludes |
| 5 | `DefaultMissingMesh` placeholder (obsolete content, `InitializeObsoleteSystem`) draws stretched over `ObjectGeometryData.m_Bounds` (translate to centre, scale to half-size) | Was: exported at authored size | **divergence — fixed.** The part now carries the same TRS (node `scale` added to the writer); counted in the report. Zero in a healthy city |
| 6 | Stack sections: counts/offsets/scales from `CalculateStackSubMeshData`, applied along `StackData.m_Direction`; every instance of a stackable prefab has `Stack` (archetype); parts with no stack role draw once, untiled | Sections expanded with the same public helper; whole-prefab file also shipped. Was: non-stack parts of a stacked prefab unreachable (whole file never referenced, sections files tile everything) | **match** on the arithmetic; the untiled-rest rule is now implemented as `__stack_rest` (file + one record per stacked instance), counted. All six stacked prefabs in the measured city are fully stack-flagged, so the rule is insurance |
| 7 | Trees: state selects the sub-mesh (`CalculateTreeSubMeshData`), growth √-lerps a uniform scale between the prefab's per-stage sizes, applied to the instance transform | One adult-state mesh, `attrs.growth`/`treeState` | **approximated by design** on the mesh (documented one-state export); the scale — which a consumer could not reproduce without `GrowthScaleData` — is now exported per instance as `attrs.growthScale`, via the game's own helper (§10.2) |
| 8 | `MeshFlags.Base`: a prefab with `BaseProperties` — and every building mesh, granted the flag by `ObjectInitializeSystem` even without one — draws one extra generated batch past its last sub-mesh: the foundation skirt. `BatchMeshHelpers.GenerateObjectMesh` builds it from the mesh's own silhouette at the base level (±0.01 m band, upward-continuing edges minus interior seams), as a 1 m quad band whose v=0 ring the shader drops to terrain; material = surface 0 of `BaseProperties.m_BaseType` or `MeshSettings.m_DefaultBaseMesh`; `MeshFlags.MinBounds` moves the base level to `m_Bounds.min.y`; `colossal_BaseState`=1 (instance not plain `OnGround`, owner's elevation for non-external building extensions) disables the stretch | Was: not exported | **reproduced (2026-08-06).** `IO/BaseSkirtBuilder` ports the silhouette scan (linear scan in place of the BVH; chain-accumulated perimeter u, loop seams split); `MeshRipSystem` gathers Base parts and the base surface on the main thread and generates on the writer, emitting `<assetId>__base.glb`. Terrain drop + elevation gate are the documented consumer rule (contract §4.1) since the bundle ships the terrain raster. Counted: skirts written, Base parts with no silhouette |
| 9 | `UnderConstruction` with `m_NewPrefab == Entity.Null` draws nothing (the finished mesh is hidden until built); `Destroyed` + physical-lot with `m_Cleared ≥ 0` draws nothing | Instance exported with `state: "construction"` / `"destroyed"` referencing the finished mesh | **deliberate deviation** — the bundle describes the built environment; the consumer decides what a construction site or rubble looks like. Documented in contract §4.2 |
| 10 | `Elevation.m_Elevation < 0` moves rendering to the Tunnel layer; `OutlineOnly` sub-meshes draw only in the outline pass | Excluded / not applicable | **match** (§2.1, tools/decomp README) |

## 10. Instances versus the game

`InstanceRipSystem` against culling, ownership, colour and growth. The transform, parent,
exclusion and elevation rows of §2.1 stand as validated; what follows is what this pass changed
or newly established.

### 10.1 The query and the exclusions

`PreCullingData` and the culling flags turn out to be irrelevant to the producer: culling
selects what is *drawn this frame*, and the bundle wants what is *placed*. The query's actual
divergences from the game's draw set are all deliberate and documented: `Moving`, `Creature`,
`Vehicle`, `Marker` (§2.1), the buried utility-node filter (§3.3), and Temp/Deleted lifecycle
state. No change.

### 10.2 Tree growth is also a scale

`BatchDataSystem.UpdateObjectData` multiplies the instance transform by the scale from
`CalculateTreeSubMeshData` — a √-lerp between `GrowthScaleData.m_ChildSize…m_DeadSize` across
the growth byte. `attrs.growth` alone cannot reproduce it because the per-stage sizes are
prefab data the bundle does not carry. The producer now calls the same public helper per tree
and writes `attrs.growthScale` (uniform, one number, omitted at 1). Without it every sapling
and half-grown tree rendered at adult size.

### 10.3 `attrs.color` was infoview state, not paint

The finding of this pass. `Game.Objects.Color` — exported since Phase 3 as "which colour
variation this object was painted" — has exactly one writer in the game: `ObjectColorSystem`,
which fills it from the **active infoview** (`activeData.m_Index` is the info-mode index,
`m_Value` a 0–255 status ramp through `InfoviewUtils.GetColor`). The exported value was UI
overlay state that depended on which info panel the player last had open: 357,176 records of it
in the shipped bundle.

The paint the renderer actually uses is computed by `MeshColorSystem` — `PseudoRandomSeed` ×
palette probabilities × season/age/gender filters × sync groups — and stored per instance in
the `MeshColor` buffer (`m_ColorSet`, three RGBA channels), recomputed for every entity on game
load, so it is always populated when a rip can run. `CustomMeshColor` overrides it when the
player painted the object (`SetCustomColor` applies entry 0 to every part).

**Fixed:** `attrs.colorSet` now carries the resolved `ColorSet` (first part's; instances whose
parts resolve different sets are counted in the report), `attrs.colorCustom` marks player
paint, and `attrs.color` is gone. Contract §7.2 corrected in place — the documented semantics
were never delivered, so this is a bug fix, not a meaning change. `colors.json` keeps its role
as the prefab's *possible* variations.

## 11. Materials and textures versus `SurfaceAsset`

`MaterialRipSystem` against `SurfaceAsset.SyncToUnityMaterial`, `GetTemplateMaterial` and the
VT routes, re-checked from source rather than trusted from the session that wrote them.

| # | Game rule | Producer | Verdict |
|---|---|---|---|
| 1 | A decode binds every entry of `m_Textures` whose property the template shader has (VT-handled entries bind through param blocks instead), plus all floats/ints/vectors/colors, plus every keyword in `m_Keywords` | The five contract maps via `TryGetTexture`, `_BaseColor`/`_EmissiveColor`/`_AlphaCutoff` scalars | **match** for the contract's surface. The run census now also accumulates the *surfaces' own* texture-dictionary keys, not just the material library's VT stack names — a non-VT-only map can no longer ship unnoticed (the §4.2 failure shape) |
| 2 | Alpha: `_SURFACE_TYPE_TRANSPARENT` and `_ALPHATEST_ON` are the keywords the game itself branches on (`ManagedBatchSystem`, `RenderPrefabRenderer`) — but the runtime material is `new Material(template)` + surface overlay, so a template's clipping reaches surfaces that serialise no keywords of their own (vegetation, §15.3) | Was: the surface's two keywords only | **divergence — fixed (superseding this row's earlier "match", which held only for surfaces that carry their own keywords).** Surface keywords and floats first, then the template's keywords/floats, surface floats vetoing — §15.3 |
| 3 | Double-sidedness is HDRP's `_DoubleSidedEnable` float on the template material; surface floats override template floats in `SyncToUnityMaterial` | Was: template-*name* substring `doublesided` | **approximated — improved.** Now reads the surface float, then the template material's property, with the name check as last fallback |
| 4 | Decals: import gates on `lod.surfaces[0].template == "DefaultDecal"` (`AssetImportPipeline.SetupDecalComponent`); the projection composites by base-colour alpha | Template-name flag `decal`; `alphaMode` forced to `blend` for opaque-keyword decals | **match** — `"DefaultDecal"` contains the substring the flag tests, and the blend override matches what the projection does. `m_TextureArea` is computed from the *import-time* mesh UVs of up-facing vertices, which differ from runtime `MeshUV0` by the V-flip the producer already verified empirically against the arrow sheet (three distinct glyphs); the source reading and the measurement agree |
| 5 | VT: `GetPreProcessedTextureGuid` still indexes `stack * layer` in this build (§2.3); the two VT schemes are complementary | Own flat-index walk; four-route `Locate` | **match**, unchanged |

## 12. Lanes versus the game

`LaneRipSystem` publishes the drivable graph; `BatchInstanceSystem.UpdateLaneInstances` draws
lane *geometry*. Two findings:

1. **The lane sub-mesh test is the object test inverted.** For lanes the accumulated flags are
   an *exclusion* mask — `(subMesh.m_Flags & state) != 0 → skip` — and the bits read
   accordingly: outside the editor the state contains `RequireEditor` (so editor-only lane
   meshes are excluded), and the traffic side is reversed — left-hand traffic excludes
   `RequireRightHandTraffic` sub-meshes. Any future lane-geometry exporter must use this
   semantics, not §9's subset test; recorded here so it is not rediscovered the hard way.
   The graph layer itself reads none of these flags — no change to `lanes.jsonl`.

2. **Lane-drawn geometry is now baked (2026-08-06)** into the additive `lanemeshes/` layer
   (contract §5B), as a stage of the network rip — same material window, same ordered
   texture-then-mesh queue. The selection reproduces `UpdateLaneInstances`: the inverted
   sub-mesh mask above plus the per-lane bits (`Unsafe`→`RequireSafe`,
   `!LevelCrossing`→`RequireLevelCrossing`, track `Switch|DiamondCrossing`→`RequireClear` else
   `RequireTrack`), `CutRange`s cutting holes, and the 0.1 m draw gate. The bend reproduces
   what `BatchDataSystem.UpdateLaneData` hands the shader: spans cut by curve delta, tiles per
   `GetTileCount` (rounded clips for authored `m_TilingCount`, partial last tile covering only
   its clip share), mesh Z mapped over each tile's arc-length share, `NodeLane.m_WidthOffset`
   width scaling, `MeshFlags.Invert` inverting the curve, and the hanging drop as the cubic
   `(d.x, (d.x+h·L)·⅔, (d.y+h·L)·⅔, d.y)` from `HangingLane.m_Distances` +
   `UtilityLaneData.m_Hanging`. Deliberate exclusions, counted per rip: tunnel-owned lanes and
   `Pipeline`/`SubPipeline`-layer parts (`SearchSystem.GetLayers` — the underground cutaway
   rendering), and decal-flagged lane parts. Approximations, also counted or noted: cut/end
   clipping is per-vertex-clamp + drop-fully-clamped-triangles where the game clips per-pixel;
   arc inversion uses a 32-sample table where the shader interpolates the `BuildCurveMatrix`
   weights; the hang bake omits the game's wind modulation (calm-day shape);
   `BuildCurveParams`' shared-end taper factors (`m_SharedStartCount`/`m_ConnectedStartCount`)
   are not reproduced.

The graph fields themselves (`m_SpeedLimit` in m/s, `m_Curviness`, flags-as-names,
master/`SecondaryLane`) re-validated against `Game.Net.CarLane`/`TrackLane`/etc. unchanged —
§2.4 stands.

## 13. Appearance versus the game

`AppearanceRipSystem` (colors.json / lights.json) against `MeshColorSystem` and the
`ProceduralLight` construction in `MeshSystem.OnUpdate`:

- **Palettes**: `ColorVariation` read per sub-mesh — confirmed as the very buffer
  `MeshColorSystem` selects from, and the sub-mesh (not the object prefab) is confirmed as the
  carrier (§7.2's census). `m_Probability` is the additive weight in the game's reservoir pick —
  correct as published. The detail the file drops — `m_GroupID`, `m_SyncFlags`, seasonal
  `ColorFilter`s, HSV ranges — remains §5.5's open item; with `attrs.colorSet` now carrying the
  resolved answer (§10.3), that detail only matters to a consumer re-randomising a city.
- **Lights**: `ProceduralLight` values are written by `MeshSystem.OnUpdate` from
  `EmissiveProperties` — colours converted to linear, intensity ×100 into `w`, response speed as
  `1/max(0.001, responseTime)`, animation indices offset past the signal-group block. The
  producer reads the post-construction component, so all of that is already applied — the
  published values are exactly what the renderer consumes. `LightEffectData.m_Range` /
  `m_ColorTemperature` confirmed as the effect-level pairing. **match**
- The palette/light files are keyed by sub-mesh asset id = glTF node name — which survives §9's
  multi-mesh merge, because the merge is within one render prefab (one node) — still sound.

## 14. The writers versus their specs

No game counterpart; validated against glTF 2.0, the PNG spec, and the bundle contract.

- **GltfWriter**: container framing (12-byte header, padded JSON/BIN chunks), accessor
  min/max after the X-mirror, winding reversal, tangent `w` flip, and the V flip all conform.
  Two changes: node `scale` is now emitted for parts that carry one (§9.5), and materials are
  pooled per file rather than per part, so a prefab whose parts share a surface declares that
  material once — same join key, smaller files, and one PBR node tree per material on import.
- **Png**: colour type 6, bit depth 8, filter `None`, zlib with Adler-32, CRC over type+data —
  spec-conformant and deterministic, which the content-hash naming requires. Per-row adaptive
  filtering would shrink output at the cost of the encoder's obvious correctness; declined.
- **TextureWriter**: two-stage dedup (claim by asset identity, then content hash) unchanged.
- **Performance**: the write path is dominated by PNG deflate on the background writer, not by
  the per-float `BinaryWriter` loops (~seconds per full city); per the prefer-simple rule, no
  micro-optimisation without a measured target. The rip's main-thread cost is unchanged by this
  pass except `attrs.colorSet`/`growthScale` (two buffer/component reads per instance, within
  the existing per-entity budget).

### 14.1 What this pass changed, in one list

Producer behaviour (all schema-1-compatible): multi-mesh geometry assets merged with the game's
surface mapping; ECS material fallback made positional; missing-asset placeholders stretched as
the game stretches them; `__stack_rest` for untiled parts of stacked prefabs; `attrs.color`
(infoview state) replaced by `attrs.colorSet`/`colorCustom` (resolved paint); `attrs.growthScale`
added; double-sidedness read from the material property before the template name; sanitised ids
capped at 160 so derived ids stay inside the contract's 180; glTF materials pooled per file;
node scale support. Counters added for every rule a vanilla city may not exercise: multi-mesh
merges, placeholder stretches, stack-rest parts, divergent per-part colour sets, `mesh:base`
census row, and lane-drawn geometry. The two gaps this pass initially left counted-but-unbuilt
were closed the same day: building foundation skirts (§9.8, `<assetId>__base.glb` + contract
§4.1) and lane-drawn geometry (§12.2, the `lanemeshes/` layer + contract §5B).

## 15. Tree foliage, validated from the game's own files (2026-08-06)

Prompted by the Blender harness showing every tree as a bare trunk. Validated offline, without
launching the game: the vanilla asset database (`Cities2_Data/Content/Game/Blob*.cok`) is a
stored ZIP whose `.Geometry` and `.Surface` entries parse with the layouts decompiled from
`Colossal.IO.AssetDatabase.GeometryAsset` (`MeshInfoHeader` at file offset 10, `SubMeshInfo`
table after) and `SurfaceAsset` (`ReadHeader`/`LoadProperties`).

| # | Game fact (from the files + decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | `OakTreeAdult01…Geometry` holds **two meshes**: mesh 0 = trunk (4,293 verts / 18,525 indices), mesh 1 = leaf cards (6,400 verts / 15,360 indices — 1,600 quads). Same layout for every growth stage and every vanilla tree checked. LODs are separate assets (`_LOD1…Geometry`, 10 verts) | The shipped bundle's `OakTree01.glb` is byte-for-byte mesh 0 — every triangle UV-maps to the bark strip of the atlas | **root cause confirmed.** The bundle predates `ReadAll` (§9.2); the one-mesh reader kept mesh 0 and dropped the canopy. The current tree exports both with flat ordinals — re-rip required |
| 2 | The adult oak render prefab carries **two surfaces** — `OakTreeAdult01` (template id 3) and `OakTreeAdult01#1` (template id 4) — and `MeshSystem` warns whenever `meshCount != materialCount`; a vanilla load logs no such warning, so serialized `m_MeshCount` agrees with the asset header on vanilla content. DLC `.Prefab` entries (`Prefabs_FreeUpdate02.cok`) confirm multi-mesh counts serialize correctly (ParkCafe01 = 5) | `TryReadGeometryAsset` trusted `renderPrefab.meshCount` alone | **hardened.** After `ObtainMeshes` the asset's own header count (`asset.meshCount`, what `CreateMeshes` iterates) is resident and now preferred; the prefab's declared count is the fallback |
| 3 | The two oak surfaces are byte-identical property bags — same four texture GUIDs (one shared atlas: leaves left ~72 %, bark right), same wind floats (`_WindRangeLvlB`, `_WindElasticityLvlB`), same keywords (`_ANIM_HIERARCHY_PIVOT`, `_TANGENTSPACE_OCTO`) — **differing only in template id**. Neither carries `_ALPHATEST_ON`, `_AlphaCutoffEnable`, or `_AlphaCutoff`: the leaf cutout lives entirely on the library template, and `SurfaceAsset.Load` builds `new Material(template)` before overlaying surface properties, with `HDMaterial.ValidateMaterial` re-deriving keywords from floats | `AlphaMode` read only the surface's keywords → leaf material would export `opaque` and Blender would draw the cards as solid rectangles | **divergence — fixed.** `AlphaMode` now falls back to the template material (keywords, then HDRP's serialized `_SurfaceType`/`_AlphaCutoffEnable` floats), with a surface-carried float vetoing the template in either direction, matching `ValidateMaterial`'s float-wins semantics. `_AlphaCutoff` falls back to the template the same way. Double-sidedness already resolved through the template (§9) |
| 4 | Leaf base colour is authored desaturated; the game tints it per instance through `_ControlMask` and the colour-set system | `attrs.colorSet` + `materials.json.colorMask` already shipped (§10, §13) | **match** — the data a consumer needs to tint foliage is in the bundle; the application formula is now stated in contract §7.2, and the Blender harness implements it (`apply_color_tints`, per-instance via `INSTANCER` attributes) |
| 5 | Leaf meshes also carry `Color` (UNorm8×4) and `TexCoord3` (float32×3) — wind animation inputs consumed under `_ANIM_HIERARCHY_PIVOT` (per-leaf pivot hierarchy), plus `_WindRange/_WindElasticity` floats | Not exported | **deliberate deviation.** Wind is engine animation, not shape; the static rest pose is complete without it. Revisit only if a consumer wants to re-create wind |
| 6 | A geometry asset's meshes draw together (§9.2), so a partial merge is lost geometry, not a variant choice | Per-mesh read failures were swallowed at Verbose with no counter | **hack removed.** `PartialMeshPrefabs` counter + report line ("Prefabs that lost meshes their geometry asset holds"); per-prefab `Warn` naming the asset in both the native and Unity-mesh paths; the reader's catch upgraded to `Warn` with the exception type; and a `"partial: …"` declaration in `warnings.jsonl` (contract §10), since an incomplete `.glb` is invisible from the file alone |

Acceptance for the re-rip: `OakTree01.glb` gains a second primitive (6,400 verts) whose
material is `OakTreeAdult01#1…` with `alphaMode: "MASK"` and an `alphaCutoff`
(`doubleSided` follows the template's `_DoubleSidedEnable`, expected true for foliage but only
observable at runtime); the report's multi-mesh counter goes from absent (0) to roughly the
vegetation prefab count; the partial-merge counter stays 0; the Blender harness shows canopies.

---

## 16. The junction interior, settled from source (2026-08-06)

The first full-city rip after §9–§15 still showed three junction defects in Blender: empty
interiors (screenshot-confirmed holes, the world background visible through them), the fill
rendering as large flat facets in a different grey, and z-fighting where the fill overlapped
the flares. All three trace to one divergence, settled by re-reading
`GenerateCompositionMesh`'s node branch against the producer:

| # | Game fact (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | `flag12` (the median branch) tests **only** `NetSectionFlags.Median` — every median piece of a node composition (surface, kerb, bottom) rides the middle matrices, at **every** node, with **no gap threshold and no width cap**. `m_Middle` is cut at 0.99 and ends at the junction centre, so the arm's median-slot pieces are what pave the interior, with authored UVs | The median bend fired only when the halves had measurably parted (`gap ≥ 0.01 m`), only for `Surface` pieces, and was capped at the composition width — 201 wedges of a full city skipped as "too wide to be a median" and left as holes | **divergence — fixed.** The bend is unconditional at nodes, takes every median-flagged piece, and the cap is gone. The threshold and cap were producer inventions |
| 2 | Nothing else covers a junction: `UpdateNetInstances` renders per-edge node meshes (plus orphans) and no other entity draws there. There is no fan | A constant-UV fan spanned flare-to-flare per junction — the flat differently-shaded facets, z-fighting the flares it overlapped; at the measured small-road junctions it did not even fire, leaving the interior empty | **hack reduced.** With the median bend always on, the fan shrinks to a residual centre patch bounded by the arms' inner-boundary and middle-curve ends (outer corners dropped from the ring), counted as APPROXIMATED |
| 3 | Bent node pieces keep their authored UVs; the shader retiles V procedurally (per-primitive `du/dz` in the vertex stream) | The wedge fill tiles geometrically with authored UVs — equivalent under the documented normals deviation | **match** |

Also found on the same screenshots and fixed the same day: the decal quad rebuild in
`TryFlattenDecal` paired positions with UVs by assumption (`+x → +u`, pre-flipped V) instead of
keeping the authored pairing the game derives `m_TextureArea` *from* — every road-marking text
in the city read mirrored ("TAXI" as "IXAT"), and some crops landed on the wrong atlas band
outright. The quad now samples each corner's nearest up-facing authored vertex UV, so
orientation and crop are the authored ones by construction. And `lanemeshes` v1's per-lane
baking (33,500 files / 8.6 GiB from 112 unique meshes) was replaced by contract §5B v2:
straight part meshes once + per-lane bend parameters, the bend reference-implemented in the
Blender harness — which is also what the game itself stores and does.

> **Superseded in part (2026-08-07, §18):** row 2's "residual centre patch" was later deleted
> outright — the interior needs no patch: the arms' node meshes tile it completely once the
> zero-width intersection piece is mapped as the three-rail wedge paver. The intermediate
> rounds 4–5 theory ("the interior is flattened terrain painted by decals") is also dead —
> §18's pixel measurement falsified it. The flattening itself is real (§17) but is not the
> visible surface.

Acceptance for the re-rip: junction interiors close (the coverage raster that showed empty
small-road junctions shows them paved), no flat differently-shaded facets and no fill
z-fighting; "too wide" no longer appears in the report; road-marking text reads correctly;
`lanemeshes/` drops from 33,500 files to ~112 plus `laneprefabs.json` and a ~100 MB
`lanemeshes.jsonl`.

## 17. Terrain, water and the roadbed, settled from source (2026-08-06, Phase 6)

§16's rounds 4–5 believed junction interiors were flattened ground; §18 later falsified the
appearance half of that (the visible interior is the arms' own node meshes). What survives —
and what this section records — is that the ground under roads and junctions genuinely IS
flattened, which the terrain layer ships. This
section records what the Phase 6 build proved about *how* that ground works, from the decompiled
`Game.Simulation.TerrainSystem`, `Game.Rendering.TerrainMaterialSystem`/`TerrainRenderSystem`
and `Colossal.IO.AssetDatabase.GeometryAsset` (dump kept at `E:\carto-out\game-src`).

| # | Game fact (decompiled source) | Consequence for the ripper |
|---|---|---|
| 1 | **`TerrainSystem.GetHeightData()` returns the road-flattened final terrain, not base heights.** The CPU array is an async readback of `m_HeightmapCascade` slice `baseLod` (`UpdateGPUReadback` → `m_AsyncGPUReadback.Request(m_HeightmapCascade, …, baseLod, 1)`), and `RenderCascades` runs `RenderCascade` for every slice from 3 **down to `baseLod` inclusive** — each blits the base `heightmap` then draws the building-lot, lane and area height adjustments over it (`DrawHeightAdjustments`). Carto's existing elevation source therefore already contains the flattening; `GetHeightData(waitForPending: true)` is all the rip needs | `TerrainRipSystem` ships those samples verbatim as `terrain/heightmap.png` (16-bit, lossless passthrough) |
| 2 | **The terrain material has no roadbed.** Its whole ground roster is rock/dirt/grass diffuse+normal plus four paintable extras (`TerrainRenderSettingsPrefab`); the splatmap is computed on the GPU from the *modified* height cascade, water, noise and the user brush (`TerrainMaterialSystem.UpdateSplatmap` — `_SplatHeightVariance`, `_SplatRockLimit`, `_SplatGrassLimit`), and no lane or net data ever enters it. The in-game "roadbed" look is the splat classifier's response to machine-graded ground plus the ~13 %-alpha drive-lane decals (§16) | There is nothing to read back for a roadbed mask; the honest mask is the flattening footprint itself |
| 3 | **The flattening footprint is `CullRoadsJob`'s walk**: every net whose *prefab* `NetGeometryData.m_Flags` carries `FlattenTerrain` (→ `LaneFlags.ShiftTerrain`) or `ClipTerrain` contributes its edge-geometry halves, per-node strip sets (roundabout ring / four-strip junction incl. the two middle-curve wedges — the petals / two-strip straight-through, gated on node length > 0.05 m) and orphan quarter-circle caps (κ = 0.2761424). `TerrainComposition.m_WidthOffset` narrows each side per control point, suppressed on a strip's shared-middle side (`MiddleLeft`/`MiddleRight`); `NetUtils.GetTerrainSmoothingWidth` adds a slope apron *outside* the flattened bed. Elevated compositions get a downward-only clamp and tunnels an upward-only one — their ground is shifted away from the road — except sides with `Low/HighTransition`, which shed that state (portals are ground); `HasSurface`-less nets (power lines) clamp for clearance only | `TerrainRipSystem.StampRoadbed` rasterises exactly this walk into `terrain/surface_index.png` (0 natural, 1 roadbed); apron not stamped; elevated/tunnel excluded, transitions included. Deliberate simplification: the roundabout ring's elevation-transition `MoveTowards` trims are omitted (over-stamps a few metres at elevated ramps, never under-stamps) |
| 4 | **`GeometryAsset.ObtainMeshes` loads CPU data only when `m_Instances == null`.** For a prefab the game is currently rendering it returns the live `Mesh[]` without touching `m_Data`, every native accessor then throws `ArgumentOutOfRangeException` off `data.meshCount == 0`, and the `keepReadable` parameter is dead in this build (the decompiled body never reads it). The 16 per-rip "Mesh 0 unreadable" warnings (`Highway_Lane_4_Mesh`, …) were exactly this; all 16 `.glb`s were complete via the Unity-mesh fallback, whose meshes `CreateMeshes` builds readable | `TryReadGeometryAsset` now gates the native path on `asset.data.IsValid` and logs the fallback at Debug — the warning was a path choice misreported as a failure |
| 5 | **Object sub-meshes with `MeshData.m_DefaultLayers` ⊆ `Pipeline\|SubPipeline` never draw in the world view** — `BatchInstanceSystem.UpdateObjectInstances` replaces their batch layer with the default layers, which map to the underground-cutaway camera layers. This is what the 29,648 still-imported pipe/cable node props were: *building-owned* utility furniture (owner = the building, so the §"marker owner" test never fired) whose every sub-mesh is Pipeline-layer | `IsUtilityMarkerSubObject` gained the every-part prefab-layer test; both rip queries drop them, matching the Pipeline-layer lane parts already excluded from §5B |

The §8 contract freeze that came out of this (16-bit PNG rasters + `rasters` scale keys, PNG
instead of the placeholder GeoTIFF wording) is recorded in `docs/bundle-schema.md` §12.1.

## 18. The junction interior, reopened by measurement (2026-08-06, late)

§16 round 5 concluded the *visible* junction asphalt was the flattened terrain (dirt splat)
under the ~13 %-alpha drive-lane decals. **A pixel measurement of an in-game screenshot
falsifies the appearance half of that claim.** Method: sample the junction interior and the
adjacent carriageway in the same shot, compare per-channel ratios; materials differ → ratios
differ regardless of lighting. Data (`E:\carto-out\shots\ingame.png`):

- junction interior (clean patch): (121, 109, 74) — R/B 1.635
- carriageway between markings:   (121, 109, 75) — R/B 1.613, and (114, 102, 69)
- road surface texture mean (bundle): (83, 82, 79) — neutral, R−B = 4
- theme dirt texture mean (bundle):   (96, 87, 79) — warm, R−B = 17

If the interior were dirt terrain its R/B under that light would be ≈ 1.87; measured 1.635 ≡
the road's. **The interior is drawn with the road's own albedo — it is geometry (or a decal
carrying the road texture), not terrain.** The drive-lane decal cannot be it: measured
(18,18,18) at α ≤ 113 × 0.5 tint — a ~12 % wear tint, confirmed as the dark curved streaks in
the same screenshot.

What still stands from §16/§17: no *fan* exists; `UpdateNetInstances` draws only per-edge node
meshes + orphans; the ground under junctions genuinely IS flattened (the ripped heightmap shows
it) and the Phase 6 terrain layer is correct and needed under everything. What falls: terrain
as the interior's visible surface. Since the interior is road-material geometry and the only
draws there are the per-edge node meshes, **the game's node meshes must tile the whole interior
— and the Blender petals are coverage our `GenerateCompositionMesh` port under-builds**, not
missing terrain. Rotated node parts are not the gap (assembled + counted; 0 fired this rip).
Round 6's task: diff the port's node coverage against the game's, piece class by piece class,
until the petal area is accounted for.

### §18 resolution (2026-08-07): the wedge-paver was misread, one line was wrong

The round-6 piece-table dump answered it. **Every** parted node composition carries a
zero-width `* Intersection Piece 0` median section (`Highway/Median/Alley Intersection
Piece 0`, `m_Size.x = 0`) whose **authored mesh has real width** (x ∈ −2..2 or −1..1, 53
verts). The decompile's subtlety: `GenerateCompositionMesh` computes `dx = vertex.x −
offset.x` **before** `vertex.x = select(vertex.x, offset.x, size.x == 0)` pins the lerp
coordinate — so the piece's side columns still route into the half frames by their authored
sign, where the pinned x lands them exactly ON the inner boundary curves, while the true
centre column rides `m_Middle` to the junction centre. A zero-width intersection piece is
therefore a **three-rail ruled surface** — inner-left curve, middle-to-centre, inner-right
curve — and it is what paves the centre wedge of every junction arm, medians or not.

The port (and audit §8's "zero-width median collapse" row, now corrected by this note) read
the `select` as "every vertex is centre column" and collapsed the piece to a zero-area ribbon
on `m_Middle` — which is precisely the astroid/petal hole at every junction of every road
class, avenues' visible medians having masked it there. Fix: drop `zeroWidth` from the
centre-column condition (`if (math.abs(dx) < 0.01f)`); the pinned `vertex.x` already sends
the side columns to the boundary rails. One line.

Method note for the next mystery of this kind: the sequence that cracked it was (1) pixel
ratios on an in-game screenshot to falsify the wrong appearance theory, (2) the info-panel
outline to see true per-arm coverage (sectors to the centre), (3) a top-down rasterisation of
the ripped glbs to shape the hole exactly (three-cusped darts), and (4) an 8-composition
piece-table dump from a live rip to name the paver. Screenshots were evidence, not
decoration — the user's overlap outlines carried the decisive shape.

## 19. Lane marking decals: the atlas rect lives on the prefab, not in the mesh (2026-08-07)

The screenshot comparison showed no crosswalks, centre lines or lane lines in Blender, while
all of them are visibly in the bundle: 81,000 of 114,249 `lanemeshes.jsonl` records reference
a `decal` catalogue part (2,952 crosswalk records alone), the part `.glb`s exist, their
materials resolve with `alphaMode: "blend"` and white base factor, and the harness bends and
lifts them. The defect was in the UVs the quads shipped with.

**Measurement (offline, shipped bundle only):** `Crosswalk_Line_2_Mesh`,
`Yellow_Solid_Line_Mesh` and `White_Solid_Line_Mesh` — three different markings — carry
**byte-identical corner UVs** `[0.625, 0.25]–[0.875, 0.5]`. Sampling the LaneLine atlas over
that rect: mean α 27/255, i.e. a mostly-transparent generic slab with a few thin stripes (the
first opaque pixel is (181,144,55) — yellow) at the wrong positions. Three distinct markings
cannot share one crop; bent with those UVs they render as ~90 % nothing.

**Mechanism (decompiled):** a decal's mesh UVs are never sampled by the shader. The game hands
it `_TextureArea` from the managed `DecalProperties.m_TextureArea` on the mesh's RenderPrefab
(`ManagedBatchSystem.CreateBatch`, both the decalProperties and the `component6` paths), and
the projector footprint is mapped into that rect. For DefaultDecal object imports,
`AssetImportPipeline.SetupDecalComponent` *derives* the area from the authored up-facing UVs —
which is why the flattened object decals (road text, arrows) rendered correctly from authored
UVs alone. The lane-line meshes are not that case: their authored box UVs are generic, and
only their prefabs' `m_TextureArea` selects the true marking band.

**Fix (producer, `MeshRipSystem.TryFlattenDecal`):** keep the authored corner pairing (the
§16-round-2 mirroring lesson — orientation is never guessed) and affinely rescale the authored
up-facing UV rect into `m_TextureArea`. Identity for object decals whose area was derived from
the same UVs; the correct band for lane parts. Contract §4.2a / §5B note it; no schema change
— the quads simply now carry the UVs §4.2a always promised. Needs a mesh + network re-rip to
land in `meshes/` and `lanemeshes/`. *(Amended by §39: this verdict covers which atlas rect
the quad carries, not how many times the lane traverses it — the along-curve repeat is
`BuildCurveMatrix`'s, audited there.)*

## 20. Terrain cutouts: the game hides tunnel portals per-frame; the heightmap never has holes (2026-08-07)

The drive-through inspection asked why the ripped terrain walls off every underground road.
Answer, decompiled from `Game.Simulation.TerrainSystem`: **the heightmap is never carved.**
The open cut you see at a tunnel mouth or along a sunken trench is a screen-space effect
re-drawn every frame:

- `CullRoadsJob` (the §17 flattening walk) also emits, per lane section, the `ClipTerrain`
  flag (from the prefab's `GeometryFlags.ClipTerrain`) and a vertical `m_ClipOffset` band.
  `GenerateClipDataJob` turns every `ClipTerrain` section into a `ClipMapDraw` — the section's
  left/right béziers with the band base added to their y column and the band height — after
  expanding the band ±0.3 m.
- `TerrainSurface.RenderClipAreas` (a per-camera HDRP callback) draws those sections with the
  `RoadClip` shader (`m_ClipMesh` instanced over the `_RoadData` buffer), plus `areaClipMesh`
  (triangles of areas whose prefab carries `ClipArea`), into a stencil/depth pre-pass that the
  terrain renderer then tests to discard covered terrain pixels. `_ClipOffset.w =
  tan(fov/2)·0.002` and `m_OffsetFactor = ±1` (`InverseClipOffset`, set on Tunnel
  compositions, cleared at transition nodes) are a depth-bias magnitude and sign, not a
  semantic inversion.
- The band (`AddSegment`): base = the composition's edge-height extremes
  (`m_EdgeHeights` start/end half by `isStart`, with `m_SurfaceHeight.min`) plus
  `TerrainComposition.m_ClipHeightOffset`. Raised to `NetGeometryData.m_ElevationLimit * 3`
  ~~(30 m for roads)~~ *(§31 correction: `m_ElevationLimit` is **4 m** for every net and 2 m
  for pathways — `NetInitializeSystem` lines 872/2067 — so the raise is **12 m** roads / 6 m
  paths; the shipped bundle's own `top` histogram {6.3, 7.3, 12.3} confirms the producer,
  which reads the field live, was never wrong — only this section's prose was)* when the
  composition is **Tunnel with a transition side** (the portal) or has a **Lowered side and
  is not elevated** (the open trench). The extra ShiftTerrain-only sections
  (`float4/float5/float6` caps) all clear `ClipTerrain`.
- Correspondingly, the *height* clamp for a Lowered composition keeps terrain anywhere in
  `[lane, lane + ElevationLimit·3]` — a trench's covering bulge stays in the height cascade
  and is only ever hidden by the clip pass — and a shallow tunnel's cover is shifted **up** to
  `lane + HeightRange.max + 1`, which sits above its own small clip band, so tube cover is
  never cut from above. Deep tunnel sections clip nothing for the same reason.

**Producer consequence (`TerrainRipSystem.StampCutout`, `terrain/cutout.png`):** the §17
roadbed walk re-runs with `GenerateClipDataJob`'s gate (prefab `ClipTerrain`; tunnels and
trenches now included) and marks a cell where the section ribbon covers it and the terrain
stands within `(lane + 0.5 m, lane + band top]`. `ClipArea` area triangles stamp with an
unbounded top. ~~One deliberate divergence: the game's band starts 0.3 m below the lane and so
clips the roadbed itself (its road meshes hide the hole); the bundle keeps ground at or below
the lane, so ordinary surface roads mark nothing.~~ *(§31: the +0.5 lip is retired — the band
floor is now the game's own `cmin(edgeHeights) + m_ClipHeightOffset.x − 0.3`.)* Schema note
and amendment: bundle-schema §8, §12.1.

## 21. The GameMesh port versus the generator, line by line (2026-08-07)

The delta audit's re-verification of the ported network pipeline
(`NetworkRipSystem.GameMesh.cs`, landed 661a441–f7cba22) against
`Game.Rendering.BatchDataHelpers` (curve matrices), `BatchMeshHelpers+GenerateBatchMeshJob.
GenerateCompositionMesh` (the vertex path), `BatchInstanceSystem.UpdateNetInstances` (draw
gates and layer replacement) and `Game.Prefabs.NetCompositionMeshRefSystem` (mesh dedup and
`m_Rotate`). Every §8 game-rule row re-verified against the port; the table below records only
what the line-by-line pass added or changed.

| # | Game rule (decompiled source) | Port behaviour | Verdict |
|---|---|---|---|
| 1 | **V density is measured over surviving triangles only** — the `\|ΔV\|/\|Δz\|` accumulation and the untiled rebase ceiling sit inside the `math.all(int7 >= 0)` block, after the front/back cut | Was measured over *all* triangles including the cut-away half; a front-cut piece with non-uniform authored V would tile at the wrong density | **divergence — fixed** (remap built first, density accumulated only over triangles whose three corners survive) |
| 2 | Roundabout median **centre column has no z rebase** — its branch has no `vertex.z -=` line, unlike both side-column branches; the final `saturate` pins the front half at the middle curve's end (`m_Middle` splits 0.99/0.01, so matrix 5 spans the last 1 %) | Subtracted 1 for the front half, spreading those vertices across matrix 5's ~1 % span (sub-centimetre difference on a real ring) | **divergence — fixed** (subtraction removed; saturate now pins exactly as the game does) |
| 3 | `UpdateNetInstances` **replaces the Default layer with `NetCompositionMeshData.m_DefaultLayers`** when non-zero (and the entity is not already Marker) — and then still draws the mesh on those layers. `CommonUtils.GetBoundsMask` names the world-view set (`NormalLayers = Default\|Moving\|Tunnel\|Marker`); only Pipeline/SubPipeline/Waterway select the utility-cutaway/overlay cameras. A composition whose layers hold **no world bit** never draws in the world; a **Tunnel** composition (union of pieces flagged `UndergroundMesh.m_IsTunnel`, `MeshSystem`) is the tunnel walls and deck, drawn on the Tunnel layer | First fix skipped every Default-less composition — which silenced **all tunnel compositions**: every underground road/track lost its walls, deck and textures (regression caught same day) | **divergence — fixed, then re-fixed** (`BuildGameComposition` skips only compositions with no `NormalLayers` bit, counted in the report; Tunnel compositions build as world geometry) |
| 4 | `TryFindComposition` attempts the rotated (mirrored) mesh match **only when `CompositionFlags.General.Node` is absent** — node compositions are never rotate-shared | Carried a "rotated node part" counter and a NOT-REPRODUCED report line for a case the game cannot produce | **hack removed** (dead counter deleted; §8's "no measured city has produced one" is now "impossible by construction") |
| 5 | `LowerBottomToTerrain` / `RaiseTopToTerrain` mark vertices for shader-side terrain conforming (`int8.z` = 1/2 against the piece's height-bounds table) | Deliberate deviation (consumer rule against `terrain/heightmap.png`) — but previously uncounted, against the method rule | **deviation now counted** (report states how many pieces carried each flag) |
| 6 | Generator reads `NetCompositionMeshData` (mesh entity: `m_Width`, `m_MiddleOffset`, `m_Flags`) — for a rotate-shared mesh these are the *creator twin's* values | Port reads `NetCompositionData` from the composition entity. Only edges rotate-share; edge vertex paths consume `m_Width` alone, and twins agree on width; `Node\|Roundabout` flags are hash-preserved across shares | **benign** (equal wherever reachable) |
| 7 | Orphan matrix 5 = `float4x4.identity` (an unused slot; only Roundabout branches route to it) | Port fills slot 5 with the degenerate centre curve instead | **benign** (unreachable for orphans; the port's value is the saner one if ever hit) |
| 8 | Non-roundabout median centre column writes `int8.x = 4` and leaves `int8.y = 0` — pair (4, 0) at lerp weight 0 | Pair (4, 4) at weight 0 | **benign** (identical result at x = 0) |
| 9 | Untiled density `trueValue = ΔV/Δz` divides unguarded (0/0 → NaN stored in the half4) | Guarded to 0 when no triangle qualifies | **benign** (protective; the game tolerates the NaN because such vertices take the rebase path) |
| 10 | Triangle indices assumed in-range for their material's vertex window | Port bounds-checks and drops out-of-window triangles silently | **benign** (protective; never observed firing — left unchanged) |
| 11 | Sync knots: right-half offsets are the left's mirrored `1 − wzyx` (`GeometrySystem` line 2603 builds them exactly so) | `VertexSync.Apply` flipped the right half through `1 − s` before the knot remap | ~~match~~ → **divergence — fixed** (see row 12; the first draft of this row accepted the flip on §8's authority, and an in-game curb report falsified it the same day) |
| 12 | **Vertex sync coordinates and anchors** — `CalculateVertexSyncTarget`'s right branch anchors on `1 − a.w` (the outer-right margin) and returns targets ascending in the right half's own xParam coordinate (0 = middle, 1 = outer edge); both branches *physically move* the flare boundary curves (`m_Left/m_Right.c/d` mutation), so the shader's knot map must pin 0 → 0 and 1 → 1, scaling the end regions | The port flipped right-half knots through `1 − s` (mirroring every right-side curb knot at active-sync flares) and translated the end regions by `t.x − o.x` (shearing the outer curb strip off the boundary curve the game keeps it on). Symptom in the shipped bundle: **kerbs jutting into the roadway and not lining up at junction flares** — reported from a drive-through, confirmed against source | **divergence — fixed** (right half applies directly in its own coordinate; end regions scale with pinned endpoints; needs a re-rip to land) |

Draw gates (edge > 0.1 m, flare halves > 0.05 m, orphan gating incl. the outside-connection
exclusion), the marker skip, winding reversal under `Invert XOR FlipMesh`, both cut planes at
±0.01, `HalfLength` doubling, `PreserveShape` rails, and the zero-width three-rail wedge (§18)
all re-verified as exact matches in the new code path. Normals remain the §8 row-14 deliberate
deviation, **re-affirmed with the machinery now assessed**: object meshes transport authored
normals (`GeometryAssetReader`), and the port *could* do the same — rotate each authored normal
by the local frame the shader builds (the Bernstein-blended curve tangents, evaluable on CPU as
derivatives of the same cubics). Declined: it roughly doubles the per-vertex curve math on the
main-thread rip for a shading-only refinement, and the rebuilt normals are visually validated.
Revisit only if a consumer demonstrates a crease/kerb artefact the rebuild causes. (The in-game
"curbs jutting into the road" report that arrived during this pass was NOT a normals artefact —
normals cannot move geometry; it was the row-12 sync divergence, and re-deriving the sync from
`GeometrySystem` rather than accepting §8's verdict is what found it.)

## 22. TerrainRipSystem versus the terrain jobs, rule by rule (2026-08-07)

The delta audit of the Phase 6 system (never independently audited; §17/§20 were written beside
its code) against `Game.Simulation.TerrainSystem.CullRoadsJob` (`GetFlags`/`AddEdge`/`AddNode`/
`AddOrphans`/`AddSegment`), `GenerateClipDataJob`, `CullAreasJob`, `TerrainUtils` and
`WaterUtils`.

| # | Game rule (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | **Water depth decodes through `WaterUtils.ToWorldSpace(float)`**: metres = `raw / scale.y − offset.y`; every game sampler (`SampleDepth`) converts before use | `BuildWaterLevel` read `m_Depth` raw as metres and tested wetness on the raw value | **divergence — fixed** (decode applied; wet = decoded > 0. Correct water levels on any map where `scale.y ≠ 1` or `offset.y ≠ 0`) |
| 2 | `AddNode`'s roundabout branch evaluates its second strip pair (`node.m_Right` against the middle end point) with **`isStart: false`** — the end-half edge heights feed that band | All node strips used the start-half band | **divergence — fixed** (`StampNode` takes an end-band context; the roundabout right pair rides it; all other strips are `isStart: true` in the game too) |
| 3 | `AddNode` sheds elevated/tunnel + transition state (and `MoveTowards`-trims the ring ends by the sync offsets) before building the roundabout right pair's sections | Raw composition flags used; ring trims omitted (§17.3's documented simplification) | **approximated — now counted** (can only widen the cut band or over-stamp the ring start, never narrow/under; report states how often it fired) |
| 4 | `GetFlags`: `ClipTerrain`/`FlattenTerrain` are prefab `NetGeometryData.m_Flags`, evaluated once per net | Same gate per prefab | **match** |
| 5 | Draw gates: edge `any(start+end length > 0.1)`, node `any(half length > 0.05)`, orphans via the `Orphan` component with outside connections excluded | Same gates, same query shape | **match** |
| 6 | `AddSegment` width trim: `m_WidthOffset` lerps each boundary toward the other per control point (`Bezier4x1` over inverse widths), suppressed on `MiddleLeft`/`MiddleRight` sides | `StampSegment`'s trim flags reproduce every call site's Middle flags (edge: both trims; junction/straight/roundabout/orphan strips: exactly the game's assignments) | **match** |
| 7 | Junction vs straight-through: `lengthsq(m_Left.m_Right.d − m_Right.m_Left.d) > 0.0001` | Same test | **match** |
| 8 | Orphan caps: κ = 0.2761424 quarter-circle béziers, world-axis aligned, `MiddleRight`/`MiddleLeft` per quarter | Same construction, same trim sides | **match** |
| 9 | Clip band: base = start/end edge-height extremes + `m_ClipHeightOffset`; raised to `ElevationLimit·3` for Tunnel-with-transition and Lowered-not-elevated-not-tunnel; `GenerateClipDataJob` expands ±0.3 m | Same, with the documented deliberate divergence: band floor = lane + 0.5 m instead of the game's lane − 0.3 m (the game clips the roadbed its road meshes hide; the bundle keeps the drivable bed) | **match** (floor deviation documented, §20) |
| 10 | Deep tube sections keep the small band (their cover sits far above it — never cut from above); elevated compositions never raise the band | Same | **match** |
| 11 | `ClipArea` areas stamp through the same stencil (`areaClipMesh`) | Triangles stamped with an unbounded top | **match** (top bound: the game's area clip mesh is authored geometry with its own extent; unbounded top can only over-cut terrain *above* an authored clip surface, which is the intent) |
| 12 | Terrain sampling: `ToHeightmapSpace` = `(world + offset) × scale`; bilinear at integer grid coords; metres = `raw / scale.y − offset.y` | `ToCell`/`SampleTerrainHeight` identical | **match** |
| 13 | Water grid mapping: `ToSurfaceSpace`/`ToWorldSpace` mirror the terrain forms | Same in both directions | **match** |
| 14 | The game resamples nothing — depth lives on the sim grid; the renderer works there | Producer builds *level* (ground-at-sim-cell + depth), dilates one sim cell, resamples bilinearly with weight-dropped no-data corners | **deliberate deviation** (documented in the system header: coarse depth on fine ground floods gorges; level is the smooth physical quantity) |
| 15 | `TerrainSystem` flattens building lots and lane/area height adjustments into the cascade before readback (§17.1) | Ships in the heightmap verbatim via `GetHeightData(waitForPending: true)` | **match** |
| 16 | The elevated/tunnel-without-transition ground shift (away from the road) marks no roadbed | `TryGetGroundComposition` skips those compositions for the roadbed stamp, keeps transitions (portals are ground) | **match** (§17.3) |
| 17 | The transition-side 3 m `ClampLengthInverse` cut section (`float4` second `LaneSection`) narrows where a portal's ground is pulled to lane height | Whole transition composition stamped as roadbed | **approximated** (documented via §17.3's apron/portal wording; over-stamps the transition run's bed — the in-game rip + Blender pass judges whether it matters) |

Also re-verified: the surface-area layer reads the same `Game.Areas.Node`/`Triangle` buffers
`CullAreasJob` consumes, and the clip-area query keys on `Game.Areas.Clip` exactly as the
job's chunk filter does (it *skips* Clip chunks for terrain-area draws — clip areas are
stencil-only, which is what `terrain/cutout.png` encodes).

### §22a addendum (2026-08-07, late): the splatmap was readable all along

A user report — square splotches of ground texture where the game blends smoothly — exposed a
**completeness gap this pass had missed**: `TerrainMaterialSystem` exposes the splat weight
map itself (`public Texture splatmap`, the 4096² RGBA8 render target bound as
`colossal_Splatmap`), spanning exactly the playable area (`world.xz = playableOffset + uv ×
playableArea`, its own `UpdateSplatmap` viewport math). §17.2's "there is nothing to read
back" was true of the *roadbed mask* but was silently over-generalised into the layer design:
the ground *blend* — the thing that makes in-game terrain smooth — has a first-class readable
artifact, and the bundle shipped a per-cell index instead.

**Why the audit missed it:** the delta pass validated the *fidelity of code that exists*
against the rules it claims to implement, deferred to §17's settled conclusions per the
don't-re-litigate rule, and never re-ran the original audit's "reachable and unread" sweep
(§5) for the terrain domain — a scope hole, now closed in the command's method (an unread-data
sweep and an appearance comparison are part of every domain's checklist).

**Fix (landed):** `TerrainRipSystem.CaptureSplatmap` reads the target back through the
standard blit path (linear — the RT is `R8G8B8A8_UNorm`; rows come back top-first = north-first,
already the rasters convention) and ships `terrain/splatmap.png` + a `rasters.splatmap`
geo-block with `SplatMultiplier`/`SplatPower`. The channel-to-ground assignment lives only in
shaders, so the producer declares rather than guesses; the Blender harness calibrates it
against slope, the roadbed mask and flat ground, prints the result, and blends grass/dirt/rock
per-pixel in the shader — falling back to the per-face painting when calibration is ambiguous.
Contract §8 documents the layer (additive, optional). Awaiting the same re-rip as everything
else in this pass.

**§22a.1 correction (2026-08-07, post-rip): the roadbed class carries no signal.** The first
full rip with the layer landed and the harness *still* painted per-face squares, because its
calibration demanded a distinct dirt channel and treated "roadbed winner == grass winner" as
ambiguity. Measured on that rip, that equality is the real answer: the splat weights the grass
channel 251/255 under roadbed vs 243 on lawns (grass channel R; rock separates on steep ground,
B at 23 vs R at 10; corr(R, slope) = −0.87). The game's blend simply has no roadbed response —
the dirt look beside roads is the §5B wear decals and §8b areas. And rock is mostly NOT a
channel either: on very steep ground *all four* channels read ≈0 while the game renders rock —
the channels are painted-ground weights and the base (rock) is the unpainted residual
`max(0, 1 − ΣRGBA)`; a painted rock-variant channel exists but is minor (+14/255 flat→steep
where present) and unstable to name region-locally. Harness now calibrates grass (flat), dirt
optional, and always blends rock as the residual (no per-face slope override on the splat
path — that override was itself a source of squares); terrain faces are also smooth-shaded
(flat-shaded 17.5 m cells at full-city stride read as a lighting checkerboard no texture can
hide). Contract §8 warns consumers off both assumptions.

**§22a.2 (same rip): why the ripped markings stayed invisible in Blender — two harness bugs,
no producer fault.** The §19 marking data was verified correct on disk (opaque bands inside
each part's UV rect). (1) A fixed `_DECAL_LIFT` over the *lane path* cannot clear the
carriageway: the rendered road top near centre lines measures a median 7 mm above the lane
curve and beats path + 15 mm on 42% of samples — the game projects decals, it never coplanes
them. The harness now drapes decal-part vertices (subdivided ~3 m) onto the §5 network meshes
by ray-cast. (2) The lane-part loader's Blender→bundle map `(-x, z, -y)` is det −1 and its
mirror was never winding-compensated (only the second mirror, P, was) — every lane part
rendered with normals down; invisible for double-sided fences, black-lit for the markings the
drape had just surfaced. Both fixes are consumer-side: existing bundles render correctly with
a re-import, no re-rip needed.

## 23. GeometryAssetReader and the mesh delta versus the game (2026-08-07)

The delta audit of `IO/GeometryAssetReader.cs` (new) and `MeshRipSystem`'s changed paths
against `RenderPrefab.ObtainMesh`, the `Game.Prefabs.Mesh*` unpack helpers,
`ManagedBatchSystem.CreateBatch` and `AssetImportPipeline.SetupDecalComponent`.

| # | Game rule (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | `RenderPrefab.ObtainMesh(materialIndex)` walks the asset's meshes in order, subtracting each `subMeshCount` — surface *i* binds the *i*-th flat (mesh, sub-mesh) pair | `ReadAll`/`ReadUnityMeshes` assign exactly that ordinal (rebased per mesh, carried through `Merge`) | **match** |
| 2 | Vertex attribute decode: `MeshVertex/MeshNormal/MeshUV0/MeshTangent/MeshIndex.Unpack(src, dst, count, format, dimension)` handle float32/float16/normalised formats | The reader calls the game's own `Unpack` overloads — no reimplementation to diverge | **match by construction** |
| 3 | Sub-mesh `baseVertex` handling: the game unpacks with a vertex offset per sub-mesh | Reader folds `SubMeshDescriptor.baseVertex` into the descriptor's index range after a whole-buffer unpack — same absolute indices | **match** (equivalent form; glTF needs absolute indices anyway) |
| 4 | `ObtainMeshes` loads CPU data only when `m_Instances == null`; `keepReadable` is dead in this build; live-instance prefabs must be read via the Unity meshes (§17.4) | `TryReadGeometryAsset` gates the native path on `asset.data.IsValid`, falls back to `ReadUnityMeshes`, always releases | **match** (as §17.4 recorded; confirmed as landed) |
| 5 | `SetupDecalComponent` derives `m_TextureArea` from vertices with `SqrMagnitude > 0.01` **and `Angle(up, n) < 0.01°`** — exactly-up, not merely upward | `TryFlattenDecal` used `n.y ≥ 0.9` for both the corner pairing and the authored-rect derivation, so the §19 rescale was only *approximately* an identity for DefaultDecal objects | **divergence — fixed** (three-pass selection: the pipeline's exact-up test first, the validated ≥ 0.9 tolerance second, all vertices last; the rescale is now an identity by construction where the game derived the area from the same UVs) |
| 6 | `CreateBatch` binds `_TextureArea` from two sources: `DecalProperties` and `OverlayProperties` (`component6`) | Only `DecalProperties` is flattened; `OverlayProperties` are tool overlays, not built environment | **benign** (out of bundle scope) |
| 7 | The asset file header's `meshCount` is what `CreateMeshes` iterates; the prefab's serialized count is a duplicate | Header preferred once resident, prefab count as fallback | **match** |
| 8 | No game counterpart merges an asset's meshes into one buffer (the game keeps them as separate draws) | `Merge` drops an attribute unless every part carries it, rather than fabricating values | **benign** (producer form; primitives keep per-part surface ordinals) |

## 24. MaterialRipSystem delta versus the VT decoder and template semantics (2026-08-07)

The delta audit of the crash fence, the two-channel normal rebuild, and the template fallback
chain, against `Colossal.IO.AssetDatabase.VTSurfaceAsset`/`VTSurfaceDataHandler`/`SurfaceAsset`
(decompiled from the Colossal assembly — first time this pass reached outside Game.dll).

| # | Game fact (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | `VTSurfaceAsset.Load()` is **fully synchronous**: `ReadHeader` → template `new Material(template)` → `FillDebugMaterial` all inside the call | The in-flight marker brackets exactly `Load()` — the whole decode happens inside the fence window | **match** (the fence cannot miss the crash, and nothing else runs in the window to be mis-convicted) |
| 2 | `FillDebugMaterial` slices with header-derived sizes into fixed `NativeArray`s — e.g. `NativeArray<byte>.Copy(src, 0, dst, dst.Length − src.Length, …)`, negative offset on a header/stream mismatch, **no bounds checks in release** → native crash | The fence's premise, confirmed line-by-line; every native-risk operation of the decode (copies, decompression) is inside `Load()` | **match** (the fenced call is genuinely the decode's whole native-crash surface; other readback routes are managed-guarded Unity API) |
| 3 | Marker protocol: written before `Load()`, deleted in `finally` — reached on success and managed failure alike | Only a native crash leaves the marker; conviction appends to `vt_crash_guard.txt`, skips are counted and declared in warnings | **match** (sound; a wrong conviction would need an unrelated crash inside a milliseconds window, and is recoverable by deleting the guard file) |
| 4 | Unity DXT5nm/BC3n packing: X in **alpha**, Y in green, red pinned 1, blue mirrors green; the shader reconstructs Z from `wy·2−1` | `ReconstructTwoChannelNormal` rebuilds `(x, y, z, 1)` exactly so, gated on the normal-map *slot* with the packing signature as confirmation (≥ 90 % red ≥ 250, alpha non-constant — thresholds measured on a full city: 784 packed normals caught, 5 metallic mask maps correctly left alone) | **match** (the detection is a producer heuristic — the game knows from the texture format — but slot-gated and measured; a false positive requires a normal-slot texture that is not a normal map) |
| 5 | `SurfaceAsset.SyncToUnityMaterial` walks `m_Textures` directly (line 603) — a non-VT surface binds whatever its texture dictionary holds | The texture census now includes `surface.textures` keys alongside the VT stack names | **match** (closes the §4.2 failure shape for non-VT maps) |
| 6 | The runtime material is `new Material(template)` with surface properties overlaid (seen verbatim in `VTSurfaceAsset.Load`); HDRP re-derives keywords from serialized floats on validate | `AlphaMode`'s chain — surface keywords → surface floats (`_SurfaceType`/`_AlphaCutoffEnable`, overriding in both directions) → template keywords/floats — and the `_AlphaCutoff`/`_DoubleSidedEnable` template fallbacks mirror that construction | **match** |

## 25. Instance exclusions versus the game's archetypes (2026-08-07)

The delta audit of the utility-marker and vehicle-cargo exclusions
(`MeshRipSystem.IsUtilityMarkerSubObject`/`IsUndergroundOnlyPrefab`/`IsVehicleCargoSubObject`,
applied identically by both rip queries) against the prefab archetype sources.

| # | Game fact (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | `Game.Net.Marker` on net entities comes **only** from `MarkerNet.GetArchetypeComponents` (node and edge archetypes alike; `MarkerObjectPrefab` adds the distinct `Game.Objects` marker) | Marker-owner test with no extra prefab-class narrowing | **match** (the §17.5 removal of the `PipelineData`/`PowerLineData` condition re-verified) |
| 2 | `TaxiwayData`'s only source is `TaxiwayPrefab.GetPrefabComponents` | The taxiway exception keys on `TaxiwayData` directly | **match** |
| 3 | `VehiclePrefab.GetArchetypeComponents` adds `Game.Vehicles.Vehicle` unconditionally; `CarTrailerPrefab : CarBasePrefab : VehiclePrefab` | `IsVehicleCargoSubObject` tests `Vehicle` on the owner (`CarTrailer` redundantly, for readability) | **match** — the exclusion itself is the documented deliberate deviation (cargo riding an agent is agent, not built environment; the bundle already excludes vehicles) |
| 4 | `UpdateObjectInstances` replaces a sub-mesh's batch layer with `MeshData.m_DefaultLayers` **per part** — a Pipeline-only part inside an otherwise visible prefab never draws in the world view | `SelectSubMeshes` filtered by state flags only; a mixed prefab would have shipped its underground hookup furniture inside its `.glb` | **divergence — fixed** (per-part skip with the conservative Pipeline/SubPipeline-only test, counted in the report; the whole-instance §17.5 rule unchanged) |
| 5 | `OutlineOnly` parts keep only the Outline layer (`layers &= ~Outline; if (OutlineOnly) layers &= Outline`) | Skipped in selection and in the no-unconditional-parts fallback | **match** (re-verified in the new code path) |

## 26. The lane geometry layer versus `UpdateLaneInstances` (2026-08-07)

The delta audit of the `lanemeshes/` parameter records (`RipLaneGeometry`,
`Domain/LaneGeometryRecord`, `IO/NetworkWriter`) and the lane census against
`BatchInstanceSystem.UpdateLaneInstances`/`IsNetOwnerTunnel` and
`BatchDataHelpers.BuildCurveScale`.

| # | Game rule (decompiled source) | Producer | Verdict |
|---|---|---|---|
| 1 | Draw gate `curve.m_Length > 0.1f` | Same | **match** |
| 2 | Sub-mesh exclusion mask: `RequireEditor` (markers hidden), the off-hand traffic bit, `RequireSafe` on Unsafe pedestrian/car lanes, `RequireLevelCrossing` when not crossing, `RequireTrack` vs `RequireClear` on Switch/DiamondCrossing — a set bit *excludes* (§12's inverted semantics) | Identical mask construction, re-verified line-by-line | **match** |
| 3 | **`IsNetOwnerTunnel` walks two links**: the owner's own `Elevation`, and — for node owners, which carry no elevation — every `ConnectedEdge` of the node (any edge with `cmin(elevation) < 0` makes the node's lanes Tunnel-layer). The game *relayers* these lanes (Default → Tunnel) and still draws them | Only tested the owner's own `Elevation`; node-owned lane furniture at tunnel junctions was mis-classified as surface. The result now **tags** the lane record (`tunnel: true`, contract §5B) rather than skipping it — tunnels ship fully rendered, mirroring the game's relayer-not-hide behaviour | **divergence — fixed** (full two-branch port, used for tagging) |
| 4 | Each uncut `CutRange` span is **re-gated on the 0.1 m threshold** (`curve3.m_Length > 0.1f` on the span's arc); a fully-cut lane draws nothing | Spans were recorded regardless of length, and a lane whose every span failed would have shipped with empty `spans` — which the contract reads as "whole curve" | **divergence — fixed** (per-span gate; all-spans-failed lanes skipped and counted with the degenerate gate) |
| 5 | `BuildCurveScale(NodeLane, NetLaneData)`: width scale = `1 + m_WidthOffset / m_Width`, identity when width is 0 | Same, start/end pair | **match** |
| 6 | Hanging: `HangingLane.m_Distances` end drops + prefab `UtilityLaneData.m_Hanging`; the cubic lift documented in contract §5B v2 | Per-lane distances in the record, factor in the catalogue | **match** (§12's verdict re-verified; formula unchanged) |
| 7 | Per-part layer replacement: `GetLayers` returns `m_DefaultLayers` **verbatim** except the exact `Pipeline\|SubPipeline` pair (owner-resolved) — a Tunnel-defaulting part draws on the Tunnel layer, a Waterway part only in the overlay view | First reading excluded every part with `m_DefaultLayers != 0` and no Default bit, which also dropped Tunnel-layer parts — world geometry inside tunnels. Catalogue now excludes only parts with no `NormalLayers` bit (Pipeline/SubPipeline/Waterway) | ~~match~~ → **divergence — fixed** (the first draft's verdict was wrong for Tunnel parts) |
| 8 | No game counterpart for the lane census (`LaneRipSystem` delta) — it is a producer cross-check that drawn-lane counts track the baked layer plus counted skips | Diagnostic only, no bundle output changed | **benign** |

*(Amended by §39: this matrix had no `BuildCurveMatrix` row — the along-curve texture scale
`N = L/max(1, meshLength)` was never audited, and the reference importer stretched every
marking texture over its whole lane. See §39 for the rule and the fix.)*

## 27. BaseSkirtBuilder versus `GenerateObjectMesh`'s base path (2026-08-07)

`IO/BaseSkirtBuilder.cs` claims to be a faithful port of the game's foundation-skirt generator
(`BatchMeshHelpers.GenerateObjectMesh` → `AddBaseLines`/`AddBaseVertices`); this section is the
line-by-line comparison against the decompiled generator read in §21's pass.

| # | Game rule | Producer | Verdict |
|---|---|---|---|
| 1 | Gate: `MeshFlags.Base`; base level = `select(0, bounds.min.y, MinBounds)` | Same gate and offset at the call site | **match** |
| 2 | Exclusion pass: edges whose third vertex is *not above* the ±0.01 band (`(y < band.y) & inBand.yzx & inBand.zxy`) mark interior seams | Identical predicate, identical edge keys (both orders checked) | **match** |
| 3 | Keep pass: edges whose third vertex is at/above the band top; per-case point pairs (b,c)/(c,a)/(a,b) with the vertex normals' xz as direction | Identical, incl. the direction-mismatch point split and the `-1 − index` arrived-marker encoding | **match** |
| 4 | Distance resolution: backward `m_PrevPos` walk per line, arbitrary seam on closed loops, end split when the arriving distance disagrees | Forward chains-from-heads + loop cut + seam split — different traversal, same outcome; both choose the loop seam arbitrarily (iteration-order-dependent in the game too), so the only possible difference is the u-origin phase along a closed loop | **match** (outcome-equivalent; phase is arbitrary in the game as well) |
| 5 | Band: top ring at base (v=1), bottom ring 1 m below (v=0), u = perimeter metres, quad strip winding `(s, s+1, e)(e, s+1, e+1)`, normals horizontal from the silhouette direction | Verbatim, `BandDepth = 1`, normals normalised (the game feeds raw xz into the octahedral encode — normalising is the safer equivalent) | **match** |
| 6 | The v=0 ring is dropped to the terrain by the vertex shader at draw time | Shipped at −1 m with the drop as a consumer rule against `terrain/heightmap.png` (contract §5.x documents it) | **deliberate deviation** (documented; the terrain layer the drop needs now exists in the bundle) |
| 7 | The game rebuilds this batch per LOD/batch rebuild via a BVH walk | Linear scan once per prefab on the writer task, off the main thread | **benign** (perf rule satisfied; no main-thread cost) |

## 28. The writer deltas versus their specs, and the delta pass close-out (2026-08-07)

**`IO/Png.cs`** (`EncodeGray8`/`EncodeGray16`/`Assemble`): IHDR colour type 0 is valid at bit
depths 8 and 16; 16-bit samples are written big-endian as the spec requires; every scanline
carries its filter byte (type 0/None); the deflate path is the §14-validated one, shared.
Verdict: **match (spec)**. Perf note: filter None compresses heightmaps worse than Sub/Up
would, but encoding runs on the writer task off the main thread and the files land once per
rip — not worth complexity until a measured need (prefer-simple rule).

**`IO/GltfWriter.cs`**: material pooling by id per file (several parts sharing a surface now
share one glTF material entry — the §6 name join unchanged, files honest about material
count); node `scale` emitted as per-axis positive magnitudes (mirroring stays in
position/rotation/winding, so no negative-determinant nodes — glTF-valid); the
single-part-at-root collapse now correctly refuses when a scale is present. Verdict:
**match (spec)**.

**Close-out.** The hack hunt over the delta files finds only counted, documented
approximations (roundabout blend weights §8/§21, roundabout cut bands §22, terrain-conform
flags §21, orphan ring trims §17.3) — no uncounted guesses. One stale doc comment describing
the removed rotated-node approximation was deleted. The shipped bundle's `warnings.jsonl`
categories (88 unresolved managed prefabs = the known unowned asset packs; 118 mesh-gap
warnings) all predate the geometry-asset `ReadAll`/`IsValid` fixes this pass verified — the
next full rip re-measures them. Perf: no new main-thread cost was added anywhere in this pass
(the sub-mesh layer test and tunnel edge-walk are per-prefab/per-lane component reads of the
kind the game itself performs per instance per frame); the V-density restructure moved work,
it did not add any.

## 29. The completeness sweep — reachable and unread, all domains (2026-08-07)

The step-4b sweep run retroactively over every audited domain (this pass's and the previous
ones'), triggered by the §22a lesson. Method: census every `colossal_*` global shader binding
in Game.dll, probe the public properties of the systems each domain reads, and re-status the
original audit's §5 items.

**Closed by this sweep** (all additive, all landed and compiling):

| Find | What the game exposes | Now shipped as |
|---|---|---|
| Ground **normal maps** | `colossal_TerrainGrass/Dirt/RockNormal` global bindings — the theme lights its ground with these; the bundle shipped only the three diffuse maps, so consumer ground was flat-lit | `terrain/surfaces/*_normal.png`, decoded through the §24.4 two-channel rebuild |
| **Paintable extras** | `colossal_TerrainExtra1..4Diffuse/Normal` — the surfaces a player paints | `terrain/surfaces/extra*.png` when bound (4×4 fallback placeholders skipped) |
| **User splatmap** | `TerrainMaterialSystem.UserSplatmap` (public, R8G8, playable extent) — the painted-surface weights | `terrain/user_splatmap.png` + `rasters.userSplatmap`, written only when any texel is non-zero |
| **Backdrop terrain** | `TerrainSystem.worldHeightmap` (public, R16, `worldOffset…+worldSize`) — the horizon terrain beyond the playable border; the contract reserved `worldTerrain` and the producer never wrote it | `terrain/world_heightmap.png` (lossless `AsyncGPUReadback`) + `rasters.worldTerrain`; playable-border continuity is the importer's self-check |

**Examined and deliberately not shipped** (out of the bundle's built-environment scope, or
simulation state):

- Water flow/velocity and sea propagation (`colossal_FlowTexture*`, `colossal_Sea*`) — motion
  state, not shape. The **backdrop water** grid *is* reachable
  (`WaterSystem.GetSurfacesData().downscaledDepths` + `hasBackdrop`) and is the one open
  candidate left in the terrain domain: a `worldWater` layer would need its own grid and
  resample, deferred until a consumer wants horizon sea that is not flat.
- Weather/season state (`colossal_SnowScale`, wetness/snow per instance) — §2's standing rule:
  the bundle is the dry, built world.
- Per-batch runtime buffers (`colossal_BuildingState`, `ColorMask0..2`, `LodFade`,
  `VisibleIndices`) — render-frame state; the persistent equivalents already ship
  (`colors.json`, §7.2 colorSet).
- Tool overlays and route UI (`colossal_Overlay*`, `colossal_Route*`) — not world content
  (routes exist as the parent Carto vector export).

**§5 of the original audit, re-statused:** 5.1 (unproduced documented attrs) and 5.4 (lane
graph) closed by the §9–§15 pass and Phase 5B; 5.2 closed by Phase 6; 5.3 is Phase 7's planned
manifest work, not a miss; 5.5 (per-
channel colour-variation detail beyond `colors.json`) still open, unchanged priority; 5.6
half-closed (surface areas §8b shipped; **districts/zones still have no layer** — the data is
plain ECS and schema-1-compatible, the remaining candidate from the earliest audit); 5.7
(semantic attrs: unit counts, placement flags, pollution) still open, low value for a
geometry consumer, additive whenever wanted.

The sweep found **no further silent reconstruction** of the splatmap's kind: everywhere else
the producer either ships the game's own data or documents the deviation with a counter. The
two remaining unshipped candidates (worldWater, districts) are now *named* rather than
unknown, which is the sweep's job.

**What this pass changed, in one list** (all schema-compatible; geometry corrections only):
1. §21: V-density measured over surviving triangles; roundabout centre-column rebase removed;
   composition-level no-world-layer gate (first drafted as "Default-less", which silenced
   every tunnel composition — corrected same day to the `GetBoundsMask` `NormalLayers` test;
   Tunnel compositions are world geometry); dead rotated-node counter removed; terrain-conform
   counters added.
2. §21 row 12: **the vertex-sync coordinate fix** — right-half knots applied in their own
   coordinate, end regions scale with pinned endpoints. This is the "curbs jutting into the
   road" defect reported from in-game comparison; needs a re-rip to land.
3. §22: water depth decoded through `ToWorldSpace` (`raw / scale.y − offset.y`); the
   roundabout ring's right pair takes the end-half clip band; roundabout cut approximation
   counted.
4. §23: decal rect/pairing derived exact-up-first, matching `SetupDecalComponent`'s vertex
   test.
5. §25: per-part Pipeline-layer skip inside mixed prefabs, counted.
6. §26: `IsNetOwnerTunnel`'s connected-edge branch ported and its result now *tags* lane
   records (`tunnel: true`) instead of skipping them — tunnel lanes ship, mirroring the
   game's relayer-not-hide behaviour (contract §5B change, same-day follow-up); cut spans
   re-gated on 0.1 m with fully-cut lanes skipped.

Every fix is unvalidated in-game until the next rip: the user validates with a full-city rip
plus the Blender harness, then accepts.

---

## 30. Surface areas are projections, not planes (2026-08-08)

Raised by a reported defect: *"terrain clips through areas (parking lot, driveways etc) and
decals do not map directly on them"*, with the hypothesis that areas lack the terrain
lowering roads get. Validated against source; the hypothesis is wrong in an instructive way,
and the defect is real and older than the §8b layer's last change.

### The game's rule

| Game | Source | Producer / harness before | Verdict |
|---|---|---|---|
| An area triangle is rendered as a **prism**, not a surface: a 6-vertex, 8-face volume literally named `"Area triangle volume"` | `AreaRenderSystem.CreateMesh`, drawn via `Graphics.RenderMeshIndirect` per triangle instance | harness drew the flat triangle at node heights | **divergence — fixed harness-side** |
| The prism spans `Triangle.m_HeightRange`, the min/max of the heightmap over the triangle's footprint **relative to its own plane**, walked cell by cell, water included | `Areas.GeometrySystem.UpdateHeightRange` → `GetHeightRange`; `AreaBatchSystem` line 1328 scales the volume to it ± `RenderedAreaData.m_HeightOffset` | not carried at all | **divergence — now shipped as `heightRanges`** |
| The material is a **decal**: `m_DecalLayerMask = DecalLayers.Terrain`. The paint lands on the terrain's own surface, so conformance is free in-game | `Prefabs.RenderedArea` | — | match (explains why the game has no such defect) |
| Texture is world-planar at the authored `m_UVScale` (default 0.2 → one repeat / 5 m), handed to the shader as `colossal_UVScale` | `RenderedArea.m_UVScale`; `AreaBatchSystem` line 2109 | harness guessed `world / 4.0`; contract told consumers to "pick a plausible metres-per-repeat" | **hack — now shipped as `uvScale`** |
| Terrain **is** levelled under areas — but only for `TerrainArea`, a `LotPrefab` component (`m_HeightOffset` 20 m, `m_SlopeWidth`, `m_NoiseScale`, `m_AbsoluteHeight`), applied by `TerrainSystem`'s `CullAreasJob` into the same height buffer as the road flattening | `Prefabs.TerrainArea`, `Simulation.TerrainSystem` line 1775 | already baked into §8's heightmap, which is read from `GetHeightData` (§17.1) | match — **nothing to add** |
| A `SurfacePrefab` area (every §8b record: pavement, grass, tiles, sand) has no `TerrainArea`, so the ground under it is never touched | archetype: `RenderedArea` without `TerrainArea` | contract §8b claimed "the game grades terrain under areas" | **contract was wrong — corrected** |
| `Node.m_Elevation` per area node | `Areas.Node` | not carried | open, unmeasured — no consumer need identified; revisit if elevated (`Space`) areas ever ship |

### Measured on the shipped full-city bundle

The game's own `GetHeightRange`, recomputed offline against `terrain/heightmap.png` over all
27,422 areas / 175,783 triangles:

- terrain rises **through** the flat triangle on **26.6%** of triangles and **61.4%** of
  areas — p95 11 cm, p99 55 cm, worst 28.7 m.
- worst classes are exactly the reported ones: `Pavement_Surface_01` 77.2% of its areas,
  `Pavement_Surface_02` 73.1%, `Tiles_Surface_02` 67.6%.

The second symptom is a separate, one-constant bug found on the way: the harness lifted areas
**2 cm** and §4.2a decal instances **1.5 cm**, so the pavement painted over the bay markings
standing on it. Of the 11,171 decal instances landing on an area, 15.3% sat below its plane
and 9.7% floated more than 5 cm above it.

### What changed

- **Producer** — `areas.jsonl` carries `heightRanges` (free: the `Triangle` buffer is already
  read); `areaprefabs.json` carries `uvScale` and `heightOffset`. Contract §8b rewritten: the
  triangle is a footprint, the consumer must drape it, and the terrain is not graded flat.
- **Harness** — `_conform_area_triangle` splits each triangle until the ground under its edge
  midpoints *and centroid* is within half the area's own lift, ray-casting the built terrain
  through the shared `_ProjectionSurface` (renamed from `_RoadSurface`: lane decals and areas
  project onto a surface for the same reason). Tolerance from the lift is the same rule the
  lane rows use (§29a/`_decal_rows`) — how far the ground may rise through a piece cannot be
  chosen independently of how far the piece is lifted clear of it. Bilinear sampling of the
  heightmap is **not** a substitute for the ray-cast: the terrain is built as quads and
  Blender picks the diagonal, worth p95 9.4 cm at 3.5 m cells and 41 cm at a full-city
  import's strided 14 m. The lift ladder is documented and ordered: terrain 0 → lane tint
  8 mm → lane marking 14 mm → area 20 mm → decal instance 30 mm.

### Three stopping rules, each of which had to be measured rather than reasoned

Every one of them was wrong on the first attempt, and each was caught by its own counter
rather than by eye:

1. **The size floor.** First cut stopped at a quarter of a terrain cell, on the argument that
   a smaller piece cannot straddle a kink. It can — the kinks are the quads' own diagonals
   (anywhere inside a cell) and the 15 cm roadbed-sink boundary every driveway crosses — and
   14.6% of pieces stopped with up to 2.1 cm still above them. Now 2% of a cell, min 10 cm.
2. **The depth cap.** At the full-city stride a big triangle needs ~10 halvings just to reach
   that floor, so a cap of 9 silently *became* the stopping rule: 810k pieces stopped with up
   to 4 m of ground above them. Raised to 12, with the budget as the real guard.
3. **The piece budget.** A flat 4096 per triangle is backwards at both ends — far too much for
   a driveway triangle inside one cell, far too little for a 25-hectare field over a gorge. Now
   scaled by the ground the triangle covers (`_AREA_PIECES_PER_CELL`), which also bounds the
   whole layer at so-many pieces per cell of painted ground.

And the deviation test itself only sampled the three edge midpoints, so a rise in the middle of
a piece — a knoll inside one cell, the peak of a quad's diagonal — went through it unseen; the
centroid is now sampled too.

The counter reports **square metres of painted ground left unconformed**, not pieces: a piece
is an arbitrary unit (a budget stop leaves a few big ones, a floor stop many small ones) and
only the area says how much of the city still shows the defect.

### Where it landed

| Import | Pieces | Unconformed ground | Worst residual | Areas layer |
|---|---|---|---|---|
| suburb region, 3.5 m terrain cells | 5,134 | **0.00%** | 1 cm | 0.6 s |
| full city, strided 14 m terrain cells | 2,710,054 | **1.05%** | 7.0 m | 81 s |

The full-city remainder is a **counted, deliberate approximation**, not an open defect: it is
the giant field and concrete surfaces draped over real relief — 11.16% of painted ground sits
on triangles whose own three corners differ by more than 2 m of terrain height
(`Concrete_Surface_01` 45.4% of its area, the Haristess farm overlays 32–48%) — resolved
against a terrain grid that is itself a 14 m subsample at that scale. A region import, which
is where anything is looked at closely, conforms completely. The tuning is worth recording
because the naive reading of the counters points the wrong way: the run that produced the
*fewest* pieces (1.82M, 62 s) had **five times** the unconformed ground of the one that
produced more (2.71M, 81 s), because its budget was starving exactly the triangles that
needed the work.

## 31. Tunnel entrances end-to-end: the welded terrain lattice and the game-true clip band (2026-08-08)

Raised by screenshot comparison (`E:\carto-out\shots`): tall vertical spike/fin artifacts
around tunnel portals, chevron sawtooth along trench walls, terrain never melding down to the
mouth, and terrain poking through roads on full-map imports. Re-derived from decompiled source
without leaning on earlier sections' conclusions; two of those conclusions fell (the §20
"30 m" figure and the deliberate `+0.5` band floor).

### The game's mechanism, verified end-to-end

| Game | Source | Producer / harness before | Verdict |
|---|---|---|---|
| Heightmap 4096², 3.5 m/texel, 6.25 cm quantum; road/lot/area flattening rasterised into it per cascade slice; **never carved** for tunnels | `TerrainSystem.FinalizeTerrainData`, `RenderCascade`/`DrawHeightAdjustments` | shipped as-is (correct) | match |
| Rendered terrain = GPU CBT adaptive subdivision (target 12–24 px triangles) over camera-centred cascade slices at 3.5 / 1.75 / **0.875 m**/texel | `UpdateCascades`, `TerrainQualitySettings`, HDRP `CBTSubdivisionTerrainEngine` | harness meshed the whole map at stride 4 = **14 m faces** — 16× coarser than the shipped raster, and the block-refinement patch pinned its edges to that subsample | **divergence — fixed: corridor-refined welded lattice** |
| Tunnel/trench cut = per-pixel clip by smooth swept prisms (2 cubic béziers + band), `RoadClip` shader; edges have no relation to the terrain grid | `GenerateClipDataJob`, `TerrainSurface.RenderClipAreas`, `CreateRoadMeshes` (`m_ClipMesh`: 36-vert closed prism) | §8c clip volumes (correct shape); consumer rasterised with a half-cell column bias | **divergence — bias fixed (consumer), mirror row bias fixed (producer `FillQuad`)** |
| Band floor = `cmin(edgeHeights) + m_ClipHeightOffset.x − 0.3` — the clip **includes the roadbed shell**, on every ClipTerrain net, everywhere | `AddSegment` lines 1419-1423, `GenerateClipDataJob` lines 1704-1705 | producer invented `+0.5` "drivable lip" (`CutoutFloor`) | **hack removed — game-true floor shipped per record** |
| Band top raise = `ElevationLimit·3` = **12 m** roads / 6 m paths (`m_ElevationLimit` = 4/2, `NetInitializeSystem` 872/2067) | `AddSegment` 1467-1495 | producer read the field live (already correct); §20's prose said 30 m | **doc corrected; live log line added (`clip band class …`)** |
| The rock/concrete cut faces at a portal are **NetPiece meshes** (`NetPieceRequirements.Tunnel/Lowered/Low+HighTransition`), not terrain | `NetCompositionHelpers`; zero hits for cliff/wall/skirt in any terrain path (4,393-file dump) | bundle already ships and places them | match — nothing to synthesize |
| Melding = FlattenTerrain sections pull the heightmap down with an 8 m smoothing apron (roads; 20 m waterway, 100 m taxiway) | `NetUtils.GetTerrainSmoothingWidth`, `CullRoadsJob` | baked into the shipped heightmap; stride-4 sampling threw it away between samples | **fixed by native-res corridors (the 3-cell dilation covers the apron)** |
| Areas never z-fight: area surfaces are `DecalLayers.Terrain` projectors onto the terrain's own pixels | `Prefabs.RenderedArea` (§30) | §30's conform machinery | match (unchanged) |

### The defects, each with its mechanism (all consumer-side)

- **Spikes/fins**: `_refine_blocks` pinned refined-block outer rings bilinearly to the 4:1
  coarse edge while interiors took native heights (up-to-30 m vertical quads, 3.5 m wide);
  the stride-4 block-min pull-down dropped all four corners of a 14 m block to its deepest
  bed texel (funnels); refined blocks bypassed the 0.15 m sink (tears). All three mechanisms
  deleted, not patched.
- **Chevrons**: 14 m per-block tent pleating + unwelded refined patches (split normals) +
  the half-cell column bias in `_fill_clip_quad` (`round()` against half-integer centres).
- **No meld**: the `+0.5` floor lip + stride-4 missing the apron + `cut_all` keeping the
  coarse envelope at block boundaries.
- **Poke-through**: 1 of 16 texels set a 14 m face's height; the sink keyed on a single
  corner sample; the pull-down triggered on a 25-cell footprint but valued a 16-cell block.

### What replaced it (harness `build_terrain`, rewritten)

One shared vertex lattice over the native texel grid; three face families indexing it —
native 3.5 m quads inside `dilate3(roadbed | cut | shoreline)` corridors, coarse 14 m quads
elsewhere, centre-fan triangles in the one-block transition ring (the fan's perimeter includes
the refined side's edge vertices, closing T-junctions without pinning). Sink applied
per-vertex (`dilate1(roadbed)`) to the lattice once, before any face family; block-min
pull-down deleted; cut decided per native cell by the stride-1 any-of-four rule everywhere a
cut can occur. Region imports (stride 1) degenerate to all-refined — the correctness anchor.
`_mesh_from_arrays` grew a `poly_sizes` array parameter to mix quads and fans in one mesh.
`clipvolumes.jsonl` is now mandatory (`_legacy_cutout_grid` deleted; strict `bottom` read),
and `--clip-floor` previews floor values against an existing bundle.

### Measured against the fresh spike bundle (rip 2026-08-08 13:44, HEAD producer)

- Full map: 22 s terrain phase; 4.17M native faces in 261k corridor blocks (25% of blocks),
  529k transition fans, 723k coarse quads; 9,428 faces cut (14,779 with the −0.3 floor
  preview — the extra 5.4k are the everyday shell along surface roads).
- **Weld: 0** coincident vertex pairs (was: every refined-block ring). **Heights: 0** of
  5.17M vertices at a height not in the raster ± the documented sink — synthesized heights,
  and with them the spike class, are structurally impossible.
- **Poke-through: 3 of 2,469** sampled surface-road car-lane stations with terrain above the
  lane, worst +0.28 m (stations from `lanes.jsonl`, tunnel/elevated/garage/waterway lanes
  excluded — the first metric draft counted seaway sandbanks and garage ramps as defects).
- Portal/trench/shoreline renders at a river-tunnel site: full-map output now visually
  indistinguishable from the stride-1 region import; shoreline steps at 3.5 m (the 14 m
  square shoreline fell out of the same corridor mechanism).

**Awaiting in-game re-rip** (producer side only): the game-true `bottom` values and the
`clip band class` log lines (which assert `L·3` = 12/6 live). Every harness-side result above
is validated against the existing bundle now.

### Still open / deferred

- The cut boundary is a native-res staircase, not the game's smooth per-pixel curve; the
  planned boundary-vertex snap (project kept boundary vertices onto the prism side polylines)
  is deferred until acceptance flags it — at 3.5 m, mostly hidden behind the NetPiece walls.
- The 3 residual poke stations (≤0.28 m) are unexplained junction-apron cases; re-measure
  after the re-rip.

## 32. Lane tile instancing: the fence layer was 85% of --lanes (2026-08-08)

`--lanes` full-city measured 185.3M stored vertices (~10.2 GB .blend): fences+hedges 157.3M
(85%), train sleepers 25.5M, markings 1.14M (0.6% — already optimised by §29a; the projector
rework would have chased 0.6% of the problem). Root cause: the harness baked and merged every
tile of every tiled part; the game stamps one shared mesh per tile in its vertex shader, and
on a straight span that stamp is a rigid transform.

Fix (consumer): a rigidity gate in `_bend_lane_part` — a full, un-hung tile whose curve
window deviates < 2 cm from its 3D chord and whose width varies < 1% emits one 4×4 instead of
baked vertices (exact where it applies: the affine is the bend's own closed form, chord shear
included); `build_lanes` assembles per-(prefab, paint, part) point-cloud instancers carrying
`tile_matrix` FLOAT4X4 point attributes through one Geometry Nodes group per shared part
mesh (Instance on Points → Set Instance Transform). `set_paint` lands on the instancer, as it
does on prop empties. Curved/tapered/hung/clipped tiles and every decal bake exactly as
before. Downtown region: 5,543 of 5,623 tiles instanced (98.6%).

Full city, measured: **112,592 of 137,906 tiles instanced (81.6%)** across 6,946 instancers
sharing 36 part meshes; stored lane vertices **185.3M → 43.7M (−76%)**; `--lanes --networks`
.blend **~10.2 GB → 0.97 GB**. The residual 43.5M baked vertices are the tiles that
genuinely bend — train sleepers are a 32 m tile, so almost any rail curvature exceeds the
2 cm chord gate, and bending them is the game's own behaviour (a rigid 32 m tile on a curve
would visibly shear off the rails); a counted, deliberate remainder, not a missed case.
Visual A/B on the downtown region: instanced fences/hedges upright, oriented and on their
lot lines; markings drape unchanged (1.08M decal verts). `FLOAT4X4` point attributes accept
`mathutils.Matrix` per element; object-socket **modifier inputs are not assignable from
Python** on this build — the shared part is baked into each part's node group instead.

## 33. The re-rip round: hole-boundary snap, and the splat channels are opacities (2026-08-08, evening)

The §31 re-rip shipped the game-true clip layer in full — **49,730 strips** (was 1,134): the
`+0.5` floor had been silently discarding every everyday-shell strip at record time, because
`TryGetClipBand`'s gate compared the top against it. With the shell present, the cell-quantised
cut exposed its over-cut rim **along every road** (sawtooth see-through slits beside
carriageways, open gaps ringing portal mouths) — the §31 "deferred" boundary snap stopped
being optional.

- **Hole-boundary snap (built, validated):** `_clip_grid` now also emits a packed
  (strip, station) reference raster for every cut cell plus each strip's evaluated boundary
  polylines; after face deletion, every hole-boundary vertex moves onto the prism surface —
  laterally outside the strip → nearest point of the side polyline with ground resampled
  there (the prism wall); over the strip above the band ceiling → down to `lane + top` (the
  portal-roof iso-edge). Kept faces stretch to the exact clip boundary, the way the game's
  surviving pixels end at it. Full city: 743,109 shell/portal faces cut, **300,400 vertices
  snapped**, terrain phase 38 s; region renders show roads seated cleanly and portal arches
  flush. Two counted, benign residues: neighbouring verts snapped to one curve point
  coincide (21,489 pairs, zero-length edges), and a few slivers collapse to zero area
  (33 full-map; dropped with a calm aggregate line, not the CONTRACT VIOLATION wording).
- **Splat weights are OPACITIES, not a partition** — the reported "user painted colours are
  lost". Measured on the painted city: texels with paint weight ≥ 128 still carry grass
  ≈ 240/255, so the harness's weights/sum normalisation let grass outvote the paint and
  washed every player-painted ground to a faint tint. The game composites: paint channels
  are written over ground that keeps its procedural weights, and the block's own published
  `splatMultiplier` (2.0) / `splatPower` (1.0) are the opacity transfer —
  `saturate(w · 2)^1`, half-weight paint covers fully. `_splat_ground_material` now
  composites base → R → G → B → A in ascending priority (paint channels last, matching
  `ApplyToSplatmap`'s write targets). Painted grounds render again; rock still shows as the
  all-channels-≈0 residual on cliffs.
- **Airport z-fighting at distance is a viewer artifact, not a data defect**: area surfaces
  ride 2 cm above the terrain (the documented lift ladder), and a kilometres-deep Blender
  viewport depth buffer cannot separate 2 cm — in-game the areas are terrain-layer decals
  (§30), so the situation cannot arise, and a game-engine consumer reproduces them as
  decals/material layers, not coplanar geometry. Raising the lift would trade a real
  close-up float for a viewport-only artifact; declined.

## 34. The portal face is a RaiseTopToTerrain piece; terrain conforming implemented (2026-08-08, night)

> **⚠ 2026-08-09, first full-city run: this implementation flattened every retaining wall —
> see §38 (fixed same day; class 1 is a downward clamp, class 2's assignment — this
> section's portal branch — is unchanged).** The section's mechanism reading stands.

Reported: in-game a wall above the tunnel mouth closes the trench; the import shows the
mouth open above the arch. Traced end-to-end: the wing walls and retaining walls arrive
(the tall geometry is in the bundle — `seg_2333057` measures 28.7 m), but the **portal face
panel** does not, because it is authored as a short strip whose top edge the game raises to
the terrain surface at render time — the §31 header's "terrain-conform flags" deviation,
which stopped being benign the moment anyone looked at a portal.

The game's mechanism (`BatchMeshHelpers.InitializeCompositionMesh`, decompiled): a piece
flagged `LowerBottomToTerrain`/`RaiseTopToTerrain` gets per-z-slice height bounds (257
slices over the piece's authored geometry, `InitializeHeightBounds`); every vertex at its
slice's min/max (±0.01) is classed 1/2 in the vertex **colour**, and the vertex shader
moves classed vertices to the terrain height sampled at their world position
(`TerrainUtils.SampleHeight` semantics against the flattened cascade). One short facade
asset thereby closes a trench of any depth, and wall skirts bury their bottom edge into any
ground.

**Validated by re-rip (2026-08-08, late): the portal face ships closed — the mouth gap is
gone.** One counted approximation remains, visible as a slightly different meet line where
the facade top meets the terrain: the game's shader conforms those vertices per frame
against the camera-adaptive cascade (down to 0.875 m/texel) and discards terrain per pixel,
while the rip conforms against the 3.5 m base slice and the harness meshes that same slice
with its own triangulation — between lattice points the two surfaces can disagree by the
diagonal error (p95 ≈ 9 cm, more on steep rock), so the seam sits within roughly a texel of
the game's. Exact per-pixel agreement is not reachable from a 3.5 m raster; a consumer-side
ray-snap of facade top verts onto the built terrain could hide even that, but it would move
producer geometry in the consumer and is deliberately not built.

**Producer change (as landed):** `NetworkRipSystem.GameMesh` now ports
the classification verbatim (per-piece slice bounds, cached; classification on the authored
vertex before flip/offset) and applies the shader's move at rip time — classified vertices'
world `y` is set to the bilinear heightmap sample at their world position, using the same
`GetHeightData` readback the terrain layer ships, captured lazily once per rip and freed
with the run. The old "shipped unconformed — a consumer rule" report line is retired; the
rip now reports how many bottom/top vertices were conformed. Consumers get closed portal
faces and seated wall skirts with no new contract surface — the meshes simply arrive in
their rendered shape, which is also what a BeamNG-class consumer wants.

## 35. "Greyscale terrain": two consumer bugs under one symptom (2026-08-08, late)

Reported: in-game the splat shows green grass and sand beaches; Blender shows grey-brown,
darker at the shore. The rip is faithful — `terrain/surfaces/grass.png` is a real green
grass texture, authored dark like every theme ground — and the defect was two independent
consumer bugs stacking:

1. **Dirt composited over grass.** §33's ascending R→G→B→A priority put the G (dirt)
   channel above grass; measured on the shipped city, dirt is **never** the dominant signal
   (6.1M texels carry dirt > 128; 99.5% of them also carry grass > 200, none carry
   grass < 64), so dirt-over-grass painted the entire land surface brown while the game
   shows green. The dirt weight is an undercoat. Order is now **base → G → R → B → A**
   (dirt under grass, player paint still on top) — probed directly: the grass hill at
   (1348, −462) carries splat (R 255, G 227) and renders green in-game.
2. **The presentation gain had never worked.** `_GROUND_GAIN` (2.8, later 3.4) was applied
   through a MixRGB colour socket whose RGBA `default_value` clamps to [0, 1] — the gain
   multiplied by white, a silent no-op since the day it landed, for the terrain grounds AND
   the §30 area materials alike. It now rides the HSV node's float `Value` input
   (unclamped), recalibrated by A/B renders against the in-game shots to
   `_GROUND_GAIN = 8.0`, `_GROUND_SATURATION = 2.0` — presentation only, pixels on disk
   untouched. Area materials get **no** gain: their textures come from ordinary prefab
   materials at normal albedo brightness, and gain-1 is the look the §30 layer was
   accepted with (the clamp bug means that is also the look it always actually had).

The beach reading also resolves: shoreline texels carry near-zero weights in every channel,
so the base ground (the pebbly beige `rock.png`) shows — under the working gain it reads as
light sand-pebble ground, matching the in-game shore rather than the old dark grey.

One more trap, worth the line because the ladder invites it: `_DECAL_LIFT` looks like a member
of the ladder and is not. It is the lane bend's **pre-drape** offset, and `_drape_block`
centres each marking's ±projector-half-height search window on the height it produces, so
raising it with the rest of the ladder pushed 8 of the crosswalk region's 4,694 decal vertices
out of the top of their own window. Nothing was visibly wrong; only the A/B count gate caught
it (`draped` 4049→4041). Split into `_DECAL_LIFT` (lane pre-drape, 15 mm, unchanged) and
`_INSTANCE_DECAL_LIFT` (§4.2a placement, 30 mm), after which the lane layer is pixel- and
count-identical again.

### Validated against a rip (2026-08-08, same day)

Terrain re-ripped; only the terrain button was needed, since both changed methods hang off
`RipTerrain`. Results:

- **`uvScale` earned its place.** The authored values are nothing like the constant they
  replaced: 0.2 on 11 of 20 prefabs, but also 0.3, 0.175, 0.1, 0.0715, 0.06, 0.03, **0.01**
  (one repeat per 100 m) and 1.0. `Forestry_Surface_01` is 0.06 — one repeat per 16.7 m where
  the harness drew one per 4 m. Confirmed applied by probing UV rates in the built scene.
  `heightOffset` came out 1.5 m on all 20.
- **Nothing was lost to the widened catalogue guard**: 41 prefabs before and after, all 19
  colours and all 19 textures intact, with the new keys added.
- **`heightRanges`** on 91.4% of areas, 132,502 triangles with a non-zero band. Against an
  independent Python recomputation of `GetHeightRange`: the game's band contains the
  recomputed one on 83.3% with ~11 mm median slack in the widening direction (its scanline is
  conservative; a building-owned area also widens by `heightOffset`), and 0.52% are *narrower*
  than a direct heightmap walk — the field can lag a terrain edit, since the game recomputes
  it only when the area changes. Written into §8b so no consumer treats it as authoritative
  ground.

Two process notes from the same pass, both mine to fix:

1. The baseline snapshot **hashed** `terrain/` instead of copying it, so when three rasters
   came back different (`heightmap`, `splatmap`, `water_level`) there was nothing to diff and
   the magnitude was unanswerable. The cause was benign — every area entity id changed between
   the two rips, i.e. the save had been reloaded into a different sim state — but that was
   inferred, not measured. Copy the small rasters next time.
2. An A/B set up to show `uvScale`'s effect was malformed: the authored value takes precedence
   over the fallback, so toggling the fallback changed nothing and both renders were identical.
   The real check is reading the UV rate back off the built mesh, which is what settled it.

The harness change is verified against the bundle — the suburb, parking-lot and forestry
regions reconstruct with continuous pavement and their bay markings on top of it, and the
lane, network and terrain layers are unchanged (`ab_compare.py`: `PASS 18 count line(s)
identical`, `PASS frac=1.00000 max=0`).

## 36. Grey fields: the OverrideMaterial branch and unresolved asset packs (2026-08-08, night)

Reported: areas the map author painted as crop fields render as grey boxes in Blender. The
2026-08-08 19:21 rip's `areaprefabs.json` splits cleanly in two: every vanilla surface
(`Grass_Surface_01`, `Pavement_Surface_02`, …) carries `uvScale`+`color`+`texture`; every
asset-pack surface with a long composite name (`Farming Surfaces & Decals Ground Haristess -
2-2 - StrawFlat1 Overlay1 Surface`, the G87 concretes, the gaga terrain surfaces, the water
mirrors — 18 prefabs) carries only `name`. No `uvScale` at all means the producer never even
reached `RenderedArea` — the failure is *prefab resolution*, not material reading.

### The game's mechanism (decompiled, `AreaBatchSystem` batch build + `PrefabSystem`)

| # | Game rule | Producer (before) | Verdict |
|---|---|---|---|
| 1 | `RenderedArea` has two versions. `Version.Original`: a baked `m_Material` carries everything. `Version.OverrideMaterial` (`m_Material == null` — every editor-authored surface, which is every asset-pack surface): the game clones the global `AreasConfigurationPrefab.m_RenderedAreaMaterial` template and pushes the authored fields onto the clone — `_BaseColor := m_BaseColor`, `_BaseColorMap := m_BaseColorMap` (an `AssetReference<TextureAsset>`, loaded on demand), plus normal/mask maps, metallic, smoothness, UVScale, edge params. | Read only `m_Material`; bailed when null. Any OverrideMaterial surface came out with `uvScale` but no colour and no texture. | **Divergence — fixed.** `CatalogueAreaPrefab` now ports the branch: `color` from `m_BaseColor`, `texture` from `m_BaseColorMap` via the house `Load → Capture → Unload` (`MaterialRipSystem.ReadBack`, now internal). A null reference is game-true "no texture at all", **not** a template fallback — `Initialize` explicitly nulls the sampler. |
| 2 | `PrefabData.m_Index < 0` marks an *obsolete* prefab: the save references an asset (by `PrefabID` = type+name+hash) that is not loaded this session. `TryGetPrefab` returns false; `GetPrefabName` falls back to `GetObsoleteID().GetName()` — the long composite names are this fallback's signature. `AreaBatchSystem.TryGetRenderedAreaData` then falls back to `MeshSettingsData.m_MissingRenderedArea`: **the game itself drew those areas grey in the rip session.** | Catalogued silently with only `name`; nothing in the report. | **Approximated → counted.** Entry now gets `"unresolved": true` (schema §8b, additive) and the rip report prints the count with the cure ("enable the packs and re-rip"). Nothing better is possible producer-side: there is no material to read. Same root cause as the `unresolved_*` instance ids in the same rip (see `cs2-instance-enumeration` memory — unowned/unloaded asset packs). |
| 3 | Missing `m_BaseColorMap` with a colour still authored (colour-only surfaces, and the vanilla "Missing Area" prefab itself). | n/a (never reached). | **Match after fix**: colour alone is emitted; the harness's `texture → color → neutral` ladder (§8b) already renders it. |

Deliberately not carried (flagged, not built): OverrideMaterial's `m_NormalMap`/`m_MaskMap`,
metallic/smoothness scalars, and the edge fade/noise params. The baked-material path doesn't
carry them either; carrying them on one branch only would be asymmetric, and no consumer reads
them yet. Candidates for a TEX-follow-up if area relief ever matters.

Harness: `build_areas` now names each grey prefab and the reason — `unresolved` (pack missing
at rip time; the game showed grey too) vs genuinely colourless — instead of leaving grey boxes
as a riddle.

**Status: built, awaiting in-game re-rip.** The fields will only gain colour once the packs
resolve in the rip session (Options → Mods: the packs must be enabled for the playset; a
version/hash mismatch also lands in rule 2). The 2026-08-08 19:21 bundle *correctly* records
that session — the game was drawing those fields grey then, whatever the map author's
screenshots show. Related, same rip: the 15 `empty SubMesh buffer` warnings (parking lots,
parks, courts) are the documented meshless-building pattern (§8b — their pavement IS the area
layer, their markings are §4.2a decals), benign; the `unresolved_*` mesh warnings are rule 2
wearing its instance-layer hat.

### §36 validated against the re-rip (2026-08-08 22:18), plus one more finding

The OverrideMaterial branch works: all 17 previously-grey pack surfaces (11 Farming/Haristess,
4 G87 Concrete, 2 gaga TTP) resolved this session — the packs were installed all along; the
19:21 session simply hadn't loaded them — and every one ripped `color` + `texture` + its
authored `uvScale` (gaga TTP: 0.05, a value the fallback would have gotten 4× wrong).
`Missing_Area` now carries its authored mid-grey `[0.502, 0.502, 0.502]`, confirming the
branch against the game's own missing-area look. Still unresolved, correctly: the two
`[G87] Water Mirror` surfaces (pack genuinely not installed; 24 areas, 239 triangles) — their
new `prefabId` hashes differ *per asset*, so the identity behind `Hash128.Calculate` is
per-asset (EAI runtime assets), not the pack's mod ID: hash-matching a candidate mod ID
cannot convict the pack. The pack title + `G87` asset prefix remain the identification.

**New finding: `Clip_Surface` was double-shipped and painted.** It resolved fine but has no
`RenderedArea` — the harness's "did not resolve" message was a legacy-sniff misdiagnosis
(`uvScale` absent ⇒ unresolved held only for the pre-§36 producer; sniff removed per the
no-legacy-fallbacks rule). Renderer-end proof per rule 4c: only
`RenderedArea.GetArchetypeComponents` puts `Game.Areas.Batch` on an area entity, and every
`AreaBatchSystem` draw/cull query requires `Batch` (decomp lines 1523/1533) — the game
*structurally* never paints an area whose prefab lacks `RenderedArea`. Counted against the
22:18 bundle: exactly 266 areas, all referencing `Clip_Surface`, all 266 already present in
`clipvolumes.jsonl` (§8c) — the paint copy drew grey boxes over tunnel portals that the game
does not draw. The producer now skips them (counted in the rip report as "unpainted area(s)
excluded"); unresolved-prefab areas are deliberately NOT skipped — the game does draw those
(grey, Missing Area), and dropping them would hide the packs-missing signal.

**Status: built, awaiting re-rip (terrain button only) for the clip exclusion; the colour
path is validated and done.**

## 37. The notification toast stack is a public, mod-usable progress API (2026-08-09)

Phase 8 §5 asked whether the game's top-left toast stack can carry the rip's progress bar, to
replace the reflection hack on `AppBindings.m_BackgroundProcessMessageBinding`. Decompiled
`Game.UI.Menu.NotificationUISystem` and its facade `Game.PSI.NotificationSystem`. **It holds,
with room to spare** — this is the API the base game uses for mod download/install progress,
and it is fully public:

| Fact (decompiled) | Consequence for the ripper |
|---|---|
| `Game.PSI.NotificationSystem.Push(identifier, title, text, titleId, textId, thumbnail, progressState, progress, onClicked)` — **public static**, upserts by `identifier` into `NotificationUISystem.AddOrUpdateNotification` (also public). `Pop(identifier, delay, …)` removes, optionally after a delay with a final state; `Exist(identifier)` queries. | No reflection anywhere. One stable id (`"Carto.RipWorld"`) is pushed repeatedly; the stack shows a single live toast. |
| `NotificationInfo` carries `progressState` (`Colossal.PSI.Common.ProgressState`: `None, Progressing, Indeterminate, Complete, Failed, Cancelled, Warning`) and `progress` (int, the game always feeds 0–100). | Real progress bar states: `Progressing` + percent during stages, `Indeterminate` for unsized phases (terrain), `Complete`/`Failed` on `Pop` with a 2 s linger, matching the game's own `kDelay = 2f` idiom. |
| `title`/`text` are `Game.UI.Localization.LocalizedString`, and `LocalizedString.Value(string)` renders **free text** (id = null); `LocalizedString.Id(key)` renders a locale key. | The percent no longer needs to be baked into pre-expanded locale keys — that hack existed only because `app.backgroundProcessMessage` renders a bare key. Title stays a locale key (translated); the stage/percent line is free text. |
| `AddOrUpdateNotification` mutates plain `Dictionary`s and a `List` behind the binding, sets `m_Dirty`; `OnUpdate` flushes via `TriggerUpdate()`. Nothing is synchronised. | **Main-thread only.** The sink is `RipProgress.Pump()` — already called once per frame on the main thread — so off-thread reporters keep writing fields and never touch the toast directly. |
| `UpdateNotification` only overwrites `title`/`thumbnail` when currently unset (`text`, `progressState`, `progress` update freely). | Set the title once on the first `Push`; per-frame pushes update text + progress only. A title *change* (new stage) requires text, not title. |
| `NotificationUISystem` is a `UISystemBase` created unconditionally (`OnCreate` binds the PSI facade; the mod-download handlers run in-game too). | The toast stack works during gameplay, not just in the main menu — consistent with major mods driving live progress bars through it. |

Decision: **`NotificationSystem.Push`/`Pop` is the progress sink for the one-button rip.** The
background-process caption (`RipProgress` reflection path) stays as the fallback if `Push`
ever throws, and `RunDeferred`'s show-caption-then-block contract is unchanged — the toast
cannot paint during a synchronous stage either; nothing paints mid-freeze.

## 38. The §34 conforming port flattened retaining walls — the class-1 move is a clamp, not an assignment (2026-08-09)

**Status: offline acceptance PASSED on the 2026-08-09 11:53 network re-rip; visual A/B
pending.** Diagnosed 2026-08-09 (acceptance run), re-diagnosed from scratch in a fresh session
per the user's decision, fixed and re-ripped the same day. Nothing here is a §34-mechanism
retraction — the game reading stands; the landed port applied the wrong *move* to class-1
vertices.

Re-rip results (segment ids are per-session — the old `seg_50940` re-ripped as
`seg_1349140`, identified by its exact anatomy: 360 verts, 192+128 tris):

- `seg_1349140` ("Highway Oneway - 2 lanes", sunken): wall prim drawn y-span **[−5.10,
  +5.10]** (was flat +5.10), per-column spread **10.20 m** (the authored 10.2 m wall), tri
  counts unchanged, **0 orphans** on the union of primitives' indices. All 12 sampled sunken
  road/highway segments show standing walls (column spread 10–30 m); none flat.
- Portal guard: `TunnelTopEnding` still stretches to y = **63.47** (§34's ~63.5 case) —
  class-2 output preserved.
- Probe report: **out-of-window drops = 0** across every piece; the flagged table lists the
  LoweredSide/TunnelSide/TunnelTop families, with the two-rail walls marked and showing the
  clamp signature (e.g. `Lowered Side Piece 1`: 6352 bottom verts kept at road level vs 20
  buried — under assignment all 6372 would have moved).
- Benign residual: 20–40 unreferenced vertices on some segments — cut-boundary verts whose
  triangles were front-cut (pre-existing remap behaviour, dead weight only, not the §38
  block signature).

**Symptom** (screenshots `E:\carto-out\shots\`, same cutting in-game vs Blender): the concrete
retaining walls of sunken roads and tunnel approaches are textured and solid in-game, but flat
gray "missing texture" sheets in the import — see-through cuttings, a distant wall piece with
a spiked top edge.

**What it is NOT**, each eliminated against the acceptance bundle (2026-08-09, ~03:30 rip +
04:25 terrain re-rip):

- Not §36/clip volumes: `clipvolumes.jsonl` is healthy — 14,578 edge + 34,886 node strips,
  266 area records all carrying inline polygons (schema 2).
- Not materials: the wall materials (`RoadParts_fd2f…`, `TunnelTopEnding_2c77…`) fully resolve
  to painted textures; all 9,351 network primitives carry material + TEXCOORD_0. The gray
  look is collapsed geometry, not a failed texture join.
- Not dropped triangles: a `seg_50940.glb` re-scan found **zero** triangles missing. The
  first diagnosis's "144 orphaned vertices" was a shared-vertex-list artifact — that block is
  referenced by prim1 (the Road draw, the trench floor), not prim0. Orphan scans must run on
  the UNION of all primitives' indices.
- Not a cross-window/remap defect: falsified offline — 0 of 10,414 `.Geometry` assets in the
  game DB have multi-submesh meshes; material ranges are self-contained. (The `ia >=
  vertexCount` triangle rejection stays as a data-contract guard, now counted in the report.)

**The corrected anatomy** (`networks/seg_50940.glb`, "Highway Oneway - 2 lanes", elevation
−7.5…−10): the wall quads exist and are drawn, but are **degenerate** — all 216 wall vertices
sit at exactly +5.10, the sampled terrain lip. `RoadLoweredSide1` is authored as a **two-rail
10.2 m wall**: a bottom vertex row at −0.2 and a top row at +10.0, nothing between. Its piece
flags default `m_LowerBottomToTerrain = true` and set `m_RaiseTopToTerrain`, so **100 % of its
vertices classify** — bottom rail class 1, top rail class 2 (classification port verified
line-identical to decompiled `BatchMeshHelpers`; the earlier "strip thinner than the ±0.01
tolerance" hypothesis is moot — full classification is by two-rail *authoring*, not tolerance).
The port assigned `world.y = SampleConformHeight(world)` to BOTH classes, so both rails landed
on the same terrain lip: a zero-height wall wearing a stretched texture — the flat gray sheet.

**The fix — class-1 is a downward clamp.** In-game the same classified vertices render as a
standing wall, so the game's shader cannot be assigning terrain height to class 1 — proof
without RenderDoc. The semantics that fit every observed case (deep trench, shallow cutting,
`seg_698585`'s +60 m portal stretch, embankment skirts):

- **class 1 (`LowerBottomToTerrain`): `world.y = min(world.y, terrain)`** — bury the bottom
  edge where the terrain is below it, never lift it; a trench wall's base stays at road level.
- **class 2 (`RaiseTopToTerrain`): assignment, unchanged** — the §34-validated portal-stretch
  branch. Unchanged ⇒ portal output bit-identical ⇒ §34's win survives by construction.

Fix site: the single conform move in `NetworkRipSystem.GameMesh.cs` (`RipNetworks`' vertex
loop). Explicitly NOT changed: classification, `GetPieceHeightBounds`, remap/vertex-add/
triangle emission, primitive indexing, UVs (game-true V density; texture-stretch judgment
deferred per prefer-simple-over-speculative — resolved as §39, a lane-decal consumer defect
unrelated to this network-layer port). The 3.5 m bilinear lip spike stays a known residual.

**Probe added** (rip report): per flagged piece — prefab name, flags, `m_Size`, vertex total,
class-1/class-2 counts + share (two-rail pieces flagged at 100 %), buried/kept/raised counts,
dropped-tri counts by reason (front cut / back cut / out-of-window, the last expected 0).

**Validation once re-ripped:** Network-layer re-rip only (Ripper tab → Network master alone),
then offline:

- `seg_50940.glb`: prim0 drawn y-span ≈ [−5.3, +5.1] (was flat +5.10); per-column spread
  ≈ 10 m; tri counts unchanged (192+128); orphan scan on the union of primitives' indices.
- `seg_698585.glb`: prim2 (TunnelTopEnding) still spans to ~63.5 — §34 regression guard,
  expected bit-identical.
- Probe report: out-of-window drops = 0; the flagged-piece table lists the
  RoadLoweredSide1/TunnelSide/TunnelTopEnding families.
- Blender vs in-game on the user's screenshot cutting. If wall tops sit below the game's,
  revisit class-2 assignment-vs-max — that A/B screenshot is the only remaining discriminator.

## 39. Lane-marking decals: the projector stretches, the texture repeats (2026-08-09)

**Symptom** (user report, deferred from §38's session): dashed lane lines, crosswalks and
stop lines render stretched in Blender but not in-game. Measured on the acceptance bundle:
**15,704 marking instances** (decal parts with `tilingCount > 0`) were mis-mapped — median
stretch **5.0×** on `NA_Car_Lane_Line`/`NA_Highway_Lane_Line` (max 18.5× on
`EU_Car_Lane_Line`), and the inverse on short patterned parts: yield lines at **0.22×** (all
nine shark teeth crammed into ~3 m), alley crosswalks at 0.5×. Solid lines (`tilingCount`
0) hid the defect — stretching a uniform stripe is invisible.

**Mechanism (decompiled `BatchDataHelpers.BuildCurveMatrix`):** the projector *box* is
stretched chord-to-chord (`BuildTransformMatrix`'s isDecal fit), but the box scale is not
the texture scale. `BuildCurveMatrix` multiplies the per-instance arc table (the
`CurveMatrix` w-channel) by `num = curve.m_Length / max(1f, size.z)` — the lane's length in
mesh-lengths — quantised `round(num·tilingCount)/tilingCount` when `m_TilingCount ≠ 0`, so
the shader's along-curve coordinate runs 0→N and the `m_TextureArea` cell repeats N times.
`m_TilingCount` is "pattern periods per mesh length" (verified in the shipped texture:
`White_Dashed_Line_Mesh`, T=2, carries exactly 2 dash cycles per 12 m V span), and its two
jobs are quantising N to whole periods and gating the draw. There is **no isDecal branch in
the tile-count path**: `GetTileCount` runs for decals exactly as for tiled geometry, and
with `T ≠ 0 ∧ ¬geometryTiling` it returns `min(1, ceil(clip/T))` — **zero instances** when
`clip = round(L/S·T) == 0` (a sub-half-period stub, < 3 m for a standard dash line). Every
decal part in the shipped catalogue has `geometryTiling == false`, so a marking is always
one instance whose pattern lives in the texture.

| # | Game rule (decompiled source) | Consumer (reference importer) | Verdict |
|---|---|---|---|
| 1 | `BuildCurveMatrix`: along-curve texture parameter scaled by `N = L / max(1, meshLength)`, quantised `clip/T` when `T ≠ 0` — texture repeats N times over the stretched box | Forced `tiles, clip, tiling_count = 1, 0, 0` for every decal and lerped the quad's corner UVs once 0→1 over the whole span ("a decal never tiles" — true of geometry, false of texture) | **divergence — fixed** (`_decal_station_rows` bakes `frac(n)` per lattice row, seam-split with duplicated stations at every integer of n; `tools/test_decal_rows.py` pins the invariants) |
| 2 | `GetTileCount` for decals: `clip == 0` ⇒ 0 instances, nothing drawn | Zeroing `tiling_count` made the clip gate unreachable — markings drawn on every stub | **divergence — fixed** (the existing `_lane_tile_count` guard, now fed the real T; drops counted on the import summary) |
| 3 | `BuildTransformMatrix` isDecal: box padded by `m_SmoothingDistance` at both ends, `_SmoothingDistance` end-fade bound per lane | Not shipped; quad runs the unpadded span, no end fade | **approximated** (counted here; producer unchanged) |
| 4 | `curve.m_Length` is the stored arc length | Re-derived from a 32-sample arc table (sub-0.1 % error → same clip in practice) | **approximated** (counted here; producer unchanged) |

**Fix (harness + contract only, no producer change, no re-rip):** `laneprefabs.json`
already ships `tilingCount`, `geometryTiling` and `boundsMin/Max` — everything the rule
needs. `blender_import_bundle.py` now computes `tiles/clip` for decals via
`_lane_tile_count` on the span's own arc (geometry stays the single chord-to-chord box),
derives `N = clip/T` (or `L/max(1,S)` when T=0, wear tint included — game-true, visually
neutral for uniform textures), and bakes the repeat into the lattice V with duplicated
rows at each period boundary so no triangle spans the wrap seam (a REPEAT sampler cannot
substitute — the UVs address an atlas rect). Contract §5B carries the normative rule;
schema (v2), keys and data unchanged.

**Blind-spot note:** §26's delta matrix audited `UpdateLaneInstances` and `BuildCurveScale`
but had **no `BuildCurveMatrix` row** — the along-curve texture scale was never audited,
and §19's "fixed" verdict was scoped to *which atlas rect* the quad carries, not *how many
times* the lane traverses it. Both sections now carry amendment pointers. The lesson
repeats §22a's: an appearance mismatch against an in-game screenshot is a finding even when
every implemented rule matches.
