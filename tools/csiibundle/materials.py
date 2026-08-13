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
| `mask` | linear | **R** metallic · G ambient occlusion · B detail/emissive · **A** smoothness |
| `emissive` | **sRGB** | RGB emission |
| `colorMask` | linear | R/G/B per-texel weights for §7.2's three colours · A snow (advisory) |

**`mask.A` is SMOOTHNESS, so `roughness = 1 - A`.** Getting this backwards is the single most
common way to make a bundle look wrong and it is silent: everything renders too shiny or too
flat, and neither reads as a bug.

**Only R and A are safe to wire.** `mask.G` and `mask.B` are the producer's reading of CS2 as an
HDRP derivative; the game's own asset-authoring documentation describes them instead as a
glossy-coat layer and an unused channel. CS2 ships its shaders compiled inside asset bundles, so
the packing is not recoverable from managed code and the disagreement is recorded rather than
resolved. Both readings agree on R and on A. `wire_plan()` therefore returns those two and
leaves G and B out on purpose — do not "improve" it by feeding G in as ambient occlusion.

**`mask` and `colorMask` carry DATA in their alpha channel.** Any renderer that premultiplies
colour by alpha on load will silently scale the other three channels; in Blender that means
`alpha_mode = "CHANNEL_PACKED"`. This is the same trap that once made the whole terrain render
at a tenth of its albedo.

**`flags` names the CS2 SHADER TEMPLATE, and some templates have no maps at all.** Every
material carries it. It matters because a template is sometimes the material's *entire*
appearance: CS2 draws water, glass and some foliage with a shader and no texture, the bundle
cannot export a compiled shader, and a glTF loader meeting a material with no `baseColorTexture`
falls back to `baseColorFactor` — which the producer writes as `[1, 1, 1, 1]`. **Those materials
render as flat white paint.** On the reference city that is 218 materials:

| template | count | what it needs instead |
|---|---|---|
| `DefaultGlassDoubleSided` | 109 | transmission — every window pane in the city |
| `WaterGeneric` | 48 | `csiibundle.water`'s `Appearance` — pools, ponds, fountains |
| `Grass` | 40 | a foliage colour; the shader tints from the ground |
| `WaterStream`, `CurvedOverlay`, `CurvedTransparent`, `Placeholder` | 9 | see `tint`, below |

`is_textureless()` finds them and `template()` says which. This is the answer to "why are the
swimming pools white".

**`tint` is the flat colour of a material that has no texture.** Only 7 materials carry it and
every one is textureless — an `InvisibleCarPath` overlay ships
`tint: [0.213, 0.602, 0.792, 1.0]` and nothing else. Where it is present it IS the base colour.

Everything else in this table is also inside the `.glb` — see `reader.load_materials`. This
layer's unique content is `mask`, `colorMask`, `flags` and `tint`.
"""

from . import reader

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


# The map slots whose presence means "this material has real texture data". A material with
# none of them is drawn entirely by its shader template.
TEXTURE_SLOTS = ("baseColor", "normal", "mask", "emissive")

# Templates that are pure shader and ship no maps, with what each actually is. A consumer must
# substitute an appearance for these or they render white. CONTRACT (the names; the treatment is
# the consumer's).
SHADER_ONLY_TEMPLATES = {
    "DefaultGlassDoubleSided": "glass",
    "WaterGeneric": "water",
    "WaterStream": "water",
    "Grass": "foliage",
    "CurvedOverlay": "overlay",
    "CurvedTransparent": "overlay",
    "Placeholder": "placeholder",
}


def load(bundle_dir):
    """`materials.json` as `{materialId: entry}` — see `reader.load_materials`."""
    return reader.load_materials(bundle_dir)


def template(entry):
    """The CS2 shader template name from `flags`, e.g. `"WaterGeneric"`, or `None`."""
    for flag in (entry or {}).get("flags", ()):
        if flag.startswith("template:"):
            return flag[9:]
    return None


def semantics(entry):
    """The non-template `flags` — `"glass"`, `"foliage"`, `"decal"` — as a frozenset.

    The producer's own classification, coarser than the template and stable across templates.
    """
    return frozenset(f for f in (entry or {}).get("flags", ()) if not f.startswith("template:"))


def is_textureless(entry):
    """Whether this material ships NO maps, so a glTF loader will render it flat white.

    See the module docstring for the table of which templates these are and what each needs.
    """
    return not any(slot in (entry or {}) for slot in TEXTURE_SLOTS)


def shader_only_kind(entry):
    """`"water"`, `"glass"`, `"foliage"`, `"overlay"`, `"placeholder"` — or `None`.

    What a textureless material actually is, so a consumer can substitute the right appearance
    rather than leaving it white. `None` means the material has maps and needs no substitute.
    """
    if not is_textureless(entry):
        return None
    return SHADER_ONLY_TEMPLATES.get(template(entry))


def tint(entry):
    """The material's flat RGBA colour when it ships one, else `None`.

    Present only on textureless materials, where it IS the base colour.
    """
    value = (entry or {}).get("tint")
    return tuple(value[:4]) if value else None


def texture_path(bundle_dir, entry, slot):
    """Absolute path of one map of one material, or `None` when that slot is absent.

    An absent slot is normal and means "the game has no such map here", not a missing file.
    """
    import os
    rel = (entry or {}).get(slot)
    return os.path.join(bundle_dir, rel) if rel else None


def roughness_from_smoothness(smoothness):
    """`1 - smoothness`. Trivial, and named so the call site says which way round it is."""
    return 1.0 - smoothness


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
    return [("mask", "R", "metallic", False),
            ("mask", "A", "roughness", True)]


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
