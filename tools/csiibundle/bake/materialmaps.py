"""The `materials` product's per-file jobs — mask splits, normal rebuilds, tinted albedos.

Module-level functions taking one tuple, which is what `Pool` (spawn on Windows) needs; the
same functions run in-process when `jobs <= 1`, so the serial path is the same code. The
product that drives them is `csiibundle.bake.materials`; the variant selection they tint for
is `csiibundle.bake.paint`.

**The §7.2 tint is a pure per-texel MULTIPLIER.** `tints.apply_reference` is the game's
`lerp(base, base*c, mask)` in LINEAR light: each channel's step is
`base*(1-w) + base*c*w = base*((1-w) + w*c)`, so the whole chain is `base * M(texel)` with `M`
a function of the colour mask and the centre alone — never of the albedo. That is what makes a
single `baseColorFactor` per variant a candidate: it costs ZERO textures where a tinted albedo
costs a megabyte, and the error it makes is directly measurable against the exact image
`tint_job` already computes. So both are produced: the constant that minimises the
(alpha-weighted) squared error, the worst 8-bit sRGB channel error it makes, and the texture
ONLY where that error exceeds `paint_factor_max_error` (0 keeps every texture, the
pre-2026-08-18 behaviour).

**Where `M` is constant the error is exactly 0 and nothing needs measuring.** A colour mask
that is uniform over the whole texture — 31.4 % of the 7,592 (albedo, variant) pairs on the
reference city — makes `M` a single RGB triple, so the "approximation" IS the exact image and
the two sRGB encodes that measured the difference between them were 23 minutes of proving a
tautology. That case takes the constant straight from the mask value and never decodes the
albedo at all. Where `M` does vary, the approximate image comes from a 256-entry lookup table
per channel rather than a full `power()` over the texture: `base` is uint8 and the constant is
constant, so `f(g(v)·c)` has 256 distinct answers, and the LUT is the same arithmetic on the
same values — bit-identical output, one encode per pair instead of two.
"""

import hashlib
import os

import numpy

from .. import colourspace, images, materials, png, tints

# Jobs per worker hand-off for the `materials` product's parallel passes. They are all "one
# small file per job" - a mask to split, a normal to rebuild, a mesh to scan, an albedo to tint
# - so the IPC per job dominates unless several travel together. Also the threshold: below this
# many jobs a pool costs more to start than it saves. TUNED.
CHUNK = 8


def sha_stem(path):
    """`textures/<sha>.png` → `<sha>`; anything else → a sha of the path."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return hashlib.sha256((path or "").encode("utf-8")).hexdigest()


def resample_mask(img, h, w):
    """The colour mask at the albedo's size — box-filtered when it is being shrunk.

    The mask is a per-texel BLEND WEIGHT, so averaging it over the texels that collapse into one
    is what the collapse means; taking one of them (`images.resize_nearest`) makes a hard edge out of
    a boundary the source resolves smoothly, and the texel then ships fully tinted or fully
    untinted rather than the mixture. Measured over 40 real (albedo, mask) pairs against tinting
    at 1024 and averaging afterwards: nearest is 1.441/255 RMS with 0.53 % of channel samples off
    by more than 4/255, the box filter 0.532/255 and 0.15 %.

    Only for an exact integer shrink on both axes, which is the only case the cap produces;
    anything else (a mask smaller than its albedo, a non-multiple size) is still nearest, because
    a box filter has nothing to average there.
    """
    if (
        img.shape[0] > h
        and img.shape[1] > w
        and img.shape[0] % h == 0
        and img.shape[1] % w == 0
        and img.shape[0] // h == img.shape[1] // w
    ):
        return png.downsample_box(img, img.shape[0] // h)
    return images.resize_nearest(img, h, w)


def split_mask_job(args):
    """One §7.1 mask -> its metallic (R) and roughness (255 - A) greys. `(rel, outs, error)`."""
    bundle, derived, rel = args
    stem = sha_stem(rel)
    outs = {
        "metallic": f"textures/{stem}_metallic.png",
        "roughness": f"textures/{stem}_roughness.png",
    }
    src = os.path.join(bundle, rel)
    if not os.path.isfile(src):
        return rel, None, None
    try:
        rgba = png.rgba_array(src)
        png.write_gray(os.path.join(derived, outs["metallic"]), rgba[:, :, 0])
        png.write_gray(os.path.join(derived, outs["roughness"]), 255 - rgba[:, :, 3])
    except Exception as exc:
        return rel, None, repr(exc)
    return rel, outs, None


def normal_job(args):
    """One normal map: detect its packing and rebuild it to RGB when it is two-channel.
    `(rel, entry_or_None, checked, error)`."""
    bundle, derived, rel = args
    src = os.path.join(bundle, rel)
    if not os.path.isfile(src):
        return rel, None, False, None
    try:
        rgba = png.rgba_array(src)
        stride = max(1, rgba.shape[0] // 64)
        sample = rgba[::stride, ::stride].reshape(-1, 4).astype(numpy.float64) / 255.0
        packing = materials.detect_normal_packing(sample)
        if packing == materials.NORMAL_RGB:
            return rel, None, True, None
        f = rgba.astype(numpy.float64) / 255.0
        x = 2.0 * (f[:, :, 3] if packing == materials.NORMAL_AG else f[:, :, 0]) - 1.0
        y = 2.0 * f[:, :, 1] - 1.0
        z = numpy.sqrt(numpy.clip(1.0 - x * x - y * y, 0.0, 1.0))
        out = numpy.stack([(x + 1) * 0.5, (y + 1) * 0.5, (z + 1) * 0.5, numpy.ones_like(z)], axis=2)
        out8 = numpy.clip(numpy.rint(out * 255.0), 0, 255).astype(numpy.uint8)
        dst = f"textures/{sha_stem(rel)}_normal_rgb.png"
        png.write_rgba(os.path.join(derived, dst), out8)
    except Exception as exc:
        return rel, None, False, repr(exc)
    return rel, {"packing": packing, "path": dst}, True, None


def _uniform_mask_factors(mask, variants):
    """The uniform-mask tautology: `M` is one RGB triple, the constant IS exact.

    Uniformity is decided on the FULL-RESOLUTION mask: nearest-resampling a uniform channel
    keeps it uniform, so this can only under-claim, never over-claim.
    """
    if not (
        mask.size
        and bool(numpy.all(mask[:, :, :3].min(axis=(0, 1)) == mask[:, :, :3].max(axis=(0, 1))))
    ):
        return None
    mask_lo = mask[:, :, :3].min(axis=(0, 1)).astype(numpy.float64) / 255.0
    out = {}
    for vkey, centre in variants:
        constant = tints.multiplier(mask_lo, [(*tuple(centre[i]), 1.0) for i in range(3)])
        out[vkey] = {
            "factor": [round(float(v), 5) for v in constant],
            "maxErr": 0,
            "path": None,
        }
    return out


def _tint_variant(prepared, vkey, centre, variant_out, max_error):
    """One variant against one prepared albedo: the constant, its error, and the texture only
    where the error is too big. Returns 1 when a texture was written."""
    derived, base_rel, base, base_lin, mask_r, alpha, visible, any_visible = prepared
    colours = [(*tuple(centre[i]), 1.0) for i in range(3)]
    multiplier = tints.multiplier(mask_r[:, :, :3], colours)
    lo = multiplier.min(axis=(0, 1))
    if numpy.array_equal(lo, multiplier.max(axis=(0, 1))):
        # this variant's centre flattened a mask that is not itself flat (every channel it
        # touches is 1.0): same tautology, same answer
        variant_out[vkey] = {"factor": [round(float(v), 5) for v in lo], "maxErr": 0, "path": None}
        return 0
    constant = tints.best_constant(multiplier, alpha)
    exact8 = colourspace.linear_to_srgb8(base_lin * multiplier)
    approx8 = _apply_lut(_srgb8_lut(constant), base[:, :, :3])
    delta = numpy.abs(exact8.astype(numpy.int16) - approx8.astype(numpy.int16))
    if any_visible:
        delta = delta[visible]
    max_err = int(delta.max()) if delta.size else 0
    factor_row = [round(float(v), 5) for v in constant]
    if max_err <= int(max_error):
        variant_out[vkey] = {"factor": factor_row, "maxErr": max_err, "path": None}
        return 0
    rel = f"textures/{sha_stem(base_rel)}_v{vkey}.png"
    path = os.path.join(derived, rel)
    variant_out[vkey] = {"factor": factor_row, "maxErr": max_err, "path": rel}
    # A file already there is reused — the encode is deterministic — UNLESS it is the
    # wrong size. `variant_max_size` is a shaping option, so changing it re-runs this
    # product, and a name that does not carry the size would otherwise keep serving the
    # old cap's pixels for ever.
    h, w = base.shape[:2]
    if png_size(path) != (h, w):
        png.write_rgba(path, numpy.concatenate([exact8, base[:, :, 3:4]], axis=2))
        return 1
    return 0


def tint_job(args):
    """One source albedo tinted into every wanted variant — the multiplier identity and the
    factor-vs-texture split are the module docstring.
    `(base_rel, written, error, {vkey: {"factor", "maxErr", "path"}})`."""
    bundle, derived, base_rel, mask_rel, variants, variant_max, max_error = args
    try:
        mask = png.rgba_array(os.path.join(bundle, mask_rel))
    except Exception as exc:
        return base_rel, 0, repr(exc), {}
    uniform = _uniform_mask_factors(mask, variants)
    if uniform is not None:
        return base_rel, 0, None, uniform

    try:
        base = png.rgba_array(os.path.join(bundle, base_rel))
    except Exception as exc:
        return base_rel, 0, repr(exc), {}
    h, w = base.shape[:2]
    factor = 1
    while max(h, w) // factor > variant_max:
        factor *= 2
    if factor > 1:
        base = png.downsample_box(base, factor)
        h, w = base.shape[:2]
    mask_r = resample_mask(mask, h, w).astype(numpy.float64) / 255.0
    base_lin = colourspace.srgb8_to_linear(base[:, :, :3])
    alpha = base[:, :, 3].astype(numpy.float64) / 255.0
    visible = alpha > 0.5
    prepared = (derived, base_rel, base, base_lin, mask_r, alpha, visible, bool(visible.any()))
    written = 0
    out = {}
    for vkey, centre in variants:
        written += _tint_variant(prepared, vkey, centre, out, max_error)
    return base_rel, written, None, out


def png_size(path):
    """`(height, width)` from a PNG's IHDR, or `None` when there is no readable file there.

    Row-major, the way numpy reports a shape — the opposite order to `png.read_header`, which is
    why this swaps rather than simply forwarding.
    """
    header = png.read_header(path)
    return None if header is None else (header[1], header[0])


def _srgb8_lut(constant):
    """`(3, 256)` uint8: `colourspace.linear_to_srgb8(colourspace.srgb8_to_linear(v) * constant[j])` for every byte `v`.

    The albedo is uint8, so a constant multiplier has only 256 possible answers per channel and
    the whole `power()` over the texture is a gather. Same function, same inputs, same bits.
    """
    lin = colourspace.srgb8_to_linear(numpy.arange(256, dtype=numpy.uint8))
    return numpy.stack([colourspace.linear_to_srgb8(lin * float(constant[j])) for j in range(3)])


def _apply_lut(lut, rgb8):
    """`(h, w, 3)` uint8 through a per-channel `(3, 256)` table."""
    return numpy.stack([lut[j][rgb8[:, :, j]] for j in range(3)], axis=2)
