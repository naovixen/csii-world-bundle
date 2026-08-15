"""The worker side of the progress contract: a `csiibundle.bake.progress.Progress` that turns
begin/advance/log/end into messages on a multiprocessing queue, plus a heartbeat thread.

Rate limits are here so a stage advancing a million times sends a few messages a second, not a
million; cancellation is a shared `Event` the runner sets and `cancelled()` reads, so a cancel
returns within one chunk of whatever the stage was doing.
"""

import threading
import time

from csiibundle.bake.progress import Progress

ADVANCE_INTERVAL = 0.25     # seconds between "adv" messages per phase
HEARTBEAT_INTERVAL = 0.5


class QueueProgress(Progress):
    def __init__(self, queue, cancel_event, stage="", clock=time.monotonic):
        self.queue = queue
        self.cancel_event = cancel_event
        self.stage = stage
        self.clock = clock
        self._phase = None
        self._done = 0
        self._total = None
        self._unit = ""
        self._last_sent = 0.0
        self._last_done_sent = -1
        self._last_stagep = 0.0

    def set_stage(self, stage):
        self.stage = stage

    def begin(self, name, total=None, unit="items"):
        self._phase, self._done, self._total, self._unit = name, 0, total, unit
        self._last_sent = 0.0
        self._last_done_sent = -1
        self.queue.put({"t": "phase", "stage": self.stage, "phase": name, "total": total, "unit": unit, "at": time.time()})

    def set_total(self, total):
        self._total = total
        self.queue.put({"t": "total", "stage": self.stage, "total": total})

    def advance(self, n=1):
        self._done += n
        now = self.clock()
        if now - self._last_sent >= ADVANCE_INTERVAL:
            self._flush(now)

    def _flush(self, now=None):
        now = self.clock() if now is None else now
        if self._done != self._last_done_sent:
            self.queue.put({"t": "adv", "stage": self.stage, "done": self._done, "total": self._total})
            self._last_done_sent = self._done
        self._last_sent = now

    def log(self, message):
        self.queue.put({"t": "log", "stage": self.stage, "text": str(message), "at": time.time()})

    def stage_progress(self, fraction, remaining_seconds):
        """Optional, for stages that know their own overall shape (the bake: products × phases):
        a stage-level fraction 0..1 and seconds remaining. The runner prefers these over its
        phase- and history-based guesses. Rate-limited like `advance`."""
        now = self.clock()
        if now - self._last_stagep >= ADVANCE_INTERVAL or fraction >= 1.0:
            self._last_stagep = now
            self.queue.put({"t": "stagep", "stage": self.stage, "fraction": float(fraction),
                            "remaining": None if remaining_seconds is None else float(remaining_seconds), "at": time.time()})

    def end(self, summary=None):
        self._flush()
        self.queue.put({"t": "phase_end", "stage": self.stage, "phase": self._phase, "done": self._done,
                        "total": self._total, "summary": summary})
        self._phase = None

    def cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()


class Heartbeat(threading.Thread):
    """Ticks on the queue while the worker is alive; the runner turns a missing tick into
    'worker busy' (long numpy call) and, past a longer limit, into a hard stop."""

    def __init__(self, queue, interval=HEARTBEAT_INTERVAL):
        super().__init__(daemon=True)
        self.queue = queue
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.queue.put({"t": "hb", "at": time.time()})
            except Exception:  # noqa: BLE001 - the queue is gone; nothing to say
                return

    def stop(self):
        self._stop.set()
