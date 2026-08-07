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

## 3. Networks

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
