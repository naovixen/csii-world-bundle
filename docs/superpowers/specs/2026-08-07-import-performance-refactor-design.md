# Performance refactor of `tools/blender_import_bundle.py` — design

**Date:** 2026-08-07
**Status:** implemented 2026-08-07 on branch `perf-refactor`, A/B-verified (see the plan and the SDD ledger for measured results)

## Goal

Optimize every phase of the Blender acceptance harness without changing its output:
same geometry, same materials, same collection structure, same reported counts. Add a
stylized, informative console log so a full-city import narrates what it is doing at
every step. The script stays a single file, single process — it remains the readable
reference implementation of the bundle contract (CLAUDE.md).

## Non-goals

- No multiprocessing, no split into a package (decided: single file).
- No per-phase "instrument first, decide later" pass (decided: optimize everything).
- No behavior or CLI changes; no new dependencies beyond `numpy` (bundled with Blender).
- No producer/mod changes.

## Where full-city time goes today

| Phase | Driver |
|---|---|
| Lane bend | 116k records × pure-Python per-vertex math; verts stored as Python tuples (RAM ≈ 18 GB) |
| Network glb import | 7,080 files × ~100 ms fixed Blender glTF-importer overhead |
| Prefab glb import | 1,219 building glbs / 1.4 GB through the same importer |
| Drape + object build | BVH ray-casts (numpy'd 2026-08-07), `from_pydata`, per-loop UV writes |
| Terrain | pure-Python PNG unfilter ×6 rasters; per-face classification; per-loop UV writes |

## Changes

### 1. Fast glb loader (networks + buildings + lane parts)

A numpy GLB parser: JSON chunk + BIN chunk → accessors → mesh via
`vertices.add`/`foreach_set`, loop triangles, UV0 `foreach_set`, per-vertex custom
normals (`normals_split_custom_set_from_vertices`), material slots resolved **by name**
from `bpy.data.materials`.

Safety contract — the fast path only claims a file when it can prove it handles it:

- triangle primitives only; float32 POSITION/NORMAL/TEXCOORD_0; u16/u32 indices;
- no skins, morphs, animations, sparse accessors, or extensions;
- **every material name in the file is already registered in Blender.**

Anything else — including the first file that mentions each distinct material — imports
through Blender's stock glTF importer exactly as today, and its materials are registered
for subsequent fast-path reuse (base-name keyed, cooperating with the existing duplicate-
material collapse). The fast path never constructs a material, so material/texture
rendering cannot regress. Since ~7,080 segments share ~170 materials and building
prefabs share hashed material names, the stock importer runs a bounded number of times.

Both glb consumers are covered: `place_network_mesh` and the instance-prefab loader
(plus the tiny lane-part sources). All glbs are produced by this repo's own `GltfWriter`,
so the parser's feature surface is the writer's feature surface.

### 2. Vectorized lane bend

`_bend_lane_part` becomes per-span numpy: arc tables as arrays, `np.interp` for
arc-length → t, batched Bézier point/tangent/width evaluation (one call per span
covering all rows × tiles), vertex blocks accumulated as numpy arrays instead of
~100-byte tuples (~4× RAM cut). Sinks store `(material, tri_index_block, uv_block)`
runs; object build concatenates blocks and assigns material indices via `np.repeat` +
`foreach_set`. The drape keeps its logic (per-vertex BVH ray-casts, same probe/depth,
same epsilons); hit results are applied with array indexing. The subdivision cache and
frame memoization introduced 2026-08-07 are subsumed by vectorization.

Float-level differences from reordered arithmetic are acceptable; visible differences
are not (see Verification).

### 3. Vectorized terrain

- **PNG decode:** keep the dependency-free reference decoder, but unfilter with numpy —
  filter 0 (None) and 2 (Up) fully vectorized, 1 (Sub) via the cumsum trick; 3 (Average)
  and 4 (Paeth) keep the per-byte path only for scanlines that actually use them.
- Slope and face classification from the height array directly (numpy), not per-face
  Python.
- Splat calibration sampling, SplatUV/UVMap writes, roadbed sink, cutout deletion masks
  and the water mesh: numpy + `foreach_set`.
- Grid mesh built with the shared helper below instead of `from_pydata`.

### 4. Shared `_mesh_from_arrays` helper

One helper building a mesh from numpy arrays (verts, tris, per-loop UVs, material runs)
via `foreach_set`, used by lanes, terrain, and the ribbon-network fallback. `from_pydata`
remains only on cold paths.

### 5. Console log

A small `_Log` helper used by every phase:

- **Phase headers** with elapsed time per phase and a closing summary line
  (items processed, objects built).
- **Progress indicators** for every long loop (instances placed, glbs imported, lanes
  bent, decal verts draped, terrain rows built): in-place `\r` bar when stdout is a TTY;
  periodic plain lines when piped/redirected (PowerShell `Select-String` pipelines keep
  working).
- **Colors** (phase = bold cyan, ok = green, warnings = yellow, `CONTRACT VIOLATION` =
  red) via ANSI, enabled only when stdout is a TTY and `NO_COLOR` is unset; `os.system("")`
  once on Windows to enable VT processing in classic consoles.
- **Silence Blender's glTF importer INFO spam** (the per-file `Data are loaded…` /
  `glTF import finished…` lines) by raising the `io_scene_gltf` logger level — our own
  progress line replaces thousands of theirs. Fast-path files never hit that importer
  anyway.
- All existing message *content* (counts, warnings, calibration results) is preserved —
  formatting may change, numbers may not.

## Explicitly unchanged

CLI flags and defaults; bundle reading order; drape epsilons/step; splat calibration
rules (grass from flat, dirt optional, rock = unpainted residual); material node rigs and
per-instance tint; collection structure and object naming; bilingual comment style; the
contract-reference role (fail loudly with `CONTRACT VIOLATION` on undeclared gaps).

## Verification — the "without regressing" gate

1. Baseline: today's script copied aside before the refactor (plus the step-0 commit).
2. Three A/B regions with old vs new script — suburb junction (2208, 2365), gorge
   (−1454, 5934), crosswalk junction (−2810, 652), each `--kinds none --networks --lanes
   --terrain`: rendered top-down and **pixel-diffed** — pass requires ≥ 99.9% of pixels
   within 2/255 per channel and no pixel beyond 16/255 (allows AA/float noise, fails on
   any moved/missing geometry); all reported numbers (records bent, draped/missed,
   calibration line, face counts, network counts) must match exactly.
3. One `--kinds building` A/B region to exercise the prefab fast path against the stock
   importer (same pixel-diff gate).
4. All 44 contract tests pass.
5. One full-city run; summary numbers compared against the user's current run.

## Risks and fallbacks

- **Fast parser mis-reads an edge-case glb** → guard list falls back to the stock
  importer per file; worst case is today's speed, not wrong output.
- **`normals_split_custom_set_from_vertices` shading differs from importer** → A/B
  building region render catches it; fallback is stock importer for prefabs.
- **In-place progress garbles piped output** → TTY detection defaults to plain lines.
- **Numpy bend produces float drift** → pixel-diff tolerance is tight; anything visible
  fails the gate and the affected sub-path reverts to scalar code.

## Step 0 (housekeeping)

Commit the currently validated harness + docs state first (markings drape/winding,
splat residual-rock calibration, smooth shading, progress lines), so the refactor lands
as its own reviewable diff.

---

## Outcome (2026-08-07, measured)

Full-city `--kinds none --terrain --networks --lanes`, which **died before this work**:

| Phase | Before | After |
|---|---|---|
| Networks (7,380 glbs) | 1,541.1 s | **45.8 s** |
| Lane geometry (119,302 records) | 374.9 s | 381.8 s |
| Terrain | per-face Python | 4.2 s |
| **Total wall** | died / 2,000 s | **499 s** |

Full-city `--kinds building` (the instance path): 8,730 instances from 1,220 prefabs in
**104.7 s** (~9 min before). glb fast path claimed 7,233/7,248 files for networks but only
517/1,170 for buildings — each new building prefab introduces unregistered materials, and the
safety contract sends first-sightings to the stock importer, so the buildings win is ~5x, not ~47x.

Two design assumptions from the plan were **falsified by measurement and dropped**:

- *One drape sample per marking (17x fewer ray-casts).* Measured inside Blender against the full
  imported surface: errors up to 657 mm, 42/1092 samples beyond a marking's own lift. The road
  crown is not the smooth profile the estimate assumed. Shipped an exact per-station reduction
  instead.
- *Left/right ray-cast dedup is exact.* It was only "exact" because the drape was sampling the
  lane centreline rather than each vertex's own laterally-offset position — a defect worth up to
  205 mm on crosswalks, with 40% of vertices past the 14 mm budget. Fixed to sample per vertex;
  the dedup evaporated with it, leaving ~2.1x (sagitta subdivision alone).

The lesson worth keeping: the Tier 1b verification was rigorous but measured only the arc-length
axis, while the defect lived on the width axis. A test can be thorough and still be aimed the
wrong way.

The terrain-grid triangulation the plan mandated was **not** done: the implementer kept quads via
a `poly_size` parameter, because choosing a diagonal can flip a slope's silhouette under the
pixel gate. Good call; the plan was wrong.

Follow-ups recorded, not done here: Tier 2 (instance one shared quad per marking, the game's own
approach) and Tier 3 (stop baking `networks/`; emit a composition catalogue + per-segment
parameters — a bundle-contract change, and the one most visible to a downstream consumer).
