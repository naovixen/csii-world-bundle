"""A top-down picture of a bundle's ground — the one recipe, for every consumer that wants one.

Two of them do. `cs2beamng.stages.terrain` writes `<level>_minimap.png`, the image BeamNG's old
dashboard map draws its tile from; `cs2beamng.pipeline.mapview` draws the settings screen's map,
which is what makes "which part of my city?" and "where is this fast-travel point?" answerable
without knowing any coordinates. They want the same picture at different sizes and from slightly
different sources, so the picture is here and neither of them owns it.

It is not a render and does not pretend to be one: each pixel is its ground's mean albedo, shaded
by the heightfield's own slope, with water tinted over the top. That is the same information the
level draws, at metres per pixel.
"""

import os

import numpy

# The lamp: up and to the north-west, the convention every relief map uses, so a ridge reads as a
# ridge. Not a light in the scene - a slope shade over a flat colour.
LIGHT = (-0.5, 0.5, 0.707)
SHADE_FLOOR, SHADE_GAIN = 0.55, 0.55
WATER_RGB = (38.0, 86.0, 140.0)
WATER_FLOOR, WATER_GAIN = 0.6, 0.6
NEUTRAL = (128.0, 128.0, 128.0)


def shade(heights, metres_per_sample):
    """`(h, w)` in 0..1 — the slope shading a relief map applies, from the heightfield alone."""
    h = numpy.asarray(heights, dtype=numpy.float32)
    dzdx = numpy.gradient(h, metres_per_sample, axis=1)
    dzdy = -numpy.gradient(h, metres_per_sample, axis=0)
    nx, ny, nz = -dzdx, -dzdy, numpy.ones_like(h)
    norm = numpy.sqrt(nx * nx + ny * ny + nz * nz)
    lit = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
    return numpy.clip(lit / norm, 0.0, 1.0)


def ground_image(heights, layers, palette, metres_per_sample, water=None):
    """`(h, w, 3)` float — ground colour by layer, shaded by slope, water tinted over it.

    `layers` indexes `palette`; `water` is the water SURFACE height in metres on the same grid
    (0 or below the ground where there is none), or None.
    """
    lit = shade(heights, metres_per_sample)
    index = numpy.clip(numpy.asarray(layers), 0, len(palette) - 1)
    rgb = numpy.asarray(palette, dtype=numpy.float64)[index].astype(numpy.float32)
    rgb *= (SHADE_FLOOR + SHADE_GAIN * lit)[..., None]
    if water is not None:
        wet = (water > 0) & (water > numpy.asarray(heights))
        if wet.any():
            rgb[wet] = numpy.asarray(WATER_RGB) * (WATER_FLOOR + WATER_GAIN * lit[wet])[..., None]
    return rgb


def ground_palette(surfaces_dir, grounds):
    """`(N, 3)` — the mean albedo of each named ground's own texture, or a neutral grey.

    A palette is cosmetic: a texture that cannot be read gives grey rather than an exception, and
    a build is never failed for one.
    """
    out = []
    for ground in grounds:
        colour = NEUTRAL
        path = os.path.join(surfaces_dir, f"{ground}.png")
        if os.path.isfile(path):
            try:
                colour = tuple(read_rgb(path).reshape(-1, 3).mean(axis=0))
            except Exception:                             # noqa: BLE001 - cosmetic, never fatal
                pass
        out.append(colour)
    return numpy.asarray(out or [NEUTRAL], dtype=numpy.float64)


def read_rgb(path):
    """`(h, w, 3)` uint8 from an 8-bit RGB or RGBA PNG (the theme textures are either)."""
    from . import png

    try:
        return png.rgba_array(path)[:, :, :3]
    except ValueError:
        from PIL import Image                             # noqa: WPS433 - colour type 2 only

        return numpy.asarray(Image.open(path).convert("RGB"))
