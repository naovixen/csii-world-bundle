# Bundle changelog

What changed in the **format**, when, and — the part a consumer actually needs — **how to tell
which side of the change a bundle on your disk is on**.

`schema` is a single integer with no minor version, so it cannot answer that question on its
own. Every additive change is detected by the *presence of a key*, which is why §1's rule is
that a consumer reads every path from `layers` and treats an absent key as an absent feature.
Follow that rule and most of this file is history rather than work.

Normative text lives in `docs/bundle-schema.md` §12; this is the reading order and the
detection recipe. Entries are newest first.

---

## `schema: 2` — current

Frozen 2026-08-09. A v1 consumer given v2 data fails at the manifest's `schema` const, which is
the intended loud failure.

**The break itself:** §8c `kind: "area"` clip records carry their polygon **inline**
(`nodes` + `triangles`) instead of referencing `areas.jsonl`. §36 had correctly removed
clip-surface areas from that layer — the game structurally never paints them — which left the
old reference dangling on 229 records in the first full rip carrying both changes. Clip records
are closed objects, so the repair took the version bump.

*Detect:* `bundle.json` → `schema == 2`. No bundle carrying the broken intermediate form was
ever valid under either version.

### Amendments within `schema: 2`

All additive; none bumps the version.

| date | change | how to detect |
|---|---|---|
| **2026-08-14** (H10) | Ten JSON Schemas added, covering every documented layer (fifteen in `docs/schema/`). **Two field descriptions corrected**: §5A `owner` is the generic ECS owner and resolves into no exported record 59.2% of the time — not a `networks.jsonl` foreign key; §7.2 `parentMesh` is a *colour selector*, not a parent, and a punctual light's `position` is in the **object frame**. `boneIndex` added to a punctual light. | Nothing about any file's contents changed, so there is nothing to detect — but if you wrote a consumer against the old wording, **fix it**: it was placing lights against a rule the game does not use. `boneIndex` is present only on the rare bone-space lights (4 of 609 on the reference city). |
| **2026-08-14** (producer) | Area normal maps are rebuilt from CS2's two-channel packings before writing, the way object and ground normals already were. Before this the area path never called the rebuild, and 20 of 35 area normal maps shipped packed — read as tangent RGB they shade **black**. | Sample `terrain/surfaces/area_*_n.png`: red pinned to 255 (AG) or blue pinned to 0 (RG) means an old bundle. `tools/test_normal_packing.py` prints the count; `csiibundle.materials.detect_normal_packing` classifies one. A consumer reading an older bundle must detect and unpack. |
| **2026-08-13** | `grading.json` gains `splitToning` — the only operator in the colour chain that shifts hue by luminance. Omitted when neutral. | key presence |
| **2026-08-12** | `layers.grading` declared in the manifest. The file had been written at the bundle root since 2026-08-10 but never indexed, which broke §1's "read every path from `layers`" rule: a layer absent from the index is a layer a conforming consumer cannot find. Write order fixed to grading-then-manifest. | `layers.grading` present. An older bundle has the file but no key; reading it by name still works. |
| **2026-08-12** | `grading.json` gains `fog` (HDR tint, mean free path, vertical band) and `diagnostics.unread`, the producer's own blind-spot report. | key presence |
| **2026-08-12** | §6.1 tree states `__teen` and `__elderly` export. Before this, 119,237 Elderly and 33,581 Teen trees — 59.5% of stated trees — drew as adults. | `meshes/*__teen.glb` present |
| **2026-08-12** | Terrain tiling for a layer with no texture resolves from the game's own fallback settings prefab. | values only |
| **2026-08-11** | `grading.json` gains `lighting` (sun and moon colour/temperature/intensity/direction, `PhysicallyBasedSky` tints, indirect multipliers, the in-game clock) and the exposure limits and LUT fields. | key presence |
| **2026-08-10** | **Splat channel map corrected.** `R` is dirt and `G` is grass — a bundle declaring `"R": "grass"` predates this and its map is **inverted**. `B` and `A` each pack **two 4-bit weights**, so all four paintable extras are live where two were assumed. | `rasters.splatmap.channels.R` |
| **2026-08-09** (audit #40, wave 3) | Network `.glb`s carry **authored** normals and tangents through the composition bend rather than face-rebuilt ones. §5B decal parts gain `decalRole` (`marking`\|`wear`), retiring the asset-id substring heuristic. §6.1 state variants `__dead`/`__stump`/`__partial1`/`__partial2`/`__full` ship. `areaprefabs.json` gains the always-present `roundness`/`rendererPriority`/`expandAmount`/`decalLayers` and the PBR block. | key presence; `NORMAL`+`TANGENT` in a network primitive |
| **2026-08-09** (wave 2) | `lights.json` gains the `kind: "punctual"` family — photometry that had been *documented but never produced*, because the producer read the wrong entity. The procedural example's `range`/`temperature` are gone; they were never real. §4.2's long-documented attrs are now actually emitted (`level`, `zone`, `zoning`, `brand`, `theme`, `address`, custom `name`), plus new `fullness`. §5B records gain `wear`. `bounds` encloses the lane layers. `ripOptions` gains `meshLimit`. | `kind: "punctual"` present |
| **2026-08-09** (wave 1) | §8c `kind: "area"` records gain per-area `bottom`/`top` — the game builds a **closed prism**, so "area clips are open-topped" was a misreading and terrain above the top is **not** clipped. §10 gains the `instance` and `lane` warning kinds (carrying `id`, not `assetId`) and the mesh-cap declaration rule. `rasters` gains optional `water` and `terrainTiling`. **The mesh-limit default became All** — the old cap of 50 was a dev-measurement default that made a fresh install's rip drop ~99% of distinct meshes undeclared. | `top` on an area clip record; `rasters.water` |

---

## `schema: 1` — superseded

The Phase-0 freeze. Its amendments are listed here because a bundle ripped during that period
may still be on someone's disk, and two of them change how existing values must be *read*
rather than adding new ones.

| date | change | how to detect |
|---|---|---|
| **2026-08-08** | **Clip band floor made game-true**; the producer-invented `+0.5` lip retired. The floor now reaches ~0.3 m *below* the lane and clips the roadbed shell itself — as in game, where the road meshes cover that hole. Honour the record as shipped. | `bottom` sign on a `kind: "edge"` record |
| **2026-08-08** | **§8c clip volumes added; `terrain/cutout.png` retired.** A top-down 3.5 m mask cannot express a volume open at one height and closed at another, and it was taken against the terrain as shipped, so it went stale the moment a consumer edited the ground. Shipping the volumes moves the intersection to the side that knows its own resolution. Costs roughly 4× the mask's bytes. | `layers.clipVolumes` present / `terrain/cutout.png` absent |
| **2026-08-08** | **Areas are projections, not planes.** The section had said the game grades terrain under an area; it does not. A triangle is a footprint; the game draws a prism and projects onto whatever ground is inside it. Drape, do not lay flat. | reading change only — applies to every bundle |
| **2026-08-08** | Manifest catch-up (Phase 7): layer keys, counts and `rasters` sub-blocks for every layer added since the freeze. `id`/`parent` documented correctly. | `bundle.json` completeness |
| **2026-08-07** | §8b surface areas added (`areas.jsonl` + `areaprefabs.json`). Without them a consumer shows grass wherever the game shows pavement. | `layers.areas` present |
| **2026-08-07** | **The water layer changed from depth to surface LEVEL**, in the heightmap's own encoding, 0 = dry. `rasters.depthScale` was retired with it — a bundle carrying that key predates the change and its raster means something else. | `rasters.depthScale` absent |
| **2026-08-06** | Terrain rasters frozen as **16-bit PNG**, not GeoTIFF. | file extension |
| **2026-08-06** | **`attrs.color` removed.** It had been documented as the chosen colour variation, but the component it was read from is written only by the game's infoview overlay and holds UI state, not paint. `attrs.colorSet` carries what the description always promised. A consumer must tolerate both keys' absence. | `attrs.colorSet` present |
| **2026-08-06** | `colorMask` semantics made explicit; **`colorMask.A` polarity corrected** to "white = snow may accumulate", against the freeze's "white = no snow". Measured over 731 masks, every vegetation mask is all-black, which under the frozen reading meant permanently snow-covered foliage. Still advisory — not recoverable from managed code. | reading change only |
| **2026-08-05** | `colorMask` added to the (closed) material object. Without it the palette in `colors.json` cannot be applied to anything. | key presence |

---

## Reading a bundle of unknown vintage

1. **`schema` first.** Reject what you do not implement.
2. **Then `layers`.** Absent key, absent feature. Never hard-code a path.
3. **Then the two inversions**, because they change how *existing* bytes must be read rather
   than adding anything: `rasters.splatmap.channels.R` (a bundle saying `grass` has its splat
   map inverted) and `rasters.depthScale` (a bundle carrying it stores water *depth*, not
   surface level).
4. **Then `warnings.jsonl`.** What the producer knows it could not deliver.
5. **Then `grading.json`'s `diagnostics`.** `stackEvaluated: false` or `volumesFound: 0` means
   every value in that file is an HDRP default rather than the game's grade — and
   `diagnostics.unread` is the producer naming its own remaining blind spots.

`tools/validate_bundle.py` does 1, 2 and 4 for you and reports 5 as a note.

---

## What "one rip" means, and why a bundle can be mixed-vintage

A bundle is the output of **one rip at one moment**, not a live view — and the producer runs
eight stages, of which a partial rip may run only some. Files whose stage did not run are left
alone rather than deleted, so an unchecked stage leaves its layer at its **previous** vintage.
That is deliberate: a partial rip is ~5 minutes against ~28 for the full one, and a stale layer
beats a missing one.

The consequence for a consumer is that two layers can disagree about the same entity. The
symptom worth watching is `validate_bundle.py`'s "an area record carries an owner that resolves
to no instance" warning — §8b's owner join is total on a single-rip bundle (27,047 of 27,156,
none dangling), so a dangling one means the area layer and the instance layer came from
different rips.
