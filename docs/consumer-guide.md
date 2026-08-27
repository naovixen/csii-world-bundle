# Building a consumer for the CS2 World Bundle

You have a directory with a `bundle.json` in it and you want a city out of it. This is the
route, in the order you should walk it.

**What you need:** the bundle, and `tools/csiibundle/` if you are writing Python (standard
library plus numpy; it never imports `bpy` or any engine API). If you are writing something
else, the package is the reference implementation of every algorithm named here — read the
module, port the algorithm.

**What is authoritative:** `docs/bundle-schema.md` is the contract, `docs/schema/*.json` the
fifteen machine-checkable schemas, and `tools/validate_bundle.py` the check. This guide is
navigation, not law: where it and the contract disagree, the contract wins.

**Start here, before writing any code:**

```sh
python tools/validate_bundle.py docs/example-bundle   # a complete bundle in 40 KB
python tools/validate_bundle.py /path/to/your/bundle  # and then yours
```

`docs/example-bundle/` is hand-authored, deterministic and carries **at least one record in
every layer the contract documents** — including one instance whose mesh is deliberately
missing and declared in `warnings.jsonl`, because handling that case is part of conforming.
Build against it first. A real city is 4.7 GiB and will not tell you which of your assumptions
is wrong.

---

## 1. The five things that will bite you

Ranked by how much of the city they ruin, from the record of what actually went wrong here.

1. **A `.glb` carries no metallic-roughness map.** Measured twice on the reference city, on
   independent samples of 1,449 and 638 glTF materials: **every single one** lacks a
   `metallicRoughnessTexture` and hard-codes `roughnessFactor` 0.9 — while **4,563 of the
   bundle's 4,784** `materials.json` entries ship a real per-texel `mask` whose **R is metallic
   and A is smoothness** (`roughness = 1 − A`). A glTF-only consumer renders every window,
   metal roof, wet carriageway and polished floor at one flat roughness. → `materials`, §7.1.
2. **Some materials have no textures at all, and they are not broken.** CS2 draws water and
   glass with shaders, so `template:WaterGeneric` and `DefaultGlassDoubleSided` serialise with
   no maps. Measured on the reference city: **218 of 4,784 materials carry no texture in any
   slot**, and `csiibundle.materials.shader_only_kind` names 206 of them — 109 glass, 51 water,
   40 foliage, 4 overlay, 2 placeholder, 12 unclassified. A glTF loader falls back to
   `baseColorFactor` `[1,1,1,1]`, which is why every swimming pool renders as a white rectangle
   until you give those templates an appearance. → `materials.shader_only_kind`, `water`, §7.1.
3. **Vegetation is authored grey.** Colour reaches a tree only through the per-instance
   `attrs.colorSet` applied through the material's `colorMask`, as a masked **multiply**. Skip
   it and every tree, bush and hedge is grey-brown and the city reads as dead. → `tints`, §7.2.
4. **The terrain heightmap walls off every tunnel.** The game hides that ground with a
   per-frame stencil pass rather than carving it, so `heightmap.png` legitimately contains the
   cover over every portal and sunken trench. `clipvolumes.jsonl` is what opens it. → `terrain`,
   §8c.
5. **A `.glb` is one STATE of a prefab.** `__teen`/`__elderly`/`__dead`/`__stump` and
   `__partial1`/`__partial2`/`__full` ship as derived meshes and `attrs.treeState` /
   `attrs.fullness` say which one an instance is in; `attrs.growthScale` is a uniform scale
   that is **not** folded into `scale` (a tree record has no `scale` field at all). On the
   reference city that is 153,991 of 697,256 trees in a non-adult state (119,237 Elderly,
   33,581 Teen, 1,173 Dead) and 299,972 carrying a growth scale, 130,880 of them below 1.0 —
   all drawing as full-grown adults if you ignore them. →
   `instances.mesh_candidates` / `instances.growth_scale`, §6.1.

None of these fail loudly. Every one of them produces a city that looks plausible and is wrong.

---

## 2. Layer tour

`bundle.json`'s `layers` object is the index. **Read every path from it; never hard-code one.**
A key that is absent means the feature is absent — degrade, do not error.

| layer key | file | what it is | §
|---|---|---|---|
| `instances` | `instances.jsonl` | every building, prop, tree, fence, decal and POI: prefab reference, world transform, attributes | 4 |
| `meshes` | `meshes/` | one `.glb` per unique prefab, deduped — a city of 1.28 M placements draws from 5,792 meshes | 6 |
| `materials` | `materials.json` | the PBR table the `.glb` material *names* join into | 7 |
| `textures` | `textures/` | PNGs named by their own SHA-256 | 8 |
| `colors` | `colors.json` | per-sub-mesh colour palettes with weights | 7.2 |
| `lights` | `lights.json` | **two record shapes in one file** — photometric lights and emissive colour slots | 7.2 |
| `networks` | `networks.jsonl` | the road *surface*: ripped mesh, or a centreline to extrude | 5 |
| `networkMeshes` | `networks/` | per-segment generated geometry | 5 |
| `lanes` | `lanes.jsonl` | the road *graph*: where a vehicle may go, how fast — what a simulation consumer wants most | 5A |
| `laneGeometry` + `lanePrefabs` + `laneMeshes` | `lanemeshes.jsonl`, `laneprefabs.json`, `lanemeshes/` | geometry drawn *along* a lane: cables, fences, hedges, road markings, wear tint — parts stored once plus per-lane bend parameters | 5B |
| `terrain` / `water` / `surfaceIndex` | `terrain/*.png` | 16-bit heightmap, water surface level, 8-bit surface ids | 8 |
| `splatmap` / `userSplatmap` / `surfaces` | `terrain/…` | the ground blend weights and the theme's ground textures | 8 |
| `terrainFine` | `terrain/heightmap_fine.png` | the ground the game draws near the camera — the heightmap with its road grading at 1.75 m (8192²). Mesh this one when present; absent on rips before 2026-08-16 or when the capture could not run (declared in `warnings.jsonl`) | 8 |
| `worldTerrain` | `terrain/world_heightmap.png` | the backdrop beyond the playable border. Its absence is a fact about the session, not a failure | 8 |
| `areas` + `areaPrefabs` | `areas.jsonl`, `areaprefabs.json` | painted ground: driveways, parking pavement, park paths, courts | 8b |
| `clipVolumes` | `clipvolumes.jsonl` | the volumes that open tunnel portals in your terrain | 8c |
| `grading` | `grading.json` | the game's resolved colour grading **and the light it sits on** | 8 |
| `warnings` | `warnings.jsonl` | what the producer could not deliver, declared | 10 |
| `vector` | `vector/` | the parent mod's unchanged GeoJSON/Shapefile/GeoTIFF export | 9 |

---

## 3. Pipeline order, and its two hard dependencies

Most of this is independent and can run in any order or in parallel. Two orderings are not
optional:

1. **Terrain before anything that drapes onto it** — surface areas (§8b) and lane markings
   (§5B). Both are *projections* in the game, with no height of their own; rebuilt as geometry
   they need one, and it comes from the surface underneath. Build terrain first and a marking
   that finds no road under it falls back to the ground; build it after and that marking keeps
   an authored height with nothing beneath it.
2. **Every mesh loaded before the material passes** — the per-instance tint (§7.2), the PBR
   mask wiring (§7.1) and the shader-only materials all operate on materials that only exist
   once their `.glb` is in. Run them as a final sweep, not per-mesh.

The reference adapter's order, which is a safe default:

```
instances  →  networks  →  terrain  →  lanes / lane markings  →  areas  →  material passes
                              │              │                     │
                              └──────────────┴─── drape target ────┘
```

Networks come before lane markings for the same reason terrain does: a marking drapes onto the
carriageway when there is one and onto the ground when there is not.

---

## 4. Must-implement tiers

Each tier is a city that looks right in one more way than the last. The module names are
`csiibundle`'s.

### Tier 0 — placement (`reader`, `frames`, `instances`, `glb`)

Every building, prop and tree in the right place at the right size. This is the whole of §2
and §4, and it is genuinely small: a manifest read, a JSONL stream, a matrix per record, a
glTF parse.

Do not skip, inside this tier:

- `instances.mesh_candidates()` — the state-variant join (item 5 above). It returns candidates
  in preference order because the step *set* is per-prefab: a two-step prefab has no
  `__partial2`, so walk down until a file exists. `instances.first_mesh_state(record, mesh_dir,
  probed)` is that walk, if your meshes are on disk: it takes a dict it fills with one file probe
  per distinct name, which over a million records is the difference between a pass and a stall.
- `instances.growth_scale()` — the sapling scale that is not in `scale`.
- `instances.should_draw()` — `attrs.state == "construction"` draws **nothing**. The game skips
  the building; the crane and site fence are separate records the bundle already carries.
  Drawing it puts a finished tower on a building site.
- `instances.wants_skirt()` — a building whose `elevationFlags` are absent or exactly
  `["OnGround"]` takes its `__base` foundation skirt. One that is stacked or lowered does not.
- `reader.read_declared_gaps()` — before treating a missing mesh as a defect. §10 is how the
  producer declares one honestly; an *undeclared* missing mesh is the real error.

### Tier 1 — appearance (`materials`, `water`, `tints`)

Items 1–3 of §1 above. This is the largest visible jump for the least code: no new geometry,
three passes over the material table.

### Tier 2 — ground (`png`, `splat`, `terrain`, `drape`, `areas`)

The heightmap, the ground blend, the clip volumes that open your tunnels, and the painted
surfaces draped onto the result.

Two rules here have each cost a visible defect:

- **The splatmap is not a partition.** Composite over the base in channel order — `R` dirt,
  `G` grass, then the four extras — at `saturate(w / 255 · splatMultiplier) ^ splatPower`.
  Normalising the weights turns every meadow half brown. `B` and `A` are **bit-packed**: two
  4-bit weights each, normalised by **15**, sampled with **point** filtering. Reading either as
  one 0–255 opacity merges two grounds and drops two more.
- **Only a *deep* clip band may carve.** A shallow one flattens and never deletes, and a face
  is deleted only when a volume contains **the whole face** — testing corners against the
  *union* of volumes tears holes at junctions, where four corners are covered by four different
  volumes while the interior escapes them all.

**Heightmap-native engines** (BeamNG class): do not replicate a mesh pipeline. Feed
`terrain/heightmap.png` to your terrain system directly — it is the game's own final,
road-flattened ground — and punch per-cell terrain holes where the clip volumes intersect it.
The network meshes then floor and wall every portal exactly as they do in game.

### Tier 3 — linear detail (`bezier`, `lanes`, `decals`, `networks`)

Road markings, wear tint, cables, fences and hedges. The one thing to port verbatim rather than
reinvent is the game's `GetTileCount`: `N = L / max(1, size.z)` quantised through the clip
window, with the `−0.0001` epsilon. A consumer that rounds differently gets a different fence
from the game on every short span, and `tilingCount == 0` means the part draws **nothing**.

### Tier 4 — simulation and light (`lanegraph`, `lights`, `colour`)

`lanegraph` is what a driving or traffic consumer wants and a pure geometry consumer can skip
entirely. `lights` and `colour` come last because the bundle ships albedo: both the
illumination and the grading on top of it are things the game does, not things the textures
carry.

---

## 5. The drape protocol

Two layers need to land on a surface — lane decals onto the road (§5B) and surface areas onto
the terrain (§8b) — and `csiibundle.drape` asks for the smallest possible interface, so you can
back it with whatever spatial index you already have (a BVH, a physics collider, a depth
pre-pass). If you have nothing, `HeightfieldSurface` ray-casts the §8 heightmap in numpy.

```python
class ProjectionSurface:
    def drop(self, x, y, z, half_up, half_down): ...   # nearest to z, in that window, or None
    def height(self, x, y): ...                        # topmost surface, or None
```

**All coordinates are Z-up** (X east, Y north, Z up) — a surface is a thing the *consumer*
owns, so the protocol speaks the consumer's frame and `drape_block` converts on the way in.

**The two queries are different questions and substituting one for the other is a real
defect.** `drop` takes the surface *nearest an expected height*, because a marking has a height
it should be near; taking the highest hit in the window instead snapped ~2.5% of samples onto
kerb tops, median walls and bridge railings, since a kerb 0.4 m above the road is still inside
a ±0.5 m reach and wins on height alone. `height` takes the topmost surface from far above,
because an area has no expected height — only the ground it is painted on.

### The lift ladder

Everything the game draws by projection lands on the same ground and cannot z-fight. Rebuilt as
geometry it can, so each layer is lifted clear of the one it paints over, in the order the game
composites them:

| rung | lift | what |
|---|---|---|
| terrain | 0.000 | the thing being painted on |
| lane tint | 0.008 | wear tint, draped onto the road per vertex |
| lane marking | 0.014 | markings and crosswalks |
| area | 0.020 | §8b painted surfaces |
| instance decal | 0.030 | §4.2a decal instances — bay markings, arrows |

**The order is CONTRACT; the magnitudes are TUNED.** They are all far below the tolerance of
the ground they sit on. Getting the order wrong is visible: an area lifted above the decal
instances standing on it paints the parking bays out of a parking lot, which is exactly what
0.020 against 0.015 did before it was fixed.

---

## 6. CONTRACT and TUNED

Every constant in `csiibundle` is tagged one of two ways, and the distinction is the whole
point of the tag:

- **`CONTRACT`** — fixed by the bundle format. Change it and your reader is wrong. Examples:
  the lift-ladder *order*; the `−0.0001` tile-count epsilon; `N = L / max(1, size.z)`; "a
  zero-instance decal draws nothing"; quaternions stored **`[x, y, z, w]`** (glTF order, not
  the `[w, x, y, z]` most maths libraries take); the `diag(-1, 1, 1)` mirror; the closed `kind`
  sets.
- **`TUNED`** — a presentation choice with a measured justification. Pick another value and you
  are still reading the bundle correctly. Examples: the lift *magnitudes*; the 2 cm decal shape
  tolerance; the mitre limit of 4; the drape's 1 mm dedup quantisation.

A few constants are a relationship rather than a number — `DIP_FRACTION` is `CONTRACT` *as a
relationship* (the vertical dip over a chord must not consume more than half the part's own
lift, or the rule permits exactly the burial the lift exists to prevent) and `TUNED` as the
value 0.5.

---

## 7. Worked example: one instance, end to end

Every number below is from `docs/example-bundle/`, so you can run it.

**1. Read the manifest and resolve the layer.** Never `os.path.join(bundle, "instances.jsonl")`.

```python
import csiibundle
from csiibundle import reader, instances, frames

BUNDLE = "docs/example-bundle"
assert reader.schema_version(BUNDLE) == csiibundle.SCHEMA_VERSION   # 2
path = reader.layer_path(BUNDLE, "instances")                       # -> ".../instances.jsonl"
```

**2. Take a record.**

```jsonc
{ "id": 1002, "assetId": "carto_example_cube", "kind": "building",
  "pos": [160.0, 12.5, -340.0],
  "rot": [0.0, 0.707106781, 0.0, 0.707106781],
  "attrs": { "name": "Example Building B", "level": 1, "elevation": 12.5,
             "height": 4.0, "state": "normal" } }
```

`rot` is a 90° yaw about +Y in the **left-handed** bundle frame, stored `[x, y, z, w]`. There
is no `scale`, which means `[1, 1, 1]`.

**3. Decide whether to draw it, and with which mesh.**

```python
instances.should_draw(record)        # True  — state "normal"
instances.mesh_candidates(record)    # ["carto_example_cube"] — no tree state, no fullness
instances.growth_scale(record)       # 1.0
instances.wants_skirt(record)        # True  — no elevationFlags means on the ground
instances.ground_level(record)       # 0.0   — pos[1] - attrs.elevation, NOT attrs.elevation
```

That last line is the one people get wrong: `attrs.elevation` is how far the object stands
**above** the ground, and it is signed — on the reference city 970 of 483,351 records carrying
it are negative, down to −86.3 m.

**4. Build the placement matrix.** The contract's formula (§2.2) is

```
v_world = T(pos) · R(rot) · S(scale) · diag(-1, 1, 1) · v_glb
```

The mirror is there because glTF mandates a right-handed frame, so **mesh payloads are stored
X-negated with their winding reversed**, while instance transforms, bounds and rasters are raw
left-handed bundle values. `frames.instance_matrix` composes exactly that:

```
[[ -0.  0.  1.  160.0 ]
 [  0.  1.  0.   12.5 ]
 [  1.  0.  0. -340.0 ]
 [  0.  0.  0.    1.0 ]]
```

Check it against a corner: the cube's `.glb` vertex `(2, 4, 2)` lands at
`(162.0, 16.5, −338.0)`. The X-mirror and the 90° yaw compose to the swap you can see in the
matrix.

**5. Look up the material.** The `.glb`'s glTF material **name** is the join key:
`carto_example_cube_mat` → `materials.json`. Take `baseColor` from there, and — the part a
glTF-only loader misses — take metallic from `mask.R` and roughness from `1 − mask.A` if a
`mask` is present, because the `.glb` carries neither.

**6. Apply the paint.** If the material has a `colorMask` and the instance has an
`attrs.colorSet`, tint per texel as a masked multiply (§7.2):

```
tinted = baseColor.rgb
tinted = lerp(tinted, tinted × colorSet[i].rgb, colorMask[i])   for i in R, G, B
```

Not a replacement — replacing flattens every surface to a solid tone.

**7. Place the skirt.** `wants_skirt` was true, so if `meshes/carto_example_cube__base.glb`
exists, place it with the *same* matrix. It is the foundation that closes the gap between a
building's flat bottom and sloping ground; it is referenced implicitly and never by an
`assetId` of its own, which is why a validator does not warn that nothing places it.

---

## Appendix A — LH-Y-up to RH-Z-up

The bundle frame is **left-handed, Y-up, X east, Y up, Z north**, in metres. Most DCC and
simulation engines are right-handed Z-up. The conversion is a single basis change, and
`csiibundle.frames` does all of it.

**Points.** `(x, y, z)_bundle → (x, z, y)_zup`. That is `frames.bundle_to_zup_point`, and it is
**its own inverse** — call it again to come back. The swap flips handedness, which is exactly
what is wanted: no separate mirror is needed at the point level.

**Transforms — two different matrices, do not mix them up.**

`frames.to_zup_transform(M)` conjugates a bundle-frame matrix by the same basis change:
`M_zup = P · M · P⁻¹`, with `P` the `(x, y, z) → (x, z, y)` permutation. Use it on a transform
whose *inputs are already Z-up*. For the worked example:

```
[[ -0.  1.  0.  160.0 ]
 [  1.  0.  0. -340.0 ]
 [  0.  0.  1.   12.5 ]
 [  0.  0.  0.    1.0 ]]
```

`frames.zup_instance_matrix(record, mesh_basis=None)` is the one you usually want: it takes
mesh vertices **as the `.glb` stores them** straight to Z-up world, mirror and all.

```
[[ -0.  0.  1.  160.0 ]
 [  1.  0.  0. -340.0 ]
 [  0.  1.  0.   12.5 ]
 [  0.  0.  0.    1.0 ]]
```

Check it on the same corner: `.glb` vertex `(2, 4, 2)` → `(162.0, −338.0, 16.5)`, which is
`bundle_to_zup_point` of the `(162.0, 16.5, −338.0)` from step 4. The two matrices differ
because one is a change of basis on a world transform and the other also carries the mesh
frame; applying the first to raw `.glb` vertices puts the building on its side.

**Meshes, and the trap.** The `.glb` payload is already right-handed (X-negated, winding
reversed) — that is the *only* place the bundle converts anything. So:

- If your glTF loader hands you vertices **as stored**, pass `mesh_basis=None` to
  `frames.zup_instance_matrix` and the `diag(-1, 1, 1)` term does the rest.
- If your loader has *already* applied its own Y-up→Z-up basis change to the vertices — many
  do, as `(x, y, z) → (x, −z, y)` — you must undo it, or you apply the change twice. Pass
  `mesh_basis=frames.GLTF_YUP_TO_ZUP` and `zup_instance_matrix` cancels it.

  This is **not** a bundle convention. It is a property of one class of loader, and it lives in
  the package only so it can be undone.

**Quaternions** are stored `[x, y, z, w]`, glTF's order, and are used **as stored, without
renormalising** — the same choice every engine's `quat_to_mat` makes, so a producer that ever
shipped a non-unit quaternion shows up as a scaled instance rather than being silently
corrected. (`validate_bundle.py` checks unit length to 1e-3.)

**Winding.** Triangle winding was reversed when the meshes were written, so glTF front faces
stay counter-clockwise in the right-handed mesh frame. Do not reverse it again.

---

## Appendix B — where to look next

| question | file |
|---|---|
| the format, normatively | `docs/bundle-schema.md` |
| machine-checkable shapes | `docs/schema/*.schema.json` (fifteen) |
| things that will surprise you | `docs/consumer-caveats.md` |
| what changed between rips | `docs/bundle-changelog.md` |
| the reference implementation | `tools/csiibundle/` — every module names the § it implements |
| a working adapter | `tools/blender_import_bundle.py` |
| the tools themselves | `tools/README.md` |
| what the producer knows it got wrong | your bundle's `warnings.jsonl`, and `grading.json`'s `diagnostics.unread` |
