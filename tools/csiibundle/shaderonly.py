"""Textureless materials (§7) — the shader-only templates, and what to draw instead.

A CS2 template is sometimes the material's ENTIRE appearance: the game draws water, glass and
some foliage with a compiled shader and no texture, and a glTF loader meeting one falls back to
a white `baseColorFactor` — flat white paint on every window pane and swimming pool. The counts
and the evidence behind every table here are `csiibundle.materials`'s module docstring;
`materials.resolve` applies the substitutes.
"""

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


def template(entry):
    """The CS2 shader template name from `flags`, e.g. `"WaterGeneric"`, or `None`."""
    for flag in (entry or {}).get("flags", ()):
        if flag.startswith("template:"):
            return flag[9:]
    return None


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
