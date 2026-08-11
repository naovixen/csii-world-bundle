# Next-session handoff — written 2026-08-12 00:15, after the clip-cut / piece-residency session

## Start here

Read this file, then `docs/plans/2026-08-11-next-session-handoff.md` for the consumer hand-off
effort this was a detour from. Where they disagree, this one wins — it is later and its numbers
come off a full rip.

**One-line status:** three reported visual defects were chased to root cause and two are fixed and
accepted; the bundle on disk is fresh, VALID and trustworthy; the effort is still parked at
**Phase 5 — extract `tools/cs2bundle/`**, which nothing here has touched.

## State of the world, verified 2026-08-12 00:10

- **Bundle is fresh and reflects HEAD's producer.** Full rip **2026-08-11 23:37**, 1,807 s, all
  eight stages, no failures. `git log --since='2026-08-11 23:37' -- Systems IO Domain` lists
  nothing, so no producer commit post-dates it. Safe to measure against, all layers.
- **Validator: VALID, 133 warnings** — every one the known bridge-pillar class ("mesh is not
  referenced by any instance"; pillars are referenced by networks, not instances). Same count as
  the pre-session baseline, so no new class. Run twice, agreed.
- `tools/test_clip_cut.py` green on all six windows, hole 0.0–0.2 m². `dotnet build -c Release`
  0 warnings / 0 errors, deployed.
- Networks grew this session: 7,236 → **7,380** mesh-form segments, 8.55M → **9.29M** triangles,
  550 → 601 MiB. That is recovered geometry, not bloat — see "what was fixed" below.
- All **4,714** materials now carry a `template:<shader>` flag (23 distinct templates).

## What was fixed, and what it cost

### 1. Terrain holes at junctions — FIXED, user-confirmed in Blender (`516777b`)

A shallow surface band now **flattens but never carves**; only a trench or portal band
(`top > _CLIP_DEEP_BAND_TOP`) deletes terrain. Shallow strips still fill the cell mask, because
that drives the refine corridor, and `_snap_hole_boundary` declines a non-carving strip as a
candidate.

Ground deleted with nothing over it went 9.1 → 0.0 (train tracks), 8.8 → 0.0 (road junction),
41.3 → 0.0 m² (width transition), for +0.8–8.7% terrain faces. Accepted by render: an orthographic
pass over the junction with a transparent film left **0.0 m² of a 110 × 110 m window undrawn**.

**The method lesson, which matters more than the fix.** `test_clip_cut.py` scored the cut against
the clip PRISM and read 1 m² of sky over a window with two holes plainly on screen — because the
prism is not the pavement. It now scores against the real `networks/seg_*.glb` meshes. Its two
exemptions (portal mouth, lot clip) are defined **independently of the rule under test**; deriving
them from `carves()` made the test vacuous, reading 0.8 m² for the exact behaviour that put the
triangles there. **Probe any new gate by reverting the fix and checking it fails.**

### 2. Missing tram deck / piers / truss bridges — FIXED (`0eeb24f`)

`MeshVertex` and friends are a **render cache**, not asset data: `BatchMeshSystem.LoadMeshes`
fills them on demand under a memory budget, a cap of 30 concurrent async loads, and eviction. Which
pieces carry them is a property of the SESSION. `GetPieceGeometry` now prefers
`RenderPrefab.geometryAsset` (as the object path always has) and falls back to the buffers.

`DROPPED:` is gone from the rip report; 35 pieces were read from the asset instead. Elevated trams
102% up-facing (was 13% or 102% by coin toss), ground trams 95% (was 3–67%). Piers, elevated paths,
`TrussArchBridge01Net`, gravel and public-transport lanes were intermittently hollow the same way
and are now deterministic.

`NetMeshHelpers.CacheMeshData` is public and would fill the buffers directly — **rejected**: it
writes to prefab entities behind the renderer's own accounting, and this mod only reads.

### 3. Material shader evidence — SHIPPED (`85ab88d`)

Every material carries `template:<name>` in its `flags`. In `flags` and not a new key because §9's
material object is closed (`additionalProperties: false`), so a `template` key would take the
schema to 3, while `flags` is documented as an open list. Schema description updated.

## Open items

| # | item | severity | what is already known |
|---|---|---|---|
| FLARE | **Junction flare: the outer half of a node's flare is never built.** | MED — visible | Deterministic across three rips with two different dropped sets. Composition **`218888`** (`seg_2301693`) right-reach **0.471**; **`572262`** (`seg_2302757`) left-reach 0.532. Independently, the mesh stops at **xParam 0.475 at every station** while the frame widens 4.36 → 7.26 m — constant in the parameter, not in metres, which is why it only shows where a wider arm stretches the frame. Compare against healthy **`220300`** (right 1.000): both 8 m alleys, `middleOffset 0`. `218888` spans only x ∈ [−1.5, +1.88] of a ±4 m section. Ruled out: `NodeCurves` matches the decompiled `CalculateNodeParameters` term for term in both branches; no piece yielded zero geometry; not a dropped piece; not a systematic under-paving hidden by a neighbour's overlap (own-mesh coverage: `Alley Oneway` 96.5%, `Alley` 99.9%, Medium/Small Road 100.0%). |
| ALPHA | **~2,500 materials ship a meaningful alpha channel with no `alphaMode`.** | MED — visible | 3,944 of 4,714 have a baseColor and no `alphaMode`; of a 160 sample, 104 carry alpha with min < 250. Worked example: the 91 taxiway segments draw as solid **black** — `Invisible_Pathway` resolves to `template:NetCompositionMeshLit` with a baseColor that is pure black with alpha 0→254, and the .glb carries no `alphaMode`, so glTF's OPAQUE default discards it. **Do NOT fix this from the texture's alpha** — the sample is billboards, signs and window hatches alongside surfaces whose alpha channel carries something else, and blanket transparency would make solid walls see-through. The source is shader state: HDRP `_SurfaceType` / `_BlendMode` / `_AlphaCutoffEnable` on the surface or its template, which `MaterialRipSystem`'s sweep misses for several templates. |
| DEADPASS | `hide_invisible_materials` in the harness fires **zero** times. | LOW | It looks for "invisible" in a template name; templates are shader names (`NetCompositionMeshLit`), never asset names. Harmless and correct if such a material ever appears. Decide: keep as a guard, or delete as dead code once ALPHA is fixed. |
| PROBE | The `SHORT HALF FRAME:` / `COLLAPSED:` probes are still in the producer. | LOW | They earned their keep. Decide whether they stay permanently or come out once FLARE is closed. |

Everything in `docs/plans/2026-08-11-next-session-handoff.md`'s open table (GRADE-1, P16-REG,
WATER-2, V1, PILLARS, D1–D6, N items) is **untouched and still open**.

## Method notes worth keeping

- **Two rips before any "layer X is missing" verdict — but the `DROPPED:` line now answers it in
  one.** It counts placements whose buffers were absent and names the distinct pieces. Read
  `Logs/Carto.log` before censusing `networks/`.
- **Not everything intermittent is residency.** FLARE survived three rips with two different
  dropped sets, rail profile identical to the digit.
- **Partial rips are cheap and were essential here.** Uncheck **Rip Objects** and **Rip Terrain**
  in Options → Carto → Ripper: ~5 min instead of ~28, because the mesh stage alone is 1,313 s of a
  1,807 s rip. Files whose stage does not run are NOT cleaned, so the bundle goes mixed-vintage
  rather than broken. Developer mode is not needed for this.
- **Mixed-vintage bundles lie, and I fell for it mid-session.** §42.6 concluded `Invisible_Pathway`
  "serialises to `{}`" from a networks-only rip whose texture stage had not run. On the full rip it
  has a baseColor and a mask. State which layer came from which rip, or do not draw the conclusion.
- **The log is one rip per file** — it is rewritten each session, so capture anything needed from a
  rip before the next one starts.
- **Verify a deployed DLL from its own bytes, not its mtime**, and remember .NET strings are
  **UTF-16** — an ASCII `grep` for a report string finds nothing and reads as "not deployed".
  `data.find(s.encode("utf-16-le"))`.
- **The deploy step fails while CS2 holds the DLL** (`MSB3231`), and the C# still compiles, so a
  build can look successful and ship nothing. That is the likely reason a counter "did not fire" in
  the 20:23 rip and led to a wrong conclusion in §42.3.

## Measurement recipes (the scratchpad is session-scoped; these are the durable parts)

- **Road-mesh coverage scoring** — the reusable core lives in `tools/test_clip_cut.py`
  (`road_cover`, `_fill_triangles`). glTF vertex → bundle frame is
  `world = pos + diag(-1, 1, 1) · v`; every network record ships an identity rotation.
- **Up/down-facing area census** — per category and form, triangle normals from the glb against
  each record's own `length × width`. Reference numbers: road elevated 100%/99%, train elevated
  94%/79%, tram elevated 102%/101% (healthy) vs 13%/101% (deck missing).
- **Node fan coverage** — evaluate a `kind:"node"` clip record's `left`/`right` béziers, walk
  across the frame at each station, and ask how far in you must move before a road triangle covers
  you. A healthy sector reads 0.00 m the whole length.

## Suggested opening prompt

> Continue in `E:\csii-world-bundle`. Read `docs/plans/2026-08-12-next-session-handoff.md` first,
> then `docs/plans/2026-08-11-next-session-handoff.md` for the parked consumer hand-off effort.
> The bundle at `…\ModsData\Carto` is a full rip from 2026-08-11 23:37, VALID with 133 known
> warnings, and reflects HEAD's producer — safe to measure against.
>
> Either close **FLARE** (audit §41.9 — composition 218888 right-reach 0.471 against healthy
> 220300, both 8 m alleys; the diff is in their piece lists and offsets, and it needs the ECS, so
> expect a producer probe and a partial rip), or take **ALPHA** (§42.7 — ~2,500 materials with a
> real alpha channel and no alphaMode; find the shader state `MaterialRipSystem` is not reading,
> and do not infer transparency from the texture).
>
> If neither is wanted, the effort's actual next step is **Phase 5: extract `tools/cs2bundle/`**.
