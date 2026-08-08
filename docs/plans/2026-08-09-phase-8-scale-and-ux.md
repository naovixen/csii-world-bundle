# Phase 8 — Scale, robustness & the one-button rip

PLAN.md's Phase 8 ("Scale & robustness") plus the UX consolidation agreed 2026-08-09: the
five numbered dev buttons collapse into a single asynchronous **Rip World** pipeline with
layer selection and visible progress. The dev-button flow was scaffolding for building phases
in isolation; Phase 8 retires it.

## Scope

### 1. One-button orchestration (replaces the numbered buttons)

A single **Rip World** button runs the whole pipeline in order, asynchronously:

1. Instances + appearance (colors/lights)
2. Meshes, materials & textures
3. Lanes + networks
4. Terrain, water & surfaces
5. Vector export (optional, off by default — the vanilla synchronous call, unchanged)
6. **Manifest — always last, unconditionally.** Folding `BundleWriter.WriteManifest` into
   the pipeline kills the "forgot to re-finalize" foot-gun; it describes whatever the run
   left on disk, so it is also correct after a partial (layer-deselected) rip.

Each stage reports into one end-of-rip summary dialog + `Logs/Carto.log`; the per-step
dialogs go away. The numbered buttons are demoted to a `[SettingsUIDeveloper]` group (they
remain useful for isolating a stage during development), not deleted.

### 2. Ripper tab with coarse layer selection

A dedicated settings tab for the ripper (the buttons currently squat in General). Toggles
map 1:1 to pipeline stages, in consumer-friendly words:

- **Buildings, props & trees** (instances + meshes + appearance)
- **Roads & lanes** (networks + lane graph + lane geometry)
- **Terrain & water** (heightmaps, splat, surfaces, clip volumes are part of this stage's
  output already)
- **Textures** cap + **mesh limit** (the existing dropdowns, moved here)
- **Vector export** (GeoJSON/Shapefile/GeoTIFF alongside)

Keep it this coarse. The contract already handles partial bundles (absent layer = absent
feature; the manifest declares only what exists; the validator agrees), so deselection needs
no new contract work. Fine-grained per-attribute toggles multiply test surface for nothing.

### 3. Texture sizes stay a single-choice cap — DECIDED, do not revisit

Multi-size checkboxes were considered (idea: ship 512/1024/2048 so a consumer like BeamNG
can offer quality settings) and **rejected as redundant on both ends**:

- CS2 stores every texture with a pre-generated mip chain; the cap reads a smaller *existing*
  mip — no resampling. The shipped image at any cap is the game's own image.
- Every consumer engine regenerates mip chains at import (BeamNG converts to DDS with mips;
  its Texture Quality setting drops top mips at load). One full-resolution source yields the
  entire quality ladder downstream for free.
- Shipping N sizes costs ~1.3× storage and N× file count, breaks dedup-by-hash /
  one-path-per-map in `materials.json`, and would need `schema: 2` — for data every consumer
  can derive.

The cap's real meaning is a producer-side ceiling on rip time and bundle size. Rip at the
highest cap you're willing to store; the consumer makes the tiers.

### 4. Async everywhere the mod owns the code

- Meshes/networks already run a slice per frame against a bounded writer queue — the model.
- Instance + lane enumeration currently block the main thread for the whole pass →
  frame-budget them like meshes.
- Terrain stays as-is (short, inherently main-thread GPU readbacks).
- The manifest scan moves off the main thread entirely: capture `BundleManifest` metadata on
  the main thread (cheap), then Task.Run the directory scan; the pipeline waits on it before
  reporting.
- The vanilla vector export stays synchronous (parent-Carto code; out of scope).
- **COMP-1 lesson still binds:** the writer thread's throughput gates the whole rip — no new
  work on the writer's critical path.

### 5. Progress UI — the cheap tier first

Replace/augment the background-process caption (a reflection hack on a locale-key binding)
with the game's own **notification system**, which supports progress-bearing notifications
(the top-left toast stack; several major mods drive live progress bars through it).

- First step is `tools/decomp` on the notification UI system to confirm the API
  (push/update/remove, progress state) — do this before building anything on it.
- If it holds: one notification per rip showing current stage + percent, updated from
  `RipProgress` (the fields already exist; only the sink changes).
- If it does not hold: keep the current caption; a custom cohtml/React UI module is
  **out of scope** — a new JS build toolchain is not worth one progress bar.

### 6. The original Phase 8 acceptance, on top

Run the one-button rip on a big, dense city (the biggest save available):

- completes without OOM, without lockup (frame budget held throughout), game playable after;
- summary reports timings per stage, dedup ratios (instances per mesh, texture dedup),
  bundle size by layer;
- `tools/validate_bundle.py` → 0 errors end-to-end;
- caps (mesh limit, texture size) verified to actually bound time/size.

## Order of work

1. Decomp the notification system; decide progress sink.
2. Orchestrator (stage chain + single summary + manifest-always-last), buttons demoted.
3. Ripper tab + layer toggles wired to the orchestrator.
4. Frame-budget instances/lanes; off-thread manifest scan.
5. Progress notifications.
6. Big-city acceptance run (needs the user in-game), fix what it surfaces, commit per green
   step.

Known constraints carried in: dedupe-by-prefab is non-negotiable; never import the stale
bundle to judge these changes (only the acceptance rip counts); the rip remains additive and
writes only under `ModsData/Carto/`.
