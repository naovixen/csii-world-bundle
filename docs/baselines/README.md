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
