"""E4 gate, real-build tier — the cost estimate scored against a level actually built.

Split from `tools/test_estimate.py` (refactor Phase 4): the arithmetic and selection tiers
stay there and run anywhere; this tier needs a built level with a report on this machine, so
it says loudly when it is skipped rather than passing silently. The texture half is scored
file by file (the sizing is the exporter's own arithmetic); the byte totals are CALIBRATED and
gate only regressions — the honest tracker is the score every build prints into its own report.

`CS2BEAMNG_LEVEL=<dir>` points it at a particular level; without it the newest level under the
detected levels folder that carries a report is used.
"""

import json
import os
import sys

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
    print(f"\n-- {title} --")


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


def _load_real_build():
    """The newest built level with a report and its bundle, or None with the skip said."""
    level = find_level()
    if level is None:
        SKIPPED.append("the whole real-build tier: no built level with a report on this machine")
        print("  skip  no built level with a report; the estimate is untested against bytes")
        return None
    report = json.load(open(os.path.join(level, "cs2beamng_report.json"), encoding="utf-8"))
    measured = ((report.get("validation") or {}).get("checks") or {}).get("resident")
    settings_path = os.path.join(level, "cs2beamng_settings.json")
    if not measured or not os.path.isfile(settings_path):
        SKIPPED.append(
            f"the real-build tier: {os.path.basename(level)} predates the residency "
            f"measurement or the settings it was built with"
        )
        print("  skip  that level's report has no residency measurement to score against")
        return None
    values = json.load(open(settings_path, encoding="utf-8"))["settings"]
    print(f"  ..    {os.path.basename(level)}, built {report.get('finishedAt')}")

    bundle = values.get("bundle")
    if not bundle or not os.path.isfile(os.path.join(bundle, "derived", "materials.json")):
        SKIPPED.append("the real-build tier: the bundle that built it is gone")
        print("  skip  the bundle that built it is not on this machine any more")
        return None
    return level, values, bundle, measured


def test_against_a_real_build():
    from cs2beamng.pipeline import census as census_mod
    from cs2beamng.pipeline import estimate, meshindex

    section("against a level that was actually built")
    loaded = _load_real_build()
    if loaded is None:
        return
    level, values, bundle, measured = loaded
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
    stale_bake, wanted, baked = _stale_bake(estimate, values, bundle)
    _check_texture_half(values, bundle, level, stale_bake, wanted, baked)

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

    _check_score_and_bases(estimate, est, measured, stale_bake)


def _stale_bake(estimate, values, bundle):
    """THE BAKE ON DISK HAS TO BE THE ONE THAT BUILT THIS LEVEL. Both halves of the estimate
    are sized from what the bake produced — the texture plan from its material table, the shape
    count from its colour centres — so a bake made under other recolouring settings describes a
    different level than the one on disk. That is not the estimate being wrong; it is
    `_stale_bake_note`, which the meter declares on screen. Scoring across it would be scoring
    something the estimate never claimed."""
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
    return stale_bake, wanted, baked


def _check_texture_half(values, bundle, level, stale_bake, wanted, baked):
    """THE TEXTURE HALF, FILE BY FILE. The claim is that this is the exporter's own sizing, so
    the gate is every file's bytes and not a total that could be right by cancellation."""
    exact, compared, worst, unnamed = _texture_file_check(values, bundle, level)
    if not compared:
        SKIPPED.append("the per-file texture check: the level ships no DDS")
        print("  skip  the level ships no DDS textures to compare against")
        return
    check(
        exact == compared,
        f"every one of {compared:,} exported textures is sized to the byte (worst error {worst:,})",
    )
    # The materials stage sweeps its own orphans, so every file in the folder is one THIS
    # settings combination exported. A file the enumeration cannot name is therefore a hole
    # in it — which is what the refactor onto `plan_canonical` had to be proved free of.
    #
    # ...but only against the bake that built it (see `_stale_bake`).
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


def _check_score_and_bases(estimate, est, measured, stale_bake):
    """The calibrated byte totals (a regression gate, not a proof) and the stated bases."""
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
    test_against_a_real_build()
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    for what in FAILS:
        print("  FAIL:", what)
    for what in SKIPPED:
        print("  SKIP:", what)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_estimate_build.py`."""
    assert main() in (None, 0)
