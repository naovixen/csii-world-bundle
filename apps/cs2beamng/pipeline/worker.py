"""The worker process: runs the stages, reports through the queue, honours cancel.

Started by `Runner` via `multiprocessing.Process(target=main, ...)`. Everything heavy happens
here; the UI process only reads the queue. A stage that raises records `failed` and the build
stops (a level with a missing stage is not a level); `Cancelled` records `cancelled` and stops
cleanly; the final message is `done` with the report or `error` with a traceback.

**Stages are skipped when nothing they read has changed** (A4). `state.py` holds the mechanism;
what lives here is the loop: fingerprint the stage's declared inputs, compare against
`level_state.json`, and on a match replay its level objects and its `ctx.shared` delta instead
of running it. `rebuild` (setting) or `--force` bypasses the lot. A stage that read a setting it
did not declare is NOT recorded — the report says so and the next build re-runs it. Nor is one
that returns `_norecord`, which is how a stage says "I gave up for a reason the level cannot
see" (the cook, when BeamNG is running or absent).
"""

import os
import signal
import time
import traceback

from csiibundle.bake.progress import Cancelled

from ..settings import region_of
from ..stages import BuildContext, all_stages, level_of
from . import history, state, statepayload
from .progress import Heartbeat, QueueProgress
from .workerstate import (
    clear_generated,
    manifest,
    merge_report,
    record,
    remove_stale_report,
    shared_snapshot,
    summary,
    write_report,
    write_settings,
)


def _jobs(settings):
    jobs = int(settings.get("jobs") or 0)
    if jobs <= 0:
        jobs = max(1, (os.cpu_count() or 2) - 1)
    return jobs


def guard_level_dir(settings, level_dir):
    """Refuse to build into anything that is not one level's own directory.

    Twice a build has written a level's insides straight into the user's `levels/` folder -
    6.5 GB of `art/`, `forest/` and state beside every other level, and BeamNG probing each
    fragment as a level on every scan (2026-08-03 and 2026-08-29, found 2026-08-31). The join
    `output + level_name` makes that a one-empty-string accident, so the worker checks the
    RESULT: a blank name, a level_dir that IS the output dir, or one that already contains
    other levels' `info.json` folders is refused before a byte is written.
    """
    if not str(settings.get("level_name") or "").strip():
        raise RuntimeError(
            "level_name is empty - the build would write into the output folder itself"
        )
    if os.path.normcase(os.path.normpath(level_dir)) == os.path.normcase(
        os.path.normpath(settings["output"])
    ):
        raise RuntimeError(f"level_dir equals the output folder ({level_dir!r}) - refusing")
    if os.path.isdir(level_dir):
        others = [
            child
            for child in os.listdir(level_dir)
            if os.path.isfile(os.path.join(level_dir, child, "info.json"))
        ]
        if others and not os.path.isfile(os.path.join(level_dir, "info.json")):
            raise RuntimeError(
                f"{level_dir!r} contains other levels ({', '.join(others[:3])}...) - it is a "
                f"levels ROOT, not a level; check output and level_name"
            )


def main(settings, census, queue, cancel_event, hard_cancel=None):
    # Ctrl-C in the parent's console reaches this process too; cancellation must arrive through
    # the event (between chunks), never as a KeyboardInterrupt in the middle of a write.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    heartbeat = Heartbeat(queue, hard_cancel=hard_cancel)
    heartbeat.start()
    queue.put({"t": "hello", "pid": os.getpid(), "at": time.time()})
    progress = QueueProgress(queue, cancel_event)
    report = {
        "settings": dict(settings.items()),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stages": {},
    }
    level_dir = os.path.join(settings["output"], settings["level_name"])
    guard_level_dir(settings, level_dir)
    derived_dir = os.path.join(settings["bundle"], "derived")
    ctx = BuildContext(
        settings=settings,
        bundle=settings["bundle"],
        level_dir=level_dir,
        derived_dir=derived_dir,
        census=census,
        region=region_of(settings),
        report=report,
        jobs=_jobs(settings),
        shared={},
    )
    ok = True
    force = bool(settings.get("rebuild", False))
    previous = None if force else state.load(level_dir)
    current = state.new_document(settings)
    bundle_manifest = manifest(settings["bundle"])
    skipped = {}
    try:
        os.makedirs(level_dir, exist_ok=True)
        write_settings(
            level_dir, settings, queue
        )  # first, so an interrupted build still says what it was
        remove_stale_report(
            level_dir
        )  # a killed build must not be explained by the previous one's report
        if force:
            clear_generated(level_dir, queue)  # --force is the "start from nothing" path
        for stage in all_stages():
            if not stage.enabled(settings):
                queue.put(
                    {"t": "stage", "stage": stage.key, "status": "skipped", "at": time.time()}
                )
                continue
            progress.set_stage(stage.key)
            sig = reason = None
            if state.incremental(stage) and not force:
                try:
                    sig = state.signature(stage, ctx, bundle_manifest, current)
                    fresh, reason = state.check(previous, stage, sig, level_dir)
                except (
                    OSError,
                    ValueError,
                ) as exc:  # a fingerprint that cannot be taken is not a skip
                    fresh, reason = False, f"could not fingerprint inputs ({exc!r})"
                if fresh:
                    result = statepayload.replay(level_dir, stage.key, level_of(ctx), ctx.shared)
                    if result is not None:
                        current["stages"][stage.key] = dict(
                            (previous.get("stages") or {})[stage.key]
                        )
                        current["stages"][stage.key]["skippedAt"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        merge_report(report, result)
                        report["stages"][stage.key] = {
                            "seconds": 0.0,
                            "skipped": True,
                            "reason": reason,
                            "result": result,
                        }
                        skipped[stage.key] = reason
                        queue.put(
                            {
                                "t": "stage",
                                "stage": stage.key,
                                "status": "fresh",
                                "seconds": 0.0,
                                "summary": f"unchanged, skipped ({reason})",
                                "at": time.time(),
                            }
                        )
                        continue
                    reason = "replay payload unreadable"
            if any(str(i).startswith("level:") for i in (stage.inputs or ())):
                # This stage READS the level folder (`validate` does), and only stages that RUN
                # call `write_objects` — so anything replayed since the last stage that ran is
                # still missing from disk, and the one closing write at the end of the build
                # happens after this stage, not before it. A build where `instances` was replayed
                # after `derived` ran therefore reported "1,159,098 forest items but no Forest
                # object in the level tree" on a tree that was in fact complete.
                #
                # It has to be HERE and not after each replay: a partial tree written mid-loop
                # differs from the finished one, so every `items.level.json` would be rewritten
                # twice and a no-op rebuild would churn the very mtimes this stage fingerprints.
                level_of(ctx).set_stage(None)
                level_of(ctx).write_objects()
            recorder = state.Recorder(settings)
            stage_ctx = ctx._replace(settings=recorder)
            level_of(ctx).set_stage(stage.key)
            before = shared_snapshot(ctx.shared, stage.shared_keys)
            queue.put({"t": "stage", "stage": stage.key, "status": "running", "at": time.time()})
            if sig is not None and reason:
                queue.put(
                    {
                        "t": "log",
                        "stage": stage.key,
                        "text": f"re-running: {reason}",
                        "at": time.time(),
                    }
                )
            started = time.monotonic()
            try:
                result = stage.run(stage_ctx, progress) or {}
            except Cancelled:
                queue.put(
                    {"t": "stage", "stage": stage.key, "status": "cancelled", "at": time.time()}
                )
                ok = False
                report["cancelled"] = stage.key
                break
            except Exception as exc:
                queue.put(
                    {
                        "t": "log",
                        "stage": stage.key,
                        "text": traceback.format_exc(),
                        "at": time.time(),
                    }
                )
                queue.put(
                    {
                        "t": "stage",
                        "stage": stage.key,
                        "status": "failed",
                        "summary": repr(exc),
                        "at": time.time(),
                    }
                )
                ok = False
                report["failed"] = {"stage": stage.key, "error": repr(exc)}
                break
            seconds = time.monotonic() - started
            merge_report(report, result)
            report["stages"][stage.key] = {"seconds": round(seconds, 1), "result": result}
            per_key = result.pop("_history", None) if isinstance(result, dict) else None
            if per_key:  # heterogeneous stages learn per sub-key
                for key, (u, sec) in per_key.items():
                    history.update(key, u, sec)
            else:
                history.update(stage.key, stage.units(census, settings), seconds)
            # A stage that did not do its work because the MACHINE was not ready for it — no
            # BeamNG installed, the game already running, a level outside the userfolder — must
            # not have that recorded as the state of the level. `cook` bails on all three; caching
            # "skipped, BeamNG was running" would leave the shapes uncooked for good, because
            # closing the game changes nothing this stage declares.
            norecord = bool(result.pop("_norecord", False)) if isinstance(result, dict) else False
            if sig is not None and not norecord:
                record(
                    level_dir,
                    current,
                    stage,
                    sig,
                    recorder,
                    ctx,
                    before,
                    result,
                    seconds,
                    queue,
                    report,
                )
            queue.put(
                {
                    "t": "stage",
                    "stage": stage.key,
                    "status": "done",
                    "seconds": seconds,
                    "summary": summary(result),
                    "at": time.time(),
                }
            )
        # The object tree is rewritten whole from the accumulated `Level`, and only the stages
        # that RUN call `write_objects` themselves — so after a build where a later stage was
        # replayed rather than run, the last writer's snapshot is missing everything replayed
        # after it. One final write closes that: it is the same tree either way.
        level_of(ctx).set_stage(None)
        level_of(ctx).write_objects()
        if skipped:
            report["skippedStages"] = skipped
        if ok:
            _score_estimate(report, settings, census, queue)
            state.save(level_dir, current)
        report["status"] = "done" if ok else ("cancelled" if "cancelled" in report else "failed")
        report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_report(level_dir, report, queue)
        queue.put({"t": "done", "ok": ok, "report": report, "at": time.time()})
    except Exception:
        queue.put({"t": "error", "text": traceback.format_exc(), "at": time.time()})
    finally:
        heartbeat.stop()
        try:
            queue.close()
        except Exception:
            pass


def _score_estimate(report, settings, census, queue):
    """Score the cost meter against what the build actually wrote, and learn from the difference.

    This is the line that keeps the estimator honest. The meter tells a person, before an hour
    and a half of building, how much video memory the level will want; the validator then
    MEASURES the same bytes off the finished level. Printing the two side by side is the only
    thing that can catch an estimate drifting away from the thing it estimates - an estimator
    nobody scores is an estimator nobody can trust.

    It runs at the END rather than the start because the bake writes the derived products the
    texture half is enumerated from: on a first build there is nothing to size until the bake
    has run, and the derived tree the build used is the one that is there now.
    """
    from . import estimate as estimate_mod
    from . import estimatescore, meshindex

    measured = ((report.get("validation") or {}).get("checks") or {}).get("resident")
    if not measured or float(measured.get("totalMiB") or 0) < estimatescore.LEARN_FLOOR_MIB:
        return  # too small to score, let alone learn from
    try:
        index = meshindex.load(settings["bundle"])
        if index is None:
            queue.put(
                {
                    "t": "log",
                    "stage": "system",
                    "at": time.time(),
                    "text": "estimate: indexing the city's shapes so the cost meter has a "
                    "baseline (one pass, kept for next time)",
                }
            )
            index = meshindex.build(settings["bundle"])
        estimated = estimate_mod.take(settings, census, index)
        score = estimatescore.score(estimated, measured)
    except Exception as exc:
        queue.put(
            {
                "t": "log",
                "stage": "system",
                "text": f"estimate: not scored ({exc!r})",
                "at": time.time(),
            }
        )
        return
    if not score:
        return
    report["estimate"] = {
        "predicted": {k: v for k, v in estimated.items() if k != "coefficients"},
        "score": score,
    }
    for name in ("mesh", "texture", "total"):
        row = score.get(name)
        if row:
            queue.put(
                {
                    "t": "log",
                    "stage": "system",
                    "at": time.time(),
                    "text": f"estimate: {name} {row['estimatedMiB']:,.0f} MiB estimated, "
                    f"{row['actualMiB']:,.0f} MiB actual ({row['errorPercent']:+.1f} %)",
                }
            )
    estimatescore.observe(estimated, measured)


# -- the incremental bookkeeping -------------------------------------------------------------------
