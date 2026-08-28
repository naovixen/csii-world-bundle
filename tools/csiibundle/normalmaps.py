"""Normal-map packings (§7.1) — telling RGB, AG and RG apart, and rebuilding a texel.

A packed normal read as an RGB one does not look subtly wrong, it looks BLACK; the module
docstring of `csiibundle.materials` tells that story. The tests here are the producer's own
(`MaterialRipSystem.ReconstructTwoChannelNormal`), so a consumer and the ripper agree on what a
packed map is.
"""

# The three layouts a normal map in this bundle can arrive in, and what carries X in each.
# §7.1 promises the first; the other two are what a two-channel packing looks like when it
# reaches disk unrebuilt, and reading one of them as if it were the first is silent and total —
# see `detect_normal_packing`.
NORMAL_RGB = "rgb"  # X in red, Y in green, Z in blue. The contract.
NORMAL_AG = "ag"  # DXT5nm/BC3n: X in ALPHA, Y in green, red pinned white, Z reconstructed
NORMAL_RG = "rg"  # BC5: X in red, Y in green, blue pinned black, Z reconstructed

# What "pinned" means, in 0-1. Block compression dents a constant channel by a few counts, so
# these are not exact tests, and the fraction below is what actually decides.
_PINNED_HIGH = 250.0 / 255.0
_PINNED_LOW = 5.0 / 255.0
_PINNED_FRACTION = 0.9


def detect_normal_packing(samples):
    """Which of `NORMAL_RGB` / `NORMAL_AG` / `NORMAL_RG` a normal map is stored in.

    **A packed normal read as an RGB one does not look subtly wrong, it looks black.** A
    two-channel normal map ships X and Y and lets the shader reconstruct Z, and both of the
    layouts Unity's own `UnpackNormalmapRGorAG` straddles reach this bundle: AG pins red at
    white and hides X in alpha, so reading RGB gives `normal.x = +1` — a shading normal lying
    flat along the surface, which on level ground faces 90 degrees away from any sun and shades
    black. RG pins blue at black, so reading RGB gives `normal.z = -1`, a normal pointing into
    the surface, which shades black too. Measured on the reference city: 20 of 35 area surface
    normals are AG and 2 are RG, which is why every painted area rendered black.

    `samples` is any iterable of `(r, g, b, a)` floats in 0-1 — a subsample of the image is
    plenty, since what separates the layouts is a channel being CONSTANT across the whole map.
    The tests are the producer's own (`MaterialRipSystem.ReconstructTwoChannelNormal`), so a
    consumer and the ripper agree on what a packed map is.

    Returns `NORMAL_RGB` for anything that matches neither signature — including a map this
    cannot classify. Guessing at an unrecognised layout would invent relief the game does not
    draw; leaving it alone renders what the bundle actually says.
    """
    total = high_red = low_blue = low_red = 0
    varying_alpha = False
    for r, _g, b, a in samples:
        total += 1
        if r >= _PINNED_HIGH:
            high_red += 1
        if r <= _PINNED_LOW:
            low_red += 1
        if b <= _PINNED_LOW:
            low_blue += 1
        if a < _PINNED_HIGH:
            varying_alpha = True
    if total == 0:
        return NORMAL_RGB

    threshold = total * _PINNED_FRACTION
    # Each layout needs a POSITIVE signature as well as a pinned channel. AG needs alpha to be
    # carrying something: a map whose alpha is constant has no X in it. RG needs the same of RED,
    # which is where it keeps X — without that clause a black or near-black placeholder normal
    # (blue dark everywhere, red nowhere near white) reads as RG and gets "unpacked" from
    # x = y = -1 into a uniform sideways normal, which is the exact defect this detects.
    if (high_red >= threshold) and varying_alpha:
        return NORMAL_AG
    if (low_blue >= threshold) and (high_red < threshold) and (low_red < threshold):
        return NORMAL_RG
    return NORMAL_RGB


def normal_x_channel(packing):
    """Which channel holds X under `packing` — `"A"`, `"R"`. Y is always green, Z always derived.

    `None` for `NORMAL_RGB`, where nothing needs rebuilding.
    """
    return {NORMAL_AG: "A", NORMAL_RG: "R"}.get(packing)


def unpack_normal(packing, r, g, b, a):
    """One texel as a tangent-space `(x, y, z)` in -1..1. The reference the node graph mirrors.

    Z is reconstructed rather than read for the packed layouts, exactly as the game's shader
    does — the stored blue is meaningless in both of them.
    """
    if packing == NORMAL_RGB:
        return (2.0 * r - 1.0, 2.0 * g - 1.0, 2.0 * b - 1.0)
    x = 2.0 * (a if packing == NORMAL_AG else r) - 1.0
    y = 2.0 * g - 1.0
    return (x, y, max(0.0, 1.0 - x * x - y * y) ** 0.5)
