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
    # `Windows` is a real window template with 632 uses on the reference city, 625 of them
    # carrying maps — so it reaches here only for the 7 whose maps did not bind, and those are
    # window panes. Gated on `is_textureless`, as every entry in this table is.
    "Windows": "glass",
}

# CS2's own material-name suffix, as the second-choice evidence when the template says nothing.
# A textureless material whose template is the generic `Default` has no other signal, and on the
# reference city there are five: `SmallFountain01/02/03_Wat_*`, `LargeFountain03_Wat_*`,
# `HotTub02_Wat_*`. Left unclassified they render as flat white slabs, which is the exact defect
# this module exists to prevent.
#
# This is NOT the assetId substring test that `decalRole` retired. That one guessed at a
# distinction the producer could publish and did not; this one reads a naming convention the
# bundle CORROBORATES 47 times over — every other `_Wat_` material on this city carries the
# explicit `WaterGeneric` template, and every `_Gls_` one carries `DefaultGlassDoubleSided`. It
# fires only when the template gives no answer at all. TUNED (the mapping is evidence-backed but
# it is a naming convention, not a contract).
SHADER_ONLY_NAME_SUFFIXES = (
    ("_Wat_", "water"),
    ("_Gls_", "glass"),
    ("_Win_", "glass"),
)


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


def shader_only_kind(entry, material_id=None):
    """`"water"`, `"glass"`, `"foliage"`, `"overlay"`, `"placeholder"` — or `None`.

    What a textureless material actually is, so a consumer can substitute the right appearance
    rather than leaving it white. `None` means the material has maps and needs no substitute —
    or that it is textureless and nothing here can say what it is, which a caller should surface
    rather than silently skip.

    Pass `material_id` (the `materials.json` key) to enable the name-suffix fallback for
    templates that carry no meaning of their own; see `SHADER_ONLY_NAME_SUFFIXES`. Without it,
    the reference city leaves 5 fountains and hot tubs unclassified.
    """
    if not is_textureless(entry):
        return None

    kind = SHADER_ONLY_TEMPLATES.get(template(entry))
    if kind is not None:
        return kind

    for suffix, named in SHADER_ONLY_NAME_SUFFIXES:
        if material_id and (suffix in material_id):
            return named

    return None


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
    return [("mask", "R", "metallic", False), ("mask", "A", "roughness", True)]


# The three layouts a normal map in this bundle can arrive in, and what carries X in each.
# §7.1 promises the first; the other two are what a two-channel packing looks like when it
# reaches disk unrebuilt, and reading one of them as if it were the first is silent and total —
# see `detect_normal_packing`.
NORMAL_RGB = "rgb"  # X in red, Y in green, Z in blue. The contract.
NORMAL_AG = "ag"  # DXT5nm/BC3n: X in ALPHA, Y in green, red pinned white, Z reconstructed
NORMAL_RG = "rg"  # BC5: X in red, Y in green, blue pinned black, Z reconstructed

# What "pinned" means, in 0-1. Block compression dents a constant channel by a few counts, so
# these are not exact tests, and the fraction below is what actually decides.
_PINNED_HIGH = 250.0 / 255.0
_PINNED_LOW = 5.0 / 255.0
_PINNED_FRACTION = 0.9


def detect_normal_packing(samples):
    """Which of `NORMAL_RGB` / `NORMAL_AG` / `NORMAL_RG` a normal map is stored in.

    **A packed normal read as an RGB one does not look subtly wrong, it looks black.** A
    two-channel normal map ships X and Y and lets the shader reconstruct Z, and both of the
    layouts Unity's own `UnpackNormalmapRGorAG` straddles reach this bundle: AG pins red at
    white and hides X in alpha, so reading RGB gives `normal.x = +1` — a shading normal lying
    flat along the surface, which on level ground faces 90 degrees away from any sun and shades
    black. RG pins blue at black, so reading RGB gives `normal.z = -1`, a normal pointing into
    the surface, which shades black too. Measured on the reference city: 20 of 35 area surface
    normals are AG and 2 are RG, which is why every painted area rendered black.

    `samples` is any iterable of `(r, g, b, a)` floats in 0-1 — a subsample of the image is
    plenty, since what separates the layouts is a channel being CONSTANT across the whole map.
    The tests are the producer's own (`MaterialRipSystem.ReconstructTwoChannelNormal`), so a
    consumer and the ripper agree on what a packed map is.

    Returns `NORMAL_RGB` for anything that matches neither signature — including a map this
    cannot classify. Guessing at an unrecognised layout would invent relief the game does not
    draw; leaving it alone renders what the bundle actually says.
    """
    total = high_red = low_blue = low_red = 0
    varying_alpha = False
    for r, _g, b, a in samples:
        total += 1
        if r >= _PINNED_HIGH:
            high_red += 1
        if r <= _PINNED_LOW:
            low_red += 1
        if b <= _PINNED_LOW:
            low_blue += 1
        if a < _PINNED_HIGH:
            varying_alpha = True
    if total == 0:
        return NORMAL_RGB

    threshold = total * _PINNED_FRACTION
    # Each layout needs a POSITIVE signature as well as a pinned channel. AG needs alpha to be
    # carrying something: a map whose alpha is constant has no X in it. RG needs the same of RED,
    # which is where it keeps X — without that clause a black or near-black placeholder normal
    # (blue dark everywhere, red nowhere near white) reads as RG and gets "unpacked" from
    # x = y = -1 into a uniform sideways normal, which is the exact defect this detects.
    if (high_red >= threshold) and varying_alpha:
        return NORMAL_AG
    if (low_blue >= threshold) and (high_red < threshold) and (low_red < threshold):
        return NORMAL_RG
    return NORMAL_RGB


def normal_x_channel(packing):
    """Which channel holds X under `packing` — `"A"`, `"R"`. Y is always green, Z always derived.

    `None` for `NORMAL_RGB`, where nothing needs rebuilding.
    """
    return {NORMAL_AG: "A", NORMAL_RG: "R"}.get(packing)


def unpack_normal(packing, r, g, b, a):
    """One texel as a tangent-space `(x, y, z)` in -1..1. The reference the node graph mirrors.

    Z is reconstructed rather than read for the packed layouts, exactly as the game's shader
    does — the stored blue is meaningless in both of them.
    """
    if packing == NORMAL_RGB:
        return (2.0 * r - 1.0, 2.0 * g - 1.0, 2.0 * b - 1.0)
    x = 2.0 * (a if packing == NORMAL_AG else r) - 1.0
    y = 2.0 * g - 1.0
    return (x, y, max(0.0, 1.0 - x * x - y * y) ** 0.5)


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

# Substitutes for the shader-only kinds — a flat colour, roughness, and how the surface is
# drawn. Glass and foliage are TUNED (the game's shaders are compiled and ship no numbers);
# water is EXACT from `rasters.water` and is filled in by `resolve` from the bundle's block.
SHADER_ONLY_SUBSTITUTE = {
    "glass": {
        "baseColorFactor": (0.92, 0.96, 1.0, 0.35),
        "roughness": 0.05,
        "metallic": 0.0,
        "alphaMode": "blend",
        "doubleSided": False,
    },
    "foliage": {
        "baseColorFactor": (0.28, 0.42, 0.18, 1.0),
        "roughness": 0.85,
        "metallic": 0.0,
        "alphaMode": "opaque",
        "doubleSided": True,
    },
    "overlay": {
        "baseColorFactor": (0.5, 0.5, 0.5, 0.5),
        "roughness": 0.9,
        "metallic": 0.0,
        "alphaMode": "blend",
        "doubleSided": False,
    },
    "placeholder": {
        "baseColorFactor": (0.6, 0.6, 0.6, 1.0),
        "roughness": 0.9,
        "metallic": 0.0,
        "alphaMode": "opaque",
        "doubleSided": False,
    },
}


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
