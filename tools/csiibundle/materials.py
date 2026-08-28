"""Materials (§7) — the channel packing, and the maps glTF does not carry.

**Read this before wiring a shader, because the most consequential thing here is an absence.**
The `.glb` files describe their materials well enough for a glTF loader to produce something
plausible — base colour, normal, emissive, alpha mode, double-sidedness — and that plausibility
is the trap. Measured on the reference bundle: **every one of 1,449 glTF materials sampled has
NO `metallicRoughnessTexture` and a hard-coded `roughnessFactor` of 0.9**, while **4,563 of
4,784 `materials.json` entries ship a real per-texel `mask`**.

So a consumer that stops at the glTF renders the entire city — every window, metal roof, wet
carriageway, polished floor and painted vehicle — at one flat roughness. Nothing looks broken.
It just looks like plastic. The per-texel values are in `mask`, referenced only from this layer.

**The packing is fixed in v1:**

| map | colour space | channels |
|---|---|---|
| `baseColor` | **sRGB** | RGB albedo, A alpha |
| `normal` | linear | tangent-space, **OpenGL convention (+Y up)** |
| `mask` | linear | **R** metallic · G *not AO, see below* · B unused · **A** smoothness |
| `emissive` | **sRGB** | RGB emission |
| `colorMask` | linear | R/G/B per-texel weights for §7.2's three colours · A snow (advisory) |

**`mask.A` is SMOOTHNESS, so `roughness = 1 - A`.** Getting this backwards is the single most
common way to make a bundle look wrong and it is silent: everything renders too shiny or too
flat, and neither reads as a bug.

**Only R and A are safe to wire, and `mask.G` is NOT ambient occlusion** — settled by measurement
2026-08-17 (audit §54), where it had been an open disagreement between the HDRP reading and CS2's
own asset-authoring documentation. CS2 ships its shaders compiled inside asset bundles, so the
packing is not recoverable from managed code and no amount of reading settles it; what does settle
it is the content. Over **all 890 distinct mask textures** of the reference city:

| channel | mean | shape | verdict |
|---|---|---|---|
| R | 34/255 | 75 % of texels exactly 0, 7 % exactly 255 | **metallic** — binary, as metallic is |
| G | **9/255** | 91 % of texels exactly 0; 34 % of masks all-zero | **not AO** (see below) |
| B | 2/255 | 99 % zero — except `NetCompositionMeshLit` (mean 103) | unused for world assets |
| A | 98/255 | populated everywhere (0.3 % all-zero), uncorrelated with R/G/B | **smoothness** |

An ambient-occlusion map is near-white with dark creases. A channel whose mean is 9/255 is not
one, and the 18 masks (2 %) whose G mean clears 96 are not one either: **nine are uniformly white**
(σ ≤ 0.3 — a no-op) and **nine are bimodal** (σ 40–126, half the texels at 0 and half at 255 — a
selector, not a gradient). Applying all three AO clauses at once (high mean, mostly bright, not
two-level) leaves **2 of 890 = 0.22 %**, both near-white at G mean 162 and 242. So binding G as AO
is either pointless or a hard stencil that paints half a surface black; that is exactly what "the
tunnels are closed off" was. G is also not the emissive/window mask (IoU 0.009 against the emissive map), and
neither is R (IoU 0.011). G and B correlate with each other at +0.64 and with nothing else, so they
are a related pair of effect masks whose meaning stays unknown — recorded as unknown rather than
guessed at again.

`wire_plan()` therefore returns R and A only, and no consumer may bind an occlusion slot from this
layer.

**A normal map is not always three channels, and reading a packed one as RGB shades it BLACK.**
The convention above is what the producer *rebuilds* every normal map to; where a rip predates
that rebuild on some path, the raw two-channel packing reaches disk instead — X in alpha with
red pinned white (DXT5nm), or X in red with blue pinned black (BC5). Both read as RGB give a
nonsense normal that faces away from every light, which is what "the areas are black" was.
`detect_normal_packing()` tells the three apart from a handful of texels and
`unpack_normal()` rebuilds one; a consumer that skips the check gets no warning, only a dark
city.

**`mask` and `colorMask` carry DATA in their alpha channel.** Any renderer that premultiplies
colour by alpha on load will silently scale the other three channels; in Blender that means
`alpha_mode = "CHANNEL_PACKED"`. This is the same trap that once made the whole terrain render
at a tenth of its albedo.

**`flags` names the CS2 SHADER TEMPLATE, and some templates have no maps at all.** Every
material carries it. It matters because a template is sometimes the material's *entire*
appearance: CS2 draws water, glass and some foliage with a shader and no texture, the bundle
cannot export a compiled shader, and a glTF loader meeting a material with no `baseColorTexture`
falls back to `baseColorFactor` — which the producer writes as `[1, 1, 1, 1]`. **Those materials
render as flat white paint.** On the reference city that is 218 materials, all 218 classified:

| template | count | what it needs instead |
|---|---|---|
| `DefaultGlassDoubleSided` | 109 | transmission — every window pane in the city |
| `WaterGeneric` | 48 | `csiibundle.water`'s `Appearance` — pools, ponds, fountains |
| `Grass` | 40 | a foliage colour; the shader tints from the ground |
| `Windows` | 7 | glass. 632 materials use this template and 625 carry maps, so these are panes whose maps did not bind |
| `Default` | 5 | water — the fountains and hot tub, caught by name suffix rather than by template |
| `WaterStream`, `CurvedOverlay`, `CurvedTransparent`, `Placeholder` | 9 | see `tint`, below |

`is_textureless()` finds them and `shader_only_kind(entry, material_id)` says which. **Pass the
id**: the five `Default` ones have no template signal at all and are classified by CS2's own
`_Wat_` name suffix, which 47 sibling materials corroborate. This is the answer to "why are the
swimming pools white".

**`tint` is the flat colour of a material that has no texture.** Only 7 materials carry it and
every one is textureless — an `InvisibleCarPath` overlay ships
`tint: [0.213, 0.602, 0.792, 1.0]` and nothing else. Where it is present it IS the base colour.

Everything else in this table is also inside the `.glb` — see `reader.load_materials`. This
layer's unique content is `mask`, `colorMask`, `flags` and `tint`.
"""

from . import reader
from .normalmaps import (  # noqa: F401  (re-export: §7.1 normal packings)
    NORMAL_AG,
    NORMAL_RG,
    NORMAL_RGB,
    detect_normal_packing,
    normal_x_channel,
    unpack_normal,
)
from .shaderonly import (  # noqa: F401  (re-export: the textureless-material rules)
    SHADER_ONLY_NAME_SUFFIXES,
    SHADER_ONLY_SUBSTITUTE,
    SHADER_ONLY_TEMPLATES,
    TEXTURE_SLOTS,
    is_textureless,
    shader_only_kind,
    template,
    tint,
)

# Which colour space each map must be loaded in. Loading `mask` or `normal` as sRGB applies a
# transfer curve to data that is not colour, which shifts every roughness and every normal.
# CONTRACT (§7.1).
COLOUR_SPACE = {
    "baseColor": "sRGB",
    "emissive": "sRGB",
    "normal": "linear",
    "mask": "linear",
    "colorMask": "linear",
}

# Maps whose ALPHA channel is data, not opacity. A renderer that premultiplies must be told.
# CONTRACT.
DATA_ALPHA = ("mask", "colorMask")

# The `mask` channels this repo will wire, and what each drives. G and B are deliberately
# ABSENT — see the module docstring. CONTRACT for R and A; G/B advisory and unused.
MASK_CHANNELS = {
    "R": "metallic",
    "A": "smoothness",
}

# Tangent-space normal map convention. CS2 authors +Y up (OpenGL); a renderer expecting DirectX
# (-Y) must flip green, or every surface lights from the wrong side of its bumps. CONTRACT.
NORMAL_CONVENTION = "OpenGL"

# What the `.glb` guesses when it has no metallic-roughness texture, which on this producer is
# always. Recorded so a consumer can recognise the default it is replacing.
GLTF_FALLBACK_ROUGHNESS = 0.9
GLTF_FALLBACK_METALLIC = 0.0


def load(bundle_dir):
    """`materials.json` as `{materialId: entry}` — see `reader.load_materials`."""
    return reader.load_materials(bundle_dir)


def semantics(entry):
    """The non-template `flags` — `"glass"`, `"foliage"`, `"decal"` — as a frozenset.

    The producer's own classification, coarser than the template and stable across templates.
    """
    return frozenset(f for f in (entry or {}).get("flags", ()) if not f.startswith("template:"))


def texture_path(bundle_dir, entry, slot):
    """Absolute path of one map of one material, or `None` when that slot is absent.

    An absent slot is normal and means "the game has no such map here", not a missing file.
    """
    import os

    rel = (entry or {}).get(slot)
    return os.path.join(bundle_dir, rel) if rel else None


def wire_plan(entry):
    """What to connect for this material beyond what a glTF loader already does.

    Returns a list of `(slot, channel, target, invert)` tuples:

        [("mask", "R", "metallic", False), ("mask", "A", "roughness", True)]

    `invert` means the target takes `1 - channel` — true for roughness, because the stored value
    is smoothness. An empty list means the glTF alone is faithful for this material.

    Deliberately narrow: it names only the channels §7.1 is certain about. Everything a glTF
    loader handles well — albedo, normal, emissive, alpha mode, double-sidedness — is left to it
    rather than duplicated here, and `mask.G`/`mask.B` are left out because the two available
    readings of them disagree.
    """
    if not entry or "mask" not in entry:
        return []
    return [("mask", "R", "metallic", False), ("mask", "A", "roughness", True)]


def coverage(catalogue):
    """`{slot: count}` over a whole catalogue — how much of each map the bundle actually has.

    The quick way to see what a consumer is leaving on the table: on the reference city 4,563 of
    4,784 materials carry a `mask`, and a glTF-only pipeline uses none of them.
    """
    counts = {}
    for entry in (catalogue or {}).values():
        for slot in entry or ():
            counts[slot] = counts.get(slot, 0) + 1
    return counts


# -- the resolved material: what a renderer needs, decided once ----------------------------------


def resolve(entry, material_id=None, water_appearance=None):
    """One material's §7 record → the engine-neutral resolved dict every consumer wants.

    ```
    {"id": material_id,
     "kind": None | "water" | "glass" | "foliage" | "overlay" | "placeholder",
     "baseColor": path|None, "normal": path|None, "emissive": path|None, "colorMask": path|None,
     "mask": path|None,               # the packed source; split maps are the bake's product
     "metallicSource": ("mask", "R") | None,      "roughnessSource": ("mask", "A", "invert") | None,
     "aoSource": ("mask", "G") | None,             # advisory (§7.1) — a consumer may ignore it
     "baseColorFactor": (r, g, b, a),  # 1,1,1,1 unless textureless (tint or substitute)
     "emissiveFactor": (r, g, b) | None,   # None = the bundle carries none (the usual case)
     "emissiveNits": float | None,         # set by `bake.materials` from the mesh's colour slot
     "roughness": float | None, "metallic": float | None,   # factors when there is no mask
     "alphaMode": "opaque"|"mask"|"blend", "alphaCutoff": float, "doubleSided": bool,
     "flags": [...], "tintable": bool}
    ```

    Decides, once, the things every consumer otherwise re-derives: what a textureless material
    is (`shader_only_kind`) and what to draw for it; where metallic/roughness come from
    (`wire_plan`); the roughness convention (`1 - smoothness`). Pass `water_appearance`
    (`csiibundle.water.Appearance`) so water gets the bundle's own colour and roughness.
    Textures are the record's relative paths, unresolved.
    """
    entry = entry or {}
    kind = shader_only_kind(entry, material_id)
    flat = tint(entry)
    out = {
        "id": material_id,
        "kind": kind,
        "baseColor": entry.get("baseColor"),
        "normal": entry.get("normal"),
        "emissive": entry.get("emissive"),
        "colorMask": entry.get("colorMask"),
        "mask": entry.get("mask"),
        "metallicSource": None,
        "roughnessSource": None,
        "aoSource": None,
        "baseColorFactor": tuple(float(v) for v in (flat or (1.0, 1.0, 1.0, 1.0)))[:4],
        # `None` when the bundle carries none, and it carries none on ANY material of the
        # reference city: `MaterialRipSystem` writes the key only when the surface's
        # `_EmissiveColor` is non-black, and CS2 does not drive emission that way — it drives it
        # per-instance through the mesh's colour slots (`bake.materials` resolves those below).
        # This used to substitute `(0, 0, 0)`, which is truthy, so every consumer's own
        # "no factor -> (1,1,1)" fallback was silently defeated and 2,373 BeamNG materials
        # multiplied their emissive map by ZERO. A missing factor is missing, not black.
        "emissiveFactor": (
            tuple(float(v) for v in entry["emissiveFactor"])[:3]
            if entry.get("emissiveFactor")
            else None
        ),
        # Emissive luminance in nits, when a colour slot has been joined to this material
        # (`bake.materials`); `None` otherwise. Engine-neutral: it is CS2's own
        # `SingleLightMapping.intensity x kIntensityMultiplier`, scaled once, declared there.
        "emissiveNits": None,
        "roughness": None,
        "metallic": None,
        "alphaMode": (entry.get("alphaMode") or "opaque").lower(),
        "alphaCutoff": float(entry.get("alphaCutoff", 0.5) or 0.5),
        "doubleSided": bool(entry.get("doubleSided", False)),
        "flags": list(entry.get("flags") or []),
        "tintable": bool(entry.get("colorMask")),
    }
    for slot, channel, target, invert in wire_plan(entry):
        if target == "metallic":
            out["metallicSource"] = (slot, channel)
        elif target == "roughness":
            out["roughnessSource"] = (slot, channel, "invert" if invert else "direct")
    if entry.get("mask"):
        out["aoSource"] = ("mask", "G")
    if out["metallicSource"] is None:
        out["metallic"] = GLTF_FALLBACK_METALLIC
    if out["roughnessSource"] is None:
        out["roughness"] = GLTF_FALLBACK_ROUGHNESS

    if kind == "water":
        app = water_appearance
        colour = tuple(app.base_color)[:4] if app is not None else (0.08, 0.22, 0.35, 1.0)
        out["baseColorFactor"] = (
            float(colour[0]),
            float(colour[1]),
            float(colour[2]),
            float(app.surface_alpha()) if app is not None else 0.8,
        )
        out["roughness"] = float(app.roughness) if app is not None else 0.05
        out["metallic"] = 0.0
        out["alphaMode"] = "blend"
    elif kind in SHADER_ONLY_SUBSTITUTE:
        sub = SHADER_ONLY_SUBSTITUTE[kind]
        if flat is None:
            out["baseColorFactor"] = tuple(sub["baseColorFactor"])
        elif kind == "glass":
            out["baseColorFactor"] = (flat[0], flat[1], flat[2], sub["baseColorFactor"][3])
        out["roughness"] = sub["roughness"]
        out["metallic"] = sub["metallic"]
        out["alphaMode"] = sub["alphaMode"]
        out["doubleSided"] = out["doubleSided"] or sub["doubleSided"]
    return out


def resolve_all(catalogue, water_appearance=None):
    """`{material_id: resolve(entry)}` for a whole `materials.json` table."""
    return {key: resolve(entry, key, water_appearance) for key, entry in (catalogue or {}).items()}


def texture_set_key(resolved):
    """The tuple two materials share when a renderer could not tell them apart: same maps, same
    alpha mode/cutoff, same double-sidedness, same factors. The bake collapses such materials
    into one (BeamNG's first performance cost is unique materials per view); the alias map it
    writes lets a consumer follow a mesh's material name to the canonical one."""
    return (
        resolved.get("baseColor"),
        resolved.get("normal"),
        resolved.get("mask"),
        resolved.get("emissive"),
        resolved.get("colorMask"),
        resolved.get("kind"),
        resolved.get("alphaMode"),
        round(float(resolved.get("alphaCutoff") or 0.5), 3),
        bool(resolved.get("doubleSided")),
        tuple(round(float(v), 4) for v in resolved.get("baseColorFactor") or ()),
        tuple(round(float(v), 4) for v in resolved.get("emissiveFactor") or ()),
        None if resolved.get("emissiveNits") is None else round(float(resolved["emissiveNits"]), 3),
        None if resolved.get("roughness") is None else round(float(resolved["roughness"]), 4),
        None if resolved.get("metallic") is None else round(float(resolved["metallic"]), 4),
    )
