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

import json
import os
import shutil
import signal
import time
import traceback

from csiibundle import reader
from csiibundle.bake.progress import Cancelled

from ..settings import SCHEMA as SETTINGS_SCHEMA
from ..settings import app_dir, region_of
from ..stages import BuildContext, all_stages, level_of
from . import history, state
from .progress import Heartbeat, QueueProgress


def _jobs(settings):
    jobs = int(settings.get("jobs") or 0)
    if jobs <= 0:
        jobs = max(1, (os.cpu_count() or 2) - 1)
    return jobs


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
    manifest = _manifest(settings["bundle"])
    skipped = {}
    try:
        os.makedirs(level_dir, exist_ok=True)
        _write_settings(
            level_dir, settings, queue
        )  # first, so an interrupted build still says what it was
        _remove_stale_report(
            level_dir
        )  # a killed build must not be explained by the previous one's report
        if force:
            _clear_generated(level_dir, queue)  # --force is the "start from nothing" path
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
                    sig = state.signature(stage, ctx, manifest, current)
                    fresh, reason = state.check(previous, stage, sig, level_dir)
                except (
                    OSError,
                    ValueError,
                ) as exc:  # a fingerprint that cannot be taken is not a skip
                    fresh, reason = False, f"could not fingerprint inputs ({exc!r})"
                if fresh:
                    result = state.replay(level_dir, stage.key, level_of(ctx), ctx.shared)
                    if result is not None:
                        current["stages"][stage.key] = dict(
                            (previous.get("stages") or {})[stage.key]
                        )
                        current["stages"][stage.key]["skippedAt"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        _merge_report(report, result)
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
            before = _shared_snapshot(ctx.shared, stage.shared_keys)
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
            _merge_report(report, result)
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
                _record(
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
                    "summary": _summary(result),
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
        _write_report(level_dir, report, queue)
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


REPORT_NAME = "cs2beamng_report.json"
SETTINGS_NAME = "cs2beamng_settings.json"  # written first, never removed: see _write_settings


# -- the incremental bookkeeping -------------------------------------------------------------------


def _manifest(bundle_dir):
    """`bundle.json` read once per build; every stage's layer fingerprints resolve through it."""
    try:
        return reader.load_manifest(bundle_dir)
    except (OSError, ValueError):
        return None


def _shared_snapshot(shared, keys):
    """A shallow copy of the declared `ctx.shared` entries before a stage runs, so its delta can
    be worked out afterwards. Sets and dicts are copied one level deep because stages mutate
    them in place (`materials_used.add(...)`)."""
    out = {}
    for key in keys or ():
        value = (shared or {}).get(key)
        if isinstance(value, (set, frozenset)):
            out[key] = set(value)
        elif isinstance(value, dict):
            out[key] = {
                k: (set(v) if isinstance(v, (set, frozenset)) else v) for k, v in value.items()
            }
        elif value is not None:
            out[key] = value
    return out


def _record(level_dir, document, stage, sig, recorder, ctx, before, result, seconds, queue, report):
    """Record what this stage was built from — unless it read a setting it did not declare, in
    which case it must re-run next build rather than be trusted (that exact miss cost a
    25-minute build on 2026-08-18)."""
    missed = state.undeclared(recorder, stage, SETTINGS_SCHEMA)
    if missed:
        text = (
            f"{stage.key}: reads settings it does not declare {missed} - not recorded, so it re-runs "
            f"next build. Add them to the stage's settings_keys."
        )
        queue.put({"t": "log", "stage": stage.key, "text": text, "at": time.time()})
        report.setdefault("stateWarnings", []).append(text)
        return
    try:
        delta = state.diff_shared(before, ctx.shared or {}, stage.shared_keys)
        state.write_payload(
            level_dir, stage.key, level_of(ctx).stage_objects(stage.key), delta, result
        )
    except (OSError, ValueError, TypeError) as exc:
        text = (
            f"{stage.key}: could not write its replay payload ({exc!r}); it will re-run next build"
        )
        queue.put({"t": "log", "stage": stage.key, "text": text, "at": time.time()})
        report.setdefault("stateWarnings", []).append(text)
        return
    present = [
        rel
        for rel in (stage.outputs or ())
        if os.path.exists(os.path.join(level_dir, *rel.split("/")))
    ]
    document["stages"][stage.key] = {
        "signature": sig,
        "seconds": round(seconds, 1),
        "outputs": present,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# Folders `--force` / `rebuild` empties before starting. **Only on force** since A4: clearing
# `art/shapes/` on every build threw away every `.cdae` too, and BeamNG's own compiler is built
# to reuse them (`compileMeshes.lua`: "existing .cdae will be reused. Delete them beforehand if
# a clean state is required") — that one `rmtree` was the whole 6m26s cook on a rebuild that
# changed no geometry. The shape-writing stages now sweep their own orphans at the end instead,
# which is the pattern art/textures/ already used and whose comment explains why: an unchanged
# file keeps its mtime, so BeamNG keeps its cooked copy in temp/ (load #11 re-cooked all 260
# textures on every first load because the folder was cleared and rewritten each build).
GENERATED_DIRS = (("art", "shapes"),)


def _clear_generated(level_dir, queue):
    for parts in GENERATED_DIRS:
        path = os.path.join(level_dir, *parts)
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                queue.put(
                    {
                        "t": "log",
                        "stage": "",
                        "text": f"cleared {'/'.join(parts)}/ from the previous build",
                        "at": time.time(),
                    }
                )
            except OSError as exc:
                queue.put(
                    {
                        "t": "log",
                        "stage": "",
                        "text": f"could not clear {'/'.join(parts)}/: {exc}",
                        "at": time.time(),
                    }
                )


def _write_settings(level_dir, settings, queue):
    """`cs2beamng_settings.json` — every schema key this build ran with, written FIRST.

    The report carries the settings too, but it is written last and deleted at the start of the
    next build, so an interrupted or killed run leaves a level nobody can reproduce. It has
    happened twice: on 2026-08-19 the settings behind `cs2_city_fine` had to be reconstructed out
    of the per-stage results (terrain size, texture caps, collider counts), which recovers most of
    them and guesses the rest. This file is small, it is written before any stage runs, and it is
    never removed - a level folder now says what made it whatever else happens to the build.
    """
    try:
        os.makedirs(level_dir, exist_ok=True)
        recorded = {
            key: settings.get(key, default)
            for key, (default, _kind, _help) in SETTINGS_SCHEMA.items()
        }
        payload = {
            "schema": "cs2beamng.settings/1",
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "settings": recorded,
        }
        paths = [os.path.join(level_dir, SETTINGS_NAME)]
        # ...and a copy outside the level, so deleting or rebuilding the folder cannot take the
        # last record of it with them
        keep = os.path.join(app_dir(), "levels")
        os.makedirs(keep, exist_ok=True)
        paths.append(os.path.join(keep, f"{settings.get('level_name')}.json"))
        for path in paths:
            # Unchanged settings must not rewrite the file: `validate` fingerprints the level
            # FOLDER, so a new mtime here alone would re-run it on every no-op build.
            if _same_settings(path, recorded):
                continue
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=1, sort_keys=True, default=str)
    except OSError as exc:
        queue.put(
            {
                "t": "log",
                "stage": "system",
                "text": f"could not write {SETTINGS_NAME}: {exc}",
                "at": time.time(),
            }
        )


def _same_settings(path, recorded):
    try:
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(existing, dict) and existing.get("settings") == recorded


def _remove_stale_report(level_dir):
    try:
        os.remove(os.path.join(level_dir, REPORT_NAME))
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_report(level_dir, report, queue):
    """The build report lives beside the level; the worker writes it last so every stage — the
    validator included — has its timing and result in it. A cancelled or failed build gets one
    too (with `status` saying so), which is what makes a partial level explainable later."""
    try:
        os.makedirs(level_dir, exist_ok=True)
        path = os.path.join(level_dir, REPORT_NAME)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=1, sort_keys=True, default=str)
    except OSError as exc:
        queue.put(
            {
                "t": "log",
                "stage": "system",
                "text": f"could not write {REPORT_NAME}: {exc}",
                "at": time.time(),
            }
        )


def _merge_report(report, result):
    """Move a stage's `_report` delta into the report proper, and out of its result.

    A stage that writes straight into `ctx.report` loses that write the moment it is SKIPPED: the
    replay puts back its objects, its `shared` delta and its result, and nothing else. `validate`
    did exactly that with `report["validation"]`, so an incremental rebuild dropped the level's
    verdict from the report while still claiming the stage was fresh. Returning the delta instead
    makes it part of the replayed payload, which is the one thing a skip is guaranteed to restore.
    """
    if isinstance(result, dict):
        # `get`, not `pop`: the replay payload is written from this same dict a few lines later,
        # and popping the delta out of it is exactly how the verdict failed to survive a skip the
        # first time. It costs one duplicate copy under `stages.<key>.result`, which is what makes
        # the payload self-describing.
        delta = result.get("_report")
        if isinstance(delta, dict):
            report.update(delta)


def _summary(result):
    if not isinstance(result, dict):
        return None
    parts = []
    for key, value in list(result.items())[:4]:
        if isinstance(value, (int, float, str)):
            parts.append(f"{key}={value}")
    return ", ".join(parts) or None
