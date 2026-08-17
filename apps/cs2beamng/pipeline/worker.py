"""The worker process: runs the stages, reports through the queue, honours cancel.

Started by `Runner` via `multiprocessing.Process(target=main, ...)`. Everything heavy happens
here; the UI process only reads the queue. A stage that raises records `failed` and the build
stops (a level with a missing stage is not a level); `Cancelled` records `cancelled` and stops
cleanly; the final message is `done` with the report or `error` with a traceback.
"""

import json
import os
import shutil
import signal
import time
import traceback

from csiibundle.bake.progress import Cancelled

from ..settings import region_of
from ..stages import BuildContext, all_stages
from . import history
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
    report = {"settings": {k: v for k, v in settings.items()}, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "stages": {}}
    level_dir = os.path.join(settings["output"], settings["level_name"])
    derived_dir = os.path.join(settings["bundle"], "derived")
    ctx = BuildContext(settings=settings, bundle=settings["bundle"], level_dir=level_dir,
                       derived_dir=derived_dir, census=census, region=region_of(settings),
                       report=report, jobs=_jobs(settings), shared={})
    ok = True
    try:
        os.makedirs(level_dir, exist_ok=True)
        _remove_stale_report(level_dir)               # a killed build must not be explained by the previous one's report
        _clear_generated(level_dir, queue)            # shapes/textures are rewritten whole: no orphans from an older build
        for stage in all_stages():
            if not stage.enabled(settings):
                queue.put({"t": "stage", "stage": stage.key, "status": "skipped", "at": time.time()})
                continue
            progress.set_stage(stage.key)
            queue.put({"t": "stage", "stage": stage.key, "status": "running", "at": time.time()})
            started = time.monotonic()
            try:
                result = stage.run(ctx, progress) or {}
            except Cancelled:
                queue.put({"t": "stage", "stage": stage.key, "status": "cancelled", "at": time.time()})
                ok = False
                report["cancelled"] = stage.key
                break
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                queue.put({"t": "log", "stage": stage.key, "text": traceback.format_exc(), "at": time.time()})
                queue.put({"t": "stage", "stage": stage.key, "status": "failed", "summary": repr(exc), "at": time.time()})
                ok = False
                report["failed"] = {"stage": stage.key, "error": repr(exc)}
                break
            seconds = time.monotonic() - started
            report["stages"][stage.key] = {"seconds": round(seconds, 1), "result": result}
            per_key = result.pop("_history", None) if isinstance(result, dict) else None
            if per_key:                                   # heterogeneous stages learn per sub-key
                for key, (u, sec) in per_key.items():
                    history.update(key, u, sec)
            else:
                history.update(stage.key, stage.units(census, settings), seconds)
            queue.put({"t": "stage", "stage": stage.key, "status": "done", "seconds": seconds,
                       "summary": _summary(result), "at": time.time()})
        report["status"] = "done" if ok else ("cancelled" if "cancelled" in report else "failed")
        report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_report(level_dir, report, queue)
        queue.put({"t": "done", "ok": ok, "report": report, "at": time.time()})
    except Exception:  # noqa: BLE001
        queue.put({"t": "error", "text": traceback.format_exc(), "at": time.time()})
    finally:
        heartbeat.stop()
        try:
            queue.close()
        except Exception:  # noqa: BLE001
            pass


REPORT_NAME = "cs2beamng_report.json"


# Folders every build rewrites from scratch. A tile left over from a previous build (a stage
# since removed, a region change) is not referenced by the new object tree, but BeamNG still
# mounts and compiles it, and a stale `.cdae` beside a missing `.dae` is a ghost shape.
# art/textures/ is NOT cleared: `export_texture` rewrites a texture only when its bytes change
# and the materials stage removes the orphans, so an unchanged texture keeps its mtime and
# BeamNG keeps its cooked copy — clearing it cost a ~5 min re-cook on every first load.
GENERATED_DIRS = (("art", "shapes"),)


def _clear_generated(level_dir, queue):
    for parts in GENERATED_DIRS:
        path = os.path.join(level_dir, *parts)
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                queue.put({"t": "log", "stage": "", "text": f"cleared {'/'.join(parts)}/ from the previous build", "at": time.time()})
            except OSError as exc:
                queue.put({"t": "log", "stage": "", "text": f"could not clear {'/'.join(parts)}/: {exc}", "at": time.time()})


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
        queue.put({"t": "log", "stage": "system", "text": f"could not write {REPORT_NAME}: {exc}", "at": time.time()})


def _summary(result):
    if not isinstance(result, dict):
        return None
    parts = []
    for key, value in list(result.items())[:4]:
        if isinstance(value, (int, float, str)):
            parts.append(f"{key}={value}")
    return ", ".join(parts) or None
