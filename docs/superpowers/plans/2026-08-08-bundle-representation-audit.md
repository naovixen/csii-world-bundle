# Bundle Representation Audit — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `docs/cs2-storage-audit.md` — a per-domain, measured account of what CS2 stores versus what the bundle ships, ending in a ranked table of candidate wins that decides the order and content of Phases B–F.

**Architecture:** Phase A is an audit, not a build. Every domain section answers the same four questions from two independent sources — the decompiled game (`tools/decomp`) for *what the game stores*, and the shipped 4.6 GB bundle on disk for *what we actually ship* — and every claim carries a number and the command that produced it. Candidate savings are **measured on a sample and extrapolated**, never guessed; that discipline exists because on 2026-08-07 two confident plan assumptions about where the cost lived were falsified by measurement.

**Tech Stack:** Python 3.13 (stdlib + `PIL` 12.3, `numpy` 2.5, already present), `tools/decomp` (ICSharpCode.Decompiler → real C#), Git Bash, the shipped bundle and `Carto.log` on disk. No C# is written or built in this phase.

## Global Constraints

Copied from the approved spec (`docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md`). Every task's requirements implicitly include this section.

1. **Phase A changes no production code.** No edits under `Systems/`, `IO/`, `Domain/`, `Geodata/`, `tools/*.py`, or the bundle contract. The only committed artifact of this whole plan is `docs/cs2-storage-audit.md` (plus a one-line index entry where the repo already indexes docs).
2. **Measurement scripts are throwaway.** They live in your session scratchpad (`$SCRATCH`, defined in Task 0), are never committed, and are never imported by anything. Reproducibility comes from pasting the command into the doc next to its number, not from shipping a tool.
3. **No information may be dropped.** Size wins must come from re-encoding, dedup, or deriving-instead-of-baking. Anything lossy is recorded as a *separate, explicitly-flagged* candidate row with `lossless? = NO` and never mixed into a lossless recommendation.
4. **Engine-neutral.** No BeamNG-specific or engine-specific proposal enters the candidate table. "Serve BeamNG-style level building well, without coupling to it."
5. **Additive layers, retire later.** Every candidate must be expressible as a new layer shipping *alongside* the one it replaces, gated by `tools/ab_render.py` + `tools/ab_compare.py`, with the old layer retired only in a deliberate later bump — the `lanemeshes/` pattern.
6. **Textures: investigate before choosing a format.** Lossless wins are enumerated and measured before any lossy format is even named.
7. **Estimates are measured, not asserted.** A candidate row's "bytes saved" must come from actually re-encoding a sample and extrapolating by a stated rule, or be marked `arithmetic` (computed from format widths) or `unmeasured`. Never leave the provenance implicit.
8. **Two sources, never one.** A decompile is a claim about *code*, not about *data* (`attrs.elevation` was once "proved" clamped-at-zero from correctly-decompiled source, and the bundle has 29,610 negative values). Read the shipped bundle before generalising any code reading.
9. **Phase 7's manifest work stays where PLAN.md has it.** This audit must not quietly redesign `bundle.json`.

## Adaptation note: why these tasks are not red-green-refactor

The writing-plans skill's default task shape is write-failing-test → implement → pass → commit. Phase A produces no code, so there is nothing to test. Each task keeps the same *rhythm* — do the work, then run an explicit verification step with pass criteria that can fail, then commit — with the verification being "this number reproduces when the command is re-run, and no claim in this section lacks a number." Do not skip the verification step; it is the only thing standing between this audit and the guesswork it exists to replace.

## File Structure

**Created (committed):**
- `docs/cs2-storage-audit.md` — the sole deliverable. One section per domain, uniform template (Task 0 defines it), ending in the ranked candidate table.

**Created (scratchpad, never committed):** `$SCRATCH/audit/*.py`, `$SCRATCH/audit/*.txt` — measurement scripts and their raw output.

**Read (never modified):**
- Bundle: `$BUNDLE/` (see Task 0 for the path) — `instances.jsonl`, `lanes.jsonl`, `networks.jsonl`, `lanemeshes.jsonl`, `materials.json`, `colors.json`, `warnings.jsonl`, `meshes/`, `networks/`, `textures/`, `terrain/`, `lanemeshes/`, `areas.jsonl`.
- Rip log: `$LOG` — per-phase durations and per-texture source GPU formats.
- Producer source, for "what we ship": `IO/InstanceWriter.cs` (132 lines), `IO/LaneWriter.cs` (79), `IO/NetworkWriter.cs` (194), `IO/TextureWriter.cs` (200), `IO/Png.cs` (229), `IO/GltfWriter.cs` (694), `IO/GeometryAssetReader.cs` (558), `Systems/MeshRipSystem.cs` (2403), `Systems/MaterialRipSystem.cs` (1932), `Systems/NetworkRipSystem.GameMesh.cs` (664).
- Contract, for "what a consumer is promised": `docs/bundle-schema.md` (§4 instances, §5 networks, §5A lanes, §5B lanemeshes, §6 meshes, §7 materials, §7.1 channel semantics, §8 textures/terrain).
- Prior validation, so nothing is re-derived: `docs/cs2-api-audit.md` (§8 network assembly, §9 object meshes, §11 materials/textures, §12 lanes, §19 marking atlas rects, §21 GameMesh port, §23 mesh delta, §24 material delta).

---

## Task 0: Baseline — environment, doc skeleton, verified sizes and rip-time attribution

Every later task's "estimated bytes saved" and "estimated rip-time saved" is a share of the numbers established here, so these must be right and reproducible before anything else runs. This task also resolves a live discrepancy: the spec's table says `networks/` holds 7,380 baked meshes, `ls` says 7,248 files, and `Carto.log` says 8,259 segments were written. Three numbers, one truth.

**Files:**
- Create: `docs/cs2-storage-audit.md`
- Create (scratchpad): `$SCRATCH/audit/baseline.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the shell variables `$BUNDLE`, `$LOG`, `$SCRATCH` used verbatim by every later task; the doc's section template, which every later task fills in; the baseline byte and second totals that later "share of total" percentages divide by.

- [ ] **Step 1: Fix the paths and check the toolchain**

Run this and confirm every line prints without error. If `$BUNDLE` does not exist, stop — the audit has no subject and needs a fresh full-city rip first.

```bash
export BUNDLE="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/ModsData/Carto/spike"
export LOG="/c/Users/naomi/AppData/LocalLow/Colossal Order/Cities Skylines II/Logs/Carto.log"
export SCRATCH="<paste your session's scratchpad path here>"   # e.g. /c/Users/naomi/AppData/Local/Temp/claude/E--cs2-carto-ripper/<session-id>/scratchpad
mkdir -p "$SCRATCH/audit"

ls "$BUNDLE" | head
head -1 "$LOG"
python -c "import PIL, numpy, zlib, lzma, struct, json; print('deps ok')"
```

Expected: the bundle listing shows `instances.jsonl`, `meshes`, `textures`, …; the log's first line is the mod-loaded banner with a timestamp; `deps ok`.

- [ ] **Step 2: Measure the baseline, exactly**

```bash
cat > "$SCRATCH/audit/baseline.py" <<'PY'
"""Byte-exact bundle census: per top-level entry, total bytes and file count."""
import os, sys
from pathlib import Path

root = Path(sys.argv[1])
rows = []
for entry in sorted(root.iterdir()):
    if entry.is_file():
        rows.append((entry.name, entry.stat().st_size, 1))
    else:
        total = count = 0
        for dirpath, _dirnames, filenames in os.walk(entry):
            for name in filenames:
                total += (Path(dirpath) / name).stat().st_size
                count += 1
        rows.append((entry.name + "/", total, count))

grand = sum(size for _n, size, _c in rows)
rows.sort(key=lambda r: -r[1])
print(f"{'entry':<24}{'bytes':>15}{'MiB':>10}{'share':>8}{'files':>9}")
for name, size, count in rows:
    print(f"{name:<24}{size:>15,}{size/1048576:>10.1f}{100*size/grand:>7.1f}%{count:>9,}")
print(f"{'TOTAL':<24}{grand:>15,}{grand/1048576:>10.1f}{100.0:>7.1f}%")
PY
python "$SCRATCH/audit/baseline.py" "$BUNDLE" | tee "$SCRATCH/audit/baseline.txt"
```

- [ ] **Step 3: Resolve the networks count discrepancy**

```bash
ls "$BUNDLE/networks" | wc -l                                    # baked .glb files on disk
wc -l < "$BUNDLE/networks.jsonl"                                 # segment records
grep -c '"assetId":"seg_' "$BUNDLE/networks.jsonl"               # records pointing at a baked mesh
grep -E 'segment\(s\) to networks.jsonl' "$LOG"                  # what the rip claimed
```

Write down which of the three numbers means what — records without a `seg_` mesh (elevated/tunnel/zero-length segments that produced no geometry) are the likely gap. Record the reconciliation in the doc; a table whose own row counts disagree cannot rank anything.

- [ ] **Step 4: Attribute rip time from the log**

Per-phase durations exist in the log both explicitly and as timestamp deltas.

```bash
grep -nE '^\[2026' "$LOG" | cut -c1-140 > "$SCRATCH/audit/log-timeline.txt"
grep -nE ' in [0-9.]+ s|MiB in |Wrote .* (instance|lane|segment|\.glb)' "$LOG"
```

Known anchors from the 2026-08-07 rip, to be re-confirmed rather than trusted: mesh pass `4198 .glb, skipped 120, failed 0, in 1178.3 s`; networks `8259 segment(s) … in 65.8 s`. For phases with no explicit duration (textures/materials, instances, lanes), compute the delta between the first and last log line belonging to that phase from `log-timeline.txt` and say in the doc that it is a timestamp-bracket estimate, not an instrumented one.

- [ ] **Step 5: Create the audit document with its frozen section template**

Every later task fills in exactly this shape. Do not let sections drift apart — the ranked table in Task 7 is assembled mechanically from the per-section candidate tables.

````bash
cat > docs/cs2-storage-audit.md <<'MD'
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

<!-- Task 0 fills this in: the byte-exact census table, the networks count reconciliation,
     and the rip-time attribution with each phase marked instrumented or bracket-estimated. -->

## 1. Textures

## 2. Meshes

## 3. Networks

## 4. `instances.jsonl`

## 5. `lanes.jsonl`

## 6. Markings and decals

## 7. Terrain

## 8. Rip time, by phase

## 9. Ranked candidates

MD
````

Then paste the Task 0 findings under `## 0. Baseline`: the census table from Step 2 (as a markdown table, with the generating command in a fenced block above it), the Step 3 reconciliation in prose, and the Step 4 per-phase seconds table with each row marked `instrumented` or `bracket`.

- [ ] **Step 6: Verify**

```bash
python "$SCRATCH/audit/baseline.py" "$BUNDLE" | tail -1     # re-run: TOTAL must match the doc
grep -c 'TODO\|TBD\|approximately\|roughly\|probably' docs/cs2-storage-audit.md
```

Pass criteria: the re-run TOTAL is byte-identical to the number written in §0; the hedge-word count is `0`; §0 contains no claim without a number; the three networks counts are each explained.

- [ ] **Step 7: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §0: byte-exact bundle baseline and rip-time attribution"
```

---

## Task 1: Textures — 1.9 GB, 41%

The largest share, and a prime rip-time suspect (PNG encoding of 3,510 images). One producer detail is already known and must be measured rather than assumed: `IO/Png.cs:51` writes filter byte `0` ("None") on **every** scanline, with `Deflater.DEFAULT_COMPRESSION` (`IO/Png.cs:167-171`), and the file's own comment concedes "per-row filter selection would shrink the output somewhat". Adaptive filtering is bit-exact lossless — the decoded pixels are identical — so if it is worth 20–40% here it is 400–800 MB for a producer-side change with **zero consumer cost**. Measure it; do not assume it.

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§1)
- Create (scratchpad): `$SCRATCH/audit/tex_inventory.py`, `$SCRATCH/audit/tex_channels.py`, `$SCRATCH/audit/tex_reencode.py`
- Read: `IO/TextureWriter.cs`, `IO/Png.cs`, `Systems/MaterialRipSystem.cs`, `docs/bundle-schema.md` §8 and §7.1, `docs/cs2-api-audit.md` §11 and §24

**Interfaces:**
- Consumes: `$BUNDLE`, `$LOG`, `$SCRATCH` from Task 0; the §0 baseline total that §1's share divides by.
- Produces: §1 of the doc and its candidate rows, which Task 7 copies into the ranked table verbatim.

- [ ] **Step 1: Inventory what is actually on disk**

```bash
cat > "$SCRATCH/audit/tex_inventory.py" <<'PY'
"""Census of textures/: size, dimensions, bit depth, colour type, and per-channel constancy."""
import sys
from collections import Counter
from pathlib import Path
import numpy
from PIL import Image

root = Path(sys.argv[1]) / "textures"
files = sorted(root.glob("*.png"))
by_dim, by_mode, by_depth = Counter(), Counter(), Counter()
total = 0
for path in files:
    total += path.stat().st_size
    with Image.open(path) as image:          # header only, no decode
        by_dim[image.size] += 1
        by_mode[image.mode] += 1
        by_depth[image.bits if hasattr(image, "bits") else 8] += 1
print(f"{len(files):,} files, {total/1048576:.1f} MiB")
print("\ndimensions:"); [print(f"  {w}x{h}: {n:,}") for (w, h), n in by_dim.most_common(20)]
print("\npixel modes:"); [print(f"  {m}: {n:,}") for m, n in by_mode.most_common()]
print("\nsize percentiles (MiB):")
sizes = sorted(p.stat().st_size for p in files)
for q in (50, 90, 99, 100):
    print(f"  p{q}: {sizes[min(len(sizes)-1, q*len(sizes)//100)]/1048576:.2f}")
print("\nlargest 15:")
for path in sorted(files, key=lambda p: -p.stat().st_size)[:15]:
    with Image.open(path) as image:
        print(f"  {path.name[:16]}… {path.stat().st_size/1048576:6.2f} MiB {image.size} {image.mode}")
PY
python "$SCRATCH/audit/tex_inventory.py" "$BUNDLE" | tee "$SCRATCH/audit/tex_inventory.txt"
```

- [ ] **Step 2: Establish what the game stores, from two sources**

The rip log records the *source* GPU format and header resolution per texture — that is the game's own storage, straight from the run that produced this bundle:

```bash
grep -oE '(RGBA|R|RG|BC)[A-Za-z0-9_]*(SRGB|UNorm|Float)' "$LOG" | sort | uniq -c | sort -rn
grep -oE 'header [0-9]+x[0-9]+' "$LOG" | sort | uniq -c | sort -rn
grep -oE '[0-9]+x[0-9]+ [A-Z][A-Za-z0-9_]+' "$LOG" | sort | uniq -c | sort -rn | head -20
```

Then confirm the format story in source rather than inferring it from names:

```bash
cd tools/decomp && dotnet build -c Release && cd ../..
./tools/decomp/bin/Release/net8.0/decomp.exe type Colossal.IO.AssetDatabase Colossal.IO.AssetDatabase.SurfaceAsset  > "$SCRATCH/audit/SurfaceAsset.cs"
./tools/decomp/bin/Release/net8.0/decomp.exe type Colossal.IO.AssetDatabase Colossal.IO.AssetDatabase.VTSurfaceAsset > "$SCRATCH/audit/VTSurfaceAsset.cs"
```

Answer, with numbers: how many bytes would the *game's* representation of these same 3,510 images occupy (BC7 is 1 byte/pixel; BC5 is 0.5; compute from the dimension census), and what is our multiple over it? That ratio is the honest framing of question 3 for this domain.

- [ ] **Step 3: Find channels nothing samples, and channels that carry no information**

Two different wins hide here. A channel the shader never reads is *information we invented*; a channel that is constant across the whole image is *representation*.

```bash
cat > "$SCRATCH/audit/tex_channels.py" <<'PY'
"""Per-channel constancy and derivability, over a size-weighted sample."""
import sys, random
from collections import Counter
from pathlib import Path
import numpy
from PIL import Image

root = Path(sys.argv[1]) / "textures"
sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 300
files = sorted(root.glob("*.png"))
random.Random(0).shuffle(files)                     # fixed seed: the sample is reproducible
sample = files[:sample_size]

verdicts = Counter()
alpha_constant_bytes = grey_rgb_bytes = normal_b_bytes = 0
for path in sample:
    with Image.open(path) as image:
        pixels = numpy.asarray(image.convert("RGBA"))
    size = path.stat().st_size
    r, g, b, a = (pixels[..., i] for i in range(4))
    if a.min() == a.max():
        verdicts["alpha constant"] += 1; alpha_constant_bytes += size
    if (r == g).all() and (g == b).all():
        verdicts["rgb identical (greyscale)"] += 1; grey_rgb_bytes += size
    # Normal-map test: is B reconstructible as sqrt(1 - x^2 - y^2) from R,G?
    x = r.astype(numpy.float64) / 127.5 - 1.0
    y = g.astype(numpy.float64) / 127.5 - 1.0
    z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
    if numpy.abs((z * 127.5 + 127.5) - b.astype(numpy.float64)).max() <= 2:
        verdicts["B derivable from RG (normal map)"] += 1; normal_b_bytes += size
    for index, name in enumerate("RGBA"):
        channel = pixels[..., index]
        if channel.min() == channel.max():
            verdicts[f"{name} constant"] += 1

print(f"sample {len(sample)} of {len(files)} files")
for name, count in verdicts.most_common():
    print(f"  {name}: {count} ({100*count/len(sample):.1f}%)")
scale = sum(p.stat().st_size for p in files) / max(1, sum(p.stat().st_size for p in sample))
print(f"\nextrapolation factor (total bytes / sample bytes): {scale:.1f}x")
print(f"  alpha-constant files, extrapolated MiB: {alpha_constant_bytes*scale/1048576:.0f}")
print(f"  greyscale-RGB files,  extrapolated MiB: {grey_rgb_bytes*scale/1048576:.0f}")
print(f"  normal-B-derivable,   extrapolated MiB: {normal_b_bytes*scale/1048576:.0f}")
PY
python "$SCRATCH/audit/tex_channels.py" "$BUNDLE" 300 | tee "$SCRATCH/audit/tex_channels.txt"
```

Cross-check against what materials actually reference, so a "win" is not proposed on a map nothing samples:

```bash
python - <<'PY'
import json, collections, os
materials = json.load(open(os.environ["BUNDLE"] + "/materials.json", encoding="utf-8"))
slots = collections.Counter(k for entry in materials.values() for k in entry if isinstance(entry[k], str))
print(f"{len(materials):,} materials")
for slot, count in slots.most_common():
    print(f"  {slot}: {count:,}")
referenced = {v for entry in materials.values() for v in entry.values() if isinstance(v, str) and v.endswith('.png')}
on_disk = {f"textures/{n}" for n in os.listdir(os.environ["BUNDLE"] + "/textures")}
print(f"\nreferenced by a material: {len(referenced):,}")
print(f"on disk:                  {len(on_disk):,}")
print(f"orphans (shipped, unreferenced): {len(on_disk - referenced):,}")
PY
```

Read `docs/bundle-schema.md` §7.1 (channel semantics) before interpreting: a channel being constant is only a win if the contract lets a consumer learn the constant from `materials.json` instead of a file.

- [ ] **Step 4: Measure the lossless re-encode wins**

Four candidates, all bit-exact on decode. Measure each on the same sample so the rows are comparable.

```bash
cat > "$SCRATCH/audit/tex_reencode.py" <<'PY'
"""Lossless PNG re-encode experiments. Every variant must decode bit-identically."""
import sys, random, io, time
from pathlib import Path
import numpy
from PIL import Image

root = Path(sys.argv[1]) / "textures"
files = sorted(root.glob("*.png"))
random.Random(0).shuffle(files)
sample = files[:int(sys.argv[2]) if len(sys.argv) > 2 else 120]

def encode(pixels, mode, **kwargs):
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode).save(buffer, "PNG", **kwargs)
    return buffer.getvalue()

variants = {
    "as-shipped (filter None, deflate default)": lambda px, mode: None,
    "adaptive filter, zlib 6": lambda px, mode: encode(px, mode, compress_level=6),
    "adaptive filter, zlib 9": lambda px, mode: encode(px, mode, compress_level=9),
    "channel-reduced + adaptive": None,   # handled inline below
}

totals = {name: 0 for name in variants}
times = {name: 0.0 for name in variants}
original = 0
for path in sample:
    raw = path.read_bytes()
    original += len(raw)
    with Image.open(path) as image:
        pixels = numpy.asarray(image.convert("RGBA"))
    totals["as-shipped (filter None, deflate default)"] += len(raw)
    for name in ("adaptive filter, zlib 6", "adaptive filter, zlib 9"):
        started = time.perf_counter()
        blob = variants[name](pixels, "RGBA")
        times[name] += time.perf_counter() - started
        totals[name] += len(blob)
        assert numpy.array_equal(numpy.asarray(Image.open(io.BytesIO(blob)).convert("RGBA")), pixels), path
    # channel reduction: drop provably-redundant channels, then adaptive-filter
    r, g, b, a = (pixels[..., i] for i in range(4))
    if a.min() == a.max() and (r == g).all() and (g == b).all():
        reduced, mode = r, "L"
    elif a.min() == a.max():
        reduced, mode = pixels[..., :3], "RGB"
    else:
        reduced, mode = pixels, "RGBA"
    totals["channel-reduced + adaptive"] += len(encode(reduced, mode, compress_level=9))

print(f"sample {len(sample)} files, {original/1048576:.1f} MiB as shipped\n")
for name in variants:
    size = totals[name]
    print(f"  {name:<44}{size/1048576:8.1f} MiB  {100*size/original:6.1f}%  "
          f"{times[name]:5.1f}s encode")
PY
python "$SCRATCH/audit/tex_reencode.py" "$BUNDLE" 120 | tee "$SCRATCH/audit/tex_reencode.txt"
```

Note what the assertion buys: each variant is decoded and compared to the original array, so any row that prints is proven lossless rather than claimed to be. Extrapolate to the full 1.9 GB using the sample-bytes ratio printed by Step 3, and state that rule in the doc.

Also check the near-duplicate question the spec raises — textures that hash differently but hold the same pixels after normalisation:

```bash
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

If this prints zero the existing content-hash dedup is already exact, and the row is closed rather than left open — a negative result is a result.

- [ ] **Step 5: Write §1**

Fill the four questions in the frozen template, then a candidate table with one row per finding. Every row: `id | change | est. bytes saved | est. rip-time saved | consumer cost | risk | lossless?`. Expected rows include adaptive PNG filtering (producer-only, consumer cost *none*), channel reduction where provably redundant, orphan textures if any, and — separately and clearly flagged — any lossy format worth measuring later. Do not recommend a lossy format here; constraint 6 says investigate lossless first, and §9 ranks.

For rip time, note that PNG encoding happens off the main thread and say whether it is on the critical path, citing the log bracket for the texture phase from §0.

- [ ] **Step 6: Verify**

```bash
python "$SCRATCH/audit/tex_reencode.py" "$BUNDLE" 120 | tail -5     # numbers reproduce
grep -n 'measured\|arithmetic\|unmeasured' docs/cs2-storage-audit.md | head -20
```

Pass criteria: every candidate row in §1 carries a provenance label; every "lossless" claim traces to a variant whose assertion passed in Step 4; the extrapolation rule is written down, not implied; no hedge words.

- [ ] **Step 7: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §1: textures — game format vs shipped PNG, lossless re-encode measurements"
```

---

## Task 2: Meshes — 1.7 GB, 37%, and the `_L` question

The second-largest share and the one open question the spec insists must be *settled, not assumed*: are the `_L1`–`_L5` suffixes level-of-detail or CS2 zoning **levels**? Getting it backwards either bloats the bundle or deletes real buildings. A first look at the filenames shows the pattern is `<name>_L<n>_<w>x<h>.glb` (e.g. `EU_CommercialHotel01_L1_2x2.glb`) — the `<w>x<h>` lot-size suffix is a strong signal these are distinct zoned buildings, not LODs of one. **Confirm it in source and in the instance data; do not ship the hunch.** 2,080 of 5,634 filenames contain the pattern.

A second finding is already visible and needs its own row: 1,434 files end in `__base.glb` — the `BaseSkirtBuilder` skirts. What fraction of the 1.7 GB are they, and are they derivable from the parent mesh plus terrain?

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§2)
- Create (scratchpad): `$SCRATCH/audit/mesh_inventory.py`, `$SCRATCH/audit/mesh_quantize.py`
- Read: `IO/GltfWriter.cs`, `IO/GeometryAssetReader.cs`, `IO/BaseSkirtBuilder.cs`, `Systems/MeshRipSystem.cs`, `docs/bundle-schema.md` §6 and §6.1, `docs/cs2-api-audit.md` §9 and §23, `tools/inspect_glb.py`

**Interfaces:**
- Consumes: `$BUNDLE`, `$SCRATCH`, §0 baseline.
- Produces: §2 and its candidate rows; the settled `_L` verdict, which every later phase's mesh policy depends on.

- [ ] **Step 1: Inventory the glTF payload by accessor type**

The question "how much of 1.7 GB is float32 position data that could be normalized shorts?" is answerable directly from the glb headers, with no re-encode.

```bash
cat > "$SCRATCH/audit/mesh_inventory.py" <<'PY'
"""Census of meshes/: bytes by glTF accessor semantic and component type."""
import json, struct, sys
from collections import Counter, defaultdict
from pathlib import Path

COMPONENT = {5120: ("i8", 1), 5121: ("u8", 1), 5122: ("i16", 2),
             5123: ("u16", 2), 5125: ("u32", 4), 5126: ("f32", 4)}
COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

def read_gltf_json(path):
    blob = path.read_bytes()
    magic, _version, _length = struct.unpack_from("<4sII", blob, 0)
    assert magic == b"glTF", path
    chunk_length, chunk_type = struct.unpack_from("<II", blob, 12)
    assert chunk_type == 0x4E4F534A, path
    return json.loads(blob[20:20 + chunk_length]), len(blob)

root = Path(sys.argv[1]) / "meshes"
by_semantic = Counter(); by_component = Counter()
file_bytes = vertices = triangles = 0
skirt_bytes = skirt_files = 0
for path in sorted(root.glob("*.glb")):
    gltf, size = read_gltf_json(path)
    file_bytes += size
    if path.name.endswith("__base.glb"):
        skirt_bytes += size; skirt_files += 1
    accessors = gltf.get("accessors", [])
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            for semantic, index in primitive.get("attributes", {}).items():
                accessor = accessors[index]
                name, width = COMPONENT[accessor["componentType"]]
                nbytes = accessor["count"] * COUNTS[accessor["type"]] * width
                by_semantic[semantic] += nbytes
                by_component[(semantic, name)] += nbytes
                if semantic == "POSITION":
                    vertices += accessor["count"]
            if "indices" in primitive:
                accessor = accessors[primitive["indices"]]
                name, width = COMPONENT[accessor["componentType"]]
                by_semantic["INDICES"] += accessor["count"] * width
                by_component[("INDICES", name)] += accessor["count"] * width
                triangles += accessor["count"] // 3

print(f"{file_bytes/1048576:,.1f} MiB on disk, {vertices:,} vertices, {triangles:,} triangles")
print(f"__base skirts: {skirt_files:,} files, {skirt_bytes/1048576:.1f} MiB "
      f"({100*skirt_bytes/file_bytes:.1f}% of meshes/)")
print("\nbytes by attribute:")
for semantic, nbytes in by_semantic.most_common():
    print(f"  {semantic:<12}{nbytes/1048576:9.1f} MiB{100*nbytes/file_bytes:7.1f}% of files")
print("\nby component type:")
for (semantic, name), nbytes in sorted(by_component.items(), key=lambda kv: -kv[1]):
    print(f"  {semantic:<12}{name:<5}{nbytes/1048576:9.1f} MiB")
PY
python "$SCRATCH/audit/mesh_inventory.py" "$BUNDLE" | tee "$SCRATCH/audit/mesh_inventory.txt"
```

The gap between "MiB on disk" and the sum of attribute bytes is glTF JSON + padding overhead across 5,634 files — report it, because it is the number the one-container candidate would attack.

- [ ] **Step 2: Settle the `_L` question — source first, then data**

```bash
./tools/decomp/bin/Release/net8.0/decomp.exe list Game 'Game\.Prefabs\.(ZoneBuildingData|BuildingPrefab|ObjectSubObject|LodData|MeshData)'
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Prefabs.ZoneBuildingData > "$SCRATCH/audit/ZoneBuildingData.cs"
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Prefabs.MeshData          > "$SCRATCH/audit/MeshData.cs"
cat "$SCRATCH/audit/ZoneBuildingData.cs"
grep -n 'm_LodBias\|m_MinLod\|lod\|Lod' "$SCRATCH/audit/MeshData.cs"
```

`ZoneBuildingData` carrying a `m_LevelRange`/`m_ZoneType`-shaped field means `_L<n>` is a zoning level. Then confirm against the data, which is the source that actually settles it:

```bash
python - <<'PY'
import json, os, re, collections
from pathlib import Path
pattern = re.compile(r"^(?P<base>.+?)_L(?P<level>[1-5])_(?P<w>\d+)x(?P<h>\d+)$")
used = collections.Counter()
with open(os.environ["BUNDLE"] + "/instances.jsonl", encoding="utf-8") as handle:
    for line in handle:
        used[json.loads(line)["assetId"]] += 1
families = collections.defaultdict(set)
for asset in used:
    match = pattern.match(asset)
    if match:
        families[match.group("base")].add((match.group("level"), match.group("w") + "x" + match.group("h")))
multi = {b: v for b, v in families.items() if len({lvl for lvl, _lot in v}) > 1}
print(f"{len(families):,} _L families placed in this city")
print(f"{len(multi):,} families where MORE THAN ONE level is placed simultaneously")
for base, variants in list(multi.items())[:5]:
    print(f"  {base}: {sorted(variants)}")
PY
```

**The decisive test:** if the same city simultaneously places `X_L1_2x2` and `X_L5_2x2` at different locations, they cannot be LODs of one object — LODs are alternates of a *single* placement, never co-resident. If instead only one level per family ever appears, the question stays open and must be closed from `MeshData`/LOD fields in source. Write down which test decided it and what it showed.

- [ ] **Step 3: Measure the quantization candidate arithmetically, then verify on a sample**

```bash
cat > "$SCRATCH/audit/mesh_quantize.py" <<'PY'
"""What would meshes/ weigh under KHR_mesh_quantization, and how much error would that be?

Position -> i16 normalized over the per-mesh bounding box; NORMAL/TANGENT -> i8;
TEXCOORD -> u16; indices unchanged. Reports both bytes and worst-case metric error, so the
'lossless?' column can be answered honestly: quantization is NOT bit-exact, and its row must
say so and carry its error bound.
"""
import json, struct, sys
from pathlib import Path

COMPONENT = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
TARGET = {"POSITION": 2, "NORMAL": 1, "TANGENT": 1, "TEXCOORD_0": 2, "TEXCOORD_1": 2,
          "COLOR_0": 1, "JOINTS_0": 1, "WEIGHTS_0": 1}

root = Path(sys.argv[1]) / "meshes"
before = after = 0
worst_error = 0.0
for path in sorted(root.glob("*.glb")):
    blob = path.read_bytes()
    chunk_length = struct.unpack_from("<I", blob, 12)[0]
    gltf = json.loads(blob[20:20 + chunk_length])
    accessors = gltf.get("accessors", [])
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            for semantic, index in primitive.get("attributes", {}).items():
                accessor = accessors[index]
                components = COUNTS[accessor["type"]] * accessor["count"]
                before += components * COMPONENT[accessor["componentType"]]
                after += components * TARGET.get(semantic, COMPONENT[accessor["componentType"]])
                if semantic == "POSITION" and "min" in accessor and "max" in accessor:
                    extent = max(hi - lo for lo, hi in zip(accessor["min"], accessor["max"]))
                    worst_error = max(worst_error, extent / 65534.0)
print(f"attribute bytes before: {before/1048576:9.1f} MiB")
print(f"attribute bytes after:  {after/1048576:9.1f} MiB  ({100*after/before:.1f}%)")
print(f"worst-case position error over all meshes: {worst_error*1000:.3f} mm")
PY
python "$SCRATCH/audit/mesh_quantize.py" "$BUNDLE" | tee "$SCRATCH/audit/mesh_quantize.txt"
```

Then measure generic compressibility as an upper bound on what a container-level compression candidate could give, since neither `gltfpack` nor Draco is installed:

```bash
python - <<'PY'
import lzma, zlib, os, random
from pathlib import Path
root = Path(os.environ["BUNDLE"]) / "meshes"
files = sorted(root.glob("*.glb")); random.Random(0).shuffle(files)
sample = files[:200]
raw = b"".join(p.read_bytes() for p in sample)
print(f"sample {len(sample)} files: {len(raw)/1048576:.1f} MiB")
print(f"  zlib -9 : {len(zlib.compress(raw, 9))/1048576:.1f} MiB")
print(f"  lzma    : {len(lzma.compress(raw))/1048576:.1f} MiB")
PY
```

Concatenating before compressing is deliberate: it measures the cross-file redundancy a single-container candidate would capture and per-file compression cannot. If `gltfpack` happens to be on PATH, run it on the same 200 files and report the measured number instead; otherwise label these rows `arithmetic`.

- [ ] **Step 4: Answer the variant question**

```bash
python - <<'PY'
import json, os, collections
from pathlib import Path
placed = collections.Counter()
with open(os.environ["BUNDLE"] + "/instances.jsonl", encoding="utf-8") as handle:
    for line in handle:
        placed[json.loads(line)["assetId"]] += 1
on_disk = {p.stem for p in (Path(os.environ["BUNDLE"]) / "meshes").glob("*.glb")}
unplaced = on_disk - set(placed)
size = sum((Path(os.environ["BUNDLE"]) / "meshes" / f"{name}.glb").stat().st_size for name in unplaced)
print(f"{len(on_disk):,} meshes on disk, {len(placed):,} distinct assetIds placed")
print(f"{len(unplaced):,} meshes never referenced by an instance: {size/1048576:.1f} MiB")
for name in sorted(unplaced)[:15]:
    print("  ", name)
PY
```

Interpret carefully before proposing anything: sub-object and variant meshes (`__base`, `__stack_*`) are referenced by the *contract* rather than by an `assetId` line, so "unreferenced" here does not mean "unused". Check `docs/bundle-schema.md` §4.1 and §6.1 before writing a row, and say in the doc which unreferenced classes are genuinely dead versus resolved by another mechanism.

- [ ] **Step 5: Write §2**, four questions plus candidate rows. The quantization row must carry its error bound in the "lossless?" column (`NO — ≤ X mm`), per constraint 3. State the `_L` verdict as a heading-level conclusion with the evidence that settled it.

- [ ] **Step 6: Verify**

```bash
python "$SCRATCH/audit/mesh_inventory.py" "$BUNDLE" | head -3     # reproduces
grep -n '_L' docs/cs2-storage-audit.md | head
```

Pass criteria: §2 states the `_L` verdict in one unambiguous sentence with its evidence; the attribute-bytes table sums to within reported JSON overhead of the on-disk total; every quantization row carries an error bound; no candidate proposes deleting a mesh class without first showing it is unreferenced by *both* mechanisms.

- [ ] **Step 7: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §2: meshes — attribute census, _L variant question settled, quantization measured"
```

---

## Task 3: Networks — 423 MB, 9%

The spec is explicit that this is **not** the main event, and the plan must not re-inflate it. The structural facts are already known and validated (`docs/cs2-api-audit.md` §8 and §21): the game keeps one composition mesh plus per-segment matrices and a curve, and `Systems/NetworkRipSystem.GameMesh.cs` already ports that pipeline and already caches piece geometry per composition — then discards the cache to bake `seg_<id>.glb` per segment. The audit's job is to size the win, not to rediscover the mechanism.

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§3)
- Read: `Systems/NetworkRipSystem.GameMesh.cs`, `IO/NetworkWriter.cs`, `docs/bundle-schema.md` §5, §5.1, §5B, `docs/cs2-api-audit.md` §8, §21

**Interfaces:**
- Consumes: `$BUNDLE`, §0 baseline including the reconciled segment counts.
- Produces: §3 and its candidate rows.

- [ ] **Step 1: Measure the dedup ratio, which *is* the estimate**

```bash
python - <<'PY'
import json, os, collections
from pathlib import Path
root = Path(os.environ["BUNDLE"])
by_asset = collections.Counter()
records = 0
with open(root / "networks.jsonl", encoding="utf-8") as handle:
    for line in handle:
        record = json.loads(line)
        records += 1
        by_asset[record.get("attrs", {}).get("asset", "?")] += 1
meshes = sorted((root / "networks").glob("*.glb"))
total = sum(p.stat().st_size for p in meshes)
print(f"{records:,} records, {len(meshes):,} baked meshes, {total/1048576:.1f} MiB")
print(f"{len(by_asset):,} distinct network prefabs (the composition-layer upper bound)")
for asset, count in by_asset.most_common(12):
    print(f"  {count:6,}  {asset}")
print(f"\nmean baked mesh: {total/max(1,len(meshes))/1024:.1f} KiB")
PY
```

The composition layer's cost is roughly *one geometry per distinct composition* plus a matrix-and-curve record per segment. The number of distinct prefabs above is the ceiling on the geometry count; the per-segment record is measurable directly from `lanemeshes.jsonl`, the layer that already did this (27 MB for 71 prefab geometries — the proven precedent). Compute the projected size and state the arithmetic.

- [ ] **Step 2: Confirm the cache-then-discard claim in our own source**

```bash
grep -n 'cache\|Cache\|Dispose\|composition' Systems/NetworkRipSystem.GameMesh.cs | head -40
```

Cite the specific lines where per-composition geometry is built and where it is released, so §3's question 2 ("what do we ship instead") is anchored in code rather than in recollection.

- [ ] **Step 3: State the consumer cost honestly**

A composition layer is contract-visible: a consumer must assemble geometry from composition + matrices + curve instead of loading a baked `.glb`. `lanemeshes/` §5B v2 is the precedent for how that is documented and gated. Write what a consumer has to implement, in three or four concrete sentences, and note that constraint 5 requires it to ship *alongside* `networks/` until proven.

- [ ] **Step 4: Write §3**, four questions plus candidate rows. Include the import-time dimension: 7,248 file opens versus one composition set.

- [ ] **Step 5: Verify** — pass criteria: the projected composition-layer size is arithmetic from measured counts, with the arithmetic shown; §3 does not re-derive anything already settled in `cs2-api-audit.md` §8/§21 but cites it; the framing does not present networks as the largest opportunity.

- [ ] **Step 6: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §3: networks — composition-layer sizing from measured dedup ratio"
```

---

## Task 4: `instances.jsonl` — 310 MB, 7%

1.28 million placements as JSON text. This is the purest representation-versus-information case in the bundle: no semantics to renegotiate, only an encoding. A single sampled record shows where the bytes go — `pos`/`rot` at full float text precision, and `attrs.colorSet` carrying three RGBA quadruples per tree, which is a large share of a tree-heavy city.

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§4)
- Create (scratchpad): `$SCRATCH/audit/instances_census.py`
- Read: `IO/InstanceWriter.cs`, `Domain/InstanceRecord.cs`, `docs/bundle-schema.md` §4, §4.1, §4.2, §4.2a

**Interfaces:**
- Consumes: `$BUNDLE`, §0 baseline.
- Produces: §4 and its candidate rows.

- [ ] **Step 1: Census the bytes by field**

```bash
cat > "$SCRATCH/audit/instances_census.py" <<'PY'
"""Where do instances.jsonl's bytes go, and what would a columnar binary cost?"""
import json, sys, collections
from pathlib import Path

path = Path(sys.argv[1]) / "instances.jsonl"
field_bytes = collections.Counter()
attr_bytes = collections.Counter()
kinds = collections.Counter()
assets = collections.Counter()
records = 0
raw_total = path.stat().st_size
with path.open(encoding="utf-8") as handle:
    for line in handle:
        records += 1
        record = json.loads(line)
        kinds[record.get("kind", "?")] += 1
        assets[record.get("assetId", "?")] += 1
        for key, value in record.items():
            blob = json.dumps({key: value}, separators=(",", ":"))
            field_bytes[key] += len(blob) - len(key) - 4
            if key == "attrs" and isinstance(value, dict):
                for attr_key, attr_value in value.items():
                    attr_bytes[attr_key] += len(json.dumps({attr_key: attr_value}, separators=(",", ":")))

print(f"{records:,} records, {raw_total/1048576:.1f} MiB, mean {raw_total/records:.0f} B/record")
print(f"{len(assets):,} distinct assetIds, {len(kinds)} kinds\n")
print("bytes by top-level field:")
for key, nbytes in field_bytes.most_common():
    print(f"  {key:<12}{nbytes/1048576:9.1f} MiB{100*nbytes/raw_total:7.1f}%")
print("\nbytes by attrs key:")
for key, nbytes in attr_bytes.most_common(20):
    print(f"  {key:<18}{nbytes/1048576:9.1f} MiB{100*nbytes/raw_total:7.1f}%")
print("\nrecords by kind:")
for kind, count in kinds.most_common():
    print(f"  {kind:<12}{count:>10,}")

# What would a packed columnar payload cost? Sizes are exact, not guessed.
#   id u32 | assetId u16 index into a catalogue | kind u8 | pos 3xf32 | rot 3xi16 (quat, w from sign)
#   parent u32 | attrs in typed side-columns
fixed = records * (4 + 2 + 1 + 12 + 6 + 4)
print(f"\ncolumnar fixed part: {fixed/1048576:.1f} MiB for {records:,} records "
      f"({100*fixed/raw_total:.1f}% of current)")
PY
python "$SCRATCH/audit/instances_census.py" "$BUNDLE" | tee "$SCRATCH/audit/instances_census.txt"
```

- [ ] **Step 2: Check the precision question**

Text floats carry whatever precision the writer emitted; a binary f32 carries 24 bits of mantissa. Confirm the encoding is not silently *losing* precision today, and that a f32 column is therefore not a downgrade:

```bash
python - <<'PY'
import json, os, collections
decimals = collections.Counter()
with open(os.environ["BUNDLE"] + "/instances.jsonl", encoding="utf-8") as handle:
    for index, line in enumerate(handle):
        if index >= 200000: break
        for value in json.loads(line)["pos"]:
            text = repr(value)
            decimals[len(text.split(".")[1]) if "." in text else 0] += 1
print("decimal places on pos components (first 200k records):")
for places, count in sorted(decimals.items()):
    print(f"  {places}: {count:,}")
PY
```

Then say plainly in the doc whether a f32 binary column is lossless *relative to what we currently ship* (it is, if the text is already rounded to fewer significant digits than f32 carries) — that distinction is what makes the row lossless under constraint 3.

- [ ] **Step 3: Measure the compressibility floor**

```bash
python - <<'PY'
import lzma, zlib, os
with open(os.environ["BUNDLE"] + "/instances.jsonl", "rb") as handle:
    raw = handle.read(200 * 1024 * 1024)
print(f"sample {len(raw)/1048576:.1f} MiB")
print(f"  zlib -9: {len(zlib.compress(raw, 9))/1048576:.1f} MiB")
print(f"  lzma   : {len(lzma.compress(raw))/1048576:.1f} MiB")
PY
```

This bounds the "just gzip it" alternative, which any columnar proposal must beat to be worth a contract change. Report both, and if plain compression is close, say so — the cheap answer winning is a legitimate outcome.

- [ ] **Step 4: Write §4**, four questions plus candidate rows, including the consumer cost (a reader for a packed payload versus `json.loads` per line) and the import-time win, which is the second half of this candidate's value.

- [ ] **Step 5: Verify** — pass criteria: field-byte shares sum to ~100% of the file; the columnar estimate names every column and its width; the lossless claim is justified against *current shipped precision*, not against the game's internal precision; the gzip baseline is reported alongside.

- [ ] **Step 6: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §4: instances.jsonl — per-field byte census and columnar sizing"
```

---

## Task 5: `lanes.jsonl` — 231 MB, 5%

Lane *paths*, not geometry — genuinely useful to a level builder for traffic and AI, so the data stays; only the encoding is in question. There is a specific, high-value hypothesis to test here: the sampled paths look like evenly-spaced points along a curve (`[[-3954.759,721.689,-988.69],[-3892.965,721.624,-926.896],…]`, 9 points, uniform spacing), and the game stores lanes as a `Bezier4x3` — four control points. If our polyline is a resampling of that Bezier, then storing the Bezier is **fewer bytes and strictly more information**, and the polyline is derivable. Test it; do not assume it.

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§5)
- Create (scratchpad): `$SCRATCH/audit/lanes_bezier.py`
- Read: `IO/LaneWriter.cs` (79 lines — read all of it), `Systems/LaneRipSystem.cs`, `Domain/LaneRecord.cs`, `docs/bundle-schema.md` §5A, `docs/cs2-api-audit.md` §12

**Interfaces:**
- Consumes: `$BUNDLE`, §0 baseline.
- Produces: §5 and its candidate rows.

- [ ] **Step 1: Read the producer and the game side**

```bash
cat IO/LaneWriter.cs
grep -n 'Bezier\|m_Curve\|Sample\|Position(' Systems/LaneRipSystem.cs | head -30
./tools/decomp/bin/Release/net8.0/decomp.exe type Game Game.Net.Curve > "$SCRATCH/audit/Curve.cs"
head -40 "$SCRATCH/audit/Curve.cs"
```

If `LaneRipSystem` samples a `Bezier4x3` at N points to build `path`, question 3 answers itself — the delta is representation, and the sampling is the *lossy* direction we are currently shipping.

- [ ] **Step 2: Census the paths and test the Bezier hypothesis on real data**

```bash
cat > "$SCRATCH/audit/lanes_bezier.py" <<'PY'
"""Path-point census, and: can each path be refit by a cubic Bezier within tolerance?"""
import json, sys, collections
from pathlib import Path
import numpy

path_file = Path(sys.argv[1]) / "lanes.jsonl"
counts = collections.Counter()
kinds = collections.Counter()
records = 0
points_total = 0
errors = []
with path_file.open(encoding="utf-8") as handle:
    for index, line in enumerate(handle):
        record = json.loads(line)
        records += 1
        points = record.get("path", [])
        counts[len(points)] += 1
        points_total += len(points)
        kinds[record.get("kind", "?")] += 1
        if index % 500 == 0 and len(points) >= 4:
            # Fit a cubic Bezier through the endpoints with least-squares interior controls,
            # using uniform parameterisation — the same rule a resampler would have used.
            P = numpy.asarray(points, dtype=float)
            t = numpy.linspace(0.0, 1.0, len(P))
            B = numpy.stack([(1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t**2, t**3], axis=1)
            control, *_ = numpy.linalg.lstsq(B, P, rcond=None)
            errors.append(float(numpy.abs(B @ control - P).max()))

size = path_file.stat().st_size
print(f"{records:,} lanes, {size/1048576:.1f} MiB, {points_total:,} path points")
print(f"mean {points_total/records:.1f} points/lane, {size/records:.0f} B/lane\n")
print("points per lane:")
for n, count in sorted(counts.items())[:15]:
    print(f"  {n:3}: {count:>9,}")
print("\nkinds:"); [print(f"  {k}: {v:,}") for k, v in kinds.most_common()]
if errors:
    errors = numpy.asarray(errors)
    print(f"\ncubic-Bezier refit error over {len(errors):,} sampled lanes (metres):")
    for q in (50, 90, 99, 100):
        print(f"  p{q}: {numpy.percentile(errors, q):.6f}")
print(f"\n4 control points x 3 f32 + id u32 + kind u8 + length f32 = {records*(48+4+1+4)/1048576:.1f} MiB")
PY
python "$SCRATCH/audit/lanes_bezier.py" "$BUNDLE" | tee "$SCRATCH/audit/lanes_bezier.txt"
```

Read the percentiles carefully. A p100 refit error at millimetre scale means the polyline *is* a Bezier sampling and the curve form is exact-or-better. A large p100 means some lanes are genuinely polylines (or use non-uniform parameterisation) and the candidate needs a per-lane fallback — which is a finding, not a failure. Say which.

- [ ] **Step 3: Write §5**, four questions plus candidate rows. If the Bezier hypothesis holds, this row is unusual and should be labelled as such: it *adds* information (the true curve) while *removing* bytes. State the consumer cost — evaluating a cubic Bezier is a four-line function, and `docs/bundle-schema.md` §5B already documents a curve-consuming layer the consumer may have implemented.

- [ ] **Step 4: Verify** — pass criteria: the refit error percentiles are in the doc; the candidate's lossless status is argued from those percentiles with an explicit tolerance, not asserted; the fallback for badly-fitting lanes is described if any exist.

- [ ] **Step 5: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §5: lanes.jsonl — path census and Bezier-refit measurement"
```

---

## Task 6: Markings/decals and terrain — the two confirm-and-move-on domains

Both are small on disk and both are already understood; the audit's job is to record *why they are closed* so a later phase does not reopen them speculatively. Markings are already parametric (`lanemeshes.jsonl` + `lanemeshes/`, 30 MB total) and their cost is **import time**, not bytes: the game draws one instanced projector box per marking with a curve matrix and no CPU per-vertex work, while the harness builds 2.24M vertices and 2.24M ray-casts for the same 83k markings. Terrain is 69 MB of rasters and is expected to yield nothing; confirm and move on.

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§6, §7)
- Read: `docs/bundle-schema.md` §5B, §8, `docs/cs2-api-audit.md` §19, §22, §22a, `Systems/TerrainRipSystem.cs`, `tools/blender_import_bundle.py` (the marking drape path)

**Interfaces:**
- Consumes: `$BUNDLE`, §0 baseline.
- Produces: §6 and §7, each ending in either candidate rows or an explicit "closed, and here is why" statement.

- [ ] **Step 1: Size the markings layer and its import cost**

```bash
python - <<'PY'
import json, os, collections
from pathlib import Path
root = Path(os.environ["BUNDLE"])
records = collections.Counter(); points = 0; count = 0
with open(root / "lanemeshes.jsonl", encoding="utf-8") as handle:
    for line in handle:
        record = json.loads(line); count += 1
        records[record.get("category", "?")] += 1
        points += len(record.get("curve", []))
geo = sum(p.stat().st_size for p in (root / "lanemeshes").glob("*"))
print(f"lanemeshes.jsonl: {(root/'lanemeshes.jsonl').stat().st_size/1048576:.1f} MiB, {count:,} records, {points:,} curve points")
print(f"lanemeshes/: {geo/1048576:.1f} MiB, {len(list((root/'lanemeshes').glob('*'))):,} prefab geometries")
for category, n in records.most_common():
    print(f"  {category}: {n:,}")
PY
grep -nE 'draped|marking|ray_cast|raycast' tools/blender_import_bundle.py | head -20
```

The import-time number belongs in §8 (rip/import time), not in the bytes column. Keep them separate — conflating them is exactly the framing error the spec was written to prevent.

- [ ] **Step 2: Confirm terrain has no cheap win**

```bash
ls -la "$BUNDLE/terrain" && cat "$BUNDLE/terrain/rasters.json"
python - <<'PY'
import os, zlib
from pathlib import Path
import numpy
from PIL import Image
root = Path(os.environ["BUNDLE"]) / "terrain"
for path in sorted(root.glob("*.png")):
    with Image.open(path) as image:
        pixels = numpy.asarray(image)
        mode, size = image.mode, image.size
    raw = pixels.tobytes()
    print(f"{path.name:<22}{path.stat().st_size/1048576:7.2f} MiB  {size} {mode}  "
          f"unique={len(numpy.unique(pixels)):>6}  zlib9={len(zlib.compress(raw,9))/1048576:6.2f} MiB")
PY
```

If the shipped files are already at or near their zlib-9 floor, write "closed" with the number. If `splatmap.png` (11.9 MB) or `heightmap.png` (4.7 MB) shows real headroom, it becomes a candidate row like any other — but do not manufacture one.

- [ ] **Step 3: Write §6 and §7.** Each must end with either candidate rows or an explicit closure sentence carrying the number that closed it. "We looked and there is nothing here, and here is the measurement" is a valid and valuable section.

- [ ] **Step 4: Verify** — pass criteria: no marking or terrain candidate appears in the bytes column without a measured number; the marking import-time cost is stated in the units it was measured in (vertices, ray-casts, seconds) and is filed under §8.

- [ ] **Step 5: Commit**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §6-§7: markings closed as import-time, terrain confirmed near its floor"
```

---

## Task 7: §8 rip time, §9 the ranked table, and the sequencing recommendation

This is the task the whole plan exists for. Everything before it produced per-domain rows; this one makes them comparable, ranks them, and states which phase should run first — which may well contradict the B→C→D→E→F ordering the spec sketched, and is *supposed to* if the measurements say so. The spec is explicit: "Every later phase is chosen from that table, not from this document's guesses."

**Files:**
- Modify: `docs/cs2-storage-audit.md` (§8, §9)
- Read: every prior section of the doc; `docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md`

**Interfaces:**
- Consumes: the candidate tables from §1–§7 and the rip-time attribution from §0.
- Produces: the ranked table that Phases B–F are planned from, and the recommended order with its justification.

- [ ] **Step 1: Complete §8 — rip time by phase**

Assemble from §0's log attribution plus each domain section's rip-time note. One row per rip phase: seconds, instrumented-or-bracketed, and which candidates would move it. State the total and each phase's share, so "estimated rip-time saved" in §9 has a denominator.

- [ ] **Step 2: Build §9 — the ranked table**

One row per candidate from every section, no exceptions and no editorialising them away. Columns, in this order:

| id | domain | change | est. bytes saved | provenance | est. rip-time saved | consumer cost | risk | lossless? | additive-layer shape |

Rules for the table, all from the governing constraints:
- **Sort by bytes saved descending**, then present a second short ordering by *bytes saved per unit of consumer cost* — the two rankings disagreeing is itself a finding worth stating.
- **Every `lossless? = NO` row is visually separated** into its own sub-table below the lossless ones, with its error bound in the cell. Constraint 3: nothing lossy is adopted silently or mixed into a lossless recommendation.
- **`provenance` is `measured`, `arithmetic`, or `unmeasured`** — no blanks.
- **`additive-layer shape`** names the new layer and the layer it would eventually retire, per constraint 5. If a candidate cannot be expressed as an additive layer, say so; that is a risk, not a footnote.

- [ ] **Step 3: Write the sequencing recommendation**

Two or three paragraphs: which phase should run first and why, according to the table. Explicitly compare against the spec's sketched order (B lossless re-encoding → C networks → D meshes → E textures → F import-time) and say where the measurements confirm it and where they overturn it. If the largest measured win is a producer-only PNG filtering change with zero consumer cost, then it outranks a contract-visible networks change and the recommendation must say so plainly rather than deferring to the sketch.

- [ ] **Step 4: Self-review against the spec**

Not a subagent dispatch — read the spec and the doc side by side and check:

```bash
sed -n '47,86p' docs/superpowers/specs/2026-08-08-bundle-representation-audit-design.md   # Phase A section
grep -nE '^#{2,3} ' docs/cs2-storage-audit.md
```

Confirm each of these, and fix inline what fails:
1. Every domain the spec lists has a section: textures, meshes, networks, `instances.jsonl`, `lanes.jsonl`, markings/decals, terrain.
2. Every section answers all four questions, under recognisable headings.
3. Every claim has a number; no hedge words (`grep -ci 'roughly\|probably\|approximately\|should be fine\|TBD\|TODO'` returns 0).
4. Every candidate row has a provenance label and an additive-layer shape.
5. No lossy candidate is recommended without its measurement and its own sub-table.
6. Nothing in the doc proposes an engine-specific format, and nothing redesigns `bundle.json` (that is Phase 7's, per PLAN.md).
7. The two figures the earlier work established as *not* results are not quoted as results — check `docs/cs2-api-audit.md` and the Phase 5 notes before citing any network throughput number.

- [ ] **Step 5: Verify**

```bash
grep -ci 'roughly\|probably\|approximately\|TBD\|TODO' docs/cs2-storage-audit.md   # expect 0
git status --porcelain                                                            # only the audit doc
git diff --stat HEAD -- Systems IO Domain Geodata tools                           # expect empty
```

Pass criteria: the last command produces **no output** — Phase A changed no production code, per constraint 1. The hedge count is 0. §9 has at least one row per domain section or an explicit closure statement in that section.

- [ ] **Step 6: Commit and index**

```bash
git add docs/cs2-storage-audit.md
git commit -m "Audit Phase A §8-§9: rip-time attribution, ranked candidate table, sequencing recommendation"
```

- [ ] **Step 7: Record the outcome in memory**

Update the memory note that pointed here (`bundle-representation-audit-next`) to say Phase A is complete, name the top three candidates with their measured savings, and state the recommended Phase B — so the next session starts from the table rather than from the spec's sketch. Link `[[harness-perf-refactor-2026-08]]` (why measurement-first exists) and `[[check-the-shipped-bundle-first]]`.

---

## After Phase A

**No Phase B–F tasks are written in this plan, deliberately.** The spec's whole premise is that Phase A decides what comes next: "Every later phase is chosen from that table, not from this document's guesses." Writing detailed B–F tasks now would bake in the very assumptions the audit exists to test — the same failure mode as 2026-08-07, when the harness was optimised on the assumption that networks was the expensive layer and two confident plan assumptions were falsified by measurement.

When Task 7 lands, write a **second plan** for the top-ranked phase, following the shape every layer in this repo has used:

1. Additive layer — the new payload ships alongside the one it replaces, contract-documented as a versioned amendment in `docs/bundle-schema.md` §12.1.
2. A/B gate — `tools/ab_render.py` renders old and new, `tools/ab_compare.py` gates pixels (`frac >= 0.999`, `peak <= 16`) and log counts.
3. Validator + tests — `tools/validate_bundle.py` learns the new layer; `tools/test_bundle_contract.py` gains its cases.
4. Contract docs — the new layer documented well enough to build a consumer against without reading the mod's source.
5. Retirement only later — the old layer is removed in a deliberate later bump, once its replacement has passed the gate on a full-city rip.
