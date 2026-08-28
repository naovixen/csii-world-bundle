"""The game's own ground blend as one Blender material, driven by terrain/splatmap.png.

Everything about WHICH channel is which ground, and how the four extras are packed into two
bytes, lives in `csiibundle.splat` — read its module docstring before touching any of it. What
is here is the node graph: one image datablock per ground, sampled at the game's own three
tiling rates and faded between them by camera distance, composited over the rock base in
channel order at the game's own multiplier and power.
"""

import os

import bpy
from csiibundle import materials
from csiibundle import splat as splat_reader

from . import shadernodes
from .log import LOG

# Which ground each splatmap channel weights, and the stand-in albedo for a ground whose
# texture the bundle does not carry. Both live in `csiibundle.splat`, whose docstring records
# the two measurements that fix R=dirt / G=grass and the nibble packing of B and A — the
# inverted map is what "the terrain renders brown" was, and it looked entirely plausible.
CHANNELS_DEFAULT = splat_reader.SPLAT_CHANNELS_DEFAULT
FALLBACK_COLOUR = splat_reader.GROUND_FALLBACK_COLOUR
FALLBACK_TILING = splat_reader.GROUND_FALLBACK_TILING


class Splat(splat_reader.Splat):
    """`csiibundle.splat.Splat` plus the one Blender-side step: turning the unpacked paint
    weights into an image datablock the shader can sample.

    Everything about WHICH channel is which ground, and how the four extras are packed into
    two bytes, lives in the package — read its module docstring before touching any of it.
    """

    def extras_image(self):
        """The four packed paint weights as one ordinary RGBA image, `extra1..4` in R,G,B,A.

        Unpacking in numpy rather than in shader nodes is what lets this tap be filtered
        LINEARLY: a packed byte cannot be interpolated (halfway between 0x0F and 0x10 decodes
        to a nibble pair unrelated to either neighbour), so the old point-sampled + FLOOR chain
        gave every painted region — the player's beaches included — literal 3.5 m square steps.
        The decode is bit-exact; nothing derived is written into the bundle.
        """
        # Blender's image rows run south-to-north; the PNG's run north-to-south (§3, row 0
        # north), and SplatUV already puts V = 0 at the south edge — so the flip belongs here.
        flat = self.extras(flip_v=True).reshape(-1)

        image = bpy.data.images.new(
            "terrain_splat_extras", self.width, self.height, alpha=True, float_buffer=False
        )
        # ORDER IS LOAD-BEARING, and getting it wrong fails silently — the image reads back as
        # the blank default (RGB 0, alpha 1), which paints the whole map with extra4 and
        # nothing else. Both settings must precede the write, because changing either one
        # afterwards re-generates the buffer and discards it, and `update()` must follow it.
        image.colorspace_settings.name = "Non-Color"
        # extra4 rides the alpha channel as DATA. Without this Blender multiplies the other
        # three weights by it on every read — the premultiply trap that once made the whole
        # terrain render at a tenth of its albedo.
        image.alpha_mode = "CHANNEL_PACKED"
        image.pixels.foreach_set(flat)
        image.update()
        image.pack()  # a generated image has no file to reload from
        return image


class _Blend:
    """The node graph under construction: the shared tree, UV taps and per-ground samplers.

    A class rather than a nest of closures because every one of these steps reads the same
    four things (the tree, the world UV, the camera and the tiling block) and two of them
    memoise into the same cache.
    """

    def __init__(self, tree, surfaces_dir, tiling_block):
        self.tree = tree
        self.surfaces_dir = surfaces_dir
        self.tiling_block = tiling_block
        self.world_uv = tree.nodes.new("ShaderNodeUVMap")
        self.world_uv.uv_map = "WorldUV"  # world metres; the scale is per ground
        self.camera = tree.nodes.new("ShaderNodeCameraData")
        self._blend_cache = {}

    def lod_factor(self, start, end):
        """0 below `start`, 1 above `end`, ramped between — the game's tiling LOD blend,
        keyed on view distance. Cached: grass, dirt and the extras all share one pair."""
        key = (start, end)
        if key not in self._blend_cache:
            node = self.tree.nodes.new("ShaderNodeMapRange")
            node.clamp = True
            self.tree.links.new(self.camera.outputs["View Distance"], node.inputs["Value"])
            node.inputs["From Min"].default_value = start
            node.inputs["From Max"].default_value = max(end, start + 1e-3)
            node.inputs["To Min"].default_value = 0.0
            node.inputs["To Max"].default_value = 1.0
            self._blend_cache[key] = node.outputs["Result"]
        return self._blend_cache[key]

    def tap(self, image, metres_per_repeat):
        """One sample of `image`, world-anchored, repeating every `metres_per_repeat`."""
        node = self.tree.nodes.new("ShaderNodeMapping")
        node.vector_type = "POINT"
        node.inputs["Scale"].default_value = (1.0 / metres_per_repeat, 1.0 / metres_per_repeat, 1.0)
        self.tree.links.new(self.world_uv.outputs["UV"], node.inputs["Vector"])
        texture = self.tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        self.tree.links.new(node.outputs["Vector"], texture.inputs["Vector"])
        return texture.outputs["Color"]

    def math(self, op, a, b):
        """A Math node, each input linked when it is a socket and set when it is a number."""
        node = self.tree.nodes.new("ShaderNodeMath")
        node.operation = op
        for socket, value in zip((node.inputs[0], node.inputs[1]), (a, b)):
            if hasattr(value, "is_linked"):
                self.tree.links.new(value, socket)
            else:
                socket.default_value = value
        return node.outputs[0]

    def mix(self, colour_a, colour_b, fac):
        """`colour_a` toward `colour_b` by `fac` — the one blend the whole composite uses."""
        node = self.tree.nodes.new("ShaderNodeMixRGB")
        node.blend_type = "MIX"
        self.tree.links.new(fac, node.inputs["Fac"])
        self.tree.links.new(colour_a, node.inputs["Color1"])
        self.tree.links.new(colour_b, node.inputs["Color2"])
        return node.outputs["Color"]

    def sampled(self, image, ground):
        """One ground image at the game's own three tiling rates, blended by camera distance."""
        shipped = splat_reader.ground_tiling(self.tiling_block, ground)
        if shipped is None:
            return self.tap(image, FALLBACK_TILING)
        (near, mid, far), (mid_start, mid_end, far_start, far_end) = shipped
        # near -> mid -> far, each fading in over its own camera-distance band. One image
        # datablock, three samples: the whole point is that the same texture repeats at three
        # different rates so no single rate's grid can dominate.
        blended_near_mid = self.mix(
            self.tap(image, near), self.tap(image, mid), self.lod_factor(mid_start, mid_end)
        )
        return self.mix(blended_near_mid, self.tap(image, far), self.lod_factor(far_start, far_end))

    def ground_texture(self, ground):
        """This ground's albedo, or a flat stand-in colour when the bundle ships no texture."""
        path = os.path.join(self.surfaces_dir, f"{ground}.png")
        if not os.path.isfile(path):
            rgb = self.tree.nodes.new("ShaderNodeRGB")
            rgb.outputs[0].default_value = FALLBACK_COLOUR.get(ground, (0.3, 0.3, 0.3, 1.0))
            return rgb.outputs[0]
        return self.sampled(shadernodes.unpremultiply(bpy.data.images.load(path)), ground)

    def ground_normal(self, ground):
        """The ground's own normal map, at the same three tiling rates, or None.

        Every ground layer ships one — `<ground>_normal.png`, seven of them on the reference
        bundle — and nothing read them, so the terrain lit perfectly flat: no grain in the sand,
        no aggregate in the gravel, no relief on a rock face. The theme's own maps are the only
        source of that, since §8 deliberately does not reproduce the game's per-pixel ground
        classification.

        Sampled at the SAME tiling as the albedo it belongs to, or the bumps would swim against
        the colour at every LOD band.
        """
        path = os.path.join(self.surfaces_dir, f"{ground}_normal.png")
        if not os.path.isfile(path):
            return None
        image = shadernodes.unpremultiply(bpy.data.images.load(path))
        image.colorspace_settings.name = "Non-Color"  # a normal is data, not colour

        # Measured on the reference bundle, all seven grounds ship tangent RGB — unlike the
        # area surfaces, which ship two-channel packings and shaded black for it. Checked
        # rather than assumed, because the failure is silent and the blend below mixes three
        # taps of the image with its alpha already dropped, so unpacking here would mean
        # threading alpha through `tap`. If this ever fires, that is the work it needs.
        packing = materials.detect_normal_packing(shadernodes.sample_rgba(image))
        if packing != materials.NORMAL_RGB:
            LOG.warn(
                f"ground normal {ground}_normal.png is {packing}-packed, not the tangent "
                "RGB of contract §7.1 — its relief will be wrong; re-rip to ship it "
                "unpacked (see materials.detect_normal_packing)"
            )
        return self.sampled(image, ground)


def _weight_sockets(blend, splat):
    """The eight per-texel weights: R/G/B/A off the splatmap, extra1..4 off the unpacked image.

    The four paint weights are already unpacked out of B and A into an ordinary RGBA image
    (`Splat.extras_image`). Because they are plain weights now, that tap is Linear like the
    soil one: the player's painted regions blend across the splatmap's 3.5 m texels instead
    of stepping at every texel edge, which is what the old point-sampled + FLOOR decode
    forced. Nothing about the game's own values changes — the decode is bit-exact.
    """
    tree = blend.tree
    splat_uv = tree.nodes.new("ShaderNodeUVMap")
    splat_uv.uv_map = "SplatUV"
    splat_node = tree.nodes.new("ShaderNodeTexImage")
    splat_node.image = shadernodes.unpremultiply(bpy.data.images.load(splat.path))
    splat_node.image.colorspace_settings.name = "Non-Color"
    splat_node.interpolation = "Linear"
    splat_node.extension = "EXTEND"
    tree.links.new(splat_uv.outputs["UV"], splat_node.inputs["Vector"])

    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(splat_node.outputs["Color"], separate.inputs["Color"])
    channels = {
        0: separate.outputs["Red"],
        1: separate.outputs["Green"],
        2: separate.outputs["Blue"],
        3: splat_node.outputs["Alpha"],
    }

    extras_node = tree.nodes.new("ShaderNodeTexImage")
    extras_node.image = splat.extras_image()
    extras_node.image.colorspace_settings.name = "Non-Color"
    extras_node.interpolation = "Linear"
    extras_node.extension = "EXTEND"
    tree.links.new(splat_uv.outputs["UV"], extras_node.inputs["Vector"])
    separate_extras = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(extras_node.outputs["Color"], separate_extras.inputs["Color"])
    extra_weight = {
        "extra1": separate_extras.outputs["Red"],
        "extra2": separate_extras.outputs["Green"],
        "extra3": separate_extras.outputs["Blue"],
        "extra4": extras_node.outputs["Alpha"],
    }
    return channels, extra_weight


# All four channels are opacities, composited over the rock base in channel order at
# saturate(weight * splatMultiplier)^splatPower — the game's own multiplier and power,
# shipped in the rasters block. This is NOT a partition: R and G are both near-saturated
# on flat land (251.6 and 201.7) and routinely sum past 255, so normalising them turns
# every meadow half brown. Rock is the residual of R alone and needs no channel: where the
# dirt weight falls away, which is what a cliff face is, the base shows through.
#
# The order is what makes it read correctly: dirt (R) blankets everything including the
# lakebed, then grass (G) covers the land on top of it, then the player's paint. So the
# shoreline — dirt with grass falling to zero at the water — is the sandy band, and the
# land above it is green.
#
# A channel the producer could not name is skipped — that ground is present but
# unnameable, and painting it as a guess would be worse than leaving the ground below.
#
# B AND A ARE BIT-PACKED, two 4-bit weights each (audit SPLAT-6). That is how four paintable
# extras fit an R8G8 user splatmap, and it is why reading them as single 0-255 opacities lost
# the player's sand: it merged extra1 with extra2, extra3 with extra4, and rendered the
# merged result with the wrong ground's texture entirely.
#     B.low = extra1 (tool 01 "Dry")     B.high = extra2 (tool 02 "Lush")
#     A.low = extra3 (tool 03 "Dirt")    A.high = extra4 (tool 04 "Sand")
# The unpacking happens in numpy at import (`Splat.extras_image`), not in shader nodes, so
# `extra_weight` already holds four continuous 0..1 weights.
def _composite_order(blend, splat, mapping, channels, extra_weight):
    """`[(ground, opacity socket)]` in the order the grounds paint over the base.

    The two continuous soil weights first — dirt blankets everything including the lakebed,
    then grass covers the land on top of it — and then the player's paint in extra order.
    Built as a list so the NORMALS can be composited in exactly the same order with exactly
    the same opacities.
    """

    def opacity_of(weight):
        out = blend.math("MINIMUM", blend.math("MULTIPLY", weight, splat.multiplier), 1.0)
        return out if splat.power == 1.0 else blend.math("POWER", out, splat.power)

    composite = []
    for index, key in ((0, "R"), (1, "G")):
        ground = mapping.get(key)
        if ground:
            composite.append((ground, opacity_of(channels[index])))
    for ground in ("extra1", "extra2", "extra3", "extra4"):
        composite.append((ground, opacity_of(extra_weight[ground])))
    return composite


def _wire_normals(blend, bsdf, base_ground, composite):
    """The normals composite in the SAME order with the SAME opacities as the albedo, so a
    texel's bumps always come from the ground whose colour it is showing. Blending tangent
    normals as vectors is an approximation — the correct operation is a reorientation — but at
    these opacities it is far closer than the flat surface it replaces, and it costs one mix
    per ground rather than a node graph per pair."""
    normal_layers = [(ground, blend.ground_normal(ground)) for ground, _op in composite]
    if not any(socket for _g, socket in normal_layers):
        return
    blended_n = blend.ground_normal(base_ground)
    if blended_n is None:
        flat = blend.tree.nodes.new("ShaderNodeRGB")
        flat.outputs[0].default_value = (0.5, 0.5, 1.0, 1.0)  # tangent-space "no bump"
        blended_n = flat.outputs[0]
    for (_ground, socket), (_g, opacity) in zip(normal_layers, composite):
        if socket is not None:
            blended_n = blend.mix(blended_n, socket, opacity)
    normal_map = blend.tree.nodes.new("ShaderNodeNormalMap")
    # CS2 authors OpenGL (+Y up), which is Blender's own convention — no green flip.
    blend.tree.links.new(blended_n, normal_map.inputs["Color"])
    blend.tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])


def ground_material(surfaces_dir, splat, mapping, tiling_block=None):
    """One ground material mixing the game's grounds by its own splat weights.

    The splatmap image is sampled with linear interpolation in its own UV layer (SplatUV),
    so the blend is per-pixel smooth — the game's look — instead of per-face. The ground
    textures keep the world-anchored 8 m tiling of the legacy path.

    `mapping` names the ground each of R, G, B, A weights, plus the `base` ground. The base
    is not a channel: it is the residual of the DIRT channel alone, which is why a cliff
    renders rock — measured, the dirt weight falls 251.6 → 9.4 from flat ground to a 45°+ face.

    All four channels composite over that base in channel order as opacities at
    saturate(weight · splatMultiplier)^splatPower. See `_composite_order` for why normalising
    them, or splitting them into two kinds, both render the map wrong (audit SPLAT-3).
    """
    material = bpy.data.materials.new("terrain_ground")
    tree = shadernodes.ensure_nodes(material)
    bsdf = tree.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 0.95
    shadernodes.diffuse_ground(bsdf)

    blend = _Blend(tree, surfaces_dir, tiling_block)
    channels, extra_weight = _weight_sockets(blend, splat)

    # The base ground's own default is "rock" — NOT the "R" channel's "dirt". `mapping["base"]`
    # is None exactly when the producer wrote `channels.base: "unknown"` (a theme that binds
    # no rock ground), which is precisely where the base shows through: every cliff face and
    # gorge wall, the places the dirt weight has fallen away. Falling back to "dirt" painted
    # them all with `dirt.png` while the log printed `base=none`.
    base_ground = mapping["base"] if mapping.get("base") else CHANNELS_DEFAULT["base"]
    composite = _composite_order(blend, splat, mapping, channels, extra_weight)

    blended = blend.ground_texture(base_ground)
    for ground, opacity in composite:
        blended = blend.mix(blended, blend.ground_texture(ground), opacity)

    # Straight to the BSDF: no gain, no grading. The ground ships albedo, and `--colour` grades
    # the whole scene in the display pipeline rather than pre-multiplying the terrain.
    tree.links.new(blended, bsdf.inputs["Base Color"])
    _wire_normals(blend, bsdf, base_ground, composite)
    return material
