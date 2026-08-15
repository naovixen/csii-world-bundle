# `tools/` — what each one is for

Everything here is **development and acceptance tooling**. None of it ships with the mod, none
of it is part of the bundle contract, and the mod builds and runs without any of it. The repo's
job is to *produce* a correct bundle; these are how that is checked and how a consumer author
gets started.

Two things here are meant to be reused outside this repo:

* **`csiibundle/`** — the engine-neutral reader package. A consumer imports it.
* **`validate_bundle.py`** — the standalone validator. A consumer author runs it on a bundle
  they were handed.

Everything else is this repo's own harness.

---

## Reuse: the reader package

### `csiibundle/`

Nineteen modules plus `__init__`, ~6,200 lines, **no `bpy` anywhere** — `python -c "import csiibundle"` under a
plain system Python is part of the gate. Dependencies: the standard library plus `numpy`.

```python
import sys, os
sys.path.insert(0, "<repo>/tools")     # Blender's --python does not add it for you
import csiibundle
```

Read `csiibundle/__init__.py` first: it lists every module against the contract section it
implements, and names the minimum viable consumer. `docs/consumer-guide.md` is the route
through it. Every constant is tagged `CONTRACT` (fixed by the format) or `TUNED` (a
presentation choice with a measured justification).

### `validate_bundle.py`

```
python tools/validate_bundle.py <bundle_dir> [--schema-dir DIR] [-q]
```

**Python 3.8+ and the standard library only** — no pip, no game, no mod source, and it does not
import `csiibundle`. Exit 0 means valid; warnings are allowed.

It does two jobs: structural, validating every declared layer against the fifteen schemas in
`docs/schema/` through a small built-in evaluator; and semantic, checking the cross-file
invariants a schema cannot state — assetIds resolve to meshes, textures are named by their own
SHA-256, every `.glb` is well-formed glTF 2.0 whose material names join into `materials.json`,
an area's `heightRanges` runs parallel to its `triangles` and its `owner` joins to a building,
manifest counts match reality, quaternions are unit, `bounds` encloses the content.

A full city takes about **seven minutes**; `docs/example-bundle` takes a second.

---

## The bake: `csiibundle/bake/` — engine-neutral derivatives, once per rip

```
python -m csiibundle.bake [bundle] [--check] [--only markings,areas] [--region X Z R] [--force]
csiibundle-bake ...            # the same, after `pip install -e .` at the repo root
```

The offline second stage of the producer (contract §13). The in-game rip stays raw and fast;
the bake computes what every consumer would otherwise recompute — draped markings, bent lane
furniture, conformed areas, dropped skirts, the terrain hole mask, split PBR maps, generated
LODs — into `<bundle>/derived/`, with a `derived.json` that fingerprints each product's input
layers so a partial re-rip re-bakes only what changed. Products register in
`csiibundle/bake/__init__.py`; the framework (manifest, staleness, progress protocol, CLI) is
gated by `test_bake_manifest.py`. The `cs2beamng` application (`apps/cs2beamng/`) runs it as
its first stages. `python -m csiibundle.bake --list` shows what is registered.

---

## `beamng/` — offline probes against the BeamNG install

* **`probe_ter.py`** — determines the `.ter` heightmap orientation from a shipped BeamNG level
  by sampling under its DecalRoad nodes (roads sit on the terrain). Read-only; re-run on a new
  BeamNG version before trusting `cs2beamng.beamng.ter`. Result on 0.39.2 `small_island`: row 0
  south, col 0 west, `h = stored·maxHeight/65536 + position.z`, 2 cm median.

---

## Building and checking the example bundle

### `make_example_bundle.py`

```
python tools/make_example_bundle.py [output_dir]      # default docs/example-bundle
```

Generates the hand-authored example: one cube mesh, one texture, one material, three instances,
one network segment, and **at least one record in every layer the contract documents**.
Deterministic — re-running rewrites byte-identical files, and a test asserts it.

Its third instance references a prefab with no `.glb`, declared in `warnings.jsonl`. That is
the §10 declared-gap amnesty as a live case rather than as prose: handling it is part of
conforming.

### `test_bundle_contract.py`

```
python tools/test_bundle_contract.py
```

87 tests. Positive: the example validates clean, and its triangle winding really follows §2.2 —
decoded from the GLB and checked against the centroid, not asserted. Negative: **each way a
bundle can be wrong is actually caught**, one test per invariant, because a validator that
accepts everything would prove nothing.

---

## The Blender acceptance harness

### `blender_import_bundle.py`

An **adapter over `csiibundle`**, not a second copy of the reconstruction — it was 6,269 lines
before the extraction and is now a Blender-side driver over the package.

```sh
BLENDER="G:/SteamLibrary/steamapps/common/Blender/blender.exe"
BUNDLE=".../ModsData/Carto"

"$BLENDER" --background --factory-startup --python tools/blender_import_bundle.py -- \
    "$BUNDLE" --networks --terrain --lane-markings --center interchange --radius 350 \
    --save out.blend
```

`--center` takes `interchange`, `downtown`, `auto` or an explicit `X Z`. A full city imports;
a region is what you want for a gate.

**The standard region does not exercise everything.** Four consecutive refactor steps once came
back byte-identical on the interchange while the path they changed had never run. What the
region misses, and the run that covers it:

| path | the run that covers it |
|---|---|
| rigid-tile instancing | `--kinds none --lanes` |
| area conformance | `--kinds building --areas --terrain --center downtown` |
| §5 `ribbon` centreline form (pipe, cable, waterway, taxiway — all off by default) | `--kinds none --networks --net-categories all` |
| per-instance tints | `--kinds building,tree,prop`, then dump `cs2_color*` |
| coarse quads, transition fans, the sub-cut (a region import is stride 1) | a full-map `--kinds none --terrain` |
| `HeightfieldSurface` (the harness keeps its own BVH) | `tools/test_heightfield.py` |

### `ab_render.py`, `ab_compare.py`

```sh
"$BLENDER" --background out.blend --python tools/ab_render.py -- out.png <X> <Y> 528
python tools/ab_compare.py base.png step.png          # pixel diff
python tools/ab_compare.py --counts base.log step.log # log-count diff, secondary signal
```

The camera arguments are the *"aimed N viewport(s) at X … Y …"* line from the import log, not
world coordinates — the harness has already converted them. `ab_compare --counts` strips
elapsed times, so timing jitter never fails a run.

**The pixel gate has a noise floor and it is not zero.** Rendering the *same* `.blend` twice
gives `max=1`; EEVEE is not bit-deterministic. "Pixel-identical" therefore cannot mean a zero
diff, and a step reporting `max=1` or `2` has proved nothing on its own. Where the answer
matters, compare the **scene** instead: dump every object's `matrix_world`, every mesh's
vertex/loop/UV digest and every `FLOAT4X4` attribute from both `.blend` files and diff those.
It is a thirty-line script and it is the reason the extraction's results are facts.

### `probe_point.py`, `inspect_glb.py`, `colour_model.py`

- **`probe_point.py`** — ends arguments about *which* patch of ground is being looked at. Pick a
  spot in Blender; it prints an unambiguous coordinate block.
- **`inspect_glb.py`** — one `.glb` in detail, where `validate_bundle.py` checks a whole bundle
  shallowly. Standalone, stdlib only.
- **`colour_model.py`** — the Blender/CLI adapter over `csiibundle.colour`. The derivation
  itself lives in the package; this keeps the locator, the default bundle path and
  `--explain` working.

---

## Standalone tests

Each of these gates one algorithm that the render A/B either cannot see or would not blame
correctly. Run them with plain `python`.

| test | what it gates |
|---|---|
| `test_bundle_contract.py` | the contract itself — 87 positive and negative cases |
| `test_png_decode.py` | §8 raster decode, against PIL on real rasters, so nothing colour-manages or premultiplies a sample that is not a colour |
| `test_splat_extras.py` | the packed-nibble decode, including a check that scores `Splat.extras` against its own reference decode |
| `test_normal_packing.py` | AG/RG two-channel normal detection and round-trip, with the black itself asserted as `N·L` rather than by eye |
| `test_decal_rows.py` | marking subdivision — that no station's chord dips below its own lift |
| `test_drape_pick.py` | which surface a decal vertex lands on (nearest-to-expected, not highest) |
| `test_clip_cut.py` | the §8c containment invariant — only a deep band carves, and a face is deleted only when a volume contains the whole face |
| `test_node_fan.py` | the junction fan: every cross-section piece stretched across the node's boundary curves |
| `test_heightfield.py` | `csiibundle.drape.HeightfieldSurface`, which the Blender harness never calls because it keeps its own BVH |
| `test_bake_manifest.py` | the bake framework — `derived.json` validates against `docs/schema/derived.schema.json`, staleness is per product from input fingerprints, cancel is honoured between chunks |

---

## `decomp/`

An offline decompiler over `%CSII_MANAGEDPATH%`, answering "what does CS2 actually do?" in real
C#. Not part of the mod and excluded from its compile glob.

```sh
cd tools/decomp && dotnet build -c Release
./bin/Release/net8.0/decomp.exe type Game Game.Rendering.BatchDataHelpers
./bin/Release/net8.0/decomp.exe all  Game /tmp/game-src     # then ripgrep it
```

**Use this, not IL-guessing.** It replaced an IL dumper that produced two wrong readings in one
session — IL answers "what does this call, in what order"; anything with arithmetic or operand
flow needs source. `all` + ripgrep is also how you sweep *every* reader of a field, which is
what "this field feeds only X" claims actually require: two of this project's own documented
claims died to exactly that sweep.

---

## The rule that governs all of it

**The bundle on disk goes stale the moment producer code changes.** `…/ModsData/Carto` is the
output of *one* rip at *one* moment, not a live view. Any change under `Systems/`, `IO/` or
`Domain/` makes those bytes a description of the **old** code, and only a rip in-game refreshes
them.

So: a harness running clean **never** validates a producer change. Say "built, awaiting in-game
re-rip". Check staleness before trusting a bundle:

```sh
git log --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' \
        --since='<bundle.json mtime>' -- Systems IO Domain
```

and note that the check gives false positives — the mod deploys on `dotnet build`, not on
commit, so a commit dated after a rip may already have been live during it. Confirm against
something the rip itself emitted before calling a layer stale.
