"""cs2beamng A0 gate, part 2 — the incremental build (A4).

Run: `python tools/test_app_incremental.py`. Split from `test_app_shell.py` (Phase 4); the
shell proper lives there and the delivery features in `test_app_features.py`.

Stage declarations (the static half of `pipeline/state.py`'s guard), the cook's fingerprint
and ladder index, and the cold / no-op / one-setting-changed rebuild of the example bundle
end to end. The shared-state codec rides in `test_app_features.py`.
"""

import inspect
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from shellcommon import EXAMPLE, check, run_gate  # noqa: E402


def _stage_modules():
    """`{stageKey: (stage, [modules])}` — every `cs2beamng` module a stage's `run` can reach.

    The transitive closure matters: `shapes` and `instances` read `trees`, `props`, `buildings`
    and the two density sliders only through `beamng.placement.admit`, so a scan of the stage
    file alone would pass a stage that is missing five of its declarations.
    """
    import importlib
    import types

    from cs2beamng.stages import all_stages

    # The stub-stage hook has to come off to see the real ones, and go back on afterwards: the
    # runner and server checks that follow depend on it (`all_stages` caches per override, so
    # restoring the variable restores the stubs).
    stub = os.environ.pop("CS2BEAMNG_STAGE_MODULE", None)
    try:
        stages = all_stages()
    finally:
        if stub is not None:
            os.environ["CS2BEAMNG_STAGE_MODULE"] = stub

    out = {}
    for stage in stages:
        root = importlib.import_module(stage.run.__module__)
        reached, pending = {root}, [root]
        while pending:
            for value in list(vars(pending.pop()).values()):
                name = (
                    value.__name__
                    if isinstance(value, types.ModuleType)
                    else getattr(value, "__module__", None)
                )
                if not isinstance(name, str) or not name.startswith("cs2beamng."):
                    continue
                # `cs2beamng.settings` DEFINES the schema (its source names every key once) and
                # the `cs2beamng.stages` package namespace holds every stage module as an
                # attribute once they are imported, so following either makes every stage look
                # like it reads everything. A setting reached through ANOTHER STAGE's helper is
                # therefore outside this scan — the runtime recorder in pipeline/state.py is
                # what catches that case, and it is fail-closed.
                if name in ("cs2beamng.settings", "cs2beamng.stages") or (
                    name.startswith("cs2beamng.stages.") and name != root.__name__
                ):
                    continue
                child = sys.modules.get(name)
                if child is not None and child not in reached:
                    reached.add(child)
                    pending.append(child)
        out[stage.key] = (stage, sorted(reached, key=lambda m: m.__name__))
    return out


def test_stage_declarations():
    """Every setting a stage's own source (or a `cs2beamng` helper it imports) reads by name must
    be in that stage's `settings_keys`.

    This is the static half of the guard `pipeline/state.py` describes. The failure it exists for
    is a 25-minute build on 2026-08-18: `paint_kinds` was read but not declared, the bake logged
    `fresh, skipped`, and 448k props shipped the previous build's untinted albedo with no error
    anywhere. Same shape one level up now that stages skip too, and the runtime recorder alone
    would only catch it AFTER a build had already run with the wrong declaration.
    """

    from cs2beamng import settings as settings_mod
    from cs2beamng.pipeline import state as state_mod

    print("stage declarations")
    reads = re.compile(r'settings(?:\.get\(\s*"([a-z_0-9]+)"|\[\s*"([a-z_0-9]+)"\s*\])')
    for key, (stage, modules) in sorted(_stage_modules().items()):
        if not state_mod.incremental(stage):
            continue  # not skippable: it always runs, so nothing to declare
        found = set()
        for module in modules:
            try:
                source = inspect.getsource(module)
            except (OSError, TypeError):
                continue
            found |= {a or b for a, b in reads.findall(source)}
        # The exemption list is `state.NOT_FIDELITY_SETTINGS` itself, not a copy: this gate and
        # the runtime recorder must agree, and the one time they did not, the materials stage
        # could never be recorded (it reads `level_name` to name a paint-variant material).
        undeclared = sorted(
            (found & set(settings_mod.SCHEMA))
            - set(stage.settings_keys)
            - state_mod.NOT_FIDELITY_SETTINGS
        )
        check(
            not undeclared,
            f"stage {key!r} declares every setting it reads"
            + (f" - reads {undeclared}" if undeclared else ""),
        )


def test_stage_fields():
    """A stage's declared inputs and settings have to be well formed, or a signature raises
    mid-build."""
    from cs2beamng import settings as settings_mod

    print("stage fields")
    for key, (stage, _mods) in sorted(_stage_modules().items()):
        bad = [
            ref
            for ref in (stage.inputs or ())
            if not (
                ref.startswith("layer:")
                or ref.startswith("derived:")
                or ref in ("level:", "shapes:")
            )
        ]
        check(
            not bad,
            f"stage {key!r} inputs are all layer:/derived:/level:/shapes: refs"
            + (f" - {bad}" if bad else ""),
        )
        unknown = [k for k in stage.settings_keys if k not in settings_mod.SCHEMA]
        check(
            not unknown,
            f"stage {key!r} declares only real settings" + (f" - {unknown}" if unknown else ""),
        )


def _write_dae(shapes, stem, tris):
    with open(os.path.join(shapes, stem + ".dae"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n<COLLADA>\n  <library_geometries>\n'
            f'    <geometry id="{stem}-mesh">\n      <triangles count="{tris}"/>\n'
            "    </geometry>\n  </library_geometries>\n  <library_visual_scenes>\n"
            f'    <node id="{stem}_a100" name="{stem}_a100" type="NODE">\n'
            f'      <instance_geometry url="#{stem}-mesh"/>\n'
            "    </node>\n  </library_visual_scenes>\n</COLLADA>\n"
        )


def _cook_level(root):
    """A scratch level with two shapes, each a `.dae` and a cooked `.cdae`."""
    level = os.path.join(root, "cooklevel")
    shapes = os.path.join(level, "art", "shapes", "cs2")
    os.makedirs(shapes, exist_ok=True)
    for stem, tris in (("a", 10), ("b", 20)):
        _write_dae(shapes, stem, tris)
        with open(os.path.join(shapes, stem + ".cdae"), "wb") as handle:
            handle.write(b"cooked")
    return level, shapes


def test_cook_fingerprint(tmp):
    """The `shapes:` fingerprint must move for a rewritten `.dae` and a DELETED `.cdae` (a user
    who cleared the cooked shapes has to get a recook, not a level that ships raw XML), and
    stay still for anything else in the level."""
    from cs2beamng.pipeline import state as state_mod

    print("cook state: the shapes fingerprint")
    level, shapes = _cook_level(os.path.join(tmp, "fp"))
    base = state_mod.shapes_fingerprint(level)
    with open(os.path.join(level, "info.json"), "w", encoding="utf-8") as handle:
        handle.write("{}")
    check(
        state_mod.shapes_fingerprint(level) == base,
        "a write outside art/shapes/ does not move the fingerprint",
    )
    os.remove(os.path.join(shapes, "b.cdae"))
    check(
        state_mod.shapes_fingerprint(level) != base,
        "deleting a .cdae moves it, so a cleared level is recooked",
    )


def test_cook_ladder_index(tmp):
    """The ladder index must carry over the shapes this cook is not compiling (re-reading them
    cost 4.4 GiB of XML per build) and keep the entry of a shape whose source a `keep_dae:
    false` build removed — the one case the index exists for, and one a rewrite emptied."""
    from cs2beamng.beamng import budget as budget_mod
    from cs2beamng.stages import cook as cook_mod

    print("cook state: the ladder index")
    level, shapes = _cook_level(os.path.join(tmp, "ladder"))
    count, rescanned = cook_mod._write_ladder_index(
        level,
        os.path.join(level, "art", "shapes"),
        rescan=[os.path.join(shapes, "a.dae"), os.path.join(shapes, "b.dae")],
    )
    check((count, rescanned) == (2, 2), f"a cold index scans every shape ({count}, {rescanned})")
    ladder = os.path.join(level, budget_mod.LADDER_INDEX)
    with open(ladder, encoding="utf-8") as handle:
        indexed = json.load(handle)["shapes"]
    check(
        set(indexed) == {"art/shapes/cs2/a.dae", "art/shapes/cs2/b.dae"},
        f"both shapes indexed ({sorted(indexed)})",
    )
    check(
        indexed["art/shapes/cs2/a.dae"]["levels"] == {"100": 10},
        "the ladder is read out of the .dae",
    )

    _write_dae(shapes, "b", 30)  # only this one moved: only this one is re-read
    count, rescanned = cook_mod._write_ladder_index(
        level, os.path.join(level, "art", "shapes"), rescan=[os.path.join(shapes, "b.dae")]
    )
    check(
        (count, rescanned) == (2, 1),
        f"only the compiled shape is re-scanned ({count}, {rescanned})",
    )
    with open(ladder, encoding="utf-8") as handle:
        indexed = json.load(handle)["shapes"]
    check(
        indexed["art/shapes/cs2/b.dae"]["levels"] == {"100": 30},
        "the re-scanned shape carries its new numbers",
    )

    os.remove(os.path.join(shapes, "a.dae"))  # what `keep_dae: false` leaves behind
    count, rescanned = cook_mod._write_ladder_index(
        level, os.path.join(level, "art", "shapes"), rescan=()
    )
    with open(ladder, encoding="utf-8") as handle:
        indexed = json.load(handle)["shapes"]
    check(
        "art/shapes/cs2/a.dae" in indexed and rescanned == 0,
        f"a dropped .dae keeps its entry while the .cdae is there ({sorted(indexed)}, rescanned {rescanned})",
    )
    os.remove(os.path.join(shapes, "a.cdae"))
    cook_mod._write_ladder_index(level, os.path.join(level, "art", "shapes"), rescan=())
    with open(ladder, encoding="utf-8") as handle:
        indexed = json.load(handle)["shapes"]
    check(
        "art/shapes/cs2/a.dae" not in indexed,
        "a shape that is gone from the level leaves the index",
    )


def _level_objects(level_dir):
    from cs2beamng.beamng.level import read_objects

    return sorted(
        (rel, json.dumps(obj, sort_keys=True)) for rel, _n, obj in read_objects(level_dir) if obj
    )


def _mtime(root, rel):
    try:
        return os.stat(os.path.join(root, rel)).st_mtime_ns
    except OSError:
        return None


def test_incremental(tmp):
    """Build the example bundle three times: cold, no-op, and with one setting changed. The
    no-op must skip every stage AND leave the level byte-identical; the third must re-run only
    the stages that declare the setting (and whatever declares `after` on them)."""
    print("incremental rebuild (real stages)")
    os.environ.pop("CS2BEAMNG_STAGE_MODULE", None)
    from cs2beamng.cli import main as cli_main
    from cs2beamng.pipeline import state as state_mod

    out = os.path.join(tmp, "levels", "inc_example")
    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1"])
    check(code == 0, f"cold build succeeds ({code})")
    first_objects = _level_objects(out)
    dae_stamps = {}
    for root, _dirs, files in os.walk(os.path.join(out, "art", "shapes")):
        for name in files:
            full = os.path.join(root, name)
            dae_stamps[os.path.relpath(full, out)] = os.stat(full).st_mtime_ns
    check(os.path.isfile(state_mod.state_path(out)), "cold build writes level_state.json")
    # `cook` is incremental now, but this level is not under a BeamNG userfolder so the stage
    # bails — and a bail-out is `_norecord`. Recording it would skip the cook for good on the day
    # the level moves somewhere the compiler can see, because nothing it declares would move.
    check(
        "cook" not in (state_mod.load(out) or {}).get("stages", {}),
        "a cook that bailed for an environmental reason records no state",
    )

    _noop_rebuild(cli_main, out, first_objects, dae_stamps)
    _setting_rebuild(cli_main, out, first_objects)
    _forced_rebuild(cli_main, out)


def _noop_rebuild(cli_main, out, first_objects, dae_stamps):
    """The no-op rebuild: every incremental stage skipped, the level byte-identical."""
    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1"])
    check(code == 0, f"no-op rebuild succeeds ({code})")
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    ran = sorted(k for k, v in report["stages"].items() if not v.get("skipped"))
    check(
        set(ran) <= {"bake", "cook"},
        f"no-op rebuild skips every incremental stage, the validator included (ran {ran})",
    )
    check(
        _level_objects(out) == first_objects, "a skipped stage's objects are replayed identically"
    )
    unchanged = [rel for rel, stamp in dae_stamps.items() if _mtime(out, rel) == stamp]
    check(
        len(unchanged) == len(dae_stamps),
        f"every .dae keeps its mtime, so BeamNG keeps its .cdae ({len(unchanged)}/{len(dae_stamps)})",
    )


def _setting_rebuild(cli_main, out, first_objects):
    """One changed setting re-runs exactly the stages that declare it (and their `after`s)."""
    code = cli_main(
        ["build", EXAMPLE, "--out", out, "--set", "jobs=1", "--set", "lod_detail_scale=9.0"]
    )
    check(code == 0, f"rebuild with a changed setting succeeds ({code})")
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    ran = sorted(k for k, v in report["stages"].items() if not v.get("skipped"))
    check("shapes" in ran, f"the stage that declares lod_detail_scale re-runs (ran {ran})")
    check("instances" in ran, "a stage that declares `after` on it re-runs too")
    check("materials" in ran, "so does the materials stage, which exports what the shapes bound")
    check("terrain" not in ran, "a stage that does not read it stays skipped")
    check("validate" in ran, "the validator re-runs because the level folder moved")
    check(
        len(_level_objects(out)) == len(first_objects),
        "the object tree is still complete after a partial rebuild",
    )


def _forced_rebuild(cli_main, out):
    """`--force` runs everything, and nothing read an undeclared setting doing it."""
    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1", "--force"])
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    check(
        code == 0 and not any(v.get("skipped") for v in report["stages"].values()),
        "--force runs every stage",
    )
    check(
        not report.get("stateWarnings"),
        f"no stage read an undeclared setting {report.get('stateWarnings')}",
    )


def _body(tmp):
    test_stage_declarations()
    test_stage_fields()
    test_cook_fingerprint(tmp)
    test_cook_ladder_index(tmp)
    test_incremental(tmp)


def main():
    return run_gate(_body)


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_app_incremental.py`."""
    assert main() in (None, 0)
