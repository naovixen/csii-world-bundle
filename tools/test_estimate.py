"""E4 gate — the cost estimate: is the texture half arithmetic, is the mesh half selective, and
does every figure say where it came from?

Run: `python tools/test_estimate.py`  (needs `pip install -e .`; a couple of seconds).

Three tiers, because the claims are of three different kinds:

* **Arithmetic** — `dds.encoded_bytes` against what `dds.encode` actually writes, and
  `materials.capped_size` against what `_cap` actually produces. These need no bundle and no
  build; if they hold, the texture half is the exporter's own sizing rather than a model of it.
* **Selection** — `meshindex.select` over a hand-built index: a kind switch drops its rows, the
  density slider is decided by the recorded rank, and a region by the recorded cells.
* **Against a real build** — when a built level with a report is on this machine, the whole
  estimate is scored against the bytes the validator measured off it. This is the gate the plan
  asks for and it cannot run in CI, so it says loudly when it is skipped.

`CS2BEAMNG_LEVEL=<dir>` points the third tier at a particular level; without it the newest
level under the detected levels folder that carries a report is used.
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
    from cs2beamng.beamng import materials as bm

    section("the size cap is arithmetic")
    ok = True
    for width, height in [(2048, 2048), (1024, 512), (300, 700), (64, 64), (5, 5)]:
        for cap in (0, 128, 256, 512, 1024, 2048):
            arr = numpy.zeros((height, width, 4), dtype=numpy.uint8)
            capped = bm._cap(arr, cap)
            want = (capped.shape[1], capped.shape[0])
            got = bm.capped_size(width, height, cap)
            if got != want:
                ok = False
                check(False, f"capped_size({width}x{height}, cap {cap}) = {got}, _cap gives {want}")
    check(ok, "capped_size is what _cap produces, over 30 (size, cap) pairs")


def test_formats_are_covered():
    from cs2beamng.beamng import dds
    from cs2beamng.beamng import materials as bm

    section("every exported kind has a format")
    ok = True
    for kind in bm.KIND_CAP_SETTING:
        for grey in (False, True):
            for keep_alpha in (False, True):
                fmt = bm.dxgi_format(kind, keep_alpha, grey)
                if fmt not in dds._BLOCK_BYTES:
                    ok = False
                    check(False, f"dxgi_format({kind}, alpha={keep_alpha}, grey={grey}) = {fmt}")
    check(ok, "dxgi_format returns a known format for every kind, greyness and alpha")
    # the one case a flat table gets wrong: an RGB data map is BC7, not BC4
    check(bm.dxgi_format("data", False, True) == dds.BC4_UNORM, "a grey data map is BC4")
    check(
        bm.dxgi_format("data", False, False) == dds.BC7_UNORM, "an RGB data map is BC7 (data_rgb)"
    )
    check(bm.dxgi_format("opacity", False, False) == dds.BC4_UNORM, "an opacity map is always BC4")
    check(bm.dxgi_format("colormask", False, True) == dds.BC7_UNORM, "a colour mask is always BC7")


# -- 2. the selection ------------------------------------------------------------------------------


def test_index_selection():
    from cs2beamng.pipeline import meshindex

    section("the mesh index answers every slider position")
    doc = {
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
    from cs2beamng.pipeline import estimate

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
    from cs2beamng.pipeline import estimate

    section("a level too small to teach anything is not learned from")
    tiny = {"meshMiB": 0.2, "textureShippedMiB": 0.1, "totalMiB": 0.3}
    check(
        estimate.score({"meshes": {"bytes": 1}, "textures": {"bytes": 1}, "residentBytes": 2}, tiny)
        is None,
        f"a {tiny['totalMiB']} MiB level is under the {estimate.LEARN_FLOOR_MIB:.0f} MiB floor "
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


def find_level():
    """A built level with a report that measured its residency, or None."""
    from cs2beamng import settings as settings_mod

    explicit = os.environ.get("CS2BEAMNG_LEVEL")
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    root = settings_mod.default_levels_dir()
    best = None
    if not os.path.isdir(root):
        return None
    for name in os.listdir(root):
        path = os.path.join(root, name, "cs2beamng_report.json")
        if not os.path.isfile(path):
            continue
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not ((doc.get("validation") or {}).get("checks") or {}).get("resident"):
            continue
        if best is None or os.path.getmtime(path) > os.path.getmtime(best[1]):
            best = (os.path.join(root, name), path)
    return best[0] if best else None


def test_against_a_real_build():
    from cs2beamng.pipeline import census as census_mod
    from cs2beamng.pipeline import estimate, meshindex

    section("against a level that was actually built")
    level = find_level()
    if level is None:
        SKIPPED.append("the whole real-build tier: no built level with a report on this machine")
        print("  skip  no built level with a report; the estimate is untested against bytes")
        return
    report = json.load(open(os.path.join(level, "cs2beamng_report.json"), encoding="utf-8"))
    measured = ((report.get("validation") or {}).get("checks") or {}).get("resident")
    settings_path = os.path.join(level, "cs2beamng_settings.json")
    if not measured or not os.path.isfile(settings_path):
        SKIPPED.append(
            f"the real-build tier: {os.path.basename(level)} predates the residency "
            f"measurement or the settings it was built with"
        )
        print("  skip  that level's report has no residency measurement to score against")
        return
    values = json.load(open(settings_path, encoding="utf-8"))["settings"]
    print(f"  ..    {os.path.basename(level)}, built {report.get('finishedAt')}")

    bundle = values.get("bundle")
    if not bundle or not os.path.isfile(os.path.join(bundle, "derived", "materials.json")):
        SKIPPED.append("the real-build tier: the bundle that built it is gone")
        print("  skip  the bundle that built it is not on this machine any more")
        return
    index = meshindex.load(bundle)
    if index is None:
        print("  ..    building the mesh index (one pass over the instance layer)")
        index = meshindex.build(bundle)
    est = estimate.take(values, census_mod.take(values), index)
    check(est["ready"], "the estimate is ready for the settings that built it")

    # THE TEXTURE HALF, FILE BY FILE. The claim is that this is the exporter's own sizing, so
    # the gate is every file's bytes and not a total that could be right by cancellation.
    # THE BAKE ON DISK HAS TO BE THE ONE THAT BUILT THIS LEVEL. Both halves of the estimate are
    # sized from what the bake produced — the texture plan from its material table, the shape
    # count from its colour centres — so a bake made under other recolouring settings describes a
    # different level than the one on disk. That is not the estimate being wrong; it is
    # `_stale_bake_note`, which the meter declares on screen. Scoring across it would be scoring
    # something the estimate never claimed.
    baked = (
        (estimate._load_json(os.path.join(bundle, "derived", "derived.json")) or {}).get("options")
        or {}
    ).get("paint_kinds")
    wanted = [k.strip() for k in str(values.get("paint_kinds") or "").split(",") if k.strip()]
    stale_bake = baked is not None and list(baked) != wanted
    if stale_bake:
        print(
            f"  ..    the bake on disk recolours {baked}, this level was built with "
            f"{wanted or 'none'} — the byte totals are not comparable"
        )

    exact, compared, worst, unnamed = _texture_file_check(values, bundle, level)
    if compared:
        check(
            exact == compared,
            f"every one of {compared:,} exported textures is sized to the "
            f"byte (worst error {worst:,})",
        )
        # The materials stage sweeps its own orphans, so every file in the folder is one THIS
        # settings combination exported. A file the enumeration cannot name is therefore a hole
        # in it — which is what the refactor onto `plan_canonical` had to be proved free of.
        #
        # ...but only against the bake that built it (see above).
        if stale_bake:
            SKIPPED.append(
                f"the completeness check: this level was built from a bake made with "
                f"paint_kinds {wanted or 'none'}, and the one on disk has {baked}"
            )
            print(f"  skip  {unnamed} files belong to a bake this one is not — not a hole")
        else:
            check(
                unnamed == 0,
                f"the enumeration names every file the build wrote ({unnamed} unnamed)",
            )
    else:
        SKIPPED.append("the per-file texture check: the level ships no DDS")
        print("  skip  the level ships no DDS textures to compare against")

    # WHAT THIS TIER DOES AND DOES NOT PROVE. The texture check above is a real test: the sizing
    # is arithmetic and the level's own files are the answer. The mesh tolerance is NOT the same
    # kind of thing - the ratios are CALIBRATED, and on the build they were fitted to they
    # cannot miss. It is here as a REGRESSION gate: it catches a broken selection, a lost group
    # or a coefficient edited by hand. What tracks the mesh half honestly is the score every
    # build prints into its own report, on whatever settings that build used.
    # The two texture claims, separately. The computed half may only run OVER, and only by the
    # materials no shape binds; the calibrated half is a ratio and is allowed to be a ratio.
    shipped = _texture_bytes_by_folder(level)
    material = est["textures"]["materialBytes"] / (1024.0 * 1024.0)
    ground = est["textures"]["groundBytes"] / (1024.0 * 1024.0)
    if shipped["textures"]:
        over = 100.0 * (material - shipped["textures"]) / shipped["textures"]
        check(
            0.0 <= over <= 8.0,
            f"the computed textures are an upper bound and a tight one: {material:,.0f} MiB "
            f"against {shipped['textures']:,.0f} MiB shipped ({over:+.1f} %, the materials no "
            f"shape binds)",
        )
    if shipped["ground"]:
        off = 100.0 * (ground - shipped["ground"]) / shipped["ground"]
        check(
            abs(off) <= 25.0,
            f"the ground textures are within 25 % of their ratio: {ground:,.0f} MiB against "
            f"{shipped['ground']:,.0f} MiB shipped ({off:+.1f} %)",
        )

    score = estimate.score(est, measured)
    if stale_bake:
        SKIPPED.append("the byte totals: the bake on disk is not the one that built this level")
        for name in ("mesh", "texture", "total"):
            row = score[name]
            print(
                f"  skip  {name}: {row['estimatedMiB']:,.0f} MiB estimated against "
                f"{row['actualMiB']:,.0f} MiB measured ({row['errorPercent']:+.1f} %) — "
                f"across a re-bake, so not a score"
            )
    else:
        for name, tolerance in (("mesh", 10.0), ("total", 8.0)):
            row = score[name]
            note = (
                ""
                if name == "texture"
                else (
                    " (a tolerance on a calibrated figure; the "
                    "running score is the one in each build's "
                    "own report)"
                )
            )
            check(
                abs(row["errorPercent"]) <= tolerance,
                f"the {name} half is within {tolerance:.0f} %: {row['estimatedMiB']:,.0f} MiB "
                f"estimated against {row['actualMiB']:,.0f} MiB measured "
                f"({row['errorPercent']:+.1f} %){note}",
            )

    # every figure carries a basis, and every approximation says what and why
    check(
        bool(est["textures"]["basis"]) and bool(est["meshes"]["basis"]),
        "both halves state their basis",
    )
    check(all(row.get("basis") for row in est["rows"]), "every row states its basis")
    check(
        all(a.get("what") and a.get("why") for a in est["approximations"]),
        f"all {len(est['approximations'])} approximations say what they approximate and why",
    )
    check(
        est["rows"][0].get("band") in ("fits", "close", "over", "unknown"),
        f"the video memory row lands in a band ({est['rows'][0].get('band')})",
    )


def _texture_bytes_by_folder(level):
    """`{"textures": MiB, "ground": MiB}` — what the level actually ships, split the way the
    estimate splits it: the material textures it computes, and the ground textures it scales."""
    out = {"textures": 0.0, "ground": 0.0}
    for folder, key in (("textures", "textures"), ("terrains", "ground"), ("ground", "ground")):
        root = os.path.join(level, "art", folder)
        for path, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in (".dds", ".png", ".jpg"):
                    out[key] += os.path.getsize(os.path.join(path, name)) / (1024.0 * 1024.0)
    return out


def _texture_file_check(values, bundle, level):
    """`(exact, compared, worstError, unnamed)` over the DDS files the level actually holds."""
    from cs2beamng.beamng import dds as dds_mod
    from cs2beamng.beamng import materials as bm
    from cs2beamng.pipeline import estimate

    plan = estimate._texture_plan(bundle, os.path.join(bundle, "derived"), values)
    caps = bm.caps_from_settings(values)
    # `_texture_plan` keeps dimensions, not names, so the names are rebuilt from the same jobs
    predicted = {}
    for job, sizes in zip(_plan_jobs(bundle, values), plan["jobs"]):
        kind, keep_alpha, width, height, colour = sizes
        width, height = bm.capped_size(width, height, int(caps.get(kind, 0) or 0))
        predicted[bm.texture_name(job[0], kind, keep_alpha, True)] = dds_mod.encoded_bytes(
            width, height, bm.dxgi_format(kind, keep_alpha, colour in (0, 4))
        )
    directory = os.path.join(level, "art", "textures")
    if not os.path.isdir(directory):
        return 0, 0, 0, 0
    exact = compared = worst = unnamed = 0
    for name in os.listdir(directory):
        if not name.endswith(".dds"):
            continue
        want = predicted.get(name)
        if want is None:
            unnamed += 1
            continue
        got = os.path.getsize(os.path.join(directory, name))
        compared += 1
        if got == want:
            exact += 1
        else:
            worst = max(worst, abs(got - want))
    return exact, compared, worst, unnamed


def _plan_jobs(bundle, values):
    """The job tuples in the order `_texture_plan` sized them — it drops the paths, and the
    per-file check needs them back to name the files."""
    from cs2beamng.beamng import materials as bm
    from cs2beamng.pipeline import estimate

    derived_dir = os.path.join(bundle, "derived")
    table = estimate._load_json(os.path.join(derived_dir, "materials.json")) or {}
    canonical = sorted(
        {rec.get("canonical") or name for name, rec in (table.get("materials") or {}).items()}
    )
    jobs = {}
    for plan in bm.plan_canonical(bundle, derived_dir, table, canonical).values():
        for job in plan.values():
            jobs[job] = None
    for job in estimate._variant_jobs(values, derived_dir, table):
        jobs[job] = None
    for job in bm.plan_areas(bundle, derived_dir, has_alpha=estimate._header_has_alpha):
        jobs[job] = None
    return [job for job in jobs if estimate._png_header(job[0]) is not None]


def main():
    test_dds_arithmetic()
    test_cap_arithmetic()
    test_formats_are_covered()
    test_index_selection()
    test_density_rank_is_the_same_rule()
    test_waiting_states()
    test_learn_floor()
    test_history_sizes()
    test_against_a_real_build()

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
