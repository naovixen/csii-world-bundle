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
| 2 | Node: matrices 0/2 and 1/3 = the two across-width halves; sync remap `m_CompositionSync0..3` | Same, incl. piecewise-linear sync | **match** |
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
land in `meshes/` and `lanemeshes/`.
