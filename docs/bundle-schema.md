# The CS2 World Bundle — contract v2

**Status: frozen.** `schema: 2` is the seam between the csii-world-bundle mod (the sole producer)
and every consumer. *(v2 differs from v1 in exactly one record shape — the §8c clip-area
records carry their polygon inline; see §12.2. Everything else in this document is unchanged
from v1.)* This document plus `docs/schema/*.schema.json` is the *complete*
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
<bundle root>/                    # (layers key in parentheses)
  bundle.json                     # manifest — frame, geo-referencing, counts, layer index
  instances.jsonl                 # (instances)    one line per placed object
  networks.jsonl                  # (networks)     one line per network segment / node
  lanes.jsonl                     # (lanes)        one line per lane — the drivable graph (§5A)
  lanemeshes.jsonl                # (laneGeometry) one line per lane that draws geometry (§5B)
  laneprefabs.json                # (lanePrefabs)  lane prefab catalogue: parts + bend inputs (§5B)
  materials.json                  # (materials)    material table, keyed by materialId
  colors.json                     # (colors)       per-mesh colour palettes, keyed by sub-mesh assetId (§7.2)
  lights.json                     # (lights)       per-mesh light emitters, keyed by sub-mesh assetId (§7.2)
  meshes/<assetId>.glb            # (meshes)       unique prefab meshes, deduped by prefab
  networks/<assetId>.glb          # (networkMeshes) generated per-segment network meshes
  lanemeshes/<assetId>.glb        # (laneMeshes)   straight lane part meshes, stored once each (§5B)
  textures/<sha256>.png           # (textures)     textures, deduped by content hash
  terrain/heightmap.png           # (terrain)      elevation raster (§8)
  terrain/world_heightmap.png     # (worldTerrain) optional backdrop terrain beyond the border (§8)
  terrain/surface_index.png       # (surfaceIndex) per-cell ground-material id (§8)
  terrain/splatmap.png            # (splatmap)     optional ground-blend weights (§8)
  terrain/user_splatmap.png       # (userSplatmap) optional player-painted surface weights (§8)
  terrain/surfaces/index.json     # (surfaces)     id -> ground texture map, plus *.png textures
  terrain/water_level.png         # (water)        water surface level raster (§8)
  areas.jsonl                     # (areas)        rendered surface areas (§8b)
  areaprefabs.json                # (areaPrefabs)  area prefab catalogue (§8b)
  clipvolumes.jsonl               # (clipVolumes)  what opens tunnel portals and trenches (§8c)
  grading.json                    # (grading)      the game's resolved colour grading + lighting (§8)
  vector/                         # (vector)       unchanged vanilla Carto GeoJSON / Shapefile / GeoTIFF
  warnings.jsonl                  # (warnings)     advisory: what the producer skipped and why
```

Those paths are the producer's defaults, **not** part of the contract. A consumer must read
paths from `bundle.json`'s `layers` object and never hard-code them. Presence of a key in
`layers` means the layer exists; absence means it was not exported, and the consumer degrades
gracefully rather than failing.

**A bundle may be mixed-vintage, and one class of re-rip is destructive.** *(D5, 2026-08-12.)*
The producer writes into an existing bundle rather than a fresh one, and only deletes the layers
the run is about to rebuild — so a rip with some layers switched off leaves the others exactly
as an earlier rip left them, and `bundle.json` describes the union. That is deliberate, and it
is what makes a quick single-layer rip useful. The trap is that it holds for **layer** toggles
and not for the **kind** filter inside one: turning off Trees and re-ripping objects still
deletes `instances.jsonl` and all of `meshes/` first, then rebuilds them without trees, so the
trees are gone rather than preserved. Anyone reasoning from a bundle — including a producer
diagnosing its own output — should establish which rip each layer came from before comparing
two of them.

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
  "generator": "csii-world-bundle 1.0.16",
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
              "instances": 2, "networkSegments": 1, "warnings": 0,
              "lanes": 4, "laneGeometry": 1, "areas": 1, "clipVolumes": 0 },

  "layers": { "instances": "instances.jsonl", "networks": "networks.jsonl",
              "lanes": "lanes.jsonl", "laneGeometry": "lanemeshes.jsonl",
              "lanePrefabs": "laneprefabs.json",
              "materials": "materials.json", "colors": "colors.json", "lights": "lights.json",
              "meshes": "meshes/", "networkMeshes": "networks/", "laneMeshes": "lanemeshes/",
              "textures": "textures/",
              "terrain": "terrain/heightmap.png", "surfaceIndex": "terrain/surface_index.png",
              "splatmap": "terrain/splatmap.png", "surfaces": "terrain/surfaces/",
              "water": "terrain/water_level.png",
              "areas": "areas.jsonl", "areaPrefabs": "areaprefabs.json",
              "clipVolumes": "clipvolumes.jsonl", "warnings": "warnings.jsonl" },

  "rasters": { "resolution": 3.5, "size": [4096, 4096],
               "originWorld": [-7166.25, 0, 7166.25], "elevationUnits": "m",
               "elevationScale": 0.0625, "elevationOffset": 0.0,
               "splatmap": { "file": "terrain/splatmap.png", "size": [4096, 4096],
                             "resolution": [3.5, 3.5], "originWorld": [-7166.25, 0, 7166.25],
                             "channels": { "R": "dirt", "G": "grass", "B": "extra1", "A": "extra2",
                                           "base": "rock",
                                           "baseWeight": "max(0, 1 - min(1, R / 255 * splatMultiplier))" },
                             "splatMultiplier": 2.0, "splatPower": 1.0 } },

  "ripOptions": { "meshSource": "runtime", "lodCap": 0, "textureSizeCap": 2048,
                  "includeSubObjects": true, "includeVector": false }
}
```

Notes on individual fields:

- **`counts`** must equal reality. A mismatch is how a consumer detects a bundle truncated by
  a crash mid-export, so it is a hard validation error, not a warning. `meshes`/`textures`
  count files in their layer directory; `materials` counts keys; the `.jsonl` counts
  (`instances`, `networkSegments`, `lanes`, `laneGeometry`, `areas`, `clipVolumes`,
  `warnings`) count records. The four keys beyond the required five are present whenever
  their layer is declared *(added with the Phase 7 manifest, see §12.1)*.
- **`bounds`** encloses all exported content in the bundle frame, mesh extents included.
  Contributors: every instance and network placement expanded by its mesh's extents, every
  §8b area node, every §8c clip band bottom, every `lanes.jsonl` path point and
  `lanemeshes.jsonl` curve control point *(2026-08-09 — previously the two lane layers were
  only counted, so a lane-only bundle could not state bounds; control points bound the curve
  by the convex-hull property, part-mesh width is not expanded)*, and the playable-area
  raster extents (the
  world-backdrop rasters are deliberately excluded — including a 4×-map horizon would make
  `bounds` useless for framing the city). Bounds must *enclose*, not fit tightly; producers
  may expand conservatively (e.g. rotation-independent mesh radii).
- **`ripOptions.meshSource`** records how Phase 1's feasibility question actually resolved for
  this export: `runtime` (read from the live game), `offline` (joined from an offline
  `.assets` extraction), `mixed`, or `stub` (placeholder geometry substituted). Instance
  transforms are authoritative in every case, including `stub` — placement never depends on
  mesh readback.
- **`ripOptions`** describes **the run that produced the bundle**, not the settings at
  finalize time *(2026-08-09)*: new key `meshLimit` (int, 0 = uncapped — a capped rip also
  declares every capped-off prefab in §10), and `textureSizeCap` may be **negative**, encoding
  a run with textures deselected (materials ship tints/scalars only). The standalone finalize
  button still snapshots live settings, as the best available description.
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
| `id` | no | Producer-local integer (the ECS entity index), unique per **logical object**: the one sanctioned repetition is §4.1's stack expansion, where every section record of one stacked instance shares that instance's id. Present so `parent` can point at it. |
| `parent` | no | The producer-local id of the **owning entity**, when this record came from an owner's `SubObject` buffer. Resolves to an `instances.jsonl` id *or* a `networks.jsonl` id when the owner is itself exported (street furniture is owned by road edges). Owners the bundle does not carry — utility-net edges, net nodes — leave a `parent` that resolves to no record. `parent` is therefore **advisory grouping, never a required join**: records with the same `parent` belong to the same owner even when that owner is absent. |
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
exactly as it does for sub-objects above. All section records of one stacked instance share
that instance's `id` — the only sanctioned repetition of an id in `instances.jsonl`. Those records reference `<assetId>__stack_first`,
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

*(2026-08-09, audit #40 P12 — until this date none of `name`/`category`/`zone`/`zoning`/
`theme`/`brand`/`level`/`address` was ever actually produced. Now emitted, only-when-present:
`level` (int ≥ 1, buildings), `zone` (zone prefab name), `zoning`
(`residential|commercial|industrial|office`), `brand` (brand prefab name), `theme` (theme
prefab name), `address` (`[district|null, street, number]`, buildings), `name` (the player's
custom name only — generated display names are locale output and are not city data). New key
`fullness` (int 0–100, raw `Quantity` byte percentage; the game swaps stockpile sub-meshes at
33/50/66 — see §6.1). Still never produced, derive instead: `height`/`storey` (from the mesh),
`category` (from `kind` + `zoning`), `season` (already baked into every resolved `colorSet`).)*

**`state` is captured as a flag, and each value has a different consumer rule.** *(D4,
2026-08-12. Every rule below is `BatchInstanceSystem`'s own render branch, not an inference from
the name.)*

| `state` | what the game draws | what a consumer should do |
|---|---|---|
| `normal` | the building | draw it |
| `construction` | **nothing** — the game skips a building carrying `UnderConstruction` unless `m_NewPrefab != Entity.Null` (an *upgrade* keeps drawing the old building while the new one is paid for) | **do not draw the building.** The site is not empty: the crane and site fence are real entities with their own prefabs and ship as ordinary instance records, so a bundle already contains everything that is visible there |
| `condemned` | the building, unchanged — `Condemned` has **no reader in the rendering namespace at all**; it is a simulation flag about eviction | draw it exactly like `normal`. It is metadata, not appearance |
| `abandoned` | the building, with the game's own abandoned colour/décor variant already resolved into the shipped `colorSet` | draw it normally |
| `destroyed` | a collapse pose plus rubble; once `Destroyed.m_Cleared >= 0` the mesh is skipped entirely | **neither the pose nor the rubble is shipped** — the collapse is a runtime deformation and no rubble mesh exists as a prefab. Treat as `normal` geometry, or skip the record; do not expect ruins |

On the reference city: 8,889 `normal` and 7 `condemned`, and none of the other three — which is
why the three unshipped appearances above are a documented rule rather than a measured one.

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

**Scattering your own grass is game-faithful, and the splatmap is the input the game itself
uses.** *(D3, 2026-08-12 — this section previously implied grass was simply absent, which read
as a fidelity gap it is not.)* `VegetationRenderSystem` binds the whole splatmap into a single
`FoliageVFX` graph (`m_FoliageVFX.SetTexture("Terrain SplatMap", …)`) and scatters from it with
**zero per-instance data** — there is no list of grass positions anywhere in the game to rip, so
a consumer that scatters its own from the same weights is doing what the game does, not
inventing something. Drive it from the **grass** channel, and take which channel that is from
`rasters.splatmap.channels` — do not assume an index. (It is `G`; `R` is dirt. An earlier
revision of this producer had those two inverted, which is exactly why the block publishes the
map instead of leaving it to be guessed — see §8.) The channel the VFX graph samples lives
inside the `VisualEffectAsset`, which is not readable, so this is the faithful input rather than
a byte-for-byte reproduction of the game's own scatter.

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

The schema stays as frozen; this section records which of its options the ripper takes,
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

Layer key `lanes`. `networks.jsonl` and `networks/` are the road *surface*: where the asphalt
is and what it is made of. `lanes.jsonl` is the road *graph*: where a vehicle may go, in which
direction and how fast. A level built from the surface alone is scenery; the graph is what makes it drivable, and
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

Three pieces (layer keys `laneMeshes`, `lanePrefabs`, `laneGeometry` respectively):

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
         "decal": true,                       //   the tile length. `decal` (optional): the game
         "decalRole": "marking"               //   projects this part onto the ground — see below.
       } ] } }                                //   `decalRole` (optional, 2026-08-09):
                                              //   "marking" | "wear"; absent = unclassified
   ```

   *(2026-08-09, audit #40.)* **`decalRole` publishes the marking-vs-wear distinction** the
   paragraph below describes, so a consumer no longer has to string-match asset ids to pick a
   drape lift (the reference importer matched `"Line"`/`"Crosswalk"`, which fails on mods). The
   producer classifies from the part material's tint alpha — the wear surfaces (`CarLane`,
   `GravelLane`, …) carry `tint.a < 1`, every marking surface carries no tint — and omits the
   key when no material resolves. Treat an absent key as unknown, not as either class.

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
   markings a hair above the wear tint so the two never z-fight). The drape *places* the
   quad; the texture-repeat rule in the bend algorithm below *paints* it — both are
   consumer-side, and skipping the repeat stretches every dash and zebra stripe over the
   whole lane (audit §39). Like every flattened decal (§4.2a), a `decal`
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
     "tunnel": true,                   // owning network runs below ground (the game draws the
                                       // lane on its Tunnel cutaway layer). Omitted = surface.
     "wear": 3.2 }                     // LaneCondition.m_Wear, 0..10; the game tints decal
                                       // parts by wear/10 (worn asphalt/markings, normal
                                       // view). Omitted = 0. (2026-08-09, audit #40 P10.)
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
- **`decal` parts** (any `tilingCount`): the tile-count rule above still gates drawing —
  when `tilingCount > 0` and `clip == 0` (span shorter than half a pattern period) the game
  draws **zero instances**; emit nothing. Otherwise the *geometry* is always one box
  stretched chord-to-chord over the span (`arcPerZ = spanArc`) — but the **texture repeats,
  never stretches** (`BatchDataHelpers.BuildCurveMatrix`, audit §39): the along-lane
  parameter at arc position `a` is `n = N · a / spanArc` with `N = spanArc /
  max(1, meshLength)`, quantised `N = round(N·tilingCount)/tilingCount = clip/tilingCount`
  when `tilingCount ≠ 0`. Authored V at that row is `v_near + (v_far − v_near) · frac(n)`
  (U unchanged — the quad's UVs are an arbitrary atlas rect after the §4.2a `m_TextureArea`
  rescale; never assume V spans [0, 1], and a plain REPEAT sampler cannot substitute for the
  `frac`). A consumer baking UVs must split the mesh at every integer of `n` — duplicate the
  row, one copy at `frac = 1` closing the period, one at `frac = 0` opening the next — or
  triangles smear the texture across the wrap seam. The last period is partial when `N` is
  fractional, exactly as the game's per-pixel mapping produces. *(Corrected 2026-08-09 —
  this algorithm previously read "one stretched tile" for decals with no UV rule, and the
  reference importer stretched one texture copy over the whole lane: a median 5.0× stretch
  on dashed lane lines, 0.22× squeeze on yield lines, 15,704 instances in the acceptance
  city. Consumer algorithm only — keys, data and schema (v2) unchanged; audit §39.)*
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
the bundle renders tunnels in full (the §8c clip volumes open their portals), and a
consumer wanting the surface world only filters on the tag. *(Pre-2026-08-07 bundles
excluded them as "cutaway view only".)*

> **A lane's `tunnel` and a segment's `form: "tunnel"` are different predicates. Never equate
> them, and never join one to the other.** *(D1, 2026-08-12.)* The lane tag is the game's
> `BatchInstanceSystem.IsNetOwnerTunnel` — `cmin(Elevation) < 0`, i.e. the owning net dips below
> ground **at all** (for a node owner, any connected edge does). The segment form is
> `CompositionFlags.General.Tunnel`, which the game raises only for a composition deep enough to
> be built as a tunnel (−2 or −3 × `ElevationLimit`). Everything between those two — a road on a
> gentle downgrade, the approach ramp either side of a portal — is `tunnel: true` on its lanes
> and **not** `form: "tunnel"` on its segment. On the reference city that is 3,214 tagged lanes
> against 309 tunnel-form segments, an order of magnitude apart. Use the lane tag to answer "is
> this lane below ground"; use the segment form to answer "is this segment a tunnel", which is
> the one that pairs with §8c's clip volumes.

Decal-flagged parts are **included** and tagged (see
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

  *(2026-08-09, audit #40 P6.)* **Network meshes now carry authored normals and tangents too.**
  Until this date the composition bend rebuilt normals from faces — a documented deviation that
  bevelled authored creases (kerb edges, barrier rims) and shipped no `TANGENT` for
  normal-mapped road materials. The producer now transports the pieces' authored
  `MeshNormal`/`MeshTangent` through the same bend the positions take, rotating each by the
  curve frame (normal by the inverse-transpose, tangent by the Jacobian, `w` passed through),
  and implements the game's `SmoothTopNormal` rule. Face rebuilding survives only for pieces
  that carry no authored buffer, counted per rip. The frame's orthonormalization is a counted
  approximation: the game's exact choice lives in a compiled shader.
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

*(2026-08-09, audit #40 P7 + GAP 6.)* **The states a consumer could not previously draw now
ship as derived files**, joining the `__base` / `__stack_*` family:

| file | join on |
|---|---|
| `meshes/<assetId>__dead.glb`, `__stump.glb` | `attrs.treeState` |
| `meshes/<assetId>__partial1.glb`, `__partial2.glb`, `__full.glb` | `attrs.fullness` |

Each derived file is a **complete replacement** for `<assetId>.glb`, not an overlay: it already
contains every unconditional part the prefab also draws. **The base state has no suffix** — a
healthy adult tree and an empty stockpile *are* `<assetId>.glb`. A file exists only where the
prefab flags that state, so absence means the game has no such variant either and the base file
is the right answer. Fullness thresholds follow the game's own steps (`>66` full, `>33`
partial2, `>0` partial1 on a three-step prefab; the step set is per-prefab). Capped-off prefabs
declare their derived ids in §10 like stack parts do. Until this date 1,173 dead trees in a
full city had no mesh to swap to and rendered as healthy adults.

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

Layer keys `colors` and `lights`. Both are keyed by a **sub-mesh `assetId`** — the glTF node name inside the owning prefab's
`.glb` (§6) — because that is where CS2 keeps them. Measured over a full city: colour palettes
sit on 3,984 sub-meshes covering 1,132,295 instances and on no object prefab at all; procedural
lights likewise, on 1,184 sub-meshes covering 153,548 instances.

```jsonc
// colors.json
{ "NA_ResidentialHigh01_Mesh": [
    { "channels": [[0.8,0.7,0.6,1], [0.3,0.3,0.3,1], [1,1,1,1]], "probability": 100 } ] }

// lights.json — a procedural (emissive-material) entry and a punctual (HDRP light) entry
{ "StreetlightSingle02_Mesh": [
    { "slot": 0, "color": [1,0.87,0.68,1], "colorOff": [0,0,0,1],
      "purpose": "StreetLight", "responseSpeed": 0.5, "animation": -1 } ],
  "StreetlightSingle02": [
    { "kind": "punctual", "effect": "Streetlight Spot", "position": [0, 8.2, 1.1],
      "rotation": [0.5, 0.5, -0.5, 0.5], "parentMesh": 0, "range": 24.0,
      "type": "Spot", "spotAngle": 120.0, "intensity": 4000.0, "intensityUnit": "Lumen",
      "temperature": 2700.0, "color": [1,0.87,0.68,1] } ] }
```

*(2026-08-09, audit #40 P8 — the entry families split. Until this date the `range`/
`temperature` shown on procedural entries were documented but **never produced**: the producer
read `LightEffectData` off the sub-mesh entity, where the game never stores it. Punctual HDRP
lights live on **EffectPrefab** entities joined through the object prefab's `Effect` buffer,
with placement in `EffectSource.EffectSettings` — so, contrary to the older text below, CS2
**does** store a position for them.)* **Punctual entries** carry `kind: "punctual"` and are
keyed by the **object prefab's assetId** (joining `instances.jsonl`/`meshes/`, unlike
procedural entries' sub-mesh key). Fields: `effect`, `position` (local; relative to the
sub-mesh named by `parentMesh` when ≥ 0, else the object frame), `rotation`, `range`; optional
`intensityScale` (per-placement, when ≠ 1), `finalColor` (resolved linear HDR RGBA),
`colorChannel` (when the light takes the instance colorSet channel), `type`, `color` (authored
linear RGBA), `intensity` + `intensityUnit`, `temperature` (K, when used),
`spotShape`/`spotAngle`/`innerSpotPercentage` (Spot), `areaShape` (Area). Procedural entries
are unchanged and remain position-less behaviors of the emissive material.

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
own data and are exact. `colorOff` is the unlit colour, and `responseSpeed` how fast it crosses
between the two — which is what makes a day/night cycle look right rather than switching.

**Where a light goes depends on which family it is, and the two answers are opposite.** *(D5,
corrected 2026-08-12 — this paragraph said flatly "there is no position, because CS2 does not
store one", which the P8 split above had already disproved for half the layer. It was true of
the only family that existed when it was written.)*

- **Procedural** entries genuinely have no position, and none is invented. The light *is* the
  emissive material: it binds to a **material slot** (`slot`), and its place in the world is
  wherever the geometry using that material is. Place each emitter at the centroid of the
  primitive using the corresponding material in the same `.glb`. Inventing a transform here
  would be inventing data, on the same grounds as §5.1.
- **Punctual** entries (`kind: "punctual"`) **do** carry a position, because the game stores
  one: they are HDRP lights on `EffectPrefab` entities, placed by
  `EffectSource.EffectSettings`. Use the shipped `position`/`rotation` — relative to the
  sub-mesh named by `parentMesh` when it is ≥ 0, else the object frame — and do not fall back
  to the centroid rule above, which would move every streetlamp to the middle of its pole.

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
- **Terrain that the game hides rather than carves** — the ground covering tunnel portals and
  sunken trenches — is **not** in these rasters. It cannot be: the game does not carve it out
  of its heightmap. A trench's ground is merely clamped into `[lane, lane + 30 m]` and a
  shallow tube's cover is shifted *up*, and every frame a stencil pre-pass (`RoadClip`)
  discards the covering pixels on screen. `heightmap.png` therefore legitimately contains
  ground that walls off every tunnel mouth and sunken road, and **`clipvolumes.jsonl` (§8c) is
  what opens it**. A consumer that builds terrain without reading §8c gets a solid wall across
  every underground network — that is the single most visible thing to get wrong in this
  section. *(Until 2026-08-08 this was shipped precomputed as `terrain/cutout.png`; see §12.1
  for why that was retired.)*
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

  *(Added 2026-08-09, audit #40 P4/P5 — two more optional `rasters` blocks, no grid of their
  own:)* **`rasters.water`** carries the authored water appearance a consumer otherwise has to
  guess: `seaLevel` (world metres — **the sea datum, not the water surface**; see below) and
  `WaterRenderSettingsPrefab`'s values verbatim —
  `waterColor`/`scatteringColor` (RGBA 0–1), `absorptionDistance`, `maxRefractionDistance`,
  `smoothnessClose`/`smoothnessDistant`/`smoothnessRange`, scattering scalars, foam, caustics
  and wave parameters; the game's only bind-time transform is
  `endSmoothness = min(distant, close)`. **`rasters.terrainTiling`** carries the ground texture
  scales: `playableArea` `[x, z]`, per-layer (`grass`/`dirt`/`rock`) `tiling` `[near, mid, far]`
  (repeats across the playable area, as bound to `_Terrain*TextureTiling`), the precomputed
  `metresPerRepeat` `[near, mid, far]` (= `playableArea.x / tiling`), `lodBlend` camera-distance
  ranges, `extras.extra1 .. extra4` params (`dirtOverride`, `splatRange`, `blurDepth`,
  `heightOffset`; extras tile with the grass layer), and the triplanar constants. Never guess
  metres-per-repeat — the §5B lane-tiling lesson, repeated on terrain.

  **`seaLevel` is the sea datum, and a consumer must never seat water on it.** *(Corrected
  2026-08-12, WATER-2.)* `WaterSystem.SeaLevel` is what backdrop/ocean water is reset to, the
  `seaLevel` uniform bound into the water compute shader (sea propagation, evaporation,
  restore-water-in-sea), the plane the renderer measures camera depth against for
  `smoothnessRange`, and the editor's sea-level slider. It is **not** a statement about where
  water stands in the city. A map whose water is all inland leaves it far from any surface
  there: the rip that produced this document's reference bundle read `15.0` while the mapped
  water measured 508.6–528.3 m (median 521.0), and the game's own default is 511.7.
  **`terrain/water_level.png` (`layers.water`) is the authoritative surface** and is unaffected.
  Use `seaLevel` for what it is — the datum the appearance parameters above are relative to.

  A consumer that draws one ground at one scale will show a visible grid: the game blends
  **three** scales per ground by camera distance, which is its own anti-tiling, and that is what
  `tiling`/`lodBlend` are for. All **four** extras carry params — an earlier producer shipped
  only `extra1`/`extra2` on the belief that the user splatmap could not weight the other two,
  which the packed-nibble decode disproved (extra4 is the Sand players paint beaches with).

  **The channel-to-ground assignment is published, in `rasters.splatmap.channels`. Read it;
  do not calibrate it.** *(Amended 2026-08-08 — this block previously told consumers to derive
  the map from slope and roadbed contrast, and the reference importer's calibration got three
  of four channels wrong on a real bundle.)* The block names a ground per channel using
  `terrain/surfaces/index.json`'s own keys, so each joins straight to a texture:

  ```jsonc
  "channels": { "R": "dirt", "G": "grass",
                "B": "extra1:extra2 (packed, low:high nibble)",
                "A": "extra3:extra4 (packed, low:high nibble)",
                "packed": { "B.low": "extra1", "B.high": "extra2",
                            "A.low": "extra3", "A.high": "extra4",
                            "decode": "extraN weight = (channel & 15) or (channel >> 4), each 0..15; normalise by 15, NOT by 255" },
                "base": "rock", "baseWeight": "max(0, 1 - min(1, R / 255 * splatMultiplier))" }
  ```

  **`B` and `A` are bit-packed: two 4-bit weights each (corrected 2026-08-10, audit SPLAT-6).**
  That is how the game fits **four** paintable extras into an `R8G8_UNorm` user splatmap — the
  packing `TerrainMaterialSystem.PackToBAChannels` writes, four brush components at bit offsets
  0, 4, 8 and 12 across the two bytes. The four extras are the game's four paint tools, named by
  its own locale: **01 Dry, 02 Lush, 03 Dirt, 04 Sand**.

  Reading `B` or `A` as one 0–255 opacity **merges two grounds into one and drops the other two
  entirely** — it is what made a city's painted beaches render as forest floor. Sample the packed
  channels with **point/nearest** filtering, never bilinear: halfway between `0x0F` and `0x10`
  decodes to a nibble pair unrelated to either neighbour. The game's own user splatmap is created
  with `filterMode = FilterMode.Point`. `R` and `G` are genuine continuous weights and should
  still be sampled smoothly.

  A texture format cannot reveal a packing convention: the `R8G8` user splatmap carries four
  weights, not two.

  A channel the producer cannot name ships as `"unknown"`; drop it from the blend rather than
  guessing. `channelProvenance` states, per channel, the measurement behind it. If the whole
  `channels` object is absent the bundle predates this amendment; the map above is still the
  right assumption. **A bundle that declares `"R": "grass", "G": "dirt"` was ripped before
  2026-08-10 and its map is inverted — see below.**

  **`R` is dirt and `G` is grass.** Slope cannot tell the two apart and must not be used to:
  rock is `R`'s own residual, so **both** soil weights fall with slope together (0–5°: R 251.6,
  G 201.7 → 45–90°: R 9.4, G 0.5). Two measurements settle it, both resting on where a ground can
  physically exist:

  - **`G` is grass** — exactly **0** on every texel under 1 m or more of water and 0.5 on 45°+
    cliffs, against 201.7 on flat land. Identically zero on every lakebed and every cliff.
  - **`R` is dirt** — averages **249.9 underwater**, rising with depth. A weight that blankets
    the seabed is sediment.

  **How to blend it.** Composite over `base` in order — `R` (dirt), `G` (grass), then the four
  unpacked extras `extra1..extra4`. The two soil channels use
  `saturate(w / 255 · splatMultiplier)^splatPower`; each extra uses
  `saturate(nibble / 15 · splatMultiplier)^splatPower` — **normalise a nibble by 15, not 255**.
  The order is what makes it read: dirt blankets everything including the lakebed, grass covers
  the land on top of it, then the player's paint over that.

  **Do not normalise into a partition.** `R` and `G` are both near-saturated on flat land and
  routinely sum past 255, so normalising turns every meadow half brown. Rock is the residual of
  **`R` alone** — where the dirt weight falls away, the base shows through — not of all six.

  One more thing a consumer must get right: **do not treat a roadbed as dirt.** The blend has no
  roadbed response at all — it weights the same ground under roadbeds as under lawns; the dirt
  look beside roads is the §5B wear decals and §8b surface areas, not this map.

  Consumers should sample it bilinearly (it is weights, not ids) and prefer it over per-cell
  classification from `surface_index.png` wherever it is present; `surface_index.png` remains
  the *semantic* roadbed mask (physics, the sink rule), not the visual one. When the file or
  its `rasters.splatmap` block is absent, the pre-2026-08-07 behaviour (per-cell
  classification) is the documented fallback.
- **`grading.json`** (`layers.grading`, optional) — the game's **resolved HDRP colour grading** at rip
  time, read from `VolumeManager`'s post-blend volume stack: `exposure`, `whiteBalance`,
  `colorAdjustments` (postExposure, contrast, colorFilter, hueShift, saturation),
  `shadowsMidtonesHighlights`, `tonemapping`, `vignette`, and `lighting`.

  **Why it exists.** CS2 authors ground albedo near-grey — the shipped rock basecolor measures
  sRGB saturation 0.111 and sand 0.143 — and the vivid on-screen palette is post-processing
  applied on top, not anything in the textures. A consumer drawing the shipped albedo faithfully
  gets a grey world and, without this block, no way to know what the game did to it.

  **Apply it as a display transform; never bake it into the shipped textures.** The textures stay
  the faithful rip. `order` states the chain. The values are a **snapshot of one moment** —
  grading follows weather and time of day, so a rip at dusk records dusk (`weatherDependent`).
  A consumer that wants raw albedo should ignore the block entirely.

  **`lighting`** *(added 2026-08-10)* — the illumination the grading sits on top of: `sun` and
  `moon` (colour, colour temperature and whether it is in use, intensity in lux, travel direction
  plus elevation/azimuth in degrees, shadow state), `sky` (`PhysicallyBasedSky` exposure and
  zenith/horizon/ground tints), `indirect` (the bounce multipliers), and `time` (the in-game
  clock, latitude and longitude at rip). It is here because replaying the grading is not enough:
  measured against three in-game surfaces, the chain above lands within a few percent on red and
  green and leaves a consistent deficit on blue, and a near-neutral pavement (albedo linear
  0.1019/0.1018/0.1010) still renders warm beige in game. No property of an albedo can do that,
  so the missing chroma is the light — and a consumer lighting its scene white could not have
  known.

  **`tonemapping` carries the LUT fields** *(added 2026-08-10)* — `lutContribution`, `hasLut`,
  and a `lut` sub-block when one is bound. `Game.Rendering.LightingSystem` builds a
  priority-1000 volume whose tonemapping is `External` with a 3D LUT blended per time of day,
  gated on `DayNightCycleData.UseLUT`. Every rip so far has read `ACES` because that gate was
  off, but a bundle recording only the mode could not tell a consumer whether it got lucky. The
  LUT itself is **not exported** — the blended result is a GPU 3D render texture — so
  `mode: External` with `hasLut: true` means the grading block is *incomplete* and says so.

  **`exposure` carries `limitMin`/`limitMax`.** The metered value is not reachable (HDRP keeps it
  behind internal API), but these are the clamps the game drives per time of day, so they bracket
  the one term a consumer must otherwise choose blind.

  *(Declared as `layers.grading` since 2026-08-12 — see §12.3. It was briefly a root file found
  by name; that was a contract violation, because §1's rule is that a consumer reads every path
  from `layers` and never hard-codes one, so a layer absent from the index is a layer a
  conforming consumer cannot find.)*

- **`terrain/user_splatmap.png`** (`layers.userSplatmap`, optional) — RGBA8 (R/G meaningful): the player's painted
  surface weights for the paintable extra grounds, read back from
  `TerrainMaterialSystem.UserSplatmap`. *(Added 2026-08-07.)* Written only when any texel is
  non-zero; geo-referenced by `rasters.userSplatmap` (same conventions as `rasters.splatmap`).
  Its channels are packed the same way as the splatmap's `B`/`A` — `R` carries
  `extra1` (low nibble) and `extra2` (high), `G` carries `extra3` (low) and `extra4` (high).
  The source is `R8G8_UNorm`, two channels wide, and **all four extras are live**: the nibble
  packing is how they fit.
- **`terrain/world_heightmap.png`** (`layers.worldTerrain`, optional) — 16-bit grayscale, the **backdrop terrain**
  beyond the playable border (`TerrainSystem.worldHeightmap`, read losslessly via GPU
  readback). *(Added 2026-08-07.)* Geo-referenced by `rasters.worldTerrain` (texel-centre
  origin over `worldOffset…worldOffset + worldSize`); the playable area is a sub-rect of it,
  and where they overlap the main heightmap's samples are authoritative. Same elevation decode
  as the main heightmap — the game's height cascade blits both into one seamless surface, so a
  consumer can (and the reference importer should) verify continuity across the playable
  border as a self-check.

  **Its absence is a fact about the session, not a producer failure or a missing feature.**
  *(D6, 2026-08-12.)* The backdrop heightmap is a GPU resource the game keeps resident only
  when it needs it, so a rip that finds it unloaded ships no layer and says so in the rip
  report — `World terrain: no backdrop heightmap resident; layer omitted`. Treat it exactly as
  §1 says: the key is absent, the feature is absent, degrade. The report line is what
  distinguishes "the game had nothing to give" from "the producer dropped it", and it is worth
  reading before concluding the second. `layers.worldWater` is a different case: the schema
  reserves the key and the geo-referencing for a backdrop water raster, but **no producer path
  emits one today** — nothing has ever written that layer, so a consumer will not encounter it.
- The theme's ground textures under `terrain/surfaces/` now include tangent **normal maps**
  (`grass_normal.png`, `dirt_normal.png`, `rock_normal.png`) and, when the theme binds them,
  the four paintable extras (`extra1..4.png` + `extra1..4_normal.png`). *(Added 2026-08-07;
  all optional.)* Normal maps are decoded from the game's two-channel packing where present,
  like every material normal (§7.1).

The `rasters` block gains two keys for this (`elevationScale`, `elevationOffset` — see the
schema); they are v1-compatible optional manifest additions, required by the validator when a
terrain or water layer is declared. `worldTerrain`/`worldWater` (the 4× backdrop) keep the
same value semantics at their own size when present.

The producer writes the identical `rasters` object at `terrain/rasters.json` when the terrain
layers are ripped, and the Phase 7 finalize step lifts it into the manifest verbatim.
`terrain/rasters.json` is a producer intermediate, not a contract layer: a consumer of a
complete bundle reads the manifest only.

### 8b. Surface areas — `areas.jsonl` + `areaprefabs.json` *(added 2026-08-07, see §12.1)*

Layer keys `areas` and `areaPrefabs`. The painted ground patches the game draws through its
area system: **house driveways, parking-lot pavement, park paths, sports courts, campsite
clearings**. These surfaces belong to no other layer. A parking lot is a *meshless* building prefab — its instance record places
nothing visible, its bay markings are §4.2a decal instances — and the pavement itself is a
`SurfacePrefab` area the building spawns as a sub-area, triangulated on the area grid and
rendered by `AreaBatchSystem` with the prefab's `RenderedArea` material. A consumer without
this layer shows grass wherever the game shows pavement.

- **`areas.jsonl`** — one JSON object per rendered surface area:

  ```jsonc
  { "id": 482113, "prefab": "concrete_surface_01", "owner": 173512,
    "nodes": [[x,y,z], …],            // bundle-frame world positions (§2.2), NOT mirrored
    "triangles": [[a,b,c], …],        // indices into nodes, the game's own triangulation
    "heightRanges": [[min,max], …] }  // optional, per triangle — see below
  ```

  `owner`, when present, is the instance `id` of the building/object that spawned the area.

  **A triangle is a footprint, not a surface — conform it to the terrain.** *(Corrected
  2026-08-08; this section previously said the game grades terrain under areas, and it does
  not. See audit §30.)* The game never draws these triangles: it draws a **prism** per
  triangle (`AreaRenderSystem.CreateMesh`, literally named "Area triangle volume") and
  projects the surface material onto whatever ground is inside it, as a decal
  (`RenderedArea.m_DecalLayerMask = DecalLayers.Terrain`). Node heights are the polygon's own
  corner heights and the ground between them goes its own way — on a full-city rip the
  terrain rises through the flat triangle on 26.6% of triangles and 61.4% of areas, by 11 cm
  at p95 and 55 cm at p99. **A consumer must drape the triangle onto the §8 heightmap**
  (subdivide until it follows) rather than drawing the plane. Nothing flattens the ground
  underneath: the one mechanism that does — `TerrainArea`/`CullAreasJob` — belongs to
  `LotPrefab`, not to these surfaces, and whatever it did is already baked into §8's
  heightmap.

  `heightRanges`, when present, is the game's own `Triangle.m_HeightRange` per triangle, in
  the same order as `triangles`: the min and max of the terrain (and water) height over that
  triangle's footprint **relative to the triangle's own plane**, in metres. It is the vertical
  band the game's prism spans, so it states how far a drape has to reach. Omitted when every
  range on that area is zero (all its ground is flat); on a full-city rip 91.4% of areas carry
  one.

  **It is a guide, not a substitute for the heightmap.** Checked against an independent
  recomputation of `GetHeightRange` over `terrain/heightmap.png` (86,233 triangles): the game's
  band *contains* the recomputed one on 83.3%, with a median slack of ~11 mm in the widening
  direction — as the rule predicts, because the game's scanline conservatively covers cells
  just outside the triangle and a building-owned area widens the band to include its own plane
  ± the prefab's `heightOffset`. But 0.52% come out **narrower** than a direct walk of the
  heightmap, most plausibly because the game recomputes the range only when the area changes,
  so it can lag a later terrain edit. A consumer that needs the surface to sit on the ground
  must sample the ground.

- **`areaprefabs.json`** — one entry per referenced `prefab` key:

  ```jsonc
  { "concrete_surface_01": { "name": "Concrete Surface 01",
      "color": [r,g,b,a],                              // material _BaseColor, optional
      "uvScale": 0.2,                                  // optional, see below
      "heightOffset": 0.0,                             // optional, metres
      "texture": "terrain/surfaces/area_concrete_surface_01.png",     // optional
      "roundness": 0.0, "rendererPriority": 0,         // 2026-08-09, always present
      "expandAmount": 0.5, "decalLayers": ["Terrain"], //   "
      "normalMap": "terrain/surfaces/area_concrete_surface_01_n.png",  // optional PBR block
      "maskMap":   "terrain/surfaces/area_concrete_surface_01_m.png",  //   (2026-08-09)
      "metallic": 0.0, "smoothness": 0.5,
      "edgeFadeRange": [0,0], "edgeNormalRange": [0,0], "edgeNoise": [0,0] } }
  ```

  The texture is the area material's base map, captured as bound; tiling is world-planar.
  `uvScale` is the authored rate: **uv = world position × `uvScale`** — it is
  `RenderedArea.m_UVScale`, which the game hands the shader as `colossal_UVScale`, so 0.2
  means one repeat every 5 m. *(Added 2026-08-08; before that a consumer had to guess a
  metres-per-repeat. Absent in older bundles — fall back to the game's own field default,
  0.2.)* `heightOffset` is `RenderedAreaData.m_HeightOffset`, how far the projection prism
  reaches beyond the terrain's own departure from the triangle (§8b `heightRanges`); a
  consumer draping the surface as geometry does not need it, one building a real decal
  projector does. All keys are optional; a consumer falls back `texture` → `color` → its own
  neutral.

  *(2026-08-09, audit #40 P11 — the catalogue used to stop at the diffuse, so pavement and
  plazas rendered flat-lit with guessed specular and overlapping areas z-fought.)* Four keys
  are **always** present because the game pushes them for both material versions:
  `roundness`, `rendererPriority` (**the overlap draw order — without it a consumer has no
  rule for which of two overlapping areas paints on top**), `expandAmount` (metres, already
  derived by the producer so a consumer needn't know the game's per-area-type node distance)
  and `decalLayers` (flag names, the same vocabulary an instance's own decal layers use). The
  **PBR block** — `normalMap`, `maskMap`, `metallic`, `smoothness`, the `*Opacity` /
  `*AlphaSource` pair and the `edge*` feathering ranges — is present only where the game has
  the values: read from the authored override fields on an override-version surface, and read
  back off the baked material otherwise (which is what covers vanilla pavement). An
  unauthored map on an override surface is a *cleared* slot in the game too, so its key is
  simply absent. `lodBias` is deliberately **not** shipped: the game never applies it to
  rendering, only to a per-triangle distance cull the bundle does not reproduce.

  `color`+`texture` come from the surface's baked material when it has one, and otherwise
  from the authored `RenderedArea` override fields — the game's own two-version rule
  (`AreaBatchSystem`; audit §36). An entry may instead carry `"unresolved": true` *(added
  2026-08-08)*: the save references a surface asset — an asset pack, in practice — that was
  **not loaded in the rip session**, so there was no material to read and the game itself
  drew those areas with its grey Missing Area fallback. The bundle faithfully records that
  session; the cure is enabling the pack in-game and re-ripping, not consumer-side invention.
  A consumer should surface these entries by `name` rather than silently rendering neutral.
  An unresolved entry may also carry `"prefabId"`: the game's own identity string for the
  missing asset, `SurfacePrefab:Name (hash)` — the hash is `Hash128.Calculate` over the
  asset's platform ID (or its GUID). Observed to differ per asset within one pack, so it
  identifies the asset, not the pack.

  Areas whose prefab resolves but has **no `RenderedArea`** are not written at all *(as of
  2026-08-08)*: the game structurally never paints them (no `Batch` archetype component, and
  every `AreaBatchSystem` draw query requires it). The one case in practice is the map
  editor's Clip Surface, whose triangles ship through `clipvolumes.jsonl` (§8c) instead —
  older bundles carried those 266 areas here too, and consumers drew grey boxes over tunnel
  portals the game does not draw (audit §36).

Areas are ground paint, like decals (§4.2a): never generate collision from them, and draw
them just above the terrain to avoid z-fighting. **They go UNDER the §4.2a decal instances
standing on them** — a parking lot's bay markings are decal instances, and an area lifted
above them paints the bays out of the lot. The reference importer's ladder is terrain 0 →
area 6 mm → lane tint 8 mm → lane marking 14 mm → decal instance 20 mm; the order is what
matters, not the values. It places areas under `--areas` (implied by `--everything`), and
conforms them to `--terrain` when that layer is also built.

---

### 8c. Clip volumes — `clipvolumes.jsonl` *(added 2026-08-08, see §12.1)*

Layer key `clipVolumes`. **Read this if you build terrain.** The game hides the ground over
tunnel portals and sunken trenches with a per-frame stencil pass rather than carving it out of
the heightmap (§8), so a
consumer that meshes `heightmap.png` alone walls off every underground network. These are the
volumes that pass clips with; intersect them against your own mesh and delete what falls inside.

Until 2026-08-08 the bundle shipped that intersection precomputed, as `terrain/cutout.png`. It
was **retired**: a top-down 3.5 m mask cannot express a volume open at one height and closed at
another — a portal driven into a hillside got a square, height-thresholded hole instead of a
clean mouth — and it was taken against the terrain as shipped, so it went stale the moment a
consumer edited the ground. Shipping the volumes moves the intersection to the side that knows
its own resolution.

One line per clipping **strip** — not per section: a junction node contributes several strips,
and a strip is the unit the game actually clips with.

```jsonc
{"id": 106037, "kind": "edge",
 "left":  [[x,y,z],[x,y,z],[x,y,z],[x,y,z]],   // cubic Bézier control points, bundle frame
 "right": [[x,y,z],[x,y,z],[x,y,z],[x,y,z]],
 "bottom": -0.267, "top": 12.3}

{"id": 220481, "kind": "area", "area": 220481, "bottom": -0.3,
 "nodes": [[x,y,z], …], "triangles": [[a,b,c], …]}   // §8b encoding, inline since 2026-08-09
```

- **`id`** — the net entity (`kind` `edge`/`node`/`orphan`) or area entity this strip belongs
  to; net kinds join to `networks.jsonl`. **Not unique**: several strips share one id.
- **`left`, `right`** — the strip's two boundary curves, as authored *after* the composition's
  width trim, so they bound exactly the ribbon the producer's own mask was stamped from.
- **`bottom`, `top`** — the clip band, in metres **relative to the lane surface**, not absolute.
  The lane surface is recoverable from the curves: at parameter `t` it is the mean of the two
  boundaries' `y`. A cell is clipped where the strip covers it *and* the terrain stands within
  `(lane + bottom, lane + top]`.

  *(D5, 2026-08-12 — the mean is a **reference**, not the road surface.)* It is a centreline
  value and it does not tilt: the producer's own stamp holds one `laneY` constant across each
  quad of arc (the mean of that quad's four boundary corners), so on a superelevated or
  cross-falling carriageway the band hangs level while the road it belongs to leans. The
  discrepancy is half the cross-fall — centimetres on ordinary roads, against a band that is
  metres deep, which is why this is a documented reference rather than a defect. Two consequences
  worth knowing: a consumer that wants the road surface itself should interpolate
  `left.y`→`right.y` across the strip rather than reuse this mean, and one reasoning about
  marginal cells should note the error is signed — a level band sits too low over the high side
  of the camber and too high over the low side. *(The game rasterises its own clip strip on the
  GPU from the same two curves; whether that pass interpolates height laterally is not readable
  from the managed assemblies, so it is not claimed here either way.)*
- **`top` absent** means unbounded — an authored clip surface hides whatever covers it, however
  high. *(Until 2026-08-09 `kind: "area"` records always omitted it; that read the game's
  `GenerateAreaClipMeshJob` wrongly — the job builds a **closed prism**: a bottom ring at
  `min(0, aggMin(heightRange)) − 0.3` and a top ring **and cap** at
  `max(0, aggMax(heightRange)) + 0.3`, both aggregated over the area's triangles. Area records
  now carry per-area `bottom` and `top` with exactly those values, relative to each triangle's
  plane like every band. Consumers must stop treating area clips as open-topped; terrain above
  the top is NOT clipped. Audit #40 P3/T-F3.)*
- **`kind: "area"`** records carry their own polygon: `nodes` (bundle-frame world positions,
  §8b's encoding) and `triangles` (index triplets into `nodes`). `area` is the source area
  entity's id, kept as provenance — it joins to nothing, because the game never *paints* a clip
  surface and `areas.jsonl` therefore does not carry it (§8b, audit §36). *(Until 2026-08-09
  these records carried no geometry and referenced `areas.jsonl` instead; see §12.2.)*

The band is the game's own, verbatim (`CullRoadsJob.AddSegment` + `GenerateClipDataJob`): base =
the composition's edge-height extremes plus `TerrainComposition.m_ClipHeightOffset`; the top
raised to `ElevationLimit·3` (12 m roads, 6 m pathways) for tunnel portals and open trenches;
both ends then expanded 0.3 m outward. **The floor therefore reaches ~0.3 m *below* the lane and
clips the roadbed shell itself** — exactly as in-game, where the road/tunnel meshes cover that
hole; consumers get those same meshes from `networks.jsonl`, and driving-sim consumers take
collision from them, not from terrain. This also means every ordinary surface road clips the
thin shell around its own lane surface — that is the game's everyday guarantee that no terrain
crest pokes through a carriageway, and a consumer inherits it by honouring the band. A volume
may span a whole road and open only the covered part of it. **The band test is what decides,
not the strip's extent.** *(Bundles ripped before 2026-08-08 carried a producer-invented
`bottom` of `+0.5`; that lip is retired, and consumers must honour the record as shipped.)*

A cut cell's `surface_index.png` id is meaningless — the cell is not drawn at all.

**The invariant a mesh consumer owes** *(2026-08-11, audit §41)*. The game discards **per
pixel**, so `discarded ⊆ covered by the road mesh` holds by construction and a see-through gap
beside a road is impossible in-game. A mesh at any finite resolution can only approximate that
boundary, and the two directions are not symmetric: deleting beyond a volume leaves **sky**,
while deleting less leaves terrain under a road that already hides it. So **delete a face only
when the volume contains the whole face** — test the face's own interior, not a sample point,
and not its corners. Testing the corners against the *union* of volumes is the trap: at a
junction four corners can be covered by four different volumes while the face's interior escapes
them all, which reads as contained and tears a hole. Report deleted area against the clipped area
you measured; the ratio must never exceed 1.00.

Containment alone under-deletes, and the difference is invisible under a surface road but stands
in the open under a trench or portal band (whose top is `ElevationLimit·3`). Close it by moving
the hole's rim onto the volume's own wall — outward for a rim vertex inside the band, inward for
one outside — rather than by loosening the deletion rule. A strip narrower than two of your faces
holds no whole face and has no rim either; count what that leaves rather than letting it pass
silently.

**Heightmap-native consumers** (BeamNG-class engines with their own terrain system): do not
replicate a mesh pipeline. Feed `terrain/heightmap.png` to the engine's terrain directly — it is
the game's own final, road-flattened ground — and punch terrain holes where these volumes
intersect it (BeamNG supports per-cell terrain holes); the network meshes then floor and wall
every portal, trench and tunnel exactly as they do in-game.

---

## 9. `vector/`

The unchanged vanilla Carto export — GeoJSON, Shapefile, GeoTIFF, exactly as Carto has always
produced it. The ripper is strictly additive; nothing about the vector output changes, and it
is simply relocated under the bundle when both run together — as of 2026-08-09 that relocation
is real: the one-button rip's Vector stage (default off) writes the vanilla exporter's
per-format subdirectories under the bundle, `vector/GeoJSON/`, `vector/Shapefile/`,
`vector/GeoTIFF/`, and `layers.vector`/`ripOptions.includeVector` then declare it (until then
the stage wrote to the user's configured export directory and the layer could never exist).
The stage pre-cleans `vector/` and leaves the user's vanilla export directory setting
untouched. Its contents are outside this contract; see the Carto wiki.

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

Warning kinds are owned per pass, and each pass rewrites only its own kinds (so stage re-runs
cannot erase another stage's declarations): the instance pass owns `prefab` and `instance`, the
mesh pass `mesh`/`material`/`texture`, the lane pass `lane`, and the network pass every kind
**prefixed `network`** (`networkMesh`, `networkMaterial`, `networkTexture`, `networkConform`).
The network prefix is load-bearing: a network warning names a cross-section piece that is *not*
an `assetId` in `meshes/`, so filing it as `mesh` would let the next mesh rip delete it.
*(2026-08-09 — until this date the network stage wrote no warnings at all; unreadable lane-part
meshes and no-entry materials reached only the rip report.)* Two further additions from
the frame-sliced passes and the mesh cap:

- `instance` / `lane` lines carry **`id`** (the would-be record's `id`) instead of `assetId` —
  an entity removed by the simulation mid-rip no longer resolves to a prefab. Reasons:
  `"removed by the simulation mid-rip; no record written"` and
  `"failed to record (<ExceptionType>); no record written — see the log"`.
- A mesh-capped rip (Options → mesh limit ≠ All) declares every capped-off prefab:
  `{"kind":"mesh","assetId":…,"reason":"skipped by the mesh cap (N most-used prefabs);
  instances kept with placement only"}` — including a stacked prefab's derived `__stack_*` ids,
  since instances reference the part files. A capped rip therefore still validates.

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
python tools/test_bundle_contract.py   # the contract suite: the example is valid, its winding
                                       # really follows §2.2, and each way to break the
                                       # contract is actually caught
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

**Clip volumes added as §8c; `terrain/cutout.png` retired (2026-08-08).** The mask was the
intersection of the game's clip volumes with one heightmap, at 3.5 m, from directly above.
`clipvolumes.jsonl` ships the volumes instead, and the mask is **no longer written**.

Grounds. A top-down mask cannot express a volume open at one height and closed at another, so a
portal driven into a hillside got a square, height-thresholded hole; and the intersection was
taken against the terrain as shipped, so it was stale the moment a consumer edited the ground.
Measured (audit §7.3.1), **99.87% of the mask's set cells came from the section-ribbon walk,
whose curves nothing else in the bundle carries** — `networks.jsonl` has no curve field,
`lanes.jsonl` carries tessellated lane polylines rather than composition boundaries, and
`networks/` holds baked surface meshes. The remaining 0.13% is the `Clip_Surface` polygons,
which `areas.jsonl` already has, so those records carry a band and a join key rather than a
second copy of the geometry.

**Unusually for a change here, it costs bytes** — roughly 4× the 19,659 B mask on a real city,
and the mask's own bytes are the only thing recovered. It was taken on fidelity, and on there
being no consumer of `cutout.png` in the field to keep it for; this repo's own importer moves in
the same commit.

Removing a layer is v1-compatible by §12's own rule — a consumer reads paths from `layers` and
treats an absent layer as an absent feature. Be clear about what that degradation looks like
here, though: a consumer that only implemented the mask will find `layers.cutout` gone and build
terrain with **every tunnel walled off**, silently and plausibly. That is the loudest failure
available for a removal, and it is not very loud. Implement §8c.

**Clip band floor made game-true; the `+0.5` lip retired (2026-08-08).** A value change inside
an existing §8c field, not a shape change: `bottom` now carries the game's own
`cmin(edgeHeights) + m_ClipHeightOffset.x − 0.3` instead of a producer-invented constant
`+0.5`. The lip was meant to keep a "drivable bed" under the cut; in practice it left a ring of
terrain proud of the lane at every portal mouth and disabled the everyday ±0.3 m shell clip
that keeps crests out of surface carriageways — and the bed it preserved serves no consumer,
because road/tunnel meshes cover the hole (and provide the collision) everywhere, exactly as
in-game. In the same change the reference harness dropped its `cutout.png` reading path and its
`bottom` default entirely: `clipvolumes.jsonl` is mandatory and the record value is honoured as
shipped. Bundles ripped before this date still carry `bottom: 0.5` and simply keep their lip.

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

**The Phase 7 manifest: layer keys, counts and `rasters` sub-blocks catch up with the layers
(2026-08-08, Phase 7).** The first `bundle.json` a producer actually assembles made the closed
manifest objects catch up with every layer added since the freeze:

- **`layers` gains** `lanes` (§5A), `laneGeometry` + `lanePrefabs` + `laneMeshes` (§5B),
  `colors` + `lights` (§7.2), `splatmap` + `userSplatmap` (§8), `areas` + `areaPrefabs` (§8b)
  and `clipVolumes` (§8c). Every one of these layers was already specified and shipped at a
  conventional path; the manifest can now declare them, and a consumer must read their paths
  from `layers` like every other layer.
- **`layers.cutout` is deleted** from the closed schema. The layer was retired on 2026-08-08
  (above); no manifest was ever produced while the key existed, so nothing can carry it.
- **`counts` gains optional** `lanes`, `laneGeometry`, `areas` and `clipVolumes`, present
  whenever the matching layer is declared, so truncation detection covers the record layers
  that did not exist at the freeze.
- **`rasters` gains optional sub-blocks** `splatmap`, `userSplatmap`, `worldTerrain` and
  `worldWater` — the per-raster geo-referencing §8 already specified prose-first, now given a
  schema (`file`, `size`, `resolution [x,z]`, texel-centre `originWorld`, plus the
  channel-map keys).

`counts` and `rasters` are closed objects, so by §12's letter the additions require
`schema: 2`. They are amended in place on the `colorMask` grounds: no bundle conforming to v1
had ever been produced end-to-end — the manifest itself did not exist before this change — so
there is no v1 consumer in the field to break, and a consumer written against the
pre-amendment schema fails loudly at its validator, which is the promised failure mode. This
is the last amendment that may lean on "no complete bundle exists"; from the first finalized
bundle onward, closed-object changes take `schema: 2`.

**`id` and `parent` described correctly (2026-08-08, the Phase 7 acceptance run).** The first
end-to-end validation of a real city showed §4's field table describing both joins more
strictly than the producer ever implemented them; the bytes were right and the description
wrong, so like the earlier `elevation` correction this changes no format. `id` said "unique
integer" while §4.1's stack expansion has always emitted one record per section, all sharing
the stacked instance's id — measured, every one of the 1,263 repeated ids in a full city is a
pure stack-section group. `parent` said "the id of the owning instance" while owners have
always included road edges (which resolve in `networks.jsonl`, not `instances.jsonl`) and
entities the bundle deliberately does not carry (utility-net edges, net nodes — 43,911
references over one city, of which 22,952 resolve to network records and the rest to no record
at all). The table now states the real invariants; the validator accepts stack-section id
sharing, resolves `parent` against both record layers, and reports unresolvable parents as one
aggregated warning rather than an error, since §4 has always made `parent` ignorable.

**2026-08-09 (audit #40, the gap-close pass).** Additive producer catch-ups, no format
break: §8c `kind: "area"` records gain per-area `bottom`/`top` (the game's closed prism — the
"always omit top" sentence was a misreading, corrected in place); §10 gains the `instance` and
`lane` warning kinds (carrying `id`, not `assetId`) and the mesh-cap declaration rule; the
manifest `rasters` gains optional `water` and `terrainTiling` blocks. The mesh-limit default
became All — the 50 cap was a dev-measurement default that made a fresh install's rip drop
~99% of distinct meshes undeclared.

**2026-08-09 (audit #40, gap-close wave 2).** More additive catch-ups: §7.2 lights.json
gains the `kind: "punctual"` entry family (photometry that was documented but never produced —
the producer read the wrong entity; the procedural example's `range`/`temperature` are gone);
§4.2's long-documented attrs are now actually emitted (`level`, `zone`, `zoning`, `brand`,
`theme`, `address`, custom `name`) plus new `fullness`; §5B lane-geometry records gain
optional `wear` (0..10; tint = wear/10); manifest `bounds` now encloses the lane layers;
`ripOptions` gains `meshLimit`, may carry a negative `textureSizeCap`, and describes the run
rather than finalize-time settings; §9's vector relocation is implemented.

**2026-08-09 (audit #40, gap-close wave 3).** The last additive catch-ups: network `.glb`s
carry **authored** normals and tangents through the composition bend instead of face-rebuilt
normals (§6); §5B decal catalogue parts gain optional `decalRole` (`marking`|`wear`), retiring
the asset-id string heuristic a consumer needed for drape lifts; §6.1 state variants ship as
`__dead`/`__stump`/`__partial1`/`__partial2`/`__full` derived files; §8b `areaprefabs.json`
gains the always-present `roundness`/`rendererPriority`/`expandAmount`/`decalLayers` and the
optional PBR + edge-feathering block; §10 gains the network stage's `network*` warning kinds.

### 12.2 `schema: 2` (2026-08-09) — clip-area records carry their polygon

The one change: a §8c `kind: "area"` record now carries `nodes` + `triangles` (in §8b's
encoding), and its `area` field is provenance rather than a join key. Nothing else differs
from v1.

Why a version and not an amendment: two v1 changes made on 2026-08-08 contradicted each other.
§36 removed clip-surface areas from `areas.jsonl` (correctly — the game structurally never
paints them), while §8c defined clip-area records as geometry-by-reference into that same
layer. The first full rip carrying both changes shipped 229 clip volumes that referenced
records no longer present — caught by the validator on the Phase 8 acceptance run, exactly the
loud failure the contract promises. The §12.1 amnesty had already closed ("from the first
finalized bundle onward, closed-object changes take `schema: 2`"), and clip records are closed
objects, so the repair takes the version bump. A v1 consumer given v2 data fails at the
manifest's `schema` const; the only bundles carrying the broken intermediate form were never
valid under either version.

### 12.3 Amendments to `schema: 2`

**`layers.grading` added (2026-08-12), and it does not take `schema: 3`.**

`grading.json` had been written at the bundle root since 2026-08-10 but never declared in
`layers`, on the reasoning recorded in §8 at the time: `layers` is `additionalProperties: false`
in `docs/schema/bundle.schema.json`, so any new key in it looked like a closed-object change.

That reasoning was wrong, and §12 says so in its own words: adding a new layer key is listed
verbatim among the changes a producer may make *within* a version. The two statements are about
different things, and the distinction is what the closure is for:

- **A closed object protects a consumer that would otherwise drop a value silently.** That is
  the `colorMask` case (§12.1) — a v1 consumer applied `colors.json` with no mask and rendered a
  wrong city while believing it had read everything.
- **`layers` is presence-indexed by construction.** §1 already requires a consumer to treat an
  absent key as an absent feature, and §12 requires it to ignore layers it does not implement. A
  consumer handed `layers.grading` and no notion of grading behaves exactly as one handed a
  bundle ripped with grading capture unavailable — which is a state the contract already
  defines. Nothing is dropped silently, so nothing needs a loud failure.

The `additionalProperties: false` on `layers` therefore stays, but as what it actually is: a
**producer-side typo guard** that forces a new layer to be named in the schema deliberately, not
a consumer-compatibility boundary. The compatibility boundary is §12's list.

The other half of the fix was ordering. `GradingWriter.Write` ran *after*
`BundleWriter.WriteManifest`, and the manifest's layer index is a scan of what is on disk — so
even with the key defined, a first rip into an empty directory would have declared every layer
except this one, and a second rip would have declared the *previous* rip's grading file. The
writes are now grading-then-manifest.

Nothing about the file's contents changed. A consumer already reading `grading.json` by name
keeps working; one following §1 can now find it.
