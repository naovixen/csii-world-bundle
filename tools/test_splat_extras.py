"""Splat paint-channel check: the four packed extras, and whether their edges can blend.

`terrain/splatmap.png` packs the four paintable grounds as 4-bit weights inside its B and A
bytes — `extra1:extra2` in B, `extra3:extra4` in A, low nibble first (audit SPLAT-6). The
harness used to decode that in shader nodes, which forced a `Closest` tap and a `FLOOR`: a
packed byte cannot be interpolated, because halfway between 0x0F and 0x10 decodes to a nibble
pair unrelated to either neighbour. The consequence was that every painted region — the
player's beaches included — rendered as literal 3.5 m squares, the splatmap's own texel size.

`csiibundle.splat.Splat.extras` moves the unpacking to numpy at load, and the harness's
`_Splat.extras_image` wraps it in an ordinary RGBA image the GPU can filter normally. This test
pins the claims that change rests on:

  0. The shipped decode agrees with this file's own independent reading of the format, so that
     everything below is a statement about the bundle rather than about the test.
  1. The decode is right — it reproduces the independently-shipped `terrain/user_splatmap.png`,
     which carries the same four weights packed into a different pair of channels (R and G).
     Two different packings agreeing is evidence about the decode; re-deriving one from the
     other would not be.
  2. The 8-bit image is exact, not an approximation: a nibble is 0..15 and n/15 == (n*17)/255
     for all 16 values, so the byte round-trip loses nothing.
  3. A painted edge is now a ramp rather than a step — measured as the width, in metres, of the
     transition band a bilinear tap produces across real painted boundaries, against the
     zero-width one point sampling gives.

Run: `python tools/test_splat_extras.py`  (system Python + numpy; skipped when no bundle is on
disk). The Blender half of the round-trip — that `bpy.data.images.new` keeps what is written to
it — is not reproducible here; it is asserted in `_Splat.extras_image`'s own comment, which
records that the colorspace and alpha mode must be set BEFORE the write or the buffer silently
reverts to blank.
"""

import json
import os
import sys

import numpy
import pytest
from csiibundle import png as png_reader
from csiibundle import splat as splat_reader

BUNDLE = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order"
    r"\Cities Skylines II\ModsData\Carto"
)


def unpack(byte_plane):
    """The low and high nibble of one channel as weights in 0..1."""
    return (byte_plane & 15) / 15.0, (byte_plane >> 4) / 15.0


def main():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP no bundle at {BUNDLE}")
        return 0

    splat_path = os.path.join(BUNDLE, "terrain", "splatmap.png")
    user_path = os.path.join(BUNDLE, "terrain", "user_splatmap.png")
    if not os.path.isfile(splat_path):
        print("SKIP no terrain/splatmap.png")
        return 0

    with open(os.path.join(BUNDLE, "bundle.json"), encoding="utf-8") as handle:
        rasters = json.load(handle).get("rasters", {})
    multiplier = float(rasters.get("splatmap", {}).get("splatMultiplier", 1.0))

    width, height, raw = png_reader.decode_png_rgba(splat_path)
    pixels = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(height, width, 4)
    extra1, extra2 = unpack(pixels[:, :, 2])
    extra3, extra4 = unpack(pixels[:, :, 3])
    extras = numpy.stack((extra1, extra2, extra3, extra4), axis=-1)

    failures = 0

    # 0. The shipped decode must agree with the reference one written out above. `unpack` is
    # this file's own two-line reading of the format; `Splat.extras` is what every consumer
    # actually calls. Comparing them is what makes the rest of this test about the format
    # rather than about itself.
    shipped = splat_reader.Splat(splat_path, rasters.get("splatmap", {})).extras()
    worst = float(numpy.abs(shipped.astype(numpy.float64) - extras).max())
    if worst > 1e-6:
        print(
            f"FAIL csiibundle.splat.Splat.extras disagrees with the reference decode by {worst:.6f}"
        )
        failures += 1
    else:
        print("PASS csiibundle.splat.Splat.extras matches the reference nibble decode")
    painted = extras.max(axis=2) > 0
    print(
        f"splatmap {width}x{height}; {painted.mean() * 100:.2f}% of the map carries paint; "
        "means " + ", ".join(f"extra{i + 1} {extras[:, :, i].mean():.4f}" for i in range(4))
    )

    # 1. The independent packing must agree, channel for channel.
    if os.path.isfile(user_path):
        uw, uh, uraw = png_reader.decode_png_rgba(user_path)
        if (uw, uh) != (width, height):
            print(f"FAIL user_splatmap is {uw}x{uh}, splatmap {width}x{height}")
            failures += 1
        else:
            user = numpy.frombuffer(uraw, dtype=numpy.uint8).reshape(uh, uw, 4)
            user1, user2 = unpack(user[:, :, 0])  # R packs extra1:extra2
            user3, user4 = unpack(user[:, :, 1])  # G packs extra3:extra4
            for index, (mine, theirs) in enumerate(
                ((extra1, user1), (extra2, user2), (extra3, user3), (extra4, user4)), 1
            ):
                worst = float(numpy.abs(mine - theirs).max())
                if worst > 1e-6:
                    disagree = float((numpy.abs(mine - theirs) > 1e-6).mean()) * 100
                    print(
                        f"FAIL extra{index} disagrees with user_splatmap on "
                        f"{disagree:.3f}% of texels, worst {worst:.4f}"
                    )
                    failures += 1
            if not failures:
                print("PASS all four extras match user_splatmap's independent packing exactly")
    else:
        print(
            "NOTE no terrain/user_splatmap.png — the cross-check against the second "
            "packing was skipped"
        )

    # 2. The 8-bit image `_Splat.extras_image` builds is lossless for nibble data.
    quantised = numpy.round(extras * 15.0).astype(numpy.uint8) * 17
    if not numpy.array_equal(quantised / 255.0, extras):
        print("FAIL the nibble -> byte round-trip is not exact")
        failures += 1
    else:
        print("PASS nibble weights survive an 8-bit image exactly (n/15 == n*17/255)")

    # 3. A painted edge must ramp. Sample across every horizontal paint boundary at the splat
    # resolution: point sampling steps 0 -> full in one texel by construction, so the question
    # is only whether a linear tap has anything to interpolate BETWEEN, i.e. whether the
    # opacity the shader ends up with is partial anywhere along the edge.
    cell = float(rasters.get("splatmap", {}).get("resolution", [3.5, 3.5])[0])
    strongest = int(numpy.argmax([extras[:, :, i].sum() for i in range(4)]))
    weight = extras[:, :, strongest]
    opacity = numpy.minimum(weight * multiplier, 1.0)
    edge = (opacity[:, :-1] > 0) != (opacity[:, 1:] > 0)
    if not edge.any():
        print(f"NOTE extra{strongest + 1} has no painted boundary to measure")
    else:
        rows, cols = numpy.nonzero(edge)
        # Walk outward from each boundary until the opacity saturates: that run of partial
        # values is the band a linear tap can smooth across, in metres.
        sample = numpy.linspace(0, len(rows) - 1, min(len(rows), 20000)).astype(int)
        band = []
        for r, c in zip(rows[sample], cols[sample]):
            run = 0
            for step in range(1, 9):
                k = c + step if opacity[r, c + 1] > opacity[r, c] else c - step
                if not 0 <= k < opacity.shape[1] or opacity[r, k] >= 1.0 or opacity[r, k] <= 0.0:
                    break
                run += 1
            band.append(run)
        band = numpy.asarray(band, dtype=float) * cell
        partial = float((band > 0).mean()) * 100
        print(
            f"extra{strongest + 1}: {len(rows)} painted edge texel(s); "
            f"{partial:.1f}% carry a partial-opacity band, median width "
            f"{numpy.median(band[band > 0]) if (band > 0).any() else 0.0:.1f} m "
            f"(splat texel {cell:g} m)"
        )
        if partial <= 0.0:
            print(
                "FAIL every painted edge saturates in one texel — a linear tap has nothing "
                "to interpolate and the 3.5 m steps would remain"
            )
            failures += 1
        else:
            print("PASS painted edges carry partial opacity, so a linear tap ramps them")

    print("test_splat_extras:", "FAIL" if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


@pytest.mark.needs_rip
def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_splat_extras.py`."""
    assert main() in (None, 0)
