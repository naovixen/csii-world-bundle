# CS2 storage audit — what the game stores, what we ship, and what the delta is

**Phase A of** `docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md`.
**Subject:** the full-city bundle ripped 2026-08-07, at
`…/ModsData/Carto/spike/`, and the `Carto.log` of the rip that produced it.
**Status:** measurement only. Nothing here changes the producer or the contract; the ranked
table in §9 is what later phases are chosen from.

## How to read this

Each domain section answers the same four questions:

1. **What does the game store** as the authoritative description?
2. **What do we ship** instead?
3. Is the delta **information** (we add something real) or **representation** (same facts, more
   bytes)?
4. What is the **smallest lossless encoding**, and what does a consumer have to do to use it?

Every claim carries a number and the command that produced it. Estimates are labelled by
provenance: `measured` (a sample was actually re-encoded and extrapolated by a stated rule),
`arithmetic` (computed from format widths), or `unmeasured`. Two sources are used throughout —
the decompiled game for what it stores, the shipped bundle for what is actually in it — because
a decompile is a claim about code, not about data.

## 0. Baseline

### Environment

```
BUNDLE="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto/spike"
LOG="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/Logs/Carto.log"
```

`ls "$BUNDLE"` lists `areaprefabs.json`, `areas.jsonl`, `colors.json`, `instances.jsonl`,
`lanemeshes/`, `lanemeshes.jsonl`, `laneprefabs.json`, `lanes.jsonl`, `lights.json`,
`materials.json`, `meshes/`, `networks/`, `networks.jsonl`, `terrain/`, `textures/`,
`warnings.jsonl`. `head -1 "$LOG"` is
`[2026-08-07 11:50:23,206] [INFO]  Mod instance loaded. 模組實例載入完成。`.
`python -c "import PIL, numpy, zlib, lzma, struct, json; print('deps ok')"` prints `deps ok`
(Python 3.13.5, PIL 12.3, numpy 2.5).

### Byte-exact census

Command:

```
python "$SCRATCH/audit/baseline.py" "$BUNDLE"
```

(`baseline.py`: walks each top-level entry, sums `st_size` over every file beneath it.)

| entry | bytes | MiB | share | files |
|---|---:|---:|---:|---:|
| textures/ | 1,979,015,958 | 1887.3 | 40.4% | 3,510 |
| meshes/ | 1,800,230,540 | 1716.8 | 36.8% | 5,634 |
| networks/ | 424,469,220 | 404.8 | 8.7% | 7,248 |
| instances.jsonl | 324,202,611 | 309.2 | 6.6% | 1 |
| lanes.jsonl | 242,145,498 | 230.9 | 4.9% | 1 |
| terrain/ | 72,050,547 | 68.7 | 1.5% | 41 |
| lanemeshes.jsonl | 28,063,057 | 26.8 | 0.6% | 1 |
| areas.jsonl | 10,296,261 | 9.8 | 0.2% | 1 |
| colors.json | 5,795,684 | 5.5 | 0.1% | 1 |
| lanemeshes/ | 2,738,524 | 2.6 | 0.1% | 71 |
| networks.jsonl | 2,446,606 | 2.3 | 0.0% | 1 |
| materials.json | 2,120,933 | 2.0 | 0.0% | 1 |
| lights.json | 563,330 | 0.5 | 0.0% | 1 |
| laneprefabs.json | 25,433 | 0.0 | 0.0% | 1 |
| warnings.jsonl | 22,959 | 0.0 | 0.0% | 1 |
| areaprefabs.json | 7,369 | 0.0 | 0.0% | 1 |
| **TOTAL** | **4,894,194,530** | **4667.5** | **100.0%** | |

`4,894,194,530` bytes is the number every later "share of total" percentage in this document
divides by.

### Provenance note: the bundle is not one rip, and `terrain/` has already moved

The census above is of the bundle as it stood at the end of the main rip, **2026-08-07
11:50:23–12:23:24** (`head -1 "$LOG"`, §8's own bracket table). Commit `8985b73` ("Producer:
tunnel lanes tagged, splatmap semantics string; awaiting re-rip validation") has a **git commit
timestamp of 13:54 that day — after this rip finished.** Its file list, confirmed directly:

```
git show --stat 8985b73
```

→ `Domain/LaneGeometryRecord.cs` (+3), `IO/NetworkWriter.cs` (+2), `Systems/MaterialRipSystem.cs`
(1 line, a `private`→`internal` visibility change only — no behavioural effect on §1's texture
bytes), `Systems/MeshRipSystem.cs` (+68), `Systems/NetworkRipSystem.GameMesh.cs` (+109),
`Systems/NetworkRipSystem.cs` (+155), `Systems/TerrainRipSystem.cs` (+392), plus schema/tooling
files. Mapped onto this document's sections: **§2 (meshes), §3 (networks), §6 (markings, via the
new tunnel-lane tag) and §7 (terrain) touch files this commit changed; §4 (`instances.jsonl`) and
§5 (`lanes.jsonl`) do not** — `IO/LaneWriter.cs`, `Domain/LaneRecord.cs` and
`Systems/LaneRipSystem.cs` are absent from the changed-file list, the same correction §5's own
staleness note already made and verified independently here.

**But "changed after the rip" is not the same as "not yet in the rip," and `terrain/` is the
proof.** `terrain/` was re-ripped in isolation at **21:34:25–21:36:17** (the current live
`Carto.log`, 14 lines, `head -1`/`tail -1` timestamps) and file mtimes on disk confirm it: every
other top-level entry in `$BUNDLE` still carries an 11:50–12:23 mtime; every file under
`terrain/` (all 41) carries a 21:36 mtime. That much matches the obvious reading — a later rip,
of `terrain/` only, after the commit landed. What does **not** match the obvious reading:
`full-log-repr.txt` — the preserved copy of the *original* 257-line rip log, §8's own source
(now also `docs/cs2-storage-audit-riplog-2026-08-07.txt`, see §8) — already contains, inside the
original rip's own 12:23:17–12:23:24 terrain bracket, the lines `Splatmap: 4096x4096
ground-blend weights captured (multiplier 2.00, power 1.00)`, `User splatmap: painted surfaces
present, 4096x4096 captured` and `World terrain: no backdrop heightmap resident; layer omitted`
— text that exists nowhere in git history before `8985b73` (`TerrainRipSystem.cs` has zero
matches for `splat` in every ancestor commit, checked directly:
`git log --all --oneline -- Systems/TerrainRipSystem.cs` then `git show <parent>:Systems/TerrainRipSystem.cs
| grep -i splat` → no output). The only consistent reading: the splatmap-capture code was
**already built and running** for the 11:50–12:23 rip, and `8985b73`'s commit — landed
afterward — is a normal build-then-commit gap, not a boundary the terrain data itself crossed.
**Which files changed — confirmed by checksum, not by an unreproducible byte comparison.** A
SHA-256 manifest of `terrain/`'s pre-rerip state exists at
`E:\carto-out\spike-baseline-2026-08-08\terrain.sha256` (its own file mtime 21:32 — after
`8985b73` landed, before the 21:34–21:36 rerip: exactly the pre-overwrite snapshot this claim
needs). Verified directly against the bundle as it stands now:

```
cd "$BUNDLE" && sha256sum -c "/e/carto-out/spike-baseline-2026-08-08/terrain.sha256"
```

→ 38 lines `OK`, 3 lines `FAILED` — `terrain/heightmap.png`, `terrain/splatmap.png` and
`terrain/water_level.png`, and no others, out of all 41 files the manifest covers. `measured`,
full population, reproducible by anyone with access to both the manifest and the current
bundle. This **confirms** the "38 of 41 unchanged, exactly these 3 differ" claim from a
checkable artifact, independent of the byte-count comparison below.

**The pre-rerip byte counts themselves are `unmeasured` by this task and are not
independently re-derivable from this repository.** `splatmap.png` (11,881,840 B before) and
`water_level.png` (783,040 B before) were observed by the audit controller via `ls -la` on
`spike/terrain/` earlier in this session, before the 21:36 rerip overwrote them, and recorded in
`.superpowers/sdd/2026-08-08-bundle-representation-audit/progress.md` — an uncommitted,
untracked coordination ledger, not a script output or a committed file this task produced or can
point a verifier at. They are reported here as **inherited** figures, not measured ones: the
three deltas they imply — `splatmap.png` −1,839,661 B, `water_level.png` −2,485 B, `heightmap.png`
−1 B — sum to exactly 72,050,547 − 70,208,400 = **1,842,147 B**, the whole of the census-to-
current drop, which is internally consistent but does not make the inputs independently
checkable. No code diff exists between the two rips that touches `CaptureSplatmap` (established
above), so this residual delta is examined as session variance, not a format change, in §7 —
using the checksum-confirmed file-identity claim as the checkable half of the finding and the
inherited byte deltas as the clearly-labelled, unmeasured half.

**Net effect on this document's numbers:** the `terrain/` row in the census table above no
longer reproduces from a fresh rip — it is **4,894,194,530 B as censused, with `terrain/` since
reduced by 1,842,147 B (72,050,547 → 70,208,400 B), i.e. 4,892,352,383 B if the bundle were
re-assembled today from the two rips as they currently stand on disk.** §7 measures the current,
21:36 state of `terrain/` (necessarily — the 11:50–12:23 version of `splatmap.png`/
`water_level.png` no longer exists to measure) and says so at its head. A future full re-rip,
run entirely under code at or after `8985b73`, could additionally move the §1 (negligibly), §2,
§3 and §6 byte counts, none of which have been re-measured against post-`8985b73` code in this
task; §4 and §5 would not move, per the file list above.

### Networks count reconciliation

Three numbers were reported for `networks/` before this task: 7,248 (files on disk), 8,259
(a log line), 7,380 (the design spec's estimate table). Commands:

```
ls "$BUNDLE/networks" | wc -l                                     # -> 7248
wc -l < "$BUNDLE/networks.jsonl"                                  # -> 8259
grep -c '"assetId":"seg_' "$BUNDLE/networks.jsonl"                # -> 7248
grep -E 'segment\(s\) to networks.jsonl' "$LOG"                   # -> "Wrote 8259 segment(s) to networks.jsonl in 65.8 s"
```

`Carto.log` itself states the split explicitly, two lines below the write line:

```
Wrote 8259 segment(s) to networks.jsonl in 65.8 s
  mesh form: 7248 (8591923 triangles, 404.8 MiB of glTF in networks/)
  centerline form: 1011 (4026 point(s))
```

The rip records every network segment in `networks.jsonl` (**8,259** records — this is the
correct count of "how many network segments the city has"). Of those, **7,248** carry an
`assetId` pointing at a baked `networks/seg_<id>.glb` mesh, and that number equals both the file
count on disk and the count of `"assetId":"seg_` matches — those three are the same measurement
taken three ways and agree exactly. The remaining **1,011** records have no `assetId` field at
all (confirmed: `grep -v '"assetId":"seg_' "$BUNDLE/networks.jsonl" | grep -c '"assetId"'` →
`0`) — they are written in "centerline form": geometry only as the `centerline` point list plus
`attrs`, with no baked mesh. Breaking those 1,011 down by `attrs.category`
(`E:\cs2-carto-ripper\.worktrees\audit-bundle-representation\.superpowers\sdd\2026-08-08-bundle-representation-audit\measure\audit\no-mesh-records.jsonl`,
extracted with `grep -v '"assetId":"seg_' "$BUNDLE/networks.jsonl" > no-mesh-records.jsonl`):

| category | records without mesh | records with mesh | total |
|---|---:|---:|---:|
| cable | 693 | 0 | 693 |
| pipe | 105 | 0 | 105 |
| taxiway | 91 | 0 | 91 |
| waterway | 81 | 0 | 81 |
| pathway | 41 | 207 | 248 |
| road | 0 | 5,725 | 5,725 |
| highway | 0 | 943 | 943 |
| train | 0 | 313 | 313 |
| tram | 0 | 60 | 60 |

`cable`, `pipe`, `taxiway` and `waterway` are entirely mesh-less — every segment in those four
categories is centerline-only, 100% of the time. `pathway` is split: 207 of 248 have a mesh, 41
do not. All 41 mesh-less `pathway` records share `attrs.form == "elevated"` and
`attrs.asset == "LeisurePier01Small01"`, but 11 *other* `LeisurePier01Small01` segments in the
same rip *do* have a mesh, and segment length does not separate the two groups (meshed lengths
run 10.3–52.1 m, mesh-less lengths run 8.4–70.7 m, fully overlapping) — so for this one asset the
mesh/no-mesh split is not explained by category, form, prefab name, or length alone; it is
recorded here as `unmeasured` and left for whichever later task inspects piers.

7,380 (the design spec's estimate table, `docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md:21`)
matches none of the three measured values (7,248 files; 8,259 records; 7,248 records with a
`seg_` `assetId`) and is superseded by this section's measured figures.

## 1. Textures

`textures/` is 1,979,015,958 bytes (1887.3 MiB, 40.4% of the 4,894,194,530-byte total, §0),
3,510 files — the single largest share of the bundle.

### 1.1 What we ship

Command:

```
python "$SCRATCH/audit/tex_inventory.py" "$BUNDLE"
```

(`tex_inventory.py`: opens every `textures/*.png` header-only via PIL — no decode — and tallies
size, dimensions, pixel mode.) `measured`, full population (3,510 of 3,510 files):

| | |
|---|---:|
| files | 3,510 |
| bytes | 1,979,015,958 (1887.3 MiB) — matches §0's census exactly |
| pixel mode | `RGBA` on all 3,510 files, no exceptions |
| 1024×1024 | 3,446 files (98.2%) |
| other dimensions | 512×1024 (22), 1024×512 (13), 512×512 (13), 256×1024 (6), 128×1024 (6), 256×256 (4) |
| size, p50 / p90 / p99 / p100 | 0.55 / 1.08 / 1.67 / 2.48 MiB |

Every shipped texture is colour type 6 (RGBA), bit depth 8, no interlacing, filter byte `0`
("None") on every scanline, `Deflater.DEFAULT_COMPRESSION` — `IO/Png.cs:51,60,167-171` (read in
full for this section). `IO/Png.cs`'s own doc comment states per-row filter selection "would
shrink the output somewhat" and stops there; §1.4 measures "somewhat."

### 1.2 What the game stores

Two sources, per the doc's own rule. First, the rip log's own record of the *source* GPU
format and resolution, from the run that produced this bundle:

```
grep -oE '(RGBA|R|RG|BC)[A-Za-z0-9_]*(SRGB|UNorm|Float)' "$LOG" | sort | uniq -c | sort -rn
grep -oE 'header [0-9]+x[0-9]+' "$LOG" | sort | uniq -c | sort -rn
grep -oE '[0-9]+x[0-9]+ [A-Z][A-Za-z0-9_]+' "$LOG" | sort | uniq -c | sort -rn | head -20
```

`measured` (from the log, not inferred): every VT-route format string in the whole log is
`RGBA_BC7_UNorm` (9 lines) or `RGBA_BC7_SRGB` (3 lines) — **BC7 only, no BC5, no other format,
anywhere in the log.** Slot correlates with colour space, not with map kind's bit layout —
`_BaseColorMap` is `BC7_SRGB`, `_NormalMap`/`_MaskMap`/`_ControlMask` are all `BC7_UNorm`
(`$LOG` lines 108–115, 195–198 — `VT recovery [n]` / `VT surface recovery [n]`, the first four
maps of the first two prefabs the mesh pass touched). These are debug-sample lines (indices
`[1]`–`[4]`, not a per-texture log of all 3,510) but every one of the 12 that exist agrees, and
the second source below confirms the mechanism, not just the count.

The same 8 `header WxH` lines also show **one asset** whose source resolution exceeds what we
ship. Re-checked directly (`grep -n 'header [0-9]\+x[0-9]\+' "$LOG"`, full output): the 8 lines
are all four maps of exactly two prefabs, `PD-GrassLong01-TreeAdult_...` (`$LOG:108-111`, all
four maps `header 1024x1024, ... written 1024x1024` — no downsample) and
`FenceIndustrialPieceHigh02_...` (`$LOG:195-198`, all four maps `header 2048x2048, loaded
2048x2048 RGBA_BC7_SRGB/UNorm, written 1024x1024` — downsampled). So it is **one asset**,
across **4 of the 8** sampled `header` lines, not two assets or 2 of 8 as an earlier draft of
this section stated. This is `Settings.cs:537`'s `RipTextureSize` default (1024) acting as
`MeshRipSystem.BeginRip`'s/`NetworkRipSystem.BeginRip`'s `textureSizeCap` parameter
(`Systems/MeshRipSystem.cs:432`, `Systems/NetworkRipSystem.cs:420`) — a **producer setting for
this run**, not a game-storage fact, and it means some of what we ship is already a downsample
of a larger game-native asset, separate from the lossless-encoding question §1.4 answers. The
log samples only 2 of the run's thousands of textures at this granularity (indices `[1]`–`[4]`
of the first prefab each phase touches), so whether other textures anywhere in the 3,510 were
also downsampled remains `unmeasured` — this pair of examples establishes only that the
mechanism exists and fires at least once, not its overall frequency.

Second, source, confirming the format story rather than inferring it from map names:

```
cd tools/decomp && dotnet build -c Release && cd ../..
./tools/decomp/bin/Release/net8.0/decomp.exe type Colossal.IO.AssetDatabase Colossal.IO.AssetDatabase.SurfaceAsset  > "$SCRATCH/audit/SurfaceAsset.cs"
./tools/decomp/bin/Release/net8.0/decomp.exe type Colossal.IO.AssetDatabase Colossal.IO.AssetDatabase.VTSurfaceAsset > "$SCRATCH/audit/VTSurfaceAsset.cs"
```

`SurfaceAsset.cs:788` (`$SCRATCH/audit/SurfaceAsset.cs`): `GraphicsFormat graphicsFormat =
virtualTexturingConfig.stackDatas[stackConfigIndex].layerFormats[layerIndexInStack];`, and the
surrounding block errors (`AssetData.log.ErrorFormat("...is of the wrong format...")`) if a
texture's actual format doesn't match. The format is a **configured constant per (stack, layer)
slot**, validated strictly at import — not a name-based guess by the game either, which is the
same shape of fact the log's own per-slot consistency (§ above) already showed empirically.
This is the mechanism behind audit `docs/cs2-api-audit.md:1090` (§24 row 4)'s DXT5nm/BC3n
packing note, restated here rather than re-derived, per this task's brief.

**How many bytes would the game's own representation of these same 3,510 images occupy?** BC7
is 1 byte/pixel (arithmetic: a 4×4 block is 16 bytes = 1 byte/texel). Computed from §1.1's
dimension census:

```
1024×1024×3,446 + 512×1024×22 + 1024×512×13 + 512×512×13 + 128×1024×6 + 256×1024×6 + 256×256×4
  = 3,637,772,288 pixels → 3,637,772,288 bytes at 1 B/px = 3469.2 MiB
```

`arithmetic`. Our shipped 1887.3 MiB is **0.544× that** — we ship *smaller*, not larger, than
the game's own uncompressed-in-VRAM BC7 footprint at the same pixel dimensions, despite being
bit-exact lossless where BC7 is a lossy block code. That is the honest framing of question 3 for
this domain: the naive "our format must balloon over the game's" assumption is false by
measurement. (This says nothing about GPU-sampling cost or mip chains, which BC7 has and a flat
PNG dump does not — it is a raw-byte comparison, not a claim that PNG is a better texture
format in general.)

### 1.3 Information or representation?

**Orphans** — is every shipped texture actually referenced?

```
python - <<'PY'
import json, collections, os
materials = json.load(open(os.environ["BUNDLE"] + "/materials.json", encoding="utf-8"))
referenced = {v for entry in materials.values() for v in entry.values() if isinstance(v, str) and v.endswith('.png')}
on_disk = {f"textures/{n}" for n in os.listdir(os.environ["BUNDLE"] + "/textures")}
print(f"referenced by a material: {len(referenced):,}")
print(f"on disk:                  {len(on_disk):,}")
print(f"orphans (shipped, unreferenced): {len(on_disk - referenced):,}")
PY
```

`measured`: 4,635 materials reference exactly 3,510 distinct `.png` paths; `on_disk − referenced
= 0`. **Zero orphans** — every shipped texture is referenced by `materials.json` itself, so
there is nothing to cross-check against terrain/`colors.json`/decal-atlas references (the
brief's trap only bites when the first-pass orphan count is nonzero). Closed, negative result.

**Content-hash duplicate check** — are two differently-hashed files ever pixel-identical?

```
python - <<'PY'
import hashlib, os, collections
from pathlib import Path
import numpy
from PIL import Image
root = Path(os.environ["BUNDLE"]) / "textures"
seen = collections.defaultdict(list)
for path in sorted(root.glob("*.png")):
    with Image.open(path) as image:
        seen[hashlib.sha256(numpy.asarray(image.convert("RGBA")).tobytes()).hexdigest()].append(path)
dupes = {k: v for k, v in seen.items() if len(v) > 1}
waste = sum(p.stat().st_size for group in dupes.values() for p in group[1:])
print(f"{len(dupes):,} duplicate pixel-groups, {waste/1048576:.1f} MiB recoverable")
PY
```

`measured`, full population: **0 duplicate pixel-groups.** `TextureWriter.cs`'s content-hash
dedup (§8) is already exact — closed, negative result, nothing left on the table here.

**Channel constancy and derivability** — run over the **full population**, not a sample (the
300-file default was fast enough — 16.5 s — to raise to all 3,510, which turns "extrapolated"
into "exact" and needed no factor):

```
python "$SCRATCH/audit/tex_channels.py" "$BUNDLE" 3510
```

`measured`, exact (3,510 of 3,510 — extrapolation factor 1.0×, printed by the script itself):

| finding | files | % | bytes (exact) |
|---|---:|---:|---:|
| alpha channel constant across the whole image | 1,045 | 29.8% | 761 MiB |
| R=G=B everywhere (true greyscale shipped as RGBA) | 248 | 7.1% | 69 MiB |
| B reconstructible from R,G via `sqrt(1−x²−y²)` (packed normal) | 840 | 23.9% | 754 MiB |

These three are **representation, not information**: same facts, more bytes, not new facts
invented. None of them is a "channel nothing samples" claim — §7.1's `mask.G`/`mask.B` are the
one place this bundle documents a channel of *uncertain* use (`docs/bundle-schema.md:881-885`,
"advisory... not recoverable from managed code"), and that uncertainty is already recorded and
settled as unresolved by a prior pass (`docs/cs2-api-audit.md` §11/§24) — this task does not
reopen it, and proposes no drop of `mask.G`/`mask.B`, because "advisory/uncertain" is not
"proven unsampled."

The normal-map finding matches `docs/cs2-api-audit.md:1090` (§24 row 4) exactly: `SurfaceAsset`
packs normals DXT5nm/BC3n-style (X in alpha, Y in green, blue mirrors green, red pinned high),
and `MaterialRipSystem.ReconstructTwoChannelNormal` (`Systems/MaterialRipSystem.cs:1488-1518`)
already rebuilds `B` on the producer side with **exactly** this task's `sqrt(1−x²−y²)` formula,
then hard-sets `A = 255` (`:1517`). The tolerance the detection script uses (`≤2`/255) is a
*detection* margin only — §1.4 verifies the true reconstruction error separately.

### 1.4 Smallest lossless encoding

Four re-encode variants, one bit-exact-decode assertion each, on the same 120-file sample (the
brief's default; a 120-file run costs ≈2 minutes wall time here, so it was kept rather than
raised — raising to the full population would cost ≈1 hour for the zlib-9 variant alone, not
worth it for a number this section already extrapolates safely):

```
python "$SCRATCH/audit/tex_reencode.py" "$BUNDLE" 120
```

Extrapolation rule, stated explicitly: `total shipped bytes (1,979,015,958, §0) ÷ sample bytes
(74,917,559, this sample) = 26.4159…×`, applied to each variant's sample bytes. `measured`.

| variant | sample result | extrapolated full-bundle | vs. as-shipped |
|---|---|---:|---:|
| as-shipped (filter None, `DEFAULT_COMPRESSION`) | 74,917,559 B (100.0%) | 1887.3 MiB | — |
| adaptive filter, zlib 6 | 72,358,251 B (96.6%), assertion passed | 1822.9 MiB | **−64.5 MiB (−3.4%)** |
| adaptive filter, zlib 9 | 68,888,004 B (92.0%), assertion passed | 1735.4 MiB | **−151.9 MiB (−8.0%)** |

Both rows are proven lossless: the script decodes each blob and asserts
`numpy.array_equal(decoded, pixels)` before counting it, and both assertions passed for all 120
files (`$SCRATCH/audit/tex_reencode.txt`). This is the headline hypothesis's actual number —
`IO/Png.cs`'s "would shrink the output somewhat" is real (adaptive filtering alone: 3.4%) but
the brief's own speculative framing (20–40%, 400–800 MB) is **not supported by measurement**;
the true combined ceiling this section finds, below, is 11.2%.

**The brief's fourth variant ("channel-reduced + adaptive") has no assertion in its own loop and
is not safe as written** — its rule drops the alpha channel whenever it is *constant*, not only
when it is constant at 255. A standard PNG decoder reconstructs a missing alpha channel as 255
(fully opaque); a file whose true constant alpha is, say, 0 (fully transparent) round-trips
**wrong** under that rule. Checked directly (`$SCRATCH/audit/tex_reencode_channels_verify.py`,
same 120-file sample): 6 of 120 (5.0%) sampled files have alpha constant at a value other than
255 (all 6 measured at exactly 0), and re-decoding the brief's own naive output for those 6
does **not** reproduce the original pixels — confirmed by explicit round-trip check, not
inferred. This row is **not reported as measured in the brief's original form.**

A corrected, PNG-spec-safe rule — collapse to `RGB` only when alpha is constant **and equals
255**, collapse further to `L` when R=G=B too, otherwise keep `RGBA` — was measured instead on
the same sample, same zlib level, with a genuine per-file bit-exact assertion:

```
sample 120 files, 74,917,559 bytes as shipped
naive (brief's script) rule:  66,527,877 B (88.8%), UNSAFE round-trips: 6 of 120
safe (alpha==255 only) rule:  66,553,501 B (88.8%), files reduced: 29, all 120 assertions passed
```

The correction costs almost nothing (25,624 bytes over the 120-file sample, ≈0.03%): extrapolated
by the same 26.4159× rule, **1676.6 MiB, −210.7 MiB (−11.2%) vs. as-shipped, proven lossless.**
Consumer cost: **none** for a decoder that already handles standard PNG colour types — checked
against this bundle's own reference consumer, `tools/blender_import_bundle.py:2165`
(`bpy.data.images.load(texture_path)`), which is Blender's general PNG loader and expands
`L`/`RGB` to `RGBA` (alpha = 255) automatically, matching the safe rule's own invariant exactly.

**Normal-map two-channel repack** — separate candidate, not folded into the row above, because
it needs a schema change and consumer-side math, not just a PNG mode change. §1.3 found 840
files (754.3 MiB, exact/full-population) where B is derivable from R,G. Whether that
reconstruction is truly bit-exact (not merely within the detector's ±2 tolerance) was checked
directly, full population:

```
candidate files (<=2 tolerance detection): 840 of 3,510
exact after round-to-nearest reconstruction: 840 of 840
worst-case int diff among candidates: 0
```

`measured`, exact, full population — **all 840 reconstruct to the exact original byte with
`round(sqrt(1−x²−y²)×127.5+127.5)`, the identical formula and rounding rule
`ReconstructTwoChannelNormal` already applies going the other direction** (§1.3). What actually
repacking as a 2-channel PNG (colour type 4, grey+alpha, storing X in "grey" and Y in "alpha")
saves was then measured on a 120-file sub-sample (candidates that are also alpha-constant, so
the dropped `A` doesn't need separate handling), decoding, reconstructing B, and rebuilding the
full RGBA array to assert against the original:

```
120 candidate files: raw as-shipped 110,827,481 B; two-channel zlib-9 repack 77,110,849 B
saved 33,716,632 B (30.4%), all 120 round-trips bit-exact
```

Extrapolated (rule: apply the sample's kept-byte ratio, 0.6958, to the exact 754.3 MiB
full-population candidate total — not the 26.4159× sample-scale used above, because this
candidate set is a different, non-uniform subset of files, not a size-weighted random sample of
the whole bundle): **229.5 MiB saved (11.6% of this candidate subset's own extrapolation base;
12.2% of all of `textures/`), proven lossless on every file actually re-encoded.**

Consumer cost here is **not** zero: colour type 4 is a schema addition (`docs/bundle-schema.md`
§7.1's table assumes RGBA for `normal`), and a consumer must add the reconstruction step at
load time (cheap — the same formula, once) rather than getting it for free from a generic PNG
decoder the way §1.4's RGB/L collapse does. It is still fully lossless, and still an additive
layer (a new `normal2` PNG colour-type-4 convention that a consumer opts into, with the current
4-channel `normal` untouched for anyone who hasn't).

**Lossy, named but not measured** (constraint: investigate lossless first, do not recommend a
lossy format here). The game's own native format, BC7, is **not** actually smaller than our
current lossless PNG at these dimensions (§1.2: 0.544× the other way) — naming *that* as a
"smaller" candidate would be wrong. A genuinely smaller lossy codec (e.g. WebP or AVIF at a
quality setting, applied after the lossless wins above) is named here as `TEX-6` for §9 to rank,
with `lossless? = NO` and its error bound **`unmeasured`** — Phase A does not encode or measure
it.

### Rip time

There is no dedicated "texture" row in §0's phase table — texture work is interleaved inside
row 2 (Meshes) and row 3 (Lanes + Networks), not a separate bracket. `IO/TextureWriter.cs`'s own
doc comment states texture encode "is safe off the main thread," which is true and was
verified: `TextureWriter.Write` (`IO/TextureWriter.cs:140`) is called only from
`MaterialRipSystem.WriteTexture` (`Systems/MaterialRipSystem.cs:359`), which in turn is called
only from `MeshRipSystem.WriteQueued` (`Systems/MeshRipSystem.cs:715-729`, one call site,
`:727`) and `NetworkRipSystem.Encode` (`Systems/NetworkRipSystem.cs:1342-1361`, one call site,
`:1359`) — both of which run as the **single** body of a `Task.Run` (`Systems/MeshRipSystem.cs:468`,
`Systems/NetworkRipSystem.cs:512`), off the main thread, confirmed.

**But "off the main thread" is not the same as "off the critical path."** Both `WriteQueued` and
`Encode` are one `foreach` over one `BlockingCollection`-backed queue that also carries mesh
`.glb` / network-segment `.glb` writes (`Systems/MeshRipSystem.cs:719-738`,
`Systems/NetworkRipSystem.cs:1346-1369`) — texture PNG encoding and mesh serialisation share the
**same single background thread**, serially, not two independent workers. And that thread gates
phase completion: `MeshRipSystem.Finish` calls `run.Writer.Wait()`
(`Systems/MeshRipSystem.cs:1215`) before the phase's report is written and the mesh-rip phase
is considered done — confirmed by reading the call chain, not inferred from the log. The
`RipProgress.Phase` enum (`IO/RipProgress.cs:43-53`) has no separate texture phase either,
consistent with texture work being folded into `Meshes`/row 2 and the lane+network pass/row 3.

So: PNG encode time for all 3,510 textures **is** on the critical path — it is serialised
against, and directly extends, the same wall-clock bracket as mesh writing. §0's row 2 gives the
evidence this predicts: "Mesh writing: `Wrote 4198 .glb...in 1178.3 s`" (instrumented) is within
1.2 s of the entire row-2 bracket (1177.106 s) — i.e. essentially the *whole* Meshes phase is
consumed by this one serial writer thread doing both mesh serialisation and texture encoding,
while the main-thread GPU readback (347.1 s, §0) finishes with time to spare inside that same
window. **The exact split between mesh-serialise time and texture-PNG-encode time within the
1178.3 s is `unmeasured`** — the log has no per-operation instrumentation to separate them, and
adding that instrumentation would be a production-code change out of scope for Phase A.

Whether adaptive filtering (§1.4) would measurably shrink that 1178.3 s is therefore also
`unmeasured`, and deliberately not estimated from this section's Python/Pillow encode-time
numbers (10.2 s / 57.5 s for zlib 6 / zlib 9 over the 120-file sample) — those numbers are
CPython + Pillow's zlib binding, not `Deflater`/SharpZipLib in .NET, and are not a valid stand-in
for production encoder timing. A real answer needs the change made and the rip re-timed, not a
cross-language guess.

### Candidate rows

**TEX-3 and TEX-4 overlap and are not additive.** Both claim the same alpha-channel saving on
the same files: TEX-3's safe rule drops alpha whenever it is constant at 255 (keeping B);
TEX-4's repack, on the *same* alpha==255-and-normal-derivable files, drops alpha too (folding
it into a 2-channel form that also drops B). Checked directly, full population (not sampled):

```
normal-B-derivable candidates: 840
  alpha constant == 255: 840
  alpha constant, != 255: 0
  alpha not constant: 0
```

`measured`, exact — **all 840 of the 840 files TEX-4 targets also have alpha constant at 255**,
i.e. TEX-4's whole candidate set is a strict subset of TEX-3's (every TEX-4 file also qualifies
for TEX-3's "drop alpha" rule alone). Summing TEX-3's 210.7 MiB and TEX-4's 229.5 MiB therefore
double-subtracts the alpha channel on exactly those 840 files, confirmed by full-population
count rather than estimated — the correct combined figure is smaller than the sum, measured
directly below rather than summed. (A 500-file
joint sample independently corroborates the overlap at run time: of the 127 sampled files
qualifying for either row, 122 — 96.1% — hit the deeper TEX-4 branch, consistent with the exact
840/840 full-population figure above within sampling noise.)

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| TEX-1 | Adaptive per-row PNG filtering (replace fixed filter 0), same zlib level | measured, 64.5 MiB (3.4%) | unmeasured | none | low | YES | not applicable — in-place re-encode of the same file at the same path; no new layer, nothing retired |
| TEX-2 | Adaptive filtering + raise zlib level to 9 | measured, 151.9 MiB (8.0%) | unmeasured (encode CPU cost rises; needs a C#-side timed re-rip, not a Python proxy) | none | low | YES | not applicable — same as TEX-1, in-place re-encode |
| TEX-3 | Safe channel reduction (drop alpha only when constant at exactly 255; collapse to greyscale when R=G=B too) + TEX-2, replacing the brief's own unsafe rule | measured, 210.7 MiB (11.2%) *alone* — see the joint row below, this is not additive with TEX-4 | unmeasured | none (verified against the reference Blender PNG loader) | low | YES | not applicable — in-place re-encode; PNG colour type changes but the file's path/contract slot is unchanged, nothing new to retire |
| TEX-4 | Normal-map 2-channel repack (colour type 4; drop B, reconstruct via `round(sqrt(1−x²−y²)×127.5+127.5)` at load, same formula the producer already applies in reverse) | measured, 229.5 MiB (12.2% of `textures/`; 30.4% of the 754.3 MiB candidate subset) *alone* — see the joint row below, this is not additive with TEX-3 | unmeasured | schema addition (§7.1 amendment) + one reconstruction step at load; not free like TEX-3 | medium — schema change, and only covers producer-detected packed normals (840 of 3,510 files) | YES, verified exact on full population (840/840) plus a 120-file re-encode round-trip | **new layer**: `normal2`, a colour-type-4 (grey+alpha) convention alongside the existing `normal` map; **retires**: the 4-channel `normal` PNG, once consumers adopt it — until then `normal` stays and this ships alongside it |
| TEX-3+4 joint | One representation per file — the deepest each file qualifies for (TEX-4's 2-channel form where a file is both alpha==255 and normal-derivable; otherwise TEX-3's RGB/L form where only alpha==255; otherwise adaptive-filtered RGBA) — replaces citing TEX-3 and TEX-4 as separately additive | measured, **377.1 MiB (19.98%)**, see method below | unmeasured | union of TEX-3's (none) and TEX-4's (schema addition + reconstruction step) — whichever applies per file | medium (inherits TEX-4's schema-change risk for the subset it touches) | YES, all 500 sampled files decoded and reconstructed bit-exact | same as TEX-4 for the files it touches (`normal2`); in-place re-encode (no new layer) for the rest |
| TEX-5 | Delete orphan textures | 0 — none found | n/a | n/a | n/a | n/a (closed, negative result) | not applicable — no files to touch, nothing to retire |

**TEX-3+4 joint, method:** measured directly rather than summed, on a 500-file sample (raised
from 120 because the branching logic needed enough files in each branch to be meaningful — the
120-file TEX-3/TEX-4 samples individually had too few normal-and-alpha-255 files to branch-test
reliably together):

```
sample 500 of 3,510 files
raw as-shipped: 276,005,696 bytes (263.2 MiB)
joint (one representation per file): 220,863,237 bytes (210.6 MiB), 80.0% of as-shipped
saved: 55,142,459 bytes (20.0%), all 500 files verified bit-exact
branch counts: baseline (RGBA) 373, TEX-4 (2-channel) 122, TEX-3 (L) 2, TEX-3 (RGB) 3
```

Extrapolation rule, stated explicitly: `total shipped bytes (1,979,015,958, §0) ÷ this sample's
bytes (276,005,696) = 7.1702×`, applied to the joint total. `measured`: **1510.3 MiB shipped,
−377.1 MiB (−19.98%) vs. as-shipped** — this is the correct combined lossless figure for
TEX-3+TEX-4 together, smaller than the erroneous naive sum (1887.3 − 210.7 − 229.5 = 1447.1 MiB,
i.e. a claimed 440.2 MiB saved) by 63.1 MiB, exactly because that sum double-subtracted the
overlapping alpha channel. **Do not cite TEX-3's and TEX-4's individual savings as additive; cite
this row instead when a single combined number is needed.**

**Lossy candidates** (named for §9 to rank; not recommended here — constraint 6 requires
lossless to be investigated first, and none of the rows above needed a lossy fallback to reach
their savings):

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | error bound | additive layer |
|---|---|---|---|---|---|---|---|---|
| TEX-6 | Lossy re-encode (e.g. WebP/AVIF at a quality setting) of whatever remains after TEX-1…4/joint | unmeasured | unmeasured | high — consumer needs a new-format decoder | high | **NO** | `unmeasured` — Phase A does not encode or measure it; BC7 itself is *not* a smaller candidate at these dimensions (§1.2: 0.544× the other way), so it is not the lossy option named here | would replace `textures/*.png` outright for the files it touches — a bigger change than any lossless row above, not expressible as an opt-in additive layer without also adding a codec-selector field to the schema (not designed here) |

## 2. Meshes

`meshes/` is 1,800,230,540 bytes (1716.8 MiB, 36.8% of the 4,894,194,530-byte total, §0), 5,634
files — the second-largest share.

### 2.1 What we ship

Command (brief's own script, run first, exactly as given):

```
python "$SCRATCH/audit/mesh_inventory.py" "$BUNDLE"
```

**This failed the section's own pass criterion.** The brief's script sums attribute bytes
per-primitive; the sum (`TANGENT` 1122.0 + `POSITION` 841.5 + `NORMAL` 841.5 + `TEXCOORD_0`
561.0 + `INDICES` 230.4 = 3596.4 MiB) is **2.1× the 1716.8 MiB on-disk total** — the opposite of
the "gap is JSON overhead" framing the brief's own comment predicts. Root cause, found by
reading `IO/GltfWriter.cs:300-424` (read in full for this section): `AppendMesh` creates the
`POSITION`/`NORMAL`/`TEXCOORD_0`/`TANGENT` accessors **once per glTF mesh** (one mesh = one
LOD/state), then attaches the **same** accessor index to every per-material primitive's
`attributes` object (`:421-424`, inside the `foreach (string materialId in materialOrder)`
loop) — a mesh with *N* materials has *N* primitives all pointing at one shared vertex/normal/
UV/tangent accessor. Summing per-primitive, as the brief's script does, counts a shared
accessor's bytes once per material referencing it, not once. Confirmed directly rather than
just reasoned from source: **35,813 per-primitive attribute references, 23,089 unique accessor
indices — a 1.551× dedup ratio**, the exact shape of the discrepancy.

Corrected script (`$SCRATCH/audit/mesh_inventory_v2.py`, dedupes by `(file, accessor index)`
before summing — the fix is a four-line change: track a per-file `seen` set of accessor
indices and skip a semantic's byte count on a repeat):

```
python "$SCRATCH/audit/mesh_inventory_v2.py" "$BUNDLE"
```

`measured`, full population (5,634 of 5,634 files):

| | |
|---|---:|
| files | 5,634 |
| bytes | 1,800,230,540 (1716.8 MiB) — matches §0's census exactly |
| unique vertices (deduped) | 32,210,211 |
| triangles | 20,135,918 |
| `POSITION` (f32) | 368.6 MiB (21.5% of files) |
| `NORMAL` (f32) | 368.6 MiB (21.5% of files) |
| `TANGENT` (f32) | 491.5 MiB (28.6% of files) — the single largest attribute, larger than `POSITION` |
| `TEXCOORD_0` (f32) | 245.7 MiB (14.3% of files) |
| `INDICES` (u32) | 230.4 MiB (13.4% of files) |
| sum of attribute + index bytes | 1,704.9 MiB |
| JSON + padding overhead | 11.9 MiB (0.7% of files) |

1,704.9 + 11.9 = 1,716.8 MiB, matching the on-disk total exactly — the corrected script passes
the section's own check that the brief's original script failed. `TANGENT` being the largest
attribute (not `POSITION`) is worth flagging for a later task: tangents are in principle
reconstructible from `POSITION`/`NORMAL`/`TEXCOORD_0` via a standard MikkTSpace-style
per-triangle computation, but whether that reconstruction would match the game-authored tangent
bit-for-bit (it affects normal-map shading at seams) is **unmeasured** here — naming it without
verifying it would repeat the exact mistake this section just found in the brief's own script,
so it is recorded as an open observation, not a candidate row.

### 2.2 What the game stores — the `_L` verdict

**Verdict: `_L<n>` is a CS2 zoning *level* — a distinct, independently-placed building — never
a level-of-detail alternate of one placement.** Both sources agree; there is nothing to report
as a conflict.

**Source.** The brief's guessed type name, `Game.Prefabs.ZoneBuildingData`, does not exist in
the decompiled assembly:

```
./tools/decomp/bin/Release/net8.0/decomp.exe list Game 'Game\.Prefabs\.(ZoneBuildingData|BuildingPrefab|ObjectSubObject|LodData|MeshData)'
```

— returns `BuildingPrefab`, `ObjectSubObjectInfo`, `ObjectSubObjects`, `MeshData`, no
`ZoneBuildingData`. Broadening the search to `Game\.Prefabs\..*Building.*` and
`Game\.Prefabs\..*Level.*` finds the real type:

```
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Prefabs.SpawnableBuildingData
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Prefabs.BuildingData
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Prefabs.MeshData
```

`Game.Prefabs.SpawnableBuildingData` (`$SCRATCH/audit/SpawnableBuildingData.cs`):

```csharp
public struct SpawnableBuildingData : IComponentData, IQueryTypeParameter, ISerializable
{
    public Entity m_ZonePrefab;
    public byte m_Level;
    …
}
```

— the game's own field is named `m_Level`, a `byte`, on a component attached to a **whole
prefab entity**. `Game.Prefabs.BuildingData` (`$SCRATCH/audit/BuildingData.cs`) carries `public
int2 m_LotSize` on the same prefab — the `<w>x<h>` filename suffix. Both are per-prefab data:
properties of a distinct, independently-spawnable building, not of a sub-mesh or a draw-time
LOD selector.

By contrast, `Game.Prefabs.MeshData` (`$SCRATCH/audit/MeshData.cs`) — the game's actual LOD
mechanism — carries `public byte m_MinLod`, `public byte m_ShadowLod`, `public float
m_LodBias`: per-*rendering* fields on the render prefab's own mesh data, and per
`docs/cs2-api-audit.md` §9 row 2 (cited, not re-derived), "LODs are separate render prefabs
behind `LodProperties`" — the renderer chooses one at draw time for *one placement*.
`grep -n 'm_LodBias\|m_MinLod\|lod\|Lod' "$SCRATCH/audit/MeshData.cs"` confirms these three are
`MeshData`'s only LOD-shaped fields, structurally unrelated to `SpawnableBuildingData.m_Level`.

The bundle's own contract already encodes true LOD with a different, non-conflicting
convention (`docs/bundle-schema.md:718-719`, §6, cited): *"LOD n > 0 is a scene node named
`<assetId>_LOD<n>`; LOD 0 is the root node with no suffix"* — LOD alternates of one prefab are
**nodes inside one `.glb`**, never separate top-level files. `_L<n>_<w>x<h>` names are ordinary
separate top-level `.glb` files (`assetId` = sanitised prefab name, `Systems/MeshRipSystem.cs`
`BuildAssetIds`/`GetPrefabName`), so by construction they cannot be the producer's own LOD
mechanism.

**The decisive data test**, run on this bundle:

```
python "$SCRATCH/audit/l_question.py" "$BUNDLE"
```

`measured`: **123 `_L` families are placed in this city; 99 of them (80.5%) place MORE THAN ONE
level simultaneously.** Example — `NA_CommercialLow02` at lot size `5x6`: `_L1` 29 placements,
`_L2` 11, `_L3` 4, `_L4` 1, `_L5` 52 — **97 placements of five differently-numbered variants of
the same base name and lot size, at 97 distinct world positions, in one city, at one instant.**
LODs are alternates of a single placement and are never co-resident. Five variants of the exact
same base+lot-size independently and simultaneously placed at dozens of locations can only be
distinct buildings — CS2's zoning-level growth stages (a lot regrows to a higher `_L<n>` prefab
as its land value rises; the earlier stage's prefab is a different, still-extant asset, not
discarded, and both go on being placed on different lots across the city).

File-count context, measured directly rather than assumed from the brief's quoted figure:

```
python -c "import re; from pathlib import Path; root = Path(r'$BUNDLE/meshes'); print(len([p for p in root.glob('*.glb') if re.search(r'_L\d+_\d+x\d+', p.stem)]))"
```

→ **2,065** of 5,634 filenames contain the `_L<n>_<w>x<h>` pattern in this bundle — close to,
but not exactly, the brief's quoted 2,080 (this bundle is one specific rip; the brief's number
came from a description written before this rip). The 15-file difference does not affect the
verdict, which rests on the source and co-residency evidence above, not on the count. Of those
2,065, **1,034** are the plain `<name>_L<n>_<w>x<h>.glb` form with no `__base`/`__stack` suffix
— each of those 1,034 is confirmed to be its own genuinely separate `.glb` file on disk, not a
node inside a shared container.

### 2.3 Information or representation?

**The variant/unreferenced question** (Step 4) — is every shipped mesh actually referenced?

```
python "$SCRATCH/audit/mesh_unreferenced.py" "$BUNDLE"
```

`measured`: 5,634 meshes on disk, 4,310 distinct `assetId`s placed by `instances.jsonl`,
**1,444 meshes (25.6%, 29.7 MiB) never appear as an `assetId` on any instance line.** Per this
task's own warning and `docs/bundle-schema.md` §4.1/§6.1 (read before writing this), that is
**not** the same as "unused" — the script classifies each of the 1,444 against the contract's
own documented resolution mechanisms rather than assuming dead weight:

| class | files | bytes | resolved how |
|---|---:|---:|---|
| `__base` skirts | 1,436 | 27.9 MiB | §4.1: *"No instance record references `__base` explicitly; its presence beside `<assetId>.glb` is the signal."* Never expected to appear as an `assetId`. |
| plain undivided `<assetId>` of a stackable prefab | 6 | 1.6 MiB | §4.1: *"The undivided `<assetId>` is also present for completeness, though every placed instance of a stackable prefab carries a stack in CS2 … instance records normally reference only the section files."* Explicitly documented as expected-unreferenced. Confirmed by name: `2LaneWoodenCoveredBridgePillar`, `ConstructionCrane01`, `Pier01Pillar01`, `Train_Vertical_Pillar`, `TrussArchBridge01NetPillar`, `Vertical_Pillar` — exactly the six prefabs that also have `__stack_*` files on disk. |
| `__stack_mid` | 2 | 0.2 MiB | §4.1's per-instance stack-section mechanism: `Pier01Pillar01__stack_mid` and `TrussArchBridge01NetPillar__stack_mid` have zero placements *in this specific city* — checked directly, every other `__stack_*` file for those two prefabs (`_first`/`_last`) **is** referenced (61 and 4 times respectively). No placed instance of either prefab was long enough to need a repeated middle section this rip. Data-dependent (a taller instance elsewhere would reference it), not a schema defect. |
| **total classified** | **1,444** | **29.7 MiB** | **100% resolved by a documented contract mechanism** |

**Zero genuinely-dead meshes found.** Every one of the 1,444 unreferenced-by-`assetId` files is
accounted for by a mechanism `docs/bundle-schema.md` §4.1 already documents. This closes Step
4 as a negative result — **no mesh-deletion candidate is proposed**, matching this task's
constraint that no mesh class may be proposed for deletion without proving it is unreferenced
by *both* the `assetId` join and the contract's other resolution paths.

**The `__base.glb` skirts** (constraint 4) — 1,436 files, **27.9 MiB, 1.6% of `meshes/`, 0.6%
of the whole 4,894,194,530-byte bundle.** Are they derivable from the parent mesh plus terrain,
rather than baked?

**Yes — validated by a prior pass, cited rather than re-derived.** `docs/cs2-api-audit.md` §27
(read in full) is a line-by-line comparison of `IO/BaseSkirtBuilder.cs` against the game's own
`BatchMeshHelpers.GenerateObjectMesh` → `AddBaseLines`/`AddBaseVertices`: gate (row 1), exclusion
pass (row 2), keep pass (row 3) and the band itself (row 5) are all **"match"**; the
distance-resolution traversal (row 4) is **"match (outcome-equivalent)"**; row 6 — the v=0 ring
being dropped to terrain — is a **documented deliberate deviation**, already the consumer rule
this bundle's contract states (§4.1: *"moves every `v = 0` vertex down to the terrain surface at
its world X/Z"*); row 7 is **benign** (a linear scan replacing the game's BVH, perf-only). So
the entire skirt geometry is a **pure, already-verified-faithful, deterministic function of**:
the parent mesh's own silhouette at its base level (already shipped — the parent `.glb`'s own
vertex positions and bounds), a fixed 1 m band algorithm (`IO/BaseSkirtBuilder.cs`, 366 lines,
proven to match source), the base material (a small string, already present as the skirt file's
own glTF material name — see the sample below), whether the instance rests on the ground
(`attrs.elevationFlags`, already shipped per instance, §4.1), and the terrain surface itself
(already shipped, `terrain/heightmap.png`, 16-bit grayscale, `docs/bundle-schema.md:912`). Sample skirt file confirms the material reference is
tiny and already join-key-shaped:

```
2LaneWoodenCoveredBridgePillar__base.glb: 3,136 bytes, 26 vertices, 48 indices
  materials: [{"name": "Default_Base_5bafa4f37f02ba699434b2189507eb8c", …}]
```

No byte in `__base.glb` encodes a fact absent from data the bundle already ships elsewhere plus
a fixed, already-ported-and-verified algorithm — this is the textbook deriving-instead-of-baking
case. The cost is real, though: a consumer must port `IO/BaseSkirtBuilder.cs`'s 366 lines
(silhouette edge-exclusion pass, keep pass, perimeter-distance chain resolution with loop-seam
handling), not just skip a file — unlike a free in-place re-encode, this is genuine consumer
engineering, mitigated by the fact that the reference implementation to port already exists and
is independently proven faithful (§27).

### 2.4 Smallest lossless encoding

**Quantization is not lossless — measured arithmetically, per constraint 3, with its error
bound in millimetres, per this bundle's own physical scale (not a unit-cube guess).**

```
python "$SCRATCH/audit/mesh_quantize.py" "$BUNDLE"
```

(Position → `i16` normalized over each mesh's own bounding box; `NORMAL`/`TANGENT` → `i8`;
`TEXCOORD_0` → `u16`; indices unchanged — the brief's own rule, `KHR_mesh_quantization`-shaped.
Deduped by `(file, accessor index)` for the same reason §2.1's census needed the fix.)
`arithmetic`, full population:

```
attribute bytes before:    1,474.5 MiB
attribute bytes after:       522.2 MiB  (35.4%)
worst-case position error over all meshes: 20.505 mm
```

Combined with the unchanged `INDICES` (230.4 MiB) and JSON/padding overhead (11.9 MiB, §2.1):
new total = 522.2 + 230.4 + 11.9 = **764.5 MiB**, versus the as-shipped 1,716.8 MiB — **952.3
MiB saved (55.5% of `meshes/`; 20.4% of the whole bundle)**, `arithmetic`. The 20.505 mm figure
is the brief's own formula (`extent / 65534`, the full quantization step over the worst
single mesh's largest bounding-box extent) — a conservative upper bound: a round-to-nearest
quantizer's true worst-case error is at most **half** a step (≤ 10.253 mm), but the stated
figure is what the formula as given computes, and is reported as-is rather than halved without
re-deriving the brief's own rule.

**Generic compressibility — this IS the MESH-1 candidate, not a proxy for a different one.**
`gltfpack`/Draco (a distinct, mesh-topology-aware technique) are checked and confirmed absent
(`where gltfpack` → not found, no install performed per this task's resolution) and are simply
untested — no number is claimed for them anywhere in this section. The candidate this
subsection actually measures is "compress the `.glb` bytes with zlib -9 or lzma," and that is
measured exactly as the front matter defines the term (`:20`, *"a sample was actually
re-encoded and extrapolated by a stated rule"*): 200 real files, actually compressed, with an
explicit bit-exact round-trip assertion, extrapolated by a stated linear rule — the same
sample-and-extrapolate shape §1 labels `measured` for TEX-1 through TEX-4. Labelling it
`arithmetic` in an earlier draft of this section conflated it with the untested gltfpack/Draco
candidate; that framing has been removed.

```
python -c "
import lzma, zlib, random
from pathlib import Path
root = Path(r'$BUNDLE/meshes')
files = sorted(root.glob('*.glb')); random.Random(0).shuffle(files)
sample = files[:200]
raw = b''.join(p.read_bytes() for p in sample)
z = zlib.compress(raw, 9); l = lzma.compress(raw)
assert zlib.decompress(z) == raw and lzma.decompress(l) == raw
per_file = sum(len(zlib.compress(p.read_bytes(), 9)) for p in sample)
print(len(raw), len(z), len(l), per_file)
"
```

```
sample 200 files: 60,003,752 bytes (57.2 MiB)
  zlib -9 (concatenated) : 9,053,409 bytes (15.09%), round-trip bit-exact
  lzma    (concatenated) : 4,727,024 bytes ( 7.88%), round-trip bit-exact
  zlib -9 (per-file, independent, no cross-file redundancy): 9,073,697 bytes (15.12%)
```

The concatenated and per-file zlib figures differ by only 0.03 percentage points — **almost all
of the win is within-file redundancy in the raw f32 vertex data, not cross-file redundancy
between different meshes.** A single combined container buys essentially nothing over
compressing each `.glb` independently for this domain (contrast §1's texture case, where the
brief's own JSON-overhead framing expected a one-container win — meshes don't have that
shape). Extrapolation rule: `total meshes/ bytes (1,800,230,540) × (compressed sample bytes ÷
60,003,752)`:

- **zlib -9: 259.0 MiB (15.1%), saved 1,457.8 MiB — 84.9% of `meshes/`, 31.2% of the whole
  bundle.**
- **lzma: 135.3 MiB (7.9%), saved 1,581.5 MiB — 92.1% of `meshes/`, 33.9% of the whole
  bundle.**

This is lossless by construction (DEFLATE/LZMA decompression reproduces the input exactly; the
assertion above confirms it on the sample rather than taking that as given), `measured`, and is
the single largest number measured in this section — far larger than the lossy quantization
candidate above. **`meshes/`'s `.glb` files currently ship with zero compression applied to their binary
buffers** (`IO/GltfWriter.cs` writes the BIN chunk raw), unlike `textures/`'s PNGs, which
already run through `Deflater` (§1). **Not measured jointly with quantization, and the two must
not be summed**: quantized (already denser, less redundant) data compresses differently than the
raw f32 form measured here, and a joint number would need an actual quantized re-encode, not an
arithmetic byte estimate — out of scope for Phase A.

### Rip time

`meshes/` is written inside §8's row 2 (Meshes, 1177.106 s bracket) — cited, not re-derived; the
same bracket §1 already established is dominated by one serial writer thread doing mesh
`.glb` serialisation *and* texture PNG encoding together (§1's Rip time subsection, not
repeated here). The instrumented sub-step within that row, "Mesh writing: `Wrote 4198 .glb,
skipped 120, failed 0, in 1178.3 s`" (§8), is the direct mesh-write figure — `instrumented`, per
§8's own labelling, agreeing with the 1177.106 s bracket to within 1.2 s. Whether compressing
`.glb` output at write time (MESH-1 below) would add to or shrink that time is `unmeasured` — it
would need a C#-side timed re-rip, the same caveat §1 records for texture re-encoding, and is
not estimated from this section's Python zlib/lzma timings for the same cross-language reason.

**Note on the source log**: the live `Carto.log` at the path `$LOG` resolves to has since been
overwritten by a later, unrelated, much shorter mod-load session (observed directly: the file
is now 1,182 bytes, timestamped after the rip that produced this bundle) — the rip log's
content this section and §0/§1/§8 rely on is only available from the cached excerpts already
saved under `$SCRATCH/audit/` by earlier tasks (`full-log-repr.txt`, `log-timeline.txt`), not
from a fresh `grep` against `$LOG` itself. This task did not need to re-query the live log for
anything beyond what §8 already records, so it caused no gap here — flagged for whichever later
task next needs `$LOG` directly.

### Candidate rows

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| MESH-1 | Compress `.glb` binary buffers at rest (zlib -9 or lzma; currently unlike `textures/`'s PNGs, mesh BIN chunks are written raw) | measured (200-file sample, actually compressed, bit-exact round-trip asserted, extrapolated by a stated linear rule), zlib **1,457.8 MiB (84.9% of `meshes/`, 31.2% of bundle)**; lzma **1,581.5 MiB (92.1%, 33.9%)** | unmeasured (needs a C#-side timed re-rip; compression itself costs CPU at write time) | one decompress call before parsing — zlib/lzma decoders are standard-library or trivial to add in every mainstream consumer language, but this is not literally free like an in-place re-encode | medium — algorithm risk is near-zero (ubiquitous, well-tested codecs, bit-exact by construction, verified on-sample), but it is a container-format change, not an in-place one | YES, bit-exact round-trip assertion passed on the 200-file sample | **new layer**: a compressed at-rest/transport encoding of the unchanged `.glb` bytes (e.g. per-file `.glb.gz`, or one archive per directory using the already-bundled `ICSharpCode.SharpZipLib`) — needs a `compression` field amendment under §12.1; **retires**: nothing, the canonical uncompressed `.glb` bytes are recovered exactly on decompress |
| MESH-3 | Derive `__base.glb` skirts at consumer load time instead of baking them (§2.3): port `IO/BaseSkirtBuilder.cs`'s algorithm, given the parent mesh + `attrs.elevationFlags` + `terrain/heightmap.png`, all already shipped | measured, **27.9 MiB (1.6% of `meshes/`, 0.6% of bundle)** | unmeasured | real: port ~366 lines of silhouette-scan geometry (edge exclusion, keep pass, perimeter-distance chain with loop-seam handling) — nontrivial, but the reference implementation is already proven faithful to the game (§27), lowering the porting risk | medium — the port must reproduce the game's algorithm exactly to avoid visible gaps/seams; a proven-faithful reference exists to copy | YES — every fact in `__base.glb` is already derivable from data the bundle ships elsewhere plus an already-verified deterministic algorithm | **removes** a file class rather than adding one; the tiny per-prefab base-material reference (currently the skirt file's own glTF material name, e.g. `Default_Base_5bafa4f37…`) would need to move into `materials.json`/`bundle.json` for prefabs whose skirt file is dropped — negligible bytes, a real (small) schema touch |
| MESH-4 | Delete unreferenced/dead meshes | 0 — none found (§2.3: all 1,444 unreferenced-by-`assetId` files resolve to a documented contract mechanism) | n/a | n/a | n/a | n/a (closed, negative result) | not applicable — no files to touch, nothing to retire |

**Lossy candidates** (named and measured per constraint 6's "investigate lossless first," but
not recommended — MESH-1 above already gives a far larger lossless win from the same domain):

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | error bound | additive layer |
|---|---|---|---|---|---|---|---|---|
| MESH-2 | `KHR_mesh_quantization`-style re-encode: `POSITION` → normalized `i16`, `NORMAL`/`TANGENT` → `i8`, `TEXCOORD_0` → `u16`, indices unchanged | arithmetic, **952.3 MiB (55.5% of `meshes/`, 20.4% of bundle)** | unmeasured | consumer must dequantize per the `KHR_mesh_quantization` extension (or an equivalent scale/offset it reads from `accessor.min`/`max`) — every existing consumer that doesn't apply this extension silently misreads the geometry | high — not a drop-in re-encode; shipping it in place would break every consumer that doesn't already handle the extension, so it can only ship as either a genuinely separate opt-in file (doubling storage until adopted) or a breaking major-version schema change | **NO — ≤ 20.505 mm** | brief's own formula: the full quantization step over the worst single mesh's bounding-box extent; a round-to-nearest quantizer's true worst case is at most half that, ≤ 10.253 mm, not re-derived here | either a **new layer** (a parallel quantized geometry form, opt-in via `KHR_mesh_quantization`, current f32 form untouched) or a **breaking replacement** requiring a major schema version bump (§12) — not designed here, named for §9 to weigh against MESH-1's much larger, genuinely lossless win from the same 1.7 GB |

## 3. Networks

`networks/` is 424,469,220 bytes (404.8 MiB, 8.7% of the 4,894,194,530-byte total, §0), 7,248
files — third-largest share, but an order of magnitude behind `textures/` and `meshes/`. §0
already reconciled the record counts this section reuses without re-deriving: `networks.jsonl`
holds 8,259 segment records, of which 7,248 are **mesh form** (`assetId":"seg_<id>"`, a matching
`networks/seg_<id>.glb`) and 1,011 are **centerline form** (no baked mesh). This section does not
re-open that reconciliation.

### 3.1 What we ship

Command:

```
python "$SCRATCH/audit/net_dedup.py" "$BUNDLE"
```

(`net_dedup.py`: counts `networks.jsonl` records by `kind`/`assetId` shape, matching §0's split;
tallies `attrs.asset` over the mesh-form records; sums `networks/*.glb` bytes.) `measured`, full
population (8,259 of 8,259 records, 7,248 of 7,248 files):

| | |
|---|---:|
| total records | 8,259 |
| mesh-form segment records | 7,248 |
| baked meshes on disk | 7,248, 424,469,220 B (404.8 MiB) — matches §0's census exactly |
| distinct `attrs.asset` values among mesh-form segments | **45** |
| mean baked mesh | 57.2 KiB |
| dedup ratio (mesh-form records ÷ distinct prefabs) | **161.07×** |

The twelve most common prefabs account for 5,955 of the 7,248 mesh-form segments (82.2%): Small
Road 1,955, Alley 1,915, Medium Road 776, Highway Oneway – 2 lanes 426, Gravel Road 387, Highway
Oneway – 3 lanes 210, Double Train Track 209, Pavement Path 135, Pedestrian Street 105, Twoway
Train Track 100, Highway Oneway – 1 lane 100, Small Road Asymmetric 90 (full 45-row breakdown in
`$SCRATCH/audit/net_dedup.txt`). **This dedup ratio — 7,248 baked, unique-per-segment meshes
standing for 45 distinct road cross-sections — is itself the size of the win question 4 answers
below; it is not a separate finding to re-derive, per the brief's Step 1.**

`networks/` meshes are **not** deduped by design (`docs/bundle-schema.md:538`, cited: "every
segment is bent onto its own curve, so each `.glb` is unique"), unlike `meshes/`'s one-file-per-
prefab convention (§2, `docs/bundle-schema.md` §6). That is the entire shape of this domain's
question 3 below.

### 3.2 What the game stores

**Cited, not re-derived, per this task's brief.** `docs/cs2-api-audit.md` §8 ("Network assembly
versus the game, rule by rule") and §21 ("The GameMesh port versus the generator, line by line")
already establish, rule by rule against the decompiled game, that **CS2 keeps no per-segment road
mesh at all**: a *composition* names a reusable cross-section (a list of shared pieces — carriageway,
kerb, sidewalk, median — each an ordinary prefab), and the renderer bends that composition onto a
segment's own curve in the vertex shader from eight matrices that are themselves nothing but the
segment's `EdgeGeometry`/`EdgeNodeGeometry` boundary bézier control points
(`docs/bundle-schema.md:418-425`, cited). `Systems/NetworkRipSystem.GameMesh.cs` is a full CPU-side
port of that pipeline (`CalculateEdgeParameters`/`CalculateNodeParameters`/
`GenerateCompositionMesh`/vertex sync), and §8's 17-row and §21's 12-row rule tables record it as
`match` or `divergence — fixed` on every rule checked, not `unmeasured` or `approximated` except
for two shader-only refinements (roundabout blend feathering, §8 row 13; normals, §8 row 14) that
do not bear on this section's sizing question. **What the bundle currently ships (§3.1) is
therefore the baked *output* of a representation the game itself never bakes**, and that our own
producer already builds internally before discarding it — confirmed directly in our own source
below, per the brief's Step 2.

**The cache-then-discard claim, anchored in code:**

```
grep -n 'cache\|Cache\|Dispose\|composition' Systems/NetworkRipSystem.GameMesh.cs | head -40
```

What is cached, and for how long: `m_PieceGeometry` (`Systems/NetworkRipSystem.GameMesh.cs:72`,
`private readonly Dictionary<Entity, PieceGeometry> m_PieceGeometry = new();`) holds one entry per
unique cross-section *piece* — the raw `MeshVertex`/`MeshUV0`/`MeshIndex`/`MeshMaterial` ECS
buffers read once (`GetPieceGeometry`, `:598-662`; the memo check is `:601`, `if
(m_PieceGeometry.TryGetValue(piece, out PieceGeometry cached)) return cached;`; the fill is `:660`,
`m_PieceGeometry[piece] = result;`, unconditional — it runs for every piece `GetPieceGeometry` is
ever asked about, including one whose buffers are missing or empty, where `result` stays `null`).
It is cleared exactly once per rip (`Systems/NetworkRipSystem.cs:437`, `m_PieceGeometry.Clear();`,
inside `BeginRip`, commented "the cache is per-rip") and never again — so it persists across all
8,259 segments.

The rip's own report line, **`Distinct cross-section pieces read: 160`**
(`$SCRATCH/audit/full-log-repr.txt:213`), is printed from `run.PieceCache.Count`
(`Systems/NetworkRipSystem.cs:860`) — a *different* `Dictionary<Entity, MeshAsset>`
(declared `Systems/NetworkRipSystem.cs:256`) that memoizes material-id resolution, not
`m_PieceGeometry` itself. Both dictionaries are populated from the same call — `GetPieceGeometry`
fills `PieceCache` at `:638-643` (`if (!cache.TryGetValue(piece, out MeshAsset read)) { … cache[piece]
= read; }`) — but only inside the branch gated on `vertexBuffer.Length > 0 && indexBuffer.Length > 0
&& materialBuffer.Length > 0` (`:605-614`), whereas `m_PieceGeometry[piece] = result` at `:660` runs
unconditionally. So `PieceCache.Count` is a **lower bound** on `m_PieceGeometry.Count`, equal only
if every piece `GetPieceGeometry` saw in this rip had valid, non-empty mesh buffers — a condition
not separately checked here. 160 is therefore the measured count of pieces that contributed a
usable material read, and a conservative (possibly strict) lower bound on the true number of
distinct pieces `m_PieceGeometry` cached; either way it stands in for what would otherwise be at
least 7,248 ECS buffer reads (one per mesh-form segment, more once shared pieces across
compositions are counted individually).

What is **not** cached: the *assembled, bent* composition mesh — the thing that would actually let
one composition serve many segments. `AssembleNetwork` (`Systems/NetworkRipSystem.cs:1427-1485`)
allocates a fresh `List<float3> vertices = new();` (`:1432`) for every segment entity and calls
`BuildNodeSide`/`BuildGameComposition` (`:1450`, `:1462`, `:1471` →
`Systems/NetworkRipSystem.GameMesh.cs:251-420`) to loop over that composition's pieces from scratch
— vertex filter/remap, front/back cut, V-density, Bernstein bend — for that one segment's own curve,
every time. `Finish` (`:1672`) packages the result into a `MeshAsset`, which is handed straight to
the writer (`:646-656`, `run.Queue.Add(new NetworkItem { Mesh = assembled });`) and appears nowhere
keyed by composition — there is no dictionary keyed by *composition* entity anywhere in either
file. (`PieceCache`, a `Dictionary<Entity, MeshAsset>`, and `m_PieceGeometry`, a
`Dictionary<Entity, PieceGeometry>`, both exist, per the paragraph above — but both are keyed by
*piece* entity, for the material-resolution and raw-geometry purposes already described, not by
composition entity for assembled geometry.) Concretely: the
"Small Road" composition, used by 1,955 different segments (§3.1), has its full per-piece loop and
per-vertex bend recomputed 1,955 times over — the only thing that does *not* get re-read 1,955
times is each piece's raw ECS geometry, via `m_PieceGeometry`.

### 3.3 Information or representation?

**Pure representation.** §3.2's citations already establish that the 424.5 MB in `networks/` is a
deterministic, already-verified-faithful function of two things the bundle ships in far fewer
bytes: which of 45 compositions a segment uses, and that segment's own curve (already implicit in
`EdgeGeometry`, the very data `Systems/NetworkRipSystem.GameMesh.cs` reads to build the baked mesh
in the first place). No new fact is encoded by baking — every triangle in `seg_1794912.glb` is
reconstructible, bit-for-bit modulo the two shader-only approximations §3.2 names, from a
composition id plus a handful of curve control points. This is the textbook case the front matter's
question 3 is asking about, and it is a stronger case than any of §1/§2's representation findings:
those saved bytes by re-encoding the *same* per-file data more efficiently; this domain ships
7,248 independent files that are each a full re-derivation of the *same 45-item* catalogue, over
and over, onto a curve the bundle already has.

There is no orphan/dedup question to run here the way §1.3/§2.3 did: §0 already showed
`networks/`'s 7,248 files match `"assetId":"seg_` 1:1, exactly (§0, "those three are the same
measurement taken three ways and agree exactly") — nothing is shipped unreferenced, and nothing
here is duplicate-by-content in the way §1.3 checked textures (every `.glb` is bent onto its own
unique curve, so byte-identical duplicates across different segments would not be expected and
were not checked separately from that reconciliation).

### 3.4 Smallest lossless encoding

**The composition layer is the win, sized against the shipped precedent for exactly this shape of
representation** — `lanemeshes.jsonl` + `lanemeshes/` (§5B v2 of `docs/bundle-schema.md`, cited):
28,063,057 B of per-lane matrix/curve/reference records plus 2,738,524 B of
deduped straight-part geometry, covering all 119,302 drawn lanes (of which ~83k, the `lane`
category, are markings) from just 71 unique source meshes. That layer already proved the shape
works and already shipped it — §3's job, per the brief, is to size the same move for networks,
not to re-argue that it works.

Per-unit costs, measured directly from that shipped precedent (§0's byte-exact census, divided by
the corresponding record/file counts):

```
lanemeshes.jsonl:  28,063,057 B ÷ 119,302 records = 235.23 B/record   (wc -l "$BUNDLE/lanemeshes.jsonl")
lanemeshes/:         2,738,524 B ÷ 71 files        = 38,570.8 B/file
```

Applied to this domain's own measured counts (§3.1: 45 distinct compositions as the geometry-count
ceiling per the brief's own framing — "the number of distinct prefabs above is the ceiling on the
geometry count"; 7,248 mesh-form segments as the record count):

```
geometry layer:  45 compositions      × 38,570.8 B/file   = 1,735,684 B  (1.66 MiB)
record layer:    7,248 segments       × 235.23 B/record   = 1,704,926 B  (1.63 MiB)
projected composition layer total:                           3,440,610 B  (3.28 MiB)

saved  = 424,469,220 − 3,440,610 = 421,028,610 B (401.5 MiB)
       = 99.19% of networks/, 8.60% of the whole 4,894,194,530-byte bundle
```

`arithmetic` — computed from two real, measured per-unit costs (not a fresh re-encode of network
geometry: no composition-layer file has actually been built or round-trip-tested here, unlike
§1/§2's `measured` rows). The projection assumes a network composition-layer record is comparable
in richness to a `lanemeshes.jsonl` record — both need to carry a curve descriptor (a
`lanemeshes` record ships one cubic bézier, 4×3 floats; a network segment record would need the
edge's left/right boundary curves plus each node-side flare, a small integer multiple of that,
§3.2) plus a handful of scalar/reference fields most of which (`category`, `form`, `asset`, …)
`networks.jsonl`'s mesh-form records already carry today and would not need to duplicate. The
true per-record cost could run a few times higher than 235 B without changing the order of
magnitude of the result — even at 5× (1,176 B/record, 8.13 MiB record layer, 9.78 MiB total
projected layer with the geometry term unchanged) the saving is still 395.0 MiB, 97.58% of
`networks/`. **45 is a ceiling, not an exact composition-entity count**
(the brief's own qualification, repeated here rather than silently dropped): a single named
prefab like "Small Road" can in principle resolve to more than one distinct `NetCompositionData`
entity — an edge composition plus separate node-side compositions, and a roundabout composition is
a structurally different mapping again (§8 row 13, cited) — so the true geometry count the layer
would need to store could exceed 45. Even a generous 4× correction (180 geometries) only grows the
geometry-layer term to 6.62 MiB, leaving the total projected layer at 8.25 MiB and the saving at
396.6 MiB (97.96% of `networks/`) — the result is insensitive to this uncertainty.

**Consumer cost — concrete, not hand-waved (Step 3).** Today a consumer opens one `.glb` per
segment and gets triangles for free. Under a composition layer, a consumer must instead: (1) load
the 45 shared composition geometries once (§3.4's ceiling), each already fully assembled in composition-local
parameter space (the pre-bend cross-section CS2 itself never materialises as a mesh, per §3.2); (2)
for every segment, resolve its composition id(s) and read its curve (edge boundary béziers plus
start/end node flare geometry) from the new per-segment record; (3) run the same CPU-side bend
`Systems/NetworkRipSystem.GameMesh.cs` already implements — the Bernstein blend of up to eight
curve matrices, the vertex-sync remap, the front/back cut gates, the median/roundabout node
mapping (§3.2, §8's 17 rules, §21's 12 refinements) — to produce world-space triangles; (4) merge
per-material primitives into a renderable mesh, in-memory or cached to disk on first load. This is
categorically more consumer engineering than any other candidate in this document: `lanemeshes/`'s
bend algorithm is normatively documented in ~30 lines of pseudocode in
`docs/bundle-schema.md` §5B and already has a reference port in
`tools/blender_import_bundle.py`; MESH-3's `__base.glb` skirt algorithm is 366 lines with one
documented deviation (§2.3, §27). A network composition-layer consumer would need to port most of
`Systems/NetworkRipSystem.GameMesh.cs` itself — the file this task's own brief describes as
"the game's own composition-mesh generation, ported" — which is the single largest and most
rule-dense algorithm this whole codebase has needed to reproduce faithfully (§8/§21's combined 29
rule-verification rows). **Per this audit's constraint 5, a composition layer ships *alongside*
`networks/`, not instead of it, until an A/B gate proves the consumer's port matches the baked
geometry — so the transitional bundle is 424,469,220 + 3,440,610 = 427,909,830 B (408.1 MiB, 8.74%
of the current bundle total), larger than today's `networks/` alone, not smaller, for as long as
both ship together.** That is a real, first-class cost of this candidate, not a footnote.

**Import time.** §8 records the networks phase's own instrumented sub-step: "Network segment mesh
baking: `Wrote 8259 segment(s) to networks.jsonl in 65.8 s`" (§8, cited, not re-derived) — that is
rip-time cost for the producer to *write* 7,248 files, not consumer import time, but the same
7,248-file-opens-versus-one-composition-set shape applies on the read side: a consumer importing
this bundle today opens 7,248 separate `.glb` files for `networks/` alone; under the composition
layer it would open the 45 geometry files (§3.1's ceiling) once, plus one JSON record per segment
already being read from `networks.jsonl` regardless. Whether that changes measured consumer import
wall-clock time is `unmeasured` — no consumer-side composition-layer importer exists to time, the
same caveat §1/§2 record for every rip-time question this phase of the audit cannot answer without
building the candidate first.

### Candidate rows

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| NET-1 | Composition layer: ship each segment's composition id(s) + curve instead of baking a per-segment `.glb`, mirroring the already-shipped `lanemeshes.jsonl`/`lanemeshes/` v2 precedent (§5B) | arithmetic, projected from the precedent's measured per-unit costs, **401.5 MiB (99.19% of `networks/`, 8.60% of the whole bundle)** — see method above; even under a joint 5× record-cost / 4× geometry-count correction the saving is still 390.1 MiB (96.36% of `networks/`) | unmeasured (rip-time cost moves from writing 7,248 `.glb` files to writing ~45 geometry files + one record per segment; not timed, no candidate built) | real and large: consumer must port most of `Systems/NetworkRipSystem.GameMesh.cs` (§8's 17 rules + §21's 12 refinements) — the single largest algorithm this audit has needed to size a port for | high — correctness-critical geometry algorithm (junction interiors, vertex sync, roundabout mapping) with two already-known shader-only approximations (§3.2); a faithful port is achievable (the reference exists and is proven, §8/§21) but is not a small lift like MESH-3 | YES — every rule checked in §8/§21 is `match` or `fixed`; the two shader-only gaps (roundabout blend feathering, rebuilt normals) are pre-existing approximations already in today's baked mesh too, not new loss this candidate introduces | **new layer**: composition geometry set + per-segment matrix/curve records, replacing `networks/*.glb`; **retires**: `networks/` and the per-segment mesh-form baking path, but only after an A/B gate (constraint 5) — until then it ships *alongside* `networks/`, making the transitional bundle 408.1 MiB for this domain (8.74% of the bundle) instead of today's 404.8 MiB (8.60%) |

**Lossy candidates:** none proposed for this domain. A lossy geometry reduction (e.g. `MESH-2`-style
quantization of the baked `.glb` vertex data) is conceptually available but not measured here:
NET-1's lossless win (401.5 MiB) is already two orders of magnitude larger than anything a
format-level tweak to the remaining bytes could add, so investigating a lossy variant would not be
a use of this task's time, and constraint 6 requires lossless to be investigated first in any case
— which NET-1 already does, exhaustively, for this domain.

### 3.5 Compression at rest — measured for §9's COMP-1 scope

**Added 2026-08-08, after §9's first draft, because this was the one domain where the
compression-at-rest candidate class had not been measured.** `networks/` is 404.8 MiB of `.glb`
whose BIN chunks are written raw — the same file type in the same condition as `meshes/` (§2.4),
which is COMP-1's largest component. Leaving it unmeasured understated COMP-1's scope and left
NET-1's decision-relevant number (its saving *net of* compression) unknown. The same script §2.4
used for `meshes/`, unchanged except for the directory and the totals:

```
python -c "
import lzma, zlib, random, os
from pathlib import Path
root = Path(os.environ['BUNDLE']) / 'networks'
files = sorted(root.glob('*.glb')); random.Random(0).shuffle(files)
sample = files[:200]
raw = b''.join(p.read_bytes() for p in sample)
z = zlib.compress(raw, 9); l = lzma.compress(raw)
assert zlib.decompress(z) == raw and lzma.decompress(l) == raw
per_file = sum(len(zlib.compress(p.read_bytes(), 9)) for p in sample)
print(len(raw), len(z), len(l), per_file)
"
```

```
7,248 files on disk; sample 200 files: 10,190,604 bytes (9.72 MiB, 2.40% of the directory)
  zlib -9 (concatenated) : 4,186,360 bytes (41.08%), round-trip bit-exact
  lzma    (concatenated) : 2,628,828 bytes (25.80%), round-trip bit-exact
  zlib -9 (per-file, independent, no cross-file redundancy): 4,253,012 bytes (41.73%)
```

Extrapolation rule, stated explicitly and identical to §2.4's: `total networks/ bytes
(424,469,220) × (compressed sample bytes ÷ 10,190,604)`. `measured` (a sample was actually
re-encoded, with a bit-exact round-trip assertion, and extrapolated by a stated rule — the
front matter's own definition, `:20`):

- **`lzma`: 104.4 MiB (25.80%), saved 300.4 MiB — 74.20% of `networks/`, 6.44% of the whole
  bundle.**
- **`zlib -9`: 166.3 MiB (41.08%), saved 238.5 MiB — 58.92% of `networks/`, 5.11% of the whole
  bundle.**

Two differences from `meshes/` are worth stating rather than assuming the domains behave alike.
First, baked segment meshes compress **less** well than prefab meshes — 25.80% versus `meshes/`'s
7.88% under the same `lzma` call — which is what a directory of 7,248 individually bent,
non-repeating curves should do next to 5,634 prefab meshes with heavy within-file structure.
Second, the concatenated and per-file `zlib` figures differ by 0.65 percentage points here
against `meshes/`'s 0.03 — still small, so a single combined container again buys almost nothing
over compressing each file independently, but the cross-file redundancy is measurably larger in
this domain than in `meshes/`.

**Consequence for NET-1, computed here rather than left to §9.** NET-1 replaces 424,469,220 B of
baked `.glb` with a projected 3,440,610 B composition layer (§3.4). Against a *compressed*
`networks/` (109,498,571 B projected above) its marginal saving is 109,498,571 − 3,440,610 =
**106,057,961 B (101.1 MiB, 2.17% of the whole bundle)**, `arithmetic` — a measured compression
projection minus §3.4's own arithmetic layer projection. That is **3.97× smaller than NET-1's
401.5 MiB standalone figure**, and it is the number that decides whether the 664-line consumer
port is worth building.

**Lossless candidates (addition to this section's candidate block above — no row there is
changed):**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| NET-2 | Compress `networks/*.glb` binary buffers at rest (`lzma` or `zlib -9`), exactly as MESH-1 proposes for `meshes/` — the same technique on the same file type in the same raw-BIN condition | measured (200-file sample, actually compressed, bit-exact round-trip asserted, stated linear extrapolation), `lzma` **300.4 MiB (74.20% of `networks/`, 6.44% of bundle)**; `zlib -9` **238.5 MiB (58.92%, 5.11%)** | unmeasured (adds encode CPU inside §8's row 3, 23.490% of the rip) | one decompress call before parsing, identical to MESH-1/INST-1/LANE-1 | low — ubiquitous codecs, bit-exact by construction, verified on the sample | YES, round-trip asserted on the sample | **new layer**: compressed at-rest encoding of the unchanged `seg_<id>.glb` bytes, `compression` field amendment under §12.1, the same shape as MESH-1; **retires**: nothing — the canonical bytes are recovered exactly. **NET-1 and NET-2 are alternatives on the same 404.8 MiB and must not be summed**: NET-2 is the fourth component of §9's COMP-1 policy, and NET-1's saving *after* NET-2 is the 101.1 MiB computed above, not 401.5 MiB |

### The pier question

**Left `unmeasured`, per the brief's instruction not to guess — but with three specific causes
ruled out by direct measurement, not just asserted.** §0 already ruled out category, form, asset
name and segment length as the driver of `LeisurePier01Small01`'s 11-meshed/41-not split. This
task checked three more candidates directly:

**1. `attrs.elevation` presence, and 2. entity-id clustering — both ruled out.**

```
python "$SCRATCH/audit/net_pier.py" "$BUNDLE"
```

(`net_pier.py`: splits `LeisurePier01Small01` segments into meshed/mesh-less by the same
`assetId`-prefix test as `net_dedup.py`, then reports `attrs.elevation` presence and an `id`-value
cluster split at 1,700,000.) `measured`, full population (all 52 `LeisurePier01Small01` segments):

```
meshed 11, unmeshed 41
MESHED:   n=11, has elevation field: 10/11    id<1.7M: 1,  id>=1.7M: 10
UNMESHED: n=41, has elevation field: 33/41    id<1.7M: 8,  id>=1.7M: 33
```

Both groups are overwhelmingly elevation-tagged (10/11 vs 33/41) — presence of the field does not
separate them. The 52 segments' `id` values do fall into two numeric neighbourhoods suggestive of
two physically distinct piers in the city, not one, but both clusters mix meshed and mesh-less
segments in almost the same ~1:10 and ~1:4 ratio — the two-piers hypothesis may well be true, but
it does not predict the mesh/no-mesh split either.

**3. The "no world-view layer" composition skip — ruled out by the rip's own report.**
`BuildGameComposition` (`Systems/NetworkRipSystem.GameMesh.cs:292-301`) skips a composition and
increments `_pipelineCompositions` when its mesh's `NetCompositionMeshData.m_DefaultLayers` names
no world-view bit (Pipeline/SubPipeline/Waterway-only compositions — underground utility runs).
`Systems/NetworkRipSystem.cs:895-896` prints "Skipped N composition part(s) with no world-view
layer" **only when that counter is nonzero**; it does not appear anywhere in
`$SCRATCH/audit/full-log-repr.txt` (`grep -c 'no world-view layer'` → the only hit is the unrelated
lane-catalogue exclusion line, §3.4/§5B's separate mechanism). `measured`: this specific gate fired
**zero times in the whole rip** — it cannot be the pier cause.

**What remains genuinely unresolved.** `AssembleNetwork` (`Systems/NetworkRipSystem.cs:1427-1485`)
falls back to centerline form whenever its three geometry contributions — start-node flare
(`:1445-1453`), edge run (`:1455-1464`), end-node flare (`:1466-1474`) — all produce zero
vertices, which can happen per-instance if a segment's resolved `Composition.m_Edge`/`m_StartNode`/
`m_EndNode` entity is `Entity.Null`, or that entity's mesh lacks a `NetCompositionPiece` buffer, or
either node's flare/edge run length is below the game's own 0.05 m/0.1 m draw gates (§3.2, cited
from §8 row 9). All of these test the **specific composition entity CS2's simulation resolved for
that instance** — a per-instance runtime assignment, not a static property of the
`LeisurePier01Small01` prefab (a fixed prefab has one authored composition table; if instance
resolution were uniform, all 52 placements would go the same way, and they do not). None of these
resolved entity ids, buffer states, or authored flare/run lengths is exposed in `networks.jsonl`'s
own `attrs`, and no rip-report counter (checked: `_pipelineCompositions`, `SkippedNoWidth`,
`SkippedDegenerate` — none fire differently for these two groups per the log) distinguishes them
either. Settling this needs either new rip-time instrumentation logging which specific gate
returned zero vertices per segment (a production-code change, out of scope for Phase A per this
task's constraints) or a live in-game ECS inspection session (unavailable offline). Recorded here
as `unmeasured`, for whichever later task next touches piers or per-segment composition
resolution.

## 4. `instances.jsonl`

`instances.jsonl` is 324,202,611 bytes (309.2 MiB, 6.6% of the 4,894,194,530-byte total, §0),
1 file, 1,283,183 records — the fourth-largest share. Unlike §1–§3 there is no format-choice or
semantics question to open: every field a record carries is already documented and required or
optional exactly as shipped (`docs/bundle-schema.md` §4, §4.1, §4.2, §4.2a, read in full for
this section). This is the purest representation-versus-information case in the bundle — the
question is encoding, not what to keep.

### 4.1 What we ship

Command (brief's own script, run first, exactly as given):

```
python "$SCRATCH/audit/instances_census.py" "$BUNDLE"
```

**This failed the section's own pass criterion**, the same way §2.1's brief script did. The
per-field shares it prints sum to only 249.8 MiB of the file's 309.2 MiB (80.9%), not "~100%".
Root cause: the script isolates each field's value bytes by building a one-key mini-object
(`json.dumps({key: value})`) and subtracting `len(key) + 4`, which leaves `len(value) + 1` — one
stray byte per field instance, not zero — and, more importantly, the subtraction discards the
*real* per-record structure (`"key":value,` costs `len(key) + 4` bytes in the actual line, not
the 0 the subtraction assumes) entirely. The missing bytes are real JSON structural overhead
(key names, quotes, colons, commas, the record's own braces, the line's newline), not a
measurement gap. Measured directly instead (`$SCRATCH/audit/instances_fields_exact.py`, exact
per-field value bytes via `json.dumps(value)` with no subtraction, plus the overhead computed as
`raw_total − Σ value_bytes` so the table sums to exactly 100% by construction):

| field | bytes | MiB | % of file |
|---|---:|---:|---:|
| `attrs` | 137,356,398 | 130.99 | 42.37% |
| `rot` | 38,485,462 | 36.70 | 11.87% |
| `pos` | 34,773,868 | 33.16 | 10.73% |
| `assetId` | 22,677,168 | 21.63 | 6.99% |
| `id` | 8,390,653 | 8.00 | 2.59% |
| `kind` | 7,753,161 | 7.39 | 2.39% |
| `parent` | 4,134,125 | 3.94 | 1.28% |
| `scale` | 104,341 | 0.10 | 0.03% |
| *(JSON structural overhead)* | 70,527,435 | 67.26 | 21.75% |
| **TOTAL** | **324,202,611** | **309.18** | **100.00%** |

`measured`, full population (1,283,183 of 1,283,183 records). `4,310` distinct `assetId`s, `4`
kinds present (`tree` 697,163, `prop` 558,645, `decal` 18,479, `building` 8,896) — the
`4,310`-distinct-`assetId` figure matches §2.3's own count exactly ("4,310 distinct `assetId`s
placed by `instances.jsonl`"), the same measurement taken from two different domains' scripts.

`attrs` breaks down further (`$SCRATCH/audit/instances_attrs_exact.py`, same exact-value method,
run on the `attrs` object's own contents so its rows are directly comparable to the table
above rather than the brief's own per-attr method, which counts each attr's full
`{"key":value}` mini-object and is not directly additive with the top-level table):

| attrs key | bytes | MiB | % of file | % of `attrs` |
|---|---:|---:|---:|---:|
| `colorSet` | 94,447,057 | 90.07 | 29.13% | 68.76% |
| `elevation` | 2,284,166 | 2.18 | 0.70% | 1.66% |
| `treeState` | 2,001,411 | 1.91 | 0.62% | 1.46% |
| `growthScale` | 1,676,065 | 1.60 | 0.52% | 1.22% |
| `growth` | 1,463,194 | 1.40 | 0.45% | 1.07% |
| `decalLayers` | 572,849 | 0.55 | 0.18% | 0.42% |
| `elevationFlags` | 375,887 | 0.36 | 0.12% | 0.27% |
| `unresolved` | 98,920 | 0.09 | 0.03% | 0.07% |
| `state` | 71,168 | 0.07 | 0.02% | 0.05% |
| `decalHeight` | 55,460 | 0.05 | 0.02% | 0.04% |
| `colorCustom` | 956 | 0.00 | 0.00% | 0.00% |
| *(attrs-internal overhead)* | 34,309,265 | 32.72 | 10.58% | 24.98% |
| **`attrs` total** | **137,356,398** | **130.99** | **42.37%** | **100.00%** |

`colorSet` is, by a wide margin, the single largest field in the file — 90.07 MiB on its own,
29.13% of the whole 309.2 MiB, larger than `pos` and `rot` combined. This is exactly the
surprise constraint 4 predicted, and it gets its own analysis below rather than being folded
into a generic "attrs" candidate. One key present in the data, `unresolved`, is **not** in
`docs/bundle-schema.md` §4.2's documented `attrs` key list; it marks instances of unresolved
prefabs (sample: `assetId: "unresolved_2151360"`, matching the `unresolved_<id>` naming this
project's own memory of unresolved-prefab handling describes) and is schema-legal regardless —
§4.2 requires a consumer to ignore attrs keys it does not recognise — but is flagged here for
whichever later task next touches the schema text.

### 4.2 What the game stores

Two sources. First, the game's own component, not our producer's reading of it:

```
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Objects.Transform
```

`Game.Objects.Transform` (`$SCRATCH/audit/ObjectsTransform.cs`):

```csharp
public struct Transform : IComponentData, IQueryTypeParameter, IEquatable<Transform>, ISerializable
{
    public float3 m_Position;
    public quaternion m_Rotation;
    …
    public void Deserialize<TReader>(TReader reader) where TReader : IReader
    {
        reader.Read(out m_Position);
        reader.Read(out m_Rotation);
        if (!math.all(m_Position >= -100000f) || !math.all(m_Position <= 100000f)
            || !math.all(math.isfinite(m_Rotation.value)) || math.all(m_Rotation.value == 0f))
        { m_Position = default(float3); m_Rotation = quaternion.identity; }
    }
}
```

The game's own placement authority is a raw `float3`/`quaternion` — **f32, unrounded**, with a
range/finite sanity clamp on load but no precision reduction anywhere in this struct. Second,
`Game.Rendering.MeshColor` (`$SCRATCH/audit/MeshColor.cs`, same tool): `public struct MeshColor :
IBufferElementData, IEmptySerializable { public ColorSet m_ColorSet; }` — confirming
`docs/bundle-schema.md` §7.2's own description of where `attrs.colorSet` comes from, and
matching `Systems/InstanceRipSystem.cs:977-994`'s own reading of that exact buffer (cited, not
re-derived — the surrounding comment there, `:952-967`, already records the game-side story:
`MeshColorSystem` resolves `PseudoRandomSeed`, the palette's probabilities and the season
filters into this buffer once, and the rip reads the resolved answer rather than recomputing
it).

What our own producer does to that raw data is not a mystery needing a decompile — it is in this
repo. `IO/InstanceWriter.cs:107-125`:

```csharp
private static double[] Round(float3 value) => new[] {
    Math.Round((double)value.x, 3), Math.Round((double)value.y, 3), Math.Round((double)value.z, 3) };
private static double[] Round(quaternion value) => new[] {
    Math.Round((double)value.value.x, 6), … Math.Round((double)value.value.w, 6) };
```

— `pos` is rounded to 3 decimal places (millimetres, at this bundle's metre units), `rot` to 6.
`Systems/InstanceRipSystem.cs:1069-1072` rounds `colorSet` channels to 4 decimal places. None of
this rounding exists in the game's own `Transform`/`MeshColor` data — it is entirely a producer
choice, made once, at write time.

### 4.3 Information or representation?

**The core placement fields are pure representation.** `id`, `assetId`, `kind`, `pos`, `rot`,
`parent` and `scale` are a direct, lossless-modulo-rounding re-encoding of `Transform` plus the
prefab/owner/kind lookups §4.2 already documents as the game's own components — no new fact is
added by shipping them as JSON text; the only question is how many bytes that re-encoding costs,
answered in §4.4.

**`colorSet` — measured, not assumed, against the "small palette, referenced not restated"
hypothesis constraint 4 raises.** `colors.json` ships 3,619 per-sub-mesh palettes (§0's census;
cross-checked here: `python -c "import json,os; print(len(json.load(open(os.environ['BUNDLE']
+ '/colors.json'))))"` → `3619`). Whether an instance's resolved `attrs.colorSet` is simply one
of those palette entries, referenceable by a small index instead of twelve floats, was checked
directly (`$SCRATCH/audit/instances_colorset.py`, full population, excluding the 239
player-painted records `attrs.colorCustom == true` since those are genuinely one-off by
definition):

```
non-custom colorSet records: 1,207,399
distinct (assetId, colorSet) pairs: 492,085
dedup ratio (non-custom records / distinct pairs): 2.45x
```

**Mostly killed as a general hypothesis** — 2.45× is a real but modest ratio, nowhere near the
"handful of palette entries repeated across thousands of placements" shape `colors.json`'s own
3,619-entry catalogue would suggest. Broken down by asset, the picture is sharply bimodal and
the dominant byte contributors are the *worst* case for this hypothesis:

| assetId | instances | distinct `colorSet` values | distinct/instances |
|---|---:|---:|---:|
| `pd_Long_Grass` | 314,222 | 64,976 | 20.68% |
| `OakTree01` | 122,654 | 55,448 | 45.21% |
| `VFP_LiveOakTree` | 63,521 | 40,609 | 63.94% |
| `WallLightShortRange01` | 37,593 | 2,791 | 7.42% |
| `VentilationDuct04` | 20,203 | 1,658 | 8.21% |

The two largest instance counts (`pd_Long_Grass`, `OakTree01`) are also two of the *least*
redundant — a fifth to almost half of every placement's resolved colour is numerically unique.
This is consistent with `Systems/InstanceRipSystem.cs`'s own comment (§4.2): the resolved value
is not just a discrete palette pick, it is that pick further modulated by *season filters*,
which is a continuous adjustment, not a selection among `colors.json`'s finitely many `channels`
entries — so the shipped value is close to, but generally not equal to, any single
`colors.json` row, and a palette-index reference would not reproduce it losslessly. Smaller,
lower-instance-count assets (`WallLightShortRange01`, `VentilationDuct04`, ~7-8% distinct) *are*
genuinely redundant, but they are a small share of the 90.07 MiB `colorSet` total, not the
majority of it. **Verdict: `colorSet` is predominantly information (real per-instance,
season-modulated variation), not representation** — the opposite of what a naive reading of
`colors.json`'s existence would suggest, and this is reported plainly rather than forcing the
result the brief's framing hinted at.

**`decalLayers` — a genuine, if narrow, representation finding.** Every one of the 18,479
`decal` records carries a non-empty `decalLayers`; broken down by member
(`$SCRATCH/audit/instances_rot_kind.py`-adjacent check, full population): `Terrain` 18,479,
`Roads` 18,479, `Buildings` 18,479 — **every single decal record's list is the identical
three-element `["Terrain","Roads","Buildings"]`, with no exceptions**, in this city. That is
representation with zero information content *in this specific rip* (the schema permits other
subsets in principle, so this is a data fact about this city, not a contract defect) — too small
to be worth a dedicated candidate row (0.55 MiB, 0.18% of the file) but recorded because
constraint 4 asks to follow the bytes wherever they lead, not only to the largest one.

**The rotation-about-Y-only hypothesis — checked against the full population, and false.** The
brief's own sample suggested every `rot` looks like `[0, y, 0, w]`. Checked directly
(`$SCRATCH/audit/instances_columns.py`, full population, all 1,283,183 records):

```
records with rot.x != 0 or rot.z != 0: 318,225 of 1,283,183 (24.80%)
```

Broken down by `kind` (`$SCRATCH/audit/instances_rot_kind.py`):

| kind | total | tilted (x/z ≠ 0) | % |
|---|---:|---:|---:|
| `tree` | 697,163 | 246,280 | 35.33% |
| `prop` | 558,645 | 60,096 | 10.76% |
| `decal` | 18,479 | 11,845 | 64.10% |
| `building` | 8,896 | 4 | 0.04% |

**Killed as a whole-file hypothesis, confirmed as a near-exact one for `building` alone.**
Buildings are almost always Y-axis-only rotation (0.04% exceptions — architecture standing
upright on flat lots), which is what the brief's sample happened to catch, but trees lean with
terrain slope and growth variation, decals are oriented to whatever surface they are projected
onto (§4.2a, cited: decals are projector volumes, not flat placements), and even props tilt a
tenth of the time. A columnar encoding cannot assume `rot.x == rot.z == 0` and drop them
file-wide; §4.4 ships the full quaternion instead.

**id/parent range**, checked directly rather than assumed (`$SCRATCH/audit/instances_columns.py`):
`id` range `[50,769, 2,595,164]`, `parent` range identical — both comfortably fit an unsigned
32-bit column, and `0` never appears as a real `id` in this bundle, which §4.4 uses as a free
"no parent" sentinel.

### 4.4 Smallest lossless encoding

**The losslessness claim, stated against the baseline it is actually true for.** Per constraint
3: a binary `f32` column is lossless *relative to what this bundle currently ships* if the
shipped text already carries fewer significant digits than `f32`'s 24-bit mantissa resolves at
that magnitude — **not** relative to the game's own internal values, which §4.2 just showed are
full, unrounded `f32` with no clamping beyond a ±100,000 m sanity range. This bundle's text is
already rounded (`IO/InstanceWriter.cs`, §4.2): 3 dp for `pos`, 6 dp for `rot`, 4 dp for
`colorSet`. Confirmed directly, first the decimal places actually present (200k-record sample,
`$SCRATCH/audit/instances_precision.py`):

```
pos: decimal places {1: 5,536, 2: 47,800, 3: 546,664}   -- never more than 3
rot: decimal places {0: 1,004, 1: 353,054, 2: 34, 3: 456, 4: 3,606, 5: 41,276, 6: 400,570}  -- never more than 6
```

— then an actual round-trip, full population, not a plausibility argument
(`$SCRATCH/audit/instances_columns.py`): pack each shipped decimal value as an IEEE-754 `f32`,
unpack it, re-round to the shipped precision, and compare to the original text's own rounded
value:

```
pos f32 round-trip: 3,849,549 values checked, 0 failed (0.000000%)
rot f32 round-trip: 5,132,732 values checked, 0 failed (0.000000%)
```

`measured`, exact, full population, zero failures. The same check run on `colorSet`
(`$SCRATCH/audit/instances_colorset_precision.py`, 4 dp, full population):

```
colorSet f32 round-trip: 14,491,656 values checked, 0 failed (0.000000%)
```

**So: `f32` is proven bit-losslessly sufficient for every numeric field this bundle currently
ships, against the precision this bundle currently ships it at — stated in exactly those terms,
not as a claim about the game's own precision, which is finer (unrounded) and which this
producer already deliberately discards at write time, a decision this task does not revisit.**
The same full-population method applied to `elevation`, `growth`, `growthScale` and
`decalHeight` (`$SCRATCH/audit/instances_attrs_precision.py`) also found **zero** round-trip
failures across all four fields. A cheaper `u8` (1/255 step) quantization of `colorSet` was also
tested and is **not** lossless against shipped precision — `65.21%` of the 14,491,656 sampled
channel values fail to round-trip (`$SCRATCH/audit/instances_colorset_precision.py`) — so it is
reported separately as a lossy candidate, not folded into the lossless row below.

**Compressibility floor — full file, not sampled** (309.2 MiB fits comfortably in memory, so
this is exact rather than extrapolated, unlike §1/§2's sampled figures):

```
python "$SCRATCH/audit/instances_compress_full.py"   # zlib.compress(raw, 9), lzma.compress(raw), full file, bit-exact round-trip asserted
```

```
full file: 324,202,611 B (309.2 MiB)
  zlib -9: 60,210,607 B (57.4 MiB, 18.57%), round-trip bit-exact, 25.8 s
  lzma   : 38,867,676 B (37.1 MiB, 11.99%), round-trip bit-exact, 135.5 s
```

`measured`, exact, full population. `lzma` saves 285,334,935 B (272.1 MiB, **88.01%** of
`instances.jsonl`, 5.83% of the whole bundle); `zlib -9` saves 263,992,004 B (251.8 MiB,
**81.43%**, 5.39% of the bundle).

**The columnar candidate — every column named, every width checked against the real data (not
adopted from the brief's sketch unmodified).** The brief's own sketch (`id` u32, `assetId` u16
catalogue index, `kind` u8, `pos` 3×f32, `rot` 3×i16 dropping `w`, `parent` u32, `attrs` in typed
side-columns) is corrected in one place before being sized: §4.3 already showed the
rotation-about-Y-only assumption behind "`rot` 3×i16, `w` from sign" is false for 24.80% of
records, so the row below keeps the **full 4-component `f32` quaternion** (16 B) rather than the
brief's 3×i16 (6 B) — a real, deliberate departure from the brief, not an oversight; a lossy
alternative that still drops a component is named, unmeasured, as INST-4 below. Every other
width was checked against this bundle's own measured ranges/precision (§4.1, §4.3, above), not
assumed:

Fixed columns (every record), `$SCRATCH/audit/instances_columnar_total.py`:

| column | width | basis |
|---|---:|---|
| `id` | u32 (4 B) | range `[50,769, 2,595,164]` fits (§4.3) |
| `assetId` | u16 catalogue index (2 B) | 4,310 distinct fits u16 (§4.1, matches §2.3) |
| `kind` | u8 (1 B) | 4 used, 6 documented (§4.1) |
| `pos` | 3×f32 (12 B) | lossless vs. shipped 3 dp text, proven above |
| `rot` | 4×f32 (16 B) | full quaternion — lossless; the brief's 3×i16 does not hold (§4.3) |
| `parent` | u32, sentinel `0` = none (4 B) | `0` never a real `id` (§4.3), so no separate presence bit needed |
| `attrsPresent`/`scalePresent` | u16 bitmask (2 B) | up to 16 optional columns, see below |
| **fixed total** | **41 B/record** | **52,610,503 B (50.17 MiB)** |

Sparse typed side columns, ordered implicitly by the presence bitmask (no `id` repeated per
entry):

| attrs/optional field | records | width | basis | bytes |
|---|---:|---:|---|---:|
| `colorSet` | 1,207,638 | 12×f32 (48 B) | f32-lossless, proven above | 57,966,624 B (55.28 MiB) |
| `elevation` | 483,130 | f32 (4 B) | f32-lossless, proven above | 1,932,520 B (1.84 MiB) |
| `growth` | 311,999 | f32 (4 B) | f32-lossless, proven above | 1,247,996 B (1.19 MiB) |
| `growthScale` | 299,879 | f32 (4 B) | f32-lossless, proven above | 1,199,516 B (1.14 MiB) |
| `treeState` | 256,813 | u8 enum (1 B) | 4 values: Adult/Teen/Elderly/Dead (§4.3-adjacent) | 256,813 B (0.24 MiB) |
| `unresolved` | 24,730 | u8 bool (1 B) | — | 24,730 B (0.02 MiB) |
| `decalLayers` | 18,479 | u8 bitmask (1 B) | ≤6 documented layers | 18,479 B (0.02 MiB) |
| `decalHeight` | 18,479 | f32 (4 B) | f32-lossless, proven above | 73,916 B (0.07 MiB) |
| `elevationFlags` | 18,021 | u8 bitmask (1 B) | ≤4 documented flags | 18,021 B (0.02 MiB) |
| `state` | 8,896 | u8 enum (1 B) | ≤5 documented states | 8,896 B (0.01 MiB) |
| `colorCustom` | 239 | u8 bool (1 B) | — | 239 B (0.00 MiB) |
| `scale` | 6,997 | 3×f32 (12 B) | present on 0.545% of records (§4.1) | 83,964 B (0.08 MiB) |
| **side total** | | | | **62,831,714 B (59.92 MiB)** |

Plus an `assetId` catalogue (4,310 sanitised prefab-name strings, ~0.10 MiB, negligible).
**Grand total: 115,545,657 B (110.2 MiB)** — `arithmetic` (real, exact per-field record counts
from this bundle, times widths individually verified lossless by full-population round-trip
above; not a single actual re-encode-and-decode of the whole combined file). The **fixed 41
B/record core** (`id`/`kind`/`pos`/`rot`) *was* actually built and read back as real bytes,
below. Saved versus as-shipped: **208,656,954 B (199.0 MiB, 64.36% of `instances.jsonl`, 4.26%
of the whole bundle).**

**The cheap alternative wins outright here — reported plainly, per constraint 2.** `lzma`
(37.1 MiB) is **2.97× smaller** than the columnar candidate (110.2 MiB), for a fraction of the
engineering cost: one standard-library decompress call versus a new binary format, an `assetId`
catalogue, a presence-bitmask decoder and a typed side-column reader. This is not "close, and
worth noting" — it is a decisive win for the cheap answer, the same shape of result MESH-1 found
for `meshes/` (§2), and it is reported as such rather than talked around. **If the only goal is
bytes at rest, `instances.jsonl.xz` beats a columnar redesign and costs almost nothing to ship.**

**But bytes are not the whole value of this candidate — import time is the other half
(constraint 5), and here the columnar form wins by a much larger margin than it loses on
bytes.** `json.loads` cost, measured directly on the real file, full population:

```
python "$SCRATCH/audit/instances_parse_time.py"
```

```
one full json.loads pass: 1,283,183 records in 7.09 s (0.0055 ms/record)
raw line iteration, no parse: 1,283,183 lines in 0.66 s
```

`tools/blender_import_bundle.py`, this bundle's reference consumer, does not read
`instances.jsonl` once per run, and how many times it does read it depends on the invocation —
checked directly against the argument parser rather than assumed. `center` is initialised to
`None` (`:3310`) and stays `None` unless set by `--center` (`:3365-3371`) or defaulted by
`--everything` (`:3399-3400`, `if center is None: center = "downtown"`, itself gated on
`everything is not None`). A **bare invocation** (`blender --python
tools/blender_import_bundle.py -- <bundle>`, no flags) therefore keeps `kinds = {"building"}`
(`:3404-3405`) and `center = None`: `survey()` (`:558-595`) runs once to report bounds/centroid
before placing anything (`:3432`, "Always survey before placing... Knowing where the selection
is turns 'nothing appeared' from a mystery into an arithmetic error"), the `if center ==
"downtown":` skyline scan at `:3456` never fires because `center` is `None`, and the placement
loop itself (`:3517`, `for record in (read_jsonl(instances_path) if kinds else ())`) reads it a
second time to place objects — **two full `json.loads` passes**, not three. The tool's own
docstring states plainly that this bare, cheap invocation is the intended default (`:83`,
"Placing all 1.3M instances of a full city is deliberately not the default").

**A third pass is real, but belongs to a different, explicitly-requested invocation.** Passing
`--everything <radius>` (the tool's documented full-city invocation, `:76` of its own docstring)
or an explicit `--center downtown` sets `center = "downtown"`, which *does* trigger the
buildings-only skyline scan (`:3456-3462`, `survey(instances_path, {"building"},
weight=skyline_weight)`) in addition to the two passes above — **three full `json.loads` passes**
for that invocation specifically. Both figures, correctly attached to the invocation that
produces them, measured at 7.09 s/pass:

| invocation | passes | parse cost | I/O floor (`0.66 s`/pass) | `json.loads` overhead beyond I/O |
|---|---:|---:|---:|---:|
| bare run (no flags) — the tool's actual default (`:83`) | 2 | ≈14.2 s | ≈1.3 s | ≈12.9 s |
| `--everything <R>` or explicit `--center downtown` — the documented full-city invocation | 3 | ≈21.3 s | ≈2.0 s | ≈19.3 s |

A packed binary pays this once, and reads fast. Built and read back for real
(`$SCRATCH/audit/instances_packed_read_bench.py` — writes the actual `id`/`kind`/`pos`/`rot`
fixed columns above to a real file, then reads them back with `numpy.frombuffer`, asserting the
`id` array round-trips exactly):

```
built columns from 1,283,183 records in 8.52s (parsing + building four Python lists)
packed fixed columns (id u32, kind u8, pos 3xf32, rot 4xf32): 42,345,039 B (40.4 MiB), 33.00 B/record
numpy read-back of the packed fixed columns: 0.0122s for 1,283,183 records
speedup vs building the same columns from JSON: 696x
```

`measured`, real bytes, real round-trip (`id` array assertion passed). Even generously assuming
`survey()` and the placement loop would each still need an extra 0.1 s of Python-side
bookkeeping atop the raw `numpy` read, either invocation's passes over a packed columnar form
cost well under a second — ≈0.2 s for the bare run's two passes, ≈0.3 s for the full-city
invocation's three — not the ≈14.2 s or ≈21.3 s the table above measured for JSON text. A 696×
measured speedup on the fixed core is two orders of magnitude larger than INST-2's 64.36% byte
saving, exactly what constraint 5 predicted: this candidate's import-time value exceeds its byte
value **under either invocation**, not only the full-city one. This is not measured for `attrs`'
variable side-columns (only the fixed core was benchmarked), so the real end-to-end speedup for
a full-record read (including `colorSet` etc.) is somewhat lower than 696× but is `unmeasured`
beyond the fixed-core figure above — building and timing that reader is out of scope for Phase
A's measurement-only mandate.

### Rip time

`instances.jsonl` is written inside §8's row 1 (Instances bracket, 11:50:23.206–11:55:54.823,
331.617 s) — cited, not re-derived. No sub-step of that bracket is separately instrumented for
`instances.jsonl` alone (§8's instrumented sub-steps all fall in rows 2–3), so the write-time
cost of any candidate below is `unmeasured` at finer grain than the whole 331.617 s bracket.

### Consumer import time

Covered above as part of §4.4, per constraint 5 — repeated here only as a pointer, not
duplicated. Two figures, attached to the invocation each belongs to (the reference importer's
actual default is a bare, two-pass run per its own docstring, `:83`; a third pass is real but
only for `--everything`/explicit `--center downtown`, its documented full-city invocation):
**≈14.2 s of `json.loads` cost across two passes for a bare run (≈12.9 s of that is parse
overhead beyond raw file I/O); ≈21.3 s across three passes for the full-city invocation
(≈19.3 s overhead). A packed binary reader reads the same fixed columns back in 0.0122 s per
pass, a 696× measured speedup, under either invocation.**

### Candidate rows

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| INST-1 | Compress `instances.jsonl` at rest (`lzma` or `zlib -9`) | measured, full file, `lzma` **272.1 MiB (88.01% of the file, 5.83% of bundle)**; `zlib -9` **251.8 MiB (81.43%, 5.39%)** | unmeasured (producer-side compress cost at write time; not timed) | one decompress call before parsing — every mainstream language ships `lzma`/`zlib` in its standard library or a trivial dependency | low — ubiquitous, well-tested codecs, bit-exact by construction, verified on the full file | YES, full-file bit-exact round-trip asserted | **new layer**: a compressed at-rest/transport encoding of the unchanged JSON bytes (e.g. `instances.jsonl.xz`), needs a `compression` field amendment under §12.1 (same shape as MESH-1, §2); **retires**: nothing, the canonical JSON bytes are recovered exactly on decompress |
| INST-2 | Columnar binary: fixed 41 B/record core (`id`/`assetId`/`kind`/`pos`/`rot`/`parent`/presence-bitmask) + sparse typed `attrs` side-columns, full detail above | arithmetic (real per-field counts × individually-verified-lossless widths; fixed-core subset actually built and read back), **199.0 MiB (64.36% of the file, 4.26% of bundle)** — **2.97× larger than INST-1's `lzma` result, reported plainly per constraint 2: the cheap answer wins on bytes** | unmeasured (needs a C#-side writer; not timed) | real: a new binary reader, an `assetId` catalogue, a presence-bitmask decoder and typed side-column parsing, replacing `json.loads` entirely — but see the import-time result: for the reference importer's actual default (a bare, two-pass run, `tools/blender_import_bundle.py:83`), measured **≈14.2 s of `json.loads` cost → ≈0.2 s** with a packed reader (696× per-pass speedup on the fixed core); for its `--everything`/explicit `--center downtown` full-city invocation (three passes), **≈21.3 s → ≈0.3 s** | medium — new container format and schema surface, but every component width was individually verified lossless against this bundle's real data (§4.3/§4.4), not guessed | YES — every numeric column proven f32-lossless by full-population round-trip; enums/bitmasks are lossless replacements of already-closed, already-documented string sets | **new layer**: `instances.bin` + a small `assetId` catalogue alongside `instances.jsonl`, opt-in; **retires**: nothing until an A/B gate proves the reader matches the JSON form record-for-record (same transitional-bundle caveat as NET-1, §3) — until then the transitional bundle is 324,202,611 + 115,545,657 = 439,748,268 B (419.4 MiB) for this domain, larger than today's `instances.jsonl` alone, for as long as both ship together |

**Lossy candidates** (named and measured or explicitly left unmeasured per constraint 6 —
investigate lossless first, which both rows above already do exhaustively for this domain):

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | error bound | additive layer |
|---|---|---|---|---|---|---|---|---|
| INST-3 | `colorSet` channel quantization to `u8` (1/255 step) instead of `f32`, inside INST-2's side-column layout | measured, would shrink the `colorSet` side-column from 48 B to 12 B/record (4×), saving up to ~41.4 MiB of INST-2's 55.28 MiB `colorSet` column — **not recommended**: §4.3 found `colorSet` predominantly *information* (season-modulated, near-unique per instance for the dominant tree/grass assets), so lossy banding here is a real visible-quality risk, not a free win the way §1's channel-drop rows were | unmeasured | none beyond INST-2's own reader | medium — visible colour banding on the highest-instance-count assets specifically (`OakTree01`, `pd_Long_Grass`), where the entropy is real | **NO** | measured: `65.21%` of 14,491,656 sampled `colorSet` channel values fail to round-trip through a `u8` (1/255) step against the shipped 4 dp text (`$SCRATCH/audit/instances_colorset_precision.py`, full population) | would replace the `colorSet` side-column inside INST-2's own new layer, not a separate file |
| INST-4 | `rot` smallest-three quaternion compression (drop whichever of the 4 components has the largest magnitude, store the other 3 quantized, canonicalize sign via the unit-norm constraint — q and −q are the same rotation) instead of the full 4×f32 | unmeasured — Phase A does not build or measure it | unmeasured | consumer must reconstruct the dropped component and de-quantize per component | medium — well-understood technique, but not built or verified here | **NO** | `unmeasured` — no re-encode attempted; note the brief's own specific scheme ("`rot` 3×i16, `w` from sign", i.e. always drop `w`) is **not valid file-wide**, since §4.3 measured `rot.x`/`rot.z` nonzero on 24.80% of records — a valid version of this technique must drop the *largest-magnitude* component per record, not always `w` | would replace the `rot` fixed column inside INST-2's own new layer (16 B → ~7 B with a 2-bit selector + 3×i16), not a separate file |

## 5. `lanes.jsonl`

`lanes.jsonl` is 242,145,498 bytes (230.9 MiB, 4.9% of the 4,894,194,530-byte total, §0), 1
file, 680,610 records — the fifth-largest share. This is the drivable graph: where a vehicle
may go, in which direction, at what speed (`docs/bundle-schema.md` §5A, cited: `networks/` is
the road *surface*, this is the road *graph*). Unlike §1–§3's mesh/texture domains, the data
here is genuinely wanted by a level builder for traffic and AI — only the *encoding* of the
path is in question, per this task's own framing, and there is a specific hypothesis to test:
each `path` looks like points sampled evenly along a curve, and the game's own authoritative
lane geometry is a `Bezier4x3` (four control points). If true, storing the four control points
instead of nine sampled points is fewer bytes **and** strictly more information (an exact curve
vs. an approximation of one) — an unusual combination this section tests rather than assumes.

### 5.1 What we ship

Command (this section's own script, built to the brief's spec and extended for the `kind`
breakdown constraint 4 requires):

```
python "$SCRATCH/audit/lanes_bezier.py" "$BUNDLE"
```

`measured`, full population (680,610 of 680,610 records), exact per-field bytes (`json.dumps
(value)` with no subtraction, overhead computed as `raw_total − Σ value_bytes`, the same
corrected method §4.1 used after the brief's own per-field script undercounted there — applied
here from the start rather than repeating that mistake):

| field | bytes | MiB | % of file |
|---|---:|---:|---:|
| `path` | 188,520,072 | 179.79 | 77.85% |
| `flags` | 11,489,937 | 10.96 | 4.75% |
| `kind` | 6,426,340 | 6.13 | 2.65% |
| `id` | 4,557,661 | 4.35 | 1.88% |
| `owner` | 4,504,734 | 4.30 | 1.86% |
| `length` | 3,508,616 | 3.35 | 1.45% |
| `secondary` | 1,282,696 | 1.22 | 0.53% |
| `curviness` | 746,678 | 0.71 | 0.31% |
| `limit` | 573,303 | 0.55 | 0.24% |
| *(JSON structural overhead)* | 20,535,461 | 19.58 | 8.48% |
| **TOTAL** | **242,145,498** | **230.93** | **100.00%** |

`path` alone is 77.85% of the file — the single largest field by a wide margin, exactly where
this task's own hypothesis should be aimed.

**Points-per-lane** — the census the brief's script asked for, full population:

| points | records | % |
|---:|---:|---:|
| 9 | 680,565 | 99.99% |
| 8 | 1 | 0.00% |
| 7 | 2 | 0.00% |
| 5 | 1 | 0.00% |
| 4 | 5 | 0.00% |
| 3 | 21 | 0.00% |
| 2 | 15 | 0.00% |

**680,565 of 680,610 records (99.99%) carry exactly 9 points**, mean 9.00 points/lane overall.
The 45 records (0.0066%) that don't are examined in §5.3 rather than dismissed.

**Kinds** (`docs/bundle-schema.md` §5A's own closed set: `car`, `track`, `pedestrian`,
`parking`, `connection`, `other` — measured kinds match it exactly, no unknowns):

| kind | records | % |
|---|---:|---:|
| `connection` | 269,751 | 39.63% |
| `other` | 136,216 | 20.01% |
| `car` | 131,618 | 19.34% |
| `pedestrian` | 97,677 | 14.35% |
| `parking` | 44,083 | 6.48% |
| `track` | 1,265 | 0.19% |

`connection` — the brief's own sample line — is the largest single kind at 39.63%.

**Optional fields, presence and ranges** (full population): `owner` present on 677,547 records
(99.55%), range `[50,769, 2,595,164]`, **never `0`** — a free sentinel, the same pattern §4.3
found for `instances.jsonl`'s `id`/`parent`. `secondary` true on 320,674 (47.12%). `limit` and
`curviness` are present on exactly the same 132,883 records (19.52%) — this is not a
coincidence to model separately: 131,618 `car` + 1,265 `track` = 132,883 exactly, so both
fields are fully determined by `kind ∈ {car, track}`, needing no independent presence bit.
`flags` non-empty on 272,161 records (39.99%), drawn from **50 distinct names** across
821,959 total flag instances (mean 3.02 flags per flagged record) — the full vocabulary is in
`$SCRATCH/audit/lanes_bezier.txt`; the top five are `ForbidPassing` (91,420), `Unsafe`
(69,815), `AllowMiddle` (69,508), `LeftLimit` (54,775), `RightLimit` (50,370). `id` range
`[54,749, 2,559,021]` — both `id` and `owner` fit an unsigned 32-bit column comfortably, the
same as §4.4 found for `instances.jsonl`.

### 5.2 What the game stores

**Two sources, per this document's own rule.** First, our own producer — `IO/LaneWriter.cs` is
79 lines, read in full (confirmed: `wc -l` → 79):

```csharp
public void Write(LaneRecord record)
{
    if ((record.Path == null) || (record.Path.Count < 2)) return;

    List<double[]> path = new(record.Path.Count);
    foreach (float3 point in record.Path) path.Add(Round(point));
    …
}

private static double[] Round(float3 v) =>
    new double[] { Math.Round(v.x, 3), Math.Round(v.y, 3), Math.Round(v.z, 3) };
```

`LaneWriter` itself does no sampling — it only rounds whatever `record.Path` already holds to 3
decimal places (millimetres), matching every other coordinate in the bundle. The sampling
happens upstream, in `Systems/LaneRipSystem.cs`:

```csharp
private const int Samples = 8;
…
Tessellate(curve.m_Bezier, record.Path);
…
private static void Tessellate(Colossal.Mathematics.Bezier4x3 curve, List<float3> points)
{
    for (int i = 0; i <= Samples; i++)
    {
        float3 point = Colossal.Mathematics.MathUtils.Position(curve, i / (float)Samples);
        if ((points.Count > 0) && (math.distancesq(points[points.Count - 1], point) < 1e-6f)) continue;
        points.Add(point);
    }
}
```

(`Systems/LaneRipSystem.cs:55,171,297-308`.) This settles question 3 by source inspection
alone, per the brief's own prediction: **`path` is a uniform, 9-point (`i/8` for `i = 0..8`)
tessellation of `curve.m_Bezier`, with a 1 mm-radius dedup pass dropping near-duplicate
consecutive samples** (the `distancesq < 1e-6f` gate — a 1 mm distance, squared). `curve` comes
from `EntityManager.GetComponentData<Curve>(entity)` (`:131`), the same lane entity `AddLaneData`
reads speed/curviness/flags from. `Domain/LaneRecord.cs`'s own doc comment already says this in
plain language: *"The lane's path, world space, already tessellated from CS2's bézier."* This
is not a new discovery — it is already documented in this repo; this section is the first to
measure what that means in bytes.

Second, the game's own component, decompiled (the cached `decomp all` dump of `Game.dll`,
per this task's resolved instructions — located and confirmed present with `grep -A5 "public
struct Curve" "E:/carto-out/game-src/Game/Game.Net.Curve.cs"`, then read in full):

`Game.Net.Curve` (`E:\carto-out\game-src\Game\Game.Net.Curve.cs`):

```csharp
public struct Curve : IComponentData, IQueryTypeParameter, IStrideSerializable, ISerializable
{
    public Bezier4x3 m_Bezier;
    public float m_Length;

    public void Serialize<TWriter>(TWriter writer) where TWriter : IWriter
    {
        writer.Write(m_Bezier);
    }

    public void Deserialize<TReader>(TReader reader) where TReader : IReader
    {
        reader.Read(out m_Bezier);
        m_Length = MathUtils.Length(m_Bezier);
    }
}
```

The game's own on-disk save format for a lane's curve **is `m_Bezier` alone** — `Serialize`
writes only the four control points; `m_Length` is never written, and is recomputed from
`m_Bezier` every time a save loads. `Colossal.Mathematics.Bezier4x3` (decompiled directly, not
cached in the `Game.dll` dump — `./tools/decomp/bin/Release/net8.0/decomp.exe type
Colossal.Mathematics Colossal.Mathematics.Bezier4x3`) is exactly `public float3 a, b, c, d;` —
**four control points, three `f32` each**, matching the brief's own arithmetic assumption
exactly. `Colossal.Mathematics.MathUtils.Position(Bezier4x3 curve, float t)` (same decompile
target) evaluates the textbook cubic Bézier basis:

```csharp
public static float3 Position(Bezier4x3 curve, float t)
{
    float3 @float = new float3(t, 1f - t, 3f * t);
    float4 float2 = @float.yzzx * @float.yyxx * @float.yyyx;
    return curve.a * float2.x + curve.b * float2.y + curve.c * float2.z + curve.d * float2.w;
}
```

— `float2 == ((1−t)³, 3t(1−t)², 3t²(1−t), t³)`, bit-for-bit the same basis this section's own
refit script (§5.3) uses. `MathUtils.Length(Bezier4x3 curve)` — the formula `m_Length` recomputes
on every load — is a 16-sample chord-length sum: `Position(curve, i/16)` for `i = 1..16`,
summing consecutive distances. Both facts matter for §5.3/§5.4: the game's own path *and*
length are functions of the same four numbers, and our producer already reads that exact
component (`Curve`) before discarding everything but nine samples and one scalar.

`docs/cs2-api-audit.md` §12 ("Lanes versus the game") already validates the *other* lane
fields — `m_SpeedLimit`, `m_Curviness`, flags-as-names, master/`SecondaryLane` — against
`Game.Net.CarLane`/`TrackLane` etc.; that section is cited here rather than re-derived, per this
task's brief, and it does not itself cover path/curve sampling — that is this section's own,
new finding. `docs/bundle-schema.md` §5B (`lanemeshes.jsonl`, cited) already ships a
`curve` field as *"world-space cubic bézier control points"* — `[[x,y,z],[x,y,z],[x,y,z],
[x,y,z]]` — for a different lane subsystem (drawn utility/fence geometry), with a normative
bend algorithm and a working reference consumer in `tools/blender_import_bundle.py`. Storing
raw Bézier control points is therefore not a new pattern this section is proposing from
scratch; it is an existing, already-shipped, already-consumed convention in this exact bundle,
applied to a second file.

### 5.3 Information or representation?

**The Bézier hypothesis, tested on real data, not assumed.** Script (this section's own,
extended per constraint 4 to break down by `kind` and to test the parameterisation-failure-mode
distinction constraint 3 requires):

```
python "$SCRATCH/audit/lanes_bezier.py" "$BUNDLE"
```

Two refits were run, both full population, not sampled (680,610 records fit comfortably in
memory and the fit itself is cheap — 9 points against 4 unknowns per record):

1. **Exact-`t` refit** — for the 680,565 records with exactly 9 points, solve the 9×4
   least-squares system at the **known true sampling grid** `t = i/8, i = 0..8` — not a guess,
   `LaneRipSystem.Tessellate`'s own grid (§5.2). This is the clean test: zero parameterisation
   ambiguity, because the grid is read from source, not inferred from point count.
2. **Naive `linspace(0,1,len(P))` refit** — the brief's own method, re-parameterising by point
   *count* alone, run on every record with ≥4 points (680,574 of them) for direct comparability.

`measured`, full population:

| refit | n | p50 | p90 | p99 | p99.9 | p100 |
|---|---:|---:|---:|---:|---:|---:|
| exact-`t=i/8` (len==9 only) | 680,565 | 0.467 mm | 0.649 mm | 0.813 mm | 0.983 mm | 2.065 mm |
| naive `linspace` (all len≥4) | 680,574 | 0.467 mm | 0.649 mm | 0.813 mm | 0.983 mm | 2.065 mm |

The two are identical to the precision shown, because `linspace(0,1,9)` **is** `i/8` when no
point was dropped — which is true for 99.99% of the file, so the naive method and the true
method agree almost everywhere by construction, not by coincidence.

By `kind` (exact-`t` refit; constraint 4's own instruction — one class of lane behaving
differently is exactly what a single aggregate hides):

| kind | n | p50 | p99 | p100 |
|---|---:|---:|---:|---:|
| `connection` | 269,751 | 0.470 mm | 0.808 mm | **2.065 mm** |
| `other` | 136,201 | 0.463 mm | 0.818 mm | 1.242 mm |
| `car` | 131,610 | 0.481 mm | 0.822 mm | 1.314 mm |
| `pedestrian` | 97,665 | 0.457 mm | 0.808 mm | 1.277 mm |
| `parking` | 44,073 | 0.449 mm | 0.808 mm | 1.149 mm |
| `track` | 1,265 | 0.481 mm | 0.833 mm | 1.147 mm |

**No kind behaves differently in kind, only in degree** — every kind's p50 sits at ~0.45–0.48
mm and every kind's p100 is under 2.1 mm. `connection` (the brief's own sample line, and the
plurality kind at 39.63%) happens to own the single worst-fitting record in the whole file, at
2.065 mm — still two orders of magnitude below anything that matters at metre-scale lane
geometry, and the same order of magnitude as the mesh-quantisation error §2.4 already judged
acceptable at 20.505 mm.

**Read plainly: p100 at millimetre scale — the polyline *is* a Bézier sampling, judged against
this bundle's own 3-decimal-place (millimetre) coordinate convention as the applicable
tolerance** (`IO/LaneWriter.cs`, `IO/InstanceWriter.cs` §4.2 — every coordinate in this bundle
is already rounded to this precision, so it is the precision a lossless claim needs to clear,
not float64 exactness). This is not a coincidental curve-fit — §5.2 already showed the
producer's own sampling grid and the refit's Bézier basis are the identical formula.

**The 45 non-9-point records — a failure-mode check, per constraint 3, not a discard.**

```
points<4 (untestable): 36 records — kinds `other` 13, `parking` 8, `pedestrian` 8, `car` 7; length range 0.003-0.006 m
4<=points<9 (testable): 9 records — 5 with exactly 4 points, 4 with 5-8 points
```

`measured`, full population of the 45, re-derived directly rather than restated from an earlier
draft (`$SCRATCH/audit/lanes_edge_fix.py`, full re-scan): the 36 records with fewer than 4
points span **all four** of the kinds present in this bucket — `other` 13, `parking` 8,
`pedestrian` 8, `car` 7, not only `parking`/`other` as an earlier pass of this section
mis-stated — and their `length` values range **0.003–0.006 m** (three records at exactly 3 mm,
twenty at 4 mm, ten at 5 mm, six at 6 mm), not the narrower 4–6 mm range that earlier pass
claimed. Both corrections are real re-derivations, not rounding: the earlier figures did not
reproduce against the live file and are replaced here. The substance is unchanged — these are
still degenerate, near-zero-length lanes (parking-stall connectors and similarly short
pedestrian/car/other segments) where the 1 mm dedup gate (§5.2) collapses most of the 9 samples
into 2–3 surviving points because the *entire curve* spans only a few dedup radii. A cubic
Bézier still represents this exactly — the four control points are simply near-coincident — this
is a polyline-side artefact of too-few-surviving-samples, not evidence against the Bézier
hypothesis, and it needs no separate fallback in a Bézier-based encoding (unlike a
fixed-sample-count polyline encoding, which *would* need to handle a variable point count here).

**The 9 records with 4–8 points split into two populations with different evidential weight, and
they must not be pooled.** Of the 9, **5 have exactly 4 points.** A cubic Bézier has 4 unknown
control points; fitting it through exactly 4 constraint points is not a test of anything — it is
guaranteed to fit to floating-point-noise precision regardless of whether the underlying curve is
a Bézier at all. Measured directly (`$SCRATCH/audit/lanes_edge_fix.py`, chord-length refit on
each of the 5 exactly-4-point records individually):

```
exactly-4-point records (n=5): errors of 2.7e-12, 4.5e-12, 3.6e-12, 1.8e-12, 1.4e-12 m
```

`measured`: errors at the 10⁻¹² m scale — floating-point solver noise, not curve-fit residual.
**These 5 records carry no evidential weight for or against the Bézier hypothesis and are
excluded from the verdict below**, exactly as constraint 3's warning about trivial fits
predicts. The remaining **4 records (5, 7, 7, and 8 points respectively) are genuinely
overdetermined** — more constraint points than unknowns — and are the only members of this
bucket a chord-length refit actually tests:

```
overdetermined chord-length refit (n=4 records, point counts 5/7/7/8): p50 0.378 mm, p90 0.790 mm, p99 0.925 mm, p100 0.940 mm
individual errors (sorted, not positionally paired with the point-count list above): 0.000, 0.317, 0.439, 0.940 mm
  by point count: 5-point record 0.000 mm; the two 7-point records 0.317 mm and 0.940 mm; the 8-point record 0.439 mm
```

`measured`: still sub-millimetre at every percentile, on the only 4 records in this whole
45-record edge case actually capable of falsifying the hypothesis. **Verdict: no genuine
polyline and no parameterisation failure was found anywhere in the population — stated on the
evidence that supports it, not on the 5 trivial fits.** Both failure modes constraint 3 asks to
distinguish were tested and both came back negative on the overdetermined subset; the 0.0066% of
records that don't fit the clean 9-point case are short lanes whose sample count shrank under the
producer's own dedup gate, not lanes whose underlying geometry is not a Bézier — and the
headline evidence for that claim is the 4-record, 0.940 mm-p100 overdetermined result, not the
9-record aggregate an earlier pass of this section reported.

**Bonus finding: `length` is also fully derivable, not just `path`.** §5.2 already showed the
game's own `Curve.Serialize` writes only `m_Bezier`, never `m_Length` — the game recomputes
length from the curve on every load, via a 16-sample chord sum. The same formula, applied to
this section's own refit control points (packed as `f32`, §5.4), was checked against the
shipped (3-decimal-rounded) `length` value on a 4,968-record stride sample across the full file:

```
length reconstruction (16-sample chord sum): max diff 2.000 mm, mean diff 0.324 mm, records with diff > 1cm: 0
```

`measured`: `length` reconstructs to within 2 mm of the shipped value everywhere sampled — the
same order of magnitude as the path refit's own error, for the same reason (both are functions
of the same four control points, evaluated by the game's own formula). `length` is retained in
§5.4's candidate below anyway (it costs only 4 bytes/record and removes any need for a consumer
to implement `MathUtils.Length`'s 16-sample formula just to read a scalar), but it is recorded
here as **representation, not information**, exactly like §2.1's `TANGENT` finding.

**Verdict: `path` is pure representation of `curve.m_Bezier`, and the smallest lossless
encoding is smaller *and* adds information** — this is the "unusual and valuable result" this
task's brief predicted, tested rather than assumed, and it holds.

### 5.4 Smallest lossless encoding

**The cheap baseline first, per constraint 2 and the standard §4.4 set** — `lzma`/`zlib -9`
over the raw, unmodified file, full population, bit-exact round-trip asserted:

```
python "$SCRATCH/audit/lanes_compress.py" "$BUNDLE"
```

```
full file: 242,145,498 B (230.9 MiB)
  zlib -9: 64,376,733 B (61.4 MiB, 26.59%), round-trip bit-exact, 24.7 s
  lzma   : 42,393,124 B (40.4 MiB, 17.51%), round-trip bit-exact, 129.0 s
```

`measured`, exact, full population. `lzma` saves 199,752,374 B (190.5 MiB, **82.49%** of
`lanes.jsonl`, 4.08% of the whole bundle) — this is the number the structural candidate below
must beat, per §4.4's own standard ("a structural candidate that loses to `lzma` must say so
plainly").

**The structural candidate: ship `curve.m_Bezier` instead of its 9-point tessellation.** Fixed
core per record — `id` (u32, range confirmed §5.1), `kind` (u8, 6-value closed set, §5.1), four
Bézier control points (4×3 `f32`, §5.2's own field widths), `length` (f32, retained per §5.3's
bonus finding) — **actually built and read back as real bytes**, not just sized arithmetically
(the same standard §4.4 held its own fixed core to):

```
python "$SCRATCH/audit/lanes_binary_build.py" "$BUNDLE" "$SCRATCH"
```

```
built 680,610 records' fixed core in 45.1s
packed file: 38,794,770 B (37.00 MiB), 57.0 B/record
read-back + id/kind assertion passed
reconstruction check on 5,000 sampled lanes (45,000 points): worst per-coordinate diff after re-round to 3dp = 0.001000 m, lanes with diff > 1cm: 0
lzma over the packed binary: 18,738,148 B (17.87 MiB, 48.30% of packed), round-trip bit-exact, 13.5 s
```

`measured`, real bytes, real round-trip. The reconstruction check re-evaluates each packed
`f32` control-point set at the producer's own `t = i/8` grid, re-rounds to 3 decimal places
(`LaneWriter.Round`'s own rule), and compares to the shipped, already-rounded path — worst
diff **exactly one rounding unit (1 mm)**, i.e. the candidate reproduces the shipped precision
losslessly, not merely to within a tolerance.

**The fixed-core candidate alone already beats `lzma`-over-JSON, uncompressed:**

| encoding | size | vs. `lanes.jsonl` |
|---|---:|---:|
| as-shipped | 230.93 MiB | — |
| `lzma` over JSON (measured) | 40.43 MiB (17.51%) | −190.50 MiB (−82.49%) |
| Bézier fixed core (measured, uncompressed) | **36.998 MiB (16.02%)** | **−193.93 MiB (−83.98%)** |
| Bézier fixed core + `lzma` (measured) | **17.87 MiB (7.74%)** | **−213.06 MiB (−92.26%)** |

The uncompressed fixed core is 3.43 MiB smaller than compressed JSON (a 1.09× margin) — a
structural change beating a generic compressor even before compression is applied to the
structural change itself. Compressing the Bézier binary too (real bytes, real `lzma` call, both
measured) pushes it to 17.87 MiB, **2.26× smaller than `lzma`-over-JSON** and **12.9× smaller
than as-shipped** — 92.26% of the domain, 4.56% of the whole 4,894,194,530-byte bundle.

**The full candidate — every field, not just the fixed core.** Sparse optional columns, sized
`arithmetic` from §5.1's exact per-field presence counts and widths (not built/round-tripped
for real, unlike the fixed core above):

| field | records | width | bytes |
|---|---:|---:|---:|
| presence bitmask (`owner`/`secondary`/`flags-present`, 3 bits) | 680,610 | u8 (1 B) | 680,610 B |
| `owner` | 677,547 | u32 (4 B) | 2,710,188 B |
| `limit` | 132,883 | f32 (4 B) — derivable from `kind`, no presence bit needed (§5.1) | 531,532 B |
| `curviness` | 132,883 | f32 (4 B), same gate as `limit` | 531,532 B |
| `flags` | 272,161 | u8 count + u8 catalog index × 821,959 total instances | 1,094,120 B |
| **sparse total** | | | **5,547,982 B (5.29 MiB)** |

Plus a 50-entry flag-name catalog (negligible, <1 KiB). **Grand total: 44,342,752 B (42.29
MiB)**, `arithmetic` for the sparse portion, `measured` for the 37.00 MiB fixed core inside it.
This full candidate is **1.86 MiB larger than `lzma`-over-JSON uncompressed** (42.29 vs. 40.43
MiB) — reported plainly, the same honesty §4.4 applied to its own columnar candidate, because
the sparse columns here are small integers/floats that don't need JSON's punctuation overhead
but also aren't compressed the way `lzma` compresses everything together. Applying the fixed
core's own measured compression ratio (48.30%) to the full candidate as an **unmeasured
extrapolation, not an independent build**: ≈20.4 MiB, ≈1.98× smaller than `lzma`-over-JSON —
directionally the same win as the fixed-core-only comparison, but this specific number was not
built and compressed for real, and is reported at that lower confidence.

**Consumer cost.** Evaluating a cubic Bézier at a parameter `t` is the four-line function §5.2
quoted verbatim from the game's own source — trivial to port, and `docs/bundle-schema.md` §5B
already documents a curve-consuming layer (the `lanemeshes.jsonl` bend algorithm) that a
consumer may already have implemented for this exact bundle, so the marginal cost for a second
Bézier-consuming file is smaller still. A consumer that wants the old 9-point polyline gets it
back by evaluating at `i/8` for `i = 0..8` — the identical grid the producer used — or samples
at any *other* density it prefers, which the polyline-only form cannot offer. This is the
"strictly more information" half of §5.3's verdict made concrete: nine points is one specific,
fixed resampling of the truth; four control points are the truth, at any resampling a consumer
chooses.

**No lossy candidate is proposed for this domain.** The lossless Bézier candidate already
reaches a 92.26% reduction with a proven round-trip; per constraint 6 (investigate lossless
first) and the same reasoning §3 gave for `networks/`, there is no remaining gap large enough
to justify naming an unmeasured lossy fallback.

### Rip time

`lanes.jsonl` is written inside §8's row 3 (Lanes + Networks bracket, 12:15:31.929–12:23:17.290,
465.361 s) — cited, not re-derived. Row 3's only instrumented sub-steps are network segment mesh
baking (65.8 s) and network-pass texture readback (9.5 s, concurrent); §8 already notes that the
remaining 390.061 s of the bracket covers "lane collection, lane write, network edge topology,
and everything else not covered by an explicit duration" as one undivided remainder — lane
collection/write time specifically is `unmeasured` at finer grain than that combined figure,
the same limitation §8 already recorded for this bracket.

### A staleness note, checked directly rather than assumed

This section's measurements are of the bundle ripped 2026-08-07 11:50:23–12:23:24 (§8). Commit
`8985b73` ("Producer: tunnel lanes tagged, splatmap semantics string; awaiting re-rip
validation") landed at 13:54 that day — **after** this rip, raising the question of whether
these measurements are already stale against current producer code. Checked directly rather
than assumed from the commit message alone:

```
git show --stat 8985b73
```

The changed files are `Domain/LaneGeometryRecord.cs`, `IO/NetworkWriter.cs`,
`Systems/MaterialRipSystem.cs`, `Systems/MeshRipSystem.cs`,
`Systems/NetworkRipSystem.GameMesh.cs`, `Systems/NetworkRipSystem.cs`,
`Systems/TerrainRipSystem.cs`, plus docs/schema/tooling files. **`Domain/LaneRecord.cs`,
`IO/LaneWriter.cs` and `Systems/LaneRipSystem.cs` — the three files this section's own §5.1/§5.2
analysis reads — are not in that changed set.** The `tunnel: true` field the commit message
refers to is `LaneGeometryRecord.Tunnel` (`Domain/LaneGeometryRecord.cs:57`, confirmed by
`grep -n Tunnel`), which feeds `lanemeshes.jsonl` (§5B) — a different lane subsystem (drawn
utility/fence geometry) from this section's subject, `lanes.jsonl`'s drivable graph. So,
correcting the imprecision in this fact as it was initially framed: **`lanes.jsonl`'s own
producer path is byte-for-byte the same code today as it was at rip time** — a fresh rip today
would reproduce this section's field census, points-per-lane histogram and Bézier-refit
results unchanged. This bundle does still predate `8985b73`'s other changes (network/terrain/
mesh systems, none of which this section measures), so it is recorded here for the avoidance of
doubt which rip these specific numbers are of — 2026-08-07 11:50–12:23, not a later one — even
though, for `lanes.jsonl` specifically, there turns out to be nothing for a re-rip to change.

### Candidate rows

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| LANE-1 | Compress `lanes.jsonl` at rest (`lzma` or `zlib -9`) | measured, full file, `lzma` **190.50 MiB (82.49% of the file, 4.08% of bundle)**; `zlib -9` **169.53 MiB (73.41%, 3.63%)** | unmeasured (producer-side compress cost at write time; not timed) | one decompress call before parsing, same as INST-1 (§4) | low — ubiquitous codecs, bit-exact by construction, verified on the full file | YES, full-file bit-exact round-trip asserted | **new layer**: a compressed at-rest/transport encoding of the unchanged JSON bytes (`lanes.jsonl.xz`), needs the same §12.1 `compression` amendment as INST-1/MESH-1; **retires**: nothing, the canonical JSON bytes are recovered exactly on decompress |
| LANE-2 | Ship `curve.m_Bezier`'s 4 control points instead of the 9-point tessellated `path`, fixed core (`id`/`kind`/4×control-point/`length`) actually built and round-tripped, sparse `owner`/`limit`/`curviness`/`flags` columns sized arithmetically | fixed core **measured, 193.93 MiB (83.98% of the file, 4.15% of bundle), beating `lzma`-over-JSON by 3.43 MiB even uncompressed**; fixed core + `lzma` **measured, 213.06 MiB (92.26%, 4.56%), 2.26× smaller than `lzma`-over-JSON**; full candidate (all fields) uncompressed is 1.86 MiB *larger* than `lzma`-over-JSON, reported plainly — see body for the honest side-by-side; full candidate + `lzma` is an **unmeasured extrapolation** (≈210.5 MiB / 91.15%, ≈1.98× `lzma`-over-JSON) using the fixed core's own measured compression ratio, not an independent build | unmeasured (rip-time cost of reading `curve.m_Bezier` directly instead of tessellating it; `LaneRipSystem` already reads the same `Curve` component today, so this is likely a wash or a small saving, not timed) | small — evaluating a cubic Bézier is the 4-line function quoted in §5.2, and `docs/bundle-schema.md` §5B's `lanemeshes.jsonl` bend algorithm already exercises the identical primitive with a working reference consumer in `tools/blender_import_bundle.py` | low — the exact evaluation formula was confirmed identical to the game's own (§5.2), and the round-trip was checked on real packed bytes, not asserted (worst diff = 1 rounding unit, §5.4) | YES, proven by full-population refit (§5.3) plus a real packed-binary round-trip reproducing the shipped path to within its own 3-decimal rounding step (§5.4) | **new layer**: `lanes.bin` (or a `curve` field replacing `path`, schema-amendable under §12.1 the same way `lanemeshes.jsonl`'s `curve` field already exists) alongside `lanes.jsonl`, opt-in; **retires**: nothing until an A/B gate proves the reader reproduces the JSON form's polyline at matching sample density (same transitional-bundle caveat as INST-2/NET-1) — until then the transitional bundle is 242,145,498 + 44,342,752 = 286,488,250 B (273.2 MiB) for this domain, larger than today's `lanes.jsonl` alone, for as long as both ship together |

**Lossy candidates:** none proposed for this domain — LANE-2's lossless win (92.26% combined
with compression, or 83.98% even without) already exhausts the useful room, and per constraint
6 lossless is investigated first in any case, which this section already does exhaustively.

## 6. Markings and decals

Road markings are not a byte question in this bundle — they are already the parametric layer
§3.4/§5.4 hold up as the *target* shape for `networks/`/`lanes.jsonl`. `lanemeshes.jsonl` +
`lanemeshes/` (§5B v2 of `docs/bundle-schema.md`, cited, not re-derived) already ship bend
parameters plus 71 deduped straight-part meshes, not baked-per-instance geometry. What this
section adds is the number the earlier sections only referenced: the game's own draw cost for a
marking is a single instanced projector, while the reference consumer's cost for the *same* data
is a real, measured, disproportionate CPU expense — and that expense is a rip/import-time fact,
not a disk-space one. Following this task's brief, the two are kept apart rather than folded
into one "markings cost X" sentence.

**Where this section's headings answer the front matter's four questions**, since they are named
for this domain's findings rather than for the questions: question 2 (what we ship) is §6.1;
question 1 (what the game stores) is §6.3's first paragraph — one instanced projector box per
marking, positioned by a curve matrix, zero per-vertex CPU work at draw time; question 3
(information or representation) is §6.2 — the 29.4 MiB shipped is already bend parameters plus
71 deduped source meshes, the parametric form itself, so there is no baked-versus-derived delta
left to classify; question 4 (smallest lossless encoding) is §6.2's closure — there is nothing
to project onto a smaller encoding, which is why this domain contributes no candidate row to §9.

### 6.1 What we ship, and how much of it is markings

Command:

```
python - <<'PY'
import json, os, collections
from pathlib import Path
root = Path(os.environ["BUNDLE"])
laneprefabs = json.load(open(root / "laneprefabs.json", encoding="utf-8"))
decal_parts_by_prefab = {name: {i for i, p in enumerate(e.get("parts", [])) if p.get("decal")}
                          for name, e in laneprefabs.items()}
total = decal_records = tunnel_count = 0
total_by_category = collections.Counter(); decal_by_category = collections.Counter()
with open(root / "lanemeshes.jsonl", encoding="utf-8") as handle:
    for line in handle:
        rec = json.loads(line); total += 1
        total_by_category[rec.get("category", "?")] += 1
        if rec.get("tunnel"): tunnel_count += 1
        if any(p in decal_parts_by_prefab.get(rec.get("prefab"), set()) for p in rec.get("parts", [])):
            decal_records += 1; decal_by_category[rec.get("category", "?")] += 1
print(total, decal_records, dict(total_by_category), dict(decal_by_category), tunnel_count)
PY
```

`measured`, full population (119,302 of 119,302 records, cross-referencing every `parts` index
against `laneprefabs.json`'s own `decal` flag rather than trusting the `category` field alone):

| | |
|---|---:|
| `lanemeshes.jsonl` | 28,063,057 B (26.8 MiB) — matches §0's census exactly |
| `lanemeshes/` | 2,738,524 B (2.6 MiB), 71 deduped part meshes — matches §0's census exactly |
| **combined** | **30,801,581 B (29.4 MiB), 0.629% of the whole 4,894,194,530-byte bundle** |
| total records | 119,302 |
| records carrying ≥1 `decal`-flagged part (road markings) | **82,908 (69.5%)** |
| by category | `lane` 82,908, `fence` 26,240, `cable` 10,154 (no `pipe` in this city) |
| decal records by category | `lane` 82,908 — **exactly** the whole `lane` category, in both directions | 
| records tagged `tunnel: true` | 3,214 |

The `category: "lane"` field and "carries a decal part" turn out to be the same 82,908 records
in this bundle, checked directly rather than assumed — every `lane`-category record has ≥1
decal part and no `fence`/`cable` record does. This is the exact figure the brief's own framing
rounds to "~83k markings," and it is close to (not identical to — a different rip) the
83,110 the perf-refactor design doc measured on its own full-city run
(`docs/superpowers/plans/2026-08-07-import-performance-refactor.md:612-613`, cited below).

`tunnel: true` (3,214 records) is `Domain/LaneGeometryRecord.cs`'s new field, the change commit
`8985b73` names in its own message. It is already present in **this** bundle despite
`lanemeshes.jsonl`'s file mtime (12:22, inside the original 11:50–12:23 rip) predating the
commit's git timestamp (13:54) — the same build-predates-commit pattern §0's provenance note
documents for `terrain/`'s splatmap capture, confirmed here a second time on an independent
file. No re-rip is needed to pick this field up; it is already in the numbers above.

### 6.2 Byte cost: closed

**No candidate is proposed for this domain's bytes.** 29.4 MiB total, 0.629% of the bundle, is
already the smallest-representation form this document has been measuring toward for every
other geometry-heavy domain (§3.4's `networks/` projection, §5.4's `lanes.jsonl` Bézier): bend
parameters plus deduped source meshes, not per-instance baked geometry. There is nothing left to
project onto a smaller encoding — this closes the byte side of the brief's own framing.

### 6.3 Import-time cost: real, disproportionate, and filed here — not resolved by the obvious fix

**The game's own cost, for comparison.** `docs/cs2-api-audit.md` §19 (cited, not re-derived):
a lane marking's mesh UVs are never sampled by the shader at all — the game hands
`DecalProperties.m_TextureArea` from the prefab straight to `ManagedBatchSystem.CreateBatch`,
and the decal is drawn as **one instanced projector box per marking, positioned by a curve
matrix**, with zero per-vertex CPU work at draw time. §5B's bend algorithm (§ above) is already
this bundle's own reference implementation of the *matrix*, not the projector — a consumer that
wants the game's own draw cost implements an instanced-box decal shader, not a mesh.

**The reference consumer's cost is not that.** `tools/blender_import_bundle.py` builds and
drapes real per-vertex geometry for every marking instead, and the cost was measured, not
guessed, before this task (`docs/superpowers/plans/2026-08-07-import-performance-refactor.md:611-613`,
quoted verbatim): *"The game never bakes marking geometry: it draws one instanced projector box
per marking and projects it onto the depth buffer. Our baked-and-draped equivalent costs
2,240,914 verts, ~1.7 GB of Python lists and 2,240,914 BVH ray-casts for 83,110 markings."*
`measured`, from a full-city run of the tool, cited rather than re-run (re-running a full-city
Blender import is out of scope for this measurement-only task).

**That number is already stale — one optimization landed, one was tried and killed by
measurement.** `tools/blender_import_bundle.py:1070-1077` (current code, read in full):

```python
# Tier 1c (2026-08-07 amendment): the fixed _DECAL_DRAPE_STEP = 3.0 m subdivision drove both
# the vertex count and the ray-cast count of the old per-vertex drape (2.24M of each for
# 83,110 markings). Replaced by a sagitta rule: subdivide only as much as the bézier's own
# curvature demands, measured against the arc-table samples the bend already computes (free
# reuse, no extra bézier evaluation). ... Measured effect: 2.24M -> ~1.06M verts.
_DRAPE_SAGITTA_TOLERANCE = 0.02  # metres
```

A second, larger-looking optimization — one ray-cast per *marking* instead of per vertex, a
further 17× cut to ~132,395 ray-casts — was designed, implemented, and then **falsified by its
own A/B measurement and dropped**
(`docs/superpowers/specs/2026-08-07-import-performance-refactor-design.md:163-172`, quoted): *"One
drape sample per marking (17x fewer ray-casts). Measured inside Blender against the full imported
surface: errors up to 657 mm, 42/1092 samples beyond a marking's own lift. The road crown is not
the smooth profile the estimate assumed."* And the ray-cast **dedup** that made the per-vertex
form look 2× cheaper than it really was turned out to be unsound too — it was only exact because
the drape sampled the lane centreline rather than each vertex's own laterally-offset position, a
defect worth up to 205 mm on crosswalks; fixing it (sampling per vertex, as correctness requires)
removed the dedup, "leaving ~2.1x (sagitta subdivision alone)." **Current, landed state: ~1.06M
vertices and ~1.06M ray-casts** — both figures down from 2.24M by the sagitta rule alone, not by
either of the two more aggressive cuts the plan originally proposed.

**The wall-clock result is the honest, load-bearing number, and it is disappointing — reported
plainly, the same way §4 reported INST-1 beating INST-2 outright.** The same design doc's own
measured "Outcome" table, full-city run, before/after the sagitta change landed
(`docs/superpowers/specs/2026-08-07-import-performance-refactor-design.md:147-156`):

| Phase | Before | After |
|---|---:|---:|
| Lane geometry (119,302 records — cable + fence + the 82,908 markings, not markings alone) | 374.9 s | **381.8 s** |

`measured`. A **~2.1× cut in vertex and ray-cast count produced no wall-clock win** — the phase
is 6.9 s *slower* after the optimization landed, within the noise of a full-city Blender run but
certainly not the speedup a naive "half the ray-casts, half the time" model predicts. Two
follow-up techniques that might actually close the gap — instancing one shared quad per marking
(borrowing the game's own structure, named "Tier 2" in the design doc) and a bundle-contract
change to stop baking `networks/` the way NET-1 (§3.4) already sizes — are **named, not built**
(`docs/superpowers/specs/2026-08-07-import-performance-refactor-design.md:182-184`, "Follow-ups
recorded, not done here").

**Closed for this task's purposes**, per the brief's own pass criterion: the marking import-time
cost is stated in the units it was measured in (vertices: 2.24M → 1.06M; ray-casts: 2.24M →
1.06M; wall-clock: 374.9 s → 381.8 s for the whole lane-geometry phase, per the "Outcome" table
above, `docs/superpowers/specs/2026-08-07-import-performance-refactor-design.md:147-156`) and
lives here, in §6/§8, never in a bytes column. No new measurement was performed in this task
beyond the cross-checked
census in §6.1 — the import-time figures above are cited from the already-landed, already-A/B-
tested perf-refactor work, not re-derived, per this task's own scope (measurement-only, no
producer or harness code changes).

## 7. Terrain

**This section measures the current, on-disk state of `terrain/` — the 21:34:25–21:36:17 rerip,
not the 11:50–12:23 rip §0's census table rows for every other domain.** §0's provenance note
(read first) establishes why: `terrain/` was re-ripped in isolation after this bundle's main
rip, and the pre-rerip bytes of the three files that changed no longer exist to measure. File
count is unchanged (41, matching §0's census exactly) and 38 of the 41 files are confirmed
identical to the original rip by SHA-256 (§0's provenance note, checked against the
`spike-baseline-2026-08-08` manifest); only `splatmap.png`, `water_level.png` and
`heightmap.png` differ — by exactly enough to account for the whole size delta, per the
inherited (unmeasured by this task) byte counts §0 labels as such. Command:

```
ls -la "$BUNDLE/terrain" && cat "$BUNDLE/terrain/rasters.json"
```

Current total: **70,208,400 B (66.96 MiB), 41 files** — down from the census's 72,050,547 B
(68.7 MiB), a **2.56% reduction**, all of it in the three files above. As a share of the
4,894,194,530-byte census total this domain moves from 1.472% to 1.435% — the kind of change
that does not alter this document's ranking of anything, but is stated exactly rather than
waved at.

**Where this section answers the front matter's four questions**, since it carries no separate
"information or representation?" heading: question 2 is §7.1, question 1 is §7.2, and question 4
is §7.3. Question 3 is answered inside §7.2 and §7.4 — the heightmap is representation (§22 row
15: it ships verbatim, a match with no runtime component), while the splatmap is not a pure
function of static terrain state at all: its own shipped `rasters.json` semantics string names a
**noise** input, and §7.4 measures two non-code deltas (9,828,541 vs 9,828,621 wet cells;
27,422 vs 27,421 surface areas) showing two captures of the same save are not identical
simulation moments. That makes the splatmap a *render*, not a re-encoding of something smaller —
which is why §7 proposes only an encoding candidate (TERRAIN-1) and no deriving-instead-of-baking
one.

### 7.1 What we ship

Six top-level rasters (`heightmap.png`, `water_level.png`, `splatmap.png`, `user_splatmap.png`,
`surface_index.png`, `cutout.png`) plus `rasters.json` (geo-referencing and semantics for each)
and `terrain/surfaces/` (33 ground-texture PNGs — diffuse + tangent-normal per core ground,
diffuse + normal per painted "extra" ground — plus an `index.json` catalogue; 6 + 1 + 33 + 1 =
41, matching §0's file count exactly). This is a materially richer layer than the
brief's own opening description ("69 MB of rasters") anticipated — the splatmap/user-splatmap/
world-heightmap capture and the normal-map/extra-ground surfaces are all new since Phase 6 was
last measured, landed the same day as this bundle's rip (§0's provenance note; `docs/cs2-api-audit.md`
§22a). None of that is re-litigated here — §17/§20/§22/§22a already validate the roadbed,
cutout, water-level decode and splatmap mechanisms rule-by-rule against decompiled source,
cited rather than re-derived.

### 7.2 What the game stores

Not re-derived — `docs/cs2-api-audit.md` §17, §20, §22 and §22a already did this rule-by-rule
against `Game.Simulation.TerrainSystem`/`WaterUtils`/`TerrainMaterialSystem`, and this task's
brief scopes terrain as a confirm-and-close domain, not a re-audit. The one fact worth repeating
because §7.3 depends on it: §22a's own semantics block (verbatim in `terrain/rasters.json`,
§7.1) states the splatmap is *"GPU-generated ground-blend weights (height variance, rock/grass
limits, noise, user brush)"* — a per-pixel render with a **noise input**, not a pure function of
static terrain state the way the heightmap is (§22 row 15: heightmap "ships... verbatim", a
**match** with no runtime component named).

### 7.3 Smallest lossless encoding

**Pure zlib-9 floor (filter unchanged, compression level only) — already closed, by a wide
margin.** Command:

```
python - <<'PY'
import os, zlib
from pathlib import Path
import numpy
from PIL import Image
root = Path(os.environ["BUNDLE"]) / "terrain"
for path in sorted(root.glob("*.png")):
    with Image.open(path) as image:
        pixels = numpy.asarray(image)
    raw = pixels.tobytes()
    z9 = len(zlib.compress(raw, 9))
    print(path.name, path.stat().st_size, z9, f"{z9/path.stat().st_size*100:.1f}%")
PY
```

`measured`, full population (39 PNGs — 6 top-level, `rasters.json`/`index.json` are not PNGs and
are excluded, plus the 33 `surfaces/` PNGs; this rip carries no `world_heightmap.png`, §7.1):

| | shipped | zlib-9 (filter unchanged) | ratio |
|---|---:|---:|---:|
| 6 top-level rasters | 16.03 MiB | 15.56 MiB | 97.1% |
| `surfaces/` (33 PNGs) | 50.93 MiB | 50.05 MiB | 98.3% |
| **`terrain/` total** | **66.95 MiB** | **65.60 MiB** | **98.0%** |

**Closed: 2.0% headroom, the same order of magnitude as §1's
textures at the same compression-level-only step (textures: 8.0% at adaptive+zlib-9 combined;
terrain, filter-unchanged alone: 2.0%) and far below anything worth a candidate row on its
own.**

**Adaptive per-row filtering — the brief's other half of the question, and the answer is not
the one §1 found.** §1.1 established the house PNG encoder (`IO/Png.cs`) writes filter "None"
on every scanline with `Deflater.DEFAULT_COMPRESSION`, and §1.4 measured that switching to
per-row adaptive filtering (still lossless — every variant below decodes bit-identically, or
does not count) saved textures/ 8.0% at zlib-9. Terrain rasters are a different kind of image
(large smooth gradients and near-flat blends rather than photographic detail) — tested directly
rather than assumed to behave the same way:

```
# per file: encode with PIL's adaptive per-row filter heuristic at zlib 6 and zlib 9,
# assert numpy.array_equal(decoded, original) for every file before counting it
```

`measured`, full population (39 files, all round-trips bit-exact):

```
TOTAL as-shipped:   66.95 MiB (70,207,101 B)
TOTAL adaptive+z6:  69.72 MiB (73,107,969 B)  104.1% of shipped  -- WORSE
TOTAL adaptive+z9:  68.84 MiB (72,188,894 B)  102.8% of shipped  -- WORSE
```

**A blanket "adaptive filter everything" policy is a net loss for this domain** — the opposite
of §1's result. Cause, visible directly in the per-file breakdown: 27 of the 33 `surfaces/`
files (ordinary ground-texture atlases — grass, sand, tile, concrete) get *larger* under
adaptive filtering, several by 30–45% (`area_Pavement_Surface_01.png`: 0.58 → 0.79 MiB, +36%),
because PIL's per-row filter heuristic (minimum sum of absolute differences) is not the same
thing as minimum *compressed* bytes, and these textures' high-frequency natural detail defeats
the decorrelation filters try to exploit — filter "None" plus a good deflate pass is already
close to optimal for this specific content, the reverse of the photographic-texture case §1
measured.

**But 11 of the 39 files genuinely do benefit, `heightmap.png` by a wide margin** — checked
per-file rather than accepting the blanket-policy loss as the final word (a per-file `min(as-
shipped, adaptive+zlib-9)` choice, the same "ship whichever representation is smallest, per
file" logic §1's TEX-3+4 joint row already used):

| file | shipped | adaptive+zlib-9 | saved |
|---|---:|---:|---:|
| `heightmap.png` | 4.44 MiB | 3.07 MiB | **30.8%** |
| `surfaces/rock.png` | 2.22 MiB | 1.81 MiB | 18.7% |
| `user_splatmap.png` | 1.02 MiB | 0.84 MiB | 18.3% |
| `surfaces/extra4_normal.png` | 1.95 MiB | 1.61 MiB | 17.3% |
| `surfaces/rock_normal.png` | 3.07 MiB | 2.70 MiB | 12.0% |
| `water_level.png` | 0.74 MiB | 0.66 MiB | 11.7% |
| `splatmap.png` | 9.58 MiB | 8.68 MiB | 9.4% |
| `surfaces/extra3_normal.png` | 2.75 MiB | 2.48 MiB | 9.6% |
| `surfaces/dirt_normal.png` | 2.99 MiB | 2.76 MiB | 7.5% |
| `surface_index.png` | 0.22 MiB | 0.17 MiB | 23.0% |
| `surfaces/dirt.png` | 2.29 MiB | 2.16 MiB | 5.6% |

`measured`, exact, full population, every round-trip bit-exact. Selective total: **62.62 MiB,
saved 4.33 MiB (6.47% of `terrain/`, 0.09% of the whole bundle)** — real, lossless, and small in
absolute terms, exactly the shape the brief anticipated ("expected to yield nothing"), except
that it is not quite nothing: `heightmap.png`'s 16-bit grayscale gradient is genuinely
filter-friendly (a smooth, mostly-monotonic elevation field is close to ideal input for the Up/
Paeth predictors), unlike the photographic `surfaces/` textures beside it in the same directory.

### Candidate rows

**Lossless candidates:**

| id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless? | additive layer |
|---|---|---|---|---|---|---|---|
| TERRAIN-1 | Selective adaptive per-row filtering + zlib-9, applied only to the 11 of 39 files where it measurably wins (dominated by `heightmap.png`'s 30.8%); leave the other 28 (mostly `surfaces/` ground textures) at filter-None as shipped | measured, full population, **4.33 MiB (6.47% of `terrain/`, 0.09% of the whole bundle)** — a blanket policy across all 39 files is a **loss** (−2.82%), reported plainly rather than rounded up to a bigger, wrong number | unmeasured (needs a C#-side timed re-rip, same caveat as TEX-1/TEX-2, MESH-1) | none — in-place re-encode of unchanged files at unchanged paths, decodes with any standard PNG reader, same shape as TEX-1/TEX-2 | low | YES, every file's round-trip individually asserted bit-exact | not applicable — in-place re-encode; no new layer, nothing retired |

**Lossy candidates:** none proposed. TERRAIN-1's ceiling (6.47% of a domain that is already
1.4% of the bundle) does not justify investigating a lossy fallback, and constraint 6 requires
lossless to be exhausted first in any case — which the zlib-9-floor and adaptive-filter tests
above already do for this domain.

### 7.4 The splatmap shrink: what changed between the two captures, and what did not

**The premise, stated precisely, and with its provenance carried forward rather than
re-asserted.** `splatmap.png` is reported 1,839,661 B smaller in the current (21:36) capture
than in the original (12:23) one — 11,881,840 → 10,042,179 B, a 15.5% drop, by far the largest
of the three files the SHA-256 manifest (§0's provenance note) confirms changed. `water_level.png`
(−2,485 B, 0.3%) and `heightmap.png` (−1 B, ~0%) barely moved in comparison. **The *which-files-
changed* fact is checksum-confirmed; the specific *before* byte counts feeding this 15.5% figure
are the `unmeasured`, inherited numbers §0 labels as such** (observed via `ls -la` before the
overwrite, recorded only in an uncommitted ledger) — carried here rather than re-derived, since
the source bytes no longer exist to re-measure. This subsection asks why the drop happened, per
this task's own instruction: determine it from evidence if determinable, and say plainly what
was ruled out if it is not.

**Ruled out with certainty: the "splatmap semantics string" wording.** Commit `8985b73`'s own
message names a "splatmap semantics string" change. That string is the `semantics` key inside
`terrain/rasters.json`'s `splatmap` block (§7.1's sample, quoted in full in §7.2) — plain JSON
text in a file that is not `splatmap.png`. A wording edit to a JSON string in one file cannot
change the byte count of a different, binary PNG file. This is not measured, because it does
not need to be — it is ruled out by inspection of what the two files *are*.

**Ruled out by the visible commit history: any other tracked producer code change.** Checked
directly:

```
git log --all --oneline -- Systems/TerrainRipSystem.cs
```

→ `0ad936b` (21:48:30), `8985b73` (13:54:36), and three earlier commits, all before the rip.
`0ad936b` also touches `Systems/TerrainRipSystem.cs`, but (1) its diff is entirely about
`areas.jsonl`'s `heightRanges`/`uvScale` fields, not splatmap capture — confirmed by reading the
diff, no `Capture*`/`splat` line appears in it; (2) its own commit message states outright
*"Lane, network and terrain layers unchanged: PASS 18 count line(s) identical, PASS frac=1.00000
max=0"* — an explicit, self-reported A/B pass claiming zero pixel difference on the terrain
layers it did not touch; and (3) it is timestamped 21:48:30, **twelve minutes after** the
21:34–21:36 terrain-only rerip finished, so it is causally incapable of having affected that
capture's output regardless of its content. `8985b73` is the only remaining candidate, and
§0's provenance note already established that its `CaptureSplatmap`/`CaptureUserSplatmap` code
was already running during the *original* 11:50–12:23 rip — before the commit landed — so no
code change visible in git separates the two captures.

**Corroborating, not conclusive: both rips' own terrain report text is near-identical, with two
small numeric exceptions that are not code-shaped.** The original rip's terrain report
(`docs/cs2-storage-audit-riplog-2026-08-07.txt`, the preserved copy, lines 246–255) and the
rerip's own log (`log-timeline.txt`, cited in §0) both print the same nine-line report structure
— `Roadbed:`, `Cutout:`, `Water:`, `Surfaces:`, `Splatmap:`, `User splatmap:`, `World terrain:`,
`Surface areas:`, `Output:` — with the same `splatMultiplier`/`splatPower` (2.00/1.00) and the
same `4096x4096` dimensions in both. Two counts differ by a small margin: `Water: 9828541` (original)
vs `9828621` wet cells (rerip), and `Surface areas: 27422` vs `27421`. Both are simulation-derived
counts, not producer-code outputs, and both differing by a handful out of millions/tens-of-
thousands is exactly what "the same save, reloaded at a different real-world moment, with the
simulation having ticked forward some amount in between" would produce — the `Roadbed:` and
`Cutout:` lines, which depend only on static network/composition geometry rather than live
simulation state, are **byte-for-byte identical** between the two rips (6096/10805/0/1774270 and
7900/165/252/0/266 respectively, both runs).

**Verdict: `unmeasured`, with the search space narrowed to a specific, named cause rather than
left open.** The semantics-string hypothesis is ruled out with certainty; every producer-code
hypothesis visible in git is ruled out by the diff evidence and timing above. What remains is
that the splatmap is documented, in its own shipped `rasters.json` semantics string, as
depending on a **noise** input the heightmap's own "match, ships verbatim" status (§22 row 15)
does not carry — and the two small non-code deltas above (water cell count, area count) are
direct, measured evidence that the two capture sessions are not bit-identical simulation
moments, even reloading what is presumably the same save. Whether the noise term (or some other
runtime-only input the splat shader consumes) is the actual mechanism behind a 15.5% pixel-
content difference on a 4096×4096 render was not, and could not be, verified further: the
original 11,881,840-byte `splatmap.png` no longer exists on disk to diff pixel-by-pixel against
the current one (§0's provenance note), and reproducing it would require a fresh rip this task's
scope does not include. Recorded here, not guessed at, for whichever later task next touches
splatmap semantics or attempts a byte-reproducible terrain rip.

## 8. Rip time, by phase

Every timestamped log line in `Carto.log` (`grep -nE '^\[2026' "$LOG"`) plus the two explicit
in-line durations the rip logs for its heaviest steps. The rip ran as one uninterrupted mod
lifecycle from `Mod instance loaded` to `Mod instance had been disposed of`; there is no log
line marking a separate "rip triggered" event, so the first phase's bracket start is the mod
load banner itself — if the user left the mod idle after load before triggering the rip, that
idle time is included in the Instances bracket below and this is *not* separable from the log
alone.

| # | phase | start | end | seconds | type |
|---|---|---|---|---:|---|
| 1 | Instances (collect + write `instances.jsonl`, `colors.json`, `lights.json`; mesh-phase progress-indicator setup) | 11:50:23.206 | 11:55:54.823 | 331.617 | bracket |
| 2 | Meshes (mesh export + concurrent VT texture/material readback for mesh prefabs) | 11:55:54.823 | 12:15:31.929 | 1177.106 | bracket |
| 3 | Lanes + Networks (lane collect/write, network topology, network segment mesh bake, network materials/textures) | 12:15:31.929 | 12:23:17.290 | 465.361 | bracket |
| 4 | Terrain | 12:23:17.290 | 12:23:24.328 | 7.038 | bracket |
| — | **TOTAL** (mod loaded → mod disposed) | 11:50:23.206 | 12:23:24.328 | **1981.122** | bracket |

Rows 1–4 sum to 1981.122 s, matching the total bracket exactly (318.236 + 13.381 + 1177.106 +
465.361 + 7.038 = 1981.122; the 13.381 s between `colors.json`/`lights.json` being written and
the first mesh-phase progress line is folded into row 1's end boundary).

Within those brackets, four durations are logged explicitly by the rip itself and are
`instrumented`, not bracket-derived — but each is a *sub-step* of its row, not the whole row, so
they do not replace the bracket and are not additive with each other:

| instrumented sub-step | seconds | type | which row it falls inside |
|---|---:|---|---|
| Mesh writing: `Wrote 4198 .glb, skipped 120, failed 0, in 1178.3 s` | 1178.3 | instrumented | row 2 (1177.106 s bracket — the two agree to within 1.2 s) |
| Mesh-pass texture readback: `Texture bytes: 1853.6 MiB; readback held the main thread 347.1 s` | 347.1 | instrumented | row 2, concurrent with the 1178.3 s above, not sequential |
| Network segment mesh baking: `Wrote 8259 segment(s) to networks.jsonl in 65.8 s` | 65.8 | instrumented | row 3 (465.361 s bracket; 465.361 − 65.8 − 9.5 = 390.061 s of row 3 is lane collection, lane write, network edge topology, and everything else not covered by an explicit duration) |
| Network-pass texture readback: `Texture bytes: 54.9 MiB; readback held the main thread 9.5 s` | 9.5 | instrumented | row 3, concurrent with other row-3 work |

**Preserved evidence.** The live `Carto.log` this table is built from was truncated by a game
relaunch at 21:34 (§0's provenance note) — it now holds only the 21:34–21:36 terrain-only
rerip's 14 lines, not the 257-line log every number above comes from. A full copy of that
original log survives as `docs/cs2-storage-audit-riplog-2026-08-07.txt`, committed alongside
this document specifically so the numbers in this section (and §0/§1/§2's citations of it)
remain independently checkable after `$SCRATCH` is discarded. This is a deliberate, one-time
exception to this task's own "only `docs/cs2-storage-audit.md` is committed" rule — no other
non-Markdown file is committed by this task.

### Phase shares, and which candidates move which phase

The denominator for every "est. rip-time saved" cell in §9. Shares are each row's seconds over
the 1,981.122 s total above; they sum to 100.000%.

| # | phase | seconds | share of the rip | type | candidates that would move it |
|---|---|---:|---:|---|---|
| 1 | Instances | 331.617 | 16.739% | bracket | INST-1 (compress at write time), INST-2 (a binary writer replaces JSON serialisation), INST-3/INST-4 (inside INST-2) |
| 2 | Meshes (+ concurrent mesh-prefab texture/material readback) | 1177.106 | 59.416% | bracket | MESH-1 (compress each `.glb` at write time), MESH-2 (fewer attribute bytes to serialise), MESH-3 (1,436 skirt files never built or written), TEX-1/TEX-2/TEX-3/TEX-4/TEX-6 (PNG encode runs on the same single serial writer thread as mesh writing, §1's Rip time) |
| 3 | Lanes + Networks | 465.361 | 23.490% | bracket | NET-1 (replaces the 65.8 s segment bake with ~45 geometry writes), NET-2 and LANE-1 (compress at write time), LANE-2 (reads `curve.m_Bezier` directly instead of tessellating it) |
| 4 | Terrain | 7.038 | 0.355% | bracket | TERRAIN-1 |
| — | **TOTAL** | **1981.122** | **100.000%** | bracket | — |

The four instrumented sub-steps, as shares of the same denominator (they are sub-steps of their
rows, two of them concurrent with other work in the same row, so they are not additive with each
other or with the bracket rows above):

| instrumented sub-step | seconds | share of the rip | inside |
|---|---:|---:|---|
| Mesh writing (`Wrote 4198 .glb … in 1178.3 s`) | 1178.3 | 59.477% | row 2 |
| Mesh-pass texture readback (`readback held the main thread 347.1 s`) | 347.1 | 17.520% | row 2, concurrent with mesh writing |
| Network segment mesh baking (`Wrote 8259 segment(s) … in 65.8 s`) | 65.8 | 3.321% | row 3 |
| Network-pass texture readback (`readback held the main thread 9.5 s`) | 9.5 | 0.480% | row 3, concurrent |
| Row 3's undivided remainder (lane collect, lane write, network edge topology, everything else) | 390.061 | 19.689% | row 3 |

**Every candidate's rip-time effect is `unmeasured`, and for the compression family the *sign* is
unmeasured too.** No candidate in §9 has been built, so none has been timed; what this table
supplies is the denominator and the ceiling. A candidate cannot save more rip time than its
phase's share (MESH-1/MESH-3/TEX-* are bounded by row 2's 59.416%; NET-1's own baking sub-step is
3.321%; INST-* by row 1's 16.739%; LANE-* by row 3's 23.490%; TERRAIN-1 by row 4's 0.355%), and
four candidates — MESH-1, NET-2, INST-1 and LANE-1, i.e. every member of §9's COMP-1 family — add
encode CPU at write time inside rows 1–3 (99.645% of the rip combined) rather than removing it.
Deciding whether that added cost is material needs a C#-side timed re-rip, which Phase A's
measurement-only mandate excludes; §1 and §2 both record why this document declines to estimate
it from Python/Pillow or Python `zlib`/`lzma` timings.

### Consumer import time — markings (§6)

Not a rip-time number (the rip never builds per-vertex marking geometry — §6.3) but the
*consumer* import-time analogue, filed here per this task's brief rather than in §6's bytes
column. Measured by the perf-refactor work, cited in full in §6.3: the reference Blender
importer's baked-and-draped equivalent of the 82,908 shipped markings cost **2,240,914
vertices and 2,240,914 BVH ray-casts** before 2026-08-07's optimization pass; a landed
curvature-based subdivision rule (`_DRAPE_SAGITTA_TOLERANCE`, `tools/blender_import_bundle.py:1077`)
cut both to **~1.06M** (measured, ~2.1×), while a second, larger-looking cut (one ray-cast per
marking instead of per vertex, a further 17×) was designed, tried, and **dropped** after its own
A/B measurement found errors up to 657 mm against the game's actual road surface (§6.3, quoted
in full). The wall-clock result of the landed optimization, full-city, **measured**
(`docs/superpowers/specs/2026-08-07-import-performance-refactor-design.md:147-156`, the same
"Outcome" table §6.3 quotes): the lane-geometry import phase (119,302 records, markings plus
fence/cable geometry) went from **374.9 s to 381.8 s** — no measured speedup despite the ~2.1×
reduction in vertex/ray-cast count. None of this is a rip-time cost — the producer ships bend
parameters, not baked meshes,
in either state — it is the cost a *consumer* pays to turn those parameters into the per-vertex
geometry Blender needs, which the game itself never pays because it draws a marking as one
instanced projector box (§6.3, `docs/cs2-api-audit.md` §19).

## 9. Ranked candidates

Every candidate row from §1–§7, made comparable and ranked. **The sort key is absolute bytes
saved against §0's 4,894,194,530-byte baseline**, not the share of its own domain: §3's networks
win is 99.19% of `networks/` but 8.60% of the bundle, and a table sorted on domain-percentage
would put it above candidates worth five times more. Domain share is carried as a secondary
figure inside each row's byte cell.

**Provenance caveat — the bundle this table ranks is not one rip, and no single current-code
bundle exists.** §0's provenance note establishes it in full; restated here once, plainly,
because the table must not be read as measured against one homogeneous artifact: `terrain/` was
re-ripped in isolation at 21:34:25–21:36:17 with code at or after commit `8985b73`, while every
other layer is from the 11:50:23–12:23:24 rip that preceded that commit; the census total the
percentages divide by (4,894,194,530 B) is the bundle as censused at the end of that main rip,
and `terrain/` has since shrunk by 1,842,147 B, which would make a re-assembled bundle
4,892,352,383 B today. §7 measures the 21:36 state (the earlier bytes of the three changed files
no longer exist to measure); §1–§6 measure the 11:50–12:23 state. The rip log every §8 number
comes from survives only as the committed copy `docs/cs2-storage-audit-riplog-2026-08-07.txt`,
because the live `Carto.log` was truncated by the 21:34 relaunch. A future full re-rip under
post-`8985b73` code could move §2's, §3's and §6's byte counts; §4's and §5's would not move
(that commit touches no file in their producer paths, §0). No row below is a measurement of a
single current-code bundle, and none should be quoted as one.

### 9.1 How not to double-count these rows

Four overlap families exist. Adding rows across a family double-counts bytes; this already
happened once inside §1, where the naive TEX-3 + TEX-4 sum claimed 440.2 MiB against a joint
single-pass measurement of 377.1 MiB.

1. **The compression family — one policy, not four wins.** MESH-1, NET-2, INST-1 and LANE-1
   are the *same technique* (generic compression at rest) applied to the four layers that
   currently ship uncompressed (`meshes/` §2.4, `networks/` §3.5, `instances.jsonl` §4.4,
   `lanes.jsonl` §5.4). They touch disjoint files, so their bytes do add — but they are one
   decision, not four, and are presented as the single policy row **COMP-1** with the four
   named as its components. Cite COMP-1 or its components, never both.
2. **The texture ladder is nested, not additive.** TEX-1 ⊂ TEX-2 ⊂ TEX-3 ⊂ TEX-3+4 joint: TEX-2
   is TEX-1 plus zlib-9, TEX-3 is TEX-2 plus the safe channel reduction, and the joint row is the
   deepest form each file qualifies for (§1.4, §1's candidate block). Only the deepest row a
   programme actually adopts counts.
3. **A structural candidate and a compression candidate on the same layer are alternatives.**
   INST-1 versus INST-2 on `instances.jsonl`; LANE-1 versus LANE-2 on `lanes.jsonl`; MESH-1
   versus MESH-2 on `meshes/`. Their joint values are `arithmetic` bounds, not measurements,
   except where a section actually built and compressed the structural form:
   - `lanes.jsonl`: **measured** at the fixed-core level — Bézier fixed core + `lzma` is 17.87
     MiB, saving 213.06 MiB, 2.26× smaller than `lzma`-over-JSON (§5.4). The *full* candidate
     plus `lzma` (≈20.4 MiB, ≈210.5 MiB saved) is an **unmeasured extrapolation** using the
     fixed core's own measured ratio. Compressing a Bézier payload does not save what
     compressing the JSON saves, and §9.2's LANE-2 row sorts on the complete-candidate,
     uncompressed figure (188.6 MiB) rather than on either compressed figure, so the sort key is
     never an unmeasured number.
   - `instances.jsonl`: **no joint measurement.** Bound, `arithmetic`: a columnar-then-compressed
     form saves more than the larger of INST-1 (272.1 MiB) and INST-2 (199.0 MiB) and less than
     the whole file (309.2 MiB).
   - `meshes/`: **no joint measurement**, and §2.4 states explicitly that quantized data
     compresses differently from raw f32. Bound, `arithmetic`: more than MESH-1's 1,581.5 MiB,
     less than the whole directory (1,716.8 MiB). MESH-2 is lossy in any case and appears only
     in §9.3.
4. **NET-1 versus NET-2 on the same 404.8 MiB.** `networks/`'s 7,248 `.glb` files ship with raw
   BIN chunks exactly as `meshes/` does, and §3.5 now measures them: `lzma` 300.4 MiB (74.20% of
   the directory), `zlib -9` 238.5 MiB (58.92%), 200-file sample, bit-exact round-trip asserted,
   stated extrapolation rule. **NET-2 is therefore COMP-1's fourth component, and NET-1 and NET-2
   are alternatives on the same bytes — never a sum.** NET-1's decision-relevant figure is its
   saving *net of* NET-2: 101.1 MiB, `arithmetic` (§3.5), 3.97× smaller than its 401.5 MiB
   standalone headline. Both figures appear in NET-1's row below. `textures/` remains outside the
   policy by construction: §1.1 established its PNGs already run through `Deflater`, unlike the
   `.glb` BIN chunks of `meshes/` (§2.4) and `networks/` (§3.5), so the texture rows keep their
   full value under COMP-1.

### 9.2 Lossless candidates, ranked by absolute bytes saved

| id | domain | change | est. bytes saved | provenance | est. rip-time saved | consumer cost | risk | lossless? | additive-layer shape |
|---|---|---|---|---|---|---|---|---|---|
| COMP-1 | bundle-wide (`meshes/`, `networks/`, `instances.jsonl`, `lanes.jsonl`) | One at-rest compression policy (`lzma`) over the four layers that currently ship uncompressed — MESH-1 + NET-2 + INST-1 + LANE-1 as a single decision | **2,344.5 MiB (50.23% of the bundle)** = 1,581.5 + 300.4 + 272.1 + 190.5; the four layers are 2,661.8 MiB today (57.03% of the bundle) and 317.2 MiB after. `zlib -9` variant: 2,117.6 MiB (45.37%). **Supersedes this row's first-draft scope of 2,044.1 MiB / 43.80% over three layers, which omitted `networks/` because §3 had not measured it — kept visible here so the change is traceable; §3.5 supplies the missing 300.4 MiB** | measured — four independent bit-exact round-trips; `meshes/` and `networks/` by 200-file samples extrapolated on stated rules (§2.4, §3.5), both JSONLs full-file exact (§4.4, §5.4) | unmeasured, sign included — adds encode CPU inside rows 1–3 (99.645% of the rip, §8) rather than removing it | 1 line: one standard-library decompress call (repeated per layer, not four distinct implementations) | low — ubiquitous codecs, bit-exact by construction, verified on real bytes in all four layers | YES | **new layer**: compressed at-rest encodings beside the canonical bytes (`.glb.gz`/`.xz` or one archive per directory, using the already-bundled `ICSharpCode.SharpZipLib`), declared by a `compression` field amendment under §12.1; **retires**: nothing — the canonical bytes are recovered exactly |
| MESH-1 | `meshes/` | Compress `.glb` binary buffers at rest (BIN chunks are written raw today) | 1,581.5 MiB `lzma` (33.89% of bundle, 92.1% of `meshes/`); 1,457.8 MiB `zlib -9` (31.23%, 84.9%). **Component of COMP-1 — do not add to it** | measured (200-file sample, actually compressed, bit-exact round-trip asserted, stated linear extrapolation) | unmeasured; bounded by row 2's 59.416% share, and expected to add write-time CPU | 1 line: one decompress call before parsing | medium — a container-format change, not an in-place one; the codec risk itself is near-zero | YES, round-trip asserted on the sample | **new layer**: compressed encoding of the unchanged `.glb` bytes, `compression` field under §12.1; **retires**: nothing |
| NET-1 | `networks/` | Composition layer: per-segment composition id(s) + curve instead of a baked per-segment `.glb`, mirroring the shipped `lanemeshes.jsonl`/`lanemeshes/` v2 precedent | 401.5 MiB (8.60% of bundle, 99.19% of `networks/`) **standalone, against today's uncompressed `networks/`**; still 390.1 MiB under a joint 5× record-cost and 4× geometry-count correction. **Net of COMP-1's NET-2 component, which captures 300.4 MiB of the same directory for one decompress call, its marginal saving is 101.1 MiB (2.17% of the bundle), `arithmetic` (§3.5) — 3.97× smaller, and this is the figure that decides the 664-line port** | arithmetic — projected from the precedent's two measured per-unit costs; no composition-layer file was built or round-tripped | unmeasured; its target is row 3's instrumented 65.8 s segment bake, 3.321% of the rip | 664 lines: most of `Systems/NetworkRipSystem.GameMesh.cs` (§8's 17 rules + §21's 12 refinements) — the largest port this audit sizes | high — correctness-critical junction/vertex-sync/roundabout geometry; the reference implementation exists and is proven faithful | YES — every rule checked in §8/§21 is `match` or `fixed`; the two shader-only gaps are already in today's baked mesh | **new layer**: composition geometry set + per-segment curve records; **retires**: `networks/*.glb` and the segment-baking path, only after an A/B gate — until then both ship, making the transitional bundle 408.1 MiB for this domain instead of 404.8 MiB |
| TEX-3+4 joint | `textures/` | One representation per file: 2-channel `normal2` where a file is alpha==255 and normal-derivable, RGB/L where only alpha==255, adaptive-filtered RGBA otherwise | 377.1 MiB (8.08% of bundle, 19.98% of `textures/`) — measured jointly in one pass, **not** the 440.2 MiB naive TEX-3 + TEX-4 sum | measured (500-file sample, every file decoded and reconstructed bit-exact, stated 7.1702× extrapolation) | unmeasured; PNG encode is serialised into row 2's 59.416% share (§1's Rip time) | 31 lines for the `normal2` subset (`Systems/MaterialRipSystem.cs:1488-1518` is the same reconstruction in reverse); 0 for every other file | medium — inherits TEX-4's schema-change risk for the 840 files it touches | YES, all 500 sampled files bit-exact | **new layer**: `normal2`, a colour-type-4 convention beside the existing `normal`; in-place re-encode (no new layer) for every other file; **retires**: the 4-channel `normal` PNG once consumers adopt it |
| NET-2 | `networks/` | Compress the 7,248 baked segment `.glb` binary buffers at rest — the same technique as MESH-1 on the same file type in the same raw-BIN condition | 300.4 MiB `lzma` (6.44% of bundle, 74.20% of `networks/`); 238.5 MiB `zlib -9` (5.11%, 58.92%). **Component of COMP-1 — do not add to it; and an alternative to NET-1 on the same bytes, never a sum** | measured (200-file sample, actually compressed, bit-exact round-trip asserted, stated linear extrapolation, §3.5) | unmeasured; bounded by row 3's 23.490% share, and expected to add write-time CPU | 1 line: one decompress call | low | YES, round-trip asserted on the sample | **new layer**: compressed encoding of the unchanged `seg_<id>.glb` bytes, `compression` field under §12.1; **retires**: nothing |
| INST-1 | `instances.jsonl` | Compress the file at rest (`lzma` or `zlib -9`) | 272.1 MiB `lzma` (5.83% of bundle, 88.01% of the file); 251.8 MiB `zlib -9` (5.39%, 81.43%). **Component of COMP-1 — do not add to it** | measured, full file, bit-exact round-trip asserted | unmeasured; bounded by row 1's 16.739% share, and expected to add write-time CPU | 1 line: one decompress call | low | YES | **new layer**: `instances.jsonl.xz` beside the canonical JSON, `compression` field under §12.1; **retires**: nothing |
| TEX-4 | `textures/` | Normal-map 2-channel repack (colour type 4; drop B, reconstruct via `round(sqrt(1−x²−y²)×127.5+127.5)` at load) | 229.5 MiB (4.92% of bundle, 12.2% of `textures/`, 30.4% of its own 754.3 MiB candidate subset). **Nested inside the TEX-3+4 joint row — do not add** | measured — exact on the full 840-file population, plus a 120-file re-encode round-trip | unmeasured; inside row 2's 59.416% share | 31 lines (the reconstruction step at load) plus a §7.1 schema amendment | medium — schema change, covers 840 of 3,510 files | YES, 840 of 840 reconstruct to the exact original byte | **new layer**: `normal2` colour-type-4 map; **retires**: the 4-channel `normal` PNG after adoption |
| TEX-3 | `textures/` | Safe channel reduction (drop alpha only when constant at exactly 255, collapse to greyscale when R=G=B) on top of TEX-2 | 210.7 MiB (4.51% of bundle, 11.2% of `textures/`). **Nested inside the TEX-3+4 joint row — do not add** | measured (120-file sample, all 120 assertions passed, stated 26.4159× extrapolation) | unmeasured; inside row 2's 59.416% share | 0 lines — verified against this bundle's reference consumer, `tools/blender_import_bundle.py:2165`, which expands `L`/`RGB` to RGBA automatically | low | YES | not applicable — in-place re-encode at the same path; the PNG colour type changes, the contract slot does not; nothing new, nothing retired |
| INST-2 | `instances.jsonl` | Columnar binary: 41 B/record fixed core plus sparse typed `attrs` side-columns | 199.0 MiB (4.26% of bundle, 64.36% of the file) — **2.97× worse on bytes than INST-1**, stated plainly; its case is import time, not size | arithmetic (real per-field counts × individually round-trip-verified widths); the fixed core was actually built and read back | unmeasured; bounded by row 1's 16.739% share | 59 lines (the reference build-and-read implementation, `$SCRATCH/audit/instances_packed_read_bench.py`) — against which it removes ≈14.2 s of `json.loads` from the reference importer's default two-pass run and ≈21.3 s from its full-city three-pass run, a measured 696× per-pass speedup on the fixed core | medium — new container and schema surface; every width individually verified lossless against real data | YES | **new layer**: `instances.bin` + a small `assetId` catalogue beside `instances.jsonl`, opt-in; **retires**: nothing until an A/B gate — transitional bundle 419.4 MiB for this domain |
| LANE-1 | `lanes.jsonl` | Compress the file at rest (`lzma` or `zlib -9`) | 190.5 MiB `lzma` (4.08% of bundle, 82.49% of the file); 169.5 MiB `zlib -9` (3.63%, 73.41%). **Component of COMP-1 — do not add to it** | measured, full file, bit-exact round-trip asserted | unmeasured; bounded by row 3's 23.490% share, and expected to add write-time CPU | 1 line: one decompress call | low | YES | **new layer**: `lanes.jsonl.xz`, `compression` field under §12.1; **retires**: nothing |
| LANE-2 | `lanes.jsonl` | Ship `curve.m_Bezier`'s 4 control points instead of the 9-point tessellated `path` | 188.6 MiB (4.04% of bundle, 81.69% of the file) for the complete candidate, uncompressed — the sort key, chosen because it is the only complete-candidate figure that is not an extrapolation. Paired with `lzma`: 213.06 MiB measured at the fixed-core level (2.26× better than `lzma`-over-JSON), ≈210.5 MiB for the full candidate as an unmeasured extrapolation | arithmetic for the sparse columns; the 37.00 MiB fixed core inside it was built, read back and round-tripped for real | unmeasured; bounded by row 3's 23.490% share; the producer already reads the same `Curve` component today | 147 lines (the reference build-and-read implementation, `$SCRATCH/audit/lanes_binary_build.py`); the evaluation itself is the 4-line function §5.2 quotes from the game | low — the evaluation formula was confirmed identical to the game's own, and the round-trip reproduces the shipped path to within its own 1 mm rounding step | YES, proven by full-population refit plus a real packed-binary round-trip | **new layer**: `lanes.bin` (or a `curve` field beside `path`, the same shape `lanemeshes.jsonl` already ships), opt-in; **retires**: `path` only after an A/B gate — transitional bundle 273.2 MiB for this domain |
| TEX-2 | `textures/` | Adaptive per-row PNG filtering plus zlib level 9 | 151.9 MiB (3.25% of bundle, 8.0% of `textures/`). **Nested inside TEX-3 and the joint row — do not add** | measured (120-file sample, assertion passed, stated extrapolation) | unmeasured; encode CPU rises, inside row 2's 59.416% share | 0 lines — any standard PNG decoder | low | YES | not applicable — in-place re-encode; nothing new, nothing retired |
| TEX-1 | `textures/` | Adaptive per-row PNG filtering alone (replace the fixed filter 0), same zlib level | 64.5 MiB (1.38% of bundle, 3.4% of `textures/`). **Nested inside TEX-2 — do not add** | measured (120-file sample, assertion passed, stated extrapolation) | unmeasured; inside row 2's 59.416% share | 0 lines | low | YES | not applicable — in-place re-encode; nothing new, nothing retired |
| MESH-3 | `meshes/` | Derive the 1,436 `__base.glb` skirts at consumer load time instead of baking them | 27.9 MiB (0.60% of bundle, 1.6% of `meshes/`) as shipped today; ≈2.2 MiB marginal if COMP-1 ships first (`arithmetic`, the removed files' own compressed size at `meshes/`'s measured 7.88% `lzma` ratio) | measured (file-class census, full population) | unmeasured; 1,436 fewer files to build and write inside row 2's 59.416% share | 366 lines (`IO/BaseSkirtBuilder.cs`, proven faithful to the game in §27) | medium — the port must reproduce the algorithm exactly to avoid visible gaps; a proven reference exists | YES — every fact in `__base.glb` is derivable from data already shipped plus a verified deterministic algorithm | **removes** a file class rather than adding one; the per-prefab base-material reference must move into `materials.json`/`bundle.json` — negligible bytes, a small schema touch |
| TERRAIN-1 | `terrain/` | Selective adaptive filtering + zlib-9 on the 11 of 39 files where it measurably wins (dominated by `heightmap.png`'s 30.8%) | 4.33 MiB (0.09% of bundle, 6.47% of `terrain/`); a blanket policy across all 39 files is a 2.82% **loss** | measured, full population, every round-trip bit-exact | unmeasured; bounded by row 4's 0.355% share | 0 lines — in-place re-encode at unchanged paths | low | YES | not applicable — in-place re-encode; nothing new, nothing retired |
| TEX-5 | `textures/` | Delete orphan textures | 0 — 0 orphans and 0 duplicate pixel-groups found in the full population | measured (closed, negative result) | 0 | 0 lines | none | not applicable | not applicable — no files to touch, nothing to retire |
| MESH-4 | `meshes/` | Delete unreferenced or dead meshes | 0 — all 1,444 unreferenced-by-`assetId` files resolve to a documented contract mechanism | measured (closed, negative result) | 0 | 0 lines | none | not applicable | not applicable — no files to touch, nothing to retire |

**§6 (markings and decals) contributes no row, by closure rather than omission.**
`lanemeshes.jsonl` + `lanemeshes/` are 29.4 MiB, 0.629% of the bundle, and already the
parametric form every other geometry domain above is being projected toward (§6.2). Its real
cost is consumer import time — 2,240,914 vertices and ray-casts before the landed sagitta rule,
~1.06M after, with the lane-geometry import phase measured at 374.9 s before and 381.8 s after
(§6.3, §8) — which belongs in §8's import-time subsection and never in a bytes column.

### 9.3 Lossy candidates — separate, and none recommended

Kept apart from §9.2 per constraint 3. **No candidate below appears in §9.5's recommendation**,
and none may be adopted without its own decision and its own A/B evidence.

| id | domain | change | est. bytes saved | provenance | est. rip-time saved | consumer cost | risk | lossless? | error bound | additive-layer shape |
|---|---|---|---|---|---|---|---|---|---|---|
| MESH-2 | `meshes/` | `KHR_mesh_quantization`-style re-encode: `POSITION` → `i16`, `NORMAL`/`TANGENT` → `i8`, `TEXCOORD_0` → `u16` | 952.3 MiB (20.40% of bundle, 55.5% of `meshes/`) — smaller than MESH-1's lossless 1,581.5 MiB from the same directory | arithmetic (format widths over the full population) | unmeasured; fewer bytes to serialise inside row 2's 59.416% share | consumer must dequantize per the extension or silently misread the geometry | high — breaks every consumer that does not implement the extension | **NO** | ≤ 20.505 mm (the full quantization step over the worst mesh's largest extent; a round-to-nearest quantizer's true worst case is at most half that, ≤ 10.253 mm) | either a **new layer** (a parallel quantized geometry form, opt-in) or a breaking major-version replacement — not designed here |
| INST-3 | `instances.jsonl` | `colorSet` channels as `u8` (1/255 step) inside INST-2's side-column layout | 41.4 MiB (0.89% of bundle) off INST-2's 55.28 MiB `colorSet` column | measured (full-population round-trip failure rate) | unmeasured | none beyond INST-2's own reader | medium — visible banding on exactly the highest-instance-count assets, where §4.3 measured the entropy to be real | **NO** | 65.21% of 14,491,656 `colorSet` channel values fail to round-trip through a `u8` step against the shipped 4 dp text | replaces the `colorSet` side-column inside INST-2's own new layer, not a separate file |
| INST-4 | `instances.jsonl` | `rot` smallest-three quaternion compression (16 B → ~7 B) inside INST-2's fixed core | unmeasured — Phase A did not build or encode it | unmeasured | unmeasured | consumer reconstructs the dropped component and de-quantizes | medium — well-understood technique, unverified here | **NO** | `unmeasured`; the brief's own "always drop `w`" variant is invalid, since §4.3 measured `rot.x`/`rot.z` nonzero on 24.80% of records | replaces the `rot` fixed column inside INST-2's own new layer |
| TEX-6 | `textures/` | Lossy re-encode (WebP/AVIF at a quality setting) of whatever remains after the lossless texture rows | unmeasured — Phase A did not encode or measure it | unmeasured | unmeasured | high — a new-format decoder | high | **NO** | `unmeasured`; note BC7, the game's own format, is **not** a smaller candidate at these dimensions (§1.2: our PNGs are 0.544× the BC7 footprint) | would replace `textures/*.png` outright for the files it touches — not expressible as an opt-in layer without a codec-selector field |

### 9.4 The second ranking: bytes saved per unit of consumer cost

**Consumer cost is measured as lines of new consumer-side code**, anchored to a real
implementation wherever one exists — `Systems/NetworkRipSystem.GameMesh.cs` (664),
`IO/BaseSkirtBuilder.cs` (366), `Systems/MaterialRipSystem.cs:1488-1518`'s normal reconstruction
(31), and the two reference build-and-read scripts this audit actually wrote and ran,
`$SCRATCH/audit/lanes_binary_build.py` (147) and `$SCRATCH/audit/instances_packed_read_bench.py`
(59). A decompress call is 1 line. An in-place re-encode is 0. The ratio below is MiB saved
divided by `1 + lines`, so a zero-line candidate scores its full byte value rather than dividing
by zero. Lossy rows are excluded, per constraint 3.

| rank | id | MiB saved | new consumer lines | MiB per cost unit | rank by bytes (§9.2) |
|---:|---|---:|---:|---:|---:|
| 1 | COMP-1 | 2,344.5 | 1 | 1,172.2 | 1 |
| 2 | MESH-1 | 1,581.5 | 1 | 790.8 | 2 |
| 3 | TEX-3 | 210.7 | 0 | 210.7 | 8 |
| 4 | TEX-2 | 151.9 | 0 | 151.9 | 12 |
| 5 | NET-2 | 300.4 | 1 | 150.2 | 5 |
| 6 | INST-1 | 272.1 | 1 | 136.1 | 6 |
| 7 | LANE-1 | 190.5 | 1 | 95.3 | 10 |
| 8 | TEX-1 | 64.5 | 0 | 64.5 | 13 |
| 9 | TEX-3+4 | 377.1 | 31 | 11.8 | 4 |
| 10 | TEX-4 | 229.5 | 31 | 7.2 | 7 |
| 11 | TERRAIN-1 | 4.3 | 0 | 4.3 | 15 |
| 12 | INST-2 | 199.0 | 59 | 3.3 | 9 |
| 13 | LANE-2 | 188.6 | 147 | 1.3 | 11 |
| 14 | NET-1 | 401.5 | 664 | 0.6 | 3 |
| 15 | MESH-3 | 27.9 | 366 | 0.1 | 14 |

**The two rankings disagree, and the disagreement is the finding.** NET-1 falls from 3rd by
bytes to 14th of 15 by cost-efficiency — it is the third-largest win in the bundle and the
second-worst byte-per-line deal in it, because 401.5 MiB of it is bought with a 664-line port of
the most rule-dense algorithm this codebase has had to reproduce. §3.5 sharpens that: at NET-1's
*marginal* 101.1 MiB — its saving once NET-2 has compressed the same directory — the ratio falls
from 0.6 to **0.152 MiB per cost unit**, still 14th of 15, while NET-2 buys 300.4 MiB of the same
404.8 MiB at 150.2. TEX-3 rises from 8th to 3rd for the mirror-image reason: 210.7 MiB with no
consumer code at all, verified against the reference importer's stock PNG loader. MESH-3 is last
on both counts. Only the top two rows agree between the rankings, and they agree because generic
compression is simultaneously the largest win in the bundle and the cheapest thing a consumer can
be asked to do.

### 9.5 Sequencing recommendation

**Phase B should be COMP-1: one bundle-wide at-rest compression policy over `meshes/`,
`networks/`, `instances.jsonl` and `lanes.jsonl`.** It saves 2,344.5 MiB — 50.23% of the whole
bundle, and 5.84× the largest candidate that is not one of its own components (NET-1, 401.5 MiB)
— for one decompress call and a `compression` field amendment under
§12.1, and it ranks first on *both* orderings, which nothing else in §9.4 does. It renegotiates
no semantics, retires no layer, and every one of its four components was verified by a bit-exact
round-trip on real bytes. Two facts make it the sequencing decision rather than just the biggest
number. First, it is the only candidate that is unambiguously good to do first: because it
touches only the encoding of bytes, running it before or after any structural change costs
nothing either way. Second, it changes the case for almost everything below it — after COMP-1,
`networks/` is 104.4 MiB, `lanes.jsonl` is 40.4 MiB and `instances.jsonl` is 37.1 MiB, so NET-1's
marginal byte value falls from 401.5 MiB to 101.1 MiB (`arithmetic`, §3.5), LANE-2's falls from
188.6 MiB to about 20 MiB (unmeasured extrapolation, §9.1) and INST-2's byte case disappears
entirely, since its 110.2 MiB columnar output is 2.97× *larger* than the compressed JSON it would
replace. MESH-3's 27.9 MiB likewise becomes ≈2.2 MiB. Measuring that collapse before committing
engineering to it is exactly what Phase A existed to do — and `networks/`'s own share of it was
the last unmeasured thing in this audit, measured in §3.5 only after this section's first draft
had already recommended the phase order below.

**This overturns the spec's sketched order in four places, and confirms it in two.** The sketch
put B as "lossless re-encoding of `instances.jsonl` and `lanes.jsonl`" and D as mesh payload
work: the measurements move the mesh domain's compression row *into* B and make it the dominant
term (1,581.5 MiB of the 2,344.5 MiB), because the largest single candidate in the bundle turned
out to be a compression row in the mesh domain, not a JSONL re-encode. The sketch also treated
the two JSONL layers as one job; they want opposite treatments — the structural candidate loses
to `lzma` by 2.97× on `instances.jsonl` and beats it by 2.26× on `lanes.jsonl`, so LANE-2 is
worth building on its bytes and INST-2 is not. And the sketch put networks (C) second, ahead of
meshes and textures: NET-1 should instead run **last** of the structural work. Its 401.5 MiB is
`arithmetic`, not measured; it is the single most expensive port in the table at 664 lines; it
makes the bundle *larger* (408.1 MiB for the domain) for as long as the additive layer ships
beside `networks/`; and §3.5's measurement is decisive — NET-2 captures 300.4 MiB of that same
404.8 MiB for one decompress call, leaving NET-1 a marginal 101.1 MiB for its 664 lines. **On
bytes, the composition layer does not earn its consumer cost once the baked meshes are
compressed**, and if it is ever built the justification has to be something the byte column does
not hold: that a consumer opens 45 geometry files instead of 7,248, and that the producer stops
baking 7,248 meshes in the first place (row 3's instrumented 65.8 s, 3.321% of the rip). A third
thing the measurements overturn: **textures turned out largely already-efficient** — our lossless
PNGs are 0.544× the game's own BC7 footprint at the same dimensions (§1.2), and the entire
lossless ceiling on 1,887.3 MiB is 19.98%, so the sketch's expectation that textures were the
place a format choice would pay is not what the domain contains. What the sketch got right: a
representation-only change with no consumer semantics belongs first (B's *principle* holds even
though its content changes), and import-time work belongs last — §6/§8's measured result, a
~2.1× cut in marking vertices and ray-casts producing 374.9 s → 381.8 s of wall clock, is the
standing evidence that import-time work must be measured before it is built, not modelled.

**After B, the order the table implies is C = textures, D = `lanes.jsonl`, E =
`instances.jsonl`, F = networks, with terrain and skirts folded in opportunistically.** Textures
come second because they are the only domain whose candidate values survive COMP-1 intact —
§1.1's PNGs already run through `Deflater`, so the compression policy does not touch them — and
because 210.7 MiB of the 377.1 MiB joint win costs the consumer literally nothing (TEX-3, 0 new
lines, verified against the reference importer). TERRAIN-1's 4.33 MiB is the same producer-only
shape and should ride along with it rather than getting a phase. `lanes.jsonl` comes third: it is
the one structural candidate that beat generic compression on its own measured merits, at low
risk, with a 4-line evaluation function taken verbatim from the game and a real packed-binary
round-trip behind it. `instances.jsonl` comes fourth and must be justified on import time rather
than bytes — 696× measured on the fixed core, ≈14.2 s → ≈0.2 s for the reference importer's
default run — which is a genuine win but a different kind of win, and pretending otherwise is how
the 2026-08-07 harness work spent effort on the wrong layer. Networks comes last for the reasons
above. **No lossy candidate is recommended at any position**: MESH-2's 952.3 MiB is real but is
both lossy and smaller than MESH-1's lossless 1,581.5 MiB from the same directory, which settles
it, and INST-3 targets exactly the field §4.3 measured to be predominantly information.

**§3.5's measurement was taken after this order was first written, and the order does not
move — confirmed against the new numbers rather than asserted.** B stays first and gets larger
(2,044.1 → 2,344.5 MiB, 43.80% → 50.23%), because NET-2 is a component of it, not a competitor.
C stays textures: they are still the only domain COMP-1 does not touch, so their 377.1 MiB is
unaffected by the rescope, and no candidate that moved outranks TEX-3's 210.7-MiB-for-zero-lines
position in §9.4. D and E are untouched — §3.5 changes nothing about `lanes.jsonl` or
`instances.jsonl`. F stays networks and its case weakens rather than strengthens: NET-1 keeps its
#3 rank by standalone bytes and its 14th-of-15 rank by cost-efficiency, and its marginal figure
falls to 101.1 MiB. The only ordering *inside* the table that moved is §9.4's, where NET-2 enters
at 5th (150.2 MiB per cost unit) and pushes INST-1, LANE-1, TEX-1 and everything below them down
one place; no candidate crossed another in the bytes ordering except by NET-2's insertion at 5th.
