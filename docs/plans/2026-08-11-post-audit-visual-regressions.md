# Post-audit visual regressions — terrain holes, lifted markings, elevated deck gaps, unblended ground

*Written 2026-08-11. Every number below is measured against the 2026-08-10 19:53–20:40
full-city rip.*

## Outcome, 2026-08-11 (worked in this order; read this before the body)

| item | outcome |
|---|---|
| **1a** face-centre cut | **BUILT** `4b97d65`. 10,892 → 8,224 faces cut in the A/B window, **x1.32 → x1.00** of true clipped area, 2,668 faces ≈ 32,683 m² of terrain returned. |
| **1b** multi-strip snap | **BUILT** `80676e0` — the earlier "not needed" call was **wrong**. It rested on the unreferenced count being 0, which measures whether a vertex found *a* strip, not the *right* one. 46–50% of boundary vertices at the reported junctions are claimed by more than one strip, and the arbitrary first-ref pick lands a median 2.03 m off. |
| **1a′** split clip band | **BUILT** `40e680a`, after the user's re-rip still showed junction gaps. The face-centre rule's *unbiased* error was the wrong target — half of it deletes terrain no road covers. See "the gaps after the re-rip" below. |
| **1c** report the residual | **BUILT** `4b97d65`. Deleted area vs clipped-cell area as a ratio; `_snap_hole_boundary` returns the unreferenced count. |
| **2a** unpack paint in numpy | **BUILT** `91ccc7a`. Bit-exact (readback error 0.0000 on all four channels over 4096²). |
| **2b** shipped tiling | **BUILT** `91ccc7a`. Three scales per ground blended by camera distance; grid gone. |
| **2c** soil softener | **NOT BUILT** — correctly gated. 2a/2b removed the visible defect; nothing measured says the ramp is too narrow. |
| **2d** ship extra3/4 params | **BUILT** `e468704`, awaiting re-rip. `m_Extra3*`/`m_Extra4*` confirmed by decompile. |
| **3a** enumerate hits, pick by curve | **FALSIFIED AND REVERTED.** See below. |
| **3b** kerb_rejections | **NOT NEEDED.** The counter was never blind; the event is genuinely rare. |
| **3c** terrain fallback for misses | **BUILT** `e052fb5`. 374 of 1,045 misses recovered; the rest now warn. |
| **3d** read `decalRole` | **BUILT** `4b97d65`. 7 of 43 parts were misclassified. |
| **3e** city-wide BVH cost | **NOT MEASURED** — still open. |
| **4a** diagnose deck gaps | **ALL THREE NAMED SUSPECTS ELIMINATED**, root cause still open. See below. |
| **4b** gate conform on elevated | **BUILT** `e468704`, awaiting re-rip. |

### Item 3 was wrong — the marking lift is not a kerb

`tools/test_drape_pick.py` (new) parses the `.glb` and casts the vertical ray itself in numpy,
sharing no code with the harness. It refutes both halves of item 3's mechanism:

- **Nothing shadows the carriageway.** 24,059 of 24,060 samples have exactly **one** surface
  beneath them — segment meshes are single-layer shells, not stacked compositions. There is no
  hidden carriageway under a kerb to recover.
- **Enumerating every hit changes nothing.** All-hits + nearest-to-expected reproduces the
  shipped rule *to the digit* (0.21% of samples > 2 cm above their own road, max +0.298 m, at
  the city's densest 300 m window).
- **The proposed pick is worse.** Highest-candidate-below-the-lane-curve doubles the lift, to
  0.46%.
- A choice between surfaces only ever arises between **neighbouring objects**, and only where
  they stack: 2.1% of samples at the densest window, **0.0%** in a plain road window. That is
  why `kerb_rejections` reads 1 — not blindness.

The 4.4%/7.1% figures in the body below measured the road **crown standing above the lane
curve**, which is what the drape exists to follow, and attributed it to kerbs. `drop()` is
unchanged; its docstring now records this so it is not re-litigated.

### The gaps after the re-rip — resolved against the game's source

The user re-ripped, confirmed items 2 and 4b fixed, and reported residual gaps at
`(873.9, 1442.0)` (close train tracks) and `(2936.9, 1397.5)` (road junction).

`Game.Simulation.TerrainSystem.GenerateClipDataJob` settles it. Every clip volume is built
from the road's **own** composition — left/right curves trimmed by
`TerrainComposition.m_WidthOffset`, band widened by exactly 0.3 m at each end (so the
producer's `ClipBandExpansion = 0.3f` is verbatim, confirmed) — and the terrain is discarded
**per pixel** inside it. In game, therefore, `clipped ⊆ covered by road mesh` always holds and a
see-through gap is impossible.

A 3.5 m mesh cut can only approximate that boundary, so the question is which way to err, and
*unbiased is the wrong answer*: half of an unbiased error deletes terrain no road covers, and
that half is sky. But erring inward everywhere walls off tunnels. The band splits at the
carriageway — which is where the game's own geometry divides "hidden by the road mesh" from
"hidden only by the clip" — and the lane curve is the road surface exactly (`road_top −
lane_path.y` median **0.000 m**).

Centreline faces deleted, by rule:

| rule | tunnels/trenches | surface shell |
|---|---|---|
| face-centre | 98.1% | 98.4% ← the gaps |
| containment only | 88.6% ← walls off tunnels | 88.6% |
| **split band** | **99.0%** | **88.7%** |

See-through gap area (terrain hole with no road mesh over it) across the day:

| | face-centre | + nearest snap | + split band |
|---|---|---|---|
| train tracks | 1.260% | 0.521% | **0.156%** (−88%) |
| road junction | 0.208% | 0.081% | **0.018%** (−91%) |

**Still an approximation.** Exactness needs sub-cell clipping — cutting terrain faces along the
prism boundary rather than deleting whole ones. That remains the only way to reproduce the
game's per-pixel result, and it is still a rewrite of the lattice builder. The rule above is
the closest bounded approximation that never violates the game's containment invariant.

### Item 4a — the suspects are dead, the cause is not found

Measured, not argued:

- **No geometry is dropped.** Imported meshes match their `.glb` triangle-for-triangle
  (2,940/2,940/2,940/3,456 on four elevated road segments), up-facing deck area ~1,600 m² each.
  Suspect 2 (the quiet degenerate drop) cannot make a hole.
- **Alpha is not it.** Every road material imports with `Alpha = 1.000 constant`; the glb
  declares no `alphaMode` at all, so `surface_render_method = DITHERED` has nothing to dither.
- **Backface culling is not it.** `use_backface_culling` is on (from `doubleSided: false`), but
  the plan's own measurement puts inverted-winding triangles at 99 = 0.000%.
- **Suspect 1 is real but cannot cause gaps.** The fast path does silently drop `TANGENT`
  (network glbs all carry it). A missing tangent misorients normal maps; it removes no surface.

A fresh import of the interchange renders complete decks — the defect did not reproduce from
the coordinates available. **Next step needs the user:** put the 3D cursor on one gap in the
live `splat.blend` and run `tools/probe_point.py`, then join back to `networks.jsonl`. The
producer remains exonerated by the body's measurements.

## Why this exists

After audit #40's gap-close (`095322a`, `b549bbe`, `4d6959d`) and the SPLAT work (`be062e8`,
`15eb2da`, `3ec61bb`, plus the uncommitted SPLAT-3/SPLAT-6 tree), the user re-ripped and
imported (`E:\carto-out\splat.blend`, `--terrain --networks --lane-markings`). Four defects:

1. Terrain gaps where roads run close together — junction arms, parallel carriageways.
2. Road markings floating above the road.
3. Elevated train/highway decks showing long streaks of missing surface
   (`E:\carto-out\shots\TRACK GAPS.png`).
4. No blend between ground textures, and an obvious repeating grid.

**Bundle staleness:** the bundle post-dates every producer commit and was ripped with the
uncommitted `TerrainRipSystem`/`GradingWriter` work live (`grading.json` at 20:40), so it is a
faithful description of today's producer. Producer fixes below are **batched into one re-rip at
the end** (the user's call); nothing producer-side is "validated" until that rip lands.

## What the audit did and did not cause

| # | audit regression? | verdict |
|---|---|---|
| 1 | **No** | Structural. `e0b0bbe` (08-08) took the clip layer from 1,134 to 49,730 strips — every surface road now ships a ±0.3–0.5 m shell (`bundle-schema.md:1417`, intended). The audit only *narrowed* bands. The over-deletion is a harness rule. |
| 2 | **No** | Structural in `_ProjectionSurface.drop`. Nothing in waves 2/3 moved a network vertex or a lane curve; measured `road_top − lane_path.y` median **0.0000 m**. |
| 3 | **No — producer exonerated by measurement** | See item 4. The defect is downstream of `networks/*.glb`. |
| 4 | **Partly yes** | The repeating grid is old (P5's consumer half, never done). The *hard-edged paint* is new: the uncommitted SPLAT-6 tree added a `Closest` tap + `FLOOR` nibble decode. |

---

## Item 1 — the terrain cut deletes a cell it should not (harness)

`tools/blender_import_bundle.py`

### Mechanism

The cut is decided **per cell** (`_fill_clip_quad:2612-2613`; contract §3 puts vertices at cell
centres) and then applied **per face** by OR-ing four corners (`:3361-3364`). A face spans four
cells, so the deleted region is the cut mask dilated a full cell (3.5 m) in **every** direction —
a systematic outward bias, never inward. `_snap_hole_boundary` cannot recover it where two roads
are within ~7 m, because the whole interstitial strip is deleted (no surviving face to snap) and
because a boundary vertex adopts **one** arbitrary strip ref from its 3×3 neighbourhood
(`:2841-2850`).

Simulated offline against the shipped `clipvolumes.jsonl` + `heightmap.png`, densest 350 m road
window (centre 2994, 1853; 1,109 strips; 10,000 faces):

| rule | faces deleted | share |
|---|---|---|
| true clipped area (cut cells) | — | 34.9% |
| **any-of-four (today)** | 4,574 | **45.7%** |
| **face centre (proposed)** | 3,425 | **34.2%** |
| all-of-four | 2,329 | 23.3% |

### 1a. Face-centre deletion rule

A face's centre sits at a cell *corner* — integer coordinates in the cell space
`_fill_clip_quad` already uses. Rasterise a **second** mask sampled at integers with `terrain_y`
bilinearly interpolated at the face centre, and delete a face iff its own centre is inside the
prism. Parameterise the sample offset (the hardcoded `row + 0.5` at `:2591` and the
`ceil(x-0.5)`/`floor(x-0.5)` column coverage at `:2609-2610`) so one routine serves both passes.

Both rasters stay: the cell-centre pass still feeds `refine_cells` (`:3284`) and the `refs`
raster the snap projects from. Only deletion moves to the face-centre pass.

Lands on the true clipped area to within 0.7 points — unbiased, where today over-deletes by 31%.
In that one window it returns **1,149 faces ≈ 14,075 m²**, exactly the terrain between and
beside close-packed roads.

### 1b. Multi-strip boundary snap *(gated — measure after 1a)*

`_snap_hole_boundary:2841-2850` takes the first non-negative ref in the 3×3 neighbourhood.
Gather all distinct refs, project onto each candidate's side polyline, keep the nearest — the
"nearest prism wall" rule it already uses within a strip, extended across strips. With 1a's bias
gone far fewer vertices need snapping, so measure the residual before spending this.

### 1c. Report the residual

`build_terrain` prints `cut_faces` and `snapped`. Add deleted-face area as a multiple of
cut-cell area, and the count of boundary vertices that found no ref. A silent cap reads as
"covered everything".

**Not doing:** exact sub-cell polygon clipping. It is the only way to represent a 5 m sliver
exactly and it is a rewrite of the lattice builder. 1a removes the bias that causes the visible
failure at a fraction of the risk. If gaps survive at the tightest junctions, that is next — and
measured first.

---

## Item 2 — the ground blend and the repeating grid

`tools/blender_import_bundle.py` (+ one small producer follow-up)

### 2a. Unpack the paint channels in numpy; sample the result linearly

The `Closest` tap and `FLOOR` decode (`:3005-3018`, `:3060-3065`) exist because interpolating a
*packed* byte is meaningless — true, and the answer is to stop asking the GPU to do it. Splat
texels are 3.5 m, so point sampling gives every painted region (including the beach sand)
literal 3.5 m square steps, and `FLOOR` makes that unavoidable even on a linear tap.

Decode `B`/`A` into four real 0..1 weights with numpy at import, pack them into one RGBA Blender
image (`extra1..extra4` in R,G,B,A), wire the extras off **that** at `Linear`. The second image
node, the `FLOOR`/`SUBTRACT`/`DIVIDE` chain and `nibble()` all go away.

**The decode is already paid for**: `_Splat.__init__:2476` calls `_decode_png_rgba` and holds the
whole 4096² RGBA buffer in `self.pixels`. This is one numpy view plus two shifts. Build the image
in memory (`bpy.data.images.new` + `pixels.foreach_set`); do not write a derived PNG into the
bundle. While there, `_Splat.sample`/`_Splat.uv` (`:2489`, `:2491`) are dead — delete them rather
than leave two decode paths.

### 2b. Read `rasters.terrainTiling` instead of guessing 8 m

`:3440` UVs the whole terrain at `loop_x / 8.0` — 8 m per repeat for every ground — while the
bundle has shipped the real numbers since `095322a` and nothing reads them (`grep terrainTiling
tools/` → zero hits). This is audit **P5**, producer half done, consumer half never started.

Replace the single `UVMap` with a per-ground `ShaderNodeMapping` scaled from the shipped
`metresPerRepeat`, and reproduce the game's own anti-tiling: it blends **three** scales per ground
by camera distance (`_Terrain*TextureTiling` / `_Terrain*TextureTilingBlend`, decompiled in
`Game.Rendering.TerrainMaterialSystem.UpdateGrassTiling` et al). Shipped for this city:

| ground | metresPerRepeat near / mid / far | lodBlend (camera m) |
|---|---|---|
| grass | 14.34 / 71.68 / 358.4 | 100, 1000, 2000, 4000 |
| dirt | 20.48 / 71.68 / 238.93 | 100, 1000, 2000, 4000 |
| rock | 68.27 / 204.8 / 409.6 | 100, 1000, 4000, 8000 |

Extras tile with the **grass** layer — the bundle's own `semantics` says so
(`_TerrainExtraTextureTiling` is bound from `m_TerrainGrassTiling`). Drive near→mid→far from
`ShaderNodeCameraData`'s View Distance through two `Map Range` nodes using `lodBlend`. Today's
8 m is *tighter* than the game's grass near tiling (14.34 m) and 8.5× tighter than rock, so this
both fixes the grid and softens it. Keep the 8 m fallback when the block is absent, and log it.

### 2c. Do not invent a softener for the two soil channels yet *(gated)*

`opacity_of` is `min(w × splatMultiplier, 1)` with `splatMultiplier = 2.0`, so R (251.6 flat) and
G (201.7 flat) pin fully opaque over ~97% of the map and the whole grass→rock ramp is compressed
into byte values 127→0. That may be what the game does — its softness comes from
`triplanarHeightMapBlending` (0.25) and the per-extra `splatRange`/`blurDepth`, none of which the
harness reproduces. **Measure first:** with 2a and 2b in, render the same patch as
`shots/INGAME-COLOURS-*.png` through `tools/ab_render.py` and measure transition band width in
metres at a shoreline and a cliff foot, in-game vs Blender. Only if the ramp is measurably
narrower should a height-blend go in, driven by shipped `splatRange`/`blurDepth`, never a fitted
constant. This is the audit's own "fitted to the wrong patch of ground" lesson.

### 2d. Producer — ship `extra3`/`extra4` tiling params

`Systems/TerrainRipSystem.cs:1894-1900` emits `extras` for `extra1`/`extra2` only, a leftover
from when the other two were believed unrepresentable.
`Game.Prefabs.Terrain.TerrainRenderSettingsPrefab` carries identical `m_Extra3*`/`m_Extra4*`
fields (verified by decompile) and SPLAT-6 proved all four grounds live. Additive, no schema bump.

---

## Item 3 — lane lines and crosswalks lifting off the road **(root cause found)**

### Mechanism, at `_ProjectionSurface.drop` (`:1460-1488`)

```
1476   for index in self._grid.get((int(x // CELL), int(y // CELL)), ()):
1480       location = tree.ray_cast(origin, self._DOWN, half_up + half_down)[0]
1482       candidates.append(location.z)
1485   nearest = min(candidates, key=lambda c: abs(c - z))
1486   if len(candidates) > 1 and nearest != max(candidates): self.kerb_rejections += 1
```

`BVHTree.ray_cast` returns the **first hit along the ray**, and the ray starts at `z + half_up`
pointing down. So each object contributes **exactly one candidate — its own topmost surface
inside the window**. The documented "nearest-to-expected instead of highest" rule
(`:1464-1472`) therefore discriminates only *between objects*; it **cannot discriminate within
one object**.

A network segment **is** one object. `place_network_mesh:977-999` imports `<assetId>.glb` and
links it straight into `network_collection`; segment glbs are one node / one mesh / two
primitives, which both `_fast_import_glb:4583-4607` and the stock importer build as a single mesh
object with two material slots. And that mesh is the **whole composition** — carriageway, kerbs,
sidewalk caps, medians, parapets (`NetworkRipSystem.cs:1687-1716` merges edge + both node flares;
`NetworkRipSystem.GameMesh.cs:495` iterates every `NetCompositionPiece`).

**So wherever a drape sample's (x, z) falls over a kerb, cap or median standing above the
carriageway and inside the ±0.5 m window, that raised surface is the only candidate the road's
own object ever offers, and `drop` returns it unconditionally.** `_drape_block:1960` then writes
`top + 0.014`.

`half_height = (bmax[1] - bmin[1]) * 0.5` (`:1688`) is **exactly 0.500 m for every `*Line*` and
`*Crosswalk*` prefab in the bundle**, so a 0.15–0.30 m kerb sits comfortably inside the window.

### Measured on the shipped bundle

Simulating `drop` exactly (per-object topmost, nearest-to-expected) over real segment meshes with
each prefab's real bounds:

| | samples | lift > 2 cm | > 5 cm | > 10 cm | max lift |
|---|---|---|---|---|---|
| crosswalks (250 meshes, 7 stations × 3 lateral) | 5,250 | **4.4%** | 2.8% | 1.9% | **+0.317 m** |
| lane lines (80 segments, all `*Line*` parts) | 13,428 | **7.1%** | — | 0.8% | +0.175 m |

Picked surface minus the true road top beneath it: median 0.000, **max 1.807 m**. Lane lines also
*sink*: 9.4% of samples more than 5 cm (p05 −0.201 m) where the window's topmost hit is below the
lane curve.

`kerb_rejections` is **structurally blind** to all of it — it requires `len(candidates) > 1`, and
this failure has exactly one candidate. It would fire on **6 of 5,250** crosswalk samples
(0.11%), i.e. ~1/40th of the samples that actually lift. It reports a reassuringly tiny number
while ~4% of crosswalk rows sit on a kerb.

### 3a. Enumerate every hit in the window, then pick by the lane curve

Re-cast down the same ray from just below each hit until the window is exhausted, so each object
yields **all** its surfaces, not just its topmost. Then choose by the authority that actually
knows which surface the marking is painted on — the lane's own curve: take the highest candidate
that is **not above `curve + tolerance`**, falling back to the nearest below. This fixes both the
lift and the 9.4% sink in one rule.

### 3b. Make `kerb_rejections` count the real thing

Once all hits are enumerated, count "a candidate above the chosen one was rejected" rather than
"more than one object disagreed". Report it in the summary alongside `draped`/`drape_missed`.

### 3c. Give the miss path a terrain fallback

A miss silently keeps the authored height, and the two projection surfaces are disjoint —
markings drape onto the **network BVH only**, never terrain (`:5192` vs the areas' `ground_surface`).
Fall back to terrain and `LOG.warn` when misses exceed a trivial fraction.

### 3d. Read `decalRole` instead of matching strings

`:1726-1727` classifies by `("Line" in asset_id or "Crosswalk" in asset_id)`. `4d6959d` added
`decalRole` to `laneprefabs.json` *specifically to retire that test* and the harness never reads
it (`grep decalRole tools/` → zero hits). On the shipped catalogue: 43 decal parts, all 43
classified (38 `marking`, 5 `wear`); the substring test disagrees on **7**, all one way —
`Gravel_Pavement_Transition`, `Tiled_Transition`, `Train_Rails_Cover`, `Train_Sleepers_Lod`,
`Tram_Rails_Base`, `Tram_Rails`, `Public_Transport_Lane_3` are `marking` but get the 8 mm tint
epsilon instead of 14 mm. Not a lift mechanism by itself (6 mm), but a live gap against a field
the producer already ships.

### 3e. Note — a city-wide BVH on every marking ray

`build_networks` links the merged centerline ribbons (one object per `(category, form)`,
`lift=0.05`) into the same collection `_ProjectionSurface` is built over. Each is a single
city-wide object, so `__init__:1432-1437` registers it in **every** 128 m grid cell its bbox
spans, and `_tree` builds a BVH over the whole city's cables/pipes/waterways/taxiways the first
time any drape query lands anywhere. Every marking sample ray-casts that in addition to the road.
Worth excluding non-drivable ribbons from the projection surface — but measure first
(`prefer-simple-over-speculative`).

---

## Item 4 — elevated deck gaps: the producer is exonerated, look at the harness

### What was ruled out, with numbers

- **No terrain cut under elevated track.** Elevated *edge* clip volumes cut **zero** cells around
  the longest elevated rail runs; elevated bands sit 10–20 m above terrain (median +18.31 m for
  elevated highway) with ±0.5 m thickness.
- **No missing meshes.** 0 of 597 elevated roads / 471 elevated highways / 50 elevated trains
  lack their `networks/<assetId>.glb`.
- **No missing sub-mesh.** An elevated segment glb has **two** primitives — `RoadParts_*` plus the
  running surface (`Road_*` / `TrainSubwayTrack_*`) — where a ground segment has one.
- **Nothing was conformed to terrain.** Fraction of vertices within 2 cm of the bilinear heightmap
  sample at their own XZ: normal **8.13%**, tunnel 4.86%, **elevated 0.07%** (per-segment median
  0.000%). On `seg_262479` (Double Train Track, elevated, 16.9 m): `y − terrain` min +13.80,
  median +17.39 — **zero** of 3,260 vertices near terrain.
- **The deck is geometrically complete.** Rasterising up-facing triangles into the segment's PCA
  frame at 0.2 m: 55 of 56 across-columns covered over **100%** of the length (the one empty
  column is the outer vertical face, which has no up-facing triangle by construction); top-surface
  height varies 0.30 m across the whole 11 m deck. Up-facing area ÷ (L × W) is **1.013** for
  elevated Double Train Track (min 0.986), and elevated matches or beats its `normal` counterpart
  for every asset tested.
- **Spans join.** 12 consecutive elevated train segments rasterised into one world grid at 0.5 m:
  **8,708 m² of unbroken corridor, no gap at any node.** (`networks.jsonl` has 8,259 records and
  *all* are `kind:"segment"` — no node records — because the node flares are merged into the
  segment assemblies at `NetworkRipSystem.cs:1687-1716`.)
- **Winding is clean.** Materials are `"doubleSided": false` so Blender culls backfaces, but over
  250 elevated segments / 1,023,556 triangles, those whose shipped NORMAL points up while winding
  points down total **99 tris = 0.7 m² = 0.000%** of 210,641 m² of up-facing area.
- **No z-fighting between the two primitives** — they have **zero XZ overlap** on every segment
  checked. Same materials on elevated and normal, so no form-specific alpha path exists.

### 4a. Therefore: diagnose in the harness, not the producer

The gaps are downstream of `networks/*.glb`. Named suspects, in order:

1. **The glb fast path vs stock path split** (`:4543-4620`). It sets custom split normals but
   **silently drops the TANGENT that wave 3 added** — `_parse_glb_arrays` returns no tangent key
   (`:4530-4538`) while `:4402` admits `TANGENT` as a legal attribute. Which files take the fast
   path depends on material-discovery order, so the affected *set* is nondeterministic across
   `--center`/`--radius`/`--limit`.
2. **The quiet degenerate-polygon drop** (`:4309-4314`) — counted, deliberately worded to stay
   invisible to `ab_compare.py --counts`. Read the real numbers from a live import first.
3. **Backface culling** from `doubleSided: false`, combined with `SmoothTopNormal`
   (`GameMesh.cs:514, 685-689`) replacing the authored normal of every top-bound vertex with
   `(0,1,0)`.

Start by putting the 3D cursor on one gap in `splat.blend` and running `tools/probe_point.py` —
it names the coordinate at both ends. Join back to `networks.jsonl` for the segment id, then
check whether that segment took the fast path and whether its polygons were dropped.

### 4b. Producer — gate the conform on elevated anyway (latent bug)

Independent of the above, a real hazard: `NetworkRipSystem.GameMesh.cs:840` is still an
**unconditional** `world.y = terrain` for `RaiseTopToTerrain` class-2 vertices, and the whole
conform block is gated on **piece flags only** (`:512-513`, `:544-552`).
`CompositionFlags.General.Elevated` is never read in the mesh path — the only `m_Flags` reads in
the file are `General.Node` (`:483`) and `General.Roundabout` (`:484`); `Elevated` is consulted
solely at `NetworkRipSystem.cs:2155` to write the record's `form` attribute, which is a label
disconnected from geometry. `3b9494d`'s downward clamp gives an elevated deck no protection at
all: for a deck 17 m above grade `terrain < world.y` is always true, so clamp and assignment
agree. Any elevated composition whose pieces carry either flag *would* be dragged to the ground.
It is dormant in this city (0.07% above) but should be gated — the producer's own roadbed walk
already skips `Waterway | Elevated | Tunnel` (`TerrainRipSystem.cs:2043-2053`) and the mesh path
should be held to the same rule.

---

## Sequencing

1. **Harness, visible on the existing bundle, independent of each other:** 1a, 2a, 2b, 3a, 3b,
   3d. Do these first.
2. **3c**, then read `draped`/`drape_missed`/`kerb_rejections` from a real import.
3. **4a** — probe one gap, then fix in the harness.
4. **Gated on measurement:** 1b, 2c, 3e.
5. **Producer batch:** 2d, 4b → **one re-rip** → validate.

## Verification

**Offline, no Blender** — following the established pattern of extracting harness functions under
system Python (`tools/test_decal_rows.py`, `tools/test_png_decode.py` do this because the module
imports `bpy` at scope):

- New test pinning the cut rule: over the shipped `clipvolumes.jsonl` + `heightmap.png`, assert
  deleted-face area tracks true clipped-cell area within a stated tolerance and does **not**
  exceed it by the ~1.29× today's rule does. Fixture: the window at centre 2994, 1853, R 175;
  baseline: the table in item 1.
- New test pinning the drape pick: over real segment meshes, assert no marking sample lands more
  than `eps` above the road surface directly beneath it. Baseline: the 4.4% / 7.1% / +0.317 m
  figures in item 3.
- Re-simulate the splat node graph offline (the method SPLAT-1 established) and assert the four
  unpacked extras reproduce `user_splatmap.png`'s masks, and that a painted region's edge is a
  gradient rather than a 3.5 m step.

**In Blender** — `tools/ab_render.py` + `tools/ab_compare.py`:

- Baseline log + top-down ortho render of a fixed window **before** the changes
  (`--terrain --networks --lane-markings --center 2994 1853 --radius 300`), then the same after.
  `ab_compare.py --counts` gates the log numbers; the pixel diff shows the terrain that came back
  and the ground texture that stopped repeating.
- Numbers to read off the "after" log: `face(s) cut` and the new deleted/cut ratio, `snapped`,
  `draped` / `drape_missed` / `kerb_rejections`, and the fast/stock split with its dropped-polygon
  counts.
- Compare against the game at the **same coordinate** on both sides via `tools/probe_point.py` —
  the rule the SPLAT audit wrote down after tuning a rig to the wrong patch of ground.

**Producer** — `dotnet build -c Release` (0/0), then the user's single re-rip, then
`python tools/validate_bundle.py <bundle>` must stay **VALID, 0 errors**. Until that rip lands
the wording stays "built, awaiting in-game re-rip".
