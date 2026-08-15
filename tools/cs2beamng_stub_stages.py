"""Stub stages for the cs2beamng app-shell tests (`tools/test_app_shell.py`).

Selected through `CS2BEAMNG_STAGE_MODULE=cs2beamng_stub_stages` (read in the worker process too);
the behaviour comes from a `stub_mode` key smuggled in the settings dict:

    ok             one quick stage, then done
    slow           one stage advancing 1 unit / 20 ms for 200 units, checking cancel every unit
    ignore_cancel  one stage that sleeps 60 s in a single call and never checks cancel
    crash          one stage that kills the worker process mid-way (os._exit)
    fail           one stage that raises
    children       one stage that runs a multiprocessing pool (the worker must not be a daemon)
    pool_stubborn  a pool that never finishes under a stage that never checks cancel (hard cancel)
"""

import os
import time

from cs2beamng.stages import Stage


def _mode(ctx):
    return ctx.settings.get("stub_mode", "ok")


def _run(ctx, progress):
    mode = _mode(ctx)
    if mode == "ok":
        progress.begin("hello", total=3, unit="steps")
        for _ in range(3):
            progress.advance(1)
        progress.log("hello from the stub stage")
        progress.end("3 steps")
        return {"steps": 3}
    if mode == "slow":
        progress.begin("crawl", total=200, unit="ticks")
        for i in range(200):
            time.sleep(0.02)
            progress.advance(1)
            progress.check()
        progress.end("200 ticks")
        return {"ticks": 200}
    if mode == "pool_stubborn":
        # a pool whose tasks never finish and a stage that never checks cancel: hard cancel must
        # take the worker AND its children down
        import multiprocessing

        progress.begin("stubborn pool", total=None, unit="")
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            pool.map(_sleep_forever, [1, 2])
        return {}
    if mode == "ignore_cancel":
        progress.begin("stubborn", total=None, unit="")
        time.sleep(60.0)
        progress.end()
        return {}
    if mode == "crash":
        progress.begin("doomed", total=10, unit="steps")
        progress.advance(2)
        time.sleep(0.3)                  # let the advance reach the runner first
        os._exit(3)
    if mode == "fail":
        progress.begin("failing", total=None, unit="")
        raise RuntimeError("stub stage failure")
    if mode == "children":
        # what the lods bake does: a pool of its own - impossible from a daemonic worker
        import multiprocessing

        progress.begin("pool", total=4, unit="tasks")
        with multiprocessing.get_context("spawn").Pool(2) as pool:
            squares = pool.map(_square, [1, 2, 3, 4])
        progress.advance(4)
        progress.end(f"pool ok {squares}")
        return {"squares": sum(squares)}
    raise ValueError(f"unknown stub_mode {mode!r}")


def _square(x):
    return x * x


def _sleep_forever(_x):
    time.sleep(600)


def all_stages():
    return [
        Stage("prep", "Prepare (stub)", lambda s: True, lambda c, s: 1,
              lambda ctx, p: (p.log("prep done"), {"ok": True})[1]),
        Stage("work", "Work (stub)", lambda s: True, lambda c, s: 200, _run),
        Stage("skipme", "Skipped (stub)", lambda s: False, lambda c, s: 0, lambda ctx, p: {}),
        Stage("wrap", "Wrap up (stub)", lambda s: True, lambda c, s: 1, lambda ctx, p: {"done": True}),
    ]
