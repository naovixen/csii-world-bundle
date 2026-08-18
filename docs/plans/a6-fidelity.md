# A6 — one live regression and six fidelity defects from the A5 loads

A5 closed most of the performance work (audit §64): the build is 19 minutes, the level mounts
5.2 GiB, and **the imposter fix held** — 153 imposter builds this session against 2,652, and the
1–2 FPS collapse is gone. What is left is one performance item that is NOT fixed, and six
fidelity ones. Reported 2026-08-18 across two loads:

0. **The GPU still runs out of memory.** FPS decays 40–60 → 15 over 30 minutes. Not the same
   cause as the A5 one, and not fixed by it.
1. **Bushes collide.** Only trees should, and only their trunks.
2. **Buildings lose their windows a few hundred metres out** while the building itself is still
   drawn — they read as construction sites.
3. **Some buildings render at wrong contrast or completely black.**
4. **Vegetation stands in the carriageway.** Asked for explicitly as a playability deviation, not
   a fidelity fix.
5. **Areas float above the ground you drive on, and paint over the roads.** Reported 2026-08-18;
   two independent consumer-side causes, both measured below.
6. **A lot of prop locations are floating / off.** Reported 2026-08-18. The transform is exact —
   the cause is that we place a building's props after dropping the building.

Everything below the horizontal rule in each section is measured, not assumed. Where a rule is
still open it says so; **none of these should be fixed by inventing a consumer-side rule** (see
CLAUDE.md — the terrain cut cost seven guesses before someone read `TerrainSystem`).

---

## A6.0 The GPU is still running out of memory — 8,193 failed allocations

### What is measured

The imposter fix held: **153 `Creating imposters for:` lines this session**, exactly the number the
gate allows, against 2,652 before. The load went 396.982 s → **252.468 s** (`objects` 373.484 →
228.942). And yet:

```
  0.61|D|platform|  Physical memory: 31.92 GB
  5.46|I|d3d12|     Dedicated video memory: 8018 MB
329.84|E|d3d12u::GenericImage::create| D3D12: CreateResource failed for generic image
      (dim=Tex2D, w=64, h=64, mips=7, format=98, hr=0x8007000e, totalFailures=5)
   …  totalFailures 17, 33, 65, 513, 8193, 16385, 32769, 65537 …
2105.36|E|d3d12u::GenericImage::create| … totalFailures=131073 …
```

`hr=0x8007000e` is **E_OUTOFMEMORY**, and the log prints only on a power-of-two backoff — so those
20 lines are **131,073 failed GPU allocations**, starting the moment loading finishes (329 s) and
still going at 35 minutes. Each one "leaves the image empty" and the engine retries. That is the
decay: not a leak in our data, but a card that has nothing left to give.

**The level mounts 5,225 MiB** (3,906 meshes + 1,318 textures, `validate.checks.resident`) on an
**8,018 MB** card — and BeamNG needs the rest for its own render targets, shadow cascades, the
vehicle and the UI. Note from the same log that the UI canvas is **3440×1440**: this machine is
ultrawide 1440p, which is 3.2× the pixels of the 1080p `level_budget.py` assumes, and a deferred
G-buffer plus cascades at that size is not a rounding error. `RESIDENT_BUDGET_MIB = 6144` passed
this level at 5,225 and should not have.

**It grows as you explore**, which is why it takes 30 minutes: BeamNG loads a shape's buffers the
first time it is drawn and does not evict, so the 5,225 MiB is only fully paid once you have seen
the whole map. That matches the report exactly, and it means the fix is *total* footprint, not
what is on screen.

### The levers — MEASURED 2026-08-18, and three of the five estimates were wrong (audit §65)

`RESIDENT_BUDGET_MIB` is now `resident_budget_mib(vram, pixels)`, derived from vanilla's biggest
level and BeamNG's own DX12 floor, with both inputs read out of the user's `beamng.log`. It
**brackets**: 3,473 MiB is known to fit on this machine, 5,224 MiB is known to fail, and the band
between is honestly unknown. A5's 5,224 is exactly on the failure line.

The real calibration, which had never been taken: **west_coast_usa, the biggest level BeamNG
ships, mounts 1,375 MiB.** The old constant was 4.5x that; A5 shipped 3.8x it.

| lever | measured | |
|---|---:|---|
| `texture_max` + `texture_max_normal` 512 → 256 | **−497 MiB** | colour −282, normal −215. 5,166 of 7,661 DDS are ALREADY 256, which is why the "three quarters of 1,083" estimate was high |
| terrain `.png` → DDS | **−0 MiB** | PNG on disk 235 MiB ≈ BC in VRAM 235 MiB. Not a residency lever at all — it is a first-load cook stall |
| drop LOD2 | **−147 MiB** | the 1.31x is exact, but the last level is only 6.6 % of triangles; the 31 % is nearly all LOD1 |
| runtime imposters | **190 MiB** | 4,555 cached 256x128 atlases, built 62–155 s — before the 329 s onset. Not the cause |
| paint variants | 377 MiB | 3,002 variant shapes against 1,232 base shapes holding 1,847 MiB |

**Meshes are 3,906 MiB of the 5,224, so textures alone cannot reach the budget even at zero** —
cs2 objects 2,224, networks 787, areas 339, lanegeometry 281, skirts 141, markings 129. And there
is no free geometry to reclaim: the `.dae` streams are already POSITION + NORMAL + one TEXCOORD,
no tangents, no second UV. 66 B/triangle is poor vertex sharing across hard edges, not padding.

So the cheapest cut, both texture caps to 256, moves 5,224 → **4,727 MiB**: out of "known to
fail" and into the unmeasured band, with no geometry touched. Anything beyond that costs visible
fidelity, which makes it a decision rather than a fix.

### The trap

Do not spend anything on `objects 228.942s` until this is settled — it is the same data being
loaded, and shrinking it shrinks both.

---

## A6.1 Bushes collide, and they are wearing a tree's trunk

### What is measured

`kind: "tree"` in the bundle is **all vegetation**, not trees:

| | items | prefabs |
|---|---:|---:|
| `kind: tree` total | 686,641 | 75 |
| …carrying `attrs.growth` / `attrs.treeState` | **310,454** | 32 |
| …carrying neither | **376,187** | 43 |

The second group is `pd_Long_Grass` (314,222 items), `PlantPot02`, `FlowerBushCity02`,
`GreenBushCity01/02/03`, `FlowerPot01/02`, `Flowerbed01`, `GreenBushWild01/02`.

Today `stages/instances.FOREST_COLLIDABLE_MIN_HEIGHT = 0.5` decides collision **by shape height**,
so a 1.5 m city bush is collidable; and `stages/shapes._collider` gives every `kind == "tree"`
shape a `collision.trunk_cylinder`, so a bush gets a "trunk" fitted to a thing that has no trunk.

### The rule, and it is the game's

**A CS2 `TreePrefab` has a `Tree` component and therefore growth**; a plant/bush prefab does not.
The bundle already carries the evidence per instance — `attrs.growth`, `attrs.growthScale`,
`attrs.treeState` — and per prefab, as the `__teen` / `__elderly` / `__dead` / `__stump` mesh
states that only tree prefabs ship:

```
OakTree01        growth 0.796, growthScale 1.2829        OakTree01__teen/__elderly/__dead/__stump.glb
CoconutPalmTree01 growth 0.792, treeState "Teen"
GreenBushCity01  (no growth)                             GreenBushCity01.glb only
pd_Long_Grass    (no growth)                             pd_Long_Grass.glb only
```

Note the rule **disagrees with the names and is right to**: `VFP_EvergreenBushWildLarge` (8,806
items) has growth, so CS2 treats it as a tree. A name match would have got that backwards. Do not
match names.

**Confirm before building**: read `Game.Prefabs.TreePrefab` / `PlantPrefab` and the `Tree`
component in `tools/decomp` and check that growth is exactly the discriminator — the bundle
evidence is consistent with it but it is inference, not the source.

### The work

* `csiibundle.instances`: one predicate, `is_tree(record)` (or `vegetation_kind`), so both
  consumers get the same answer — the Blender adapter has the same bush problem.
* `stages/instances`: a plant ships `collidable: false` whatever its height; the height rule stays
  for trees. Report the split by count.
* `stages/shapes._collider`: only a real tree gets `trunk_cylinder`; a plant gets `()`.
* Check `trunk_cylinder` actually finds the trunk on the 32 tree prefabs (it searches material
  runs) — a canopy-width cylinder on an oak is the same defect one step along.

### The gate

* the count: ~310k collidable items, not 1,035,444;
* **and it should show up in `beamng.log`**: `Loaded Static Collision … Tris: 89,096,868, Memory:
  3720.81MB` with `collisionCache … 1,035,444 instances … using 3062.80mb`. If plants stop
  colliding, that block must fall by roughly the plant share. This is the one fidelity fix with a
  measurable performance dividend, so take the number.
* drive into a hedge; drive over long grass.

---

## A6.2 Buildings lose their windows at ~105 m — the construction-site look

### What is measured

The shapes stage already prints the cause, and has since A4:

```
building - 1057 shapes, CS2 draws the median one to 13,008 m, this level to 1,332 m
tree     -  272 shapes, CS2 draws the median one to  4,097 m, this level to   416 m
prop     - 2916 shapes, CS2 draws the median one to  1,024 m, this level to   105 m
```

**A prop dies at 105 m; the building it is bolted to lives to 1,332 m.** And props are not
free-standing decoration — **472,141 of 504,116 (94 %) carry a `parent`**, and 19,396 of the
19,397 window-ish props (`ACMachineWindows01/02`, `EU_WindowGuard*`, `EU_WindowOrnament*`) are
parented to a building. Strip those off a façade and what is left is a bare shell.

**A hypothesis already killed:** it is not the generated LOD levels dropping the window geometry.
400 multi-level building shapes were inspected and **0** lose a `*Win*` material between their
nearest and farthest level — `csiibundle.decimate` keeps every material run, as documented.

### Why now and not in A4

The ratio is CS2's own (1,024 vs 13,008 m — the game culls a prop 12.7× nearer than a building).
`lod_detail_scale` divides **both** by 10, which preserves the ratio and destroys the
relationship: at CS2's 1,024 m a window frame is sub-pixel and losing it costs nothing; at 105 m
it is plainly visible. A single global multiplier is the wrong instrument for two things whose
absolute distances differ by an order of magnitude.

### The rule to find

**Does CS2 cull a sub-object independently of its owner, or with it?** `PreCullingSystem` and the
`Owner` link (audit: "Owner (not Attached) is the ownership link") are where the answer is. Two
outcomes, and they lead to different fixes:

* **Culled with the owner** → port it: a record with a `parent` inherits its parent's cull
  distance, clamped up, and the construction-site effect is a defect we introduced by not porting
  the rule. This is the expected answer and the cheap fix.
* **Culled independently** → then CS2 also loses its window frames, just at 1,024 m, and the
  defect is `lod_detail_scale` alone. The fix is then a per-kind scale (props scaled less than
  buildings), declared as tuning, with the ratio chosen so a façade prop survives as far as the
  façade is legible.

Do not guess between these. `tools/check_instances_vs_game.py` is the harness that already samples
the game's own predicates; extend it.

### The work (shape of it, after the rule is known)

* `csiibundle.lod`: the sub-object rule, wherever it lands, beside `mesh_min_lod` — shared, so the
  Blender harness gets it too.
* `stages/shapes`: a shape's `nullPx` takes the owner's distance into account. Note this is
  per-PREFAB and ownership is per-INSTANCE, so a prefab placed both free-standing and on a
  building takes the most generous of its placements — declare and count that.
* A new number in the report: how many prefabs had their distance raised, and by how much.

### The gate

* a screenshot at 150 m, 300 m and 600 m of a façade-heavy street, before and after;
* `tools/level_budget.py` — this ADDS drawn triangles, so take the number and check it against
  the 1,469,467 A5 left. If it doubles, the ratio needs revisiting, not shipping.

---

## A6.3 Some buildings render at wrong contrast, or black

### What is measured

Buildings do **not** go through the bake's tinted-albedo path (`paint_kinds` is `tree,prop`).
They take the live path: the material carries `colorPaletteMap` (CS2's colour mask) and the
`TSStatic` carries `instanceColor` / `1` / `2` from the record's three `colorSet` channels
(`stages/instances._add_static`, `beamng/materials`). 3,774 of 4,676 materials are tintable and
every one of them has a colour mask.

**Zero-valued colour channels are common and real:**

```
7,546 buildings carry a colorSet; 212 have a channel that is dark in every component
min-component distribution over all channels: p1 0.0, median 0.597, p99 1.0

USSW_Motel02_L5_2x4     [[0.231, 0.228, 0.225], [0.78, 0.78, 0.78], [0.0, 0.0, 0.0]]
NA_CommercialLow01_L5_5x5 [[1.0, 0.408, 0.0], [0.441, 0.269, 0.054], [0.0, 0.0, 0.0]]
```

A third channel of exactly `[0, 0, 0]` is what "completely black" would look like if that channel
reaches the surface. In CS2 it evidently does not.

### The three hypotheses, in the order they should be tested

1. **BeamNG's `colorPaletteMap` is not CS2's tint.** CS2 §7.2 is
   `lerp(base, base × c, mask)` in LINEAR light — a **multiply**, which can only ever darken
   towards the base's own colour. If BeamNG's palette path is `lerp(albedo, instanceColor, mask)`
   — a **replace** — then a `[0,0,0]` channel paints the surface flat black and a saturated
   channel paints it flat saturated. That is both reported symptoms from one cause, and it
   explains why trees and props look right: **their tint is baked** by `bake/materials` using
   CS2's own arithmetic, and only buildings take BeamNG's interpretation.
   *Where the answer is:* the v1.5 shader in the game install
   (`G:\…\BeamNG.drive\shaders\`), and `Bin64\BeamNG.drive.x64.exe` strings around
   `colorPaletteMap`. Read it, do not infer it from renders.
2. **Channel order / count.** CS2's mask RGB selects palette 0/1/2; confirm BeamNG reads the same
   three channels in the same order, and what it does with the mask's alpha.
3. **Colour space.** `instanceColor` may be expected linear while `colorSet` is authored
   elsewhere. A gamma error shows up exactly as "contrast is wrong but the hue is right" —
   and [[blender-premultiplies-colour-by-alpha]] is the precedent: correct hue + wrong magnitude
   is a scaling bug, not a colour bug.

### The fallback, if BeamNG's path cannot be made to match

The bake already knows how to do this exactly: `bake/materials` computes `base × M(texel)` and
ships either a constant `baseColorFactor` (31.4 % of pairs are exactly reproducible) or a tinted
texture. Adding `building` to `paint_kinds` would put buildings on the same proven path — at the
cost of texture bytes and of a per-prefab quantisation where a building currently gets its own
exact colour. Measure that cost before choosing it; A5 spent a lot of effort getting the variant
count DOWN.

### The gate

* pick the worst offenders by name from the level and render them in Blender through
  `tools/blender_import_bundle.py`, which applies CS2's own tint — if Blender is right and BeamNG
  is black, hypothesis 1 is confirmed without loading the game;
* a screenshot of `USSW_Motel02_L5_2x4` and `NA_CommercialLow01_L5_5x5` before and after;
* count how many of the 7,546 colour-set buildings change.

---

## A6.4 Vegetation standing in the carriageway

Asked for directly, and framed correctly when it was asked: *"It would be less 1 to 1 but would
make the city more driveable."* This is a **declared deviation**, not a fidelity fix — it goes in
the report's approximations block with its count, like every other one.

### What is measured

Sampling the 169,239 `car` and `parking` lane polylines from `lanes.jsonl` (1,523,047 stations,
~4 m apart) and testing every vegetation item against them:

```
9,594 of 686,641 vegetation items stand within 2.0 m of a drivable lane centre  (1.4 %)
   of which 4,890 are TREES (growth) and 4,704 are plants/bushes/grass

  1,854 of 314,222 ( 0.6 %) plant pd_Long_Grass        433 of  7,855 ( 5.5 %) tree  FloridaPalmTree01
  1,123 of  34,217 ( 3.3 %) tree  CoconutPalmTree01    417 of  9,404 ( 4.4 %) plant PlantPot02
  1,073 of  63,521 ( 1.7 %) tree  VFP_LiveOakTree      332 of  4,967 ( 6.7 %) plant FlowerBushCity01
    837 of 122,651 ( 0.7 %) tree  OakTree01            275 of  5,381 ( 5.1 %) plant GreenBushCity02
```

**1.4 % is the whole cost of the deviation** — it is a thin band, not a swathe, and dropping it
removes 9,594 obstacles from the road while leaving 98.6 % of the city's planting alone.

### Before building it: is this even ours?

Worth thirty minutes first, because the answer changes the fix. CS2 does not normally leave a tree
in a road — so either the rip is placing them wrongly, or CS2 hides them at render time, or they
genuinely are at the kerb and our lane polyline is not where the kerb is. Check in this order:

1. **Is our 2 m the right test?** A lane is ~3–3.5 m wide, so 2 m from the centre is just past the
   lane edge; some of these are legitimately kerbside plantings and clipping them would be wrong.
   Sweep the threshold (1.0 / 1.5 / 2.0 / 2.5 m) and look at the curve before choosing.
2. **Does CS2 cull vegetation over a network?** `Game.Simulation`'s vegetation systems and the
   `Owner`/`Attached` links are where that would live. If the game has such a rule, port it and
   this stops being a deviation at all.
3. **Only then** the consumer-side clip.

### The work

* the rule belongs in `csiibundle` (both consumers place vegetation), reading the lane layer the
  app already loads;
* a setting — `foliage_road_clearance` in metres, 0 = off — because it is a deviation and the user
  must be able to turn it off;
* counted in the report's `approximations` with the threshold and the item count, per the
  non-negotiable: *every approximation is declared and counted*.

### The gate

drive a long road and a roundabout; the count in the report; and the 98.6 % that must NOT move.

---

## A6.5 Areas float above the ground you drive on, and paint over the roads

Reported 2026-08-18: *"I can't seem to drive on areas, the car gets stuck. Also, the areas seem to
render above roads causing clipping and a lot of roads to become covered."* Two symptoms, two
independent causes, both consumer-side. **No re-rip is needed** — every field this wants
(`decalLayers`) already ships in the bundle and is already read into the bake's catalogue.

**Do not "fix" this by giving the paint a collider.** `stages/derived` states the rule and it is
right: *paint is a projection lifted millimetres above a surface that already collides — a
collider there is a lip that catches wheels*. `collision_paint` is `false` in the shipped level
and the Areas `TSStatic`s are `collisionType: "None"`, which is correct. The car is not catching
on the paint; it is driving on ground the paint is hiding. The fix is to put the paint back on the
ground, not to make the paint solid.

### Cause 1 — the conform targets the MAX of both triangulations, and BeamNG draws one of them

`bake/ground.GroundSurface` conforms areas to `diagonal="envelope", mode="max"` — the upper
envelope of the two ways a heightfield square can be split. Its own docstring says why, and says
what is missing: *which one is a fact about the engine we measure in-game, not a choice we make.
Until it is measured, `envelope` answers conservatively.* **It was never measured.** `max(A, B)`
is >= both triangulations by construction, so the paint stands above the drawn and driven ground
everywhere by that square's twist.

Measured on `cs2_city_fine` (built 2026-08-18, `terrain_upsample 2`, areas baked `ground: fine`),
2,107,650 area triangle centroids from the 60 largest area tiles, against the same 8192² / 1.75 m
raster the level's `.ter` is built from:

| paint minus … | p50 | p75 | p90 | p95 | p99 | >0.05 m | >0.15 m |
|---|---:|---:|---:|---:|---:|---:|---:|
| `max` envelope (what the bake conforms to) | +0.021 | +0.022 | +0.023 | +0.028 | +0.055 | 1.2 % | 0.2 % |
| `nw_se` triangulation | +0.022 | +0.036 | +0.116 | +0.249 | +0.729 | 18.9 % | 8.0 % |
| `ne_sw` triangulation | +0.022 | +0.042 | +0.179 | +0.379 | +0.998 | 22.3 % | 11.3 % |
| the level's own `.ter`, sampled bilinearly | +0.030 | +0.073 | +0.204 | +0.342 | +0.693 | 33.3 % | 14.0 % |

Read the first row first: **against the envelope the conform is exact** — the whole distribution is
the `decals.LIFT_LADDER` `area` rung (0.020) plus the `rendererPriority` rank lift, and nothing
else. `conform_triangle` is doing precisely what it was asked. The error is entirely the choice of
target surface.

And it is not the terrain stage's: the shipped `.ter` is **bit-identical to the bundle's fine
raster** over all 67,072,362 solid samples (every percentile of `ter − bundleFine` is +0.0000;
|Δ| > 0.05 m on 0.02 %, which is `terrain.clamp_carriageway`). The driven ground *is* the bundle
raster. Whichever single triangulation BeamNG picks, the paint is a sheet floating over it — a
metre over it at p99 — and since the paint does not collide, the car drops through the visible
plaza onto relief the plaza was hiding. That is the "gets stuck".

**The instrument already exists and the plan already named it.** `beamng-consumer-plan.md:381`:
*"optional Lua `getTerrainHeight` probe settles the diagonal"*; `bake/__main__.py` already takes
`--diagonal nw_se|ne_sw|envelope`; `tools/beamng/probe_ter.py` is the same shape of in-game probe
that settled the `.ter` orientation on 3,800 road nodes. The app hard-codes `"envelope"` in two
places (`stages/bake.py:132`, `pipeline/census.py:101`).

**Test two hypotheses, not one.** Torque-derived terrain does not necessarily split every square
the same way — a per-square (checkerboard, or height-dependent) split is possible. Sample enough
adjacent squares that an alternating pattern would show, or the probe will "confirm" a uniform
diagonal that only held on the squares it happened to hit.

`bake/skirts.py` takes `mode="min"` for the mirror reason and inherits the same fix.

### Cause 2 — `decalLayers` is read and then never used

`areas.surface_entry` puts `decal_layers` in the catalogue (`csiibundle/areas.py:118`) and
**nothing reads it**. So every area is conformed to the terrain across its whole polygon,
including wherever a road happens to be, and lifted above it.

CS2 does not do that. `decalLayers` is the game's own mask for which surfaces a projection lands
on (`docs/bundle-schema.md` §4.2: *a decal whose `decalLayers` is `["Roads"]` must not be painted
across the wall behind it*). In this bundle:

| `decalLayers` | prefabs | paint | of which over a carriageway |
|---|---:|---:|---:|
| `["Terrain"]` | 34 | 11,440,503 m² | **472,957 m²** |
| `["Terrain", "Roads"]` | 6 | 1,059,171 m² | 36,491 m² |

509,448 m² — 4.1 % of the 12.5 M m² of paint in the 636 tiles that carry both areas and
networks — lies over a road surface in plan, and 93 % of that belongs to prefabs the game masks
off roads entirely. Where it overlaps at grade (398,970 m², excluding bridges and tunnels at
|Δ| > 1 m) the paint stands **proud of the carriageway over 298,895 m², 74.9 % of the overlap**:
p50 +0.052, p75 +0.178, p90 +0.278, p99 +0.616 m. Worst offenders, all of them `["Terrain"]`:

```
159,645 m² over roads / 1,293,114 m² total   Concrete_Surface_01
 68,094 m² over roads / 1,955,614 m² total   Pavement_Surface_01
 45,671 m² over roads /   517,003 m² total   Farming … GrassCrop1_GreenFaded_Overlay_Surface
 25,488 m² over roads / 1,472,353 m² total   Pavement_Surface_02
 25,387 m² over roads /   324,835 m² total   Sand_Surface_02
```

Even for the 6 prefabs that *do* target Roads, the game composites by projection onto the road
mesh and cannot z-fight; we lift by 2 cm above the **terrain**, and the `LIFT_LADDER`'s bottom
rung is the terrain — it has no rung for "the carriageway", which stands above the terrain.

**The precedent is in the same package.** `bake/markings.py:209-211` already picks its target from
`instances.decal_layers(record)` — `decal_blocks_ground` when `Roads` is absent, `decal_blocks_road`
when it is present — and `bake/roads.load_road_surface` already builds the road
`TriangleSoupSurface` an areas product would drape onto. Areas simply never called it.

### The work

* **Settle the diagonal first** — it is the larger of the two errors and it changes what "conform"
  means for every ground product. In-game probe → `ground.py` stops guessing → the app stops
  hard-coding `"envelope"`. Once it is known, `mode="max"`/`"min"` is not conservative, it is
  wrong, and both areas and skirts take the measured diagonal.
* **Use `decal_layers` in `bake/areas.py`**, the way `bake/markings.py` already does: a
  `["Terrain"]` area is *clipped* where a road surface is on top of it, not lifted over it; an
  area that includes `Roads` drapes onto the upper of the two surfaces.
* **Count both**, per the non-negotiable: m² of paint clipped off carriageways, m² draped onto
  them, and — until the diagonal is settled — the residual float the envelope is still adding.

### The gate

* re-run both measurements above: paint − the real triangulation must collapse to the lift ladder
  (p90 <= 0.03 m, matching today's envelope row), and `["Terrain"]` paint over a carriageway must
  be 0 m²;
* drive a parking lot, a plaza and a farm field — the symptom is a car that sits low and catches on
  nothing you can see;
* drive a road that a lot surface abuts, and look along it at a grazing angle, which is what makes
  a 0.3 m sheet cover "a lot of roads".

### Adjacent, not measured

0.61 % of area triangles sit over a `.ter` square marked `255` (hole). Those depend on a
`terraincut` mesh being present underneath; nobody has checked that they all are.

---

## A6.6 Floating props — we drop the building and place its props anyway

### The transform is not the bug, and that was worth proving first

Every mechanism that could move a prop was checked and cleared before anything else was looked
at, because "the placement maths is wrong" is the expensive wrong answer here:

| checked | result |
|---|---|
| `pos` is world-space for a sub-object | contract §4.1, and it holds: `pos.y − attrs.elevation` equals the terrain under the prop for **99.1 %** of the 392,155 props carrying `elevation` (median 0.004 m) |
| the shipped `.ter` matches the bundle terrain under props | median difference **0.000 m**, p1/p99 both 0.00, over all 504,116 props |
| the level's own positions match the records | `check_instances_vs_game.py` on 4,000 sampled records: position **100.0000 %**, mesh state 100 %, drawn size 100 % |
| `stages/instances._transform` vs the shared `csiibundle.frames` formula | identical to 1e-15 on 400 records once `growthScale` (which the app folds in and `instance_matrix` deliberately does not) is accounted for |
| glTF node hierarchy | `glb.parse_glb` returns accumulated WORLD matrices and `shapes._collect` applies them — sub-parts are not stacked at the origin |
| generated LOD levels drifting out of frame | 120 shapes sampled, **0** with a level centre more than 5 % of the shape's own size off LOD 0 |
| BeamNG snapping forest items to terrain on load | it does not; `core_terrain.getTerrainHeight` appears only in `util/forestGenerator.lua`, the authoring tool, never in the load path |

**A gap the harness has, and it is real:** `check_instances_vs_game.py` prints "the position is
`T(pos)` conjugated into BeamNG axes" and **never checks the rotation**, even though its own table
claims `T·R·S·diag(-1,1,1)` as the question. Rotation is not the cause here (a wrong rotation
turns a prop in place, it cannot lift it, and position is exact), but the probe should not be
read as having cleared it. Adding `R` to that row is part of the work below.

### What is actually measured

**A sub-object is placed even when the owner it hangs off was dropped.** `stages/shapes` and
`stages/instances` decide per record; nothing asks whether the record's `parent` survived. Over
the 1,191,478 records this build admits, following `parent` transitively:

| the owner was dropped because… | orphans | owners | ≥0.5 m up | ≥5 m up | worst |
|---|---:|---:|---:|---:|---:|
| **its prefab did not resolve** (`warnings.jsonl` `kind: "prefab"`) | **6,656** | 19 | 2,816 (42 %) | 2,419 | **292.5 m** |
| its mesh yielded no geometry (`kind: "mesh"`) | 5,806 | 86 | 742 (13 %) | 168 | 12.9 m |
| `attrs.state == "construction"` — **undeclared** | 59 | 6 | 47 (80 %) | 45 | 85.0 m |

3,605 of the 12,521 orphans stand clear of the ground, 993 of them more than 50 m up. They are
not scattered: **19 unresolved buildings account for 6,656 of them**, so each one is a whole
lot's planting and a whole roof's kit hanging in the sky at once —

```
690 children  unresolved_2554423   at (2442, -1010)     584  unresolved_2495283  at (3460, 509)
506 children  unresolved_2495288   at (1125,  -957)     428  unresolved_2554594  at (4970, 2958)
```

Rendered through `tools/blender_import_level.py` at (2455, −990) — the level as BeamNG reads it,
not the bundle — that is a row of `NA_LindenTree01` and a strip of rooftop decking floating in
clear sky at 819 m over a downtown with no tower under them. `GardenLight01`, `LightpolePark02/04`
and `StreetlightSingle02` are the next most numerous, which is why it reads as *props*.

### The rule, and where it comes from

This is **not** a rule to port from CS2 — the game had those prefabs and drew the buildings; the
prefab failed to resolve **at rip time**, and §10 is the contract for exactly that: the producer
declares what it could not deliver, in `warnings.jsonl`. So the rule is a consumer's obligation
against a **declared gap**, not an invention:

> A record whose owner was not placed because the producer declared it undeliverable is not
> placeable either. Drop it, count it, and name the owner.

**The two owner classes must not be treated alike, and the warning kind is the discriminator:**

* `kind: "prefab"` — *"managed prefab did not resolve; instances kept with placement only"*. The
  building is genuinely absent. Its children are unsupported. **Drop them.**
* `kind: "mesh"` — *"none of N sub-mesh(es) yielded geometry"*. The prefab resolved and simply has
  no geometry of its own: these are `ParkingLot02..14`, `CampfireSite01/02`, `ChirperPark03`,
  `AmericanFootballField01` — a lot **is** its paving, and the paving ships in the `areas` layer.
  Its 5,806 children are the lot's lamps, benches and planting, median height 0.00 m above
  terrain. **Keep them**; dropping these would delete real park furniture, which is why a blanket
  "no owner, no draw" rule is wrong.

The 59 construction-state orphans are a third case and the only undeclared one. §4's own text says
the site is not empty — *"the crane and site fence are real entities with their own prefabs"* —
so its children are meant to be drawn; but 45 of these 59 are more than 5 m up, i.e. they are the
finished building's roof kit rather than the site's. Decide it from `attrs.elevationFlags` /
ownership, not from a height threshold, and declare whichever way it goes.

**Check before building**: confirm that a `kind: "prefab"` warning really does mean the whole
prefab is missing rather than one LOD of it, by reading the producer site that emits it
(`Systems/InstanceRipSystem` / `IO/BundleWriter`) — the wording is a producer's, and A6 has one
precedent already of a doc's claim failing a thirty-second re-check.

### The remedy at source, which is cheaper than any of this

All 19 offenders are `unresolved_*` — **unowned asset packs** (the same cause as the grey area
surfaces of audit §36). Enabling those packs in CS2 and re-ripping removes the gap entirely, and
brings back 19 real buildings the level is missing today rather than merely hiding their props.
The consumer fix is still required — a bundle is allowed to declare a gap and we must survive one
— but the ask goes to Naomi first, and the two are independent.

### The work

* `csiibundle.instances`: `undeliverable_owners(bundle_dir)` — the ids whose owner carries a
  `kind: "prefab"` warning — and a `placeable(record, undeliverable)` predicate that walks
  `parent` transitively (depth is 3 at most in this city, but a stacked sub-object of a
  sub-object is legal). Shared, because `tools/blender_import_bundle.py` draws the same floating
  trees today.
* `apps/cs2beamng/beamng/placement.admit`: a new `"orphan"` reason beside `baked` / `state` /
  `kind` / `region` / `density`, so the shapes pass and the instances pass reach it identically —
  the file's whole point.
* The report: an `approximations` entry naming the count, the owner assets, and the fact that the
  underlying gap is the producer's declared one, per the non-negotiable that every approximation
  is declared and counted.
* `tools/check_instances_vs_game.py`: add the rotation column it already claims, and a row for
  "no placed record hangs off an undeliverable owner".

### The gate

* the count: **6,656 records dropped, 5,806 park-furniture records kept**, and 0 placed records
  with an undeliverable ancestor;
* re-render (2455, −990) through `blender_import_level.py`: the tree line at 819 m is gone and the
  park below it is not;
* `tools/level_budget.py` — this only ever REMOVES geometry, so drawn triangles must not rise.

---

## Order

**A6.0 first** — it is a live regression, it is the one the user is actually feeling, and every
other item here is cosmetic beside it. **A6.1 second**: its rule is in hand, it is small, and it
also cuts the 3,062 MB of forest collision. **A6.4 third** (cheap, asked for, and it interacts
with A6.1 — both are decisions about which vegetation to keep). **A6.5** next — it is the second thing the user has
actually felt, its larger half is one in-game probe the plan has owed since A2, and its smaller
half is a field the bake already loads and forgot to read. **A6.6 alongside A6.1** — its
investigation is finished, the rule is written, it is one predicate in `csiibundle` plus one
reason string, and it only ever removes geometry, so it cannot make A6.0 worse. Ask Naomi for the
asset-pack enable + re-rip at the same time, since that half is hers and runs in parallel.
**A6.3** after that: the most visible defect, and hypothesis 1 is a half-hour read of a shader.
**A6.2 last** — it needs the game's
culling rule read properly, and it is the only item here that *costs* VRAM to fix, which A6.0
says we do not have.

## Not in scope, but adjacent and known

* **181 `engine::WaterObject::initTextures| Texture missing:` errors** — one per `WaterBlock`
  (the level has 180) and the name in the message is EMPTY. Our water objects are asking for a
  texture we never name. Small, but it is 181 failed texture inits at load and nobody has looked.
* `objects` is 228.942 s of a 252.468 s load, down from 373.484 s of 396.982 s once the imposters
  went. Still four minutes. See A6.0's trap before spending anything on it.
* 235 MiB of terrain textures still ship as `.png` and are cooked at first load, unlike every
  other texture in the level.
* `bake/terrainmask` (78 s) is the last serial bake product; `terrain.clip_span` is 2.37 M numpy
  calls on tiny scanline spans, and row-band partitioning would parallelise it exactly (disjoint
  rows preserve the `refs` last-wins order).
