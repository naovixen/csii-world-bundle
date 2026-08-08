# Blender Import Harness Performance Refactor — Implementation Plan

> **For agentic workers:** implement this plan task-by-task, in order. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tools/blender_import_bundle.py` fast at full-city scale (numpy hot loops, direct glb parsing, styled progress logging) with pixel-identical output.

**Architecture:** Everything stays in the one script. Three big rewrites — a fast GLB→mesh path inside `import_glb` with per-file fallback to Blender's stock importer, a numpy lane bend with array-block sinks, and numpy terrain (PNG unfilter, classification, mesh build) — plus a `_Log` console helper. An A/B render-diff harness (two small new tools) gates every task against the committed baseline.

**Tech Stack:** Blender 5.2 Python (`bpy`, `mathutils`, bundled `numpy`); system Python 3.13 + PIL only for the *diff tool*; PowerShell for run commands.

## Global Constraints

- Single file: all runtime changes land in `tools/blender_import_bundle.py`. New files allowed only for the A/B harness (`tools/ab_render.py`, `tools/ab_compare.py`).
- No new runtime dependencies beyond `numpy` (bundled with Blender). The harness itself must never import PIL.
- CLI flags, defaults, collection structure, object naming, and all reported **numbers** are frozen. Log formatting may change; counts may not.
- Pixel gate per region: ≥ 99.9% of pixels within 2/255 per channel, no pixel beyond 16/255.
  **One amended exception (2026-08-07, user-approved):** Task 6's Tier 0/1c corrections change
  lane geometry deliberately, so lane-bearing renders use the bounded-difference gate defined in
  Task 6 Step 4. Every other layer, and every other task, keeps the strict gate.
- Keep the reference-implementation character: bilingual comment style where it exists, `CONTRACT VIOLATION` wording, no dead code left behind.
- Blender: `G:\SteamLibrary\steamapps\common\Blender\blender.exe` (not on PATH).
- Bundle: `$env:USERPROFILE\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto\spike`.
- A/B artifacts live under `E:\carto-out\ab\` (never committed).
- The four A/B regions (bundle-frame X Z, radius, camera height):
  - `suburb` 2208 2365 r180 cam 530 — markings, crosswalks, fences
  - `gorge` −1454 5934 r300 cam 560 — splat rock residual, cutout, tunnel
  - `crosswalk` −2810 652 r220 cam 528 — drape over crown
  - `buildings` 2208 2365 r300 cam 530, `--kinds building` — prefab fast path
- Every region A/B run must print identical counts (records bent, draped/missed, calibration line, roadbed/rock/water/cut face counts, network per-category counts) between baseline and refactor. Diff them with the compare tool, not by eye.

---

### Task 1: Step-0 commit of the validated harness state

**Files:**
- Modify: none (commit only)

**Interfaces:**
- Produces: a clean HEAD containing today's validated harness fixes — the baseline every later task diffs against.

- [ ] **Step 1: Commit the harness + docs + validation-command state**

The working tree mixes user-accepted harness fixes with producer changes that await an in-game re-rip. Commit them as two commits so each is reviewable:

```powershell
cd E:\cs2-carto-ripper
git add tools/blender_import_bundle.py docs/bundle-schema.md docs/cs2-api-audit.md .claude/commands/validate-vs-game.md
git commit -m @'
Harness: drape lane decals onto the road surface, fix lane winding, splat residual-rock blend

Markings were invisible for two stacked reasons (fixed lift vs road crown; det -1
loader map never winding-compensated) and terrain squares had three (dirt-channel
calibration demand; per-face rock override - rock is the splatmap's unpainted
residual, not a channel; flat-shaded grid). All consumer-side; validated by the
user against the 2026-08-07 full rip.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
git add Systems/ docs/
git commit -m @'
Producer: tunnel lanes tagged, splatmap semantics string; awaiting re-rip validation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
git status --short   # expect: clean (or only untracked scratch)
```

- [ ] **Step 2: Verify the tests still pass at HEAD**

Run: `cd E:\cs2-carto-ripper; python tools/test_bundle_contract.py`
Expected: `Ran 44 tests … OK`

---

### Task 2: A/B render + compare harness, baseline captures

**Files:**
- Create: `tools/ab_render.py`
- Create: `tools/ab_compare.py`
- Create (artifacts, not committed): `E:\carto-out\ab\baseline\<region>.png`, `E:\carto-out\ab\baseline\<region>.log`

**Interfaces:**
- Produces: `ab_render.py` — Blender script, argv after `--`: `<out.png> <cx> <cy> <cam_z>`; renders the open .blend top-down ortho (scale 400, EEVEE, Standard view transform, 1600²).
- Produces: `ab_compare.py` — system-python CLI: `python tools/ab_compare.py A.png B.png` exits 0 and prints `PASS frac=<f> max=<m>` when ≥99.9% of pixels are within 2/255 and max ≤ 16/255; exits 1 with `FAIL …` otherwise. Also `python tools/ab_compare.py --counts A.log B.log` compares every number in known count lines (`lanes:`, `draped`, `splatmap calibrated`, `terrain:`, `roadbed face`, `networks:`, `category …:`) and exits nonzero on any mismatch.
- Produces: baseline renders + logs for the four regions, generated from HEAD.

- [ ] **Step 1: Write `tools/ab_render.py`**

```python
"""Top-down ortho A/B render of an already-loaded .blend. Argv: out.png cx cy cam_z."""
import sys
import bpy
from mathutils import Vector

out, cx, cy, cz = sys.argv[sys.argv.index("--") + 1:][:4]
cx, cy, cz = float(cx), float(cy), float(cz)

cam_data = bpy.data.cameras.new("ab_topdown")
cam_data.type = "ORTHO"
cam_data.ortho_scale = 400.0
cam_data.clip_end = 2000.0
cam = bpy.data.objects.new("ab_topdown", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = Vector((cx, cy, cz + 400.0))
cam.rotation_euler = (0.0, 0.0, 0.0)

sun_data = bpy.data.lights.new("ab_sun", type="SUN")
sun_data.energy = 4.0
sun = bpy.data.objects.new("ab_sun", sun_data)
bpy.context.scene.collection.objects.link(sun)
sun.rotation_euler = (0.5, 0.3, 0.0)

scene = bpy.context.scene
scene.camera = cam
# Blender renamed the EEVEE-Next engine id back to "BLENDER_EEVEE" once legacy
# EEVEE was removed (Blender 5.x); fall back for older builds that still use
# the "_NEXT" suffix.
_available_engines = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in _available_engines else "BLENDER_EEVEE"
scene.render.resolution_x = scene.render.resolution_y = 1600
scene.render.filepath = out
scene.view_settings.view_transform = "Standard"
bpy.ops.render.render(write_still=True)
print("ab_render wrote", out)
```

(Verbatim brief text set `scene.render.engine = "BLENDER_EEVEE_NEXT"` unconditionally. The
installed Blender is 5.2.0 LTS, where that enum value no longer exists — the hardcoded id
crashes with `TypeError: enum "BLENDER_EEVEE_NEXT" not found in ('BLENDER_EEVEE',
'BLENDER_WORKBENCH', 'CYCLES')`. This probe is what was actually shipped; keep it here so a
future re-run of this plan doesn't reintroduce the crash.)

- [ ] **Step 2: Write `tools/ab_compare.py`**

```python
"""A/B gate: pixel-diff two renders, or diff the numbers in two import logs.

  python tools/ab_compare.py a.png b.png
  python tools/ab_compare.py --counts a.log b.log
"""
import re
import sys

import numpy
from PIL import Image

COUNT_MARKERS = ("lanes:", "draped", "splatmap calibrated", "terrain:", "face(s)",
                 "networks:", "category ", "record(s)", "object(s)")


def compare_pixels(path_a, path_b):
    a = numpy.asarray(Image.open(path_a).convert("RGB")).astype(int)
    b = numpy.asarray(Image.open(path_b).convert("RGB")).astype(int)
    if a.shape != b.shape:
        print(f"FAIL shape {a.shape} vs {b.shape}")
        return 1
    diff = numpy.abs(a - b).max(axis=2)
    frac = (diff <= 2).mean()
    peak = diff.max()
    verdict = "PASS" if frac >= 0.999 and peak <= 16 else "FAIL"
    print(f"{verdict} frac={frac:.5f} max={peak}")
    return 0 if verdict == "PASS" else 1


def count_lines(path):
    keep = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if any(marker in line for marker in COUNT_MARKERS):
                keep.append(re.findall(r"-?\d+(?:\.\d+)?", line))
    return keep


def compare_counts(path_a, path_b):
    a, b = count_lines(path_a), count_lines(path_b)
    if a == b:
        print(f"PASS {len(a)} count line(s) identical")
        return 0
    print(f"FAIL count lines differ ({len(a)} vs {len(b)} lines)")
    for i, (la, lb) in enumerate(zip(a, b)):
        if la != lb:
            print(f"  line {i}: {la} != {lb}")
    return 1


if __name__ == "__main__":
    if sys.argv[1] == "--counts":
        sys.exit(compare_counts(sys.argv[2], sys.argv[3]))
    sys.exit(compare_pixels(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 3: Capture the four baselines from HEAD**

For each region run import → render → keep the log. Template (repeat with the four region rows from Global Constraints; `buildings` swaps `--kinds none --networks --lanes --terrain` for `--kinds building`):

```powershell
$B = "$env:USERPROFILE\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto\spike"
$BL = "G:\SteamLibrary\steamapps\common\Blender\blender.exe"
mkdir E:\carto-out\ab\baseline -Force | Out-Null
& $BL -b --python E:\cs2-carto-ripper\tools\blender_import_bundle.py -- $B `
    --kinds none --networks --lanes --terrain --center 2208 2365 --radius 180 `
    --save E:\carto-out\ab\suburb.blend *> E:\carto-out\ab\baseline\suburb.log
& $BL -b E:\carto-out\ab\suburb.blend --python E:\cs2-carto-ripper\tools\ab_render.py -- `
    E:\carto-out\ab\baseline\suburb.png 2208 2365 530
```

- [ ] **Step 4: Sanity-check the gate against itself**

Run: `python tools/ab_compare.py E:\carto-out\ab\baseline\suburb.png E:\carto-out\ab\baseline\suburb.png`
Expected: `PASS frac=1.00000 max=0`

- [ ] **Step 5: Commit the two tools**

```powershell
git add tools/ab_render.py tools/ab_compare.py
git commit -m @'
A/B gate for the harness refactor: top-down render + pixel/count diff tools

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 3: `_Log` console helper, adopted by every phase

**Files:**
- Modify: `tools/blender_import_bundle.py` (new `_Log` near the top after the constants; every `print(...)` in the import flow routed through it)

**Interfaces:**
- Produces: module-level `LOG = _Log()` with methods later tasks call:
  - `LOG.phase(title: str)` — bold-cyan section header, starts the phase timer
  - `LOG.done(summary: str)` — green, ends the phase, appends `(N.N s)` elapsed
  - `LOG.step(msg: str)` — plain line, two-space indent
  - `LOG.warn(msg: str)` — yellow (red if it contains `CONTRACT VIOLATION`)
  - `LOG.progress(label: str, done: int, total: int | None)` — TTY: in-place `\r` bar redrawn at most every 0.1 s; non-TTY: a plain `  … label done/total` line every 5% (or every 5,000 when total is None). `LOG.progress_end()` closes an in-place bar with a newline.

- [ ] **Step 1: Implement `_Log`**

```python
class _Log:
    """Styled console narration: phase headers, timings, progress, colours.

    Colours and in-place progress bars only when stdout is a real console and NO_COLOR
    is unset; piped output (Select-String workflows) gets plain, line-oriented text.
    All counts stay in the text — the A/B gate diffs the numbers, not the styling.
    """

    def __init__(self):
        self.tty = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if self.tty and os.name == "nt":
            os.system("")  # enable VT processing in classic consoles
        self._phase_started = None
        self._bar_open = False
        self._last_draw = 0.0

    def _c(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.tty else text

    def phase(self, title):
        self.progress_end()
        self._phase_started = time.time()
        print(self._c("1;36", f"==> {title}"))

    def done(self, summary):
        self.progress_end()
        elapsed = f" ({time.time() - self._phase_started:.1f} s)" if self._phase_started else ""
        self._phase_started = None
        print(self._c("32", f"    {summary}{elapsed}"))

    def step(self, msg):
        self.progress_end()
        print(f"  {msg}")

    def warn(self, msg):
        self.progress_end()
        print(self._c("31" if "CONTRACT VIOLATION" in msg else "33", f"  {msg}"))

    def progress(self, label, done, total):
        now = time.time()
        if self.tty:
            if now - self._last_draw < 0.1 and done != total:
                return
            self._last_draw = now
            if total:
                width = 28
                fill = int(width * done / max(total, 1))
                bar = "#" * fill + "-" * (width - fill)
                sys.stdout.write(f"\r  [{bar}] {label} {done}/{total}   ")
            else:
                sys.stdout.write(f"\r  … {label} {done}   ")
            sys.stdout.flush()
            self._bar_open = True
        else:
            stride = max(1, (total // 20) if total else 5000)
            if done % stride == 0 or done == total:
                print(f"  … {label} {done}" + (f"/{total}" if total else ""))

    def progress_end(self):
        if self._bar_open:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()
            self._bar_open = False


LOG = _Log()
```

- [ ] **Step 2: Route the import flow through it**

Mechanical pass over `main` and the build functions — the content of every line is preserved, only the call changes:

- Phase starts: `LOG.phase("Instances")`, `"Networks"`, `"Lane geometry (lanemeshes.jsonl)"`, `"Terrain & water"`, `"Surface areas"`, `"Skirts"` at the existing section boundaries in `main`.
- Existing `print(f"  … {bent} lane(s) bent")` → `LOG.progress("lanes bent", bent, None)` every record (the helper rate-limits); same for `placed` instances, drape verts (`LOG.progress("decal verts draped", drape_seen, to_drape)`), network records, and terrain row loops.
- Existing summary lines (`lanes: …`, `networks: …`, `terrain: …`, `placed N instance(s) …`) → `LOG.done(...)` with the same text.
- `CONTRACT VIOLATION` and skip/warning lines → `LOG.warn(...)`.
- Everything else informational → `LOG.step(...)`.

- [ ] **Step 3: Silence the glTF importer INFO spam**

At the top of `import_glb`, once (module-level flag):

```python
import logging
logging.getLogger("glTFImporter").setLevel(logging.WARNING)
logging.getLogger("io_scene_gltf2").setLevel(logging.WARNING)
```

Verify the two logger names against one noisy import; Blender 5.2 tags the lines `INFO: Data are loaded, start creating Blender stuff` — find the emitting logger with `logging.root.manager.loggerDict` if the two above don't cover it, and pin whatever name it actually is.

- [ ] **Step 4: A/B the suburb region**

```powershell
& $BL -b --python E:\cs2-carto-ripper\tools\blender_import_bundle.py -- $B `
    --kinds none --networks --lanes --terrain --center 2208 2365 --radius 180 `
    --save E:\carto-out\ab\suburb_t3.blend *> E:\carto-out\ab\t3-suburb.log
& $BL -b E:\carto-out\ab\suburb_t3.blend --python E:\cs2-carto-ripper\tools\ab_render.py -- E:\carto-out\ab\t3-suburb.png 2208 2365 530
python tools/ab_compare.py E:\carto-out\ab\baseline\suburb.png E:\carto-out\ab\t3-suburb.png
python tools/ab_compare.py --counts E:\carto-out\ab\baseline\suburb.log E:\carto-out\ab\t3-suburb.log
```

Expected: both `PASS`. Also eyeball the new log once with `-b` in a real console for the styled output, and confirm the `*>`-redirected log contains plain lines, no `\r` garbage, no ANSI codes.

- [ ] **Step 5: Commit**

```powershell
git add tools/blender_import_bundle.py
git commit -m @'
Harness: styled phase/progress logging, glTF importer spam silenced

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 4: numpy PNG unfilter

**Files:**
- Modify: `tools/blender_import_bundle.py` — the two scanline-unfilter loops (16-bit grayscale decoder around line 1400, RGBA8 decoder around line 1470; find them by the `previous = bytearray(stride)` lines)
- Create: `tools/test_png_decode.py` (system-python test, PIL as ground truth)

**Interfaces:**
- Consumes: nothing new.
- Produces: same decoder entry points, unchanged signatures and return types (`width, height, bytes`); internal `_unfilter_scanlines(raw: bytes, width: int, height: int, bpp: int) -> bytearray` shared by both decoders.

- [ ] **Step 1: Write the failing test**

`tools/test_png_decode.py` — extraction-based (the harness imports `bpy` at top, so tests exec the needed functions out of the source, the same pattern the repo already uses):

```python
"""PNG reference-decoder check against PIL, on the real bundle rasters."""
import os
import re
import sys
import zlib

import numpy
from PIL import Image

SRC = os.path.join(os.path.dirname(__file__), "blender_import_bundle.py")
BUNDLE = os.path.expandvars(r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
                            r"\Cities Skylines II\ModsData\Carto\spike")

source = open(SRC, encoding="utf-8").read()
namespace = {"zlib": zlib, "numpy": numpy, "struct": __import__("struct"), "os": os}
for name in ("_unfilter_scanlines", "_decode_png16", "_decode_png_rgba8"):
    match = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass )", source, re.S)
    assert match, f"{name} not found in harness source"
    exec(match.group(0), namespace)

CASES = [
    (os.path.join(BUNDLE, "terrain", "heightmap.png"), "_decode_png16", "I;16"),
    (os.path.join(BUNDLE, "terrain", "splatmap.png"), "_decode_png_rgba8", "RGBA"),
    (os.path.join(BUNDLE, "terrain", "cutout.png"), "_decode_png16", None),
]
failures = 0
for path, decoder, _ in CASES:
    if not os.path.isfile(path):
        print(f"SKIP {path} (no bundle)")
        continue
    width, height, data = namespace[decoder](path)
    truth = numpy.asarray(Image.open(path))
    mine = numpy.frombuffer(bytes(data), dtype=truth.dtype.newbyteorder(">")
                            if truth.dtype.itemsize > 1 else truth.dtype)
    mine = mine.astype(truth.dtype).reshape(truth.shape)
    ok = numpy.array_equal(mine, truth)
    print(("PASS " if ok else "FAIL ") + os.path.basename(path))
    failures += 0 if ok else 1
sys.exit(1 if failures else 0)
```

Adjust the three extracted names to the harness's actual function names before running (read the two decoders first; if the 16-bit decoder is named differently, e.g. `_read_png16`, use that name in both the test and Step 3). The byte-order line matches the harness's current output convention — verify against the existing decoder's return (it stores big-endian 16-bit as PNG does; if it returns host-endian instead, drop the `newbyteorder`).

- [ ] **Step 2: Run it — must fail on `_unfilter_scanlines` missing**

Run: `python tools/test_png_decode.py`
Expected: `AssertionError: _unfilter_scanlines not found in harness source`

- [ ] **Step 3: Implement `_unfilter_scanlines` and use it from both decoders**

```python
def _unfilter_scanlines(raw, width, height, bpp):
    """Undo PNG scanline filters with numpy where the filter allows it.

    None(0) and Up(2) vectorise across the whole row; Sub(1) is a running byte sum —
    a cumulative sum per interleaved byte lane; Average(3) and Paeth(4) are genuinely
    sequential and keep the reference per-byte loop, paid only on rows that use them.
    """
    stride = width * bpp
    out = numpy.zeros((height, stride), dtype=numpy.uint8)
    previous = numpy.zeros(stride, dtype=numpy.uint8)
    for y in range(height):
        offset = y * (stride + 1)
        kind = raw[offset]
        line = numpy.frombuffer(raw, dtype=numpy.uint8,
                                count=stride, offset=offset + 1).copy()
        if kind == 0:
            pass
        elif kind == 2:
            line += previous
        elif kind == 1:
            lanes = line.reshape(-1, bpp).astype(numpy.uint32)
            line = numpy.cumsum(lanes, axis=0, dtype=numpy.uint32).astype(numpy.uint8).reshape(-1)
        else:
            line = _unfilter_slow(kind, bytearray(line), bytes(previous), bpp)
            line = numpy.frombuffer(bytes(line), dtype=numpy.uint8)
        out[y] = line
        previous = out[y]
    return bytearray(out.tobytes())
```

`_unfilter_slow(kind, line, previous, bpp)` is the existing per-byte Average/Paeth code lifted verbatim out of the current loop into a helper. Then replace the body of each decoder's scanline loop with one `_unfilter_scanlines` call (keep each decoder's own zlib/IHDR/IDAT handling and its colour-type validation error messages byte-for-byte).

Note the Sub cumsum: `uint8` cumsum overflows in numpy, hence the widen-to-`uint32`-then-truncate; the truncation *is* the mod-256 the format requires.

- [ ] **Step 4: Run the test — all real rasters must match PIL exactly**

Run: `python tools/test_png_decode.py`
Expected: `PASS heightmap.png`, `PASS splatmap.png`, `PASS cutout.png` (cutout may route through the 8-bit path — if the harness decodes it with a different function, test it with that one).

- [ ] **Step 5: Contract tests + commit**

Run: `python tools/test_bundle_contract.py` → `OK`.

```powershell
git add tools/blender_import_bundle.py tools/test_png_decode.py
git commit -m @'
Harness: numpy PNG unfilter (None/Up/Sub vectorised, Avg/Paeth kept per-byte)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 5: `_mesh_from_arrays` + numpy terrain

**Files:**
- Modify: `tools/blender_import_bundle.py` — new helper after `_Log`; `build_terrain` internals (grid build, face classification, splat calibration sampling, UV layers, roadbed sink, cutout mask, water mesh)

**Interfaces:**
- Consumes: `LOG` from Task 3.
- Produces: `_mesh_from_arrays(name, verts, tris, uv_layers=None, material_runs=None) -> bpy Mesh` where `verts` is float64 `(N,3)` **Blender-frame**, `tris` int `(M,3)`, `uv_layers` is a dict `{layer_name: (M*3, 2) float array in loop order}`, `material_runs` is a list of `(material, tri_count)` consumed in order. Sets smooth shading on every polygon. Later tasks (6) build lane meshes through this exact signature.

- [ ] **Step 1: Implement `_mesh_from_arrays`**

```python
def _mesh_from_arrays(name, verts, tris, uv_layers=None, material_runs=None):
    """Build a mesh from numpy arrays via foreach_set — the hot-path from_pydata.

    verts: (N,3) float64 Blender frame. tris: (M,3) int. uv_layers: {name: (M*3,2)}
    in loop order. material_runs: [(material, tri_count)] in tri order. All polygons
    are smooth-shaded (the game's surfaces are continuous; flat facets read as tiles).
    """
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", numpy.ascontiguousarray(verts, dtype=numpy.float32).ravel())
    tri_count = len(tris)
    mesh.loops.add(tri_count * 3)
    mesh.loops.foreach_set("vertex_index", numpy.ascontiguousarray(tris, dtype=numpy.int32).ravel())
    mesh.polygons.add(tri_count)
    mesh.polygons.foreach_set("loop_start", numpy.arange(0, tri_count * 3, 3, dtype=numpy.int32))
    mesh.polygons.foreach_set("loop_total", numpy.full(tri_count, 3, dtype=numpy.int32))
    if material_runs:
        indices = numpy.empty(tri_count, dtype=numpy.int32)
        slots = {}
        cursor = 0
        for material, count in material_runs:
            if material is not None and material.name not in slots:
                slots[material.name] = len(mesh.materials)
                mesh.materials.append(material)
            indices[cursor:cursor + count] = slots[material.name] if material is not None else 0
            cursor += count
        mesh.polygons.foreach_set("material_index", indices)
    for layer_name, uvs in (uv_layers or {}).items():
        layer = mesh.uv_layers.new(name=layer_name)
        layer.data.foreach_set("uv", numpy.ascontiguousarray(uvs, dtype=numpy.float32).ravel())
    mesh.update()
    mesh.validate()
    mesh.polygons.foreach_set("use_smooth", numpy.ones(tri_count, dtype=bool))
    return mesh
```

Caution learned from the current code: `validate()` may drop invalid faces — arrays must be pre-clean (no degenerate index triples), which the callers guarantee; assert `len(mesh.polygons) == tri_count` after validate and fall back to a `LOG.warn` + rebuild via `from_pydata` if Blender ever disagrees.

Note: the current terrain grid builds **quads**. Triangulate deterministically when converting (`(v00,v10,v11)` + `(v00,v11,v01)` per cell, same diagonal everywhere). Face-classification arrays must be emitted per-triangle (each quad's class repeats twice) so counts double consistently — the *reported* counts stay per-quad-cell as today (report `mask.sum()` over cells, not triangles). The pixel gate will catch any diagonal-direction mistake as lighting differences.

- [ ] **Step 2: Vectorize `build_terrain`**

Replace the per-face Python with array math, preserving every rule:

- Heights: `numpy.frombuffer` the decoded PNG16 → `(H,W)` float64 → `raw * elevationScale + elevationOffset`; window/stride slicing with the same `cols`/`rows` lists.
- Vertex grid: `numpy.meshgrid` of world X/Y + height → `(N,3)`; **Blender frame** conversion is `(x, z_world→y, height→z)` exactly as `bundle_to_blender_point` does today.
- Slope classes: gradients over the sampled grid at the *sampled* spacing (match the current per-face `face_slope_class` inputs — read that function first and reproduce its exact neighbours and thresholds with array shifts, don't invent a new stencil).
- Roadbed/cutout/water masks: nearest-cell lookups into `surface_index`/`cutout`/`water_level` arrays exactly where the scalar code samples them today (same rounding).
- Roadbed sink: `verts[roadbed_vertex_mask, 2] -= _ROADBED_SINK` reproducing the current "which vertices sink" rule verbatim.
- Splat calibration: sample by integer-index arrays (`splat[pz, px]`) with the same stride-of-faces subsampling (`stride_faces = max(1, len(face_rc) // 60000)`) so `counts`/means — and therefore the printed calibration line — are identical.
- UVMap: `(x/8, y/8)` per loop; SplatUV: `splat.uv(x, y)` vectorized (lift its two affine constants out and apply to arrays).
- Water mesh: same construction, through `_mesh_from_arrays`.
- Progress: `LOG.progress("terrain rows", r, len(rows))` per row-block.

- [ ] **Step 3: A/B gorge + suburb**

Run the Task-3 Step-4 command pair for `gorge` (−1454 5934 r300 cam 560) and `suburb`; both `ab_compare.py` pixel and `--counts` must PASS. The gorge exercises rock residual + cutout + water; the suburb exercises roadbed sink + flat calibration.

- [ ] **Step 4: Commit**

```powershell
git add tools/blender_import_bundle.py
git commit -m @'
Harness: numpy terrain build through _mesh_from_arrays

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 6: vectorized lane bend + drape + object build

**Files:**
- Modify: `tools/blender_import_bundle.py` — `_bend_lane_part`, `_subdivide_decal_quad` (array output), `build_lanes` (sink format, drape application, object build via `_mesh_from_arrays`); delete `_SUBDIVIDED_QUADS` frame-memo scaffolding that vectorization subsumes

**Interfaces:**
- Consumes: `_mesh_from_arrays` (Task 5 signature), `LOG`, `_RoadSurface.drop(x, y, z) -> float | None` (unchanged).
- Produces: sink format `{"blocks": [block], "colorSet": …}` where each block is `{"verts": (N,3) float64 bundle frame, "tris": (M,3) int local, "uvs": (M,3,2) float, "material": bpy material, "drape_eps": float | None}`. `build_lanes` concatenates blocks per object; `_bend_lane_part(part, source, curve, spans, curve_length, width_scale, hang, sink)` keeps its exact signature and appends blocks.

- [ ] **Step 1: Make lane part sources array-native**

In `_load_lane_source`, keep the existing extraction but store arrays: `verts (V,3) float64` (bundle frame, same `(-x, z, -y)` map), `tris (T,3) int` (with the Task-1-era winding reversal preserved — reversal now happens ONCE here, `_mesh_from_arrays` emits loops as given, and the `P` compensation moves here too; net effect on emitted triangles must equal today's double-reversal pipeline), `uvs (T,3,2) float64`, `materials`: per-tri material index into a small list (sources can mix materials). Read the current code path end-to-end before writing this: today source tris are reversed in `_load_lane_source` AND again at `from_pydata`; the new single-build path must produce the same final loop order. Get this wrong and every lane goes dark from below — the gorge/suburb A/B renders catch it immediately.

- [ ] **Step 2: Vectorize `_bend_lane_part`**

Per span (shape notes; the actual code follows these exactly):

```python
# rows+1 sample stations along the part, x-left/x-right per station: the subdivided
# quad IS a (rows+1, 2) lattice — generate it directly instead of subdividing a mesh.
# Non-quad sources (fences, cables) keep the general path: their verts' z_fractions
# become the station vector, deduplicated.
stations = numpy.unique(z_fractions)                       # (S,)
arc_pos = numpy.clip(arc_start + stations * arc_per_z, 0.0, arc_limit)   # per tile
t = numpy.interp(arc_pos, arc_table, t_table)              # arc table as arrays
points = _bezier_point_vec(bezier, t)                      # (S,3)
forwards = _bezier_tangent_vec(bezier, t)                  # (S,3), then normalise
rights = numpy.stack([forwards[:, 2], numpy.zeros(len(t)), -forwards[:, 0]], axis=1)
rights /= numpy.linalg.norm(rights[:, (0, 2)], axis=1, keepdims=True).clip(1e-9)
width = width_scale[0] + (width_scale[1] - width_scale[0]) * t_global    # (S,)
world = points[station_of_vert] + rights[station_of_vert] * (local_x * width[station_of_vert])[:, None]
world[:, 1] += local_y + lift - hang_drop_vec
```

- `_arc_table` returns numpy arrays (positions + cumulative lengths) — same sample count and formula as today.
- `_bezier_point_vec` / `_bezier_tangent_vec` are the scalar De Casteljau formulas applied to `(S,)` t-vectors.
- Tiles: loop per tile stays (tile counts are small); each tile emits one block. Decal parts: `rows = max(1, min(64, ceil(band / _DECAL_DRAPE_STEP)))` stations exactly as today, epsilons `_DECAL_DRAPE_MARKING`/`_DECAL_DRAPE_TINT` by the same `"Line" in assetId or "Crosswalk" in assetId` test.
- `_hang_drop` vectorizes to `numpy.polyval`-style evaluation of the same cubic.
- Delete `_SUBDIVIDED_QUADS`, the frames-dict memo, and `_subdivide_decal_quad` (the lattice replaces it); keep `_subdivide_decal_quad`'s guard semantics — a non-quad decal source takes the general path un-subdivided, exactly like its `return source` today.

- [ ] **Step 3: Drape + object build on blocks**

In `build_lanes`: after bending, per builder key concatenate blocks (`numpy.concatenate` verts/tris-with-offsets/uvs; material runs from block order). Drape as amended in Step 3a below. Convert to Blender frame with one array op (`verts[:, [0, 2, 1]]` — X stays, Y↔Z swap; NO sign flips, same as `bundle_to_blender_point`), then `_mesh_from_arrays(f"lane_{prefab_key}", …, uv_layers={"UVMap": uvs}, material_runs=runs)`. Keep `set_paint` and the miss/`drape_missed` counters and every summary line.

- [ ] **Step 3a: Tier 0 + Tier 1 — the drape defect and its cost**

*(Amendment 2026-08-07, user-approved, supersedes "the drape keeps its logic". Evidence:
decompiled `BatchDataSystem.UpdateLaneData` / `BatchDataHelpers.BuildTransformMatrix`; measured
on the full-city bundle. The 2026-08-07 full-city `--lanes` import died before saving — this is
the cause, so these are corrections, not optimisations.)*

The game never bakes marking geometry: it draws one instanced projector box per marking and
projects it onto the depth buffer. Our baked-and-draped equivalent costs 2,240,914 verts,
~1.7 GB of Python lists and 2,240,914 BVH ray-casts for 83,110 markings. Three changes, all
within this task and none touching the bundle contract:

- **Tier 0 — clamp the drape window to the part's own projector half-height.** `_RoadSurface.drop`
  currently probes 3 m up / 8 m down and takes the **highest** hit, which snaps ~2.5% of samples
  onto kerb tops, median walls and bridge railings (measured, 3,300 points / 300 lanes). The game's
  projector reaches **±0.5 m** (bundle median box height 1.00 m). Derive the window per part from
  its own `boundsMin/boundsMax` y-extent in `laneprefabs.json` — half-height up, half-height down,
  no floor below the authored volume. This **deletes** the tuned `probe=3.0, depth=8.0` constants
  rather than retuning them. Where no surface falls inside the window, keep the bent height + lift
  (the existing miss path).
- **Tier 1a — arrays end to end.** The block sinks of Steps 1-3 already do this: ~1.53 GB → ~125 MB.
- **Tier 1b — drape per marking, not per vertex.** Sample the road top a small number of times per
  marking and interpolate along the part, instead of ray-casting every vertex (2,244,534 → ~132,395).
  **Verify before relying on it:** the offline estimate found 45% of sample points had no surface in
  the owner segment's own glb (lanes run on into node meshes), which offline sampling cannot see but
  the full imported surface can. Measure inside Blender on the `crosswalk` region: compare per-vertex
  vs per-marking drape heights over the same lanes and report the distribution of the difference.
  If the one-sample-per-marking error exceeds the marking's own lift (~14 mm) anywhere, fall back to
  a fixed ~16 m sampling interval (~6× fewer casts) rather than per-vertex. Report which you shipped.
- **Tier 1c — subdivide by sagitta, not a fixed 3 m.** Replace `_DECAL_DRAPE_STEP = 3.0` with a
  curvature-driven station count (flat runs get 2 stations, tight curves keep their density):
  2.24M → ~1.06M verts. Keep the 64-station cap.

- [ ] **Step 4: A/B — amended gate for this task**

Tier 0 and Tier 1c change lane geometry **by design**, so lanes cannot be pixel-identical here.
The gate splits:

- **Non-lane layers keep the strict gate.** Run the `buildings` region (`--kinds building`) and a
  terrain+networks-only run of `suburb` (`--kinds none --networks --terrain`, no `--lanes`):
  both must PASS pixel and `--counts` exactly. Any change there is a regression, not a design change.
- **Lane layers get a bounded-difference gate.** For `suburb`, `gorge` and `crosswalk` (full flags),
  the run must complete, `--counts` must match on every non-drape line (`lanes: N record(s) bent
  into M object(s)`, network counts, terrain face counts, calibration line), and the pixel diff
  must be **localised**: ≤ 5% of pixels differing by more than 2/255. Save the baseline and new
  renders side by side and state in the report what moved and why.
- **Every difference must be explainable as Tier 0 or Tier 1c.** A marking that moves onto a kerb,
  disappears, or flips dark is a bug — investigate, do not rationalise. The `draped` count itself
  is expected to change (that is Tier 1b); quote old vs new.
- **Full-city smoke test** — the point of the amendment. Run
  `--kinds none --terrain --networks --lanes` over the whole city and confirm it completes and
  saves. Report wall time, peak RSS, and the final counts.

- [ ] **Step 5: Commit**

```powershell
git add tools/blender_import_bundle.py
git commit -m @'
Harness: numpy lane bend, block sinks, array drape and object build

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 7: fast glb path inside `import_glb`

**Files:**
- Modify: `tools/blender_import_bundle.py` — new `_parse_glb_arrays` + `_fast_import_glb` before `import_glb`; `import_glb` tries fast path first; material registration after stock imports

**Interfaces:**
- Consumes: `_mesh_from_arrays`, `LOG`.
- Produces: `import_glb(path)` — unchanged signature and return contract (list of parentless objects with node transforms baked into `matrix_world`, unlinked from collections). Internal: `_parse_glb_arrays(path) -> list[dict] | None` where each dict is `{"name": str, "matrix": (4,4) float64 glTF-space, "verts": (N,3) f32, "normals": (N,3) f32, "uvs": (N,2) f32 | None, "tris": (M,3) int, "material": str}`; returns `None` whenever ANY guard trips. `_KNOWN_MATERIALS: dict[str, bpy.types.Material]` maps base names to materials.

- [ ] **Step 1: Implement `_parse_glb_arrays` with the guard list**

GLB container parse (magic `0x46546C67`, JSON + BIN chunks — same pattern as the terrain PNG readers: struct + numpy.frombuffer). Guards — return `None` if any holds:

```python
if doc.get("extensionsRequired") or doc.get("skins") or doc.get("animations"):
    return None
for mesh in doc.get("meshes", []):
    for prim in mesh["primitives"]:
        if prim.get("mode", 4) != 4 or prim.get("targets"):
            return None
        attrs = prim["attributes"]
        if not {"POSITION", "NORMAL"} <= set(attrs):
            return None
        if set(attrs) - {"POSITION", "NORMAL", "TEXCOORD_0", "TANGENT"}:
            return None            # COLOR_0 or anything exotic → stock importer
        if "indices" not in prim or "material" not in prim:
            return None
for accessor in doc.get("accessors", []):
    if accessor.get("sparse"):
        return None
```

Accessor reads: componentType 5126 (f32) for attributes, 5123/5125 (u16/u32) for indices, `normalized` false — anything else → `None`. Node walk: flatten the scene graph depth-first composing TRS (or `matrix`) into one (4,4) per mesh-bearing node. Material: `doc["materials"][index].get("name")` — a missing name → `None`.

- [ ] **Step 2: Implement `_fast_import_glb`**

```python
def _fast_import_glb(path):
    """Build import_glb's result without the stock importer, when provably safe.

    Only files whose every material name is already registered qualify — this path
    reuses materials and never constructs one, which is what makes it unable to
    regress rendering. Returns None to mean "use the stock importer".
    """
    prims = _parse_glb_arrays(path)
    if prims is None:
        return None
    for prim in prims:
        if prim["material"] not in _KNOWN_MATERIALS:
            return None
    created = []
    for prim in prims:
        # glTF → Blender axis convention, exactly the stock importer's G: (x, -z, y)
        verts = prim["verts"][:, (0, 2, 1)] * numpy.array([1.0, -1.0, 1.0])
        normals = prim["normals"][:, (0, 2, 1)] * numpy.array([1.0, -1.0, 1.0])
        material = _KNOWN_MATERIALS[prim["material"]]
        uvs = None
        if prim["uvs"] is not None:
            loop_uv = prim["uvs"][prim["tris"].ravel()]
            loop_uv = numpy.stack([loop_uv[:, 0], 1.0 - loop_uv[:, 1]], axis=1)  # glTF v-flip
            uvs = {"UVMap": loop_uv}
        mesh = _mesh_from_arrays(prim["name"], verts, prim["tris"], uv_layers=uvs,
                                 material_runs=[(material, len(prim["tris"]))])
        mesh.normals_split_custom_set_from_vertices(
            numpy.ascontiguousarray(normals, dtype=numpy.float32))
        obj = bpy.data.objects.new(prim["name"], mesh)
        obj.matrix_world = _gltf_node_matrix_to_blender(prim["matrix"])
        created.append(obj)
    return created
```

`_gltf_node_matrix_to_blender(m)` conjugates the node matrix by the axis conversion (`C @ M @ C.inverted()` with `C` = the (x,-z,y) basis map) — verify it against a stock import of one multi-node file by comparing `matrix_world` of matching objects. Note `_mesh_from_arrays` smooth-shades everything; custom split normals override shading, so parity with the stock importer comes from the normals, not the smooth flag.

Winding note: glTF is CCW front-face in a right-handed system and the (x,-z,y) map is a proper rotation, so triangle order is used as-is — no reversal. Confirm on the first A/B render; a mirror-dark building means the loop order needs reversing here (one line).

- [ ] **Step 3: Hook into `import_glb` + registration**

```python
fast = _fast_import_glb(path)
if fast is not None:
    return fast
# … existing stock-importer body …
for obj in imported:
    if obj.type != "MESH":
        continue
    for slot in obj.material_slots:
        if slot.material is not None:
            base = slot.material.name.rsplit(".", 1)[0] \
                if slot.material.name.rsplit(".", 1)[-1].isdigit() else slot.material.name
            _KNOWN_MATERIALS.setdefault(base, slot.material)
            if slot.material.name != base:
                slot.material = _KNOWN_MATERIALS[base]   # reuse the canonical copy
```

That last line makes the stock path converge on canonical materials as it always eventually did via the networks dedup pass; keep that dedup pass in place (it becomes a near-no-op).

Add a per-phase fast/stock counter: `LOG.step(f"{fast_count} glb(s) fast-path, {stock_count} via glTF importer")` at each phase summary.

- [ ] **Step 4: A/B networks + buildings**

- `suburb` full-stack region: pixel + counts PASS (networks now largely fast-path after the first few files).
- `buildings` region (`--kinds building --center 2208 2365 --radius 300`): pixel + counts PASS. This is the custom-normals + node-matrix + v-flip gauntlet; investigate any FAIL by diffing one prefab: import the same glb both ways into an empty scene and compare vertex positions/normals numerically before touching the pixel gate again.

- [ ] **Step 5: Commit**

```powershell
git add tools/blender_import_bundle.py
git commit -m @'
Harness: direct glb fast path with per-file stock-importer fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 8: full-city verification, docs, wrap-up

**Files:**
- Modify: `docs/specs/2026-08-07-import-performance-refactor-design.md` (status line), auto-memory `blender-acceptance-harness.md`

**Interfaces:**
- Consumes: everything.

- [ ] **Step 1: Full-city run**

```powershell
& $BL -b --python E:\cs2-carto-ripper\tools\blender_import_bundle.py -- $B `
    --kinds none --terrain --networks --lanes --save E:\carto-out\terrain-roads-fast.blend `
    *> E:\carto-out\ab\fullcity-fast.log
```

Compare its summary numbers against the user's pre-refactor full-city log (`ab_compare.py --counts` if that log was kept; otherwise diff by eye against the numbers quoted in the session/memory: 116,088 lane records, per-category network counts, calibration line `grass=R … rock=unpainted residual`). Note total wall time and peak RAM (`Get-Process blender`) in the final report — no target, but they are the user-visible outcome.

- [ ] **Step 2: Contract tests + PNG test one last time**

Run: `python tools/test_bundle_contract.py` → `OK`; `python tools/test_png_decode.py` → all PASS.

- [ ] **Step 3: Update the spec status + harness memory**

Spec: `**Status:** implemented 2026-08-07, A/B-verified`. Memory `blender-acceptance-harness.md`: add the styled-log behavior (TTY vs piped), the fast-path/fallback rule, and that A/B tooling lives in `tools/ab_render.py`/`ab_compare.py` with baselines under `E:\carto-out\ab\`.

- [ ] **Step 4: Final commit**

```powershell
git add docs/specs/2026-08-07-import-performance-refactor-design.md
git commit -m @'
Refactor wrap-up: spec marked implemented, full-city A/B numbers recorded

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

## Follow-up specs (out of scope here, recorded 2026-08-07)

Tiers 2 and 3 of the lane/network analysis are real wins but are not this plan's business —
Tier 3 in particular is a **bundle-contract decision**, which this repo owns and a consumer
(BeamNG level builder, Blender importer) feels directly. Each needs its own brainstorm → spec →
plan cycle:

- **Tier 2 — borrow the game's structure for markings.** 47.7% of lane records are chord-straight
  to 5 cm, so their bend is a rigid transform: a linked duplicate of one 4-vert quad plus the
  game's own `isDecal` TRS. Honest end state is Geometry Nodes (instance + raycast in C, no Python
  lists at all). Harness-side only; no contract change.
- **Tier 3 — stop baking `networks/`.** 423 MB of per-instance world-space meshes for 57 distinct
  prefabs, where the game stores one composition mesh + eight matrices. `NetworkRipSystem.GameMesh.cs`
  already ports that pipeline and already caches piece geometry per composition entity, then throws
  it away to bake `seg_<id>.glb`. Emitting a composition catalogue + per-segment bend parameters —
  exactly the `lanemeshes/` + `lanemeshes.jsonl` shape of contract §5B v2 — takes it to single-digit
  MB and ~100 imports instead of 7,080. **Recommendation:** ship it as a NEW layer alongside
  `networks/`, retire the baked layer only once `ab_compare.py` passes against it. This is the
  change most visible to a downstream consumer, and the one with a real contract cost.

See auto-memory `cs2-lane-decals-are-projectors` for the decompiled evidence and measurements.
