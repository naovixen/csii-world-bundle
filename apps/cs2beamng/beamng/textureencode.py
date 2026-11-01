"""What a decoded source image becomes in the file `textureplan.export_texture` writes.

The per-pixel half of the export: the encoding each kind goes out as, the rebuild of a packed
normal, and CS2's colour mask converted into BeamNG's palette semantics. Any change to what a
given source becomes here bumps `textureplan.TEXTURE_PIPELINE_VERSION`."""

import numpy
from csiibundle import materials as bundle_materials
from csiibundle import png

from . import dds as dds_mod


def encoded_bytes(arr, form, kind, keep_alpha, use_dds):
    """The bytes `export_texture` writes for a decoded, capped `arr` read as `form`."""
    if kind == "opacity":
        alpha = arr[:, :, 3] if form == "rgba" else (numpy.full(arr.shape[:2], 255, numpy.uint8))
        return (
            _encode_dds(alpha.astype(numpy.uint8), "opacity")
            if use_dds
            else png.encode_gray(alpha.astype(numpy.uint8), bit_depth=8)
        )
    if kind == "colormask":
        # BEFORE the `form == "gray"` branch: a single-channel mask is still a colour mask, and
        # the conversion has to run on it too. (This branch was written into `texture_name` by
        # mistake in `199f084`, where `arr`/`form` do not exist — so every `colormask` export
        # raised NameError, no mask reached the level at all, and A6.3's fix was in the build
        # report and nowhere else. 724 textures on the reference city.)
        rgba = _colormask_rgba(arr, form)
        return _encode_dds(rgba, "colormask", keep_alpha=True) if use_dds else png.encode_rgba(rgba)
    if form == "gray":
        g = arr
        if g.dtype != numpy.uint8:
            g = (
                (g.astype(numpy.uint32) >> 8).astype(numpy.uint8)
                if g.max() > 255
                else g.astype(numpy.uint8)
            )
        return _encode_dds(g, kind) if use_dds else png.encode_gray(g, bit_depth=8)
    if keep_alpha:
        return _encode_dds(arr, kind, keep_alpha=True) if use_dds else png.encode_rgba(arr)
    return (
        _encode_dds(arr, kind if kind != "data" else "data_rgb") if use_dds else png.encode_rgb(arr)
    )


def _rebuilt_normal(arr):
    """`(rgba, packing)`: an AG- or RG-packed normal rebuilt to §7.1 RGB (Z reconstructed as
    the game's shader does), or the array untouched with `packing` None for an RGB map. The
    per-texel rule is `csiibundle.normalmaps.unpack_normal`; this is the same arithmetic over
    the whole image."""
    sample = arr[::16, ::16].reshape(-1, arr.shape[2]).astype(numpy.float64) / 255.0
    packing = bundle_materials.detect_normal_packing(tuple(row[:4]) for row in sample)
    if packing == bundle_materials.NORMAL_RGB:
        return arr, None
    f = arr.astype(numpy.float64) / 255.0
    x = 2.0 * (f[..., 3] if packing == bundle_materials.NORMAL_AG else f[..., 0]) - 1.0
    y = 2.0 * f[..., 1] - 1.0
    z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, None))
    out = numpy.empty_like(arr)
    for channel, value in enumerate((x, y, z)):
        out[..., channel] = numpy.clip(numpy.rint((value + 1.0) * 127.5), 0, 255).astype(
            numpy.uint8
        )
    out[..., 3] = 255
    return out, packing


def _colormask_rgba(arr, form):
    """CS2's colour mask, converted into the semantics BeamNG's palette actually implements.

    THE TWO ARE NOT THE SAME MAP, and shipping CS2's straight through is why buildings render
    wrong. Read from `shaders/common/material/shadergen/shadergen.h.hlsl::getColorPalette`:

        rgb  = rgb / (r + g + b)                    # NORMALISED to a simplex - a selector
        cov  = a                                    # the ALPHA is how much tint applies at all
        rgb *= cov ;  rest = 1 - cov
        palette = SUM rgb[i] * linear(instanceColor[i]) + rest * float3(1,1,1)
        baseColor *= palette

    CS2's mask (bundle §7.2) is three INDEPENDENT lerp weights and its alpha means nothing:

        t = lerp(t, t * colorSet[i], mask[i])   for i in R, G, B

    So BeamNG's RGB is only a *chooser* and its ALPHA carries the weight. We exported the mask as
    `data_rgb`, which forces alpha to 255 — coverage 1 on every texel, so the white "untinted"
    remainder contributes nothing. Wherever CS2's mask says (0,0,0) — the MEDIAN texel on 234 of
    400 measured masks — the palette evaluates to (0,0,0) and the surface renders BLACK; wherever
    it says (0.3,0,0) the normalisation makes it a full-strength tint instead of a 30 % one.

    The conversion is EXACT while the channels are authored disjoint, which §7.2 states they are:
    with one channel `i` active, CS2 gives `lerp(1, c_i, m_i)`, and setting `alpha = r + g + b`
    makes BeamNG compute `m_i * c_i + (1 - m_i) * 1` — the same thing. Where two channels overlap
    BeamNG can only mix where CS2 multiplies, and no alpha can fix that; it is declared in the
    report as `colorMaskOverlap` with its count.
    """
    rgba = numpy.zeros((*arr.shape[:2], 4), dtype=numpy.uint8)
    if form == "gray":
        rgba[..., 0] = arr if arr.dtype == numpy.uint8 else arr.astype(numpy.uint8)
    else:
        rgba[..., :3] = arr[..., :3]
    total = rgba[..., :3].astype(numpy.uint16).sum(axis=2)
    rgba[..., 3] = numpy.minimum(total, 255).astype(numpy.uint8)
    return rgba


def _encode_dds(arr, kind, keep_alpha=False):
    """The DDS bytes for an exported array: grey -> BC4, normal -> BC5, colour -> BC7 sRGB
    (alpha kept only when asked), RGB data -> BC7 linear."""
    rgba = dds_mod.to_rgba(arr, kind if not keep_alpha else "color_alpha")
    if kind == "color" and rgba.shape[2] == 4 and not keep_alpha:
        rgba[..., 3] = 255
    fmt = dds_mod.KIND_FORMAT.get(kind, dds_mod.BC7_UNORM_SRGB)
    return dds_mod.encode(rgba, fmt)
