# Phase 10 survey — consumer (csiibundle + the cs2beamng build)

Survey of the Python consumer's hot paths, 2026-08-29, at HEAD `53e0b9d`. Baselines: bake 42 min
(lods 1,575 s = 62 %, materials 435 s, areas 155 s, lanegeometry 145 s, markings 88 s,
terrainmask 75 s, terraincut 56 s); live level build 16 m 42 s (cook 463 s, instances 147 s,
shapes 92 s, networks 70 s, derived 55 s). No file was modified by the survey.

## 1. How many times `instances.jsonl` (1.28 M records / 312 MB) is streamed per build

There is **no columnar cache anywhere** — every consumer re-streams and re-`json.loads` the
layer; nothing is kept as numpy.

Level build (app), in stage order:

| # | Site | Filter | Extracts |
| --- | --- | --- | --- |
| 1 | `pipeline/meshindex.py:123` (`build`) | none | `kind, assetId, pos, attrs.{growth,treeState,fullness,…}` → per-shape rank/cells. Cached to `~/…/meshindex/<key>.json`, but `key_for` (`meshindex.py:63-92`) stats `derived/lods`, so **every re-bake invalidates it** (also re-run at build end from `worker.py:292`) |
| 2 | `csiibundle/orphans.py:70` via `shapedemand.py:44` | none | `id`, `assetId`, `parent` |
| 3 | `beamng/placement.py:198` via `shapedemand.py:49` | `contains='"tree"'` | full record batches of 100k |
| 4 | `stages/shapedemand.py:54` | none | full admission + `mesh_candidates` |
| 5 | `stages/instances.py:117` | none | full admission + transform + colorSet |
| 6 | `stages/lights.py:136` | none | `assetId`, admission |
| 7 | `beamng/levelcheck/terrain.py:79-84` (validate) | none | filters `kind=="building"` in Python then samples 200 + every 7th (`:96-98`) |

Bake, additionally: `bake/paint.py:89` (`collect_variants`, none, → `attrs.colorSet`);
`bake/skirts.py:58` **and** `:75` (`'"building"'` ×2, the first only to build
`{id: elevationFlags}`); `bake/markings.py:212` (`'"decal"'`); `bake/lods.py:241` (region builds).

≈ 7 unfiltered + 4 prefiltered passes; at the module's own ~15 s/pass (`meshindex.py:7`) that is
90-120 s of pure JSON parse per build, plus the same again inside the bake. Passes 2, 3, 4 all
live inside `scan_demand` and could be one pass. `shapedemand._state_suffix`
(`shapedemand.py:125-132`) builds a fresh `mesh_candidates` list per record (1.28 M) where
`stages/instances._state_suffix` (`instances.py:282-300`) memoises on
`(asset, treeState, fullness)`. `levelcheck/terrain._ground_check` decodes 1.28 M records to
sample ~200.

## 2. `reader.py` / `instances.py` — how JSONL is parsed

```python
# tools/csiibundle/reader.py:252-258
with open(path, encoding="utf-8") as handle:
    for line in handle:
        if contains is not None and contains not in line: continue
        line = line.strip()
        if line: yield json.loads(line)
```

Stdlib `json.loads` per line; `orjson`/`ujson`/`simdjson` are not installed and not in
`pyproject.toml`. Text mode decodes 312 MB bytes→`str` before parsing (`json.loads` accepts
`bytes`); `line.strip()` allocates a second string per record. No columnar/numpy loader exists;
the docstring (`reader.py:12-14`) discourages materialising, conflating "don't `list()` the
dicts" with "don't build arrays". A one-time pass producing `pos (N,3) float32`, `rot (N,4)`,
`assetId` as categorical `int32` + `kind` codes would be ~60 MB and serve passes 1-3, 5-7 and
`bake/paint`. `contains=` (`reader.py:250`) is the only optimisation and is good.

## 3. `stages/instances.py` per-record work, and `write_objects`

Per admitted record (`instances.py:117-164`): `placement.keep` → `admit` (`placement.py:132-167`,
~8 dict gets each); `_state_suffix` memoised; `placement.variant_of` → **`tints.variant_index`
(`tints.py:142-159`) is a pure-Python double loop** over k centres × 9 floats with a `zip`/`sum`
genexp, run for every forest record (~1.2 M) here and again in `shapedemand._count_record:120` —
~20 M interpreted float ops, twice; `frame.record_transform` per record; `_forest_line`
(`instances.py:303-314`): `frame.basis_flat` + two `round()` comprehensions + **`json.dumps`,
1.2 M times**; `_add_static` (`instances.py:317-345`): 15 `round(float(v), n)` in
`level.tsstatic` (`level.py:248-250`), `tints.color_set`, 9 more `round`s, and a `uuid.uuid5`
(`level.py:73`, a SHA-1 per object).

**`write_objects` is called 11-13 times per build** and each call rewrites the whole tree:
`stages/terrain.py:232`, `water.py:97,115,218`, `scene.py:210`, `networks.py:73|161`,
`derived.py:201`, `instances.py:186`, `lights.py:162`, `navgraph.py:143`, `poi.py:149`,
`groundcover.py:148`, plus `worker.py:151` and `worker.py:241`. Each call (`level.py:107-165`)
`json.dumps` every object in the accumulated tree (`_write_lines_if_changed`, `level.py:261-263`),
reads every existing `items.level.json` back to byte-compare (`:265-268`), and **`os.walk(main)`
every call** (`:152`). ~7 calls happen after the tree is ~40-50k objects → ~300k redundant
`json.dumps` + 7 full re-reads + 7 walks. Caching the serialised bytes per object would make
calls 2..n nearly free.

## 4. `csiibundle/png.py` — every raster copy, and the zlib level

Decode path (`decode_png_rgba`, 4096² RGBA = 67 MB per copy): `png.py:101` whole-file read;
`:111` per-IDAT slice; `:120` `b"".join(idat)`; `:138/164` `zlib.decompress`; `:78`
`numpy.zeros((height, stride))`; `:83` `numpy.frombuffer(...).copy()` **per row**; `:94`
`out[y] = line`; `:96` `bytearray(out.tobytes())` — **two** copies; `:165`
`bytes(unfilter_scanlines(...))`; `:182` `numpy.frombuffer(pixels)` (view). ≈ 8 full-size
copies. The 16-bit gray path adds `:144` `array("H", bytes(out))` + in-place `byteswap` + `:176`
`numpy.frombuffer(bytes(samples))`.

**The bigger cost is `unfilter_scanlines` (`png.py:64-96`): a Python `for y in range(height)`
loop** — 4,096 iterations for a 4096² image. The docstring (`png.py:14-17`) says the producer only
ever writes filter 0, so the loop can become:

```python
rows = numpy.frombuffer(raw, uint8).reshape(height, stride + 1)
if not rows[:, 0].any():   # all filter 0 — the only case a real bundle hits
    return rows[:, 1:]     # a view; no copy
```

Encode: zlib level 6 at `png.py:203` (`encode_gray`), `:237` (`encode_rgba`), `:260`
(`encode_rgb`); `write_gray`/`write_rgba` (`:286-293`) never pass one, so every bake-written PNG
is level 6 — for derived rasters only this toolchain reads back, level 1 is 3-5× faster for
~10-20 % more bytes. Copies on encode: `astype(">u2")` (`:217`), `numpy.concatenate` (`:225-227`),
`body.tobytes()` (`:232`), and **`zlib.crc32(tag + payload)` at `:199` copies the whole IDAT to
CRC it** — `zlib.crc32(payload, zlib.crc32(tag))` is free; the final `+` concatenation
(`:229-234`) copies the file once more.

Pillow ≥ 9 is already a hard dependency (`pyproject.toml:15`); `numpy.asarray(Image.open(p))` on
an `I;16`/`RGBA` PNG does no colour management. `images.load_any` (`images.py:34-47`): for a
colour-type-2 (RGB) texture it calls `rgba_array` (reads + chunk-walks the file, raises), then
`gray_array` (reads + walks it again, raises), then PIL reads it a third time.

## 5. `bake/lods.py` — 1,575 s, 62 % of the bake

Pooled: `parallel.each(_process_file, args, jobs, progress, chunksize=4)` at `lods.py:282`,
`jobs` = `cpu_count()-1` (`pipeline/worker.py:41-45`). Per mesh (`_process_file`,
`lods.py:128-217`): one `parse_glb`, `_flatten` per node, `decimate.lod_ladder` for both targets,
`displacement.level_displacement` per level, `glbwrite.write_glb`. No re-decode per LOD level.

Where the time goes:

1. **`pyfqmr` is NOT installed** in this environment. `lod_backend` defaults to `"auto"`
   (`lods.py:225`), `_quadric_available()` is False (`decimate.py:48-54`), so every mesh runs the
   numpy vertex-clustering binary search: `_cluster_to_ratio` (`decimate.py:219-238`) calls
   `vertex_cluster` 12 times per material run per target → 24 clusterings per run, each a
   `numpy.unique(composite, axis=0, return_index=True, return_inverse=True)` (`decimate.py:206`)
   — the slow void-view lexsort. ~5,792 meshes × ~2 runs × 24 ≈ 280k structured lexsorts.
2. **`_albedo_sampler_factory` is constructed inside `_process_file`** (`lods.py:139`), so its
   `cache` (`lods.py:70`) dies with every mesh; `decimate.vertex_albedo` (`decimate.py:341-382`)
   then decodes the material's albedo through `png.rgba_array` (`lods.py:78`) — ~1,400 distinct
   albedos decoded once per mesh that uses them, through the §4 decoder.
3. **The whole materials catalogue is pickled into every job** (`lods.py:248`, `catalogue` =
   `reader.load_materials(bundle)`, `lods.py:228`, 4,641 entries) — ~1,448 chunk pickles.
   `bake/areas.py:49` shows the right pattern (`Pool(initializer=_worker_init, initargs=...)`).
4. `displacement.level_displacement` per node per level (`lods.py:159`), 600 samples through a
   grid search (`displacement.py:204-238`) with a Python loop over occupied cells
   (`displacement.py:190-201`). Real but secondary.

`decimate._nearest_index` (`decimate.py:268-274`) is brute-force (quadric path only, currently
dead). `_compact_faces` uses `numpy.unique(sorted_keys, axis=0)` (`decimate.py:162`) once per
cluster attempt, i.e. also ×24.

## 6. `bake/materials.py` — 435 s, 17 %

The k-means now lives in `bake/paint.py`: `paint.kmeans` (`paint.py:23-43`), the
boolean-mask-per-centre at `:35-39` — k ≤ 4, n = hundreds; **not the hot spot**.
**`paint.collect_variants` (`paint.py:78-126`) is**: a full serial 1.28 M-record stream, and per
record with a colorSet (~1.14 M) a nested genexp
`tuple(int(round(float(c) * QUANT_STEPS)) for col in cs[:3] for c in col[:3])` (`:97`) — ~10 M
interpreted `float()/round()/int()` calls plus a `defaultdict(Counter)` increment. The docstring
at `materials.py:78-79` declares this pass serial on I/O grounds; the cost is CPU-bound key
construction, which parallelises by byte-range.

Decoded more than once: `materialmaps.split_mask_job` decodes each mask (`materialmaps.py:89`),
then `tint_job` decodes the same masks again (`:190`); `by_base` (`paint.py:160`) keys on the
base albedo and stores its own `mask`, so a mask shared by N albedos is decoded N times.
`_rebuild_normals` decodes every normal (`:105`) and `stages/materials` decodes the same or
rebuilt normal again for export. `_uniform_mask_factors` (`materialmaps.py:124-144`) is a good
short-circuit (skips the albedo decode for 31 % of pairs). `_split_masks`/`_rebuild_normals`/
`tint_albedos` are pooled (`materials.py:114, 137`, `paint.py:232`).

## 7. The remaining bake products

* **`bake/areas.py` (155 s)** — the best-engineered product: chunked pool with `shared_memory`
  for the heightfield (`areas.py:49-78, 135-147`), numpy across the boundary (`:92-112`). But
  **`load_road_surface(bundle, ...)` at `areas.py:248` re-parses all 8,259 network `.glb`s, and
  `bake/markings.py:108` does exactly the same** — the soup is built twice per bake.
* **`bake/lanegeometry.py` (145 s)** — fully serial per-record loop (`:88-173`) over 680k
  lanes; only the tile flush is pooled (`:257`). `sources` cached per assetId. The sort at
  `:212-222` indexes `baked_blocks[i]` twice per key. Embarrassingly parallel, not parallelised.
* **`bake/markings.py` (88 s)** — two serial streams (`:136`, `:212`). `_subdivide_quad`
  (`:70-75`) is a Python double loop per decal quad (17.7k decals). Vectorisable.
* **`bake/terrainmask.py` (75 s)** — the product is numpy (`:52-84`); the time is in
  `clipgrid.hole_mask` → `containment.contain_faces`, and there is a clear defect:

  ```python
  # tools/csiibundle/containment.py:151-157
  for quad, lane_y, bottom, top in quad_source(r0, c0, r1, c1):
      clipraster.fill_clip_quad(band, fine_y, sw, sh, local, lane_y, bottom, top)
      if top > CLIP_DEEP_BAND_TOP:
          clipraster.fill_clip_quad(deep, fine_y, sw, sh, local, lane_y, bottom, top)
  ```

  `_bin_deep` (`clipgrid.py:180-182`) only bins carving (deep) strips, so essentially every quad
  satisfies `top > 3.0` and `deep` is computed identically to `band` — **the rasterisation runs
  twice**. Also `clipraster.spans_scalar` (`clipraster.py:131-140`) is a Python per-row per-edge
  loop taken for any quad under 8 rows (most, per `clipraster.py:29-30`), and `_rasterise_strip`
  (`clipgrid.py:118-136`) builds 4-tuples per station in Python.
* **`bake/terraincut.py` (56 s)** — fully serial `for (r,c), station_ids in by_face.items()`
  (`:229`) over ~46k squares. `polygon_area` computed twice per kept piece (`:248-253`);
  `layer_at` (`:202-216`) builds a Python list of f-strings per triangle then
  `numpy.asarray(..., dtype=object)` per square, and `sorted(set(tri_mat.tolist()))` (`:291`)
  converts back. `by_face` is independent squares — chunkable.

## 8. `stages/networks.py` (123 units/s) and `stages/materials.py` (278/s)

Networks: pooled per tile (`networks.py:147-157`, `ordered=True, chunksize=2, spawn=True`).
Costs: (1) **`_load_parts` re-decodes glbs with no cache** (`:261-301`, `glb.parse_glb(path)` at
`:270` inside `for rec in recs`); (2) `aliases` pickled into every tile job (`:104-118`);
(3) `tilemesh.build_levels` runs `decimate.cluster_mesh` per LOD target per tile (the §5 search);
(4) DAE writing (`beamng/dae.py`): floats via `array.astype(float32).ravel().tofile(f, sep=" ",
format=FLOAT_FMT)` (`dae.py:259`) and indices via `tofile(sep=" ", format="%d")` (`:300`) —
**`ndarray.tofile(sep=...)` is numpy's legacy per-element formatter**, ~3-5× slower than block
formatting; ~900k `%.4f` conversions on a 300k-vertex tile. `_replace_if_changed`
(`dae.py:190-205`) is fine.

Materials: `export_all` → pooled `_export_chunk` (`materialexport.py:13-32`, chunk 16, spawn) →
`textureplan.export_texture` (`textureplan.py:166-222`) → `images.load_any` → the §4 decoder,
then `dds._encode_level` (etcpak, C — fast) and a mip chain. **The decode is the bottleneck**, and
**the albedo is decoded twice for every non-opaque material**: `plan_canonical` emits
`(src,"color",False)` and `(src,"opacity",False)` (`textureplan.py:88-91`) as two independent jobs
(`materials.py:96-100`) whose per-chunk `cache` (`materialexport.py:16`) keys on
`(output_name, max_size)` (`textureplan.py:177`). `_has_alpha` (`materials.py:358-363`) does a
full decode of every area texture to test `arr[::8, ::8, 3] < 250`.

## 9. `parallel.py`, `worker.py`, `runner.py`

`csiibundle/parallel.py:27-86` is a clean single helper. What is wrong is what is passed through
it — large shared payloads in each argument tuple rather than a `Pool(initializer=...)`:
`bake/lods.py:248` (the materials catalogue), `stages/shapes.py:144-184` (`aliases` + `options` +
`tinted` in every `ShapeJob`), `stages/networks.py:104-118` and `stages/derived.py:125-138`
(`aliases` per tile). Under `spawn` on Windows this is pickle + IPC + unpickle of a multi-MB dict
per chunk. Serial when it could be parallel: `bake/lanegeometry` (`:88`), `bake/markings`
(`:136`, `:212`), `bake/terraincut` (`:229`), `bake/paint.collect_variants` (`:89`),
`bake/roads.load_road_surface` (`roads.py:41`), and every `instances.jsonl` pass in `stages/` —
~370 s of the bake single-core on an 8-core machine. Re-reads between stages that are avoidable:
`derived/materials.json` loaded three ways (`shapes.py:297-301`, `materials.py:74-75`,
`tiles.py:301-305`); `bake/lods.load_index` re-reads `lods.json` it just wrote (`lods.py:62-66`);
`bake/terraincut._carriageway_ceiling` (`:334-353`) decodes `carriageway_ceiling.png` that
`bake/terrainmask` (`:63`) wrote minutes earlier, both through the slow codec.

## 10. Other quadratic / redundant things

* `tints.variant_index` (`tints.py:142-159`) — Python loop over centres, ~2.4 M calls per build.
* `networks._load_parts` has no glb memo (`networks.py:270`).
* `bake/roads.load_road_surface` built twice per bake (`markings.py:108` + `areas.py:248`).
* `bake/skirts` streams the building records twice (`skirts.py:58`, `:75`).
* `containment.contain_faces` rasterises every deep quad twice (`containment.py:151-157`).
* `terraincut` computes `polygon_area` twice per piece and builds `dtype=object` arrays per square.
* `dae.write_shape:117-121` `in list` over ≤ dozens of materials — bounded, not a win.
* `daemesh.split_mesh` (`daemesh.py:129-145`) bisection with `numpy.unique` per probe — only
  above 60k verts.
* `png.decode_png_gray`'s `array("H", …)` + `byteswap` (`png.py:143-146`) instead of a `">u2"` view.
* `meshindex.key_for` (`meshindex.py:78-91`) stats `derived/lods` — every bake invalidates it.
* `stages/lights.py:150` sorts ~177k candidates (each holding the whole record dict) to take the
  first 25k — `heapq.nsmallest`, and a memory spike.

## Ranked wins

| # | Change | Where | Expected | Why |
| --- | --- | --- | --- | --- |
| 1 | Install `pyfqmr` (make `auto` warn loudly when missing) | `decimate.py:48-54`, `lods.py:225` | bake −300 to −900 s | all 5,792 meshes run 24 `numpy.unique(axis=0)` lexsorts on the numpy fallback |
| 2 | Filter-0 fast path in `unfilter_scanlines` + kill the 8 copies | `png.py:64-96, 123-165` | bake −200 to −600 s; materials stage −30-50 % | every raster in both halves goes through a Python row loop; the producer only writes filter 0 |
| 3 | Hoist the albedo sampler cache out of `_process_file`; catalogue into a Pool initializer | `lods.py:69-90, 139, 248, 282` | bake −100 to −400 s + IPC | ~1,400 albedos decoded ~5,792 times; catalogue pickled ~1,448 times |
| 4 | Collapse the 7 `instances.jsonl` passes to 1-2 over a numpy column cache | `reader.py:240`, `orphans.py:70`, `placement.py:198`, `shapedemand.py:44-54`, `instances.py:117`, `lights.py:136`, `levelcheck/terrain.py:84`, `paint.py:89` | build −60 to −100 s; bake −20 s | ~7 passes × ~15 s |
| 5 | Cache serialised object bytes in `Level`; make `write_objects` incremental | `level.py:107-165, 259-273` | build −15 to −30 s | 11-13 calls, ~7 over a 40-50k tree |
| 6 | Parallelise the four serial bake loops | `lanegeometry.py:88`, `markings.py:136,212`, `terraincut.py:229`, `paint.py:89` | bake −250 to −350 s | ~370 s single-threaded on 8 cores |
| 7 | Drop the duplicate `fill_clip_quad` in `contain_faces` | `containment.py:151-157` | bake −40 to −60 s | `deep` is byte-identical to `band` for strips |
| 8 | Vectorise `tints.variant_index`; memo `mesh_candidates`; drop `json.dumps` from `_forest_line` | `tints.py:142-159`, `shapedemand.py:125`, `instances.py:303-314` | build −25 to −45 s | ~2.4 M interpreted centre loops, 1.2 M `json.dumps` |
| 9 | Dedupe texture decodes (group export jobs by source); build the road surface once per bake; memo `parse_glb` in `networks._load_parts` | `textureplan.py:88-91` + `materialexport.py:16`, `markings.py:108`/`areas.py:248`, `networks.py:270` | build −20 to −40 s; bake −60 to −120 s | |
| 10 | zlib level 1 for derived rasters; `crc32(payload, crc32(tag))`; replace `tofile(sep=)` in the DAE writer | `png.py:194-200, 203, 237, 260, 286-293`, `dae.py:259, 300` | bake −30 to −60 s; networks −20-30 % | |

#1 and #2 are independent and together are most of the 62 % LOD block — measure separately.
#4's column cache is the enabling change for #6 and #8.
