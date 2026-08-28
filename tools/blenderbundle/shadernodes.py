"""The shader-node and image primitives every material builder in this adapter shares.

Two things live here rather than beside one caller. The image ones — above all
`unpremultiply` — are corrections that must be applied at EVERY load site or the scene is
silently wrong, and `unpremultiply`'s docstring records the largest fidelity bug this importer
has had. The node ones (`ensure_nodes`, `diffuse_ground`, `set_alpha_blend`,
`tangent_normal_colour`) each encode one Blender-version fact that is invisible when missed.
"""

import os

import bpy
import numpy
from csiibundle import materials


def unpremultiply(image):
    """Stop Blender multiplying this texture's COLOUR by its ALPHA.

    THE SINGLE LARGEST FIDELITY BUG THIS IMPORTER HAS HAD. An Image Texture node's `Color`
    output is alpha-premultiplied when the image is loaded with the default `STRAIGHT` alpha
    mode. CS2 packs data into the alpha channel of its base-colour maps — it is not opacity —
    and it is mostly near zero: measured on this bundle, all seven ground textures have a mean
    alpha between 0.036 and 0.133 and **not one texel** at full opacity. The terrain was
    therefore drawn at 4-13% of the albedo the bundle ships. Measured at the sand reference
    point: `#24211d` against a shipped albedo of `#777066`, an 11x deficit.

    That is what "the import is very dark and the colours do not come through" was. It also
    explains the constants this file used to carry: `_GROUND_BRIGHTNESS = 3.09` was compensating
    for a ~10x darkening with a 3x gain, which is why it never quite worked and why removing it
    made things look worse rather than better.

    SCOPE: only the textures THIS file loads — the terrain grounds, the splatmap, the area
    surfaces. Blender's glTF importer already sets `CHANNEL_PACKED` itself, so object, network
    and prop textures were never affected; checked, rather than assumed, before claiming it.

    `CHANNEL_PACKED` is the right mode rather than `NONE`: it tells Blender the alpha is data,
    so `Color` comes through unmodified, while the `Alpha` output stays available for the
    materials that genuinely do cut out (foliage, decals).
    """
    if image is not None:
        image.alpha_mode = "CHANNEL_PACKED"
    return image


_NORMAL_SAMPLE_STRIDE = 64  # every 64th texel: enough to see a channel is CONSTANT


def sample_rgba(image, stride=_NORMAL_SAMPLE_STRIDE):
    """A subsample of a loaded image as `(r, g, b, a)` tuples in 0-1, for `detect_normal_packing`.

    Set the colour space BEFORE calling: `pixels` hands back what the image holds, and the point
    of the sample is to read the stored numbers rather than a managed version of them.

    Strided rather than whole: the signatures being tested are channels that are constant across
    the entire map, so a few thousand texels settle it, and pulling 4 M floats per image out of
    Blender for a yes/no answer would cost more than the shading it fixes.
    """
    width, height = image.size
    if (width == 0) or (height == 0):
        return []
    buffer = numpy.empty(width * height * 4, dtype=numpy.float32)
    image.pixels.foreach_get(buffer)
    return [tuple(float(v) for v in texel) for texel in buffer.reshape(-1, 4)[::stride]]


def tangent_normal_colour(tree, texture, packing, anchor):
    """The colour a Normal Map node wants, rebuilt from a two-channel-packed normal map.

    Mirrors `csiibundle.materials.unpack_normal` in nodes: X comes from whichever channel the
    packing put it in, Y from green, and Z is RECONSTRUCTED as `sqrt(1 - x^2 - y^2)` because the
    stored blue means nothing in either packed layout. Returns the source's own `Color` output
    unchanged when the map already conforms to §7.1, so the node graph only grows where it must.
    """
    channel = materials.normal_x_channel(packing)
    if channel is None:
        return texture.outputs["Color"]

    def math(operation, first, second=None, third=None, offset=0):
        node = tree.nodes.new("ShaderNodeMath")
        node.operation = operation
        node.location = (anchor.x - 620 + offset, anchor.y + 520)
        for index, value in enumerate((first, second, third)):
            if value is None:
                continue
            if hasattr(value, "is_linked"):
                tree.links.new(value, node.inputs[index])
            else:
                node.inputs[index].default_value = value
        return node.outputs[0]

    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = (anchor.x - 800, anchor.y + 520)
    tree.links.new(texture.outputs["Color"], separate.inputs["Color"])

    stored_x = texture.outputs["Alpha"] if channel == "A" else separate.outputs["Red"]
    stored_y = separate.outputs["Green"]

    # -1..1, square, and the Pythagorean remainder — clamped at zero because a compressed texel
    # can land marginally outside the unit disc and a negative square root is a black texel.
    x = math("MULTIPLY_ADD", stored_x, 2.0, -1.0, offset=0)
    y = math("MULTIPLY_ADD", stored_y, 2.0, -1.0, offset=160)
    remainder = math(
        "SUBTRACT",
        1.0,
        math(
            "ADD",
            math("MULTIPLY", x, x, offset=320),
            math("MULTIPLY", y, y, offset=320),
            offset=480,
        ),
        offset=640,
    )
    z = math("SQRT", math("MAXIMUM", remainder, 0.0, offset=800), offset=960)

    combine = tree.nodes.new("ShaderNodeCombineColor")
    combine.location = (anchor.x - 400, anchor.y + 520)
    tree.links.new(stored_x, combine.inputs["Red"])
    tree.links.new(stored_y, combine.inputs["Green"])
    tree.links.new(math("MULTIPLY_ADD", z, 0.5, 0.5, offset=1120), combine.inputs["Blue"])
    return combine.outputs["Color"]


def unpremultiply_all():
    """Apply `unpremultiply` to every image in the file.

    A backstop rather than the fix: the load sites above are what actually mattered, and
    Blender's glTF importer already channel-packs its own. This catches anything that arrives
    by a path nobody thought about, and reports how many so a future change that reintroduces
    premultiplied colour shows up in the log instead of silently darkening the scene.
    """
    fixed = 0
    for image in bpy.data.images:
        if image.alpha_mode != "CHANNEL_PACKED":
            image.alpha_mode = "CHANNEL_PACKED"
            fixed += 1
    return fixed


def ensure_nodes(datablock):
    """Return a material's/world's node tree, enabling nodes only if they are not already there.

    `use_nodes` is deprecated in Blender 5.x (removal slated for 6.0) because materials and worlds
    are always node-based now — assigning it emits a DeprecationWarning on every material the
    importer builds, which on a full city is thousands of lines of noise. Reading `node_tree`
    first is both quieter and correct: on 5.x it is already there, and on older Blender the
    assignment still runs.
    """
    if datablock.node_tree is None:
        datablock.use_nodes = True
    return datablock.node_tree


def diffuse_ground(bsdf):
    """Zero the white dielectric specular on a terrain ground.

    Principled defaults to a dielectric reflection that is WHITE regardless of base colour. CS2's
    terrain is diffuse ground with nothing there to reflect, so zeroing it is a modelling
    correction rather than a colour adjustment, and that is the whole justification — the effect
    is modest.

    Re-measured 2026-08-11 at the sand reference point on the fixed pipeline: leaving the
    specular on costs saturation 0.154 -> 0.141 (8.4%) and *raises* value 0.450 -> 0.462. An
    earlier note here claimed 64% of the saturation and a large brightness loss; those numbers
    were taken while the ground was being drawn at a tenth of its albedo (audit §2.5 SPLAT-5) and
    through a probe that double-encoded its own readings, and neither survived the fixes.

    The socket was renamed across versions ("Specular" -> "Specular IOR Level"); try both.
    """
    for name in ("Specular IOR Level", "Specular"):
        socket = bsdf.inputs.get(name)
        if socket is not None:
            socket.default_value = 0.0
            return


def set_alpha_blend(material):
    """Make a material's Alpha input actually read as transparency.

    Blender 4.2 moved EEVEE transparency from `blend_method` to `surface_render_method`, and the
    new default is DITHERED — stochastic transparency, which at low sample counts reads as noise
    or as fully opaque, not as a translucent surface. `blend_method` still exists on 5.x as a
    legacy alias, so a `hasattr(material, "blend_method")` guard passes and silently does nothing
    useful: the water was setting Alpha 0.8 and rendering solid. Set the modern property when it
    is there, and fall back for pre-4.2 Blender.
    """
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"


def ground_material(name, texture_path, fallback_colour):
    material = bpy.data.materials.new(name)
    ensure_nodes(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    diffuse_ground(bsdf)
    if texture_path and os.path.isfile(texture_path):
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = unpremultiply(bpy.data.images.load(texture_path))
        material.node_tree.links.new(image_node.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = fallback_colour
    return material


def bundle_material_id(name):
    """Blender suffixes `.001` on every duplicate name; the bundle id is what is left."""
    stem, dot, suffix = name.rpartition(".")
    return stem if dot and suffix.isdigit() else name


def mix_socket(node, name, outputs=False):
    """Pick a Mix node socket by name among the *enabled* ones.

    A ShaderNodeMix carries float, vector and colour variants of `A`/`B`/`Result` under the
    same names, and name lookup returns the first — the float one. Only the sockets of the
    active data_type are enabled, so enabled-ness is the reliable selector.
    """
    pool = node.outputs if outputs else node.inputs
    return next(s for s in pool if s.name == name and s.enabled)
