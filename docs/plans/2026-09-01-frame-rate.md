# Frame rate on the built city — what the symptoms say, and what the first lever measured

Naomi, 2026-09-01, `cs2_city_fine` (the v40-era build, 2,953 MiB of level files) at BeamNG's
Normal settings on the 8 GB card at 3440×1440:

* **30–40 fps outside the city**, dropping to **10–20 fps near it or looking at it**;
* the frame rate **oscillates 10–60 fps while standing still looking at one spot**.

## The diagnosis the record already supports

The third symptom is the one that names the cause. A frame rate that swings by 50 fps with a
**static camera** cannot be geometry: nothing about the draw call list changes between those
frames. What does change is memory.

* BeamNG streams texture mips on D3D12 and the decisions are **budget-driven, not view-driven**:
  above ~50 % of the driver's budget it evicts unused high mips, and near ~90 % it stops loading
  them (§8.2, and BeamNG's own texture-streaming page). In that regime the streamer cycles mips
  under a still camera — which is the oscillation.
* This level lives there. Measured with `Engine.Render.getMemoryInfo`: **5.98 GB of a 6.78 GB
  budget (88 %)** at 3,069 MiB of files (§8.7), and **5.85 of 6.09 GB (96 %)** at v33 (§8.14).
  At 5,224 MiB the card logged **131,073 `CreateResource` E_OUTOFMEMORY** failures (audit §65).
* The isolation sweep already proved it the other way round: at 96 % of budget, hiding buildings,
  vegetation, roads or markings each moved the frame rate by **under ±1 fps**, while the streamer
  settling mid-sweep moved it **13 → 27 and kept it there**. §8.14's conclusion — *"the FPS is
  MEMORY"* — is the finding this session starts from, not one it needs to rediscover.

Of the 2,953 MiB of level files, **2,396 MiB is mesh and 557 MiB is texture**, and only the
texture half can stream. The fixed mesh residency is what leaves the streamer no room.

## Lever 1 — thin the level a shape SHIPS. BUILT.

The attack on 2,396 MiB of resident mesh: take the MeshLab collapse §8.15 built (every collapse
lands on an EXISTING vertex, so the surface lies on the original by construction) and run it on
the level each shape actually ships, keeping a reduction only where the **measured p95 surface
displacement** stays inside a tolerance. Unlike every other residency lever, this one is not
"less city".

**A first pass got this wrong and it is worth recording why.** It asked for 50 % and 25 %,
measured 0.22–1.19 m of error, and concluded the mesh had no slack — that the shipped level is
already the artist's reduction and a second one has nothing to spend. Naomi pushed back, and the
curve says otherwise:

| shape | in | 90 % | 80 % | 70 % | 50 % |
|---|---:|---:|---:|---:|---:|
| `InternationalAirport01` | 57,163 | 51,609 / **0.000** | 46,055 / **0.000** | 40,503 / 0.076 | 29,449 / 1.191 |
| `MedicalUniversity01` | 126,896 | 115,590 / **0.000** | 104,286 / **0.000** | 92,982 / 0.230 | 80,460 / 0.440 |
| `CargoHarbor01` | 20,506 | 18,464 / **0.000** | 16,424 / 0.005 | 14,382 / 0.051 | 10,972 / 0.220 |
| `ParkingHall01` | 54,270 | 48,998 / **0.000** | 43,726 / 0.163 | 38,454 / 0.219 | 27,910 / 0.341 |

A tenth of the triangles comes off for **nothing measurable**. Over the heaviest 60 shapes the
rule reached **58 of them for 18.4 %** of their triangles at a 3 cm bound — **~219 MiB**
extrapolated over the bundle's 12.5 M shipped triangles. On the shapes tried end to end it runs
19–30 % out at a p95 of 0.000–0.005 m, `Five_Luxe_JBR_` included (243,371 → 172,042 at 0.000 m).

The lesson is the method, not the number: **never conclude "no slack" from aggressive ratios
alone.** Two things the curve disproves, both of which the first pass assumed:

* **the error is not monotone in the reduction** — `MedicalUniversity01` is 5.317 m at 60 % and
  0.440 m at 50 % — so every candidate is judged on its own and a walk may not stop at the first
  failure; the targets are tried deepest-first and the first that PASSES wins;
* **MeshLab stalls** — `OfficeHighSignature02` sits at 79,259 for three targets running — so a
  candidate that came back no smaller is not a reduction and is skipped.

Built as bake option `lod0_refine` (`_LOD<n>R.glb` for levels 0 and 1, targets 0.9/0.8/0.7,
bound `lod0_refine_cm`, default 3 cm — three times tighter than `lod0_max_error`, because this is
the level the viewer stands in front of) and consumed by `shapelods.refined_level` on both
ladders. Vegetation is exempt for the same reason it is exempt from the displacement switch rule.

**Cost: ~11 min at 7 jobs** (1,325 shapes clear the 2,000-triangle floor), paid once — the `lods`
product is now per-asset incremental.

## Lever 1b — the cap that drops LOD 0 on a triangle count. FIXED.

`authored_lod0_max_tris 5000` drops a shape's full-detail mesh whenever it is big, and **never
asks how far the game's own LOD 1 is from it**. That is why "some meshes look awful up close"
(Naomi, 2026-09-01).

Measured over the 644 capped shapes, LOD 1's p95 distance from LOD 0 is **p50 0.005 m, p75
0.025 m, p90 0.072 m** — the cap is right for almost all of them — and then:

| shape | p95 from LOD 0 | LOD 0 → LOD 1 |
|---|---:|---|
| `OracleParkSculpture` | **1.088 m** | 5,974 → 784 |
| `Planetario_Galileo_Galilei` | **0.892 m** | 12,455 → 622 |
| `Alvear_Tower_Buenos_Aires` | **0.423 m** | 93,755 → 19,773 |
| `Prison01` | **0.316 m** | 52,755 → 8,011 |

The existing guard (`DEGENERATE_LOD1_RATIO`, 5 % of the triangles) catches Oracle Park and the
mangrove stump but misses every one of these: it only sees *badly reduced*, never *moderately
reduced and geometrically far*. New setting **`authored_lod0_max_error`** (default 0.25 m)
refuses the drop on the measured distance instead — **8 shapes, ~26 MiB** — and those shapes keep
a *refined* LOD 0, not the full one.

**What the cap cannot become is optional.** Turning it off wholesale ships every LOD 0: 12.5 M →
20.7 M triangles, **+638 MiB even after refining**. The refinement complements the cap; it does
not replace it.

## Lever 1c — how deep, and the tile that was never touched (2026-09-01, second pass)

The first build shipped the refinement at a 3 cm bound with a 0.7/0.8/0.9 ladder and a
2,000-triangle floor, and took **222 MiB** off (2,990 → 2,768 MiB, 1,119 shapes, 19.6 % out).
In game that changed nothing Naomi could see, because the card is 3.6 GB into shared memory —
222 MiB is about 6 % of the gap. Three things followed.

**The ladder stopped too shallow to answer "deeper".** With 0.5 and 0.6 added, the
tolerance/reduction curve over the heaviest 45 shapes is:

| tolerance | 3 cm | 5 cm | 8 cm | 12 cm |
|---|---:|---:|---:|---:|
| triangles out | 22.4 % | 24.7 % | **28.8 %** | 32.5 % |

**Both of those moves were WRONG and were reverted the same day — see "the ladder went too deep"
below.** 8 cm shipped, and it tore the buildings open.

**The road tiles were the second-biggest pool and were exempt by design.** 698 MiB of network
mesh, and `tilemesh.build_levels` never decimated a tile's NEAREST level — correctly, because a
road tile ships `Visible Mesh Final`, so that mesh **is** the surface the car drives on. It is now
refined under its own much tighter bound, `tile_refine_cm` = **1 cm** against a shape's 8: a flat
run of carriageway merges at essentially zero displacement while a crown, kerb or camber measures
and is refused. Measured on a crowned 800 m carriageway: **30 % of triangles out at a p95 of
0.8 cm**, crown intact — about 209 MiB at city scale.

**Paint stays exempt, deliberately.** Markings and areas (339 MiB) are marked never-decimate
because the vertex clusterer defaced them into "black and colourless blotches" (Naomi,
2026-08-31). A displacement bound cannot see that failure — the quads barely move while the
alpha and UVs are wrecked — so shipping the refinement there would be an undeclared approximation
of exactly the kind that already bit once. Declared, not taken.

## The ladder went too deep — what the screenshots showed (2026-09-01, third pass)

The 8 cm build ran at **40–60 fps, dipping to ~20 when panning into a dense area** (from 10–20:
the memory work landed). But Naomi's screenshots showed the price:

* a tower whose **window openings tore open**, sky visible through the building, facade faceted
  and rippling;
* a curtain-wall tower whose **mullion grid shredded into triangular tents**;
* an apartment block whose **balconies collapsed into spikes and wedges**.

**Two separate blind spots, and the tolerance was neither of them.**

**(1) A p95 leaves the worst 5 % unbounded.** Measured:

| shape | ratio | p95 | p99 | MAX |
|---|---:|---:|---:|---:|
| `Alvear_Tower_Buenos_Aires` | 0.7 | **0.022 m** | 0.553 m | **1.000 m** |
| `Prison01` | 0.7 | 0.508 m | 0.846 m | **3.415 m** |
| `CargoHarbor01` | 0.7 | — | — | **4.75 m** |

Alvear Tower passes an 8 cm gate *with room to spare* while a metre of its geometry has moved.
Fixed with **`lod0_refine_peak_cm`** (default 25 cm), a hard worst-case cap a candidate must pass
alongside the p95. Verified: Alvear, CargoHarbor and the Airport are refused outright, while
`Prison01` steps back from ratio 0.8 to 0.9 and keeps a safe 9 % cut. It costs about half the
reduction (29.9 % → 16.6 % on the heaviest 40) and is worth every triangle.

**(2) No displacement bound of any kind can see a torn material seam.** `decimate.lod_ladder`
runs the reducer **per material run**, so a wall run and a glass/frame run are decimated
independently, stop sharing vertices, and the seam between them opens — the vertices barely move
while the hole appears. `preserveboundary=True` was already set and cannot help, because a window
rim between two material runs is not a mesh boundary.

**The codebase already said so.** `decimatemeshlab`'s docstring: *"a perforated facade (rows of
open balconies) still webs shut at these ratios"*; `shapelods` records generated levels being
banned from buildings as *"see-through lattices (a facade's window quads unwelded)"* (§8.12). The
0.5/0.6 rungs were added to chase bytes and reached the documented limit instead.

**Reverted to the validated envelope: ratios 0.7/0.8/0.9 at p95 3 cm**, the configuration that
shipped in the first pass and drew no complaint — now with the peak cap on top, so it is strictly
safer than the build that was looked at, not merely equal to it. Floor back to 2,000 triangles.

**And the bottleneck has moved.** Naomi's own overlay in those screenshots reads **CPU 20.7 ms vs
GPU 18.7 ms** and **CPU 22.5 vs GPU 20.8** — marginally CPU-bound now that the memory pressure is
off. That is BeamNG's documented #1 slow-scene cause (unique materials and draw calls; this level
ships 5,546 materials, 9.5x vanilla) and it matches "drops when panning into a dense area". The
next real lever is material/draw-call consolidation, not more mesh reduction.

## Where it landed, and what stays open

Rebuilt at the validated envelope (ratios 0.7/0.8/0.9, p95 3 cm, peak 25 cm):

| | session start | 8 cm (tore the buildings) | shipped |
|---|---:|---:|---:|
| meshes cs2 | 1,345 | 1,028 | **1,092** |
| meshes networks | 787 | 465 | **465** |
| textures `.dds` | 401 | 174 | **174** |
| **RESIDENT TOTAL** | **2,990** | 2,213 | **2,277 MiB** |

**-713 MiB (-24 %)**, and the safe envelope cost only **+64 MiB** over the one that broke —
nearly all of the win without the torn facades. Measured on the built level: roads **39 % of
triangles out over 702 tiles at a worst case of 0.01 m**; shapes **1,469 refined, 18.2 % out**
with both bounds binding exactly at their caps. In game: **40-60 fps, dipping to ~20 panning into
a dense area**, from 10-20 at the start of the session.

**Still open, and deliberately not chased:** diagonal facets remain visible on some buildings
even inside the validated envelope. The cause is understood (per-material-run decimation opening
seams, above) and no displacement bound can detect it; closing it properly means decimating a
shape's runs together with the seam vertices pinned, which is a change to `decimate.lod_ladder`
and not a tuning number. Naomi's call, 2026-09-01: keep as is, and expose the control instead.

`lod0_refine` and `lod0_refine_cm` are therefore in the **World** section (not Advanced) and in
the pre-advanced panel: a "Thin and clean the meshes" switch and a "How much to thin" slider,
beside the Trees and Props density sliders. `lod0_refine_peak_cm` and `tile_refine_cm` stay in
Advanced - they are safety bounds, not preferences.

**The bottleneck has moved.** Naomi's overlay reads **CPU 20.7 ms vs GPU 18.7 ms**: the level is
now marginally CPU-bound, which is BeamNG's documented #1 slow-scene cause (unique materials and
draw calls - 5,546 here against vanilla's 586) and matches the drop when panning into a dense
area. Further mesh reduction buys progressively less; material consolidation is the next lever.

## The card is paging, and that is the real ceiling

Naomi, 2026-09-01: **6.8/8 GB dedicated, 3.6 GB shared**, and *"when I tabbed out and tabbed back
in it seemed to make the game worse."* That last sentence is the audit's own signature, verbatim:

> "On an 8 GB card a working set over about 6 GiB pages continuously and never recovers, which is
> exactly the 'much worse after alt-tabbing' symptom: alt-tab makes the driver release its
> allocations and the level cannot get them all back."

So the level is not slow, it is **over-committed**: 3.6 GB of it is being fetched across PCIe
every frame. At a recorded file→GPU expansion of 1.7–2.8x, 2,768 MiB of files is 5–7.7 GB on
card before BeamNG's own ~2–2.5 GB. Closing that needs on the order of a gigabyte of files, which
is why the second pass takes every lever at once:

| lever | ~saving |
|---|---:|
| refinement at 8 cm, deeper ladder, 500-triangle floor | ~385 MiB |
| road tiles at 1 cm | ~209 MiB |
| `texture_max` 256 → 128, normal 128, variant 64 | ~280 MiB |
| `foliage_density` 33 → 20, `prop_density` 70 → 50 | ~120 MiB |

That projects to roughly 2,000 MiB of files. If it still pages, the remaining honest answer is
§8.9's: a `--region` build of the district in play is "the one that fits with room at these
settings".

**Also open:** a *square* boundary in the distance that stops the 8 km view. Fog is radial and
the values written are CS2's own (density 1/6,900 m, atmosphere height 3,000 m), so a square edge
is far more likely the terrain ending — the map is 8,192 m across — than fog. Unconfirmed; a
screenshot settles it.

## Lever 2 — the shadow pass. BUILT.

`cs2_city_fine` was built with `forest_shadows: true`, and "true" meant **576,148 forest items in
the shadow pass with no far stop at all**. Vanilla ships `shadowNullDetailSize` 40 px on the 138
items that set it — a shadow, like a mesh, has a distance past which it stops.

* New setting **`forest_shadow_null_px`** (default **40**, vanilla's own number; 0 = no far stop).
  Near trees keep their shadows, the far tail leaves the pass.
* The C5 refinement (ground-form network tiles out of the shadow pass, bridges and tunnels keep
  it) is already committed and comes in with the rebuild.

Shadow casting is BeamNG's own #3 slow-scene cause and roughly doubles geometry submission for
the objects in the pass. This is the cheapest large lever left and it costs no residency.

## Lever 3 — the elevated instances. BUILT.

A prop bolted to a roof, a balcony or the underside of a deck can never be driven into, and its
shadow lands on the roof it stands on. New setting **`collision_max_clearance_m`** (default
2.5 m, 0 = off): an instance whose own bottom stands clear of the terrain by more than that
ships no collider and casts no shadow.

* **TSStatics** take it directly; buildings are exempt, and the road surface is untouched, so
  this never removes the ground under the car.
* **Forest items** have no per-item collision or shadow flag — both live on the type — so an
  elevated item is routed to a **twin type** naming the same `shapeFile`. Torque mounts a shape
  once per path, so a twin costs one `TSForestItemData` and no mesh residency. The thin-type
  merge and the rare-type floor both skip twins, or they would silently hand the costs back
  (the merge) or delete a roof's props outright (the floor).

**THE NUMBER IS OURS** — CS2 has no notion of collision for a placed object — so it is declared
in the code that makes it and counted in the report, per the non-negotiable. The approximation is
that the measurement is the TERRAIN, so a prop on a high viaduct counts as elevated too.

## What is left, and what it needs from Naomi

With Lever 1 in, mesh residency should fall by roughly 220 MiB while close-up quality on the
worst-served shapes improves. That is unlikely to be enough on its own to clear the ~90 % budget
line, so the levers that ship less remain on the table:

1. **The build's own settings.** `cs2_city_fine` was built at `lod_detail_scale 4`,
   `view_distance 8000`, `forest_shadows true`. Naomi's saved settings are already far tighter
   (`10`, `4000`, `false`) — a rebuild at those alone is a large move, before anything above.
2. **Sooner LOD switches** (Naomi's "1 → 0.75"): px scales ×1.33 across
   `lod_detail_scale`/`_prop`/`_large`. Cuts drawn triangles and per-frame LOD work, not bytes.
3. **Texture and material trims**: `paint_variants_tree` 2 → 1, `paint_factor_max_error` 4 → 12
   (more tints become free material factors than baked albedos), and C2's oversized ground base
   map. These reach the half that streams, which is the half under pressure.
4. **A `--region` build** of the district in play — §8.9's own conclusion, *"the one that fits
   with room at these settings"*.

The measurements to take on the next load, in order: `getMemoryInfo` usage as a **fraction of
budget** (the pass mark is under ~80 %, out of the stop-loading regime); 60 s stationary to see
whether the oscillation flattened; then the `cs2diag` isolation sweep, which is only meaningful
once the level is out from under the memory ceiling — and whose ranking decides whether draw
calls (5,546 materials, 9.5× vanilla) are the next refactor. The §2.4 vanish-distance measurement
is still open and still two minutes' work.
