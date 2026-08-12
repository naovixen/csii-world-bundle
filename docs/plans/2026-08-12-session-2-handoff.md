# Handoff — 2026-08-12, the FLARE / ALPHA session

Supersedes `docs/plans/2026-08-12-next-session-handoff.md` where they disagree. Read this, then
`docs/plans/2026-08-11-next-session-handoff.md` for the parked consumer hand-off effort.

**One-line status:** FLARE and ALPHA are both closed and validated on the 2026-08-12 09:39 full rip
(VALID, 133 known warnings). Nothing from this session is outstanding. The effort is back at its
parked point, **Phase 5 — extract `tools/cs2bundle/`**.

## What closed

### FLARE — CLOSED, no producer change needed (§43, `66521af`)

It was already fixed by `0eeb24f`, and the 2026-08-11 23:37 rip is the first that could show it.
The two strips §41.9 named read **0.9% / 1.3% unpaved, ≤0.15 m of bare rail**, against 55.6% and
2.52-3.69 m across three earlier rips. City-wide: **5,872 node strips** over sixteen 400 m
windows, p99 0.36 m, max 0.93 m. User confirmed the same coordinates in Blender.

Cause, attributed offline per glTF primitive: the outer band is paved 96.8% by
`ParkingLaneAlleyShoulderToSidewalk2.5 Mesh`, one of the 35 pieces this rip read from the geometry
asset. The `Road` primitive that "stops at xParam 0.475" covers **0.0%** of it — 0.475 is simply
where that piece's own width ends. §41.9's measurement was true and about the wrong piece.

`tools/test_node_fan.py` is the gate; the `SHORT HALF FRAME:` probe is gone (it was mis-oriented
for the left half — a node's left branch lerps curves (0,2), so xParam **0** is that rail).
Closes **PROBE**.

### ALPHA — split in two (§44, `01f4f18`)

**The real defect (fixed and validated).** A `Game.Net.Marker` edge is taken off the Default layer
and put on Marker by `BatchInstanceSystem.UpdateNetInstances`, and the Marker layer draws only
when `RenderingSystem.markersVisible`. The walk's taxiway exception — *"taxiways ... are genuinely
drawn"* — is true of the vector export and false of the renderer, and it shipped **91 opaque
taxiway slabs, 27,648 triangles**. A marker edge now assembles no mesh and ships as a centerline
record: taxiways keep their route and width, and gain no surface.

**The hypothesis (falsified).** "~2,500 materials ship a real alpha channel with no `alphaMode`"
is not a defect. `alphaMode` tracks `template:` with **zero scatter** over all 4,714 materials —
every cutout template clips, every glass/decal/water/overlay template blends, the rest are opaque
— and an opaque HDRP material ignores its base-colour alpha exactly as glTF OPAQUE does. The
"meaningful alpha" on the opaque ones is sign-atlas padding (4-6% transparent border on
billboards and posters) and the `Windows` interior-atlas weight. Do not re-derive this from
texture alpha; §44.2 has the numbers.

**DEADPASS closed.** `hide_invisible_materials` deleted from the harness.

## The re-rip: DONE and green (full rip 2026-08-12 09:39)

All eight stages, 1,717 s, no failures; no producer commit post-dates it. Every prediction landed
to the digit — §44.4 has the table. Headlines: `Centerline-only: 91`, mesh form 7,380 -> **7,289**,
centerline 879 -> **970**, triangles 9,294,225 -> **9,266,577**, **zero** `Invisible_Pathway`
geometry, all 91 taxiways present as centerline records, `SHORT HALF FRAME:` gone. The four pieces
that left the asset-read list (35 -> 31) are exactly the four `Invisible Airplane *` ones.

Gates: validator **VALID, 133 warnings**; contract 58/58; `test_node_fan` PASS (5,872 strips, worst
0.93 m, control live 0.20 -> 11.64 m); `test_clip_cut` green; decal rows, png decode, splat extras,
drape pick all pass. **§43 and §44 are closed. Nothing from this session is outstanding.**

## Method notes earned this session

- **Read the caller before writing the guard.** The first fix for §44.1 was
  `HasMesh && !Hidden` — the game's own per-piece test, correct, and a **no-op**, because
  `NetCompositionMeshRefSystem.CopyMeshPieces` already applies it when it copies to the mesh
  entity, and this port reads that entity's buffer. §44.1b. The buffer we read is **pre-filtered**;
  remember that before diagnosing anything as a piece-level omission.
- **"Deterministic across N rips" rules out flapping, not absence.** A piece the renderer never
  batched in any session is deterministically missing, and that is what sent §41.9 hunting the
  piece-to-frame mapping for three rips. `DROPPED:` also caps its name list at 64 against 2,419
  dropped placements, so absence from it was never evidence.
- **A gate must be shown to fail.** `test_node_fan.py` re-scores the two named segments with their
  outer geometry amputated and fails if the census does *not* then report the defect — 0.20 m
  clean, 11.64 m amputated. Without that, "PASS" on a fresh bundle says nothing.
- **Entity indices in the rip report are per-session; segment ids are not.** The composition ids
  quoted in the 2026-08-11 handoff (`218888`, `572262`) name different compositions in the 23:37
  log. `seg_2301693` and friends survived and are still the right handles.
- Windows housekeeping: `git diff --numstat` after any Python rewrite of a C# file — `utf-8-sig`
  adds a BOM the repo does not use, and `newline=""` is needed or CRLF becomes LF.

## Still open

Everything in `docs/plans/2026-08-11-next-session-handoff.md`'s table — **GRADE-1** (highest, and
a small producer change that should ride the next rip), P16-REG, WATER-2, V1, PILLARS, D1–D6, N
items. Nothing from this session is left open.

Then: **Phase 5 — extract `tools/cs2bundle/`**.
