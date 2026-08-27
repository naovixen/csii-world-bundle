"""Pre-compressed, mipmapped DDS textures — what BeamNG's own texture cooker would make.

BeamNG cooks every `.png` a material references into `temp/levels/<level>/art/textures/*.dds`
the FIRST time it meets it, on the main thread, keyed on the level name (load #13: a level under
a new name re-cooked all 190 textures between 80 s and 290 s after "Level loaded", one stall per
texture, sub-1 fps for a minute). Vanilla levels ship `.dds` and never enter the cooker — the
`temp/levels/<vanilla>/` cache holds only imposters — so a level that ships what the cooker
would have produced loads cold-cache like a vanilla one.

The formats are the cooker's own, read back from its output (`temp/levels/cs2_city_8192/`,
2026-08-16): colour → BC7 UNORM sRGB (DXGI 99), normal → BC5 UNORM (83), single-channel data
(roughness / metallic / AO / opacity) → BC4 UNORM (80), RGB data (the colour mask) → BC7 UNORM
(98); DX10 header, full mip chain to 1×1, `DDSD_LINEARSIZE`, caps TEXTURE|MIPMAP|COMPLEX,
`arraySize` 1. The block encoder is `etcpak` (BC7 mean error 0.02/255 on the reference colour
map at 0.16 s per 1024²; BC5 0.67 max 16 on a normal map — measured against Pillow's DXT
encoders, which put single-pixel errors of 90-187/255 into high-contrast blocks).

`available()` says whether the encoder is installed; without it the materials stage keeps
writing PNGs and BeamNG cooks them (correct, just slow on the first load of a new level name).
"""

import struct

import numpy

try:  # optional dependency (pyproject `textures`)
    import etcpak
except ImportError:  # pragma: no cover - environment dependent
    etcpak = None

# DXGI formats, as the cooker writes them
BC7_UNORM_SRGB = 99
BC7_UNORM = 98
BC5_UNORM = 83
BC4_UNORM = 80

DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT = 0x1, 0x2, 0x4, 0x1000
DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x20000, 0x80000
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
DDPF_FOURCC = 0x4
D3D10_RESOURCE_DIMENSION_TEXTURE2D = 3

# bytes per 4x4 block
_BLOCK_BYTES = {BC7_UNORM_SRGB: 16, BC7_UNORM: 16, BC5_UNORM: 16, BC4_UNORM: 8}

# texture kind (the materials stage's vocabulary) -> DXGI format
KIND_FORMAT = {
    "color": BC7_UNORM_SRGB,
    "normal": BC5_UNORM,
    "data": BC4_UNORM,
    "opacity": BC4_UNORM,
    "data_rgb": BC7_UNORM,
    # CS2's colour mask, converted into BeamNG's palette semantics: linear, and its
    # ALPHA is load-bearing (the tint coverage), so it must survive the encode
    "colormask": BC7_UNORM,
}


def available():
    """True when the block encoder is importable."""
    return etcpak is not None


def _bc7_params():
    params = etcpak.BC7CompressBlockParams()
    return params


def _pad4(arr):
    """Pad a (h, w[, c]) array to multiples of 4 by edge replication (the last mips are 2x2 / 1x1
    and every BC block is 4x4)."""
    h, w = arr.shape[:2]
    ph, pw = (-h) % 4, (-w) % 4
    if not ph and not pw:
        return arr
    pad = ((0, ph), (0, pw)) + ((0, 0),) * (arr.ndim - 2)
    return numpy.pad(arr, pad, mode="edge")


def _encode_level(rgba, fmt):
    """One mip level: `rgba` is (h, w, 4) uint8 C-contiguous."""
    rgba = _pad4(numpy.ascontiguousarray(rgba))
    h, w = rgba.shape[:2]
    raw = rgba.tobytes()
    if fmt in (BC7_UNORM_SRGB, BC7_UNORM):
        return etcpak.compress_bc7(raw, w, h, _bc7_params())
    if fmt == BC5_UNORM:
        return etcpak.compress_bc5(raw, w, h)
    if fmt == BC4_UNORM:
        return etcpak.compress_bc4(raw, w, h)
    raise ValueError(f"unsupported DXGI format {fmt}")


def _reduce2(arr):
    """Box-filter one mip step of a (h, w, c) uint8 array; odd sizes drop the last row/column
    (all our textures are powers of two, this is only for robustness)."""
    h, w = arr.shape[:2]
    if h == 1 and w == 1:
        return arr
    h2, w2 = max(1, h // 2), max(1, w // 2)
    a = arr[: h2 * 2, : w2 * 2].astype(numpy.uint16)
    if h >= 2 and w >= 2:
        s = a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2]
        return ((s + 2) >> 2).astype(numpy.uint8)
    if h >= 2:
        s = a[0::2] + a[1::2]
    else:
        s = a[:, 0::2] + a[:, 1::2]
    return ((s + 1) >> 1).astype(numpy.uint8)


HEADER_BYTES = 148


def encoded_bytes(width, height, fmt):
    """How many bytes `encode` writes for a `width` x `height` texture in `fmt`, WITHOUT encoding
    it: the 148-byte header plus every mip level's blocks.

    A block-compressed level is a fixed size for its dimensions - `ceil(w/4) * ceil(h/4)` blocks
    of `_BLOCK_BYTES[fmt]` - so the file size is arithmetic, not a measurement. That is what lets
    the cost meter state the texture half of a level's VRAM as COMPUTED rather than modelled
    (`pipeline/estimate.py`): the same mip walk `encode` performs, over the same formats, without
    reading a pixel. Verified byte-for-byte against a real build's 7,716 DDS files (2026-08-20).
    """
    block = _BLOCK_BYTES[fmt]
    total = HEADER_BYTES
    while True:
        total += ((width + 3) // 4) * ((height + 3) // 4) * block
        if width == 1 and height == 1:
            return total
        width, height = max(1, width // 2), max(1, height // 2)


def header(width, height, mips, fmt, linear_size):
    """The 148-byte DDS + DX10 header the cooker writes (flags 0xa1007, caps 0x401008)."""
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_MIPMAP | DDSCAPS_COMPLEX
    out = b"DDS " + struct.pack("<7I", 124, flags, height, width, linear_size, 1, mips) + b"\0" * 44
    out += struct.pack("<4I", 32, DDPF_FOURCC, struct.unpack("<I", b"DX10")[0], 0) + struct.pack(
        "<4I", 0, 0, 0, 0
    )
    out += struct.pack("<5I", caps, 0, 0, 0, 0)
    out += struct.pack("<5I", fmt, D3D10_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 0)
    return out


def encode(rgba, fmt, mipmaps=True):
    """A complete DDS file (bytes) for a (h, w, 4) uint8 array in `fmt`, mip chain to 1x1.
    BC4 reads the R channel, BC5 R and G, BC7 all four."""
    if etcpak is None:
        raise RuntimeError("etcpak is not installed (pip install etcpak)")
    arr = numpy.asarray(rgba)
    if arr.ndim != 3 or arr.shape[2] != 4 or arr.dtype != numpy.uint8:
        raise ValueError("encode wants (h, w, 4) uint8")
    h, w = arr.shape[:2]
    levels = [_encode_level(arr, fmt)]
    if mipmaps:
        cur = arr
        while cur.shape[0] > 1 or cur.shape[1] > 1:
            cur = _reduce2(cur)
            levels.append(_encode_level(cur, fmt))
    return header(w, h, len(levels), fmt, len(levels[0])) + b"".join(levels)


def to_rgba(arr, kind):
    """The (h, w, 4) uint8 array `encode` wants, from what the texture export loads: an
    (h, w, 4) RGBA array, or an (h, w) grey one (uint8 or uint16, spread into RGB, alpha 255)."""
    arr = numpy.asarray(arr)
    if arr.ndim == 2:
        g = arr
        if g.dtype != numpy.uint8:
            g = (
                (g.astype(numpy.uint32) >> 8).astype(numpy.uint8)
                if g.max() > 255
                else g.astype(numpy.uint8)
            )
        out = numpy.empty((*g.shape, 4), dtype=numpy.uint8)
        out[..., 0] = out[..., 1] = out[..., 2] = g
        out[..., 3] = 255
        return out
    if arr.shape[2] == 3:
        out = numpy.empty((*arr.shape[:2], 4), dtype=numpy.uint8)
        out[..., :3] = arr
        out[..., 3] = 255
        return out
    out = arr.astype(numpy.uint8, copy=True)
    if kind in ("color", "normal", "data_rgb"):
        out[..., 3] = 255  # no stray coverage in an opaque map
    # "colormask" deliberately keeps its alpha — see KIND_FORMAT
    return out


def read_header(data):
    """`(width, height, mips, dxgi_format)` of a DDS byte string (DX10 header only)."""
    if data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<2I", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    fourcc = data[84:88]
    fmt = struct.unpack_from("<I", data, 128)[0] if fourcc == b"DX10" else fourcc
    return width, height, mips, fmt
