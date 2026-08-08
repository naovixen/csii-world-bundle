# Build order after the audit — what to actually write

Derived from `docs/cs2-storage-audit.md` §9. Ordered by value per hour of work, not by section
number. Every figure below is measured against the 2026-08-07 full-city bundle (4,894,194,530 B).

---

## 1. COMP-1 — compress the four uncompressed layers at rest

**2,344.5 MiB, 50.23% of the bundle.** Biggest win in the audit by 1.5×, and the cheapest
consumer change in it. Do this first.

`meshes/*.glb` (1,581.5 MiB), `networks/*.glb` (300.4 MiB), `instances.jsonl` (272.1 MiB),
`lanes.jsonl` (190.5 MiB) all ship with no compression at all. `textures/` and `terrain/` are
already PNG-compressed — leave them alone.

**Producer:**
- `IO/BufferManager.cs` — this is where the write path funnels; add an lzma wrapper there rather
  than in each writer. `ICSharpCode.SharpZipLib` is already referenced (`Carto.csproj`) but is
  zip/deflate; for lzma either use `System.IO.Compression` + a `.xz`-style container or accept
  zlib-9 instead (1,879.1 MiB, 40.26% — 465 MiB worse but zero new dependency). **Take zlib-9 if
  lzma costs a dependency.** The gap is not worth a new lib in a game mod.
- Writers to touch: `IO/GltfWriter.cs` (both `meshes/` and `networks/` go through it),
  `IO/InstanceWriter.cs`, `IO/LaneWriter.cs`.
- Extension convention: `.glb.zst`/`.jsonl.zst` (or `.gz`) — do **not** keep the bare name with
  hidden compression, it breaks every existing consumer silently.

**Contract:** `docs/bundle-schema.md` §12.1 amendment — a `compression` field naming the codec per
layer, absent meaning uncompressed. Schema-1 compatible, no version bump.

**Consumer:** `tools/blender_import_bundle.py` and `tools/validate_bundle.py` — one decompress
call each at the read boundary. `tools/test_bundle_contract.py` gains cases for both forms.

**Gate:** `tools/ab_render.py` + `tools/ab_compare.py` before and after; the render must be
pixel-identical (`frac >= 0.999`, `peak <= 16`), since this changes no geometry at all.

---

## 2. TERRAIN-4 — publish the splatmap channel map

**0 bytes. Negative consumer cost. Do it in the same session as COMP-1; it is an afternoon.**

`terrain/rasters.json` currently tells the consumer to *calibrate* the splatmap's channels by
sampling slope and flat ground. The reference importer does that and **gets 3 of 4 channels
wrong** (`tools/blender_import_bundle.py:2328-2419` — the `dirt` probe runs on the roadbed class;
rock is named by steep-versus-flat contrast so it can absorb a painted channel).

**Producer:** `Systems/TerrainRipSystem.cs` — write `R=grass, G=dirt, B=extra1, A=extra2,
rock=residual` into `rasters.json`'s splatmap block. Established from `m_UserSplatmap` being
`R8G8_UNorm` plus `ApplyToSplatmap`'s writes (`Game.Rendering.TerrainMaterialSystem.cs:758`,
`:841-852`). Caveat recorded in §7.3.2: **G = dirt is by elimination**, falsified if a theme binds
fewer than seven grounds — check one theme before shipping.

**Consumer:** delete the ~35-line calibration block in `blender_import_bundle.py` and read the
map. This is a correctness fix, not an optimisation.

---

## 3. TERRAIN-2 — stop shipping four unusable ground textures

**8,689,913 B.** `m_UserSplatmap` is `R8G8_UNorm` — two channels. Extra3 and Extra4 can carry no
weight, so `surfaces/extra3.png`, `extra3_normal.png`, `extra4.png`, `extra4_normal.png` are
unreferenceable by any consumer.

**Producer:** `Systems/TerrainRipSystem.cs` — gate the capture on `m_UserSplatmap`'s
`GraphicsFormat` rather than deleting by name, so a future format change re-enables them
automatically.

---

## 4. TEX-3 + TEX-4 — one PNG representation per texture

**377.1 MiB, measured jointly** (not 440.2 — the two overlap on the alpha channel; do not sum
them). Two rules, applied in one pass per file:

- drop alpha when it is constant **at 255** (constant-at-0 is a different image — the bug that
  ate 5% of a sample when this was first measured);
- normal maps: store R,G only and reconstruct B as `sqrt(1 - x² - y²)`. Verified bit-exact on
  all 840 candidates.

**Producer:** `IO/Png.cs` (colour-type selection), `IO/TextureWriter.cs`, `Systems/MaterialRipSystem.cs`.
**Contract:** the 2-channel normal form needs a schema amendment — a `normal2` convention or a
colour-type note in §7.1. The alpha-drop needs none.

Note: adaptive PNG filtering was measured at only **8.0%** and is not worth doing alone. On
terrain it is actively harmful — a blanket policy makes 27 of 33 ground files *larger*.

---

## 5. TERRAIN-5 — ship the game's own clip volumes

**Costs bytes: −86,736 B on this terrain, −5,153,616 B at the terrain-independent upper bound.**
Promoted into the build list by decision on 2026-08-08; it is placed here because the terrain
code is already open from items 2 and 3, not because it ranks by value per hour — it cannot, since
it saves nothing. **Its case is fidelity, and the ranking above is a saving ranking.**

`terrain/cutout.png` is a *precomputed intersection* of the clip volumes with one heightmap, at
3.5 m, top-down. That representation cannot express a volume that is open at one height and
closed at another, so a portal driven into a hillside gets a square height-thresholded hole; and
it is stale the moment a consumer edits terrain. The mask was 19,659 B and every parametric
alternative is larger — but with no consumer of it in the field, keeping two representations of
one fact was pure maintenance cost, so it is **retired** rather than kept as a cheap path.

Measured (§7.3.1): the 266 `Clip_Surface` polygons already in `areas.jsonl` account for **10 of
the mask's 7,900 set cells — 0.13%**. The other 99.87% is the section-ribbon walk, and **nothing
in the bundle carries those curves**: `networks.jsonl` has no curve field, `lanes.jsonl` has
tessellated lane polylines rather than composition boundaries, and `networks/` holds baked
surface meshes.

**Producer:** `Systems/TerrainRipSystem.cs`. Every clipping section already funnels through one
function — `StampSegment` — with the trimmed left/right béziers and the band in hand, so the
layer is a collector on that call, not a second walk. `StampClipAreas` contributes the
`ClipArea` records. Band per `TryGetClipBand`, which is already the game's `AddSegment`.

**Contract:** new `clipvolumes.jsonl` + a §12.1 amendment. `cutout.png` **is** retired — with no
consumer of it in the field, two representations of one fact was pure maintenance cost.

**Consumer:** `tools/blender_import_bundle.py` rasterises the volumes itself — evaluate a cubic
bézier per station, band-test the terrain under each quad, delete the faces. Same scanline as the
producer's, ~110 lines. Worth pairing with the `stride = 4` fix below: at stride 4 the importer
still decides whole 14 m faces from four corner samples, so it cannot yet express a cut finer
than the retired mask gave it — the representation is no longer the limit, the meshing is.

**Note:** this fixes the square *tunnel cutout* only. The square shoreline and the terrain
clipping through roads are consumer meshing (`build_terrain`'s `stride = 4`), not representation
— see the known-bad section below.

---

## 6. LANE-2 — ship the Bézier, not the sampled polyline

**~190 MiB, and strictly more information than what we ship now.** `lanes.jsonl` polylines are
resamplings of the game's `Bezier4x3`; four control points reconstruct them to sub-millimetre
(p100 0.940 mm on overdetermined fits). Storing the curve is fewer bytes *and* recovers the true
geometry.

**Producer:** `Systems/LaneRipSystem.cs` (`Tessellate` is where the sampling happens — stop
sampling), `IO/LaneWriter.cs`, `Domain/LaneRecord.cs`.
**Contract:** §5A gains a `curve` field; `docs/bundle-schema.md` §5B already documents a
curve-consuming layer, so the consumer pattern exists.
**Order note:** do this *after* COMP-1 and recompute — compressed JSON already takes `lanes.jsonl`
to ~40 MiB, so LANE-2's marginal win is ~20 MiB, not 190.

---

## 7. Do not build yet

- **INST-2 (columnar `instances.jsonl`)** — loses to plain compression on bytes by 2.97×. Its only
  remaining case is import time (14.2 s → 0.2 s of parse). Build it when import time is the
  complaint, not before.
- **NET-1 (network composition layer)** — 401.5 MiB standalone but **101.1 MiB** net of COMP-1,
  against a 664-line consumer port. It does not earn that port on bytes. Revisit only if the
  producer stops baking (65.8 s of rip) or if 7,248 file opens become the bottleneck.

*(TERRAIN-5 was here until 2026-08-08. It still costs bytes; it was promoted to §5 on a fidelity
argument, by decision rather than by ranking.)*

---

## Fixed in the harness (2026-08-08): the stride-4 meshing limit

`tools/blender_import_bundle.py` `build_terrain` picks `stride = 4` on a full map and used to
decide whole 14 m faces from a 4:1 subsample. Two independent artefacts came out of that, and
both are now fixed without touching the producer:

- **Square tunnel cutouts.** A face was cut if any of its four corner samples was — measured,
  1,184 faces = 232,064 m², **2.40x** the 96,775 m² the mask actually marks. Now a coarse face
  is cut only where its whole footprint agrees, and the blocks a boundary crosses are rebuilt at
  native 3.5 m (`_refine_blocks`). Result: 129,005 m², **1.33x** — and 1.33x is exactly what a
  fully native mesh gives, since the native any-of-four rule keeps its own deliberate half-cell
  rim. **The stride penalty is gone entirely.**
- **Terrain clipping through roads.** A height problem, not a masking one: whichever of the 16
  texels under a face landed on the sample point set the height, and a crest rose through the
  road mesh. Vertices touching a roadbed block are now pulled to the block minimum — the game's
  own clamp direction, one reduction, and it can never raise ground.

Cost, measured on the 4096² full city: **3.8 s -> 4.1 s**, +8,352 ground faces (+0.8%). The
earlier attempt written up in memory `full-map-terrain-stride` cost 13 s / 2.9 GB and rendered
refined blocks as dark rectangles with holes; that failure was the per-loop and per-face arrays
being built from the coarse face list and the refined faces appended afterwards, leaving Blender
assigning UVs and materials to faces it had never been told about. Refinement now happens before
anything per-loop or per-face is derived, and the check that proves it is `len(loops) ==
sum(len(p.vertices))` with 100% of refined normals up.

Region imports are **bit-identical** (same SHA-256 over vertices, loop indices and UVs): every
new path is gated on `stride > 1`, and at stride 1 the footprint reduction collapses to the four
corner cells the harness always used.

**Still open: the square shoreline.** `face_wet` is still any-corner at 14 m on the water
surface, so the water edge stays quantised. The same `_refine_blocks` helper applies — the water
mesh has one material and no UV layers, so it is the easier of the two — but the wet boundary is
the whole coastline, so it is the expensive one to refine and was left for its own measurement.

---

## One open defect in the audit itself

§7.4's TERRAIN-1 adaptive-filter result (4.33 MiB, 11 winners) does not reproduce: a re-run gave
3.75 MiB and 12 winners, because the encoder settings behind it were never recorded and the script
was an inline heredoc. §9 was made robust to this (its terrain totals are stated as intervals),
and TERRAIN-1 is 0.09% of the bundle, so nothing downstream depends on it. Either re-measure it
with the settings written down, or strike the row.
