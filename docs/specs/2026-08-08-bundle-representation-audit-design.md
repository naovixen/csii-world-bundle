# Bundle representation audit — store what the game stores

**Date:** 2026-08-07 (for execution 2026-08-08+)
**Status:** approved (user), pending implementation plan

## The question

A full-city bundle is **4.6 GB**. The game holds the same city in a fraction of that, because
for every domain it stores *the smallest authoritative description* and derives the rest on the
GPU. This work asks, per domain: **what does CS2 actually store, what do we ship instead, and
is the delta information or just representation?**

Where the delta is only representation, we can shrink the bundle without losing anything.

## Measured starting point

| Component | Size | Share | What it is |
|---|---|---|---|
| `textures/` | 1.9 GB | 41% | PNG per unique texture, content-hashed |
| `meshes/` | 1.7 GB | 37% | 5,634 glTF prefab meshes, float32 |
| `networks/` | 423 MB | 9% | 7,380 baked per-segment world-space meshes |
| `instances.jsonl` | 310 MB | 7% | 1.28M placements as JSON text |
| `lanes.jsonl` | 231 MB | 5% | lane paths (AI/navigation), JSON text |
| `terrain/` | 69 MB | 1.5% | rasters — already compact |
| `lanemeshes.jsonl` + `lanemeshes/` | 30 MB | <1% | already parametric (§5B v2) |
| everything else | ~11 MB | — | catalogues, materials, colours, lights |

Two things this table already settles:

- **Networks is not the main event.** It is 9%. Textures and meshes are 78%. The earlier
  "networks is the big waste" framing was too narrow.
- **Lane markings are already cheap on disk** (30 MB). Their cost is *import time*, not bytes —
  a different problem needing a different fix.

## Governing constraints (user-set)

1. **Optimise bundle size, and rip time where possible** — but **export everything we have**.
   Therefore: *no information may be dropped.* Size wins must come from re-encoding, dedup, or
   deriving-instead-of-baking. Anything lossy is a separate, explicitly-flagged decision.
2. **Serve BeamNG-style level building well, without coupling to it.** A consumer must be able
   to build a level; the bundle stays engine-neutral (CLAUDE.md).
3. **Additive layers, retire later.** Each optimised layer ships *alongside* the one it replaces,
   is proven with the A/B gate, and the old layer is retired only in a deliberate later bump —
   exactly how `lanemeshes/` replaced baked lane geometry.
4. **Textures: investigate before choosing a format.** Lossless wins may exist first.

## Phase A — the audit (no code changes)

For each domain, answer the same four questions from decompiled `Game.dll` (`tools/decomp`,
never IL-guessing — see [[inspecting-game-assemblies]]) and from the shipped bundle on disk:

1. What does the game store as the authoritative description?
2. What do we ship instead?
3. Is the delta **information** (we add something real) or **representation** (same facts, more
   bytes)?
4. What would the smallest lossless encoding be, and what does a consumer have to do to use it?

Domains, in size order:

- **Textures (1.9 GB).** Are we shipping channels no material samples? Near-duplicates that
  hash differently after re-encode? Source resolution or upscaled? 3-channel normal maps where
  2 suffice? What does the game hold (BC/DXT, channel-packed ORM, arrays)? PNG encode cost is
  also a prime *rip-time* suspect.
- **Meshes (1.7 GB).** Game uses packed/normalized vertex formats; we write float32 glTF. What
  is the LOD/variant story — are `_L1`–`_L5` level-of-detail or CS2 zoning *levels* (distinct
  prefabs)? Do we ship variants a consumer can never use? Candidates: glTF quantization
  (`KHR_mesh_quantization`), meshopt/Draco as an additive payload, one container vs 5,634 files
  (import speed).
- **Networks (423 MB).** Game keeps one composition mesh + per-segment matrices + curve;
  `NetworkRipSystem.GameMesh.cs` already ports that pipeline and already caches piece geometry
  per composition, then discards it to bake `seg_<id>.glb`. The parametric shape is proven by
  §5B v2 lanes.
- **`instances.jsonl` (310 MB).** 1.28M placements as JSON text. A packed binary/columnar
  payload with a JSON index is lossless and much smaller. Also an import-time win.
- **`lanes.jsonl` (231 MB).** Lane *paths*, not geometry — genuinely useful to a level builder
  (traffic/AI), so keep the data; question only the encoding.
- **Markings/decals.** Game draws one instanced projector box per marking with a curve matrix
  and no CPU per-vertex work ([[cs2-lane-decals-are-projectors]]). Our disk cost is already
  small; this is an **import-time** optimisation.
- **Terrain (69 MB).** Already raster; confirm no cheap win and move on.

**Phase A deliverable:** `docs/cs2-storage-audit.md` — one section per domain answering the four
questions with measured numbers, plus a ranked table of candidate wins (estimated bytes saved,
estimated rip-time saved, consumer cost, risk). Every later phase is chosen from that table,
not from this document's guesses.

## Phases B+ — sequencing principle

Phase A decides the order. The expected shape, to be confirmed or overturned by its findings:

- **B — lossless re-encoding** (`instances.jsonl`, `lanes.jsonl`): pure representation change,
  no consumer semantics to renegotiate, immediate size + import win.
- **C — networks composition layer**: biggest structural win, proven pattern, contract-visible.
- **D — mesh payload**: quantization/compression policy and the LOD/variant question.
- **E — textures**: whatever Phase A proves is available losslessly; any lossy step is a
  separate decision presented with measurements.
- **F — import-time**: instanced markings (Tier 2), and anything else Phase A flags as costing
  the consumer rather than the disk.

Each phase: additive layer → A/B gate (`tools/ab_render.py` + `tools/ab_compare.py`) → validator
+ tests → contract docs → the old layer retired only once its replacement passes.

## Explicit non-goals

- No coupling to BeamNG or any engine; no engine-specific export formats in this repo.
- No lossy transformation adopted silently — each is surfaced with its measurement.
- No dropping of extracted data to save bytes.
- Phase 7's manifest work (`bundle.json` lifting the sidecars) stays where PLAN.md has it; this
  audit must not quietly redesign it.

## Why this is worth doing

A 4.6 GB per-city bundle is awkward to store, slow to import, and slow to produce. The game
proves the same city is describable in far less, and the harness has already shown twice
(`lanemeshes/`, and this week's networks import) that deriving beats baking without losing
fidelity. The audit exists so the next round of work is aimed by measurement rather than by
the assumption that the biggest directory is the biggest opportunity.
