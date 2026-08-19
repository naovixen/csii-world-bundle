# A8 — one performance pass: every lever measured, then one rebuild

> **STATUS — PLANNED, NOTHING BUILT (2026-08-19).** Every number below is measured on the level as
> it stands after A6.9 (`cs2_city_fine`, built from the 2026-08-19 16:32 rip). Requested by Naomi
> after the load that reported *"incredibly laggy and I'm using up a lot more memory… almost
> 11000"*, with foliage density, small-prop collision and lane-furniture collision named
> explicitly. This plan covers those and everything else measurement turned up, in one pass.

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

**Work:** measure option 2 first — it is free where it applies. `bake/lods` grows a
`lod0_max_error` option; shapes whose LOD1 displacement is under it ship LOD1 as their top level
and the report counts how many and how much. **Gate:** `level_budget`'s cs2 mesh bytes, and a
screenshot of a signature building at 20 m.

## P3 — foliage: the density is not the cost, the RARE TYPES are

**Say the honest thing first: cutting density saves almost no MESH memory.** The 1,181,394 forest
items share 3,118 shapes worth 442 MiB, and removing 40 % of the items removes **none** of that —
the types stay mounted. What density does remove is real anyway:

* **`forest/*.forest4.json` is 181 MiB that BeamNG parses line by line at load** (vanilla's
  largest forest is 151 MiB, for 133,375 items against our 1,181,394);
* every item is an entry in the physics broadphase if its type collides — 652,572 today;
* every item is a culling and instancing candidate every frame.

`foliage_density` and `prop_density` are existing settings. 100 → 60 removes ~410,000 items and
~72 MiB of that file, and it is the lever for FRAME TIME, not for RAM.

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
`collidable: false`, plus every non-tree plant (the game's own `PlantObject` split). **A road sign
and a fire hydrant are both taller than 0.5 m, so both collide**, and 2,928 of 3,118 types pass.

Height alone is the wrong instrument: what matters is whether a car hitting it should stop.

**The rule to write, and it should be a FOOTPRINT rule, not a height one:** a type collides only
if its horizontal footprint is wide enough to be an obstacle rather than street furniture. The
shape's own bounds are already loaded (`row["radius"]` is written into every
`TSForestItemData`), so the predicate costs nothing. A sign post is ~0.1 m across; a hydrant
~0.3 m; a bollard ~0.2 m; a tree trunk 0.3-1.5 m; a phone box, a bus shelter and a transformer
are all over 1 m.

**This is OURS and must be declared as such** — CS2 has no collision concept for a placed object
at all, which the existing constant's comment already says. It goes in the report with its count,
like the height rule beside it.

**Work:** `FOREST_COLLIDABLE_MIN_RADIUS` beside the height constant, applied in
`_write_item_data`, counted as `tooThin` in the same stats dict. **Gate:** the report's
`forestCollision` split, and driving into a hydrant.

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
| 5 | P3 density 100 → 60 | 72 MiB of file + frame time | a visibly emptier city | one setting |
| 6 | P4 small-prop collision | collision BVH, not bytes | a hydrant stops being solid | one constant |
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
