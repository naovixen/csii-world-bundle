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
  materials.json                  # material table, keyed by materialId
  meshes/<assetId>.glb            # unique prefab meshes, deduped by prefab
  networks/<assetId>.glb          # generated per-segment network meshes
  textures/<sha256>.png           # textures, deduped by content hash
  terrain/elevation.tif           # elevation raster
  terrain/world_elevation.tif     # optional world-elevation surround
  terrain/surface_index.png       # per-cell ground-material id
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
| `kind` | yes | One of `building`, `prop`, `tree`, `fence`, `decal`, `poi`. Closed set in v1. |
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

### 4.2 `attrs`

`attrs` is deliberately open. A consumer **must** ignore keys it does not recognise and
**must not** require any key to be present — availability varies by object type, by game
version, and by which optional properties the user enabled. Documented keys:

`name`, `category`, `zone`, `zoning`, `theme`, `brand`, `level`, `elevation` (terrain height
under the instance, m), `height` (true height in m from the mesh bounds), `storey`, `state`
(`normal|construction|abandoned|condemned|destroyed`), `growth` (tree stage, 0..1), `season`,
`address` (`[district, street, number]`).

`height` comes from mesh bounds and is authoritative. `storey` and `level` are game metadata
and must never be used to derive geometry.

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
(`oneway|twoway|unknown`), `limit` (km/h), `length` (m), `width` (m), `elevation` (m above
terrain, negative below).

### 5.1 What this producer emits, and why

The schema stays as frozen; this section records which of its options the Carto ripper takes,
so a consumer knows what to expect from a real bundle without reading the mod.

**Centerline form, always.** CS2 has no per-segment road mesh to rip. It stores one straight
mesh per *composition* — a cross-section extruded over a unit length — and bends it along the
edge in the **vertex shader**, from the eight `CompositionMatrix` values the renderer writes per
instance. The mesh on its own is a straight strip at the origin, and baking the bend means
reimplementing an HLSL vertex program. What the game *does* compute CPU-side is the road
surface's own boundary — `EdgeGeometry` holds the world-space left and right bezier of the
painted surface — so the centerline written here is the exact average of those two curves,
tessellated to 3 m (straight runs collapse to two points). The mesh form remains valid in the
schema for a producer that has real meshes; this one does not pretend to.

**No `kind: "node"` records, and so no `startNode` / `endNode`.** CS2 keeps no junction geometry
on the node. The flare where an edge meets an intersection is stored on the *edge*, and a
junction is the union of the segment ends meeting in it. Every segment's centerline already runs
node-to-node, so the junction area is covered; a node record would either duplicate that or
invent geometry the game does not have.

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

### 7.1 Channel semantics — read this before wiring a shader

CS2 packs several PBR terms into one texture. The packing is fixed in v1:

| Map | Colour space | Channels |
|---|---|---|
| `baseColor` | sRGB | RGB albedo, A alpha |
| `normal` | linear | Tangent-space, **OpenGL convention (+Y up)** |
| `mask` | linear | **R** metallic · **G** ambient occlusion · **B** detail/emissive mask · **A** smoothness |
| `emissive` | sRGB | RGB emission |

Note `mask.A` is **smoothness**, so `roughness = 1 - A`. Getting this backwards is the most
common way to make a bundle look wrong, and it is silent — everything just renders too shiny
or too flat.

`flags` are producer hints a consumer may act on or ignore. Known in v1: `foliage`, `decal`,
`glass`, `emissiveWindows`, `terrainBlend`, `colorVariation`.

---

## 8. Textures, terrain, water and surfaces

**Textures** live at `<layers.textures><sha256>.png`, lossless PNG, deduped by content hash.
The file name **is** the lowercase hex SHA-256 of the file's own bytes; a validator checks
this, because it is the invariant that makes dedup verifiable rather than merely claimed.

**Elevation and water** are GeoTIFFs written by Carto's existing raster path, geo-referenced by
the same frame as the meshes (§2.3), described by the manifest's `rasters` block.

**Ground surfaces** are the meaningful upgrade over a bare heightmap:

- `terrain/surface_index.png` — 8-bit single-channel raster, one pixel per terrain cell, the
  value being a surface id.
- `terrain/surfaces/index.json` — maps that id to a name and a texture:
  ```jsonc
  { "0": { "name": "grass", "texture": "terrain/surfaces/grass.png" },
    "1": { "name": "dirt",  "texture": "terrain/surfaces/dirt.png" } }
  ```
- `terrain/surfaces/*.png` — the referenced ground textures.

The index raster shares the manifest's `rasters` geo-referencing.

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
python tools/test_bundle_contract.py   # 26 checks: the example is valid, its winding really
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
