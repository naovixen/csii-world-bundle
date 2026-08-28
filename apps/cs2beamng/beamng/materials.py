"""BeamNG materials v1.5 from the bake's resolved material table, and the texture export.

Conventions (from shipped `main.materials.json` files and the v1.5 docs):

* One object per material: `{"name", "mapTo", "class": "Material", "persistentId", "version":
  1.5, "Stages": [{...}, {}, {}, {}], flags...}`. `mapTo` is the name the `.dae` binds
  (`<material name="...">`), so it is what `dae.sanitize` produces for the same string.
* Stage 0 maps: `baseColorMap` (`.color.png`, sRGB, alpha kept when the material cuts or
  blends), `normalMap` (`.normal.png`), `roughnessMap` / `metallicMap` (`.data.png`, linear,
  from the bake's split mask — there is **no** `ambientOcclusionMap`, because the mask's G
  channel is not occlusion; audit §54), `emissiveMap`,
  `colorPaletteMap` (the CS2 colour mask: RGB selects `instanceColor/1/2` — the palette tint
  path). Scalars: `roughnessFactor`, `metallicFactor`, `diffuseColor`, `emissiveFactor`.
* Alpha: v1.5 takes opacity from an **`opacityMap`** (464 of ~500 vanilla translucent materials
  and every alphaTest one carry it), NOT from the albedo's alpha — load #7 drew every marking
  decal as an opaque white box. So mask/blend materials get the albedo alpha exported as a grey
  `_o.data.png`, `opacityFactor` = the base-colour factor's alpha; `"mask"` → `alphaTest` +
  `alphaRef`; `"blend"` → `translucent` + `LerpAlpha` + `translucentZWrite`. `doubleSided` as given.
* Every material name carries the level name (materials are a global registry; see the
  terrain lesson) — `matbind.mapto(level_name, raw_name)`, next door.

Textures: `export_texture` copies a bundle/derived PNG into `art/textures/` under the BeamNG
suffix for its colour space, box-downsampled to `max_size` (0 = full). Colour maps keep alpha
only when a material needs it; everything else is written RGB or grey (an unused alpha is
read as coverage by more than one BeamNG loader — the terrain packer already bit us).
"""

# Per-kind edge caps, because one cap for every map is one cap too few. CALIBRATED on
# `west_coast_usa`'s own DDS headers — BeamNG's biggest shipped level, 0.58 GiB of textures
# against A3's 5.72 GiB:
#
#   colour  185 of 256 files are <= 512
#   data    391 of 470 files are <= 256   (roughness/metallic carry almost no high frequency)
#   normal  512-1024
#
# `opacity` is the albedo's OWN alpha channel re-exported as a grey map, so it takes the colour
# cap: it is a coverage mask cut at the albedo's scale, and dropping it to the data cap is what
# turns a tree's leaf cut-outs blocky. `emissive` is a colour map. The keys are settings names;
# `caps_from_settings` resolves them.
# EVERY exported kind must appear here. A kind that is missing gets `caps.get(kind, 0)` = 0 =
# UNCAPPED, silently, and `colormask` was missing from A6.3 (2026-08-19) until A8: 767 of the
# level's 771 masks shipped at 1024 x 1024 while every colour and normal map beside them was
# capped at 256, which was 1,025 MiB — 58 % of ALL the level's texture bytes — for a map whose
# only job is to select per texel against a 256-pixel albedo.
KIND_CAP_SETTING = {
    "color": "texture_max",
    "normal": "texture_max_normal",
    "data": "texture_max_data",
    "opacity": "texture_max",
    "colormask": "texture_max",
}


def caps_from_settings(settings):
    """`{kind: edge cap}` from the settings, falling back to the colour cap for anything the
    settings do not name (an old settings file that only has `texture_max`)."""
    colour = int(settings.get("texture_max") or 0)
    out = {}
    for kind, key in KIND_CAP_SETTING.items():
        value = settings.get(key, colour)
        out[kind] = int(colour if value is None else value)
    return out


# -- materials -----------------------------------------------------------------------------------

# Physical emissive intensity, in nits, for a material that carries an emissive map and whose
# mesh has NO colour slot to say otherwise (314 of the reference city's 966). Vanilla's own value
# for a lit building window is 200 (`west_coast_usa`'s `highrise`); measured over all 22 vanilla
# materials that state nits the band is 200 .. 10,000 with a median of 5,000. OURS only in the
# sense that these particular materials have nothing in the bundle to port.
EMISSIVE_NITS = 200.0


def material_v15(level, name, rec, maps, pid_key=None):
    """One v1.5 material object. `name` is the final (mapTo) name; `rec` the bake's resolved
    record; `maps` the exported game paths: {baseColor, normal, roughness, metallic,
    emissive, colorMask} (any may be None). There is deliberately no occlusion map: the §7.1
    mask's G channel is not AO (audit §54)."""
    stage = {}
    if maps.get("baseColor"):
        stage["baseColorMap"] = maps["baseColor"]
    if maps.get("normal"):
        stage["normalMap"] = maps["normal"]
    if maps.get("roughness"):
        stage["roughnessMap"] = maps["roughness"]
    elif rec.get("roughness") is not None:
        stage["roughnessFactor"] = float(rec["roughness"])
    if maps.get("metallic"):
        stage["metallicMap"] = maps["metallic"]
    elif rec.get("metallic") is not None:
        stage["metallicFactor"] = float(rec["metallic"])
    if maps.get("emissive"):
        stage["emissiveMap"] = maps["emissive"]
        # `is None`, NOT falsiness. `csiibundle.materials.resolve` used to substitute a
        # `(0, 0, 0)` tuple for the absent field, which is truthy, so this fallback never fired
        # and `defaultMat.hlsl:369`'s `inLayer.emissive *= emissiveMapSample` multiplied every
        # one of the 2,373 emissive maps in the level by zero. The nits fix below was multiplied
        # by that zero and could not have worked.
        factor = rec.get("emissiveFactor")
        if factor is None:
            factor = [1.0, 1.0, 1.0]
        stage["emissiveFactor"] = [round(float(v), 6) for v in factor[:3]]
        # WITHOUT THIS THE MAP DOES NOTHING USEFUL AT NIGHT. v1.5 wants a physical intensity;
        # a layer that has none takes `MATERIAL_LAYER_FLAG2_REMOVETHIS_emissiveNoNits` and is
        # scaled by the global `emissiveFallbackIntensity` against the last frame's exposure
        # (`defaultMat.hlsl`, `permutationsV0.h.hlsl`) — an exposure-relative legacy path, not a
        # light. We shipped 2,373 emissive materials with no nits at all, which is why the city
        # never lit up. 200 nits is VANILLA'S OWN value for a lit building window
        # (`west_coast_usa`'s `highrise`); its brightest sign uses 4,000. Where the bake joined a
        # colour slot to this material the value is CS2's own (`emissiveNits`); the flat fallback
        # is for the materials no slot reaches.
        # `is None`, and for the same reason as the factor above: **a joined slot may legitimately
        # say ZERO.** 115 of the reference city's slots do - every one of them the UNLIT face of a
        # billboard (`NeonSign`, slot 1), which CS2 authors to emit nothing. Falling back on
        # falsiness would light all of them at 200 nits.
        nits = rec.get("emissiveNits")
        stage["emissiveIntensityNits"] = EMISSIVE_NITS if nits is None else round(float(nits), 1)
    if maps.get("colorMask") and rec.get("tintable"):
        stage["colorPaletteMap"] = maps["colorMask"]
        # Without this the shader takes `baseColor *= 1` and the palette is applied to NOTHING
        # (`getColorPalette`: `baseColor *= (flags & MATERIAL_LAYER_FLAG_paletteBaseColor) ?
        # colorPalette.rgb : 1`). Set explicitly rather than relying on whatever the material
        # compiler defaults it to when a `colorPaletteMap` is present.
        stage["paletteBaseColor"] = True
    if maps.get("opacity"):
        stage["opacityMap"] = maps[
            "opacity"
        ]  # v1.5 reads opacity from here, not from the albedo's alpha
    factor = rec.get("baseColorFactor")
    if factor:
        rgb = [float(v) for v in factor[:3]]
        alpha = float(factor[3]) if len(factor) > 3 else 1.0
        if any(abs(v - 1.0) > 1e-3 for v in rgb):
            stage["baseColorFactor"] = [*rgb, 1.0]
        if alpha < 0.999:
            stage["opacityFactor"] = alpha
    if not stage.get("baseColorMap") and rec.get("color"):
        stage["diffuseColor"] = [float(v) for v in list(rec["color"])[:4]] + [1.0] * (
            4 - len(rec["color"][:4])
        )
    if rec.get("uv_scale") and rec.get("uv_scale") != 1.0:
        pass  # UV scale is baked into tile UVs by the areas product
    mat = {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "persistentId": level.pid(pid_key or f"material:{name}"),
        "version": 1.5,
        "Stages": [stage, {}, {}, {}],
        "materialTag0": "beamng",
        "materialTag1": "cs2",
    }
    mode = rec.get("alphaMode") or "opaque"
    if mode == "mask":
        mat["alphaTest"] = True
        mat["alphaRef"] = int(round(255 * float(rec.get("alphaCutoff") or 0.5)))
    elif mode == "blend":
        mat["translucent"] = True
        mat["translucentBlendOp"] = "LerpAlpha"
        mat["translucentZWrite"] = True
    if rec.get("doubleSided"):
        mat["doubleSided"] = True
    if rec.get("castShadows") is False:
        mat["castShadows"] = False
    if rec.get("groundType"):
        mat["groundType"] = rec["groundType"]
    return mat


def placeholder_material(level, name, color=(0.5, 0.5, 0.5, 1.0)):
    """A flat material for a symbol nothing resolves (unresolved asset packs, `unmapped`)."""
    return {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "persistentId": level.pid(f"material:{name}"),
        "version": 1.5,
        "Stages": [{"diffuseColor": [float(c) for c in color]}, {}, {}, {}],
        "materialTag0": "beamng",
        "materialTag1": "cs2",
    }
