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

## 5. `lanes.jsonl`

## 6. Markings and decals

## 7. Terrain

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

## 9. Ranked candidates
