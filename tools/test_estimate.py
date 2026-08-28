"""E4 gate — the cost estimate: is the texture half arithmetic, is the mesh half selective, and
does every figure say where it came from?

Run: `python tools/test_estimate.py`  (needs `pip install -e .`; a couple of seconds).

Three tiers, because the claims are of three different kinds:

* **Arithmetic** — `dds.encoded_bytes` against what `dds.encode` actually writes, and
  `materials.capped_size` against what `_cap` actually produces. These need no bundle and no
  build; if they hold, the texture half is the exporter's own sizing rather than a model of it.
* **Selection** — `meshindex.select` over a hand-built index: a kind switch drops its rows, the
  density slider is decided by the recorded rank, and a region by the recorded cells.
* **Against a real build** — moved to `tools/test_estimate_build.py` (refactor Phase 4): when
  a built level with a report is on this machine, the whole estimate is scored against the
  bytes the validator measured off it.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")

FAILS = []
CHECKS = 0
SKIPPED = []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def section(title):
    print(f"\n== {title}")


# -- 1. the arithmetic ---------------------------------------------------------------------------


def test_dds_arithmetic():
    from cs2beamng.beamng import dds

    section("DDS sizing is arithmetic, not a measurement")
    sizes = [(1, 1), (4, 4), (7, 5), (256, 256), (512, 256), (1024, 1024), (2048, 1024)]
    formats = [dds.BC7_UNORM_SRGB, dds.BC7_UNORM, dds.BC5_UNORM, dds.BC4_UNORM]
    # the walk itself, independent of the encoder: blocks per mip x block bytes + the header
    for width, height in sizes:
        for fmt in formats:
            want = 148
            w, h = width, height
            while True:
                want += ((w + 3) // 4) * ((h + 3) // 4) * dds._BLOCK_BYTES[fmt]
                if w == 1 and h == 1:
                    break
                w, h = max(1, w // 2), max(1, h // 2)
            got = dds.encoded_bytes(width, height, fmt)
            if got != want:
                check(False, f"encoded_bytes({width}x{height}, {fmt}) = {got}, want {want}")
                return
    check(
        True,
        f"encoded_bytes matches the mip walk over {len(sizes) * len(formats)} (size, format) pairs",
    )

    if not dds.available():
        SKIPPED.append("encoded_bytes against a real encode (etcpak is not installed)")
        print("  skip  against a real encode: etcpak is not installed")
        return
    import numpy

    rng = numpy.random.default_rng(7)
    exact = True
    for width, height in [(4, 4), (64, 32), (256, 256)]:
        arr = rng.integers(0, 255, (height, width, 4), dtype=numpy.uint8)
        for fmt in formats:
            written = len(dds.encode(arr, fmt))
            if written != dds.encoded_bytes(width, height, fmt):
                exact = False
                check(
                    False,
                    f"encode({width}x{height}, {fmt}) wrote {written}, "
                    f"encoded_bytes said {dds.encoded_bytes(width, height, fmt)}",
                )
    check(exact, "encoded_bytes is byte-for-byte what encode() writes (12 real encodes)")


def test_cap_arithmetic():
    import numpy
    from cs2beamng.beamng import textureplan

    section("the size cap is arithmetic")
    ok = True
    for width, height in [(2048, 2048), (1024, 512), (300, 700), (64, 64), (5, 5)]:
        for cap in (0, 128, 256, 512, 1024, 2048):
            arr = numpy.zeros((height, width, 4), dtype=numpy.uint8)
            capped = textureplan._cap(arr, cap)
            want = (capped.shape[1], capped.shape[0])
            got = textureplan.capped_size(width, height, cap)
            if got != want:
                ok = False
                check(False, f"capped_size({width}x{height}, cap {cap}) = {got}, _cap gives {want}")
    check(ok, "capped_size is what _cap produces, over 30 (size, cap) pairs")


def test_formats_are_covered():
    from cs2beamng.beamng import dds, textureplan
    from cs2beamng.beamng import materials as bm

    section("every exported kind has a format")
    ok = True
    for kind in bm.KIND_CAP_SETTING:
        for grey in (False, True):
            for keep_alpha in (False, True):
                fmt = textureplan.dxgi_format(kind, keep_alpha, grey)
                if fmt not in dds._BLOCK_BYTES:
                    ok = False
                    check(False, f"dxgi_format({kind}, alpha={keep_alpha}, grey={grey}) = {fmt}")
    check(ok, "dxgi_format returns a known format for every kind, greyness and alpha")
    # the one case a flat table gets wrong: an RGB data map is BC7, not BC4
    check(textureplan.dxgi_format("data", False, True) == dds.BC4_UNORM, "a grey data map is BC4")
    check(
        textureplan.dxgi_format("data", False, False) == dds.BC7_UNORM,
        "an RGB data map is BC7 (data_rgb)",
    )
    check(
        textureplan.dxgi_format("opacity", False, False) == dds.BC4_UNORM,
        "an opacity map is always BC4",
    )
    check(
        textureplan.dxgi_format("colormask", False, True) == dds.BC7_UNORM,
        "a colour mask is always BC7",
    )


# -- 2. the selection ------------------------------------------------------------------------------


def _index_doc(meshindex):
    """Four shipped shapes spanning the kinds, ranks and cells every slider position needs."""
    return {
        "schema": meshindex.SCHEMA,
        "cellMetres": meshindex.CELL_METRES,
        "shipped": {
            "House": {
                "kind": "building",
                "count": 10,
                "rank": 0.0,
                "bytes": 1000,
                "lodBytes": 100,
                "cells": ["0,0"],
            },
            "Bench": {
                "kind": "prop",
                "count": 4,
                "rank": 0.30,
                "bytes": 200,
                "lodBytes": 20,
                "cells": ["0,0", "4,4"],
            },
            "RarePot": {
                "kind": "prop",
                "count": 1,
                "rank": 0.90,
                "bytes": 50,
                "lodBytes": 5,
                "cells": ["4,4"],
            },
            "Oak": {
                "kind": "tree",
                "count": 900,
                "rank": 0.01,
                "bytes": 400,
                "lodBytes": 40,
                "cells": ["0,0"],
            },
        },
    }


def test_index_selection():
    from cs2beamng.pipeline import meshindex

    section("the mesh index answers every slider position")
    doc = _index_doc(meshindex)
    base = {
        "trees": True,
        "props": True,
        "buildings": True,
        "foliage_density": 100,
        "prop_density": 100,
        "generate_lods": True,
        "region": None,
    }

    got = meshindex.select(doc, base)
    check(sorted(got) == ["building", "prop", "tree"], "everything on ships all three kinds")
    check(got["building"]["bytes"] == 1100, "a shape's bytes are its source plus its LOD levels")

    off = meshindex.select(doc, dict(base, generate_lods=False))
    check(off["building"]["bytes"] == 1000, "generate_lods off drops the LOD bytes")

    no_buildings = meshindex.select(doc, dict(base, buildings=False))
    check("building" not in no_buildings, "the kind switch drops the whole kind")

    thin = meshindex.select(doc, dict(base, prop_density=50))
    check(thin["prop"]["shapes"] == 1, "a prop whose first instance ranks 0.90 is gone at 50 %")
    thin = meshindex.select(doc, dict(base, prop_density=20))
    check("prop" not in thin, "at 20 % neither prop survives its rank")
    check(
        meshindex.select(doc, dict(base, foliage_density=0)).get("tree") is None,
        "density 0 places no trees at all",
    )

    near = meshindex.select(doc, dict(base, region="100 100 300"))
    check(sorted(near) == ["building", "prop", "tree"], "a region at the origin keeps cell 0,0")
    check(near["prop"]["shapes"] == 1, "...and drops the prop that is only in a far cell")
    far = meshindex.select(doc, dict(base, region="2100 2100 200"))
    check(sorted(far) == ["prop"], "a region over cell 4,4 keeps only what has an instance there")


def test_density_rank_is_the_same_rule():
    from csiibundle import instances

    section("density_rank is density_keep from the other end")
    ok = True
    for ident in range(400):
        record = {
            "id": ident,
            "attrs": {"treeState": ["Adult", "Teen", "Child", "Stump"][ident % 4]},
        }
        rank = instances.density_rank(record)
        for fraction in (0.05, 0.25, 0.5, 0.75, 0.99):
            if instances.density_keep(record, fraction) != (rank < fraction):
                ok = False
    check(ok, "rank < fraction is exactly density_keep, over 400 records x 5 fractions")


# -- 3. the shape of the answer ---------------------------------------------------------------------


def test_waiting_states():
    from cs2beamng.pipeline import (  # noqa: F401
        estimate,
        estimatebase,
        estimatescore,
        estimatetextures,
    )

    section("an estimate it cannot make says what it is waiting for")
    out = estimate.take({"bundle": os.path.join(REPO, "nowhere")})
    check(
        out["ready"] is False and out["waiting"] == "no bundle", "no bundle -> waiting 'no bundle'"
    )
    check(out["rows"] == [], "...and no rows, rather than an empty bar")
    # a bundle-shaped folder of its own, so a leftover `derived/` in the example bundle cannot
    # decide what this asserts
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "bundle.json"), "w", encoding="utf-8") as handle:
            json.dump({"schema": 2, "mapSize": 14336}, handle)
        out = estimate.take({"bundle": tmp})
        check(out["waiting"] == "bake", "a bundle with no bake -> waiting 'bake'")


def test_learn_floor():
    from cs2beamng.pipeline import (  # noqa: F401
        estimate,
        estimatebase,
        estimatescore,
        estimatetextures,
    )

    section("a level too small to teach anything is not learned from")
    tiny = {"meshMiB": 0.2, "textureShippedMiB": 0.1, "totalMiB": 0.3}
    check(
        estimatescore.score(
            {"meshes": {"bytes": 1}, "textures": {"bytes": 1}, "residentBytes": 2}, tiny
        )
        is None,
        f"a {tiny['totalMiB']} MiB level is under the {estimatescore.LEARN_FLOOR_MIB:.0f} MiB floor "
        f"and is not scored",
    )


def test_history_sizes():
    from cs2beamng.pipeline import history

    section("the size coefficients ship measured and blend")
    rates = history.load()
    for kind in ("building", "prop", "tree", "networks", "areas", "markings", "skirts"):
        check(f"size/cdae_per_source_{kind}" in rates, f"a shipped ratio for {kind}")
    check(
        rates.get("size/ground_texture_per_source", 0) > 1.0,
        "a shipped ratio for the ground textures",
    )


# -- 4. against a real build ------------------------------------------------------------------------


def main():
    test_dds_arithmetic()
    test_cap_arithmetic()
    test_formats_are_covered()
    test_index_selection()
    test_density_rank_is_the_same_rule()
    test_waiting_states()
    test_learn_floor()
    test_history_sizes()

    print()
    for note in SKIPPED:
        print(f"SKIPPED: {note}")
    if FAILS:
        print(f"\n{len(FAILS)} FAILED of {CHECKS}:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"\n{CHECKS}/{CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_estimate.py`."""
    assert main() in (None, 0)
