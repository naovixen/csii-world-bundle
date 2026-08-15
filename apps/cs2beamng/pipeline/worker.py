"""The worker process: runs the stages, reports through the queue, honours cancel.

Started by `Runner` via `multiprocessing.Process(target=main, ...)`. Everything heavy happens
here; the UI process only reads the queue. A stage that raises records `failed` and the build
stops (a level with a missing stage is not a level); `Cancelled` records `cancelled` and stops
cleanly; the final message is `done` with the report or `error` with a traceback.
"""

import os
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


def main(settings, census, queue, cancel_event):
    heartbeat = Heartbeat(queue)
    heartbeat.start()
    queue.put({"t": "hello", "pid": os.getpid(), "at": time.time()})
    progress = QueueProgress(queue, cancel_event)
    report = {"settings": {k: v for k, v in settings.items()}, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "stages": {}}
    level_dir = os.path.join(settings["output"], settings["level_name"])
    derived_dir = os.path.join(settings["bundle"], "derived")
    ctx = BuildContext(settings=settings, bundle=settings["bundle"], level_dir=level_dir,
                       derived_dir=derived_dir, census=census, region=region_of(settings),
                       report=report, jobs=_jobs(settings))
    ok = True
    try:
        os.makedirs(level_dir, exist_ok=True)
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
            units = stage.units(census, settings)
            history.update(stage.key, units, seconds)
            queue.put({"t": "stage", "stage": stage.key, "status": "done", "seconds": seconds,
                       "summary": _summary(result), "at": time.time()})
        queue.put({"t": "done", "ok": ok, "report": report, "at": time.time()})
    except Exception:  # noqa: BLE001
        queue.put({"t": "error", "text": traceback.format_exc(), "at": time.time()})
    finally:
        heartbeat.stop()
        try:
            queue.close()
        except Exception:  # noqa: BLE001
            pass


def _summary(result):
    if not isinstance(result, dict):
        return None
    parts = []
    for key, value in list(result.items())[:4]:
        if isinstance(value, (int, float, str)):
            parts.append(f"{key}={value}")
    return ", ".join(parts) or None
