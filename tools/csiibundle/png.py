"""A reference PNG decoder for the §8 rasters — deliberately not a library call.

The terrain layers are PNGs carrying **numbers, not pictures**: 16-bit elevation, 16-bit water
level, 8-bit surface ids, an RGBA8 splatmap whose alpha is data. Handing those to an image
library that colour-manages, premultiplies or normalises to 0..1 quietly corrupts every one of
them, and the corruption looks plausible. Decoding them here means nothing sits between the
file's bytes and the caller's samples.

The second reason this is spelled out rather than imported is that it doubles as the §8 file
format's executable documentation. A consumer in a language with no PNG decoder to hand has
about eighty lines to port, and the chunk walk plus the five scanline filters below are all of
it.

**What the producer actually writes** is filter 0 (None) on every row, at 8 or 16 bits, so the
fast path is the only one a real bundle exercises. The other four filters are implemented
anyway because the format allows them and a re-encoded bundle (someone runs the rasters through
`optipng`) would otherwise decode to garbage rather than fail.

16-bit samples come back as host-order `array("H")` values in 0..65535 — the raw integers, not
metres. Turning them into world units needs the `rasters` block's height range (§8); see
`reader.terrain_paths`.
"""

import struct
import sys
import zlib
from array import array

import numpy

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG colour-type codes, for the two shapes §8 uses. CONTRACT.
COLOUR_TYPE_GRAY = 0
COLOUR_TYPE_RGBA = 6


def unfilter_slow(kind, line, previous, bpp):
    """Reference per-byte reconstruction for Average(3) and Paeth(4).

    Both predict from bytes of the CURRENT row that this same loop has already reconstructed
    (`line[i - bpp]`), so — unlike None/Up/Sub — they cannot be vectorised across the row and
    keep the original sequential algorithm. `line` is mutated in place and returned.
    """
    stride = len(line)
    if kind == 3:      # Average
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif kind == 4:    # Paeth
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up = previous[i]
            corner = previous[i - bpp] if i >= bpp else 0
            estimate = left + up - corner
            distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
            predictor = (left, up, corner)[distances.index(min(distances))]
            line[i] = (line[i] + predictor) & 0xFF
    else:
        raise ValueError(f"unfilter_slow: unsupported filter type {kind}")
    return line


def unfilter_scanlines(raw, width, height, bpp):
    """Undo the PNG scanline filters with numpy where the filter allows it.

    None(0) and Up(2) vectorise across the whole row (a straight copy / a row-wide add).
    Sub(1) is a running sum per interleaved byte lane — reshaping the row to (pixel, channel)
    and taking a cumulative sum down the pixel axis reproduces `recon[i] = filt[i] +
    recon[i - bpp]` for every channel at once; the sum is widened to uint32 before truncating
    back to uint8 because numpy's uint8 cumsum overflows silently instead of wrapping mod 256,
    and the truncation *is* the mod-256 the format requires. Average(3) and Paeth(4) predict
    from the current row's own already-reconstructed bytes and are genuinely sequential, so
    they fall back to `unfilter_slow`, paid only on the rows that actually use them (the
    producer only ever writes filter 0, so in practice that fallback is cold).
    """
    stride = width * bpp
    out = numpy.zeros((height, stride), dtype=numpy.uint8)
    previous = numpy.zeros(stride, dtype=numpy.uint8)
    for y in range(height):
        offset = y * (stride + 1)
        kind = raw[offset]
        line = numpy.frombuffer(raw, dtype=numpy.uint8, count=stride, offset=offset + 1).copy()
        if kind == 0:      # None
            pass
        elif kind == 2:    # Up
            line += previous
        elif kind == 1:    # Sub
            lanes = line.reshape(-1, bpp).astype(numpy.uint32)
            line = numpy.cumsum(lanes, axis=0, dtype=numpy.uint32).astype(numpy.uint8).reshape(-1)
        else:              # Average(3) / Paeth(4)
            slow = unfilter_slow(kind, bytearray(line.tobytes()), bytes(previous.tobytes()), bpp)
            line = numpy.frombuffer(bytes(slow), dtype=numpy.uint8)
        out[y] = line
        previous = out[y]
    return bytearray(out.tobytes())


def _read_chunks(path):
    """`(width, height, bit depth, colour type, concatenated IDAT)` from one PNG."""
    data = open(path, "rb").read()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG")

    pos = 8
    width = height = depth = colour_type = None
    idat = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length                       # 4 length + 4 tag + payload + 4 CRC
        if tag == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
            depth, colour_type = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break
    return width, height, depth, colour_type, b"".join(idat)


def decode_png_gray(path):
    """Decode a single-channel PNG to `(width, height, samples)`.

    Samples come back at the file's own bit depth — 16-bit values 0..65535 for elevation and
    water level, 8-bit ids for the surface index — as an `array` of ints, host byte order. Row
    0 is NORTH (§3), which is the opposite of most image libraries' bottom-up convention and
    the usual cause of a mirrored terrain.
    """
    width, height, depth, colour_type, compressed = _read_chunks(path)
    if colour_type != COLOUR_TYPE_GRAY or depth not in (8, 16):
        raise ValueError(f"{path}: contract §8 rasters are 8- or 16-bit grayscale, "
                         f"got colour type {colour_type} at depth {depth}")

    raw = zlib.decompress(compressed)
    bpp = depth // 8
    out = unfilter_scanlines(raw, width, height, bpp)

    if depth == 16:
        # PNG stores 16-bit samples big-endian; `array("H")` reads host order.
        samples = array("H", bytes(out))
        if sys.byteorder == "little":
            samples.byteswap()
    else:
        samples = array("B", bytes(out))
    return width, height, samples


def decode_png_rgba(path):
    """Decode an RGBA8 PNG (the §8 splatmap) to `(width, height, bytes)`.

    The alpha channel is DATA, never opacity — see `csiibundle.splat`. Nothing here
    premultiplies or otherwise touches it.
    """
    width, height, depth, colour_type, compressed = _read_chunks(path)
    if colour_type != COLOUR_TYPE_RGBA or depth != 8:
        raise ValueError(f"{path}: the splatmap is RGBA8, got colour type {colour_type} "
                         f"at depth {depth}")

    raw = zlib.decompress(compressed)
    return width, height, bytes(unfilter_scanlines(raw, width, height, 4))


def gray_array(path):
    """`decode_png_gray` as a `(height, width)` numpy array — the convenient form.

    Same samples, same orientation (row 0 north), reshaped and typed. `uint16` for a 16-bit
    raster, `uint8` for an 8-bit one.
    """
    width, height, samples = decode_png_gray(path)
    dtype = numpy.uint16 if samples.typecode == "H" else numpy.uint8
    return numpy.frombuffer(bytes(samples), dtype=dtype).reshape(height, width)


def rgba_array(path):
    """`decode_png_rgba` as a `(height, width, 4)` uint8 numpy array."""
    width, height, pixels = decode_png_rgba(path)
    return numpy.frombuffer(pixels, dtype=numpy.uint8).reshape(height, width, 4)
