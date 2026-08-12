# Handoff — terrain colour fidelity (2026-08-10)

> **SUPERSEDED 2026-08-12 by [`consumer-handoff-plan.md`](consumer-handoff-plan.md)** (phase
> H4). The channel map, the packed nibbles and the ACEScg colour chain are settled; the only
> residual is SPLAT-4. Kept as the record of how the channels were re-derived.

**Status (2026-08-11): the chain is applied correctly and verified end to end, and the import
finally looks right.** The thing that was actually making it look wrong was not colour at all —
Blender was multiplying every ground texture by its alpha channel and drawing the terrain at a
tenth of its albedo (audit §2.5 SPLAT-5). Both leading explanations for the remaining colour
residual — a warm light, and the choice of tonemapper — are now **refuted by measurement**. What
is left is a hue error on warm surfaces, and the next thing to rule out is the screenshot capture
path.

This document exists so the next session re-opens the right question instead of the closed ones.

---

## 1. The open question, stated precisely

**Superseded framing (kept because the shape of the error is the lesson).** This section used to
read: "saturation is ~4× short — ours 0.101 against an in-game 0.442 — and the leading untested
hypothesis is that the game tonemaps with ACES and Blender has no ACES view transform." Both
halves were wrong, and the way they were wrong is the whole point:

* **Blender 5.2 ships ACES.** Its view transforms are `Standard, ACES 1.3, ACES 2.0, Khronos PBR
  Neutral, AgX, Filmic, Filmic Log, False Color, Raw`. Testing the leading hypothesis was one
  assignment, not an OCIO install.
* **0.101 and 0.442 were not the same quantity.** The 0.101 was a 64×64 render of an 8 m patch,
  *averaged* — chroma averaging always desaturates — against a single in-game eyedropper pixel.
  And it was measured through a chain applying about half the grading: saturation on the albedo
  (i.e. before the contrast instead of after), contrast in the *compositor* as a linear node
  (invisible in the viewport, which is where the comparison was made), SMH not at all, then AgX.

**The question as it now stands.** Applied whole, in order, on final radiance, under an ACES view
transform, the shipped sand albedo lands within 0.5% on red and green, and the entire residual is
one channel:

| | R | G | B |
|---|---|---|---|
| required ÷ delivered, green-normalised | 1.005 | 1.000 | **0.674** |
| the same with SMH also replayed | 0.999 | 1.000 | **0.727** |
| with no grading applied at all | 1.174 | 1.000 | 0.514 |

`grading.json` therefore does the great majority of the work; the deliberately-unreplayed SMH
accounts for about a sixth of the remainder; and roughly **27% excess blue** is unattributed. That
is the signature of a *light colour*, not of a missing saturation. Reproduce the table any time
with `colour_model.py --check` — it is a check, not a memory.

**Both leading candidates are now refuted by measurement. Do not re-open either.**

| | verdict |
|---|---|
| A. the game's sun/sky is warmer than our white rig | **REFUTED, 2026-08-11.** The producer now captures it: `lighting.sun.color` is `(1, 1, 1)` with `useColorTemperature` **false** (so the 6570 K is not applied), sky zenith and horizon tints `(1, 1, 1)`, indirect multipliers 1.0, moon off, sun at elevation 66° at 12:01. The light is white. A gain fitted to three surfaces had predicted R ×1.05, B ×0.87; it was wrong, which is exactly why it was never put in the code. |
| B. a different tonemapper would fix it | **REFUTED.** Every view transform Blender ships was run against all three surfaces with the measured white light. ACES 1.3 is already the best by a wide margin (error 0.29 against 0.44 for the next); ACES 2.0, Filmic, AgX and Standard are all worse, and Khronos PBR Neutral throws grass 2× the wrong way. Swapping the tonemap does not close it. |

Note that Blender's ACES 1.3 sends neutral 0.18 grey to a slightly pink `#65605f` — its white
point is unadapted. A D65-adapted ODT moves the residual the *wrong* way.

**What the measurements did find: the WORKING SPACE.** Grading in ACEScg rather than linear
Rec.709 — HDRP's documented HDR path, which this file had skipped on the strength of a bad check —
cut the residual error from 0.29 to 0.18 and took grass from 23% out on blue to within 5%. That
change has been made. The bad check is worth remembering: it compared *pre-tonemap* values on
*one* surface and concluded "under 2%".

**What is still open**, with the chain now complete and the light known white:

| | R | G | B | hue | sat |
|---|---|---|---|---|---|
| sand | 0.896 | 1.000 | 0.818 | +6.9 | +0.024 |
| grass | 0.972 | 1.000 | 1.144 | +1.2 | −0.024 |
| pavement | 0.982 | 1.000 | 0.824 | +6.2 | +0.060 |

Saturation is now nearly right everywhere. What remains is a **hue** error concentrated on the two
warm surfaces (+6.9°, +6.2°) while the green one is nearly exact (+1.2°) — a different shape from
the flat blue deficit this started with, and not something a light or a tonemap choice explains.
The next suspects, in order of cost:

1. **The screenshots themselves.** If Windows HDR or HDR game output was on when they were taken,
   the PNG values are not sRGB display values and every residual above is measuring the capture
   path as much as the game. One SDR-confirmed screenshot settles it. **Ask before doing more
   arithmetic on these numbers.**
2. **Where the white balance sits relative to the space change.** Applying it in ACEScg fits best,
   but that placement is HLSL and cannot be read; it is currently the one step chosen by fit.
3. **HDRP's ACES against a direct ACES 1.0.3 RRT+ODT implementation**, to size the difference
   rather than assume it.

**Secondary symptom, explained and fixed.** "Sand looks ok but everything else is too dark" was
`_GROUND_BRIGHTNESS = 3.09`, a gain applied to the terrain *alone*. The relative measurement
behind it is real (CS2's ground textures are 1.63 stops darker than its object textures) but the
game renders that dark ground too, and the shipped albedo reaches the in-game colour with no
ground-only boost. So the gain did not fix an imbalance, it created one: everything that was not
boosted read 1.63 stops dark as soon as exposure was pulled back to stop the ground blowing out.
Deleted, along with `_VIEW_GAMMA = 1.3`, whose justification ("Blender has no ACES") was false.

---

## 2. Settled — do NOT re-derive these

Each line cost a full in-game rip. Evidence is given so it can be checked cheaply, not repeated.

1. **Splat channel map: `R` = dirt, `G` = grass, base = rock** (the residual of `R` *alone*).
   Evidence: `G` is **exactly 0** on every texel under ≥1 m of water and 0.5 on 45°+ cliffs against
   201.7 on flat land; `R` averages **249.9 underwater**, rising with depth. Slope cannot
   distinguish them — rock is `R`'s own residual, so both soil weights fall with slope together.
2. **`B` and `A` are BIT-PACKED**, two 4-bit weights each: `B.low`=extra1, `B.high`=extra2,
   `A.low`=extra3, `A.high`=extra4. **Normalise a nibble by 15, not 255**, and sample the packed
   channels **point/nearest**. Evidence: `TerrainMaterialSystem.PackToBAChannels` writes four brush
   components at bit offsets 0/4/8/12; `Locale.cok` names the tools 01 Dry / 02 Lush / 03 Dirt /
   **04 Sand**; each nibble is a coherent brush-shaped region with extra4 tracing the shorelines.
3. **All seven ground textures ship**, and `terrain/surfaces/sources.json` records each one's
   source asset — `extra4 ← Sand_temperate_01_Basecolor`. Nothing is missing.
4. **`grading.json` carries the game's resolved grading**: saturation **+30**, contrast **+20**,
   postExposure **+0.75 EV**, whiteBalance temperature **+15**, hueShift 0, colorFilter (1,1,1),
   SMH all identity, tonemapping **ACES**, exposure mode **AutomaticHistogram**.
5. **Ground albedo is authored dark**: the 7 ground textures average **0.093** linear; a
   40-texture sample of object/network textures averages **0.287**. Objects are **3.09× (1.63
   stops) brighter**. This is why raising global exposure until the ground looks right washes out
   roads and water — a display-wide control cannot fix a *relative* imbalance.
6. **It is NOT a colour-space bug.** `MaterialRipSystem.Capture` takes the space from the loaded
   texture's `graphicsFormat`; sRGB round-trips identically through the blit.
7. **Auto exposure cannot be replayed.** In `Automatic*` modes the game meters every frame;
   `fixedExposure` is the Fixed-mode value and is not in effect. Absolute brightness is a property
   of where the camera was pointed, not of the city.

---

## 3. Methodology traps that cost this session (read before touching anything)

- **A curve fit cannot tell you that you are fitting the wrong thing.** A hand-fitted saturation of
  **3.1×** was later shown to be absorbing three unrelated bugs at once; the game's real value is
  **1.3×**. Two separate calibrations were built, "converged" convincingly, and were wrong.
- **Never calibrate before both ends agree, in coordinates, on which patch of ground is under
  discussion.** One rig was tuned to make *rock* match an in-game *beach*. `tools/probe_point.py`
  exists solely to prevent this: it prints an unambiguous coordinate block.
- **The compositor runs on RENDERED output only, never the viewport.** Grading verified in a
  scratchpad render measured a path the user never looks at. Anything that must be visible while
  working goes on the **material** or in **Color Management**.
- **A measurement tool must not change what it measures.** `probe_point.sample_render` forced
  `view_transform = "Standard"`, which silently returned three identical results in a
  view-transform comparison. Fixed, but the class of error is the point.
- **PIL premultiplies RGBA on `resize`** — it collapsed a channel mean from 243.6 to 31.3 and
  produced a completely false picture. Slice to downsample data textures.
- **The bundle goes stale the moment producer code changes.** Check its mtime against
  `git log -- Systems IO Domain`, and confirm against something the rip itself emitted.
- **Blender 5.2 API** (all measured, not remembered): `scene.node_tree` is gone — compositing uses
  `scene.compositing_node_group`; the render reaches that group through a `CompositorNodeRLayers`
  node **inside** it (a Group Input feeds pure black); `CompositorNodeMixRGB` no longer exists;
  `CompositorNodeColorBalance` has **duplicate socket names** (a float *and* a colour for each of
  Lift/Gamma/Gain) and produced a broken render, so it is out of the chain; `Material.blend_method`
  survives as a no-op legacy alias — transparency needs `surface_render_method = "BLENDED"`;
  `use_nodes` is deprecated on Material/World/Scene; EEVEE ships with **raytracing off**.
- **Principled's default white specular costs 64% of the ground's saturation** (0.198 → 0.121) on
  flat sunlit terrain. CS2's ground is diffuse.
- **A grade applied in the wrong PLACE is a different grade.** There are three places to put it in
  Blender and only one is both correct and visible: the compositor is render-only (this is where
  the contrast was, which is why viewport measurements kept disagreeing with the model); the
  materials are visible but carry albedo, not radiance, and sit before the lighting rather than
  after it; the view transform runs on final radiance, in order, in the viewport and F12 alike.
  Saturation is cross-channel, so it can only go in the view — which needs an OCIO config, which
  `colour_model.py --write-ocio` generates from the bundle's own numbers. Measured cost of moving
  it onto the materials instead: **18% on blue** for grass, the same size as the effect under
  investigation.
- **Match on VALUE before comparing colour.** Exposure is the one term the bundle cannot supply,
  so a comparison that does not free it is comparing brightness. `probe_point --render` and
  `colour_model --check` both re-expose the model to the measured value and then report hue and
  saturation.
- **A new OCIO view that is not in `active_views` still works if you name it, but nothing lists
  it** — so an "is this view available?" test says no and silently takes the fallback path while
  the config is loaded and correct. Cost an hour.
- **OCIO's `search_path` string form is colon-separated, and a Windows path has a colon in it.**
  `C:/x:G:/y` parses as four nonsense directories and every FileTransform in the config stops
  resolving — AgX and Filmic included, not just ours. It surfaces as "file not found", which
  reads like a missing file rather than a broken path list. Write the YAML **list** form.
- **`bpy.ops.render.opengl` needs a GL context**, so the viewport path cannot be sampled under
  `blender -b`. Run the probe from the Scripting tab for that number.
- **Blender premultiplies an Image Texture's `Color` output by its ALPHA** unless the image is
  loaded `CHANNEL_PACKED`. CS2 packs data, not opacity, into the alpha of its ground base-colour
  maps, and it averages 0.036–0.133 with no fully-opaque texel — so the terrain drew at a tenth
  of its albedo for as long as this importer has existed (audit §2.5 SPLAT-5). Two lessons
  beyond the fix: `_GROUND_BRIGHTNESS = 3.09` was a 3x gain compensating a 10x bug, and the
  acceptance test that "passed" did so against a synthetic plane with a hardcoded colour, which
  never touched the texture path. **A test that cannot reach the code under test cannot fail.**
- **`Image.pixels` is linear for a 16-bit PNG and sRGB-ENCODED for an 8-bit one.** Measured: a
  linear 0.25 reads back as 0.2500 at 16 bits and 0.5333 at 8. `probe_point.sample_render` wrote
  8-bit and then encoded again, so every "rendered colour" it ever reported was double-encoded —
  too bright and too flat. That is the exact shape of the original "ours: sat 0.101, val 0.733"
  reading that started this whole investigation. Fixed by forcing 16-bit and guarding on
  `img.is_float`. **A measurement tool must not change what it measures** — this is the second
  time this file has broken that rule.

---

## 4. Where things live

| | |
|---|---|
| Contract, terrain/splat/grading | `docs/bundle-schema.md` §8 and the `grading.json` entry |
| Findings + evidence | `docs/game-fidelity-audit.md` §2.5 "SPLAT" |
| Grading capture | `IO/GradingWriter.cs` (main-thread; **must** call `VolumeManager.Update` first) |
| Splat + ground capture | `Systems/TerrainRipSystem.cs` — `CaptureSplatmap`, `CaptureSurfaceTextures` |
| **The derivation, once** | `tools/colour_model.py` — grading.json → constants, `--explain` / `--check` / `--predict` / `--write-ocio`. The importer and the probe both import it so they cannot drift |
| Importer | `tools/blender_import_bundle.py` — `_apply_grading` (Color Management + the OCIO view; nothing on materials), `_diffuse_ground`, `_splat_ground_material` |
| Point probe | `tools/probe_point.py` — 3D cursor → full bundle diagnosis; `--render` reports the VIEWPORT and RENDER paths separately, each as a centre texel (eyedropper-comparable) and a patch mean |
| Game source | `tools/decomp` (real C#; never IL-guess). HLSL is **not** available — anything shader-side cannot be read |
| Memory | `cs2-colour-grading-layer`, `splat-channels-r-is-dirt` |

**Reference coordinates** (bundle frame, this city): sand/extra4 `(5618.0, 2269.5)`, adjacent
grass `(5556.5, 2260.1)`. Use these so results are comparable with everything above.

**Reference colours — mind the provenance.** `#a5915c` is an **in-game** eyedropper of sand.
`#525c2f` (grass), `#5d642e` (sand), `#a7905e` (rock) were **Blender viewport** readings on an
older build and are *not* in-game targets. Conflating the two is what produced the withdrawn
calibration.

---

## 5. Constraints

- **1:1 by default.** A plain import reproduces shipped albedo with no grading; `--colour` is
  opt-in. The user has explicitly rejected hand-tuned constants more than once.
- **Any constant must be derived** — from the bundle, from decompiled game code, or from a
  user-supplied in-game reference at a named coordinate — and its derivation recorded beside it.
- **Never bake grading into the shipped textures.** It is a display transform.
- Producer changes need an in-game rip to validate; only the user can do that. A rip with all
  layer toggles **off** refreshes `bundle.json` + `grading.json` in seconds (the Manifest stage
  runs unconditionally) — use that instead of a 24-minute full rip when only grading changed.

---

## 6. Built on 2026-08-10 (evening), and what is asked next

**Built, offline, no rip needed.** `tools/colour_model.py` derives the whole chain from
`grading.json` once — exposure, the colour-filter × white-balance matrix, the contrast solved
through the ACEScc pivot into an exact power law plus a scalar, and the saturation matrix — and
both the importer and the probe import it. `--write-ocio` emits a Blender OCIO config that adds a
**CS2 Graded** view carrying the whole chain; verified to agree with grading-in-Python plus the
stock ACES 1.3 view to **0.00/255**. `--colour` uses that view when it is active and otherwise
puts the per-channel half into Blender's native Color Management (exact, viewport-visible) and
reports the cross-channel half as MISSING rather than moving it somewhere that reorders the chain.
`_GROUND_BRIGHTNESS` and `_VIEW_GAMMA` are deleted, not retuned.

**The light rig's strength is now derived too, and verified.** `_SUN_ENERGY = 4.0` was arbitrary,
which made every model-vs-render comparison a re-exposure argument and left the graded scene
near-black. For a Lambertian surface under a sun of irradiance `E` at incidence `cos(theta)` plus
a uniform sky of radiance `W`, outgoing radiance is `albedo * (E*cos(theta)/pi + W)`, so radiance
equals albedo exactly when `E = pi * (1 - W) / cos(theta)`. Measured on a flat, unshadowed control
plane carrying the sand albedo: it renders **`#777066`**, which is that albedo to the byte. A 1:1
import now literally shows the albedo it ships.

**Acceptance, measured.** With the CS2 Graded view active, the same control plane renders
**`#907c53`** (hue 40.3, sat 0.424) against the model's predicted **`#917d54`** (hue 40.2, sat
0.422) — within 1/255 per channel. The pass condition for this phase was never "matches the
game" — it was "the scene reproduces the model", so that the remaining residual is a statement
about the world and not about the importer. It does.

**SMH is now replayed too** (added after the screenshot measurements showed it was worth a third
of the blue gap). It is the one step that is neither a matrix nor a power — the three colours are
exact, via `PrepareShadowsMidtonesHighlights`, but the blend across ranges is luminance-weighted —
so the config carries it as a log2 shaper plus a 3D LUT written from the same derivation. The LUT
is **log in, log out**: an earlier cut took log coordinates and emitted linear values, which
interpolated a multiply as a straight line between exponentially spaced nodes and measured 2/255
off. In log space a per-channel gain is an offset, and the same grid comes back to 0.12/255
against the analytic model.

Residuals against the three in-game surfaces, with the whole chain now applied:

| | R | G | B | hue | sat |
|---|---|---|---|---|---|
| sand | 0.977 | 1.000 | 0.724 | +4.8 | +0.084 |
| grass | 1.143 | 1.000 | 0.820 | −5.5 | +0.038 |
| pavement | 1.077 | 1.000 | 0.768 | −0.3 | +0.120 |

Note that a probe of the *terrain* at the sand coordinate reads much darker than this: that point
is shadowed in the rig's sun direction. Compare on lit ground, or free the exposure — the probe
re-exposes the model to the measured value for exactly this reason.

To use it:

```powershell
python tools/colour_model.py --write-ocio <bundle>\colour     # or let --colour write it
$env:OCIO = "<bundle>\colour\config.ocio"
blender.exe scene.blend                                        # pick the "CS2 Graded" view
```

**Done: the screenshots (2026-08-10 evening).** Three full-frame crops — sand, grass, and an area
pavement — compared per texel with exposure freed per shot. Result in audit §2.5 SPLAT-4: the
**neutral pavement** (`#5a5a59` albedo, neutral to one code value) renders in-game at `#b5a882`,
saturation 0.278. A neutral albedo cannot do that; the chroma comes from the light or a late
display step. One pre-tonemap light gain removes **88%** of the chromatic error across all three
surfaces; the surviving ±14% is surface-dependent and so is not a light. **The fitted gain is not
adopted** — it is a prediction for the capture below to confirm or refute.

**Asked of the user next, in cost order.**

1. ~~A screenshot~~ — done, see above.
2. **A manifest-only rip (seconds, all layer toggles off).** `GradingWriter` is now extended and
   **built, awaiting an in-game re-rip** — nothing below is validated until you run one. It adds:
   - `lighting.sun` / `lighting.moon` — colour, `useColorTemperature` + `colorTemperature`
     (HDRP multiplies the two, so both are needed), intensity in lux, travel direction with
     elevation and azimuth in degrees, shadow state;
   - `lighting.sky` — `PhysicallyBasedSky` exposure and zenith/horizon/ground tints;
   - `lighting.indirect` — the bounce multipliers;
   - `lighting.time` — in-game hour/day/year, latitude, longitude. A reference screenshot taken
     at another hour is not describing this grading, and now that is checkable;
   - `exposure.limitMin` / `limitMax` — the clamps that bracket the unreplayable metered value;
   - `tonemapping.lutContribution` / `hasLut` / `lut` — so an `External` rip cannot be mistaken
     for ACES. Not hypothetical: `LightingSystem.SetupPostprocessing` builds a priority-1000
     volume with `TonemappingMode.External` and a per-time-of-day 3D LUT, gated on
     `DayNightCycleData.UseLUT`. It was off at this rip; nothing recorded that;
   - `vignette` — not part of the colour chain, but it is why a screenshot's corners read darker
     than its centre, which matters when a reference colour is sampled off-centre.

   **The assertion to check when it lands:** the fitted light gain says the sun should be
   measurably warm — around R ×1.05, B ×0.87 relative to green, on top of the white-balance
   term. If `lighting.sun` comes back near that, the residual is explained and the *captured*
   number replaces the fitted one. If it comes back neutral, the residual was never the light
   and the tonemap is the remaining suspect.

No full re-rip is needed for any of this.
