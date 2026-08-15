"""The §8 splatmap: which ground is painted where, and the two ways to read it wrong.

`terrain/splatmap.png` is the game's own per-pixel ground blend — the weight map its terrain
shader samples — shipped verbatim as RGBA8 with a geo-reference in the manifest's
`rasters.splatmap` block. Six grounds come out of four channels, and neither the channel
assignment nor the packing is guessable. Both have been wrong in this repo, both looked
plausible on screen, and both are recorded here so nobody re-derives them:

**R is dirt and G is grass, not the other way round** (audit SPLAT-3). The inverted map is what
"the terrain renders brown" was: it painted the whole landmass dirt-brown and the whole lakebed
grass-green. Two measurements fix the assignment, and slope is not one of them — rock is the
dirt channel's own residual, so BOTH soil weights fall with slope and the slope response never
told them apart:

* G is exactly 0 on every texel under a metre or more of water, and 0.5 on 45° cliffs, against
  201.7 on flat land. Identically zero on every lakebed and every cliff — that is grass.
* R averages 249.9 underwater and rises with depth. A weight that blankets the seabed is dirt.

**B and A are each two 4-bit weights, not one 0..255 opacity** (audit SPLAT-6). That is how the
four paintable extras fit an R8G8 user splatmap. Reading them as single channels merged extra1
with extra2 and extra3 with extra4 and drew the result with the wrong ground's texture, which
is where the player's painted beaches went. Decode with `& 15` / `>> 4` and normalise by **15**:

    B.low = extra1 (paint tool 01 "Dry")     B.high = extra2 (tool 02 "Lush")
    A.low = extra3 (tool 03 "Dirt")          A.high = extra4 (tool 04 "Sand")

**The blend is a composite over a base, not a partition.** R and G are both near-saturated on
flat land (251.6 and 201.7) and routinely sum past 255; normalising them turns every meadow
half brown. Each channel is an opacity at `saturate(weight · splatMultiplier) ^ splatPower`,
composited in `COMPOSITE_ORDER` over the rock base. Rock needs no channel of its own — it is
the residual of R, so where the dirt weight falls away, which is what a cliff face is, the base
shows through. The order is what makes a shoreline read correctly: dirt blankets everything
including the lakebed, grass covers the land on top of it, then the player's paint goes over
both, so the sandy band appears exactly where grass falls to zero at the water.

A packed byte also cannot be interpolated — halfway between 0x0F and 0x10 decodes to a nibble
pair unrelated to either neighbour — so a consumer must unpack BEFORE filtering, not sample the
packed texture and unpack per-pixel. Doing it the other way forces point sampling, and every
painted region then shows the splatmap's own 3.5 m texels as literal square steps.
"""

import numpy

from . import png

# Which ground each channel weights, for a bundle ripped before the producer published the map
# itself in `rasters.splatmap.channels`. Read the published map when there is one; this is the
# fallback, not the authority. CONTRACT.
SPLAT_CHANNELS_DEFAULT = {
    "R": "dirt",
    "G": "grass",
    "B": "extra1:extra2",
    "A": "extra3:extra4",
    "base": "rock",
}

# The order the grounds composite in, base first. CONTRACT — see the module docstring; getting
# this order wrong is what puts grass under the shoreline instead of over it.
COMPOSITE_ORDER = ("R", "G", "extra1", "extra2", "extra3", "extra4")

# The four paint tools, in packing order, named by the game's own locale strings.
EXTRA_NAMES = ("extra1", "extra2", "extra3", "extra4")

# Which packed channel each extra rides in, so `Splat.layers` can honour a producer that
# declared that channel unnameable. CONTRACT (the packing order above).
EXTRA_KEY_OF = {"extra1": "B", "extra2": "B", "extra3": "A", "extra4": "A"}

# Stand-in albedo for a ground whose texture the bundle does not carry, so a missing file
# degrades to a plausible colour rather than to nothing. TUNED.
#
# These are LINEAR and sit at the same level as CS2's authored ground albedo, which is dark:
# measured means of the real grounds are 0.04-0.14 linear. Taken from the real textures —
# Grass_Dry / Forest_temperate / Light_dirt / Sand, all _temperate_01.
GROUND_FALLBACK_COLOUR = {
    "grass": (0.20, 0.30, 0.12, 1.0),
    "dirt": (0.35, 0.28, 0.20, 1.0),
    "rock": (0.30, 0.28, 0.26, 1.0),
    "extra1": (0.076, 0.077, 0.050, 1.0),   # 01 Dry   - Grass_Dry_Temperate_01
    "extra2": (0.039, 0.046, 0.019, 1.0),   # 02 Lush  - Forest_temperate_01
    "extra3": (0.118, 0.096, 0.080, 1.0),   # 03 Dirt  - Dirt_temperate_01
    "extra4": (0.155, 0.128, 0.090, 1.0),   # 04 Sand  - Sand_Surface_01
}


def channel_mapping(block):
    """`(mapping, published)` — which ground each channel weights, and whether the bundle said.

    The producer publishes the map in `rasters.splatmap.channels`; `SPLAT_CHANNELS_DEFAULT` is
    the fallback for a bundle ripped before it did. A channel the producer could not name ships
    as `"unknown"` and comes back as `None` — the theme binds no such ground, so that channel is
    DROPPED from the blend rather than guessed at. Guessing paints a real weight with a wrong
    texture, which is strictly worse than leaving the ground below it showing.
    """
    published = (block or {}).get("channels")
    if not isinstance(published, dict):
        return dict(SPLAT_CHANNELS_DEFAULT), False

    mapping = {}
    for key, default in SPLAT_CHANNELS_DEFAULT.items():
        name = published.get(key, default)
        mapping[key] = None if (not isinstance(name, str) or name == "unknown") else name
    return mapping, True


class Splat:
    """The ripped splat weight map with its geo-reference.

    `path` is `terrain/splatmap.png`; `block` is the manifest's `rasters.splatmap` block, which
    carries the world placement (`originWorld`, `resolution`, `size`) and the two blend
    parameters (`splatMultiplier`, `splatPower`) the game's own shader applies.

    The raw RGBA bytes stay on the instance as `pixels`. Row 0 is NORTH (§3), matching the PNG
    and the rest of the raster layers; anything with a south-first V axis flips at the boundary,
    not here.
    """

    def __init__(self, path, block):
        self.path = path
        self.multiplier = float(block.get("splatMultiplier", 1.0))
        self.power = float(block.get("splatPower", 1.0))
        self.width, self.height, self.pixels = png.decode_png_rgba(path)

        size = block.get("size", [self.width, self.height])
        if [self.width, self.height] != list(size):
            raise ValueError(f"splatmap is {self.width}x{self.height}, block says {size}")

        cx, cz = block["resolution"]
        ox, _, oz = block["originWorld"]           # top-left texel CENTRE, row 0 north
        self.cell_x = cx
        self.cell_z = cz
        self.min_x = ox - cx * 0.5                 # the texel's edge, not its centre
        self.max_z = oz + cz * 0.5
        self.area_x = cx * self.width
        self.area_z = cz * self.height

    # -- geo-reference ------------------------------------------------------------------------

    @property
    def max_x(self):
        return self.min_x + self.area_x

    @property
    def min_z(self):
        return self.max_z - self.area_z

    def uv(self, x, z):
        """Bundle-frame `(x, z)` metres to `(u, v)` in 0..1, V running SOUTH to north.

        The V convention is the one nearly every renderer's texture space uses, and it is the
        opposite of the PNG's row order — which is why `weights()` flips. Accepts arrays.
        """
        return ((numpy.asarray(x) - self.min_x) / self.area_x,
                (numpy.asarray(z) - self.min_z) / self.area_z)

    # -- weights ------------------------------------------------------------------------------

    def rgba(self):
        """The raw map as `(height, width, 4)` uint8, row 0 north. Packed — not weights yet."""
        return numpy.frombuffer(self.pixels, dtype=numpy.uint8).reshape(self.height,
                                                                       self.width, 4)

    def soil_weights(self):
        """R and G as `(height, width, 2)` float32 in 0..1 — dirt and grass, in that order.

        These two are ordinary 8-bit weights; only B and A are packed.
        """
        return self.rgba()[:, :, :2].astype(numpy.float32) * (1.0 / 255.0)

    def extras(self, flip_v=False):
        """The four packed paint weights as `(height, width, 4)` float32 in 0..1.

        `extra1..4` land in R, G, B, A of the result — four plain continuous weights a renderer
        can filter normally, which is the whole reason to unpack here rather than in a shader
        (see the module docstring on why a packed byte cannot be interpolated).

        `flip_v=True` reverses the rows for a renderer whose image space runs south-to-north.

        Exact, not a quantisation: a nibble is 0..15 and `n / 15 == (n · 17) / 255` for every
        one of the sixteen values, so a caller that hands the result to an 8-bit texture
        round-trips losslessly.
        """
        pixels = self.rgba()
        packed_b, packed_a = pixels[:, :, 2], pixels[:, :, 3]
        weights = numpy.stack((packed_b & 15, packed_b >> 4,
                               packed_a & 15, packed_a >> 4), axis=-1)
        if flip_v:
            weights = weights[::-1]
        return weights.astype(numpy.float32) * (17.0 / 255.0)

    def opacity(self, weight):
        """One weight in 0..1 to the opacity the game composites with.

        `saturate(weight · splatMultiplier) ^ splatPower`, with the multiplier and power the
        producer read off the running terrain material. Scalar or array. CONTRACT — this is the
        game's own expression, not a curve chosen to look right.
        """
        out = numpy.minimum(numpy.asarray(weight, dtype=numpy.float32) * self.multiplier, 1.0)
        return out if self.power == 1.0 else numpy.power(out, self.power)

    def layers(self, mapping=None, flip_v=False):
        """Yield `(ground, opacity)` for each layer in `COMPOSITE_ORDER`, base excluded.

        `opacity` is a `(height, width)` float32 array ready to composite OVER whatever is
        already there. Unnamed channels (`mapping[key] is None`) are skipped, as `channel_mapping`
        documents.

        This is a generator so only one full-resolution layer is resident at a time — a 4096²
        map is 64 MB per float32 layer, and materialising all six at once is 400 MB for no
        reason. The CPU reference for the blend is then:

            colour = albedo[mapping["base"]]
            for ground, opacity in splat.layers(mapping):
                colour = colour * (1 - opacity)[..., None] + albedo[ground] * opacity[..., None]
        """
        if mapping is None:
            mapping = SPLAT_CHANNELS_DEFAULT
        soil = self.soil_weights()
        if flip_v:
            soil = soil[::-1]
        extras = None
        for key in COMPOSITE_ORDER:
            if key in ("R", "G"):
                ground = mapping.get(key)
                weight = soil[:, :, 0 if key == "R" else 1]
            else:
                ground = key if mapping.get(EXTRA_KEY_OF[key]) else None
                if extras is None:
                    extras = self.extras(flip_v=flip_v)
                weight = extras[:, :, EXTRA_NAMES.index(key)]
            if not ground:
                continue
            yield ground, self.opacity(weight)


# -- one ground per texel, for an engine whose terrain holds a single material per sample --------

def dominant_layer(splat, mapping=None, surface_index=None, roadbed_name="roadbed",
                   roadbed_id=1, flip_v=False):
    """`(index_raster uint8 (h, w), names)` — the ground that shows most at each texel.

    A BeamNG-class terrain stores ONE material index per heightmap sample and blends only at
    the boundary between different indices, so the six-way composite has to collapse to a
    winner. The winner is chosen by the composite's own arithmetic, not by raw weight: with
    layers `L1..Ln` (in `COMPOSITE_ORDER`) of opacity `o_i`, the visible share of layer `i` is
    `o_i · Π_{j>i}(1 − o_j)` and the base's share is `Π(1 − o_j)`; argmax of those shares is
    the ground a viewer sees most of at that texel. That is why grass beats dirt on a meadow
    (dirt is under it, opaque or not) and why the base rock wins only where every weight is low
    — a cliff.

    `names[0]` is the base (`mapping["base"]`); the rest are the layer grounds in composite
    order; a channel `channel_mapping` dropped is absent. When `surface_index` (the §8
    `surface_index.png` array, same shape) is given, texels with id `roadbed_id` get an extra
    final index `roadbed_name` — the flattened apron under a road, which the game paints as
    its own ground and which a terrain engine wants as its own material so the seam under a
    road mesh reads as pavement, not meadow.
    """
    if mapping is None:
        mapping = SPLAT_CHANNELS_DEFAULT
    layers = list(splat.layers(mapping, flip_v=flip_v))
    base_name = mapping.get("base") or "rock"
    names = [base_name] + [ground for ground, _ in layers]
    height, width = splat.height, splat.width
    # visible share, computed back to front: remaining = Π(1 - o_j) for j > i
    shares = numpy.zeros((len(names), height, width), dtype=numpy.float32)
    remaining = numpy.ones((height, width), dtype=numpy.float32)
    for i in range(len(layers) - 1, -1, -1):
        opacity = layers[i][1]
        shares[i + 1] = opacity * remaining
        remaining = remaining * (1.0 - opacity)
    shares[0] = remaining
    index = shares.argmax(axis=0).astype(numpy.uint8)
    if surface_index is not None:
        surface = numpy.asarray(surface_index)
        if surface.shape != index.shape:
            raise ValueError(f"surface_index {surface.shape} does not match the splatmap {index.shape}")
        names.append(roadbed_name)
        index = numpy.where(surface == roadbed_id, len(names) - 1, index).astype(numpy.uint8)
    return index, names
