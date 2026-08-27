"""Per-instance paint (§7.2) — why the trees are grey until you apply this.

CS2 authors every tintable surface DESATURATED and colours it per object at draw time. Foliage
above all: the atlas really does store a washed-out grey-green, and the game multiplies each
tree's own colours over it. That colour therefore CANNOT live inside a shared `.glb` — the whole
point is that thousands of instances share one mesh and one material and differ only in paint.

So a consumer that ignores this layer gets a city of correctly-shaped, correctly-placed,
uniformly grey trees and bushes, and there is nothing wrong with the meshes. This is the single
most common way a technically-correct import looks broken.

The rule, and it is short:

    base_colour  =  lerp(base, base × colorSet[i], colorMask[i])   for i in R, G, B

`colorMask` is a texture named by the material (§7); its R, G and B channels select WHERE each
of the instance's three colours applies, per texel. `colorSet` is three RGBA colours on the
instance record's `attrs`. White is the identity for the multiply, so an instance with no
`colorSet` renders the atlas untouched — which is what makes this safe to apply unconditionally.

**`colorMask.A` is snow coverage**, not a fourth colour. Feeding it into the chain as one paints
winter over the city in July. Read it only if you are reproducing weather.

The chain is per-texel and per-instance, so on a GPU it belongs in the shader with the instance
colours arriving as instance attributes. `apply_reference` below is the CPU form of the same
arithmetic, for baking, for a consumer that has no per-instance attribute path, and for checking
a shader against something.
"""

import numpy

# How many colours a colorSet carries. CONTRACT — the mask has three usable channels, and the
# fourth is snow.
CHANNELS = 3

# The identity paint: white multiplies to a no-op, so an instance with no colorSet is not a
# special case anywhere downstream. CONTRACT.
IDENTITY = (1.0, 1.0, 1.0, 1.0)


def color_set(attrs):
    """One record's resolved paint as exactly `CHANNELS` RGBA tuples, padding with `IDENTITY`.

    Takes the instance record's `attrs` (or `None`). Alpha defaults to 1.0 when a shipped colour
    carries only three components. The fixed length is the point: a caller can index 0, 1, 2
    without checking, and an absent colorSet is white rather than missing.
    """
    shipped = (attrs or {}).get("colorSet") or ()
    out = []
    for i in range(CHANNELS):
        channel = shipped[i] if i < len(shipped) else None
        if channel:
            out.append(
                (
                    float(channel[0]),
                    float(channel[1]),
                    float(channel[2]),
                    float(channel[3]) if len(channel) > 3 else 1.0,
                )
            )
        else:
            out.append(IDENTITY)
    return out


def apply_reference(base, mask, colors):
    """The CPU reference for §7.2. `base` and the result are `(..., 3)` LINEAR RGB.

    `mask` is `(..., 4)` or `(..., 3)` in 0..1 — the `colorMask` texture sampled at the same
    texels as `base`. Its alpha, if present, is IGNORED: that channel is snow coverage.
    `colors` is what `color_set` returns.

    Chained over the three channels in order, exactly as the contract states it:

        for i in 0, 1, 2:   base = lerp(base, base * colors[i].rgb, mask[..., i])

    Order matters where two channels overlap on the same texel, which is why this is a chain of
    lerps and not a sum of weighted terms.
    """
    out = numpy.asarray(base, dtype=numpy.float64).copy()
    mask = numpy.asarray(mask, dtype=numpy.float64)
    for i in range(CHANNELS):
        weight = mask[..., i][..., None]
        tint = numpy.asarray(colors[i][:3], dtype=numpy.float64)
        out = out * (1.0 - weight) + (out * tint) * weight
    return out


# -- baked paint variants ------------------------------------------------------------------------

# The quantisation the bake counts distinct colour sets at (`bake/materials.py`). It is part of
# the variant contract: an index computed at any other step would land on a different centre.
VARIANT_QUANT_STEPS = 31


def multiplier(mask, colors):
    """`M(texel)` — `apply_reference` expressed as a per-texel RGB multiplier, `(..., 3)`.

    `apply_reference` is a chain of lerps ON the albedo; this is the same chain with the albedo
    factored out, so `apply_reference(base, mask, colors) == base * multiplier(mask, colors)`.
    A caller that wants to know what the tint DOES, independently of what it is applied to,
    wants this — approximating the field by a constant, measuring the error of doing so.

    Lived twice, verbatim: `bake/materials._tint_multiplier` and `measure_paint_factor.multiplier`
    — the bake and the tool that measures whether the bake's approximation is good enough. Two
    copies of the thing being measured is the one place a copy is least affordable.
    """
    out = numpy.ones((*numpy.shape(mask)[:-1], 3), dtype=numpy.float64)
    mask = numpy.asarray(mask, dtype=numpy.float64)
    for i in range(CHANNELS):
        weight = mask[..., i][..., None]
        tint = numpy.asarray(colors[i][:3], dtype=numpy.float64)
        out = out * ((1.0 - weight) + weight * tint)
    return out


def best_constant(multiplier_field, weight):
    """The per-channel constant minimising the weighted squared error against `multiplier_field`.

    Weighted by `weight` — in practice the albedo's own luminance, so the constant is chosen to
    be right where the texture is bright and the error would show. With no weight anywhere, the
    unweighted mean, which is that same minimiser in the limit.
    """
    w = weight[..., None]
    denom = float(w.sum())
    if denom <= 0.0:
        return multiplier_field.reshape(-1, 3).mean(axis=0)
    return (multiplier_field * w).reshape(-1, 3).sum(axis=0) / denom


def variant_vector(attrs):
    """One record's `colorSet` as the 9-vector the bake clusters in: three channels x RGB,
    quantised to `VARIANT_QUANT_STEPS` and back. Alpha and any fourth channel are dropped."""
    colours = color_set(attrs)
    return [
        round(float(c) * VARIANT_QUANT_STEPS) / VARIANT_QUANT_STEPS
        for colour in colours[:CHANNELS]
        for c in colour[:3]
    ]


def variant_index(attrs, centres):
    """Which of `paint_variants.json`'s centres this record's paint belongs to.

    `centres` is the prefab's `variants` list — each a `[[r,g,b], [r,g,b], [r,g,b]]`. Nearest in
    the same squared distance the bake's k-means assignment used, so a consumer's answer and the
    bake's `counts` agree. Returns 0 when there are no centres, which is the identity variant.
    """
    if not centres:
        return 0
    vector = variant_vector(attrs)
    best, best_d = 0, None
    for index, centre in enumerate(centres):
        flat = [float(v) for channel in centre for v in channel[:3]]
        flat += [1.0] * (len(vector) - len(flat))
        d = sum((a - b) ** 2 for a, b in zip(vector, flat))
        if best_d is None or d < best_d:
            best, best_d = index, d
    return best


def variant_key(centre):
    """The bake's key for one variant centre — how `materials.json`'s `derived.variants` map is
    indexed. Must stay `sha256(json.dumps(centre))[:12]` over the ROUNDED centre exactly as the
    file stores it, so this takes the parsed JSON value and re-serialises it the same way."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps(centre).encode("utf-8")).hexdigest()[:12]
