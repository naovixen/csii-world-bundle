"""PNG reference-decoder check against PIL, on the real bundle rasters plus synthetic ones.

`csiibundle.png` decodes the contract §8 terrain rasters itself rather than through an image
library, so that nothing colour-manages, premultiplies or renormalises samples that are
NUMBERS — elevation, water level, surface ids, packed splat weights. This test checks that
decoder against PIL, which is ground truth here.

It used to regex the functions out of `blender_import_bundle.py`, which imports `bpy` at module
scope and so cannot be imported under system Python. Now that they live in the package it just
imports them, and the test breaks honestly if a name moves instead of silently matching nothing.

The real bundle rasters are all written by the producer with filter 0 (None) on every row —
confirmed by histogram below — so they only exercise the None fast path. Sub(1), Up(2),
Average(3) and Paeth(4) are exercised by hand-built synthetic PNGs instead (see
`_build_synthetic_png`), one per bit depth/channel-count combination the decoder supports
(gray8, gray16, rgba8), each cycling through all five filter kinds row by row.
"""

import os
import struct
import sys
import tempfile
import zlib

import numpy
from csiibundle import png as png_reader
from PIL import Image

BUNDLE = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
    r"\Cities Skylines II\ModsData\Carto"
)
# Synthetic PNGs are throwaway test fixtures — keep them out of the repo tree entirely rather
# than relying on .gitignore or careful `git add`.
SCRATCH = os.path.join(tempfile.gettempdir(), "carto_test_png_decode")

# Keyed by the names the cases below use, so a rename in the package fails here loudly.
namespace = {
    "_unfilter_slow": png_reader.unfilter_slow,
    "_unfilter_scanlines": png_reader.unfilter_scanlines,
    "_decode_png_gray": png_reader.decode_png_gray,
    "_decode_png_rgba": png_reader.decode_png_rgba,
}

failures = 0


def check(label, decoder_name, path):
    """Decode `path` with the harness's `decoder_name` and compare against PIL, sample-exact."""
    global failures
    _width, _height, data = namespace[decoder_name](path)
    truth = numpy.asarray(Image.open(path))
    if truth.dtype.itemsize > 1:
        # PIL's I;16 already comes back as native-endian uint16 with the correct numeric
        # values (it undoes the file's big-endian storage itself). _decode_png_gray does the
        # same: it byteswaps its 16-bit `array` into host order right after decoding. So both
        # sides are plain native-endian integers here — no explicit byte-order dance needed.
        mine = numpy.array(data, dtype=truth.dtype).reshape(truth.shape)
    else:
        mine = numpy.frombuffer(bytes(data), dtype=numpy.uint8).reshape(truth.shape)
    ok = numpy.array_equal(mine, truth)
    print(("PASS " if ok else "FAIL ") + f"{label} ({os.path.basename(path)})")
    failures += 0 if ok else 1


# -- real bundle rasters ---------------------------------------------------------------------
# All six exist per the task brief; heightmap/water_level are 16-bit gray (I;16), cutout/
# surface_index are 8-bit gray (L), splatmap/user_splatmap are RGBA8 — between them every
# decoder path the harness has (_decode_png_gray at both bit depths, _decode_png_rgba) is hit.
REAL_CASES = [
    ("heightmap.png", "_decode_png_gray"),
    ("splatmap.png", "_decode_png_rgba"),
    ("cutout.png", "_decode_png_gray"),
    ("surface_index.png", "_decode_png_gray"),
    ("water_level.png", "_decode_png_gray"),
    ("user_splatmap.png", "_decode_png_rgba"),
]

print("-- real bundle rasters --")
filter_histogram = {}
for name, decoder in REAL_CASES:
    path = os.path.join(BUNDLE, "terrain", name)
    if not os.path.isfile(path):
        print(f"SKIP {name} (no bundle)")
        continue
    check(name, decoder, path)

    # Histogram the per-scanline filter byte directly off the IDAT stream, independent of the
    # decoder under test, so the coverage claim below doesn't rely on the code being checked.
    raw_file = open(path, "rb").read()
    pos = 8
    width = height = depth = colour_type = None
    idat = []
    while pos + 8 <= len(raw_file):
        (length,) = struct.unpack(">I", raw_file[pos : pos + 4])
        tag = raw_file[pos + 4 : pos + 8]
        payload = raw_file[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
            depth, colour_type = payload[8], payload[9]
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break
    bpp = (depth // 8) * (4 if colour_type == 6 else 1)
    stride = width * bpp
    raw = zlib.decompress(b"".join(idat))
    counts = {}
    for y in range(height):
        kind = raw[y * (stride + 1)]
        counts[kind] = counts.get(kind, 0) + 1
    filter_histogram[name] = counts

print("\nFilter-byte histogram across real rasters (kind -> row count):")
for name, counts in filter_histogram.items():
    print(f"  {name}: {dict(sorted(counts.items()))}")
only_none = all(set(c) <= {0} for c in filter_histogram.values())
if only_none:
    print(
        "  -> every real raster uses filter 0 (None) exclusively; "
        "Sub/Up/Average/Paeth are untested by real data, covered by synthetics below."
    )


# -- synthetic PNGs exercising every filter kind --------------------------------------------


def _paeth_predictor(left, up, corner):
    p = left + up - corner
    pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return corner


def _forward_filter(kind, raw_line, raw_prev, bpp):
    """The PNG encoder's inverse of the harness's decoder: predicts from the true (unfiltered)
    neighbour samples, exactly as libpng does, so that decoding the result must reproduce
    `raw_line` byte for byte."""
    stride = len(raw_line)
    out = bytearray(stride)
    for i in range(stride):
        left = raw_line[i - bpp] if i >= bpp else 0
        up = raw_prev[i]
        corner = raw_prev[i - bpp] if i >= bpp else 0
        if kind == 0:
            pred = 0
        elif kind == 1:
            pred = left
        elif kind == 2:
            pred = up
        elif kind == 3:
            pred = (left + up) // 2
        elif kind == 4:
            pred = _paeth_predictor(left, up, corner)
        else:
            raise ValueError(kind)
        out[i] = (raw_line[i] - pred) & 0xFF
    return bytes(out)


def _png_chunk(tag, payload):
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _build_synthetic_png(path, width, height, depth, colour_type, bpp, rng):
    """Hand-assemble a PNG whose rows cycle through all five filter kinds (0..4), so the
    decoder under test is forced through Sub/Up/Average/Paeth as well as None — filters the
    real bundle rasters never use. Pixel data is pseudo-random bytes covering the full 0..255
    range so predictors see varied, non-degenerate neighbours."""
    stride = width * bpp
    raw_rows = [bytes(rng.randint(0, 255) for _ in range(stride)) for _ in range(height)]

    body = bytearray()
    for y in range(height):
        kind = y % 5
        prev = raw_rows[y - 1] if y > 0 else bytes(stride)
        body.append(kind)
        body += _forward_filter(kind, raw_rows[y], prev, bpp)

    idat = zlib.compress(bytes(body), 9)
    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour_type, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_png_chunk(b"IHDR", ihdr))
        f.write(_png_chunk(b"IDAT", idat))
        f.write(_png_chunk(b"IEND", b""))

    kinds_used = sorted({y % 5 for y in range(height)})
    return kinds_used


os.makedirs(SCRATCH, exist_ok=True)
import random  # noqa: E402 - after the sys.path bootstrap above

rng = random.Random(20260807)

print("\n-- synthetic PNGs (all five filter kinds) --")
SYNTH_CASES = [
    ("gray8.png", 8, 0, 1, "_decode_png_gray"),
    ("gray16.png", 16, 0, 2, "_decode_png_gray"),
    ("rgba8.png", 8, 6, 4, "_decode_png_rgba"),
]
for filename, depth, colour_type, bpp, decoder in SYNTH_CASES:
    path = os.path.join(SCRATCH, filename)
    kinds = _build_synthetic_png(
        path, width=17, height=23, depth=depth, colour_type=colour_type, bpp=bpp, rng=rng
    )
    assert kinds == [0, 1, 2, 3, 4], f"{filename}: synthetic build didn't cover all filters"
    check(f"synthetic depth={depth} colour_type={colour_type}", decoder, path)

print("\n-- encoders (csiibundle.png.encode_gray / encode_rgba round-trip, deterministic) --")
import numpy as _np  # noqa: E402 - after the sys.path bootstrap above
from csiibundle import png as _png  # noqa: E402 - after the sys.path bootstrap above

_cases = [
    (
        "enc_gray8.png",
        ((_np.arange(17 * 23, dtype=_np.uint32).reshape(23, 17) * 3) % 256).astype(_np.uint8),
        _png.gray_array,
    ),
    (
        "enc_gray16.png",
        (_np.arange(17 * 23, dtype=_np.uint32).reshape(23, 17) * 197 % 65536).astype(_np.uint16),
        _png.gray_array,
    ),
    (
        "enc_rgba8.png",
        _np.frombuffer(
            bytes(rng.getrandbits(8) for _ in range(23 * 17 * 4)), dtype=_np.uint8
        ).reshape(23, 17, 4),
        _png.rgba_array,
    ),
]
for filename, arr, back in _cases:
    path = os.path.join(SCRATCH, filename)
    data = _png.encode_rgba(arr) if arr.ndim == 3 else _png.encode_gray(arr)
    with open(path, "wb") as f:
        f.write(data)
    again = _png.encode_rgba(arr) if arr.ndim == 3 else _png.encode_gray(arr)
    ok = _np.array_equal(back(path), arr) and again == data
    print(("ok  " if ok else "FAIL") + f"  {filename}: round-trip {'exact' if ok else 'MISMATCH'}")
    failures += 0 if ok else 1
_ds = _png.downsample_box(_np.arange(16, dtype=_np.uint8).reshape(4, 4), 2)
ok = _ds.tolist() == [[2, 4], [10, 12]]
print(("ok  " if ok else "FAIL") + "  downsample_box 4x4 -> 2x2 block means")
failures += 0 if ok else 1

if __name__ == "__main__":
    sys.exit(1 if failures else 0)


def gate():
    """pytest entry point (tools/conftest.py).

    This file is still a script: its body runs at import, and `check()` counts what failed
    rather than raising. So the gate reads the counter. Phase 4 turns the body into real
    per-claim pytest functions, at which point this goes away.
    """
    assert failures == 0, f"{failures} check(s) failed — see the captured output above"
