# The flat single-colour patches around terrain cuts — RESOLVED

**Status 2026-08-18: cause proven, fix built and measured, awaiting Naomi's in-game validation.**
The full write-up is **audit §61**. This file is kept for the reasoning trail; §61 is authoritative.

## The answer

**CS2's terrain samples its grounds TRIPLANAR, and the cut mesh projected everything from above.**

`Game.Rendering.TerrainMaterialSystem` sets two shader globals from the theme's
`TerrainRenderSettingsPrefab` — the same prefab §47 already reads the ground tilings from:

```
_TriplanarHeightmapBlending      <- m_TriplanarHeightMapBlending   (0.5)
_TerrainTriplanarBlendStrengthY  <- m_TriplanarBlendStrengthY      (0.2)
```

A horizontal `(x, z)` projection stretches a face by `1/cos(slope)`. The heightfield steps **10-13 m
between adjacent 1.75 m samples** where CS2 graded a corridor into a hillside, so **11.6 % of the cut
mesh's area sits on faces at 85 deg or more**, and **11.0 % of its area sampled the ground at 4-10x**
the intended 40 m period — one texel of a 68 m rock tiling stretched over a whole wall. That renders
as a flat slab of whatever single colour that texel happens to be: cream, near-black, and (in shade)
almost neutral.

**It predicts the pixels.** Observed cream **(199.8, 185.8, 172.8), sigma 0.4**; predicted from the
stretched triangles' own material composite **(198, 186, 172)** — **dE 2**, with 134 of the 373
stretched triangles within 25 of it.

## The fix — `terraincut` v18

Project each face in the plane it is most parallel to (`argmax(|n|)`), per triangle. Geometry
untouched. Faces under 45 deg keep the projection they had, so 83 % of the mesh is unchanged.
Shared code, so the Blender harness gets it too.

Measured before → after, over the same 76,326 triangles / 20,396 m2:

| stretch vs 40 m/uv | before | after |
|---|---|---|
| 1.0-1.2x (nominal) | 75.27 % | **98.52 %** |
| 2-4x | 5.76 % | 0.00 % |
| **>= 4x** | **10.95 % (2,234 m2)** | **0.00 % (0 m2)** |

APPROXIMATED, declared and counted in the run report: the game blends the three projections per
pixel; a baked UV set collapses that to one plane per face. 5,337 faces / 5,023 m2 (24.6 %) project
from a side plane on the reference city.

## What the hypotheses turned out to be

| hypothesis | verdict |
|---|---|
| The albedo gain clips bright ground texels | **dead** (§60.1) — 0 % clipping at both gains |
| Base/detail composite clips in the built textures | **dead** (§60.1) |
| A UV-stretched face freezes the detail texel and goes black | **dead** (§60.1) |
| Inverted winding / downward normals | **dead** (§60.1) — 0 % face downward |
| A missing or unbound texture/material | **dead** — validator clean, and the cooked BC7 `.color`/`.data` DDS both import cleanly per `beamng.log` |
| §60.2 the planar UV stretches steep faces | **CORRECT** — this was the cause |
| §60.4 the planar UV is deliberate and matches BeamNG's terrain | **OVERTURNED** — true of BeamNG, but the authority is the game, and the game is triplanar |
| §49.6 / §60.5 one ground per triangle vs a 4-ground blend | **NOT the cause** — measured: mesh ground vs the built `.ter`'s ground beside it **agrees on 95.1 %** of triangles, and every mismatch is a boundary sliver. The four-stage opacity-mask work was **not built** |

## Still open — the vivid lime green

**(142, 200, 28)**, sampled properly at last, and it is **not any ground**: closest texel in any
ground-mesh composite is dE 60, in any terrain-tier composite dE 51; **no level PNG carries a lime
texel** and **no material has a lime `baseColorFactor`**. So it is a DDS-only texture (the
`art/textures/` BC7 set) or engine state. A handful of small spots, a different defect from the
slabs, and now bounded rather than guessed at.

Also cleared in passing: the **90 orphaned `art/ground/*blend*.dds`** are leftovers from an
abandoned 2026-08-17 14:50 experiment, not from any code in the tree.

## The rules that got this one right

1. **Read the module's contract before measuring it.** Still true — but note §60.4 read the
   contract and then trusted it over the game. A contract is a claim about the game, not the game.
2. **Check the frame and axis convention of every file you measure.** The DAEs are `Z_UP`.
3. **A hypothesis is not a cause until it predicts the observed numbers.** dE 2 is what that looks
   like; "roughly flat" is not.
4. **Kill each hypothesis with a measurement before proposing the next.**
5. **The consumer engine is never the authority.** BeamNG's terrain has no triplanar, so matching
   BeamNG made the mesh confidently wrong. Decompile the game first.
