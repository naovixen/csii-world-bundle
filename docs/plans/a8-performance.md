# A8 — one performance pass: every lever measured, then one rebuild

> **STATUS — CLOSED, VALIDATED IN GAME 2026-08-20.** Naomi loaded the 2026-08-19 22:15 build of
> `cs2_city_fine` and reported it good: the memory and lag this plan was written for, the LOD 0
> regression §87 opened and closed on the way, and A7's navigation with it. Nothing here is open.
> The plan text below is left as it was written, and the closing numbers are at the foot.
>
> **What was built:** P1, P5, P3-density (TREES ONLY, 60 %), P4, P2 and both P7 halves — approved
> 2026-08-19. P3's rare-type drop (items 3 and 4 of the order table) stayed **deferred to
> "Extras"**, not rejected; `prop_density` stayed at 100. Audit **§89** has the build's own
> before/after, **§87** the LOD 0 promotion defect it caused and the `lod0_min_triangles` floor
> that fixes it, **§88** `collision_lights`.
>
> **The original ask (2026-08-19).** Every number below is measured on the level as it stands
> after A6.9 (`cs2_city_fine`, built from the 2026-08-19 16:32 rip), requested after the load that
> reported *"incredibly laggy and I'm using up a lot more memory… almost 11000"*, with foliage
> density, small-prop collision and lane-furniture collision named explicitly.

## Where the bytes are today

Three numbers have been called "the budget" and they mean different things. All three, measured:

| | what it is | now |
|---|---|---:|
| `validate.checks.resident` | VRAM side, textures at `textureReductionLevel 2` | 4,282 MiB |
| `level_budget` RESIDENT TOTAL | the asset bytes BeamNG **reads off disk** | 5,926 MiB |
| the level folder | everything shipped, `.dae` sources included | 11,803 MiB |
| the process | system RAM, reported by Naomi | **~11 GB** of 31.9 |

Neither instrument counts the collision BVH (4,428,499 triangles), 1,184,286 forest transforms,
6,171 materials, or BeamNG's baseline with a CEF UI — which is why ~5.9 GB of assets reads as
11 GB of process. **`west_coast_usa`, the biggest level BeamNG ships, mounts 780 MiB of `.cdae`.
This level mounts 4,173 — 5.4x.**

### Meshes, 4,173 MiB

```
cs2 (objects)   2,419      buildings 1,977 (1,170 shapes)   props 325 (2,827)
                             trees        46   (138)        unknown 71 (154)
networks          787      793,092 triangles drawn — the most efficient bytes in the level
areas             387
lanegeometry      284      lane furniture
skirts            160      collision ON, 641 tiles, none ever culled
markings          130
terraincut          6
```

**A shape is mounted WHOLE the moment one instance is in view** — every LOD and its collision
mesh — so the cost rides on the shape LIST, not the instance count. Sampled over 200 shapes and
again over the 60 heaviest, the split inside a shape is stable:

```
LOD0 (full detail)   65-66 %      collision   14.2 %
LOD1                 16-17 %      LOD2         4.0 %
```

and the heaviest shapes carry it: **top 100 of 4,289 = 966 MiB, top 300 = 1,484 MiB**, the largest
single one 26.1 MiB (`MedicalUniversity01`).

### Textures, 1,753 MiB

```
colormask     1,025 MiB    771 files   avg 1,361 KiB   <- 767 of them are 1024 x 1024
color           305 MiB  3,679 files   avg    85 KiB   <- 256 x 256
data            160 MiB  2,521 files
normal          159 MiB    927 files   <- 256 x 256
terrain .png    104 MiB     80 files   still uncooked
```

### Collision and instances

```
19,593 TSStatics, all "Collision Mesh"          4,428,499 collision triangles in the shapes
1,181,394 forest items over 3,118 types         trees 514,554  props 512,456  unknown 154,384
2,928 of 3,118 forest types collide             528,822 items already non-collidable
```

---

## P1 — the colour mask ships at 1024 while everything else ships at 256

**A defect, not a tuning, and it is the single biggest win available.**

`beamng.materials.KIND_CAP_SETTING` names `color`, `normal`, `data` and `opacity`. It does not
name **`colormask`**, so `caps.get("colormask", 0)` returns 0 — *uncapped* — and
`export_texture` never resizes it. Measured on the level:

```
colormask   767 of 771 files at 1024 x 1024   1,025 MiB
color       3,628 of 3,671 at  256 x  256       305 MiB
normal        904 of   919 at  256 x  256       159 MiB
```

The mask is a per-texel selector for the palette that multiplies the **albedo**, and the albedo is
256. There is nothing for the extra 16x of mask texels to select against.

```
1,025 MiB  ->  ~64 MiB          SAVES ~960 MiB of texture bytes (55 % of all of them)
```

It has been invisible until now because A6.3's masks only reached disk today (§80) — before that
the level shipped **zero** of them.

**Work:** add `"colormask": "texture_max"` to `KIND_CAP_SETTING`; bump the materials stage. One
line, no visual cost that is not already paid by the albedo. **Gate:** no `*.colormask.dds`
larger than the colour cap.

## P2 — the heaviest building shapes carry LOD0 nobody sees at full detail

Buildings are **1,977 MiB of the 2,419**, and ~66 % of a shape is LOD0. The top 100 shapes alone
are 966 MiB.

CS2 itself does not draw these at full detail from across the city — that is what `m_MinLod` is
for, and A6.7 already built the ladder. What the level does NOT do is spend fewer *bytes* on the
detail level it rarely draws: LOD0 is shipped exactly as ripped.

**Three options, and the choice needs one measurement before it is made:**

1. **Decimate LOD0 itself above a byte threshold.** `csiibundle.decimate` already exists with
   boundary pinning, and `bake/lods` already runs it. A 50 % decimation of the top 300 shapes is
   ~740 MiB; the visual question is what a 26 MiB hospital looks like at half its triangles from
   the pavement outside it.
2. **Promote LOD1 to LOD0 for shapes whose LOD1 error is under a threshold.** Free where it holds,
   nothing where it does not — the decimator already records displacement per shape
   (`p50 0.0169 m, p90 0.1135, p99 0.5858` on the last bake), so the shapes that can take it are
   already identified and counted.
3. **Nothing, and take P1 + P3 + P4 instead.** Defensible if they are enough.

**Do NOT** simply drop LOD0. You drive past these buildings; LOD1 as the close-range mesh is a
visible downgrade and A6.7 spent a whole section proving the switch distances matter.

**MEASURED 2026-08-19, and option 2 is the one to build.** `derived/lods/lods.json` already
records every shape's LOD1 displacement (the p95 distance back to LOD0's surface), so the trade is
readable without building anything:

```
LOD1 error <=      shapes      saves      the biggest thing it takes
     0.02 m         2,074      155 MiB    Pontoon_Boat (0.002 m, 160,805 -> 40,337 tris)
     0.05 m         2,947      282 MiB    ParkingHall01 (0.049 m, 231,552 -> 58,210)
     0.10 m         3,375      387 MiB    Five_Luxe_JBR_ (0.080 m, 243,371 -> 59,389)
     0.20 m         3,523      450 MiB
     0.50 m         3,683      585 MiB    InternationalAirport01 (0.423 m, 257,130 -> 40,027)
   promote all      4,165    1,176 MiB
```

**0.10 m is the threshold**: 387 MiB for a surface nowhere more than 10 cm from the one it
replaces — under the width of a window reveal, on shapes measuring tens of metres.
`Five_Luxe_JBR_` drops 243,371 triangles to 59,389 at 8 cm. Above 0.2 m the curve flattens and the
errors start landing on landmarks (`InternationalAirport01` at 42 cm) for little more.

*The displacement is a p95, so 5 % of each surface is further out than the figure — which is why
the threshold sits four times tighter than the point where it would start to show.*

**Work:** a `lod0_max_error` setting (default 0.10 m, 0 disables) read in
`stages/shapes._lod_levels`: where LOD1 exists and its measured displacement is under it, LOD1
becomes the shape's top detail and LOD2 the only generated one — the ladder simply starts one rung
coarser, so nothing about the switch logic changes. Counted in the report. **Gate:**
`level_budget`'s cs2 mesh bytes, and a screenshot of a signature building at 20 m.

## P3 — foliage: the density is not the cost, the RARE TYPES are

**Say the honest thing first: cutting density saves almost no MESH memory.** The 1,181,394 forest
items share 3,118 shapes worth 442 MiB, and removing 40 % of the items removes **none** of that —
the types stay mounted. What density does remove is real anyway:

* **`forest/*.forest4.json` is 181 MiB that BeamNG parses line by line at load** (vanilla's
  largest forest is 151 MiB, for 133,375 items against our 1,181,394);
* every item is an entry in the physics broadphase if its type collides — 652,572 today;
* every item is a culling and instancing candidate every frame.

`foliage_density` (trees) and `prop_density` (props) are existing settings, both at 100. Each is a
deterministic per-record hash test — stable across builds, monotone (a lower setting is always a
SUBSET of a higher one), and weighted by the tree's own state so mature trees outlive saplings
(`DENSITY_WEIGHT`: Adult 1.0, Dead 0.9, Teen 0.75, Child 0.5, Stump 0.4). Measured over the
1,256,113 tree and prop records:

```
setting     trees kept    props kept       total removed    forest4.json
   100 %       697,256       558,857                   0       181 MiB
    80 %       550,737       447,089    258,287  (21 %)      ~143 MiB
    60 %       413,089       335,483    507,541  (40 %)      ~109 MiB
    40 %       275,484       222,771    757,858  (60 %)       ~72 MiB
```

**This is the lever for FRAME TIME and LOAD TIME, not for RAM** — and since the report that
prompted this plan was *"incredibly laggy"* as much as it was about memory, that makes it more
relevant than its position in a MiB-ordered table suggests. Even at 40 % the level still carries
~500,000 forest items against `west_coast_usa`'s 133,375.

**The RAM lever is the type tail, and it is severe.** A `TSForestItemData` mounts its shape whole
whether it holds one item or ten thousand. Measured over all 3,118 types:

```
types placed under N      types      their shapes      their items    share of the forest
        N = 10            1,807         241 MiB              4,713           0.4 %
        N = 25            2,055         285 MiB              8,691           0.7 %
        N = 50            2,272         326 MiB             16,376           1.4 %
        N = 100           2,457         346 MiB             29,661           2.5 %
        N = 250           2,712         371 MiB             70,380           6.0 %
```

**2,457 of 3,118 types — 79 % of them — exist to draw 2.5 % of the foliage, and they cost 346 MiB
of the 442.** That is the whole forest budget sitting in a tail nobody looks at.

`forest_min_items` does NOT reach this. It folds a *paint variant* into its untinted twin of the
same prefab (`_merge_thin_types`), so it can only merge colours — and this build merged **0**.
These 2,457 are distinct prefabs with no twin to fold into.

**The work is a new rule and it must be a DROP, declared and counted**, because the alternative —
substituting a "similar" prefab — is an invention with no counterpart in the game. A type under
the threshold is not shipped and its items are not placed; the report says how many types, how
many items and how many MiB, exactly as the road-clearance and orphan drops already do.

**Naomi picks the threshold from the table above.** N = 10 is nearly free (241 MiB for 4,713
items, 0.4 %); N = 100 is the knee (346 MiB for 2.5 %); beyond 250 it starts removing things a
viewer would notice.

**Work:** `forest_rare_type_min` in `stages/instances`, applied where the type list is built,
counted in the report beside `forestCollision`. **Gate:** `level_budget` forest types and items,
and a screenshot of a park.

## P4 — small props should not collide (Naomi's second ask)

The existing rule is `FOREST_COLLIDABLE_MIN_HEIGHT = 0.5 m` — anything shorter ships
`collidable: false`, plus every non-tree plant (the game's own `PlantObject` split). **A fire
hydrant is 0.97 m tall and a road sign 2.2 m, so both pass it**, and 2,928 of 3,118 types collide.

**Height is the wrong instrument. Footprint is the right one**, and the shapes stage already
measures it: `row["boundsMin"]/["boundsMax"]` are in the row that `_shape_height` reads, so
`max(x_extent, z_extent)` costs nothing. *(Note for whoever builds it: `TSForestItemData.radius`
is a bounding SPHERE — a palm tree reads 6.42 m and a pier 16.76 — so it cannot express "thin".)*

Measured over all 2,928 collidable types:

```
footprint < 0.40 m ->    50 types      < 0.80 m ->   442 types
footprint < 0.60 m ->   165 types      < 1.00 m ->   659 types
```

**0.80 m is the threshold that matches the request exactly.** What it drops, measured by name:

```
NA_FireHydrant01        0.46 m      NA_TrafficLightCar01     0.56 m
NA_OnewaySign01         0.49 m      NA_TrafficLightCrosswalk 0.40 m
NA_ParkingSign01        0.54 m      Trashbin01..04      0.49-0.73 m
NA_MotorwaySign01       0.61 m      NA_MailboxStand01..04    0.30-0.66 m
EU_ParkingSign01        0.61 m      SignSidewaySmall01/02    0.78 m
NA_YieldSign01          0.73 m      GardenLight01/03    0.13-0.16 m
```

Below 0.80 m nothing survives that a car should stop against — **with one exception the rule gets
wrong and which must be declared rather than papered over: `Bollard01`/`Bollard02` are 0.24 m and
a bollard exists precisely to stop a vehicle.** Exempting them would mean matching a name, which
this repo has a standing rule against (a name is not a property of the object); the honest
handling is to state it, count it, and revisit if driving into a bollard turns out to matter.

**This is OURS and is declared as such** — CS2 has no collision concept for a placed object at
all, which the existing constant's comment already says.

**The saving is not in bytes.** A `Colmesh` is 14.2 % of a shape and these are small shapes; what
this removes is entries in the physics broadphase and their BVH, which is system RAM and frame
time, and which no instrument here measures (see P8).

**Work:** `FOREST_COLLIDABLE_MIN_FOOTPRINT = 0.8` beside the height constant, applied in
`_write_item_data`, counted as `tooThin` in the same stats dict, and exposed as a setting so the
threshold is not buried. **Gate:** the report's `forestCollision` split, and driving into a
hydrant.

## P5 — lane furniture: the collision is already off, the geometry is the cost

Checked, because both halves of this ask already have settings:

* **`collision_furniture` is already `False` in this build**, and the built level agrees —
  `lanegeometry` ships `"collision": false`. There is nothing to remove.
* **`lane_furniture: False` already removes the geometry entirely**: 284 MiB and 4,607,585
  triangles across 281 tiles. It is one setting.

**Work:** none. Decide whether the fences and cables are worth 284 MiB and set it. The plan's job
here is to say that the switch exists rather than build a second one.

## P6 — the other collisions, each with its number

```
skirts        160 MiB, 1,805,811 triangles, collision ON, 641 tiles NEVER culled
terraincut      6 MiB,   152,652 triangles, collision ON
lanegeometry  284 MiB, collision already off        areas / markings / networks: already off
```

`collision_skirts` exists. Skirts are the ground-to-wall foundation strips — you can drive onto
them, so removing their collision means driving through a foundation. **Recommend leaving on**;
listed so the number is on the table.

`collision_preset` is `simplified` (the decimated LOD1 hull). `exact` would be worse and `off`
removes everything; neither is wanted.

## P7 — disk, which is not RAM but is 7 GB

* **`temp/levels/cs2_city_fine` holds 9,047 cooked `.cdae` (4,998 MiB) where the level has 4,289.**
  BeamNG never removes a cooked shape whose source is gone, so every rebuild since 2026-08-18 has
  left its dead shapes behind. The cook stage should sweep `temp` for shapes the level no longer
  has. **Not a RAM saving** — the engine loads only what the level references.
* **The level ships 5,452 MiB of `.dae` beside 4,173 MiB of `.cdae`.** Verified: all 4,289 `.cdae`
  are newer, so BeamNG loads none of the sources, and vanilla ships both too
  (`west_coast_usa`: 1,654 MiB `.dae` / 780 MiB `.cdae`). A `keep_dae` setting defaulting to off
  would halve the folder. **Not a RAM saving either** — but it is 5.4 GB.
  **Checked before building it: 182 of vanilla's 6,361 `.cdae` ship with NO `.dae` beside them**
  (`Industrial.zip` alone has 74), so a cooked shape whose source is absent loads fine and this has
  the game's own precedent. The reverse never happens — 0 vanilla `.dae` lack a `.cdae`.
* **104 MiB of terrain still ships as `.png`** and is cooked by BeamNG at first load. Already on
  the A6 "adjacent and known" list.

## P8 — two questions this pass should answer, not assume

1. **`level_budget` reports 18,439 triangles drawn for 12,448 cs2 statics — 1.5 each.** That
   cannot be a drawn-triangle count for buildings. Either the objects are being culled far harder
   than intended or the instrument is not measuring them. **The A5 gate of 1,469,467 drawn
   triangles may therefore be blind to every object in the level**, which is exactly the failure
   §74 recorded for the A6.2 gate. Settle it before quoting the number again.
2. **Nothing has ever measured system RAM.** Both instruments measure bytes on disk. The one
   number that matters to Naomi's report has no instrument at all, and every figure in this plan
   is a proxy for it. Worth one pass with the process's own working set beside the level's bytes.

---

## Order, and what it is worth

| | lever | saves | risk | work |
|---|---|---:|---|---|
| **1** | **P1 colour mask cap** | **~960 MiB** | none — a defect | one line |
| 2 | P5 lane furniture off | 284 MiB | fences and cables vanish | one setting |
| 3 | **P3 drop rare forest types (N=10)** | **241 MiB** | 4,713 items of 1.18 M vanish (0.4 %) | one predicate |
| 4 | P3 the same at N=100 | 346 MiB | 29,661 items vanish (2.5 %) | the same |
| 5 | P3 density 100 → 60 | 72 MiB of file, 507,541 fewer items | a uniformly emptier city — **the main LAG lever** | one setting |
| 6 | P4 small-prop collision (0.8 m) | 442 types out of the broadphase | hydrants, signs, bins and BOLLARDS stop being solid | one predicate |
| 7 | **P2 building LOD0** | **up to ~740 MiB** | visible on signature buildings | one bake option |
| 8 | P7 `keep_dae` off | 5.4 GB of DISK | none | one setting |
| 9 | P7 temp sweep | 2.5 GB of DISK | none | one cook step |

**P1 first and alone if nothing else happens.** It is a bug, it is free, and it is bigger than
every deliberate sacrifice below it combined.

**Then 2-6, which are settings and two predicates** — no new machinery, and each is reversible by
rebuilding one stage.

**P2 last, and only after its own measurement.** It is the only item that trades appearance for
memory, and option 2 (promote LOD1 where its measured error is small) is the only version of it
that costs nothing visually. Take the free part; leave the rest until a load says it is needed.

## The gate

* `level_budget`: mesh bytes by group, texture bytes by kind, the heaviest-shapes section, and
  forest types/items — before and after, in one table;
* `validate.checks.resident` against A6.0's band (fits at 3,473 MiB, fails at 5,224); the level is
  at 4,282 now and P1 alone should clear the lower bound;
* **the process's own memory in game**, which is the number that started this and the only one
  none of our instruments reports;
* a screenshot of the same street before and after, because five of these eight levers remove
  something a viewer can see.

---

## Extras — measured, not built

Both are the same predicate at two thresholds: **do not ship a forest type placed fewer than N
times**. `forest_min_items` does not reach these (it merges paint variants of one prefab, and
merged 0); this needs a new drop rule in `stages/instances`, declared and counted like the
road-clearance and orphan drops beside it.

```
N       types dropped     items dropped     saves     per item removed
 10           1,807             4,713      241 MiB      52 KiB
 25           2,055             8,691      285 MiB
100           2,457            29,661      346 MiB       4 KiB for the 10-99 band
```

**N = 10 is the good trade and N = 100 is not.** At 10 the heaviest things dropped are one-offs —
`Pontoon_Boat__Floating_` is 9.8 MiB and the city moors three, `CargoHarbor01_Cargo_Cranes` 7.4 MiB
for three, `ShippingContainerPileLargeHigh01` 4.5 MiB for seven. Going to 100 adds only 106 MiB and
costs 24,948 more items, and they are scenery rather than one-offs: ~170 mangroves (a shoreline),
52 `EU_Railing06Long` (a run of fence), 30 alders, rubble piles, food stalls.

**The other extra is the rest of P2.** The 0.10 m promotion deliberately left the landmarks alone,
so the top 100 shapes are still 964 MiB — 29 % of all mesh bytes — with `MedicalUniversity01` at
26.1 MiB. Taking those needs decimating LOD 0 itself rather than promoting LOD 1, which is the
only lever in this document that trades appearance on the things a viewer looks at.

---

## CLOSED — the level Naomi validated (2026-08-20)

The build behind the accepted load is `cs2_city_fine`, 2026-08-19 22:07–22:15, `status: done`,
`cs2beamng_settings.json` written first so it says so itself. Where it landed:

| | A6.9, the level this plan was written against | A8 as built (§89) | as VALIDATED |
|---|---:|---:|---:|
| `validate.checks.resident` total | 4,282 MiB | 3,382 | **3,680** |
| of which meshes | | | 3,483 |
| of which textures, at `textureReductionLevel 1` | | | 198 |
| shapes | 4,289 | 4,275 | 4,275 |
| LOD 0 promoted / kept for being small | — | 3,338 / 0 | **397 / 2,941** |
| `colliders.lightNone` (§88) | — | — | 194 |
| validator problems | | | 1 (the band note below) |

**The `lod0_min_triangles` floor put 298 MiB back, and that is the trade §87 argued for**: 2,941
shapes that the p95 displacement was happy to flatten keep their geometry, which is what stopped
a road sign shipping as 4 triangles and a facade as 381. It also puts the level back **over**
A6.0's measured "known to fit" line of 3,473 MiB, into the unmeasured band — the one validator
problem the build reports is exactly that, and the load says the band is not a wall:

```
budget: the level mounts 3,680 MiB (3,483 meshes + 198 textures, textureReductionLevel 1 (Low))
on a 8,018 MiB card at 4.95 Mpixel - in the unmeasured band between what is known to fit
(3,473) and what is known to fail (5,224). 2.7x the 1,375 MiB of west_coast_usa.
```

**Still true and still unmeasured** — P8's two questions are not closed by this load:

* the process's own system RAM has no instrument, and every figure here is bytes on disk;
* `level_budget`'s drawn-triangle count for objects was the blind spot §74 recorded for A6.2's
  gate; the three instruments fixed in §89 read a `.dae`-less level correctly now, but nothing
  has re-derived the A5 drawn-triangle gate against them.
