"""A packed normal map never reaches the level packed (validation pass 2026-08-31, B1 + B2).

Two guards on the same defect. `textureencode._rebuilt_normal` rebuilds an AG- or RG-packed
normal (`csiibundle.normalmaps`: read as RGB it shades BLACK) to §7.1 RGB before the DDS
encoder sees it, with Z reconstructed as the game's shader does; and `plan_canonical` takes the
bake's own rebuilt map (`derived.normalRgb`) where the bake made one — it used to look the map
up by material NAME in a table keyed by PATH, a lookup that could never hit.

Run: `python tools/test_texture_normals.py`  (system Python + numpy; no bundle needed).
"""

import os
import sys
import tempfile

import numpy

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


def _tilted(size=64):
    """A tangent-space normal field tilted a known way: (0.36, 0.48, 0.80), unit length."""
    n = numpy.array([0.36, 0.48, 0.80])
    rgb = numpy.rint((n + 1.0) * 127.5).astype(numpy.uint8)
    out = numpy.empty((size, size, 4), dtype=numpy.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255
    return out, n


def _check_rebuilt_normals():
    """B1: an AG- or RG-packed normal is rebuilt to RGB; an RGB one is left as it is."""
    from cs2beamng.beamng import textureencode
    from csiibundle import materials

    rgb, n = _tilted()
    same, packing = textureencode._rebuilt_normal(rgb)
    check(packing is None and same is rgb, "an RGB normal is left exactly as it is")

    ag = numpy.empty_like(rgb)  # DXT5nm: red pinned white, X in alpha, Y in green
    ag[..., 0] = 255
    ag[..., 1] = rgb[..., 1]
    ag[..., 2] = 0
    ag[..., 3] = rgb[..., 0]
    out, packing = textureencode._rebuilt_normal(ag)
    got = out[0, 0, :3].astype(numpy.float64) / 127.5 - 1.0
    check(packing == materials.NORMAL_AG, f"an AG map is recognised ({packing})")
    check(
        numpy.abs(got - n).max() < 0.02 and out[0, 0, 3] == 255,
        f"...and rebuilt to the normal that was packed ({numpy.round(got, 3).tolist()} vs {n.tolist()})",
    )

    rg = numpy.empty_like(rgb)  # BC5: X in red, Y in green, blue pinned black
    rg[..., 0] = rgb[..., 0]
    rg[..., 1] = rgb[..., 1]
    rg[..., 2] = 0
    rg[..., 3] = 255
    out, packing = textureencode._rebuilt_normal(rg)
    got = out[0, 0, :3].astype(numpy.float64) / 127.5 - 1.0
    check(packing == materials.NORMAL_RG, f"an RG map is recognised ({packing})")
    check(numpy.abs(got - n).max() < 0.02, "...and rebuilt with Z reconstructed")


def _check_plan_reads_the_rebuilt_map():
    """B2: `plan_canonical` takes the bake's rebuilt map where the bake made one."""
    from cs2beamng.beamng import textureplan

    # the plan reads the bake's rebuilt map by the material's own `derived.normalRgb`
    with tempfile.TemporaryDirectory() as tmp:
        table = {
            "materials": {
                "m": {
                    "baseColor": "textures/a.png",
                    "normal": "textures/n.png",
                    "derived": {"normalRgb": "materials/n_normal_rgb.png"},
                },
                "plain": {"baseColor": "textures/a.png", "normal": "textures/n.png", "derived": {}},
            },
            "normalsRebuilt": {
                "textures/n.png": {"packing": "ag", "path": "materials/n_normal_rgb.png"}
            },
        }
        plans = textureplan.plan_canonical(tmp, os.path.join(tmp, "derived"), table, ["m", "plain"])
        src = plans["m"]["normal"][0]
        check(
            src == os.path.join(tmp, "derived", "materials/n_normal_rgb.png"),
            "a material whose normal the bake rebuilt plans the derived RGB map",
            src,
        )
        src = plans["plain"]["normal"][0]
        check(
            src == os.path.join(tmp, "textures/n.png"), "one it did not plans the bundle's map", src
        )


def _check_mip_chain():
    """C6: an sRGB map mips in linear light, a normal map mips renormalised."""
    # C6: the mip chain. A black/white checker's first sRGB mip is linear half-grey (code 188,
    # not the naive 128); a tilted normal's first mip stays unit length.
    from cs2beamng.beamng import dds as dds_mod

    checker = numpy.zeros((8, 8, 4), dtype=numpy.uint8)
    checker[::2, ::2] = checker[1::2, 1::2] = 255
    checker[..., 3] = 255
    mip = dds_mod._reduce2_srgb(checker)
    check(
        abs(int(mip[0, 0, 0]) - 188) <= 1,
        f"an sRGB checker mips to linear half-grey (code {int(mip[0, 0, 0])}, not 128)",
    )
    check(int(mip[0, 0, 3]) == 255, "...and its alpha stays a plain average")
    steep, _n = _tilted(8)
    flat = numpy.full((8, 8, 4), 255, dtype=numpy.uint8)
    flat[..., 0] = flat[..., 1] = 128
    flat[..., 2] = 255
    mixed = steep.copy()
    mixed[::2, ::2] = flat[::2, ::2]
    mip = dds_mod._reduce2_normal(mixed)
    xy = mip[0, 0, :2].astype(numpy.float64) / 127.5 - 1.0
    z2 = 1.0 - xy[0] ** 2 - xy[1] ** 2
    check(z2 > -1e-6, "a normal mip is renormalised - x^2 + y^2 never exceeds 1")


def main():
    _check_rebuilt_normals()
    _check_plan_reads_the_rebuilt_map()
    _check_mip_chain()
    print("test_texture_normals: " + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py)."""
    assert main() == 0
