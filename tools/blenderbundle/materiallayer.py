"""Apply the §7 material layer onto the materials the glTF importer built.

Three rules, each a documented consumer obligation and each the difference between a city and
a grey one: the §7 `mask` into metallic and roughness (the .glb hard-codes roughness 0.9 for
every material there is), the textureless water and glass CS2 draws with a shader and no map,
and the §7.2 per-instance paint that is why vegetation is not grey. `report_materials` is the
diagnosis that comes first, so a grey scene has a reason rather than a shrug.
"""

import json
import os

import bpy
from csiibundle import materials, reader, tints, water

from . import shadernodes
from .log import LOG


def report_materials(bundle_dir):
    """Say what the material layer offers, so a grey scene has a diagnosis rather than a shrug.

    Reads materials.json only to count and to check that its texture paths resolve. The meshes
    reference textures themselves (contract §6), so nothing here feeds the import.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        LOG.warn("materials: no materials.json — meshes will import untextured")
        return

    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)

    slots = ("baseColor", "normal", "mask", "emissive", "colorMask")
    referenced, dangling, mapless, masked = set(), [], 0, 0
    for mat_id, material in table.items():
        paths = [material[s] for s in slots if s in material]
        if not paths:
            mapless += 1
        if "colorMask" in material:
            masked += 1
        for rel in paths:
            referenced.add(rel)
            if not os.path.isfile(os.path.join(bundle_dir, rel)):
                dangling.append(f"{mat_id}.{rel}")

    LOG.step(
        f"materials: {len(table)} ({mapless} with no maps), {len(referenced)} distinct textures"
    )

    # colors.json is a palette; colorMask is what says where each channel of it lands. Counting
    # them here is how "did the colour layer actually come out?" gets answered by the harness
    # rather than by opening a texture folder and squinting.
    LOG.step(f"materials: {masked} with a colorMask (the map colors.json needs to be applicable)")
    if dangling:
        # A path in the table with no file behind it is a producer bug: §7 lets a map be absent,
        # but not present-and-broken.
        LOG.warn(
            f"CONTRACT VIOLATION: {len(dangling)} texture reference(s) do not resolve: {dangling[:5]}"
        )


_TINT_PROPS = ("cs2_color0", "cs2_color1", "cs2_color2")
_TINT_LABEL = "cs2_tint"


def set_paint(obj, attrs):
    """Store a record's resolved paint (contract §7.2) as custom properties on its object.

    The three colours land on the instancing empty (or the placed mesh object, for lane
    geometry), where the material's Attribute nodes read them per instance — one shared
    material, thousands of individually-painted copies, exactly the game's own split between
    prefab data and per-object state. `csiibundle.tints` resolves them and explains why
    ignoring this layer leaves every tree in the city grey.
    """
    for prop, channel in zip(_TINT_PROPS, tints.color_set(attrs)):
        obj[prop] = list(channel)


_SHADER_LABEL = "cs2_shader_only"


def apply_shader_only_materials(bundle_dir):
    """Give the textureless materials an appearance, instead of the white glTF leaves them.

    CS2 draws water, glass and some foliage with a **shader and no texture**. The bundle cannot
    export a compiled shader, so those materials ship with no maps at all — and a glTF loader
    meeting a material with no `baseColorTexture` falls back to `baseColorFactor`, which is
    `[1, 1, 1, 1]`. **That is why swimming pools render as flat white paint**, and every window
    pane with them: 206 materials on the reference city, 109 of them glass and 51 water.

    What each becomes:

    * **water** — the authored `rasters.water` appearance (`csiibundle.water`), the same block
      the ocean surface uses, because in the game it is the same shader.
    * **glass** — transmissive, at the water block's close roughness. CS2's glass shader is not
      readable, so this is the honest minimum: see-through rather than white.
    * **foliage / overlay / placeholder** — the material's own `tint` when it ships one,
      otherwise left alone; there is nothing in the bundle to do better with.

    Returns `(water_count, glass_count, tinted_count)`.
    """
    table = reader.load_materials(bundle_dir)
    if not table:
        return 0, 0, 0
    appearance = water.load(bundle_dir)

    counts = {"water": 0, "glass": 0, "tint": 0}
    for mat in bpy.data.materials:
        material_id = shadernodes.bundle_material_id(mat.name)
        entry = table.get(material_id)
        # The id goes in too: a textureless material whose template is the generic `Default`
        # is classified by CS2's own name suffix, which is the only signal left. Without it the
        # reference city leaves five fountain bowls and a hot tub rendering as white slabs.
        kind = materials.shader_only_kind(entry, material_id)
        if kind is None:
            continue
        bsdf = _untextured_bsdf(mat)
        if bsdf is None:
            continue

        # Mark the BSDF itself, so a `--keep-existing` re-run can tell this material is done
        # without needing a node type to hang a label on.
        bsdf.label = _SHADER_LABEL

        flat = materials.tint(entry)
        if kind == "water":
            _make_water(mat, bsdf, appearance)
            counts["water"] += 1
        elif kind == "glass":
            _make_glass(mat, bsdf, appearance, flat)
            counts["glass"] += 1
        elif flat:
            bsdf.inputs["Base Color"].default_value = flat
            counts["tint"] += 1

    return counts["water"], counts["glass"], counts["tint"]


def _untextured_bsdf(mat):
    """This material's Principled BSDF, or None when it is already done or has a texture."""
    tree = getattr(mat, "node_tree", None)
    if tree is None or any(node.label == _SHADER_LABEL for node in tree.nodes):
        return None  # already done
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None or bsdf.inputs["Base Color"].links:
        return None  # it has a texture after all; leave it
    return bsdf


def _make_water(mat, bsdf, appearance):
    """The authored `rasters.water` block, transmissive where this Blender supports it."""
    bsdf.inputs["Base Color"].default_value = appearance.base_color
    bsdf.inputs["Roughness"].default_value = appearance.roughness
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = water.WATER_IOR
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 1.0
    else:
        bsdf.inputs["Alpha"].default_value = appearance.surface_alpha()
        shadernodes.set_alpha_blend(mat)


def _make_glass(mat, bsdf, appearance, flat):
    """Architectural glass at the water block's close roughness — see-through, not white."""
    bsdf.inputs["Base Color"].default_value = flat or (0.92, 0.96, 1.0, 1.0)
    bsdf.inputs["Roughness"].default_value = appearance.roughness
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = 1.5  # architectural glass
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 1.0
    else:
        bsdf.inputs["Alpha"].default_value = 0.15
        shadernodes.set_alpha_blend(mat)


_PBR_LABEL = "cs2_pbr"


def apply_pbr_masks(bundle_dir):
    """Wire each material's §7 `mask` into metallic and roughness. Returns how many were rigged.

    **This is the difference between a city and a city made of plastic.** The producer writes no
    `metallicRoughnessTexture` into its `.glb` files — measured, 1,449 of 1,449 glTF materials
    carry none and every one hard-codes `roughnessFactor` 0.9 — so a stock glTF import renders
    every window, metal roof, wet carriageway and polished floor at one flat roughness. The real
    per-texel values are in `mask`, referenced only from `materials.json`, and 4,563 of the
    bundle's 4,784 materials have one.

    Only R (metallic) and A (smoothness, so roughness is `1 - A`) are wired.
    `csiibundle.materials.wire_plan` decides that, and its docstring says why G and B are left
    alone rather than fed in as ambient occlusion.

    The mask is loaded Non-Color — it is data, and an sRGB curve on a roughness is a silent
    error — and CHANNEL_PACKED, because its alpha is data too and Blender would otherwise
    multiply the other three channels by it.
    """
    table = reader.load_materials(bundle_dir)
    if not table:
        return 0

    rigged = 0
    for mat in bpy.data.materials:
        entry = table.get(shadernodes.bundle_material_id(mat.name))
        plan = materials.wire_plan(entry)
        if not plan:
            continue
        tree = getattr(mat, "node_tree", None)
        if tree is None or any(node.label == _PBR_LABEL for node in tree.nodes):
            continue  # already rigged
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is None:
            continue
        mask_path = materials.texture_path(bundle_dir, entry, "mask")
        if not mask_path or not os.path.isfile(mask_path):
            continue  # report_materials flags it

        _wire_mask(tree, principled, plan, mask_path)
        rigged += 1

    return rigged


def _wire_mask(tree, principled, plan, mask_path):
    """The mask texture, split into channels and linked to whatever `wire_plan` named."""
    image = shadernodes.unpremultiply(bpy.data.images.load(mask_path, check_existing=True))
    image.colorspace_settings.name = "Non-Color"

    anchor = principled.location
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image, tex.label = image, _PBR_LABEL
    tex.location = (anchor.x - 1000, anchor.y + 300)
    sep = tree.nodes.new("ShaderNodeSeparateColor")
    sep.label = _PBR_LABEL
    sep.location = (anchor.x - 700, anchor.y + 300)
    tree.links.new(tex.outputs["Color"], sep.inputs["Color"])

    for _slot, channel, target, invert in plan:
        socket = (
            tex.outputs["Alpha"]
            if channel == "A"
            else sep.outputs[{"R": "Red", "G": "Green", "B": "Blue"}[channel]]
        )
        if invert:
            node = tree.nodes.new("ShaderNodeMath")
            node.operation, node.label = "SUBTRACT", _PBR_LABEL
            node.inputs[0].default_value = 1.0
            node.location = (anchor.x - 450, anchor.y + 200)
            tree.links.new(socket, node.inputs[1])
            socket = node.outputs[0]
        name = {"metallic": "Metallic", "roughness": "Roughness"}[target]
        if name in principled.inputs:
            tree.links.new(socket, principled.inputs[name])


def apply_color_tints(bundle_dir):
    """Wire contract §7.2 into every imported material that carries a colorMask.

    For each such material: base colour becomes
    `lerp(base, base × colorN, colorMask.{R,G,B})`, chained over the three channels — the
    per-texel selection the contract documents, with the per-instance colours read from the
    `cs2_color*` properties `set_paint` stored. `colorMask.A` is snow coverage and a harness
    city has no weather, so it is ignored. Returns how many materials were rigged.
    """
    path = os.path.join(bundle_dir, "materials.json")
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)

    rigged = 0
    for mat in bpy.data.materials:
        entry = table.get(shadernodes.bundle_material_id(mat.name))
        if not entry or "colorMask" not in entry:
            continue
        tree = getattr(mat, "node_tree", None)
        if tree is None:
            continue
        if any(node.label == _TINT_LABEL for node in tree.nodes):
            continue  # already rigged (--keep-existing rerun)
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is None:
            continue
        base_in = principled.inputs.get("Base Color")
        if base_in is None or not base_in.links:
            continue  # no base texture to tint

        mask_path = os.path.join(bundle_dir, entry["colorMask"])
        if not os.path.isfile(mask_path):
            continue  # report_materials already flagged it
        _wire_tint(tree, principled, base_in, mask_path)
        rigged += 1

    return rigged


def _wire_tint(tree, principled, base_in, mask_path):
    """The colorMask split into three channels, each lerping the base toward one cs2_color."""
    image = shadernodes.unpremultiply(bpy.data.images.load(mask_path, check_existing=True))
    image.colorspace_settings.name = "Non-Color"

    anchor = principled.location
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image, tex.label = image, _TINT_LABEL
    tex.location = (anchor.x - 1000, anchor.y - 600)
    sep = tree.nodes.new("ShaderNodeSeparateColor")
    sep.label = _TINT_LABEL
    sep.location = (anchor.x - 700, anchor.y - 600)
    tree.links.new(tex.outputs["Color"], sep.inputs["Color"])

    upstream = base_in.links[0].from_socket
    for i, (prop, channel) in enumerate(zip(_TINT_PROPS, ("Red", "Green", "Blue"))):
        attr = tree.nodes.new("ShaderNodeAttribute")
        attr.attribute_type = "INSTANCER"  # instancer empty first, object itself second
        attr.attribute_name, attr.label = prop, _TINT_LABEL
        attr.location = (anchor.x - 700 + i * 60, anchor.y - 900)
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type, mix.blend_type, mix.label = "RGBA", "MULTIPLY", _TINT_LABEL
        mix.location = (anchor.x - 450 + i * 150, anchor.y - 500)
        tree.links.new(sep.outputs[channel], shadernodes.mix_socket(mix, "Factor"))
        tree.links.new(upstream, shadernodes.mix_socket(mix, "A"))
        tree.links.new(attr.outputs["Color"], shadernodes.mix_socket(mix, "B"))
        upstream = shadernodes.mix_socket(mix, "Result", outputs=True)

    tree.links.new(upstream, base_in)  # replaces the existing link
