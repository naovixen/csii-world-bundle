# Phase-0 baselines

Captured 2026-08-21, before any refactor code moved. Refactor Phase 10 is the only phase allowed
to change a performance number, and these are the numbers it must compare against — same
instrument, before and after (`docs/plans/refactor.md`, principle 10).

| File | What it is |
| --- | --- |
| `bundle-2026-08-20.json` | sha256 + byte count for all 16,991 files of the ripped bundle, plus per-layer record counts. **Superseded as the anchor** — the save it describes has since been played on; see "2026-08-27" below. |
| `bundle-2026-08-27.json` | the same digest for the first post-refactor rip, and the current anchor. `python tools/bundle_manifest.py --compare <this> <after>` lists every difference. |
| `cs2_city_fine-report-2026-08-20.json` | the built level's own report — **its `settings` block is the input to every consumer-phase rebuild** (refactor Rule 6), and its `validation` block is the 0-problem baseline. |
| `cs2_city_fine-budget-2026-08-20.txt` | `cs2beamng budget` — resident MiB and triangles actually drawn. |
| `bake-example-cprofile-2026-08-21.txt` | a cProfile of the example-bundle bake. **Not a performance baseline** — see below. |
| `cs2_city_fine-report-after-phase1.json` | the same level rebuilt after refactor Phase 1 (`cook: true`, 16 m 03 s). Every validation check matches the baseline; `cs2beamng budget` came out **byte-identical**, which is why there is no "after" budget file. |
| `cs2_city_fine-report-after-phase2.json` | the same level rebuilt after refactor Phase 2 (`cook: true`, `--force`, 30 m 44 s) — against the NEW bundle, so read the 2026-08-27 note below before diffing it. |
| `cs2_city_fine-budget-after-phase2.txt` | `cs2beamng budget` for that rebuild. |
| `profile.md` | the numbers themselves, and what instrument produced each. |

## What the digest deliberately excludes

`derived/` is not hashed. It is the offline bake's output, so it is a function of the bundle
*and* of consumer code — hashing it into a producer baseline would report a consumer change as a
producer one. The vector exports (`GeoJSON/`, `GeoTIFF/`, `Shapefile/`, `Styles/`) are excluded
for the same reason in reverse: they are upstream Carto's output, which the refactor does not
touch.

## The bundle this describes

Ripped **2026-08-20 09:08:59–09:37:38** (28 min 39 s). No `Systems/ IO/ Domain/` commit landed
between that rip and this capture, so it is a valid pre-refactor golden. Every producer phase
after Phase 11 diffs a fresh rip against it; a difference that no phase declared is a defect.

## Why the cProfile is not a baseline

`docs/example-bundle` bakes in **0.375 s**, and half of that is `importlib`. Nothing in Phase
10's producer or consumer work-list is visible at that scale. Worse, the real bake runs its
products in worker **processes**, and `cProfile` only sees the parent — so profiling the real
bake this way would measure the coordinator and miss the work.

The instrument that does answer the question is the bake's own per-product seconds, which the
runner already reports and which `~/.cs2beamng/history.json` accumulates as units-per-second
across builds. `profile.md` records both.

## 2026-08-27: the re-rip, and what it cost the golden

`bundle-2026-08-27.json` is the first post-refactor rip — 2026-08-27 17:07:01–17:43:21
(36 min 20 s), nine stages, **zero failures in any of them**, `validate_bundle.py` **VALID with
one warning** (the same advisory-`parent` warning the golden carried). It covers the four
producer commits `e212516`, `a4acd61`, `32855e1`, `303715e`, which were "built, awaiting in-game
re-rip" until now.

The mod that produced it is provably HEAD: `Mods/Carto/Carto.dll` is stamped
**2026-08-21 14:14:48**, ninety seconds before `303715e` was committed (the build precedes the
commit), and no file under `Systems/ IO/ Domain/` has an mtime after that stamp.

**The save moved between the two rips, and that costs the golden its job.** The city was played:

| Layer | 2026-08-20 | 2026-08-27 |
| --- | --- | --- |
| instances | 1,276,060 | 1,283,009 |
| lanes | 676,863 | 680,316 |
| areas | 26,855 | 27,145 |
| meshes | 5,781 | 5,792 |
| materials | 4,774 | 4,784 |
| textures | 3,715 | 3,729 |

So the sentence above — "every producer phase after Phase 11 diffs a fresh rip against it; a
difference that no phase declared is a defect" — **no longer holds for the 08-20 golden**, and
it never held as strongly as it was written: it is only true across rips of an *unchanged save*.
A digest baseline compares producer code only when the input is held still. That is now written
into the plan as a precondition of Phase 11 rather than an assumption of Phase 0.

`bundle-2026-08-27.json` replaces it as the anchor, and the four producer commits are the
last that will ever be measured against a moved save.

### What the diff did and did not explain

Every difference is accounted for by the save except three files:

* **11 GONE / 36 NEW.** Every one of the 10 vanished meshes is a brand-suffixed advertisement
  (`BillboardSmall02_-_Limzabar`, `SignFrontwaySmall01_-_Carpoco`, …) — the city rotates its ad
  contracts. The new ones are the same rotation plus genuinely new buildings
  (`EU_ResidentialHigh01_L3_3x4`, `IndustrialManufacturing05_L5_6x6`, …).
* **57 of 7,289 network segment meshes changed at identical byte size** — all ordinary roads
  (Alley ×14, elevated highway ×10, Large Road Oneway ×10, …), not the twelve pieces §42.6
  reported as non-resident this session. `terrain/heightmap.png` also changed, and these are
  exactly the pieces that conform to it: `LowerBottomToTerrain` vertices move when the ground
  under them moves, without changing the vertex count. Terraforming explains this.
* **Three prefab meshes changed that the save should not touch — OPEN.**
  `IndustrialManufacturingDecoration03_1x3_Oil`, `ShippingContainerPileSmallLow01` and
  `SportParkStandSmall02` each changed content at *identical* size, and each one's `__base`
  skirt **shrank** (12,932→11,972; 45,960→43,368; 23,424→21,792 bytes).

The skirt is not the suspicious part — `BaseSkirtBuilder` collects silhouette edges inside a
**±0.01 m band**, so a float difference of a hair in the source mesh moves edges across that
threshold and the skirt changes size. The skirt shrinking is a *symptom*: the prefab mesh's
vertices differ, at the same vertex count.

It is **not** one of the four producer commits. Only `a4acd61` touches `MeshRipSystem` at all,
and it moves `SanitizeAssetId`/`BuildAssetIds`/`Discriminator`/`Digest` verbatim — the only lines
that differ are call-site qualification, the two dead queries it deletes, and the `EntityManager`
parameter that made `DescribeComponents` shareable. No geometry code (`GeometryAssetReader`,
`BaseSkirtBuilder`, the sub-mesh state walk) is touched by any of the four. And an assetId change
would rename a file, not refill one byte-for-byte the same size.

**It cannot be settled from here**: the 08-20 bundle held only these hashes, not the bytes, and
the rip overwrote the files. The cheap experiment is the *next* rip — if these three change again
with no producer change and no play between them, the mesh rip is non-deterministic (see the
"rip residency is intermittent" note); if they hold, the save explained them after all.

## The Phase-2 rebuild, 2026-08-27

`cs2_city_fine-report-after-phase2.json` and `cs2_city_fine-budget-after-phase2.txt`: the level
rebuilt through the Phase-2 code, in place, `cook: true`, settings read from its own report,
BeamNG closed, `--force`. **30 m 44 s, exit 0, one problem — the same budget warning the golden
carried.**

**It is measured against the NEW bundle, so most of the diff is the city, not the code.** The
rebuild had to use the 08-27 rip (the level's own settings point at the bundle directory), and
that city has been played since the golden was built. `instances.scanned` goes 1,276,060 →
1,283,009 — the bundle's own instance count, exactly — and everything downstream follows it:
forest items +5,470, TSStatics +102, shape files +4, resident 4,256 → 4,273 MiB. The spawn point
moved because the spawn is the drivable lane nearest the region centre and the city has new roads.

**What the diff shows that is the CODE:**

* `scene.result.sunColorTemperatureIgnored: false` — the new declared approximation, reporting
  that the sun's colour-temperature flag is off on this save, so the gap did not bite.
* `validate.checks.groundCheck.sampler: "bilinear"` — the new counted field naming which height
  sampler produced the ground numbers.
* Nothing else. No new problem, no new warning class, no check that changed for a reason the
  bundle does not explain.

**One change went unexercised, and it is the only behavioural one.** This level has
`lights: false` in its settings — as the golden did — so the lights stage is **skipped**, and the
`frames.quat_matrix` change in `stages/lights._world` never ran. It was measured directly instead,
against the rip's own `lights.json`: 654 rotations, 814 of 5,886 written floats moving by at most
2e-06. A build cannot confirm it; a build with lights on would.

**The in-game look: VERIFIED 2026-08-27.** Rule 6's last step, the one no automated check can
stand in for, is done — the level was loaded and looked at, and it is right. That closes the
hand-over debt for Phase 1 and Phase 2 together; both were committed ahead of it, which the
commit messages said at the time.

**One number needed chasing, and it was cache accounting.** `bake.materials.tintedTextures` reads
57 in the golden and **2,575** here — a 45x jump in a counter fed by `_png_size`, which Phase 2
rewrote onto `png.read_header`. If that rewrite were broken, every texture would be rewritten
every run and would look exactly like this. It is not: `_png_size` returns the true `(h, w)` for
every file tested and `None` for a missing one, and re-running the identical bake with the cache
warm writes **0** textures. The 2,575 is a cold cache — the new bundle re-baked everything — and
`textures written` counts misses, not outputs. There are exactly 2,575 such files on disk.

(The first attempt at that check ran `--only materials` without the app's `paint_kinds`, so it
baked a *different, smaller* job — 96 source albedos against 493 — and proved nothing. Recorded
because the invalid test looked like a valid one.)

## The Phase-3 rebuild, 2026-08-27

`cs2_city_fine-report-after-phase3.json`: rebuilt in place, `cook: true`, `--force`, settings from
its own report, BeamNG closed. **23 m 50 s, exit 0, one problem — the same budget warning.** The
bake reported `fresh, skipped` for all eight products in 5 s, which is itself the check that the
option-set restore was exact.

Every count matches the Phase-2 build: 4,275 shapes, 7,289 segments in 1,024 tiles, 908,841 forest
items over 3,104 types, 15,697 TSStatics, 15,393 navgraph roads, 36,502 terrain holes, 4,784
materials, resident 4,272.7 MiB. **Nothing code-attributable moved.** The thirteen non-bake report
differences are all state rather than behaviour: leftover temp `.cdae` files from the previous run
(10 → 0), texture cache reuse (0 → 5,007, the previous run being the first after the cache was
restored), and four budget-threshold fields that changed because BeamNG **logged a real resolution
this time** — 3440x1440 instead of the REFERENCE 1080p fallback — since the level had been opened
for the in-game look.
