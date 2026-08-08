# The CS2 World Bundle — contract v1

**Status: frozen.** `schema: 1` is the seam between the Carto-Ripper mod (the sole producer)
and every consumer. This document plus `docs/schema/*.schema.json` is the *complete*
specification: a consumer can be written against it with no access to the game and no access
to the producer's source. If this document and the mod ever disagree, this document is right
and the mod has a bug.

See [Versioning](#12-versioning-and-compatibility) before changing anything here.

> **Scope and boundary.** A bundle contains geometry and textures extracted from the
> producer's own copy of Cities: Skylines II, for interoperability with software they own.
> Bundles are local artefacts. Do not publish, upload or redistribute them, and never commit
> one to a source repository.

---

## 1. Layout

A bundle is a directory. `bundle.json` at its root is the only file whose name is fixed;
everything else is found through the manifest's `layers` index.

```
<bundle root>/
  bundle.json                     # manifest — frame, geo-referencing, counts, layer index
  instances.jsonl                 # one line per placed object
  networks.jsonl                  # one line per network segment / node
  lanes.jsonl                     # one line per lane — the drivable graph
  lanemeshes.jsonl                # one line per lane that draws geometry (§5B)
  laneprefabs.json                # lane prefab catalogue: parts + bend inputs (§5B)
  materials.json                  # material table, keyed by materialId
  colors.json                     # per-mesh colour palettes, keyed by sub-mesh assetId
  lights.json                     # per-mesh light emitters, keyed by sub-mesh assetId
  meshes/<assetId>.glb            # unique prefab meshes, deduped by prefab
  networks/<assetId>.glb          # generated per-segment network meshes
  lanemeshes/<assetId>.glb        # straight lane part meshes, stored once each (§5B)
  textures/<sha256>.png           # textures, deduped by content hash
  terrain/elevation.tif           # elevation raster
  terrain/world_elevation.tif     # optional world-elevation surround
  terrain/surface_index.png       # per-cell ground-material id
  terrain/cutout.png              # per-cell terrain cut mask (tunnel portals, trenches)
  terrain/surfaces/index.json     # id -> ground texture map
  terrain/surfaces/*.png          # ground textures
  water/depth.tif                 # water depth raster
  vector/                         # unchanged vanilla Carto GeoJSON / Shapefile / GeoTIFF
  warnings.jsonl                  # advisory: what the producer skipped and why
```

Those paths are the producer's defaults, **not** part of the contract. A consumer must read
paths from `bundle.json`'s `layers` object and never hard-code them. Presence of a key in
`layers` means the layer exists; absence means it was not exported, and the consumer degrades
gracefully rather than failing.

Path rules for every value in `layers` and every path inside `materials.json`:

- POSIX separators, relative to the bundle root, no leading `/`, no `..` segment.
- A value ending in `/` is a directory; anything else is a file.

`.jsonl` files are **JSON Lines**: one complete JSON object per line, LF-terminated, no
enclosing array, no blank lines, no trailing commas. This is what makes a 200,000-tree city
streamable on both sides.

A bulk layer may additionally be **stored gzip-compressed**, in which case its name says so
(`instances.jsonl.gz`, `meshes/<assetId>.glb.gz`) and its contents are unchanged once
decompressed. Both forms are valid; see §12.1 for the three rules a consumer needs.

---

## 2. The coordinate frame

This is the single most important section. Read it before writing any placement code.

### 2.1 The bundle frame

Every position in the bundle — instance `pos`, network `centerline`, `bounds`, raster
geo-referencing — is in **CS2 world space**, verbatim, in metres:

| | |
|---|---|
| Handedness | **left-handed** |
| Up axis | **+Y** |
| Axes | **X = east, Y = up, Z = north** |
| Units | metres |
| Rotations | unit quaternions `[x, y, z, w]`, left-handed about their axis |

The manifest states this literally in `frame`, so a consumer converts to its own convention
deterministically rather than by experiment. Values are *not* pre-converted, so a position in
the bundle equals the position you read off the game's own debug UI. That property is what
makes spot-checking possible.

### 2.2 The mesh frame — the one place values are converted

glTF 2.0 mandates a **right-handed** frame, so mesh payloads cannot be stored verbatim. Every
`.glb` in the bundle is written with:

- every vertex **position, normal and tangent mirrored on X** (`x → -x`), and
- **triangle winding reversed**, so glTF front faces remain counter-clockwise.

The manifest declares this in `frame.meshFrame` (`conversion: "negate-x"`,
`windingReversed: true`). The consequence, and the only formula a consumer needs:

```
v_world = T(instance.pos) · R(instance.rot) · S(instance.scale) · diag(-1, 1, 1) · v_glb
```

where `diag(-1, 1, 1)` is the mirror that undoes the conversion. Nothing else in the bundle is
mirrored. The payoff is that a `.glb` also opens correctly, right-way-round, in Blender or any
glTF viewer on its own — which is exactly how mesh extraction is acceptance-tested.

### 2.3 Geo-referencing

`geo` anchors the bundle frame to the earth:

- `geo.originWorld` — the bundle-frame point (normally `[0,0,0]`) that the anchor describes.
- `geo.originLatLon` — `[latitude, longitude]` in WGS84 of that point.
- `geo.originProjected` — `[easting, northing]` of that point in `geo.crs`.
- `geo.crs` — the target CRS, e.g. `"EPSG:32651"`, `"EPSG:4326"`, or `"custom-TM"`.
- `geo.tmParams` — the full Transverse-Mercator parameter set (ellipsoid, origin, false
  easting/northing, scale factor, optional 7-parameter Helmert `toWgs84`), present whenever
  `projection` is `TransverseMercator` or `UTM`. Enough to reproduce the projection without
  the producer's PROJ port.
- `geo.north` — degrees to rotate the bundle's `+Z` counter-clockwise about `+Y` to reach
  geographic north. `0` means `+Z` is already north.

Rasters use the same frame, so the heightmap and the meshes line up without further work.

---

## 3. `bundle.json`

Schema: [`docs/schema/bundle.schema.json`](schema/bundle.schema.json). The manifest is
**closed** — unknown top-level keys are an error, so that a v1 consumer never silently ignores
something meaningful.

```jsonc
{
  "schema": 1,
  "generator": "carto-ripper 1.0.16",
  "generatedAt": "2026-01-01T00:00:00Z",
  "game": { "name": "Cities: Skylines II", "version": "1.5.7f1", "mode": "Game" },
  "city": "Example City",
  "map": "Example Map",

  "frame": {
    "units": "m", "handedness": "left", "up": "Y", "axes": "X=east,Y=up,Z=north",
    "meshFrame": { "handedness": "right", "conversion": "negate-x", "windingReversed": true }
  },

  "geo": {
    "crs": "EPSG:32651", "projection": "UTM",
    "tmParams": { "ellipsoid": "WGS84", "semiMajor": 6378137.0, "inverseFlattening": 298.257223563,
                  "originLon": 123.0, "originLat": 0.0, "falseEasting": 500000.0,
                  "falseNorthing": 0.0, "scaleFactor": 0.9996, "utmZone": 51, "hemisphere": "N",
                  "toWgs84": [0,0,0,0,0,0,0] },
    "originLatLon": [25.033, 121.5654],
    "originProjected": [353128.0, 2770346.0],
    "originWorld": [0, 0, 0],
    "north": 0.0
  },

  "bounds": { "min": [x,y,z], "max": [x,y,z] },
  "mapSize": 14336.0,

  "counts": { "meshes": 1, "materials": 1, "textures": 1,
              "instances": 2, "networkSegments": 1, "warnings": 0 },

  "layers": { "instances": "instances.jsonl", "networks": "networks.jsonl",
              "materials": "materials.json", "meshes": "meshes/", "textures": "textures/" },

  "rasters": { "resolution": 4.0, "size": [4096, 4096],
               "originWorld": [-8192, 0, 8192], "elevationUnits": "m", "noData": -9999 },

  "ripOptions": { "meshSource": "runtime", "lodCap": 0, "textureSizeCap": 2048,
                  "includeSubObjects": true, "includeVector": false }
}
```

Notes on individual fields:

- **`counts`** must equal reality. A mismatch is how a consumer detects a bundle truncated by
  a crash mid-export, so it is a hard validation error, not a warning.
- **`bounds`** encloses all exported content in the bundle frame, mesh extents included.
- **`ripOptions.meshSource`** records how Phase 1's feasibility question actually resolved for
  this export: `runtime` (read from the live game), `offline` (joined from an offline
  `.assets` extraction), `mixed`, or `stub` (placeholder geometry substituted). Instance
  transforms are authoritative in every case, including `stub` — placement never depends on
  mesh readback.
- **`rasters`** is required if any raster layer is declared. `originWorld` is the bundle-frame
  position of the **top-left cell centre**; row index increases toward `-Z` (south), column
  index toward `+X` (east).
- **`game`** is diagnostic. A consumer must not branch on it.

---

## 4. `instances.jsonl`

Schema: [`docs/schema/instance.schema.json`](schema/instance.schema.json) (validates one
line). One line per placed object.

```jsonc
{ "id": 1001, "assetId": "carto_example_cube", "kind": "building",
  "pos": [120.0, 12.5, -340.0], "rot": [0, 0, 0, 1], "scale": [1, 1, 1],
  "attrs": { "name": "…", "category": "Residential", "zone": "…", "theme": "…",
             "level": 3, "elevation": 12.5, "height": 4.0, "state": "normal" } }
```

| Field | Required | Meaning |
|---|---|---|
| `assetId` | yes | Sanitised prefab name. Resolves to `<layers.meshes><assetId>.glb`. Pattern `^[A-Za-z0-9][A-Za-z0-9._-]*$`. |
| `kind` | yes | One of `building`, `prop`, `tree`, `fence`, `decal`, `poi`. Closed set in v1. `decal` has collision implications — see §4.2a. |
| `pos` | yes | World position, bundle frame, metres. |
| `rot` | yes | Unit quaternion `[x,y,z,w]`, bundle frame. Validators check `|q| = 1` to 1e-3. |
| `scale` | no | Per-axis scale; absent means `[1,1,1]`. |
| `id` | no | Producer-local unique integer (the ECS entity index). Present so `parent` can point at it. |
| `parent` | no | The `id` of the owning instance, when this record came from an owner's `SubObject` buffer. |
| `attrs` | no | Semantic attributes; see below. |

### 4.1 Sub-objects and multi-part buildings

Street trees on a road, landscaping on a lot, and a building's wings/extensions/upgrades are
separate ECS entities with their own transforms. They are emitted as **their own instance
records**, with `parent` pointing at the owner, and with `pos`/`rot` **already baked to world
space**. A consumer can therefore ignore `parent` entirely and still place everything
correctly; `parent` exists only so that a consumer *can* reassemble a building and its parts
into one logical object if it wants to.

This is deliberate: baking per-part transforms into a shared `.glb` would destroy per-instance
variation (a wing that is absent, a prop rotated differently on two otherwise identical lots)
and break the 1:1 building-count requirement.

Fixed sub-*meshes* of a single rigid prefab are the opposite case — they have no per-instance
variation, so they live inside that prefab's one `.glb` as a node hierarchy (§6).

**Stacked objects are expanded into one record per section.** CS2 does not store a tall pillar, a
high wall or a long fence. It stores a foot, one repeating section and a cap, and repeats the
middle at draw time to span whatever that instance needs — a viaduct pillar's shaft is authored
two metres tall. A bundle cannot carry that as one mesh, because the repeat count is a property of
the *instance*, so the producer emits a record per section, each with its world transform baked,
exactly as it does for sub-objects above. Those records reference `<assetId>__stack_first`,
`__stack_mid` and `__stack_last`, which are ordinary meshes under `layers.meshes`; a prefab whose
sub-meshes include parts with *no* stack role additionally ships them as `<assetId>__stack_rest`,
referenced once per stacked instance — the game draws such parts untiled at the instance
transform. The undivided `<assetId>` is also present for completeness, though every placed
instance of a stackable prefab carries a stack in CS2 (the `Stack` component is part of the
archetype), so instance records normally reference only the section files. A section carries
`scale` only where the game stretches it to make the sections meet exactly. A consumer needs to
do nothing special: these are ordinary instance records naming ordinary meshes.

**Foundation skirts: `<assetId>__base.glb`** *(producer addition, 2026-08-06)*. CS2 draws one
extra generated batch under most buildings: the *foundation skirt*, a band of wall that fills the
gap between the building's flat floor and sloping terrain. The game generates it from the
building's own silhouette at its base level (`BatchMeshHelpers.GenerateObjectMesh`) and stretches
its lower edge down to the terrain in the vertex shader; its material comes from the render
prefab's `BaseProperties.m_BaseType`, or a game-wide default when the prefab declares none. The
producer bakes that same silhouette band and ships it as `<assetId>__base.glb` beside the
prefab's own file — separate, because it obeys a draw rule of its own:

- The band's top ring sits at the prefab's base level (UV `v = 1`); the bottom ring is authored
  **1 m** below it (UV `v = 0`), and `u` is the perimeter distance in metres.
- A consumer places the file at each instance's transform and **moves every `v = 0` vertex down
  to the terrain surface at its world X/Z** (never up above the top ring), exactly as the game's
  shader does. Left as authored, the band is simply a 1 m plinth — acceptable, but slopes will
  show gaps.
- Skip the skirt for instances not resting on the ground: any instance whose
  `attrs.elevationFlags` is present and is not just `OnGround` (for a building extension —
  a record with `parent` — read the parent's flags). This mirrors the game's
  `colossal_BaseState` switch.

No instance record references `__base` explicitly; its presence beside `<assetId>.glb` is the
signal. Buildings get a skirt even when their prefab declares no `BaseProperties` — the game
grants `MeshFlags.Base` to every non-decal building mesh — so expect one for nearly every
building prefab whose walls touch its base level.

### 4.2 `attrs`

`attrs` is deliberately open. A consumer **must** ignore keys it does not recognise and
**must not** require any key to be present — availability varies by object type, by game
version, and by which optional properties the user enabled. Documented keys:

`name`, `category`, `zone`, `zoning`, `theme`, `brand`, `level`, `elevation`, `elevationFlags`,
`height` (true height in m from the mesh bounds), `storey`, `state`
(`normal|construction|abandoned|condemned|destroyed`), `growth` (tree stage, 0..1), `treeState`,
`growthScale` (the uniform scale the game draws a growing tree at — see §7.2), `colorSet`,
`colorCustom` (§7.2), `season`, `address` (`[district, street, number]`), `decalLayers`,
`decalHeight` (§4.2a).

`height` comes from mesh bounds and is authoritative. `storey` and `level` are game metadata
and must never be used to derive geometry.

**`elevation` is how far the object stands _above_ the ground, not the ground's own height.**
A consumer that reads it as terrain height gets a city where everything sits at a couple of
metres. Ground level under an instance is `pos[1] − elevation`.

It is **signed**: negative means buried. Measured over one full city, of 517,398 records
carrying the key, 436,329 are positive, 51,459 are exactly zero and 29,610 are negative, down
to −86.3 m — the buried utility hookups under every lot, and the underground parts of transit
stations. The component is absent altogether for objects sitting flat on the ground.

`elevationFlags` carries CS2's own `ElevationFlags` names alongside it — `Stacked`, `OnGround`,
`Lowered`, `OnAttachedParent` — which say *why* an object is off the ground where the magnitude
says only by how much.

### 4.2a Decals — read this before generating collision

`kind: "decal"` is a road marking, a parking bay, a patch of grime: something the game paints
onto a surface rather than an object standing on one.

**The mesh is the painted quad.** In CS2 a decal is a *projector volume* — for a road arrow, a
box roughly a metre tall hovering over the carriageway — and the marking is the artwork projected
down through it. The producer resolves that projection and ships the result: four vertices at the
volume's mid-height, UV-mapped to the atlas rect the game would have sampled. It is flat, it lies
on the carriageway, and it draws correctly in any renderer with no further work.

Shipping the raw volume instead is not an option, and this is worth stating because it looks like
the more faithful choice. The volume does not identify the marking. Every variant shares one box:
`NA_RoadArrow_Forward`, `NA_RoadArrow_Forward_Left90` and `NA_RoadArrow_Left180` are byte-identical
3 × 1 × 6 m boxes with byte-identical UVs and one shared material, and those mesh UVs are a default
box unwrap across six unrelated atlas rects. What distinguishes them is
`DecalProperties.m_TextureArea`, which the game hands the shader as `_TextureArea` alongside
`_MeshSize` and `_DecalLayerMask`. A consumer given the box has no way to recover the arrow.

The flattened quad's UVs are therefore the authored pairing (which way `u`/`v` run — the
producer never guesses this; a guessed orientation once mirrored every road text in the city)
**affinely rescaled into `m_TextureArea`** — the rect the shader actually samples. For most
object decals the two coincide, because the game derives the area from these same UVs at asset
import; for the lane decal parts of §5B they do not — the authored box UVs all crop one
generic, mostly-transparent slab of the LaneLine atlas, and shipping them unscaled is what
made every crosswalk and centre line invisible in the first full import. *(Producer fix
2026-08-07; the promise above is unchanged — it is now actually kept.)*

A consumer **must not** treat a decal as ordinary geometry:

- **Never generate collision from it.** A city holds thousands of road arrows and lane markings.
  This is the single most likely way to render a bundle unusable.
- **It is paint, not surface.** A decal quad is coplanar with the road by construction, so a
  renderer that depth-tests it against the carriageway will z-fight. Lift it by a millimetre or
  two, or draw it with a depth bias. The material also carries `"decal"` in its `flags` (§7).
- **It blends by alpha.** A decal material always declares `alphaMode: "blend"` — the marking's
  texture is the graphic plus a transparent rectangle around it, and compositing by the base
  colour's alpha is what a decal *is*. CS2's decal shaders carry none of the lit-shader
  transparency keywords, so a producer reading only keywords would call them opaque and a
  consumer would render each road arrow as a solid box.
- **`attrs.decalLayers`** names what CS2 targets the projection at — `Terrain`, `Roads`,
  `Buildings`, `Vehicles`, `Creatures`, `Other`. It matters if you re-project: a road marking
  whose `decalLayers` is `["Roads"]` must not be painted across the wall behind it.
- **`attrs.decalHeight`** is the height of the volume the marking was projected through, in
  metres. It is the one thing flattening discards, and it is what you need to re-project onto a
  surface the producer's flat quad does not follow — a heavily cambered or sloped carriageway.
  Nothing else about it is a thickness.

Decals are ordinary instance records in every other respect, and a consumer that only wants
drivable geometry can drop them with a single `kind` test.

### 4.3 What is *not* an instance

Ground detail and grass are rendered by the game as a terrain surface layer, not as discrete
objects. They appear in `terrain/surface_index.png` plus the ground textures (§8), **never**
in `instances.jsonl`. Do not expect per-blade instances.

**Markers are excluded.** Spawn points, pedestrian access points, and pipe, cable and network
nodes are simulation anchors the game never draws, and a city holds enough of them that they
outnumber most real props. A bundle contains **visible world geometry only**, so they appear
in neither `instances.jsonl` nor `meshes/`. A consumer therefore never has to recognise and
filter them, and the object count in a bundle is deliberately lower than the game's own
internal object count. Building, tree and prop counts are unaffected.

---

## 5. `networks.jsonl`

Schema: [`docs/schema/network.schema.json`](schema/network.schema.json). One line per network
segment or junction node. Each record carries geometry in **exactly one** of two forms:

**Mesh form** — the producer read the generated segment mesh:

```jsonc
{ "id": 2001, "kind": "segment", "assetId": "seg_2001",
  "pos": [0,0,0], "rot": [0,0,0,1],
  "attrs": { "form": "elevated", "lanes": 4, "width": 16.0, … } }
```

`assetId` resolves under `<layers.networkMeshes>`. Network meshes are generated per segment,
so they are generally unique rather than deduped.

**Centerline fallback** — the mesh could not be read, so the consumer extrudes it:

```jsonc
{ "id": 2001, "kind": "segment",
  "centerline": [[100,12,-360], [140,12,-360], [180,12.2,-360]], "width": 16.0,
  "attrs": { "category": "road", "form": "normal", "lanes": 4,
             "direction": "twoway", "limit": 50.0, … } }
```

`centerline` is a world-space polyline from start node to end node, already tessellated by the
producer; `width` is the total width to extrude symmetrically about it. A bundle may mix both
forms freely — a consumer must implement both.

`startNode` / `endNode` reference the `id` of `kind: "node"` records in the same file.

Network `attrs`: `name`, `asset`, `category`, `form` (`normal|elevated|tunnel|bridge` — what a
consumer needs to decide bridges, tunnels and terrain cutting), `lanes`, `direction`
(`oneway|twoway|unknown`), `limit` (km/h), `length` (m), `width` (m), `elevation`,
`elevationStart`, `elevationEnd` (m above terrain, negative below).

**Prefer `elevationStart` / `elevationEnd` to `elevation`.** CS2 stores an elevation per end of
a segment, not one for the segment; `elevation` is their mean, kept so a consumer reading only
it keeps working. A segment climbing from `0` m onto a `12` m viaduct and one sitting flat at
`6` m have the same `elevation` and are completely different roads — and it is the sloping ones
that a consumer has to bridge or cut terrain for.

### 5.1 What this producer emits, and why

The schema stays as frozen; this section records which of its options the Carto ripper takes,
so a consumer knows what to expect from a real bundle without reading the mod.

**Both forms, mixed — mesh where there is geometry, centerline where there is not.** A record
is one form or the other and never both, but a bundle mixes them freely, and this is what that
is for. Roads, rail, paths and taxiways are built from a cross-section and come out as real
assembled geometry in the mesh form. Pipes and cables have no surface at all — they are only
ever a line — so they come out in the centerline form, which is the honest description of them.

**How a network mesh is assembled.** CS2 stores no per-segment road mesh, but the parts are all
CPU-side. A composition names a cross-section: a list of pieces — carriageway, shoulder, kerb,
sidewalk, median, side skirt — each an ordinary prefab with its own geometry and materials,
placed by an offset across the width and tiled along the length. The game bends that assembly
onto the road surface in the vertex shader, and the eight matrices it uses turn out to be
nothing but the four control points of `EdgeGeometry`'s left and right boundary béziers. The
producer therefore does the same bend itself, on the same data. Each `.glb` is one edge plus
the junction flare at either end, so a segment covers node to node with nothing between.

**Sections mirrored, both ways.** CS2 authors one side of a road and mirrors it for the other.
Pieces on the mirrored side are reflected across the width (`Invert`), which matters wherever a
piece is asymmetric — a sidewalk with a parking bay has its kerb on one side only. The far
carriageway of a two-way road is additionally reversed end for end (`FlipMesh`), and triangle
winding is reversed on `Invert` XOR `FlipMesh`, which is the game's own rule: each mirror flips
handedness, two mirrors restore it. A producer that reversed on `Invert` alone shipped every
`FlipMesh` piece inside out — faces wound downward, rebuilt normals following, one side of the
road lit from below.

**One-off pieces at a node contribute only their node-side half.** `GenerateCompositionMesh`
keeps a triangle at a node only when all three of its vertices sit at authored `z ≥ −0.01`, so an
end cap (a `DisableTiling` piece) draws just its rounded front half, spanning the flare with its
tip on the node end. Bending the whole cap instead wraps its straight back half around the
flare's converged tip curves — a crossing-triangle dart where a path's rounded end should be, and
end caps that do not line up with the run they cap. The same cut serves `HalfLength` sections,
whose front half the game stretches over the full run.

**Four more of the game's rules, reproduced after a ground-up validation** (the full rule-by-rule
matrix lives in `docs/cs2-api-audit.md` §8): a composition whose cross-section is another's
mirror image shares that mesh with `m_Rotate` set, and the edge is assembled 180°-turned —
inverted curves, start↔end and left↔right swapped — exactly as the game's `RotatedEdge` path
draws it, which is what keeps an asymmetric road's wide side on the correct side. The game's draw
gates apply: an edge part below 0.1 m of run, or a flare below 0.05 m per half, emits nothing.
V coordinates accumulate across tiles at the game's own density (mean |ΔV|/|Δz|) instead of
repeating per tile, so tiling textures are continuous along a run. And **orphan nodes** — placed
nodes no edge connects to — are ripped as the two half-disc caps the game draws for them,
emitted as ordinary mesh-form records with `assetId` `orphan_<id>`; outside connections and
markers are excluded, as the game excludes them. `SkipBottomHalf` pieces are halved and placed as
the game places them, and `PreserveShape` pieces at a node ride the straight corner rails
(matrices 6/7) at their authored length — a crosswalk sits straight across a junction entry
however the flare curves. Roundabout compositions are approximated: the halves and the wedge fill
cover the ring surface out to the island edge (`m_Middle`, cut at 0.99 like the game's matrix 4),
while the exact shader-side blend feathering is not reproduced; the rip report counts every one
of these so a bundle states how often each rule fired.

**No `kind: "node"` records, and so no `startNode` / `endNode`.** CS2 keeps no junction geometry
on the node: the flare where an edge meets an intersection is stored on the *edge*, and each
segment mesh carries its own half of every junction it touches.

**Junctions are closed by the arms' own node meshes — CS2's mapping, ported** *(settled
2026-08-07; the record of every earlier wrong reconstruction is `docs/cs2-api-audit.md`
§16–§18)*. An early version of this section claimed a junction is the union of the segment
ends meeting in it. That is measurably false — across a full city, **1,607 of 1,817 road
junctions have every flare ending more than a metre short of the node**, 316 more than four
metres — but the closing geometry is still entirely the game's: each edge's node mesh tiles
its whole sector of the interior, out to the junction centre, and the union of the arms'
sectors closes the junction with no producer-invented fill of any kind.

Two mappings do it, both from `GenerateCompositionMesh`'s node branch. **Median pieces**
(`NetSectionFlags.Median`) divert onto the middle matrices: the piece's centre column of
vertices (`|x − offset.x| < 0.01`, computed on the authored x) rides
`EdgeNodeGeometry.m_Middle` (cut at t = 0.99, ending at the junction centre), while every
other vertex keeps its proportional position within its half's frame — kerbs keep their
authored width, edges stay flush with the lanes either side, authored UVs and heights carry
across, at **every** node with no gap threshold and no width cap. And **the zero-width
intersection piece** closes the wedge between the halves: every node composition — alleys and
highways as much as boulevards — carries a `* Intersection Piece 0` median section with
`m_Size.x = 0` and a real-width authored mesh, and the generator computes each vertex's side
(`dx`) **before** pinning its lerp coordinate to the offset. The pinned side columns therefore
land exactly on the inner boundary curves of the two halves while the centre column runs down
`m_Middle` — a **three-rail ruled surface** spanning inner-left → centre → inner-right, which
is the pointed dart of road surface a consumer can see interlocking in the game's own
selection outlines. Misreading that pin as "every vertex is centre column" collapses the piece
to a zero-area ribbon and opens a three-cusped hole at every junction of every road class —
the producer shipped exactly that bug until 2026-08-07 (audit §18), which is why this
paragraph documents the order of operations explicitly.

**Everything at a junction centre is now reproduction, not reconstruction.** No fan, no wedge
fill, no residual centre patch, no fallback stretch — every prior producer invention here is
deleted, and the interior a consumer reconstructs is triangle-for-triangle the mapping the
game bakes. The one remaining approximation at nodes is the roundabout three-matrix blend
(0x80 colour bits, shader-side feathering), still approximated by the nearer pair and counted
in the rip report.

**Vertex sync is applied.** A junction stretches some lanes across its flare and squeezes others,
so that a lane edge still meets whatever it meets on the far side. CS2 describes that as four
positions across each half of the cross-section (`NetCompositionData.m_SyncVertexOffsets*`) and
four places they have to reach in this particular junction (`EdgeNodeGeometry.m_SyncVertexTargets*`),
handed to the vertex shader as `m_CompositionSync0..3`. The producer applies the same remap, in
the same coordinate — `CalculateSyncVertexOffsets` normalises each half by
`(x - middleOffset) / (width / 2)`, and `middleOffset` is zero on every real composition measured,
which makes it exactly the coordinate the assembly already interpolates in. Both outermost knots
are fixed points in the measured data, so this moves lane boundaries *within* a flare and never its
outer kerb or its median: it is not what closes a junction, it is what stops the pieces inside one
from stepping. On a measured five-way ground crossroads the interior knots move 0.20 m and 0.60 m
on the left half and −0.54 m and −0.18 m on the right; at an ordinary node offsets and targets are
equal and nothing moves. The interpolation *between* knots is the shader's and is read here as
piecewise linear — the reading the measured knots support, and the only one that puts every knot
exactly on its target.

Ruled out by measurement: `m_MiddleRadius` (zero), the intersection piece (an ordinary 4 m slot),
and `NetCompositionArea` (read only by the tools' snapping, so it places buildings rather than
drawing anything).

`NetCompositionMeshData.m_IndexFactor` is **not** a UV or tiling term, which its scale invites you
to assume. It is `Σ (piece index count / piece length)` — indices per metre of road, summed across
the cross-section, used to size render batches. Computed from the pieces this producer reads it
reproduces the game's own value to five significant figures on both a ground road (21.000) and an
elevated highway node (125.812 against 125.813), which is also the check that the producer is
reading the same geometry the game rasterises rather than some richer authored LOD. UVs need no
correction at all: `GenerateCompositionMesh` copies each piece's authored UV through unchanged, and
so does this.

Also ruled out, having been suspected: the `NetSectionFlags.Hidden*` family does not hide anything
in this path. `GenerateCompositionMesh` — the game's own piece-to-vertex assembly — skips a piece if
and only if it has no `MeshVertex` buffer, and reads no `Hidden*` bit at all; `Hidden` instead
raises `CompositionState.Hidden` on the composition as a whole. `MeshLayer` likewise selects batch
groups on the *baked* composition mesh, which this producer does not use, and every composition
measured reports `defaultLayers 0`. `NetPieceFlags.Bottom` needs no ground/elevated rule either:
ground compositions carry no `Bottom` piece to begin with.

**Network meshes are not deduped.** Every segment is bent onto its own curve, so each `.glb` is
unique. Expect roughly one file per edge, of the order of tens of kilobytes each. The mesh's
origin is the point `pos` places it at, which is *not* its bounding-box centre — a flare
extends past the edge that owns it.

**`form` is never `bridge`.** CS2 elevates a road and lets the terrain and water underneath
decide what that reads as. An elevated segment over water is a bridge, which a consumer decides
by sampling the water raster (§8) — the producer does not guess.

**`category`** is one of `road`, `highway`, `train`, `tram`, `subway`, `pathway`, `waterway`,
`taxiway`, `runway`, `pipe`, `cable`, `fence`, `other`, read off the composition's own
components. A consumer that only wants drivable surfaces filters on it: pipes and cables are in
the file because they are real placed networks, not because anything expects them to be
extruded as ribbons.

**Markers are excluded**, on the same grounds as in §4: network markers are the invisible
anchors for pipe and cable junctions, plus airport airspace. Taxiways carry the marker component
and *are* drawn, so they are the one exception.

---

## 5A. `lanes.jsonl` — the drivable graph

`networks.jsonl` and `networks/` are the road *surface*: where the asphalt is and what it is
made of. `lanes.jsonl` is the road *graph*: where a vehicle may go, in which direction and how
fast. A level built from the surface alone is scenery; the graph is what makes it drivable, and
CS2 holds it as plain ECS data, so it is one of the cheapest useful things in the bundle.

One JSON object per lane:

```jsonc
{ "id": 481230, "owner": 434062, "kind": "car",
  "path": [[100,12,-360], [110,12,-360], [120,12.1,-360]],
  "length": 20.4, "limit": 50.0, "curviness": 0.02,
  "flags": ["Twoway", "Yield"] }
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Producer-local unique integer. |
| `kind` | yes | `car`, `track`, `pedestrian`, `parking`, `connection`, `other`. Closed set in v1. |
| `path` | yes | World-space polyline, bundle frame, already tessellated from CS2's bézier. At least two points, no two consecutive points coincident. |
| `length` | yes | The curve's length in metres — CS2's own value, not the polyline's. |
| `owner` | no | The `id` of the `networks.jsonl` record this lane belongs to, where it belongs to an edge. Absent for lanes owned by a node. |
| `limit` | no | Speed limit in km/h, for the kinds that have one (`car`, `track`). |
| `curviness` | no | How much the lane bends, on CS2's own scale. Carried rather than recomputed. |
| `secondary` | no | `true` where CS2 derived this lane from a master lane rather than placing it. |
| `flags` | no | Lane-type flag **names** (`Twoway`, `Yield`, `Runway`, …). Open — ignore what you do not recognise. Names, not a bit field, because the numeric values are CS2 internals that can change meaning between game versions. |

Both master and derived lanes are emitted, the derived ones marked: a consumer laying out
traffic wants every lane a vehicle can occupy, while one drawing lane lines may want only the
placed ones.

---

## 5B. `lanemeshes.jsonl` — lane-drawn geometry *(v2, 2026-08-06)*

A lane is usually just a path — but a lane whose prefab carries sub-meshes is **drawn world
geometry**: hanging utility cables between poles, fences, hedge rows, all assembled along the
lane's curve at draw time (`BatchInstanceSystem.UpdateLaneInstances`). Neither `networks/` (the
road surface) nor `meshes/` (straight prefab meshes) carries it, so it is its own additive
layer.

**v2 replaces the baked form on the day of its introduction.** v1 baked each lane into its own
`lanemeshes/lane_<id>.glb`; a full city produced 33,500 files and 8.6 GiB from only 112 unique
source meshes, in breach of the bundle's dedupe-by-prefab principle (§6). v2 ships what the
game itself stores — the straight meshes once, plus per-lane bend parameters — and the
consumer bends. No v1 bundle was ever accepted, so there is no compatibility path.

Three pieces:

1. **`lanemeshes/<assetId>.glb`** — one file per unique straight part mesh, standard mesh
   frame (§2.2 / §6), materials joining `materials.json` as everywhere else.
2. **`laneprefabs.json`** — the catalogue: one entry per lane prefab that draws.

   ```jsonc
   { "PowerLine_Cable": {
       "category": "cable",          // cable | pipe | fence | lane (from UtilityTypes)
       "hanging": 0.15,              // UtilityLaneData.m_Hanging, 0 when the prefab never sags
       "parts": [ {
         "assetId": "PowerLine_Cable_Mesh",   // -> lanemeshes/<assetId>.glb
         "pos": [0,0,0], "rot": [0,0,0,1],    // part offset within the prefab, §2.2 frame
         "inverted": false,                   // MeshFlags.Invert: bend runs the curve backwards
         "tilingCount": 0,                    // MeshData.m_TilingCount (see bend algorithm)
         "geometryTiling": true,              // MeshFlags.Tiling: real tiling vs single stretch
         "boundsMin": [-0.1,-0.1,-4], "boundsMax": [0.1,0.1,4],  // authored bounds; z span is
         "decal": true                        //   the tile length. `decal` (optional): the game
       } ] } }                                //   projects this part onto the ground — see below
   ```

   **`decal` parts are wear tint and markings, not the junction surface.** The junction
   interior itself is composition geometry — the arms' node meshes tile it to the centre
   (§5.1) — and the ground beneath is flattened by `TerrainSystem` (the §8 terrain layer).
   The drive lanes crossing a junction carry decal-state sub-meshes that project over that
   surface: a ~12%-opacity near-black wear tint on the drive lanes (measured: texture
   ~(18,18,18) at α ≤ 113 × 0.5 material tint) and the fully opaque road markings —
   crosswalks, centre lines, stop lines (α ≈ 254 where painted). A consumer that skips
   `decal` parts loses the markings and the wear shading, not the asphalt. **A fixed lift is
   not enough to place them:** the lane curves sit at the carriageway's base plane and the
   rendered road surface crowns above them — measured on a full-city rip, the road top near
   centre lines is a median 7 mm above the lane path and clears path + 15 mm on 42% of
   samples, so bent-and-lifted quads sink into the asphalt exactly where the game shows
   paint. The game wins because a decal is a projector volume; the flattened-quad equivalent
   is a drape — subdivide the quad along its length (~3 m) and snap each vertex to the road
   surface below (the reference importer ray-casts the §5 network meshes and adds ~1 cm,
   markings a hair above the wear tint so the two never z-fight). Like every flattened decal (§4.2a), a `decal`
   part's `.glb` ships with its UVs already rescaled into `DecalProperties.m_TextureArea` —
   the authored box UVs of the lane-line meshes all crop one generic transparent slab of
   their atlas, and only the area rect selects the actual marking band. *(Corrected
   2026-08-07 — an earlier version of this note claimed the decals stood in for a mesh-less
   junction interior; audit §18 records why that was wrong.)*

3. **`lanemeshes.jsonl`** — one line per lane that draws:

   ```jsonc
   { "id": 481233, "prefab": "PowerLine_Cable", "category": "cable", "owner": 434062,
     "curve": [[x,y,z],[x,y,z],[x,y,z],[x,y,z]],  // world-space cubic bézier control points
     "parts": [0],                     // catalogue part indices this lane draws — the game's
                                       // per-lane sub-mesh selection, already resolved
     "spans": [[0.0,0.42],[0.55,1.0]], // uncut curve-parameter ranges (CutRange holes: a
                                       // driveway through a fence). Omitted = whole curve.
     "widthScale": [1.0,1.1],          // NodeLane width scale at curve start/end. Omitted = 1.
     "hang": [0.0,0.0],                // HangingLane.m_Distances end drops. Omitted = none.
     "colorSet": [[…],[…],[…]],        // resolved paint, §7.2 shape. Omitted when absent.
     "tunnel": true }                  // owning network runs below ground (the game draws the
                                       // lane on its Tunnel cutaway layer). Omitted = surface.
   ```

**The bend algorithm** (normative; reference implementation in
`tools/blender_import_bundle.py`, ported from the game-validated producer code — audit §12):

- For each span (default `[0,1]`): cut the bézier to the span; if the part is `inverted`,
  reverse the control points. Let `spanLength = curveLength × (span₁ − span₀)`; skip spans
  ≤ 0.1 m (the game's draw gate). `curveLength` is the arc length of the whole curve.
- Tile count, `meshLength = max(boundsMax.z − boundsMin.z, 0.001)`:
  with `tilingCount > 0`: `clip = round(spanLength / max(1, meshLength) × tilingCount)`,
  `tiles = ceil(clip / tilingCount)` capped at 256 when `geometryTiling`, else 1, and each
  tile `t` covers clips `⌊t·clip/tiles⌋ … ⌊(t+1)·clip/tiles⌋` of the arc;
  with `tilingCount == 0`: `geometryTiling` ? `clamp(ceil(spanLength/meshLength), 1, 256)`
  tiles of `meshLength` metres each : one stretched tile.
- Per vertex: `local = rot·v + pos`; `zFraction = (local.z − boundsMin.z) / meshLength`;
  `arcPos = clamp(arcStart + zFraction · arcPerZ, 0, arcLimit)` (the clamp is an
  approximation — the game clips per-pixel at cuts); invert the 32-sample arc-length table
  for `t`; frame is `forward = normalize(tangent)`, `right = normalize(cross(up, forward))`,
  `up = +y`; `width = lerp(widthScale₀, widthScale₁, tGlobal)`;
  `world = point + right·(local.x·width) + up·local.y`.
- Hanging (cables): with catalogue `hanging` h and record `hang` `[d₀,d₁]`, drop `world.y` by
  the scalar cubic `(d₀, (d₀+h·L)·⅔, (d₁+h·L)·⅔, d₁)` at `tGlobal` (`L = curveLength`) — the
  game's quadratic-to-cubic lift of the sag parabola, at calm air; the game additionally sways
  these with sampled wind in the shader, which a static bundle does not carry.

**What is deliberately not here**, each counted in the rip report — excluded at the
catalogue, since it is a property of the part: parts whose `MeshData.m_DefaultLayers` holds
no world-view layer (`Pipeline`/`SubPipeline`/`Waterway` — underground pipes, cables and
waterway overlays the game draws only in its utility cutaway and overlay views; the game's
`GetBoundsMask` names `Default|Moving|Tunnel|Marker` as the world set). **Tunnel-owned lanes
are included** and tagged `tunnel: true` — the game draws them on its Tunnel cutaway layer;
the bundle renders tunnels in full (the §8 terrain cutout opens their portals), and a
consumer wanting the surface world only filters on the tag. *(Pre-2026-08-07 bundles
excluded them as "cutaway view only".)* Decal-flagged parts are **included** and tagged (see
above) — an earlier revision excluded them as "projected markings", which is what opened
every junction centre.
Editor-only and wrong-traffic-side sub-meshes are excluded per lane by the game's own
(inverted) lane sub-mesh test (audit §12), already resolved into each record's `parts`.

---

## 6. Meshes — glTF 2.0 binary (`.glb`)

One `.glb` per unique prefab, **deduped by prefab**: a city with 40,000 trees of nine species
ships nine tree meshes. The file name is `<assetId>.glb`, and `assetId` is the join key from
`instances.jsonl`.

Conventions, all frozen in v1:

- **Frame** — right-handed, X-mirrored, winding reversed. See §2.2.
- **Container** — GLB with a JSON chunk and a BIN chunk; `asset.version` is `"2.0"`.
- **Root node** — the scene has one root node named exactly `<assetId>`.
- **Sub-meshes** — parts of the same rigid prefab are glTF nodes under that root node, with
  their local transforms, one primitive per material.
- **LODs** — no `MSFT_lod`. LOD *n* > 0 is a scene node named `<assetId>_LOD<n>`; LOD 0 is the
  root node with no suffix. A consumer that ignores LODs renders only the unsuffixed node.
  `ripOptions.lodCap` says how many were exported.
- **Materials — the join key is the glTF material's `name`.** It must be a key in
  `materials.json`. The glTF material's own PBR fields are a best-effort approximation for
  standalone viewing; `materials.json` is the authoritative description.
- **Textures are referenced, not embedded.** `images[].uri` is a relative path into the
  texture directory (e.g. `../textures/<sha256>.png`), which keeps a shared atlas stored once
  instead of once per mesh. A URI that resolves outside the bundle is invalid. Embedded
  `data:` URIs are tolerated but defeat dedup and are flagged.
- **Attributes** — `POSITION` always; `NORMAL`, `TEXCOORD_0`, `TANGENT` and `COLOR_0` when the
  source has them. `TEXCOORD_0` follows glTF's top-left UV origin, so the producer flips V from
  the game's bottom-left convention (`v_glb = 1 - v_game`). `TANGENT.w` has its sign flipped
  along with the X mirror, since mirroring reverses bitangent handedness.
- **Indices** — always indexed geometry, `UNSIGNED_INT`, one index accessor per primitive.

### 6.1 One state, not every state

A prefab's sub-meshes are not all drawn together. The game mixes two kinds in one list: parts
drawn *simultaneously* (a building and its roof units) and *alternatives* it picks between at
draw time — a tree prefab carries sapling, adult, dead and stump meshes as four entries on the
same asset. Exporting all of them would stack every alternative into one file.

A bundle therefore contains **one documented state per prefab**: healthy, adult, unloaded,
undamaged, forward-facing, and with the traffic side the city is actually configured for. A
prefab that exists *only* as state variants falls back to its first entry rather than
producing no file, so **every `assetId` referenced by an instance still resolves to a `.glb`**.

Per-instance state is not lost — it is on the instance, not the mesh. `attrs.growth`,
`attrs.treeState` and `attrs.state` say what state each object is actually in, and a consumer
that cares can scale, swap or tint accordingly.

---

## 7. `materials.json`

Schema: [`docs/schema/materials.schema.json`](schema/materials.schema.json). A flat object
keyed by `materialId`.

```jsonc
{
  "eu_res_01_mat": {
    "baseColor": "textures/<sha256>.png",
    "normal":    "textures/<sha256>.png",
    "mask":      "textures/<sha256>.png",
    "emissive":  "textures/<sha256>.png",
    "colorMask": "textures/<sha256>.png",
    "tint": [1, 1, 1, 1],
    "emissiveFactor": [1, 1, 1],
    "alphaMode": "opaque",
    "alphaCutoff": 0.5,
    "doubleSided": false,
    "flags": ["emissiveWindows"]
  }
}
```

Every map is optional; a consumer substitutes its own default for anything absent. Material
objects are **closed** — an unknown field is an error, so a consumer never quietly drops a map
it should have handled.

The table is bundle-wide and **accumulates**. Meshes and networks are ripped by separate passes,
each resolving the materials of what it touches, and a later pass merges into the file rather
than replacing it — a network rip must not strip out the buildings' entries, since every glTF
material name in the bundle has to remain a key here (§6). Where two passes produce the same
`materialId`, the later one wins.

### 7.2 `colors.json` and `lights.json`

Both are keyed by a **sub-mesh `assetId`** — the glTF node name inside the owning prefab's
`.glb` (§6) — because that is where CS2 keeps them. Measured over a full city: colour palettes
sit on 3,984 sub-meshes covering 1,132,295 instances and on no object prefab at all; procedural
lights likewise, on 1,184 sub-meshes covering 153,548 instances.

```jsonc
// colors.json
{ "NA_ResidentialHigh01_Mesh": [
    { "channels": [[0.8,0.7,0.6,1], [0.3,0.3,0.3,1], [1,1,1,1]], "probability": 100 } ] }

// lights.json
{ "StreetlightSingle02_Mesh": [
    { "slot": 0, "color": [1,0.87,0.68,1], "colorOff": [0,0,0,1],
      "purpose": "StreetLight", "responseSpeed": 0.5,
      "range": 24.0, "temperature": 2700.0, "animation": -1 } ] }
```

**Colours.** A variation is CS2's `ColorSet`: three channels, and **the material's `colorMask`
map (§7.1) is what selects between them, per texel.** Not the `mask` map — that is the PBR
packing, and confusing the two is how this layer shipped once with a palette nothing could
apply. A consumer needs both the `colorMask` texture on the primitive's material and the
instance's resolved colours.

**How to apply it — the formula.** The tint is a masked *multiply*, not a replacement:

```
tinted = baseColor.rgb
tinted = lerp(tinted, tinted × colorSet[0].rgb, colorMask.R)
tinted = lerp(tinted, tinted × colorSet[1].rgb, colorMask.G)
tinted = lerp(tinted, tinted × colorSet[2].rgb, colorMask.B)
```

Multiplying keeps the albedo's authored detail; the channels are authored disjoint, so the
chain order is immaterial in practice. CS2's shaders ship compiled, so this is not lifted from
shader source — it is the standard tint-mask-over-desaturated-albedo construction the game's
asset-authoring documentation describes, and `tools/blender_import_bundle.py`
(`apply_color_tints`) implements it as the working reference. Reading "selects" as *replace the
albedo with the channel colour* flattens every surface to a solid tone; reading it as this
multiply is correct.

**Vegetation is grey until you do this.** CS2 authors tintable albedo desaturated — trees,
bushes and hedges above all — and gives it colour only through this per-instance tint. A
consumer that skips `colorSet` renders every tree and bush grey-brown, and the whole city reads
as dead. The `foliage` flag (§7.1) marks the materials concerned. The companion trap is alpha:
leaf canopies are alpha-tested card geometry (`alphaMode: "mask"` on the leaf material), so a
loader that ignores `alphaMode` draws each canopy as solid grey rectangles. Honouring
`alphaMode` is required glTF conformance, but game-engine import pipelines frequently need it
switched on per material — check this first when foliage looks wrong.

The paint a given object was drawn with is on the instance, as
`attrs.colorSet = [[r,g,b,a],[r,g,b,a],[r,g,b,a]]` — three linear RGBA channels, the game's own
resolved answer read from the renderer's per-instance state (`MeshColor`). CS2 computes it once
from a per-object random seed, the palette's probabilities and the season filters; the bundle
records the result rather than reproducing the computation. `attrs.colorCustom: true` marks a
player-painted override. The palette in this file remains what it is: the prefab's *possible*
variations with their weights, for a consumer re-colouring a city rather than reproducing this
one. Without `colorSet`, every copy of a prefab renders identically where the city shows a
street of different colours.

*(Correction, 2026-08-06: this file previously documented `attrs.color = {variation, value}` as
the chosen variation. The component that value was read from — `Game.Objects.Color` — is written
only by the game's infoview overlay system and holds UI-overlay state, not paint; the documented
semantics were never actually delivered. `attrs.color` is gone and `attrs.colorSet` carries what
the description always promised. A consumer must tolerate both keys' absence, per §4.2's own
rule.)*

A material with no `colorMask` has no per-texel selection, so its meshes take channel 0
everywhere or, more safely, are left at their base colour.

**Lights.** The colours, purpose, response speed, range and colour temperature are the light's
own data and are exact. **There is no position, because CS2 does not store one.** A procedural
light binds to a *material slot*, and its place in the world is wherever the geometry using that
material is. A consumer places each emitter at the centroid of the primitive using the
corresponding material in the same `.glb`. `colorOff` is the unlit colour, and `responseSpeed`
how fast it crosses between the two — which is what makes a day/night cycle look right rather
than switching. Inventing a transform here would be inventing data, on the same grounds as
§5.1.

### 7.1 Channel semantics — read this before wiring a shader

CS2 packs several PBR terms into one texture. The packing is fixed in v1:

| Map | Colour space | Channels |
|---|---|---|
| `baseColor` | sRGB | RGB albedo, A alpha |
| `normal` | linear | Tangent-space, **OpenGL convention (+Y up)** |
| `mask` | linear | **R** metallic · **G** ambient occlusion · **B** detail/emissive mask · **A** smoothness |
| `emissive` | sRGB | RGB emission |
| `colorMask` | linear | **R**, **G**, **B** — per-texel weights for the three `colorSet`/`colors.json` channels, applied by §7.2's formula · **A** snow mask (advisory — see below) |

Note `mask.A` is **smoothness**, so `roughness = 1 - A`. Getting this backwards is the most
common way to make a bundle look wrong, and it is silent — everything just renders too shiny
or too flat.

`mask.G` and `mask.B` are the producer's reading of CS2 as an HDRP derivative, and the game's
own asset-authoring documentation describes them instead as a glossy-coat layer and an unused
channel. CS2 ships its shaders compiled inside asset bundles, so the packing is not recoverable
from the game's managed assemblies and the disagreement is recorded rather than resolved. Both
readings agree on R and on A, which are the two that matter most. Treat G and B as advisory.

`colorMask.A` was documented at the freeze as "snow coverage (white = no snow)", with no
recorded source. Measured over a full city — 731 distinct `colorMask` textures — 317 are
all-black in A, 59 all-white and 355 mixed, and **every vegetation mask is all-black**, which
under the frozen reading would mean permanently snow-covered foliage. The polarity is therefore
recorded as the opposite — **white = snow may accumulate here** — but like `mask.G`/`B` it is
not recoverable from managed code and stays advisory: a consumer building weather should
validate it against the game before trusting either reading. *(Corrected 2026-08-06; see
§12.1.)*

`flags` are producer hints a consumer may act on or ignore. Known in v1: `foliage`, `decal`,
`glass`, `emissiveWindows`, `terrainBlend`, `colorVariation`.

---

## 8. Textures, terrain, water and surfaces

**Textures** live at `<layers.textures><sha256>.png`, lossless PNG, deduped by content hash.
The file name **is** the lowercase hex SHA-256 of the file's own bytes; a validator checks
this, because it is the invariant that makes dedup verifiable rather than merely claimed.

**Terrain, water and surfaces** *(frozen 2026-08-06, Phase 6)* are single-channel PNGs on one
grid, geo-referenced by the manifest's `rasters` block (§3) and placed in the same frame as the
meshes (§2.3): `originWorld` is the top-left cell centre, rows run toward −Z (south), columns
toward +X (east).

- **`terrain/heightmap.png`** (`layers.terrain`) — 16-bit grayscale. World height in metres is
  `value * rasters.elevationScale + rasters.elevationOffset`. These are the **final** heights —
  after the game's road job has flattened the ground under roads and junctions
  (`TerrainSystem`'s ShiftTerrain/ClipTerrain lane draws, building lots and area grading are
  all baked in). Junction interiors are paved by the arms' node meshes (§5.1) — this terrain
  is the flattened ground *beneath* them, not the visible junction surface; skipping the
  heightmap costs the ground everywhere, not the junction floors. *(Corrected 2026-08-07,
  audit §18.)*
- **`terrain/water_level.png`** (`layers.water`) — 16-bit grayscale. The water **surface
  level** — an elevation, in the heightmap's own encoding: level in metres is
  `value * rasters.elevationScale + rasters.elevationOffset`; the stored value `0` means dry
  (no water data). **Water exists at a cell where level > ground height**; its depth there is
  `level − ground`. The level is computed per simulation cell (ground at the sim cell + depth,
  both on the simulation's own ~28 m grid), dilated one sim cell outward, and resampled
  bilinearly onto the terrain grid — so the surface extends flat a little past the shoreline
  and the heightmap cuts it exactly where the game's shoreline is. Consumers must NOT treat
  the value as depth and add it to ground: depth is not smooth across a shoreline, and
  resampling it against the finer terrain grid re-bases mid-channel depths onto gorge walls.
  *(Replaces `terrain/water_depth.png`, 2026-08-07; see §12.1.)*
- **`terrain/surface_index.png`** (`layers.surfaceIndex`) — 8-bit grayscale, the value being a
  surface id, **not** a colour (nearest-neighbour only; never resample ids bilinearly).
  - `0` — natural ground. The game classifies it per-pixel on the GPU (grass/dirt/rock by
    slope, height and noise); the bundle does not reproduce that classification.
  - `1` — **roadbed**: ground the road job flattens. Stamped from the exact footprints the
    game draws its terrain adjustments with — every edge segment, junction-node strip and
    orphan cap of a net whose prefab carries `FlattenTerrain`/`ClipTerrain`, trimmed by
    `TerrainComposition.m_WidthOffset`, elevated decks and tunnels excluded (their terrain is
    shifted *away* from the road, not onto it). The smoothing apron where terrain slopes to
    meet the road is not included. In-game this ground reads as dirt tinted by the drive
    lanes' ~13%-alpha wear decals (§5B `decal`); consumers texture it as roadbed and give it
    road physics. **Consumer rule — sink roadbed faces slightly (~0.15 m).** The game clamps
    the ground under a carriageway to the *minimum* of the road's surface band, below the
    asphalt top; a consumer rebuilding ground from these 3.5 m samples by linear interpolation
    will crest through the road meshes between samples. Dropping the faces marked `1` by a
    small constant is the same clamp direction and keeps ground and carriageway from
    z-fighting; the reference importer does this (`_ROADBED_SINK`). *(Added 2026-08-07.)*
- **`terrain/cutout.png`** (`layers.cutout`) — 8-bit grayscale, `0` = terrain present,
  `255` = **cut**: the game never shows this cell's ground. *(Added 2026-08-07, see §12.1.)*
  The game does not carve tunnel portals or sunken trenches out of its heightmap — a trench's
  ground is merely clamped into `[lane, lane + 30 m]` and a shallow tube's cover is shifted
  *up*, and every frame a stencil pre-pass (`RoadClip`) discards the covering terrain pixels
  on screen. The heightmap therefore legitimately contains ground that walls off every tunnel
  mouth and sunken road. This mask is that stencil pass projected top-down: a cell is marked
  where a clip section's ribbon (every edge segment, node strip and orphan cap of a net whose
  prefab carries `ClipTerrain`, plus the triangles of `ClipArea` surfaces) crosses it **and**
  the terrain stands above the lane surface within the section's clip band — the composition's
  edge-height band, raised to `ElevationLimit·3` (30 m for roads) at tunnel portals
  (Tunnel + transition side) and open trenches (Lowered side), expanded by the game's own
  0.3 m. Terrain above the band (the cover over a deep tube) is kept, exactly as in-game.
  **Consumer rule — cells marked `255` must be removed** (delete the faces / punch heightfield
  holes); rendering them walls off the underground network they hide. The mask **overrides
  `surface_index.png`**: a cut cell's surface id is meaningless. One deliberate divergence
  from the game: the game's clip band starts 0.3 m *below* the lane and so also discards the
  roadbed itself (its road meshes hide the hole); the bundle keeps any ground at or below the
  lane — only terrain that would *block* the way is cut — so the mask is empty over ordinary
  surface roads.
- **`terrain/surfaces/index.json`** (`layers.surfaces`) — maps each id to a name and a
  representative texture:
  ```jsonc
  { "0": { "name": "natural", "texture": "terrain/surfaces/grass.png" },
    "1": { "name": "roadbed", "texture": "terrain/surfaces/dirt.png" } }
  ```
  `texture` may be `null` when the theme texture could not be read. The referenced
  `terrain/surfaces/*.png` are the loaded map theme's ground textures, ripped as bound.
- **`terrain/splatmap.png`** (`layers.splatmap`, optional) — RGBA8, **the game's own ground
  blend**: the 4096² per-pixel weight map the terrain shader mixes grass/dirt/rock (and
  painted surfaces) with, read back verbatim from `TerrainMaterialSystem`'s splat render
  target (`colossal_Splatmap`). *(Added 2026-08-07.)* It is generated on the GPU from height
  variance, the theme's rock/grass limits, noise and the user's surface brush, and it is what
  makes in-game ground transitions smooth. Geo-referenced by its own `rasters.splatmap`
  sub-block (`file`, `size`, per-axis `resolution`, texel-centre `originWorld`, row 0 toward
  −Z like every raster, plus the shader shaping constants `splatMultiplier`/`splatPower`); its
  grid is the playable area, not the heightmap grid — sizes differ.

  **The channel-to-ground assignment is published, in `rasters.splatmap.channels`. Read it;
  do not calibrate it.** *(Amended 2026-08-08 — this block previously told consumers to derive
  the map from slope and roadbed contrast, and the reference importer's calibration got three
  of four channels wrong on a real bundle.)* The block names a ground per channel using
  `terrain/surfaces/index.json`'s own keys, so each joins straight to a texture:

  ```jsonc
  "channels": { "R": "grass", "G": "dirt", "B": "extra1", "A": "extra2",
                "base": "rock", "baseWeight": "max(0, 1 - (R + G + B + A) / 255)" }
  ```

  A channel the producer cannot name ships as `"unknown"`; drop it from the blend rather than
  guessing. `channelProvenance` states, per channel, how firmly each is established — `G` is
  reached by elimination rather than measured, so it is the one to doubt. If the whole
  `channels` object is absent the bundle predates this amendment; the map above is still the
  right assumption, since it is a property of the game's `TerrainMaterialType` ordering rather
  than of any bundle.

  Two things a consumer must still get right (both produced square-splotch terrain when they
  were not): **(1) `base` is not a channel.** The four weights are *painted*-ground weights and
  a cliff is simply unpainted — very steep faces carry ≈0 in all four while rendering rock
  in-game — so the base ground's weight is the residual `max(0, 1 − (R+G+B+A))`. Normalising
  the four channels into a partition loses rock entirely. **(2) Do not treat a roadbed as
  dirt.** The blend weights grass as heavily under roadbeds (mean 251/255) as under lawns
  (243); the dirt look beside roads is the §5B wear decals and §8b surface areas, not this map.

  Consumers should sample it bilinearly (it is weights, not ids) and prefer it over per-cell
  classification from `surface_index.png` wherever it is present; `surface_index.png` remains
  the *semantic* roadbed mask (physics, the sink rule), not the visual one. When the file or
  its `rasters.splatmap` block is absent, the pre-2026-08-07 behaviour (per-cell
  classification) is the documented fallback.
- **`terrain/user_splatmap.png`** (optional) — RGBA8 (R/G meaningful): the player's painted
  surface weights for the paintable extra grounds, read back from
  `TerrainMaterialSystem.UserSplatmap`. *(Added 2026-08-07.)* Written only when any texel is
  non-zero; geo-referenced by `rasters.userSplatmap` (same conventions as `rasters.splatmap`).
  Its channels are published the same way — `"channels": { "R": "extra1", "G": "extra2" }`,
  straight off the decompiled paint path. The source texture is `R8G8_UNorm`, two channels
  wide, so `extra3` and `extra4` **can carry no weight anywhere**; the block lists them under
  `unrepresentable` and `terrain/surfaces/` ships no texture for them.
- **`terrain/world_heightmap.png`** (optional) — 16-bit grayscale, the **backdrop terrain**
  beyond the playable border (`TerrainSystem.worldHeightmap`, read losslessly via GPU
  readback). *(Added 2026-08-07.)* Geo-referenced by `rasters.worldTerrain` (texel-centre
  origin over `worldOffset…worldOffset + worldSize`); the playable area is a sub-rect of it,
  and where they overlap the main heightmap's samples are authoritative. Same elevation decode
  as the main heightmap — the game's height cascade blits both into one seamless surface, so a
  consumer can (and the reference importer should) verify continuity across the playable
  border as a self-check.
- The theme's ground textures under `terrain/surfaces/` now include tangent **normal maps**
  (`grass_normal.png`, `dirt_normal.png`, `rock_normal.png`) and, when the theme binds them,
  the four paintable extras (`extra1..4.png` + `extra1..4_normal.png`). *(Added 2026-08-07;
  all optional.)* Normal maps are decoded from the game's two-channel packing where present,
  like every material normal (§7.1).

The `rasters` block gains two keys for this (`elevationScale`, `elevationOffset` — see the
schema); they are v1-compatible optional manifest additions, required by the validator when a
terrain or water layer is declared. `worldTerrain`/`worldWater` (the 4× backdrop) keep the
same value semantics at their own size when present.

**Until the producer assembles `bundle.json` (Phase 7)**, the identical `rasters` object is
written at `terrain/rasters.json`, and Phase 7 lifts it into the manifest verbatim. A consumer
of a *complete* bundle reads the manifest only.

### 8b. Surface areas — `areas.jsonl` + `areaprefabs.json` *(added 2026-08-07, see §12.1)*

The painted ground patches the game draws through its area system: **house driveways,
parking-lot pavement, park paths, sports courts, campsite clearings**. These surfaces belong
to no other layer. A parking lot is a *meshless* building prefab — its instance record places
nothing visible, its bay markings are §4.2a decal instances — and the pavement itself is a
`SurfacePrefab` area the building spawns as a sub-area, triangulated on the area grid and
rendered by `AreaBatchSystem` with the prefab's `RenderedArea` material. A consumer without
this layer shows grass wherever the game shows pavement.

- **`areas.jsonl`** — one JSON object per rendered surface area:

  ```jsonc
  { "id": 482113, "prefab": "concrete_surface_01", "owner": 173512,
    "nodes": [[x,y,z], …],            // bundle-frame world positions (§2.2), NOT mirrored
    "triangles": [[a,b,c], …] }       // indices into nodes, the game's own triangulation
  ```

  `owner`, when present, is the instance `id` of the building/object that spawned the area.
  Node heights follow the ground (the game grades terrain under areas; §8 heightmap has it).

- **`areaprefabs.json`** — one entry per referenced `prefab` key:

  ```jsonc
  { "concrete_surface_01": { "name": "Concrete Surface 01",
      "color": [r,g,b,a],                              // material _BaseColor, optional
      "texture": "terrain/surfaces/area_concrete_surface_01.png" } }  // optional
  ```

  The texture is the area material's base map, captured as bound; tiling is world-planar
  (the game maps these in world space — a consumer picks a plausible metres-per-repeat).
  Both keys are optional; a consumer falls back `texture` → `color` → its own neutral.

Areas are ground paint, like decals (§4.2a): never generate collision from them, and draw
them just above the terrain to avoid z-fighting. The reference importer places them under
`--areas` (implied by `--everything`).

---

## 9. `vector/`

The unchanged vanilla Carto export — GeoJSON, Shapefile, GeoTIFF, exactly as Carto has always
produced it. The ripper is strictly additive; nothing about the vector output changes, and it
is simply relocated under the bundle when both run together. Its contents are outside this
contract; see the Carto wiki.

---

## 10. `warnings.jsonl`

Advisory, not schema-validated in v1. One JSON object per line describing something the
producer could not do, so that a missing object is explicable rather than mysterious:

```jsonc
{ "kind": "mesh", "assetId": "some_prefab", "reason": "mesh not readable at runtime" }
```

A consumer may ignore this file entirely. `counts.warnings`, if present, is its line count.

**It is also how a producer declares a gap it could not close.** An `assetId` referenced by
`instances.jsonl` with no `.glb` behind it is normally invalid — but dropping those instances
instead would lose their placement, which §4.1 requires to survive even when geometry cannot.
So a missing mesh **declared here** validates as a warning; an **undeclared** one stays an
error. A consumer substitutes a stand-in and the objects still land in the right places.

A declared `assetId` may also **exist but be incomplete**: a geometry asset whose meshes could
not all be read still writes the `.glb` from what survived, with a `"partial: …"` reason here —
because the meshes of one asset draw together (§6), an incomplete file is invisible from the
file alone. Presence of a warning therefore does not imply absence of the file; check the
reason. *(Added 2026-08-06.)*

---

## 11. Validating a bundle

```
python tools/validate_bundle.py <bundle_dir>
```

Standard library only — Python 3.8+, no pip installs, no game, no mod source. It checks the
structural schemas *and* the cross-file invariants a schema cannot express: every `assetId`
resolves to a mesh, every texture reference resolves and is named by its own hash, every
`.glb` is a well-formed glTF 2.0 whose material names join into `materials.json`, manifest
counts match reality, quaternions are unit length, and `bounds` actually encloses the content.
Exit code 0 means valid.

`docs/example-bundle/` is a complete, minimal, hand-authored bundle — one cube mesh, one
texture, one material, two instances, one network segment — that validates clean. Build a
consumer against it before pointing one at a real city.

```
python tools/make_example_bundle.py    # regenerate it (deterministic, byte-identical)
python tools/test_bundle_contract.py   # 30 checks: the example is valid, its winding really
                                       # follows §2.2, and each way to break the contract is
                                       # actually caught
```

---

## 12. Versioning and compatibility

`schema` is a single integer. There is no minor version.

**A consumer must:**

- reject a bundle whose `schema` it does not implement;
- read every path from `layers` and treat an absent layer as an absent feature;
- ignore unknown keys inside `attrs` objects (the only open objects in the contract);
- implement both network geometry forms (§5).

**Within `schema: 1`, the producer may** add a new optional key to an `attrs` object, add a
new value to the `flags` array, add a new layer key, or add a new optional manifest key that a
v1 consumer can safely ignore.

**Anything else is a breaking change and requires `schema: 2`** — including adding a value to
a closed enum (`kind`, `form`, `state`, `alphaMode`, `direction`), changing a channel packing,
changing the coordinate frame or the mesh conversion, renaming a field, or making an optional
field required.

The closed enums and the closed manifest/material objects exist precisely so that these
changes cannot slip through unnoticed: a v1 consumer given v2 data fails loudly at its
validator rather than quietly rendering a wrong city.

### 12.1 Amendments to schema 1

Changes made to `schema: 1` after the freeze are recorded here rather than folded in
silently.

**`colorMask` added to the material object (2026-08-05).** The material object is closed, so by
the rule above this needed `schema: 2`. It was amended in place instead, on these grounds: the
map is the one that makes `colors.json` applicable at all (§7.2), so without it the frozen v1
was internally inconsistent — it specified a colour palette and no way to use it; the closure
exists to stop a consumer *silently dropping* a map, and a consumer that has never seen
`colorMask` drops exactly the map the closure was meant to protect; and no bundle conforming to
v1 has been produced end-to-end yet, because the manifest (§3) and the terrain layers (§8) are
not built, so there is no v1 consumer in the field to break.

A consumer written against the pre-amendment v1 rejects a bundle carrying `colorMask` at its
validator, which is the loud failure the closure promises. Any further change to a closed object
needs `schema: 2`.

The other changes made at the same time are ordinary v1-compatible additions: the `attrs` keys
`elevationFlags` and `decalLayers` (§4.2), and the network `attrs` keys `elevationStart` and
`elevationEnd` (§5). The corrected descriptions of `elevation` (§4.2) and of what selects
between colour channels (§7.2) are not changes to the format at all — the producer always wrote
those values; the document described them wrongly.

**`colorMask` semantics made explicit; `colorMask.A` polarity corrected (2026-08-06).**
Documentation only — no producer byte changed; the textures pass through from the game as they
always did. Two description fixes: §7.2 now states the tint's application formula (a masked
multiply), which was previously only implied by "selects" and could be misread as
replace-the-albedo; and §7.1's `colorMask.A` row said "white = no snow", which the shipped data
contradicts — every vegetation mask is all-black in A, which would have meant permanently
snowed foliage. A is now documented as "white = snow may accumulate", advisory. A consumer
built on the pre-correction description of A renders inverted snow if it implemented weather;
nothing else it could have built is affected.

**Surface areas added as §8b (2026-08-07, Phase 6).** An ordinary v1-compatible addition: two
new optional files (`areas.jsonl`, `areaprefabs.json`) and no change to any existing object.
Added because the screenshot acceptance showed grass where the game shows pavement, and the
inventory of placed-but-meshless assetIds (ParkingLot*, courts, campsites — 118 assetIds,
~25k records) plus house driveways all resolve to the same missing mechanism: `SurfacePrefab`
areas drawn by `AreaBatchSystem`, a domain no existing layer carried. A consumer built before
this addition ignores both files and simply keeps showing grass there.

**Water layer changed from depth to surface level (2026-08-07, Phase 6).** §8's
`terrain/water_depth.png` (`value * depthScale` metres of depth) is replaced by
`terrain/water_level.png` (surface elevation in the heightmap's encoding, 0 = dry), and the
`rasters.depthScale` key is retired — the closed `rasters` object now rejects it, loudly.
Grounds: the frozen depth layer was **unusable as specified**. Depth lives on the simulation's
~28 m grid, measured against that grid's own coarse ground; §8 told consumers to reconstruct
the surface as fine-grid ground + resampled depth, which re-bases every sample onto ground the
simulation never saw — against a gorge wall, mid-channel depth lands halfway up the cliff, and
the first real import flooded the river's gorge. The surface *level* is the quantity that is
actually smooth across a shoreline, so it is what survives resampling; depth is recoverable at
any cell as `level − ground` while the reverse reconstruction is not faithful. The layer was
frozen 2026-08-06 and no consumer besides this repo's own harness ever read it; the harness
moves in the same commit. A consumer written against the depth reading fails loudly at its
validator (`depthScale` is gone; pre-amendment validators required it), which is the closure
working as promised.

**Terrain cutout mask added (2026-08-07, Phase 6).** A v1-compatible addition: one new raster
(`terrain/cutout.png`, `layers.cutout`, 8-bit on the §8 grid) and no change to any existing
object. Added because the first drive-through inspection found every underground network
walled off: the game never removes the terrain covering tunnel portals and sunken trenches
from its heightmap — `TerrainSystem` clamps a Lowered composition's ground into
`[lane, lane + ElevationLimit·3]` and hides the remainder per-frame with the `RoadClip`
stencil pass (`GenerateClipDataJob` → `TerrainSurface.RenderClipAreas`, decompiled) — so the
§8 heightmap alone reproduces the blockage faithfully and uselessly. The mask is that stencil
pass projected top-down (see §8 for the exact band rule and the one divergence: the roadbed
under the lane is kept). It also settles the splat question the same inspection raised: a cut
cell's `surface_index` value is declared void rather than re-stamped. A consumer built before
this addition ignores the file and keeps the walls; one that honours it must delete the marked
cells.

**Bulk layers may be stored compressed (2026-08-08).** An ordinary v1-compatible addition: a
layer file may be gzip-compressed at rest, in which case its name carries `.gz` —
`instances.jsonl.gz`, `meshes/<assetId>.glb.gz`. **Both storage forms are valid schema 1, and a
consumer must handle both, resolved per file rather than per bundle.** Nothing about a layer's
*contents* changes: decompress and you have byte-for-byte the JSON Lines or GLB this document
already specifies.

Three rules make this safe to add without a version bump:

- **The suffix is the declaration.** A compressed layer is never stored under its bare name. A
  consumer that has not implemented this fails to *find* `instances.jsonl` — the loud failure —
  rather than succeeding in parsing gzip bytes as JSON.
- **`layers` names the file that is really there,** suffix included, since §1 already requires a
  consumer to read paths from the manifest rather than hard-code them. For robustness a consumer
  should also accept a manifest naming the logical path (`instances.jsonl`) when only
  `instances.jsonl.gz` exists; bundles produced before this amendment do that.
- **An `assetId` is joined on the logical name.** `meshes/AppleTree01.glb.gz` backs assetId
  `AppleTree01`, not `AppleTree01.glb`. Strip `.gz` before taking the stem.

Which layers this applies to is a producer choice, not part of the contract. This producer
compresses every `.glb` and every `.jsonl` except `warnings.jsonl`, which it reads back between
passes and leaves as text. It does **not** compress `textures/`, `terrain/`, or the `.json`
catalogues: PNG is already deflate-compressed, so re-wrapping it costs decode time and returns
nothing, and the catalogues are too small to matter.

gzip specifically, rather than zstd or lzma, because every consumer language decompresses it
from its standard library. The producer writes it deterministically — MTIME zero, no filename
field — so two rips of the same city still produce identical bytes.

**Terrain rasters frozen as 16-bit PNG, not GeoTIFF (2026-08-06, Phase 6).** The original §8
placeholder said elevation and water would be "GeoTIFFs written by Carto's existing raster
path". Freezing the layer for real changed that to single-channel PNG with scale/offset keys
in `rasters`, on these grounds: the vanilla GeoTIFF writer is inseparable from the CRS options
and file-naming of the vector export, and the bundle's geo-referencing authority is the
manifest, not per-file tags; 16-bit grayscale is a *lossless passthrough* of the game's native
height samples where a float GeoTIFF would be a re-quantisation twice the size; PNG is already
the bundle's only raster format, so consumers gain no second decoder; and no bundle with a
terrain layer existed before this freeze, so no consumer breaks. The vanilla GeoTIFF export
remains available unchanged through `vector/` (§9) for CRS-referenced GIS workflows. The
`rasters` additions (`elevationScale`, `elevationOffset`, `depthScale`) are ordinary
v1-compatible optional-key additions.
