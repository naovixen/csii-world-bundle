# A6 — one live regression and nine defects from the A5 and 2026-08-19 loads

A5 closed most of the performance work (audit §64): the build is 19 minutes, the level mounts
5.2 GiB, and **the imposter fix held** — 153 imposter builds this session against 2,652, and the
1–2 FPS collapse is gone. What is left is one performance item that is NOT fixed, and nine
more — seven fidelity, one whole layer that never worked, and one serialisation bug under all
of them. Reported 2026-08-18 across two loads, A6.7 on 2026-08-19, and A6.8/A6.9
out of the 2026-08-19 11:27 load:

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
7. **They are STILL floating or misaligned.** Reported 2026-08-19 after A6.6 shipped. Different
   cause, and not a placement one at all — but not the one this plan first wrote down either:
   **the generated LOD files were written without their source node's transform**, so 670 shapes
   teleported up to 75.9 m the moment they switched level, colliders included. Fixed, together
   with the ladder that put the switch there.
8. **Every `rotationMatrix` in the level is the transpose of the rotation it means.** Found while
   proving A6.9's spot cones point the wrong way; it is one flatten helper, it is not confined to
   lights, and it goes first.
9. **The lights do not work, and the emissive never lights up.** Reported 2026-08-19 after the
   `149601a` load. Five independent defects, four of them ours and all five measured below; the
   "only 500 of thousands" is our own `lights_max`, and neither engine's rule is a static cap.

Everything below the horizontal rule in each section is measured, not assumed. Where a rule is
still open it says so; **none of these should be fixed by inventing a consumer-side rule** (see
CLAUDE.md — the terrain cut cost seven guesses before someone read `TerrainSystem`).

---

## A6.0 The GPU is still running out of memory — 131,073 failed allocations

> **MEASURED ON NAOMI'S LOAD, 2026-08-19 08:03 — a 1,016x improvement, but NOT zero.**
> `totalFailures` **131,073 → 129**, onset 329 s → **1,477 s** (24.6 min in), and it PLATEAUED —
> nothing further in the last 3 minutes of a 28-minute session. The failing allocations changed
> character too: all ten are **1024x1024** BC7, where before they were 64x64. So the card now
> survives loading and 24 minutes of driving and then briefly cannot find room for a large
> texture. **The gate was zero, so this is not closed** — but it brackets the true ceiling to just
> under the 4,685 MiB this level mounts.
>
> **STATUS** — instrument recalibrated (`80f50d7` + `d502f17`, audit §65). The chosen cut
> (both texture caps 512 → 256) is BUILT INTO THE LEVEL and the level mounts **4,685 MiB**,
> down from 5,224. The preset change is deliberately UNCOMMITTED until a load
> validates it. **Gate: `grep "CreateResource failed" beamng.log` → `totalFailures`. Zero.**

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

> **MEASURED ON NAOMI'S LOAD, 2026-08-19 — the gate is met EXACTLY.** `collisionCache` went
> **1,035,444 → 659,257 instances**, a fall of **376,187 — precisely the plant count**. Static
> collision **3,720.81 → 3,311.52 MB** (−409 MB) and **89.1 M → 78.6 M triangles**. The plan asked
> for "roughly the plant share"; it is the plant share to the item.
>
> **STATUS — DONE and BUILT INTO THE LEVEL (`4b6745f`, audit §66).** 84 forest types now ship
> as plants (non-collidable) against 188 tree types keeping their trunk cylinder, and 517,708
> forest items no longer collide. The dividend to look for is `beamng.log`'s static-collision
> block, which was 3,720 MB over 1,035,444 instances.
> The rule is confirmed from source (`TreeObject` adds `Game.Objects.Tree`, `PlantObject`
> does not), `csiibundle.instances.is_tree` is the shared predicate, and both call sites
> follow it. The trunk-fit question was measured and deliberately NOT fixed: the name branch
> fires 0 times in 32, the fallback is wrong for 5 prefabs = 4.5 % of tree items, declared as
> `treeTrunkCylinderFit`. The Blender adapter did not have this problem after all.

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

> **STATUS — BUILT** (`83c2905`, audit §70 + §74). `lod_detail_scale_prop` = 4.0 takes props
> from 105 m to **262 m**, chosen off a measured curve (8,231 props in reach of spawn against
> 884). It costs DRAWN TRIANGLES, not residency, so the reason this was deferred was wrong.
> **Gate 3 is BLIND to it**: `level_budget` counts TSStatics and 94 % of props are FOREST
> items, so its 1.4 % rise (1,827,374 vs 1,801,779) measures nothing about this change. Only
> a load will show the cost; `lod_detail_scale_prop` 5.0/6.0 are the next rungs down.
>
> The game's own sub-object rule is NOT ported and that is structural: BeamNG holds detail
> thresholds per SHAPE, ownership is per INSTANCE.
>
> **The rule as found (audit §70):**
> `PreCullingSystem.RelativeCullingJob` is a HYBRID, neither of the two outcomes below: a
> sub-object takes its **owner's bounds** for the distance but keeps its **own `m_MinLod`**, and
> `CalculateLod` has no size term — so the owner buys the building's extent, not an order of
> magnitude. The plan's second branch is therefore the operative one: the defect is
> `lod_detail_scale` being a single global multiplier over two things whose distances differ
> 12.7x. **Not built yet on purpose** — this is the one A6 item that COSTS residency and drawn
> triangles, and the level is at 4,685 MiB inside the unmeasured band. Do it after a load returns
> `totalFailures` 0.

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

> **STATUS — SOLVED (`199f084`, audit §69).** Read from BeamNG's own shader, not inferred.
> Hypothesis 1 was wrong in its MECHANISM — BeamNG multiplies exactly as CS2 does — but right
> that the paths differ: CS2's mask RGB are three independent lerp weights, BeamNG's RGB only
> CHOOSES among the three colours and the **ALPHA** is the tint amount. We shipped the mask as
> `data_rgb`, which forces alpha to 255, so an untinted texel `(0,0,0)` evaluated to
> **pure black** — and 61 % of tintable texels are untinted. Fixed exactly (alpha = R+G+B, worst
> error 0.001) plus `paletteBaseColor`, which was never set at all. Hypothesis 3 (colour space)
> is still open and now testable — see §69's last section.

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

> **STATUS — BUILT (`868e2c8`, audit §67 + §68).** Question 2 answered, and it is partly ours. CS2 *does* hide
> vegetation something was built over: `Game.Common.Overridden` leaves `PreCullingSystem`'s mask at
> `BoundsMask.Debug`, never ORing in the mesh layers. The producer had never read the component.
> `attrs.overridden` + `should_draw` are built (**awaiting an in-game re-rip**) and both consumers
> get it free — they already call `should_draw`.
>
> **But it explains only 275 of the 4,890** trees in a carriageway (the signature is 3.2x enriched
> near lanes: 5.6 % against 1.8 %). The other ~94 % are ordinary grown trees genuinely close to a
> lane centre, so **question 1 — the threshold sweep — is still open and still the next step**, and
> A6.4 remains mostly a declared deviation.

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

> **STATUS — BUILT, and the diagonal needed no in-game probe after all** (`83c2905`,
> `a9ec823`; audit §73, §74). BeamNG splits **SW-NE** = the bake's `ne_sw`, measured OFFLINE
> off vanilla road nodes on six levels, exact to 0.0000 m; the checkerboard hypothesis is
> falsified. **Gate 1 MET**: paint minus the real triangulation is p90 **+0.023 m**, collapsed
> to the lift ladder (was +0.116/+0.179). **Gate 2 95 % met**: 379,892 m² clipped, 18,871 m²
> left — and the road sits a median **13 mm** above that residual, i.e. coplanar, not
> floating. The clip shipped once clipping NOTHING (wrong key, wrong api) and reported a
> clean 0; both bugs are fixed and a silent zero is now impossible.

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

> **STATUS — DONE and BUILT INTO THE LEVEL (`9baf422`, audit §72).** 6,714 orphans identified over
> 22 absent owners, 6,378 actually skipped (the rest were already skipped for an earlier reason —
> `admit` returns the first). **The proposed discriminator was wrong and the plan's own "check
> before building" caught it**: `kind: "prefab"` covers 87 absent prefabs AND 2 that exist without
> geometry, and `read_declared_gaps` collapses the distinction away. The construction case was
> decided by measurement: 7 sites, 14 children, and the tall ones are the `ConstructionCrane01`
> that §4 says is real — a height threshold would have deleted it. The harness rotation column is
> added and reads **100.0000 %**.
>
> **VERIFIED IN THE BUILT LEVEL, 2026-08-19 (A6.7's investigation).** Walking the 10:14 build's
> `main/MissionGroup` and `forest/` and matching every object back to a bundle record: of the
> ~5,900 descendants of the 12 `unresolved_*` owners, **0 are placed**, and the deliberate keeps
> are all still there — `CampfireSite02` 916 of 964, `ChirperPark03` 698 of 738, `ParkingLot13`
> 657 of 711, the two `Agriculture_Area_Placeholder` 637, `AmericanFootballField01` 216.
> **A6.6 is not the residual, and no other orphan class is either**: counting every level group
> AND every other forest item as a supporter, **568 of 1,146,441 forest items** (0.05 %) stand
> more than 3 m above the terrain with nothing under them, and they are buoys, moored boats,
> bridge-pillar tops and the lights on a radio mast — all correct. **The props Naomi is still
> seeing float are A6.7.**

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

## A6.7 The props do not move — the building under them does, 56 m out

> **STATUS — BUILT, and the cause was a DEFECT, not the tuning this section first diagnosed**
> (audit §77). The half below that says "the level never draws LOD 0" is right; the half that says
> the LOD1 SURFACE is a metre out was measured across a frame mismatch and is **retracted** — see
> *The correction* below. What was actually wrong:
>
> **`bake/lods` wrote every generated level with its vertices in the NODE's local frame, and
> `glbwrite` writes identity nodes.** LOD 0 is placed by the node matrix and the LODs were not, so
> the two disagree by exactly that transform. **791 of 4,453 mesh-bearing nodes carry one** (166
> of them a rotation), which is **670 shapes and 39,565 placements** whose geometry TELEPORTED at
> the switch — straight out of the shipped `.dae`, `Mangr0be_Tree__elderly` jumps **9.0 m** at
> 17.6 m from the camera (10,886 of them), `NA_ResidentialLow03_L5_2x6` **7.0 m**, and
> `Amazon_Warehouse` **75.9 m** (those are centre shifts; the worst VERTEX moves further still —
> `NA_ResidentialLow01_L5_2x6` rotates as well as translates, 4.1 m at the centre and 29.1 m at
> the corner). **And it was a collision defect too**: the colmesh is cut from
> LOD 1, so **628 shapes / 5,870 placements** carried an invisible collider up to 29 m off the
> building you can see. Fixed by `_flatten` (+ the unrotated normals in `_collect`, the same
> oversight one file along), gated in `tools/test_bake_products.py`, and verified after a re-bake:
> the LOD centres now agree with LOD 0's to the centimetre.
>
> **The ladder is replaced as well, because it was still built from the wrong number.** Each
> level now takes its switch point from its own MEASURED displacement
> (`decimate.level_displacement` → `lod.displacement_switch_px`), not from a multiple of the
> shape's cull threshold. Budget: `lod_switch_error_px`, default **6 px**, measured at **+0 % to
> +16 %** drawn triangles over 1.16 M placements (1 px would be +384 % to +630 %). No residency
> cost either way — every level was already in the `.dae` and already mounted.
>
> **MEASURED ON THE REBUILT LEVEL (13:24, cook on).** In the shipped `.dae`, every generated
> level now sits on its LOD 0 — `Mangr0be_Tree__elderly` 9.0 m → **0.48 m**,
> `NA_ResidentialLow03_L5_2x6` 7.0 → **0.05 m**, `NA_ResidentialLow01_L5_2x6` 4.1 → **0.09 m**,
> `Amazon_Warehouse` 75.9 → **0.13 m**, colliders with them — and what is left is the decimation
> itself, which is what the ladder now budgets: **every level ships at 6.0 px of error** (p50
> 6.00, p90 6.01 on 4,228 shapes; 523 LOD1s are identical to LOD 0 and are drawn everywhere).
> `level_budget` on the level that ships: drawn triangles **1,482,557** against A5's 1,469,467
> (**+0.9 %**) and resident **4,675 MiB** against 4,685 — the two numbers the gate asks not to
> move, and they did not. (Those are from the spawn; the simulated downtown cost of the deferrals
> is +16 % to +21 %, which `level_budget`'s single viewpoint does not see.)
>
> **THE FIRST BUILD OF THIS WAS A REGRESSION AND IS CORRECTED** (13:24 → `stages/shapes` v8).
> The switch rule shipped working in BOTH directions, so a level whose surface measured close to
> LOD 0's was allowed NEARER than the ladder had ever put it: **2,586 of 2,903 prop shapes
> (335,072 placements) and 615 of 1,057 building shapes** moved in, a streetlight drawing its
> 24-triangle level from **13.1 m** and a window guard from 7.9 m. Naomi's load: *"made things
> worse … cranes rotated and floating, highrises all diagonal"*. **A surface distance is not a
> visual error for thin or small geometry** — flattening a lamp post's tube moves its surface
> 0.06 m and destroys the object — and the ratio ladder, whatever else is wrong with it, was
> protecting that by keying off the shape's SIZE. The ladder is now the **ceiling**: the
> measurement may only DEFER a level, never advance it, so this can only be an improvement on
> what was there, and `test_a3_objects.test_switch_points_only_ever_defer` gates the direction.
>
> **And vegetation is exempt** (v9). Applying the rule to it deferred 262 of 268 tree shapes over
> **1,346,600 placements** for **+24 %** drawn triangles in a forest — and the reason to decline
> that is not only the cost: the rule's input is a SURFACE DISTANCE, and moving a canopy's leaves
> 0.3 m measures the same as moving a wall 0.3 m while reading as nothing at all. The metric does
> not know the difference, so vegetation keeps the ladder until someone measures foliage error
> properly. **Measured cost of what ships** (1.16 M placements; the two densest forest
> neighbourhoods and the two densest downtown ones): at 8 px **+0 %, +0 % forest and +16 %, +21 %
> downtown**, 1,004 shapes deferred. 6 px is +30 %/+39 % downtown and 4 px +51 %/+67 %, so the
> default moves to 8 px — every gross case is deferred 16x either way, and A6.0 says the card is
> the constraint.
>
> **AWAITING NAOMI'S LOAD**, and the commit waits with it.

### The correction — what the tables below actually measured

The displacement figures in this section were taken by comparing the source mesh in WORLD space
against the generated level file in its own space, i.e. **across the very frame mismatch that
turned out to be the defect**. They therefore measure the node offset, not the decimator. In one
frame the same shapes come out an order of magnitude smaller:

| shape | as tabled below | frame-consistent | its node offset (worst vertex) |
|---|---:|---:|---:|
| `NA_ResidentialLow03_L5_2x6` | 6.640 m | **0.060 m** | 7.00 m |
| `NA_ResidentialLow01_L5_2x6` | 4.580 m | **0.377 m** | 29.12 m |
| `USSW_ResidentialMedium_L1_2x4` | 4.517 m | **0.114 m** | 5.66 m |

Over the whole bundle the real LOD1 distribution is **p50 0.018 m, p90 0.550 m, p99 6.167 m**
(LOD2: 0.057 / 2.030 / 8.290), with **603 of 4,261 shapes whose LOD1 is geometrically identical to
LOD 0**. So **do not quote** the "1.315 m / 30.9 px against 0.024 m / 2.9 px" headline or the
rooftop-support table: both are contaminated. What survives is the SHAPE of the finding — a
building's generated level is an order of magnitude further out than a prop's, and one ladder of
ratios cannot serve both — which is why the switch rule was replaced anyway.

Everything from here to the end of the section is the original 2026-08-19 investigation, kept
because its reasoning about the ladder stands and its numbers show exactly how a frame mismatch
reads when you are not looking for one.

### What is measured

**The ladder is entirely ours.** `stages/shapes` already declares it — `lodLadderSwitchPoints`,
*"CS2 ships no LOD chain for these prefabs (lodCap 0), so there is no game rule to port; the
ratios are vanilla BeamNG's spacing"* — with `count: 4228` and **no cost**. This is that cost.

Switch distances, from the build's own `drawDistance` block and `LADDER_RATIOS`. Each level is
used from its own pixel size upward, so level *n* reaches `ourCull / ratio`:

| kind | CS2 culls at | this level culls at | LOD0 to | LOD1 to | LOD2 to |
|---|---:|---:|---:|---:|---:|
| building | 13,008 m | 1,332 m | **55.5 m** | 222 m | 888 m |
| tree | 4,097 m | 422 m | 17.6 m | 70 m | 282 m |
| prop | 1,024 m | 262 m | 10.9 m | 44 m | 175 m |

What each level costs the geometry — p95 distance from the generated level's surface to the LOD0
surface, instance-median over all 4,100 shapes (`decimate` targets 24.5 % and 6.4 % of the
triangles; a median building goes 5,097 → 1,326 → 351):

| kind | median shape | LOD1 displacement | LOD2 displacement |
|---|---:|---:|---:|
| **building** | 33.28 m | **1.315 m** (4.0 % of the shape) | **2.804 m** (8.4 %) |
| tree | 9.30 m | 0.378 m (4.1 %) | 0.378 m (4.1 %) |
| **prop** | 1.56 m | **0.024 m** (1.5 %) | **0.067 m** (4.3 %) |
| decal | 6.71 m | 0.000 m | 0.000 m |

Put the two tables together with `lod.screen_px`, the repo's own projection, and the defect is one
line — **the error at the moment each level takes over**:

```
building LOD1   1.315 m at  55.5 m  =  30.9 px      prop LOD1   0.024 m at 10.9 m  =  2.9 px
building LOD2   2.804 m at 222.0 m  =  16.5 px      prop LOD2   0.067 m at 43.7 m  =  2.0 px
tree     LOD1   0.378 m at  17.6 m  =  28.0 px
```

A building's LOD1 error is only sub-pixel beyond **1,715 m**; we start drawing it at 55.5 m. The
props standing on it are inside 3 px throughout. **Ten to one — that mismatch is the defect.**

And the consequence, measured directly. 171,842 rooftop props (a child whose local Y is in the top
40 % of an owner over 4 m tall), distance from the prop to the nearest point of its owner's
surface, against each level of the owner in turn:

| owner drawn at | p50 | p90 | p99 | > 1 m | > 2 m | > 4 m |
|---|---:|---:|---:|---:|---:|---:|
| LOD0 (the bundle) | 0.30 m | 0.68 | 1.32 | 4,952 (2.9 %) | **617 (0.4 %)** | 160 |
| LOD1 (from 55.5 m) | 0.40 m | 1.87 | 7.89 | 28,966 (16.9 %) | **16,052 (9.3 %)** | 8,189 |
| LOD2 (from 222 m) | 0.58 m | 2.74 | 10.25 | 49,471 (28.8 %) | **23,720 (13.8 %)** | 10,892 |

### What this clears, so nobody looks there again

Everything A6.6 cleared was re-checked against the shipped bundle **and** the built level, and
three more mechanisms with it. All hold:

| checked | result |
|---|---|
| the bundle's own sub-object geometry | **0** of 171,842 rooftop props overhang their owner's XZ footprint box; transforming each prop's own bbox corners into the owner's frame, 46 of 407,232 overhang by more than 1 m and 43 of those are billboards on a wall |
| a second orphan class (owner is a prop, a network, or nothing) | 189 records hang off a prop that is not placed and **0 of them reach the level**; counting every level group AND every other forest item as a supporter, 568 of 1,146,441 forest items stand >3 m above the terrain unsupported, and they are buoys, moored boats, bridge-pillar tops and the lights on a radio mast |
| shape origin / scale drift through the bake | the level's `.dae` bbox equals the bundle `.glb` bbox for every shape sampled; **every non-vegetation forest type ships `scale: 1.0`** (only trees carry `growthScale`) |
| the terrain the props stand on | `terrainFine − heightmap` at 1,190,757 prop and tree positions: median **0.000 m**, \|Δ\| > 0.25 m on 2.2 % — `terrain_upsample 2` is not moving the ground under them |
| forest rotation | 292,299 of 1,146,441 items carry a non-yaw rotation and it is written in full, so the writer is not flattening anything |

**One genuine LOD0 residual, and it is small and separate.** 1,219 props stand more than 3 m from
any surface of their owner at their own height *and* more than 1.5 m above the terrain **even at
LOD0** — concentrated on about 24 instances of signature and modded assets (`asset_4f0b85f7` 79,
`USSW_PoliceHeadquarters` 58, `Prison01` 50, `Amazon_Warehouse` 45, the `CargoHarbor01` group
33–37 each, `IncinerationPlant01` 15). Rendered from the level at (2835, 144) through
`tools/blender_import_level.py` it is a rooftop duct run that leaves the roof and crosses a street
on nothing. **Whether CS2 draws it that way too is not established** — these are Paradox-mods
assets and a duct bridging two wings of one complex is a thing a modder does. Do not fold it into
A6.7's fix: it is 0.3 % of the rooftop props and it needs its own half hour against the game.

### A separate finding on the way past: nobody knows which decimator ran

`pyfqmr` is **not installed** in this environment, so `decimate.lod_ladder(backend="auto")` fell
back to the numpy vertex clusterer its own docstring calls *"crude"*. `bake/lods.py:73` throws
away `lod_ladder`'s resolved `stats["backend"]` and `:135` writes the **requested** option
instead, so `derived.json` says `"backend": "auto"` and the build report repeats it. A build
cannot tell you which of two algorithms shaped every LOD in the level.

**It is not the cause, and that was worth measuring before assuming it.** Installing `pyfqmr` and
re-decimating the busiest buildings gives the same error, not a better one (p95 displacement, m):

```
                                       L1 cluster  L1 quadric   L2 cluster  L2 quadric
NA_ResidentialLow03_L5_2x6                  6.640       6.265        5.407       6.265
NA_ResidentialLow01_L5_2x6                  4.580       4.629        3.186       4.629
USSW_ResidentialMedium_L1_2x4               4.517       4.562        4.511       4.562
NA_CommercialLow01_L5_5x6                   3.414       3.423        3.096       3.423
instance-weighted mean over the 6 busiest    3.210       3.110        3.066       3.110
```

(Two of the six — `USSW_BonusResidentialMedium01_L1_3x2`, `EU_ResidentialLowWaterfront01_L5_2x6` —
came back from `pyfqmr` at 0.000, i.e. **unreduced**, so the quadric column flatters itself; and it
took 222 s for six shapes against the clusterer's 226 s for all 4,262.) A CS2 residential block is
3,295 triangles of which most are balconies, dormers and window reveals; taking 75 % of them away
removes metre-sized features whatever the algorithm. **So the answer is to switch later, not to
decimate better.** Record the resolved backend anyway — an undeclared approximation is
indistinguishable from a bug once its artefact appears.

### The rule, and it is the repo's own instrument

There is no CS2 counterpart to port: the game has no LOD chain here. But the repo already owns the
right rule for *this* question and already uses it for the cull distance — **a level may be used
where its own error is under a pixel**. `lod.calculate_lod_limit` is that idea and `lod.screen_px`
is the projection. The invented `(1.5, 6.0, 24.0)` is a fixed multiple of the shape's *cull*
threshold, and that threshold is a statement about the shape's SIZE, not about its LOD's ERROR —
which is exactly why one constant lands at 30.9 px for a building and 2.9 px for a prop.

So: **derive each level's switch distance from that level's measured displacement**, the same way
`nullPx` is derived from the mesh's bounds and area. The bake already builds both meshes; it only
has to measure the distance between them and ship it.

### The work — as built

* **`bake/lods._flatten`** (the defect, and it was not in the plan): each node's own transform is
  applied to the level's vertices and normals before it is written, because `glbwrite` emits
  identity nodes. `tools/test_bake_products.py` gates it by bounding box.
* **`stages/shapes._collect`**: the node's rotation is applied to its NORMALS too — 166 nodes were
  lit by an unrotated normal at every level, LOD 0 included.
* `csiibundle.decimate` / `bake/lods`: measure each generated level's displacement from LOD 0 (p95
  of level→LOD0 nearest distance, area-weighted) into `derived/lods/lods.json` beside its triangle
  count, **and record the resolved backend** instead of the requested one. It is a sidecar rather
  than `derived.json` because it is one row per prefab mesh state and the manifest is a staleness
  document; the aggregate distribution does go into the manifest.
* `stages/shapes._lod_levels`: each threshold comes from that error rather than from
  `LADDER_RATIOS` — a level is allowed from the distance at which its displacement is worth
  `lod_switch_error_px`, floored at `nullPx * COARSEST_SWITCH_RATIO` so the ladder still ends. A
  shape whose LOD1 is geometrically identical keeps it to the horizon; a building whose LOD1 is a
  metre out keeps LOD0 until a metre stops showing. **The budget is not a pixel**: measured over
  1.16 M placements from the four densest viewpoints, 1 px costs +384 % to +630 % drawn triangles,
  4 px +1 % to +61 %, **6 px +0 % to +16 %**, 8 px −2 % to +2 %. 6 ships, and the presets run 3
  (ultra) to 16 (low).
* Declare it: `lodLadderSwitchPoints` gains the displacement distribution and the count of shapes
  whose switch moved, so the approximation is counted as well as declared.
* `pyproject.toml` already has the `[lods]` extra; the build should **say** which backend it took
  rather than silently taking one. **Do not just install `pyfqmr` and rebuild** — it was installed
  and removed again during this investigation precisely because of the timing above: 37 s per
  shape against the clusterer's 0.05 s would turn the 226-second LOD bake into roughly forty
  hours, for no measured gain. `derived/lods/lods.json` and `derived.json` now both say
  **`"backend": "cluster"`** on this machine.

**The measurement's own trap, worth keeping.** `decimate.distance_to_surface` is
point-to-SURFACE, not point-to-vertex (a CS2 wall is two triangles across ten metres, so
nearest-vertex answers the distance to a corner). Its first version ranked candidate triangles by
`|p − centroid| − circumradius` and took the best twelve **without checking** — and a big
triangle's circumradius bound is deeply negative everywhere, so a dozen of them crowd out the
small triangle the point is sitting on. Worst error on this bundle: **16.9 m**, which is the size
of the very effect being measured. The shipped version proves each answer against the k-th bound
and redoes exhaustively what it cannot prove; `tools/test_lod.py` gates that exact case.

**The interim, if the error-driven rule is too much for one session**: per-kind `LADDER_RATIOS`,
buildings at `(1.5, 3.0, 6.0)` — LOD0 to 222 m, which covers every distance these screenshots were
taken at. Simulated over 4,100 shapes and 1.19 M placements from four viewpoints, drawn triangles
go 3.95 / 12.18 / 7.54 / 9.81 M → 4.89 / 19.71 / 8.18 / 11.99 M, i.e. **+24 % to +62 %**, with
residency unchanged. `(1.5, 2.0, 4.0)` (LOD0 to 333 m) is +73 % to +92 %; buildings at LOD0
everywhere is 2.6–5.3x and is not a candidate.

### The gate

* re-run the rooftop-support table above against the level's **actual** detail levels: `> 2 m` must
  come back to LOD0's 0.4 %, not LOD1's 9.3 %;
* `tools/level_budget.py` — this ADDS drawn triangles and nothing else. Take the number against the
  1,469,467 A5 left, and take `validate.checks.resident` too, which must not move at all;
* the same free-camera shots Naomi took: a roof at 60 m, 150 m and 300 m, before and after. The
  duct runs and the rooftop kit must sit on the roof at all three.

### It also re-opens half of A6.2

A6.2 raised a prop's cull distance from 105 m to 262 m because façades read as construction sites.
That was right, but it cannot have been the whole cause: **at 56 m the building itself drops to a
level whose surface is 1.3 m out**, and window reveals, mullions and balconies are exactly the
metre-and-under features that go first. Re-shoot A6.2's 150 / 300 / 600 m gate after this lands,
before spending anything more on `lod_detail_scale_prop`.

---

## A6.8 Every `rotationMatrix` we write is the transpose of the rotation it means

> **BUILT 2026-08-19 (audit §78), and it was the defect behind "rotations are still completely
> off".** `rotationMatrix` is one field on `SceneObject`, so the same serialisation carries every
> `TSStatic`, every forest item, every light and the spawn sphere. **BeamNG reads the nine floats
> as three basis vectors in groups of three; we wrote the standard matrix row by row, which is the
> transpose — i.e. the inverse rotation.**
>
> Re-measured independently before building (the reading below reproduced exactly), and then
> settled a fourth way that needs no BeamNG data at all: **CS2 builds a building facing its
> street**, so over 6,000 buildings within 40 m of a lane, the angle to the nearest lane is a
> median **0.01°** under the correct reading and **14.24°** under the one that shipped (94.2 %
> against 19.1 % within 5°). On a north-south grid the transpose is invisible, which is why whole
> districts looked right and the diagonal ones were scrambled.
>
> `beamng.frame.basis_flat` is now the one place the ordering lives; `_add_static`,
> `_forest_line`, `lights._world` and `heading_matrix` all go through it, the Blender level
> importer transposes on the way in, and the harness compares through it instead of agreeing with
> itself. Gate: `test_a3_objects.test_rotation_matrix_is_basis_vectors`, anchored on vanilla
> SpotLights. **Verified on the rebuilt level: 16,768 statics near a lane, median 0.10° off the
> street.** The forest question the plan raised is answered too — 2.48 M vanilla forest items say
> the loader reads it the same way (0.987 against 0.908, and 84 % of items on a slope tip downhill
> as terrain-normal alignment requires, against 50 % = no signal).

### What is measured

Two independent vanilla witnesses, neither of them ours:

```
4,138 vanilla SpotLights      element[5] (group 1's z) <= -0.9 on 3,417   -> lights point DOWN
                              element[7] (column 1's z) >= +0.9 on 2,788   -> the exact mirror: UP
26 vanilla TSStatic barriers  mean |alignment of group 1 with the barrier line| = 0.951
   laid end to end            mean |alignment of column 1 with the same line|   = 0.562
```

A tunnel light cannot point up and a barrier cannot sit across its own line, so the reading is
settled: **elements (0,1,2) are the object's X axis, (3,4,5) its Y axis, (6,7,8) its Z axis**, and
a spot light's beam is its **+Y**.

And our composed matrix is a standard column-basis matrix, which the light data proves directly:
the true beam direction of our 75 street lights sits at elements **(1,4,7)** on 75 of 75, not at
(3,4,5). `stages/instances._quat_rows` returns the standard rotation (columns = basis vectors),
`_swap_yz` conjugates it correctly into BeamNG axes, and then `_add_static`, `_forest_line` and
`_light_object` all flatten it **row by row**.

### Why the city still looks broadly right, and why that is not reassurance

For a pure yaw the transpose is a yaw of **−θ** — still upright, still axis-aligned on a grid,
just facing the mirrored direction. The meshes themselves are mirrored on X by the bundle→BeamNG
axis conversion, and the bundle frame (x east, y up, z north) is left-handed against BeamNG's
right-handed one, so a yaw-only object is *self-consistent* under both wrongs at once. A light has
no mesh to mirror — only a direction — which is why it is the light cones that exposed it.

**The harness cannot see this.** `check_instances_vs_game.py`'s new rotation column compares the
level's matrix against `_transform`'s output — the same function that produced it — so its
100.0000 % is a statement about our own consistency, not about the game. That is the second time
a probe has read 100 % on a question it was not actually asking.

### The work — as built

* one shared flatten in `beamng/frame`: `basis_flat`, `[m00,m10,m20, m01,m11,m21, m02,m12,m22]`,
  called by `_add_static`, `_forest_line`, `lights._world` and `heading_matrix` (the spawn sphere
  was mirrored too, so the car spawned facing the wrong way down its own lane);
* **ANSWERED — the forest loader reads it the same way**, measured on 2.48 M vanilla forest items
  (§78): elongated types laid in runs score 0.987 against 0.908, and of 23,966 items on a slope,
  84 % tip downhill under the groups reading — which is what aligning to the terrain normal does —
  against 50 %, i.e. no signal, under the other. The original caution, which was right to insist
  on it: `.forest4.json` is read by a
  different loader than `items.level.json`, and a tree is yaw-only and upright, so no vanilla
  forest file can distinguish the two readings. The non-vegetation forest items — props — are
  where it matters. Find a vanilla `.forest4.json` holding an asymmetric item in a line and run
  the barrier test above on it; if it comes out the other way, the forest keeps its own flatten
  and that is declared.
* re-check A6.7's residual afterwards: a rooftop duct placed at an exact position but with the
  inverse of its rotation reads as *"the props are misaligned"*, which is the report we have.

### The gate

* the barrier test, run against our own level: for a run of fence or lane-furniture items laid
  along a line, group 1 must align with the line;
* the same free-camera shots as A6.7 — a building whose entrance faced the wrong street now faces
  the right one;
* drawn triangles and residency must not move by a single byte; this changes nine numbers per
  object and nothing else.

---

## A6.9 The lights — five defects, and neither engine's rule is a cap of 500

> **BUILT 2026-08-19 (audit §79), AWAITING A LOAD.** D1, D3, D5 and the cap are in;
> D4 landed in A6.8; **D2 turned out not to be a defect** — vanilla ships every one of its
> 21 levels the same way. See "The work — as built" at the end of this section for what
> the investigation below got wrong. Originally reported as *"none of them are working and I'm
> unable to see the emissive lights; apparently only 500 of them out of the thousands are
> rendering."* Both halves are real and they are **five separate defects**, four of them ours:
>
> | # | defect | reach |
> |---|---|---:|
> | **D1** | every emissive material ships `emissiveFactor [0,0,0]` — the map is multiplied by **zero** | 2,373 materials, **166,545 instances** |
> | **D2** | the lights are `nightLight`s and the level is permanently at **13:05** | all 500 |
> | **D3** | `intensity` is written in CS2's authored Lux, not BeamNG's candela; no `brightness` at all | all 500, ~1,500x too dim |
> | **D4** | `rotationMatrix` is serialised transposed, so every spot cone aims somewhere random | 482 spots (and see A6.8) |
> | **D5** | the 1,896 colour slots — CS2's own emissive drive — are read and never applied | 1,380 reach the city |
>
> **The "500" is ours alone.** CS2 keeps all 176,799 of its punctual lights in the world and draws
> at most `RenderingSystem.maxLightCount` = **2,048 per frame** (1,024/2,048/4,096/8,192 by
> quality), nearest-first by a range-relative distance with an adaptive reject distance
> (`Game.Effects.LightCullingSystem`). BeamNG 0.39 does the same job in the engine — clustered
> shading with frustum, size, occlusion and **importance** culling plus flare substitution for
> what it drops. A static author-time cap of 500 is neither engine's rule, and it is the only
> reason 99.7 % of the city's lights are absent.

### D1 — the emissive map is multiplied by zero

Every one of the 2,373 emissive materials in the built level:

```
emissiveMap present            2,373
emissiveIntensityNits  200.0   2,373      (149601a, correct)
emissiveFactor      [0, 0, 0]  2,373      <- the map never contributes anything
```

BeamNG's shader is `inLayer.emissive = max(0, ld.emissive) … ; inLayer.emissive *= emissiveMapSample`
(`shaders/common/material/shadergen/defaultMat.hlsl:236,369`), so a zero factor is a zero
emission whatever the nits. **`149601a`'s nits fix was multiplied by zero and could not have
worked.**

The cause is a two-file handshake and both halves look right on their own:

* `csiibundle/materials.py:427` — `entry.get("emissiveFactor") or (0.0, 0.0, 0.0)`. **The bundle
  has no `emissiveFactor` field at all** (966 materials carry `emissive`; the key does not exist
  on any of the 4,676), so every resolved record gets `(0.0, 0.0, 0.0)`.
* `beamng/materials.py:235` — `rec.get("emissiveFactor") or [1.0, 1.0, 1.0]`. The fallback was
  written for `None`; a non-empty tuple of zeros is **truthy**, so it never fires.

166,545 instances draw an emissive material — 140,690 props, 21,846 trees, 4,009 buildings. Every
lamp lens, sign face and lit window in the city is currently black.

### D2 — they are night lights, and the level is permanently at 13:05

`nightLight` is not an invention: **3,063 of vanilla's 5,033 lights carry it**, and
`lua/ge/extensions/core/environment.lua:561` reads it —

```lua
if getObjectFieldBoolFast(obj, "nightLight") == true then addNightLightUnique(obj, …) end
…
for _, data in ipairs(nightLights.lights) do setLightEnabledSafe(lightObj, night) end
```

`night` is `isNightTime(tod.time)` = `time ∈ (0.25, 0.75)`. The level ships

```
TimeOfDay  time 0.0451  startTime 0.0451  animate "0"  play false     -> 13:05, and it never moves
```

and the load confirms the consequence in one line:

```
343.245|I|GELua.core_environment.environment.nightLights|
        Registered 500 night lights, 0 night emissive TSStatics and 0 day/night intensity lights
```

All 500 registered, all 500 disabled, permanently. `tod_hour` is `-1` = the CS2 clock at rip
time, and the rip was taken in the afternoon. **Nothing else about the light stage could have
shown up in that load** — D3 and D4 are invisible behind this one.

Note the second and third numbers too: **0 night-emissive TSStatics** — BeamNG's own material
day/night mechanism is there and we use none of it (D5) — and 0 day/night intensity lights.

Vanilla also writes `isEnabled: false` on a night light (2,987 of 3,063); we write `true`, so
ours are lit for the seconds between load and the environment refresh, and then go out.

### D3 — the photometry: wrong unit, no `brightness`, three orders of magnitude too dim

**BeamNG's units, from its own editor** (`editor/inspector.lua:1999-2050`,
`editor/lightClassConverter.lua:35-42`): `intensity` is stored **in a class-specific engine unit
— lumens for a PointLight, candelas for a SpotLight** — `brightness` is *"the actual render
value"*, and the engine keeps the two in sync. The only valid `intensityUnit` strings are
`lm` / `cd` / `ev` / `w`.

Measured over all 5,033 vanilla lights, the sync constant is exact:

```
intensity / brightness = 5000.0   for all 4,047 lights carrying both   (point: after /4pi)
SpotLight   median intensity 15,000 cd   brightness 3.0     range p50 15 m
PointLight  median intensity  5,000 lm   brightness 0.0796
```

We write `intensity: 5.0` or `10.0`, `intensityUnit: "Lux"` — not one of the four — and **no
`brightness` at all**. 10 cd against vanilla's 15,000.

**The right number is already in the bundle and it needs no approximation.** `LightEffect`'s own
arithmetic (decompiled, `Game.Prefabs.Effects.LightEffect`):

```csharp
ComputeLightIntensity()  => LightUtils.ConvertLuxToCandela(m_Intensity, m_LuxAtDistance)   // lux * d^2
ComputeLightFinalColor() => m_Color.linear * ComputeLightIntensity()
                            * CorrelatedColorTemperatureToRGB(m_ColorTemperature) * m_LightDimmer
```

so **`finalColor.a` in `lights.json` IS the light's intensity in candela** — the authored
`intensity` is a near-constant 10 Lux (627 of 638 entries) and `m_LuxAtDistance`, which the
bundle does not ship, is what actually varies. Verified on the file: `intensity 10, range 5` →
`finalColor.a 250` = 10 x 5², CS2's own default `m_LuxAtDistance = 5f`.

| | CS2 `finalColor.a` (cd) | what it is |
|---|---:|---|
| `WallLightShortRange01` (34,006) | 48 | a wall lamp |
| `GardenLight01` (3,705) | 90 | a garden bollard |
| `VFP_CoconutPalmTree01-Illuminated` (43,282) | 250 | palm uplighting |
| `StreetlightSingle01/02/03` (16,039) | 1,562.5 | the road lighting |
| `Oracle_Park` floodlights | 16,000 | a stadium |

Percentiles over all 176,799: p1 36, p50 250, p90 1,562, p99 2,890 cd. That is a physically
sensible spread, it is the game's own resolved value, and it maps **unit for unit**:

```
SpotLight :  intensity = finalColor.a                 brightness = finalColor.a / 5000
PointLight:  intensity = finalColor.a * 4*pi          brightness = finalColor.a / 5000
```

### D4 — every spot cone points somewhere random

Vanilla spot lights point **down**; ours point everywhere:

```
element[5] of rotationMatrix, all 4,138 vanilla SpotLights:  <= -0.9 on 3,417   (down)
element[5] of rotationMatrix, our 482:                        uniform over [-1, +1]
```

The direction is not lost — it is in the wrong place in the array. Taking elements **(1, 4, 7)**
instead of (3, 4, 5) on our own 75 street lights gives `z = -0.996` on **75 of 75**, and every
other class comes out right at the same time:

| our lights | (1,4,7).z | what CS2 says it should be |
|---|---:|---|
| range 18 — `StreetlightSingle*` | **-0.996** | down |
| range 12 — `RoofLight*` | **-1.000** | down |
| range 6 — `LightpolePark01` | **-1.000** | down |
| range 4 — `WallLight*` | **-0.707** | 45° down |
| range 30 — palm uplights | **+1.000** | up |

(CS2 lights shine along their local **+Z**; `StreetlightSingle02`'s entry quaternion takes +Z to
`(0, -0.996, +0.087)` in the bundle frame — straight down.) So the composed matrix is right and
**the serialisation is transposed**. That is A6.8, and it is not confined to lights.

### D5 — the 1,896 colour slots are CS2's emissive drive, and BeamNG has the same mechanism

`csiibundle/lights.py` already says these are *"emissive material state, not light sources"*, and
that is exactly right — but it files them as having no counterpart, and the stage counts them as
an approximation. **BeamNG has the counterpart, in the same shader we are already writing into.**

The game's side, decompiled:

```csharp
// Game.Prefabs.MeshSystem — the buffer the producer reads
dynamicBuffer5[n] = new ProceduralLight {
    m_Color  = float4(mapping.color.linear.rgb,    mapping.intensity * 100f),   // kIntensityMultiplier
    m_Color2 = float4(mapping.colorOff.linear.rgb, mapping.intensity * 100f), … }

// Game.Rendering.ProceduralEmissiveSystem
float4 value = math.lerp(proceduralLight.m_Color, proceduralLight.m_Color2, lightState.m_Color);
value.w *= lightState.m_Intensity;                       // 0 = fully ON, 1 = fully OFF
```

and `EmissiveProperties.SingleLightMapping.materialId` is the join: **a slot drives one material
of the mesh.** So the bundle's `color` is the lit colour, `colorOff` the unlit one, and **the
alpha is the emissive intensity x 100** — which is why every slot's `color` and `colorOff` share
an alpha. (Purposes, on the placed meshes: NeonSign p50 alpha 20, DecorativeLight p50 500.)

BeamNG's counterpart is three fields we already emit into:

* `emissiveFactor` = the slot's lit RGB (D1's zero becomes CS2's own colour);
* `emissiveIntensityNits` from the slot's alpha (declared tuning — see the work below);
* `instanceEmissive: true` on the material plus `nightEmissive` / `nightEmissiveColor` on the
  TSStatic, which is exactly `lerp(color, colorOff, night)`: `environment.lua:585-616` writes
  `instanceColor` to the night colour at night and `(0,0,0)` by day, and `defaultMat.hlsl:253`
  multiplies the emissive by it. Vanilla's `trafficLight` and `floodlight` materials are built
  precisely this way (`instanceEmissive: true`, nits 5,000 and 4,000).

**The join is measurable and it nearly always works by ordinal.** Of the 781 slot-carrying
meshes whose `.glb` is in the bundle:

| slots : emissive materials | meshes | |
|---|---:|---|
| 1 : 1 | 232 | exact |
| 2 : 2 | 407 | exact |
| 2 : 1 | 129 | ambiguous |
| 6 or 4 : 1 | 13 | ambiguous |

**639 of 781 (81.8 %) match one for one.** 672 of the 966 emissive materials in the bundle sit on
a slot-carrying mesh and can take CS2's own colour; the other 294 — building window sheets,
mostly — get a declared default.

**And the vehicle question answers itself.** Splitting the 1,896 slots by whether any instance
references their mesh:

```
1,380 slots on meshes the city actually places   — NeonSign 1,095, DecorativeLight 160,
                                                   EffectSource 53, traffic signals 34, None 29
  516 slots on meshes nothing places             — every Headlight/BrakeLight/TurnSignal, and
                                                   127 DecorativeLight on vehicles and boats
vehicle-purpose slots on a placed mesh:  0
```

Both rip queries already drop `Vehicle` (audit: agents and markers excluded), so no headlight or
brake lamp can reach the level. `purpose` is still the right thing to *state* in the report, but
it has nothing to exclude.

### The performance answer: neither engine caps at author time, and the free tier is enormous

**BeamNG 0.39 ships a clustered light pipeline built for exactly this** (strings and tooltips out
of `Bin64/BeamNG.drive.x64.exe`, presets in `lua/ge/extensions/core/settings/clusteredQuality.lua`):

| mechanism | what it does | Normal preset |
|---|---|---|
| frustum + size cull | *"lights smaller than this fraction of the screen height are dropped"* | `minLightScreenSize` 0.02 |
| occlusion cull | *"drop lights whose volume sits fully behind the visible geometry"* | on |
| **importance cull** | *"colour x brightness x camera exposure x apparent screen size below the floor… **night exposure is far higher so a high daytime floor still keeps all lights at night***" | on |
| flares | *"draw a cheap glow sprite in place of lights too small to shade"* | `lightFlares` true |
| shadows | *"budgeted, not every light updates every frame"* | `shadowMaxCasters` 64 |

That is the same architecture as `LightCullingSystem`: keep everything, cull and rank per frame.
So the design is **not** "pick 500 at build time" — it is "ship the lights and let the engine rank
them", with an author-time budget only where the *scene-graph* cost bites.

Measured, so the budget is a number and not a feeling:

```
176,799 punctual lights on 176 placed prefabs (638 catalogue entries, 2 skipped for bone space)
  by kind:  prop 133,006   tree 43,692   building 101
  by class: 43,282 palm uplights (range 30, 250 cd, 2 per tree) = 24 % of the whole count
            59,669 wall lamps    (range  4, 48-96 cd)           = 34 %
            17,847 street lights (range 18, 1,562 cd)           = 10 %  <- the ones that matter

lights within R of a lit place    R=100 m   R=200 m   R=500 m
                           p50        272       918     5,040
                           p90        557     1,766     8,060
                           p99        907     2,527    10,651
```

A 90° frustum sees roughly a quarter of those, so **~1,300 lights in view at p50 and ~2,700 at
p99 out to 500 m — the same order as CS2's own 2,048/frame cap**, and against vanilla's 4,053
lights in a whole level. That is at the edge, but it is the edge the engine was built for.

Load cost is measured too: the last load put **482 SpotLights up in 0.090 s** (0.19 ms each)
against 18,517 TSStatics in 154 s (8 ms each). All 176,799 would add ~33 s to a 252 s load.

**The two real risks, both nameable:**

1. **`nightLight` does not scale.** `refreshnightLightsObjects` walks every `LightBase` *and*
   every `TSStatic` through `scenetree.findObject`, and `setNightLightsEnabled` walks the
   registered list again on **every** day/night transition. At 500 it is free; at 176,799 it is a
   Lua loop of that size twice a day. Either shard it (only the lights worth switching carry
   `nightLight`) or drop it and let the importance cull do the daylight work — the engine's own
   tooltip says that is what it is for.
2. **`nightEmissive` collides with the palette.** It drives `instanceColor` slot 0, and A6.3's fix
   put the building tint on `instanceColor 0/1/2`; with a `colorPaletteMap` present the shader
   resolves the palette from all three and `shadergen.h.hlsl:178` multiplies the emissive by
   *that*. Writing `(0,0,0)` into slot 0 by day would break the tint. So `nightEmissive` belongs
   on non-tintable emissive shapes only, until the tint moves.

### The work — as built (2026-08-19, audit §79)

**D1, D3, D5 and the cap are BUILT; D4 landed in A6.8; D2 turned out not to be a defect.**

1. **D1 — built.** `csiibundle.materials.resolve` returns `None` for an `emissiveFactor` the
   bundle does not carry instead of substituting `(0, 0, 0)`, and `beamng.materials.material_v15`
   tests `is None` rather than truthiness. `test_bake_products` gates it: no material may carry an
   all-zero factor. 166,545 instances stop being multiplied by zero.
2. **D2 — investigated and CLOSED as not-a-defect.** Vanilla ships every one of its 21 levels at a
   DAYTIME hour with `play: false`, with 3,063 night lights registered and 2,987 of them
   `isEnabled: false` — which is exactly what we shipped. The plan's premise ("the level is stuck
   at 13:05, so the lights are permanently off") is true of vanilla too; the player moves the time
   and `environment.lua` re-runs `applynightLightsState`. So `tod_hour` keeps its default, we now
   write `isEnabled: false` beside `nightLight` as vanilla does (ours were lit for the seconds
   between load and the first transition), and the **build report says the built hour and names
   `tod_hour`** when that hour is a daytime one. The 21:00 screenshot is a screenshot instruction.
3. **D3 — built, exactly.** `csiibundle.lights.candela(entry)` is `finalColor.a x intensityScale`,
   with the authored `intensity` as the fallback only when `finalColor` is absent;
   `stages.lights._light_object` writes `intensity` in the CLASS's own unit (`cd` for a spot,
   `4*pi*cd` lumens for a point) **and** the `brightness` the renderer uses, at BeamNG's own exact
   5,000:1 ratio — measured on all 4,047 vanilla lights that carry both, and the ratio is fixed by
   the class, not by `intensityUnit`. The module docstring's old "use `color` x `intensity`"
   advice is retracted in place. Gated in `test_a3_objects.test_lights`.
4. **D4 — landed in A6.8** as one flatten helper, as predicted.
5. **D5 — built, both halves, and the re-rip landed (audit §81).** `bake.materials._emissive_slots`
   writes CS2's own lit colour into `emissiveFactor` and `alpha x 10` nits into a new
   `emissiveNits`; the join runs BEFORE the dedup because `texture_set_key` includes both.
   `AppearanceRipSystem.ReadLights` ships the game's own `materialId`/`layerId` and
   `glb.primitive_materials` is the slot-indexed list to join it against, so **on the 2026-08-19
   rip 765 of 778 meshes join exactly and NOTHING is ambiguous** (785 materials); the 13 left name
   a material LAYER, which the bundle has no counterpart for, and are all vehicles. The ordinal
   path is kept for an older bundle and reports itself when it runs.

   **One correction the new data forced:** 115 slots are authored with intensity **0** — every one
   the unlit face of a billboard — and `material_v15` tested the nits for falsiness, so a real
   zero would have taken the flat 200 fallback. `is None` now, the same distinction D1 was. It
   changes nothing on this city (none of the 115 carries an emissive map) and is gated.

   **Two things the investigation had wrong.** `nightEmissive` is not "for non-tintable shapes
   only" — it is unusable, measured: **all 639 joined shapes carry a `colorPaletteMap` material**,
   so `instanceColor` slot 0 is the palette's own channel on every one of them and writing
   `(0, 0, 0)` into it by day would black out the building tint. And 12 materials are contested
   (five neon frames share a texture set across three sign colours); first-in-sorted-order wins
   and the rest are counted.
6. **The cap — built.** `lights_max` 500 → **25,000**, and the ranking is BeamNG's own importance
   metric (`luminance x candela x range`, the engine's tooltip with exposure and distance factored
   out) instead of reach alone. The old rank was the real defect: the city's 43,282 palm uplights
   outrank its 17,847 street lights by reach, so **a cap of 500 placed no street light at all**.
   25,000 is the measured knee — the smallest cap that takes every street light, before the palm
   uplighting starts arriving at 50,000.

### The gate — as it stands

* `grep "Registered .* night lights" beamng.log` — must now read 25,000, not 500. **`night
  emissive TSStatics` stays 0 and that is now a measurement, not an omission** (see D5 above).
* a screenshot at **21:00** of a lit street, a neon frontage and a motel sign — set the time in
  game, the level ships at the rip's hour by design;
* `tools/level_budget.py`: lights add no residency and no triangles, so `validate.checks.resident`
  and drawn triangles must not move. What moves is frame time, and the number to take is BeamNG's
  own clustered-shading panel — *"lights in scene / lights sent gpu / importance culled"*;
* the spot cones: element[5] of a `StreetlightSingle*`'s matrix must be <= -0.9 (A6.8's gate,
  which this depends on).

---

## Order

> **ALL OF A6.0–A6.9 IS NOW BUILT, ON A FRESH RIP (2026-08-19 16:32, audit §81).** Both producer
> changes the plan was waiting on landed and are verified in the output: A6.9/D5's `materialId`
> (1,416 slots carry it, 462 carry a `layerId`, none neither) and A6.4's `attrs.overridden`
> (33,511 instances). The level `cs2_city_fine` was rebuilt in place from that rip and awaits ONE
> load. The order below is kept because it records why each item went where it did.
>
> **What the re-rip changed by itself:**
>
> * **A6.9/D5's ordinal guess is retired.** 765 of 778 meshes join by the game's own key, **zero
>   ambiguous** (was 639 by ordinal with 142 ambiguous); 785 materials take CS2's own emission.
>   The 13 left name a material LAYER and are all vehicles.
> * **A6.4 went live with no consumer change** — `should_draw` already read the field. **33,106
>   instances the game does not draw are no longer placed.**
> * The asset packs were NOT enabled (89 unresolved prefabs, unchanged), so A6.6's orphan rule
>   still carries them: 7,629 identified, 6,964 skipped.
>
> **What that load has to answer, and nothing else can:**
>
> 1. **A6.0** — still the only open item, and it moved the WRONG WAY. 129 failed allocations at
>    24.6 min on the last load; the level now mounts **4,282 MiB** (4,003 → 4,063 for A6.3's colour
>    masks, then → 4,282 for the bigger rip's 201 extra meshes), which is **3.1x**
>    `west_coast_usa` and still inside the unmeasured band — fits at 3,473, known to fail at 5,224.
>    Gate: `grep "CreateResource failed" beamng.log`.
> 2. **A6.3, for the first time.** It shipped NOTHING until today — audit §80: the conversion was
>    written into the naming function and the stage version was never bumped, so the built level
>    carried zero `colorPaletteMap` and not one mask file. It now carries 1,398 and 724. Whatever
>    the buildings looked like on the last load was NOT A6.3.
> 3. **A6.9's emissive half at a NIGHT hour.** The level ships at 13:05 by design (vanilla ships
>    all 21 of its levels by day); move the time past 18:00 in game, or rebuild with
>    `tod_hour 21`. Nothing about the 25,000 lights or the neon can be judged at 13:05.
> 4. **A6.7 and A6.8's own gates**, which have never had a load: a roof at 60/150/300 m, and a
>    building square with its street.
> 5. **Frame time with 25,000 lights** — BeamNG's clustered-shading panel (*"lights in scene /
>    lights sent gpu / importance culled"*), which is the one number the cap default was set
>    without.
>
> **The one producer-side thing still outstanding is A6.6's asset-pack enable** — 89 prefabs still
> do not resolve, and their 7,629 orphans are dropped rather than drawn. That is a settings change
> in the game, not code.

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

**A6.8 goes first of everything, A6.0 included.** It is one flatten helper, it costs nothing at
all, and until it lands every orientation in the level — objects, forest, lights — is the inverse
of the one intended, so any screenshot taken to judge another item is taken against a level that
is wrong in a way nobody is looking for. It also has to land before A6.9's spot cones can be
judged, and it may take a bite out of A6.7's residual.

**A6.9's D1 next, ahead of A6.7.** Two lines, no rebuild of anything but `materials`, and it
turns 166,545 instances from black to lit; it is the single largest visible change available in
this document for the least work. **D2 with it** — a `tod_hour` decision, one line, and without it
neither D1 nor anything else about the lights can be seen at all. D3 and D4 follow immediately
(they are small and they are exact ports); **D5 and the cap are the real piece of work**, and the
cap in particular wants a load and BeamNG's own clustered-shading panel before the default moves
far. Ask Naomi for the re-rip that lands `materialId` at the same time as A6.6's asset-pack
enable — both are producer changes and they ride one rip.

**A6.7 is BUILT (2026-08-19, audit §77) and is awaiting a load.** What it turned out to be is not
what this section first wrote down — the generated LOD files were being written without their
source node's transform, so 670 shapes and their colliders teleported at the switch — and the
switch rule was replaced in the same pass. **Its load is still worth taking before A6.8**, because
the two are independent and A6.7's own gate (a roof at 60/150/300 m) reads on a level that still
has A6.8's inverted rotations: an object in the RIGHT PLACE facing the WRONG WAY is A6.8, an
object in the wrong place is not A6.7 any more. The original reasoning for the queue jump, kept
because it still explains the ordering against A6.2:

**A6.7 jumps the queue, above everything except A6.0.** It is what Naomi is actually reporting
now, its investigation is finished, it costs **no residency at all** (the levels are already in
the shapes and already mounted — only which one is drawn changes), and it re-opens half of A6.2,
so doing A6.2 before it would tune against a moving target. Take the interim per-kind ratios first
if the error-driven rule looks long: it is one constant, one rebuild and one load, and the gate is
`level_budget`'s drawn-triangle number against A5's 1,469,467.

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
