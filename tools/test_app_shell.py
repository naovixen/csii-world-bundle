"""cs2beamng A0 gate — the application shell: runner (cancel, hard-cancel, dead worker, failure),
server (token, API, SSE), settings schema/groups, and the headless CLI end-to-end.

Run: `python tools/test_app_shell.py`  (needs `pip install -e .`; ~25 s; no bundle beyond
`docs/example-bundle`, no pywebview). Stub stages come from `tools/cs2beamng_stub_stages.py`
through the `CS2BEAMNG_STAGE_MODULE` hook, so the real bake never runs here except in the final
headless check. Part 1 of the A0 gate: the incremental build is `test_app_incremental.py` and
the delivery features are `test_app_features.py` (split in Phase 4, tally via `shellcommon`).
"""

import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from shellcommon import EXAMPLE, check, run_gate, stub_settings  # noqa: E402

# -- settings ------------------------------------------------------------------------------------


def test_settings():
    from cs2beamng import settings as settings_mod

    print("settings")
    ui = settings_mod.schema_for_ui()
    check(
        [e["key"] for e in ui] == list(settings_mod.SCHEMA),
        "schema_for_ui covers all keys, in declaration order",
    )
    for name, preset in settings_mod.PRESETS.items():
        check(all(k in settings_mod.SCHEMA for k in preset), f"preset {name} sets only known keys")
        for k, v in preset.items():
            try:
                settings_mod.coerce(k, v)
            except ValueError:
                check(False, f"preset {name}: {k}={v!r} coerces")
    check(settings_mod.coerce("texture_max", "1024") == 1024, "numeric choice coerces to int")
    check(settings_mod.coerce("trees", "off") is False, "bool coerces")
    d = settings_mod.detected()
    check(isinstance(d.get("bundles"), list) and "platform" in d, "detected() returns lists")

    # -- D2: the basic panel, and help that fits on one line ------------------------------------
    # The point of the tiering is that the default screen stays small. A "basic" list that grows
    # to thirty keys is the problem it was made to solve, so the ceiling is checked, not just the
    # membership.
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.BASIC),
        "every BASIC key is a real setting",
    )
    check(len(settings_mod.BASIC) == len(set(settings_mod.BASIC)), "BASIC has no duplicates")
    check(
        len(settings_mod.BASIC) <= settings_mod.BASIC_MAX,
        f"BASIC stays under {settings_mod.BASIC_MAX} controls (it has {len(settings_mod.BASIC)})",
    )
    long_help = {k: len(v[2]) for k, v in settings_mod.SCHEMA.items() if len(v[2]) > 120}
    check(not long_help, f"every help string is one sentence (<=120 chars); over: {long_help}")
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.DETAILS),
        "DETAILS only describes real settings",
    )
    check(
        sorted(settings_mod.PRESET_INFO) == sorted(settings_mod.PRESETS),
        "every preset has a description",
    )
    # A fresh install names a preset in `quality_preset` and must actually BE on it, or the app
    # opens claiming a preset it is not on - and, since E2, showing a badge for a setting the
    # user never touched. This found `lod_switch_error_px` at 8.0 against high's 6.0.
    named = settings_mod.SCHEMA["quality_preset"][0]
    shipped = settings_mod.defaults()
    off = {k: (shipped[k], v) for k, v in settings_mod.PRESETS[named].items() if shipped[k] != v}
    check(not off, f"the shipped defaults ARE the {named} preset; off: {off}")
    tiers = {e["key"]: e["tier"] for e in ui}
    check(
        set(tiers) == set(settings_mod.SCHEMA) and set(tiers.values()) <= {"basic", "advanced"},
        "schema_for_ui tiers every key",
    )
    check(all(tiers[k] == "basic" for k in settings_mod.BASIC), "the BASIC keys are tiered basic")
    check(
        all(("detail" in e) == (e["key"] in settings_mod.DETAILS) for e in ui),
        "schema_for_ui ships a detail exactly where DETAILS has one",
    )


# -- E1: the settings metadata the redesigned screen is built from --------------------------------
#
# Three things are checked, and the third is the one that matters. The partition ("every key
# lands in exactly one place") is what keeps a setting from being silently dropped by a layout
# change. The lengths keep a one-line blurb one line. The LANGUAGE check is the gate on the
# actual complaint - the settings read like variable names - and it is mechanical on purpose: a
# rule a person applies by eye is a rule that decays the first time somebody is in a hurry.


def test_bake_stage_cancel():
    """The bake reports a cancelled product as an outcome; the stage must turn that into
    `Cancelled` (a cancel once ended as DONE, with validate running on a half-baked cache)."""
    from collections import namedtuple

    from cs2beamng.stages import BuildContext
    from cs2beamng.stages import bake as bake_stage
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Cancelled, Progress

    print("bake stage: cancelled outcome")
    Outcome = namedtuple("Outcome", "name status reason seconds stats")
    real = bake_runner.run
    bake_runner.run = lambda *a, **k: [
        Outcome("materials", "cancelled", "cancelled by caller", 1.0, None)
    ]
    try:
        ctx = BuildContext(
            settings={"tile_size": 256},
            bundle=EXAMPLE,
            level_dir="",
            derived_dir="",
            census={},
            region=None,
            report={},
            jobs=1,
        )
        try:
            bake_stage.run(ctx, Progress())
            check(False, "cancelled outcome raises Cancelled")
        except Cancelled:
            check(True, "cancelled outcome raises Cancelled")
    finally:
        bake_runner.run = real


# -- runner ---------------------------------------------------------------------------------------


def run_mode(tmp, mode, action=None, timeout=30, act_on_progress=True):
    """Start the stub pipeline in `mode`, optionally run `action(runner)` once the work stage is
    running (and, with `act_on_progress`, has advanced at least once), and return
    (final_state, seconds_from_action_to_end, max_state_call_ms)."""
    from cs2beamng.pipeline.runner import Runner

    runner = Runner()
    runner.start(stub_settings(tmp, mode))
    acted_at = None
    slowest_state_ms = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        t1 = time.monotonic()
        state = runner.state()
        slowest_state_ms = max(slowest_state_ms, (time.monotonic() - t1) * 1000)
        work = next((s for s in state["stages"] if s["key"] == "work"), None)
        ready = (
            work is not None
            and work["status"] == "running"
            and (not act_on_progress or work.get("phaseDone", 0) > 0)
        )
        if action is not None and acted_at is None and ready:
            action(runner)
            acted_at = time.monotonic()
        if state["status"] != "running":
            return state, (time.monotonic() - acted_at) if acted_at else None, slowest_state_ms
        time.sleep(0.05)
    return runner.state(), None, slowest_state_ms


def test_runner(tmp):
    from cs2beamng.pipeline import runner as runner_mod

    print("runner: ok")
    state, _, slow = run_mode(tmp, "ok")
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "done", f"status done ({state['status']})")
    check(by["skipme"]["status"] == "skipped", "disabled stage reported skipped")
    check(
        all(by[k]["status"] == "done" for k in ("prep", "work", "wrap")), "all enabled stages done"
    )
    check(
        state.get("report") and "work" in state["report"]["stages"],
        "report carries the stage results",
    )
    check(
        any("hello from the stub" in e["text"] for e in state["log"]),
        "stage log reached the runner",
    )
    check(state["overall"]["fraction"] == 1.0, "overall fraction 1.0 when done")
    check(slow < 100, f"state() never blocked (slowest {slow:.1f} ms)")

    print("runner: cancel mid-stage")
    state, latency, _ = run_mode(tmp, "slow", action=lambda r: r.cancel())
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "cancelled", f"status cancelled ({state['status']})")
    check(
        by["work"]["status"] == "cancelled" and by["wrap"]["status"] == "pending",
        "work cancelled, wrap never ran",
    )
    check(latency is not None and latency < 1.0, f"cancel returned within 1 s ({latency})")

    print("runner: worker ignores cancel -> hard kill")
    old = runner_mod.HARD_CANCEL_SECONDS
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(
            tmp, "ignore_cancel", action=lambda r: r.cancel(), act_on_progress=False
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(state["status"] == "cancelled", f"stubborn worker ends cancelled ({state['status']})")
    check(
        latency is not None and latency < 4.0,
        f"terminated within HARD_CANCEL_SECONDS + slack ({latency})",
    )
    check(
        any("terminated" in e["text"] for e in state["log"]), "log says the worker was terminated"
    )

    print(
        "runner: repeated cancel does not postpone the hard kill; hard-killed stage has finishedAt"
    )
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:

        def cancel_twice(r):
            r.cancel()
            threading.Timer(
                1.0, r.cancel
            ).start()  # a second cancel 1 s later must not reset the deadline

        state, latency, _ = run_mode(
            tmp, "ignore_cancel", action=cancel_twice, act_on_progress=False
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(
        state["status"] == "cancelled" and latency is not None and latency < 4.5,
        f"second cancel did not extend the deadline ({latency})",
    )
    work = next(s for s in state["stages"] if s["key"] == "work")
    check(
        work.get("finishedAt") is not None and work.get("elapsed") == state["stages"][1]["elapsed"],
        "hard-killed stage carries finishedAt",
    )
    check(
        sum(1 for e in state["log"] if "cancel requested" in e["text"]) == 1,
        "one 'cancel requested' log line, not two",
    )

    print("runner: hard cancel with a live pool")
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(
            tmp, "pool_stubborn", action=lambda r: r.cancel(), act_on_progress=False, timeout=40
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(
        state["status"] == "cancelled" and latency is not None and latency < 6.0,
        f"worker with a stubborn pool ends cancelled in time ({state['status']}, {latency})",
    )

    print("runner: worker dies")
    state, _, slow = run_mode(tmp, "crash")
    check(state["status"] == "failed", f"dead worker -> failed ({state['status']})")
    check("exited" in (state.get("error") or ""), f"error names the exit ({state.get('error')})")
    check(slow < 100, f"state() stayed responsive while the worker died (slowest {slow:.1f} ms)")

    print("runner: stage spawns a pool (non-daemon worker)")
    state, _, _ = run_mode(tmp, "children")
    by = {s["key"]: s for s in state["stages"]}
    check(
        state["status"] == "done" and by["work"]["status"] == "done",
        f"stage with a child pool runs ({state['status']}: {by['work'].get('summary')})",
    )
    check(
        (state.get("report") or {})
        .get("stages", {})
        .get("work", {})
        .get("result", {})
        .get("squares")
        == 30,
        "pool result came back",
    )

    print("runner: stage raises")
    state, _, _ = run_mode(tmp, "fail")
    by = {s["key"]: s for s in state["stages"]}
    check(
        state["status"] == "failed" and by["work"]["status"] == "failed", "raising stage -> failed"
    )
    check(any("Traceback" in e["text"] for e in state["log"]), "traceback is in the log")
    check(
        state.get("report") and state["report"].get("failed", {}).get("stage") == "work",
        "report names the failed stage",
    )


# -- server ---------------------------------------------------------------------------------------


# -- headless ---------------------------------------------------------------------------------------


def test_headless(tmp):
    print("headless CLI on docs/example-bundle (real stages)")
    os.environ.pop("CS2BEAMNG_STAGE_MODULE", None)
    from cs2beamng.cli import main

    out = os.path.join(tmp, "levels", "cli_example")
    t0 = time.monotonic()
    code = main(["build", EXAMPLE, "--out", out, "--set", "jobs=1"])
    check(code == 0, f"exit code 0 ({code}) in {time.monotonic() - t0:.1f}s")
    report = os.path.join(out, "cs2beamng_report.json")
    check(os.path.isfile(report), "report written")
    if os.path.isfile(report):
        with open(report, encoding="utf-8") as handle:
            doc = json.load(handle)
        check(
            "bake" in doc["stages"] and "validate" in doc["stages"], "report lists bake + validate"
        )


def _body(tmp):
    test_settings()
    test_bake_stage_cancel()
    test_runner(tmp)
    test_headless(tmp)


def main():
    return run_gate(_body)


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_app_shell.py`."""
    assert main() in (None, 0)
