"""Loading a PNG that is a PICTURE rather than a raster.

`png.py` next door is the exact, standard-library decoder for the §8 rasters — elevation, water
level, surface ids, the splatmap — which are **numbers**, and which must never pass through an
image library that colour-manages or normalises them. It deliberately implements only the two
shapes the contract uses: 8/16-bit grey (colour type 0) and RGBA8 (colour type 6).

A bundle also carries ordinary **textures**, and those are whatever the producer's `TextureWriter`
emitted — including colour type 2 (RGB), which that decoder refuses on purpose. This module is
the rule for those: **the exact decoder first, PIL for the rest.** Order matters. `decode_png_gray`
and `decode_png_rgba` never colour-manage, so anything they accept goes through them, and PIL is
the fallback for shapes they do not implement rather than the first thing tried.

That rule used to be written out four times — `groundmap.read_rgb`, `beamng/materials._load_any`,
`stages/terrain._read_alpha` and `stages/terrain._read_rgb` (a wrapper on the first) — across
three modules, none of which is about images. Refactor Phase 2 moved it here.

PIL is imported inside the functions, not at module scope: a consumer that only touches rasters
must not need it installed.
"""

import numpy

from .png import gray_array, rgba_array


def load_any(path):
    """`(array, kind)`: `(h, w, 4)` uint8 for colour images, `(h, w)` uint8|uint16 for grey ones.

    `kind` is `"rgba"` or `"gray"`. An RGB image with no alpha channel comes back as `"rgba"`
    with alpha 255 throughout, because that is what PIL's conversion produces and callers key
    off the channel count rather than the file's colour type.
    """
    try:
        return rgba_array(path), "rgba"
    except ValueError:
        pass
    try:
        return gray_array(path), "gray"
    except ValueError:
        pass
    from PIL import Image

    image = Image.open(path)
    if image.mode in ("L", "I;16", "I"):
        return numpy.asarray(image.convert("I")).astype(numpy.uint16), "gray"
    return numpy.asarray(image.convert("RGBA")), "rgba"


def load_rgb(path):
    """`(h, w, 3)` uint8, alpha dropped — for a texture being read as a colour.

    Grey deliberately does NOT go through `gray_array` here: PIL's own `convert("RGB")` is what
    this has always used for a single-channel source, and it is the one that knows how to bring
    a 16-bit channel down to 8 bits. Colour is the only case worth decoding exactly.
    """
    try:
        return rgba_array(path)[:, :, :3]
    except ValueError:
        from PIL import Image

        return numpy.asarray(Image.open(path).convert("RGB"))


def load_alpha(path):
    """`(h, w)` uint8 alpha, or `None` when the image has none or it is uniform.

    A uniform alpha channel carries no information — an RGB source padded to 255, or a mask that
    was never authored — and every caller here wants to know that, so it reads as absent rather
    than as a plane of 255s.
    """
    array_, kind = load_any(path)
    if kind != "rgba":
        return None
    alpha = array_[:, :, 3]
    return None if int(alpha.max()) == int(alpha.min()) else alpha


def resize_nearest(img, h, w):
    """`img` resized to `(h, w)` by nearest sample, taking the FIRST sample of each block.

    For a texture being matched to another texture's size — a colour mask brought to its albedo's
    dimensions. `bake/materials.py` prefers a box filter for the exact integer shrink that the
    size cap actually produces and falls back here for everything else; `measure_paint_factor.py`
    had a byte-identical copy, which meant the tool measuring the bake and the bake itself were
    two opinions about one resize.

    **It disagrees with `reader.resample_nearest` by half a texel, and that is not settled here.**
    That one adds `+ 0.5` before flooring, so it samples the CENTRE of each block, and §8 gives
    the reason: the fine texel centres sit at the coarse cell's quarter points. This one does not,
    so it takes each block's top-left corner. Centring is the better convention and applying it
    here would be a defect fix, not a move — it would change what the bake writes — so it is
    recorded rather than folded into a refactor. Do not "tidy" the two into one without measuring
    what the second one moves.
    """
    if img.shape[0] == h and img.shape[1] == w:
        return img
    rows = numpy.clip((numpy.arange(h) * img.shape[0] / h).astype(numpy.int64), 0, img.shape[0] - 1)
    cols = numpy.clip((numpy.arange(w) * img.shape[1] / w).astype(numpy.int64), 0, img.shape[1] - 1)
    return img[rows][:, cols]
