# A6 — three fidelity defects from the A5 load

A5 closed the performance work (audit §64): the build is 19 minutes, the level mounts 5.2 GiB, and
the imposter fix held in game. What is left is fidelity. Three defects, reported 2026-08-18 after
the load:

1. **Bushes collide.** Only trees should, and only their trunks.
2. **Buildings lose their windows a few hundred metres out** while the building itself is still
   drawn — they read as construction sites.
3. **Some buildings render at wrong contrast or completely black.**

Everything below the horizontal rule in each section is measured, not assumed. Where a rule is
still open it says so; **none of these should be fixed by inventing a consumer-side rule** (see
CLAUDE.md — the terrain cut cost seven guesses before someone read `TerrainSystem`).

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

## Order

**A6.1 first** — it is the only one whose rule is already in hand, it is small, and it pays back
memory as well as fidelity. **A6.3 second**: it is the most visible defect and hypothesis 1 is a
half-hour read of a shader. **A6.2 last** — it needs the game's culling rule read properly, and it
is the only one of the three that costs performance to fix.

## Not in scope, but adjacent and known

* `objects 373.484s` of a 396.982 s load (`beamng.log`). Imposter generation ran right through
  that window and should have taken most of it with it — re-measure on the next load before
  spending anything on it.
* 235 MiB of terrain textures still ship as `.png` and are cooked at first load, unlike every
  other texture in the level.
* `bake/terrainmask` (78 s) is the last serial bake product; `terrain.clip_span` is 2.37 M numpy
  calls on tiny scanline spans, and row-band partitioning would parallelise it exactly (disjoint
  rows preserve the `refs` last-wins order).
