"""The runner: owns the worker process, folds its messages into a JSON-able state with ETAs,
and never blocks the caller.

    runner = Runner()
    runner.start(settings)          # spawns the worker; returns at once
    runner.state()                  # {"status", "stages": [...], "overall": {...}, "log": [...]}
    runner.cancel()                 # sets the cancel event; the worker stops within one chunk;
                                    # a worker that ignores it for HARD_CANCEL_SECONDS is killed

State per stage: status (pending/running/done/fresh/failed/cancelled/skipped) — `fresh` is a
stage the worker skipped because nothing it declared had changed, `skipped` one the settings
turned off — the current phase, done
/ total / unit, elapsed, live rate (EMA), ETA. Overall: elapsed, ETA (live remaining + the
history-based estimate of every pending stage), fraction. `heartbeatAge` says how long since
the worker last spoke — the UI shows "worker busy" past a few seconds and the runner declares
the worker dead past `DEAD_SECONDS` if the process is also gone.

`headless.build_headless(args)` is the CLI: same runner, a terminal renderer.
"""

import multiprocessing
import threading
import time
from collections import deque
from queue import Empty

from .. import settings as settings_mod
from . import census as census_mod
from .runstate import copy_state, empty_state, initial_state, refresh_estimates
from .worker import main as worker_main

HARD_CANCEL_SECONDS = 15.0
DEAD_SECONDS = 20.0
LOG_KEEP = 800
RATE_EMA = 0.3


class Runner:
    def __init__(self):
        self._lock = threading.RLock()
        self._process = None
        self._queue = None
        self._cancel = None
        self._hard_cancel = None
        self._reader = None
        self._state = empty_state()
        self._settings = None
        self._census = None
        self._cancel_requested_at = None
        self._log_seq = 0
        self._log = deque(maxlen=LOG_KEEP)

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self, settings, census=None):
        with self._lock:
            if self._process is not None and self._process.is_alive():
                raise RuntimeError("a build is already running")
            problems = settings_mod.validate(settings)
            if problems:
                raise ValueError("; ".join(problems))
            self._settings = dict(settings)
            self._census = census or census_mod.take(settings)
            self._state = initial_state(self._settings, self._census)
            self._log.clear()  # the seq keeps counting: SSE clients filter by it across builds
            ctx = multiprocessing.get_context("spawn")
            self._queue = ctx.Queue()
            self._cancel = ctx.Event()
            self._hard_cancel = (
                ctx.Event()
            )  # the worker's heartbeat thread watches this and self-terminates
            # NOT a daemon: parallel stages (the lods bake) spawn a pool of their own, and a
            # daemonic process may not have children. `kill()` is what stops a stray one instead.
            self._process = ctx.Process(
                target=worker_main,
                args=(self._settings, self._census, self._queue, self._cancel, self._hard_cancel),
                daemon=False,
                name="cs2beamng-worker",
            )
            self._process.start()
            self._state["status"] = "running"
            self._state["startedAt"] = time.time()
            self._cancel_requested_at = None
            self._reader = threading.Thread(
                target=self._pump,
                args=(self._queue, self._process),
                daemon=True,
                name="cs2beamng-runner",
            )
            self._reader.start()

    def cancel(self):
        with self._lock:
            if self._process is None or not self._process.is_alive():
                return False
            if self._cancel is not None:
                self._cancel.set()
            if (
                self._cancel_requested_at is None
            ):  # a repeated cancel must not push the hard-kill deadline
                self._cancel_requested_at = time.time()
                self._state["cancelRequested"] = True
                self._append_log("system", "cancel requested — finishing the current chunk")
            return True

    def running(self):
        return self._process is not None and self._process.is_alive()

    @staticmethod
    def plan(settings, census):
        """The stage list a build with these settings would run, with per-stage estimates - what
        the UI shows before Start. Same code path as the state a real start begins from."""
        return initial_state(settings, census)["stages"]

    def kill(self, reason="worker terminated at application exit"):
        """Terminate the worker outright (application exit, or the watchdog's last resort). The
        worker's own pool children are asked to go first through the hard-cancel event; the join
        happens outside the lock so `state()` keeps its no-blocking promise."""
        with self._lock:
            process = self._process
            if process is None or not process.is_alive():
                return False
            if self._hard_cancel is not None:
                self._hard_cancel.set()
        process.join(1.0)  # give the worker a moment to reap its children itself
        if process.is_alive():
            process.terminate()
        process.join(5.0)
        with self._lock:
            if self._state["status"] == "running":
                self._mark_stopped("cancelled", time.time(), reason)
        return True

    def stop(self, grace=None):
        """Cancel, wait up to `grace` seconds for a clean stop, then kill. What every exit path
        (window close, Ctrl-C, application shutdown) calls."""
        if not self.running():
            return
        self.cancel()
        deadline = time.time() + (HARD_CANCEL_SECONDS + 5.0 if grace is None else grace)
        while self.running() and time.time() < deadline:
            time.sleep(0.2)
        self.kill()

    def _mark_stopped(self, status, now, log_text=None):
        """Under the lock: the build ended without the worker saying so (terminated, crashed)."""
        self._state["status"] = status
        self._state["finishedAt"] = now
        for st in self._state["stages"]:
            if st["status"] == "running":
                st["status"] = status
                st["finishedAt"] = now
        if log_text:
            self._append_log("system", log_text)

    # -- state ---------------------------------------------------------------------------------

    def state(self):
        with self._lock:
            state = copy_state(self._state)
            now = time.time()
            state["now"] = now
            state["heartbeatAge"] = (
                (now - state["lastHeartbeat"]) if state.get("lastHeartbeat") else None
            )
            refresh_estimates(state, self._census, now)
            if state["status"] == "running":
                self._watchdog(now)
            state["log"] = list(self._log)
            return state

    def log_since(self, seq):
        with self._lock:
            return [entry for entry in self._log if entry["seq"] > seq]

    # -- internals -----------------------------------------------------------------------------

    def _append_log(self, stage, text):
        self._log_seq += 1
        self._log.append({"seq": self._log_seq, "stage": stage, "text": text, "at": time.time()})

    def _pump(self, queue, process):
        """The reader thread: bound to ITS queue and process (a stale thread from a killed build
        must never read the next build's queue). It also drives the watchdog, so hard cancel and
        dead-worker detection do not depend on anyone polling `state()`."""
        while True:
            try:
                msg = queue.get(timeout=0.5)
            except Empty:
                with self._lock:
                    if self._process is process:
                        self._watchdog(time.time(), process)
                if not process.is_alive():
                    self._on_process_exit(process)
                    return
                continue
            except (EOFError, OSError):
                self._on_process_exit(process)
                return
            with self._lock:
                if self._process is not process:
                    return  # superseded by a newer build
                self._apply(msg)
                if msg.get("t") in ("done", "error"):
                    self._finish(msg)
                    return

    def _on_process_exit(self, process=None):
        with self._lock:
            if process is not None and self._process is not process:
                return
            if self._state["status"] == "running":
                if (
                    self._cancel_requested_at is not None
                ):  # it left because we asked (hard cancel): not a crash
                    self._mark_stopped("cancelled", time.time(), "worker terminated on hard cancel")
                else:
                    self._state["error"] = "worker process exited without reporting"
                    self._mark_stopped("failed", time.time(), "worker process exited unexpectedly")

    def _finish(self, msg):
        if msg["t"] == "error":
            self._state["status"] = "failed"
            self._state["error"] = msg.get("text")
            self._append_log("system", msg.get("text", "error"))
        else:
            self._state["report"] = msg.get("report")
            if not msg.get("ok"):
                self._state["status"] = (
                    "cancelled"
                    if any(s["status"] == "cancelled" for s in self._state["stages"])
                    else "failed"
                )
            else:
                self._state["status"] = "done"
        self._state["finishedAt"] = time.time()

    def _apply(self, msg):
        kind = msg.get("t")
        now = time.time()
        if kind == "hb":
            self._state["lastHeartbeat"] = msg.get("at", now)
            return
        self._state["lastHeartbeat"] = now
        if kind == "hello":
            self._state["workerPid"] = msg.get("pid")
        elif kind == "stage":
            st = self._stage(msg["stage"])
            if st is None:
                return
            status = msg["status"]
            st["status"] = status
            if status == "running":
                st["startedAt"] = now
            elif status in ("done", "fresh", "failed", "cancelled", "skipped"):
                st["finishedAt"] = now
                st["summary"] = msg.get("summary")
                if status in ("done", "fresh") and st.get("total"):
                    st["done"] = st["total"]
                if status in ("done", "fresh"):
                    st["fraction"] = 1.0
        elif kind == "phase":
            st = self._stage(msg["stage"])
            if st is None:
                return
            st["phase"] = msg.get("phase")
            st["phaseTotal"] = msg.get("total")
            st["phaseDone"] = 0
            st["unit"] = msg.get("unit") or ""
            st["phaseStartedAt"] = now
            st["phaseRate"] = None
            st["phases"] = st.get("phases", 0) + 1
        elif kind == "total":
            st = self._stage(msg["stage"])
            if st is not None:
                st["phaseTotal"] = msg.get("total")
        elif kind == "adv":
            st = self._stage(msg["stage"])
            if st is None:
                return
            done = msg.get("done", 0)
            elapsed = max(1e-6, now - (st.get("phaseStartedAt") or now))
            rate = done / elapsed
            st["phaseRate"] = (
                rate
                if st.get("phaseRate") is None
                else RATE_EMA * rate + (1 - RATE_EMA) * st["phaseRate"]
            )
            st["phaseDone"] = done
        elif kind == "phase_end":
            st = self._stage(msg["stage"])
            if st is None:
                return
            st["phaseDone"] = msg.get("done", st.get("phaseDone"))
            if msg.get("summary"):
                self._append_log(msg["stage"], f"{msg.get('phase')}: {msg['summary']}")
            st["phaseSummary"] = msg.get("summary")
            st["phase"] = None
        elif kind == "stagep":
            st = self._stage(msg["stage"])
            if st is not None:
                st["stageFraction"] = msg.get("fraction")
                st["stageRemaining"] = msg.get("remaining")
                st["stageProgressAt"] = now
        elif kind == "log":
            self._append_log(msg.get("stage") or "", msg.get("text", ""))

    def _stage(self, key):
        for st in self._state["stages"]:
            if st["key"] == key:
                return st
        return None

    def _watchdog(self, now, process=None):
        """Under the lock. Hard cancel: the worker ignored the event for HARD_CANCEL_SECONDS —
        raise the hard-cancel event (the worker's heartbeat thread reaps its pool children and
        exits) and, a little later, terminate. Dead worker: no heartbeat and no process."""
        process = process or self._process
        if self._cancel_requested_at and self._state["status"] == "running":
            overdue = now - self._cancel_requested_at - HARD_CANCEL_SECONDS
            if overdue > 0 and process is not None and process.is_alive():
                if self._hard_cancel is not None and not self._hard_cancel.is_set():
                    self._hard_cancel.set()
                    self._append_log(
                        "system", "worker did not stop in time; asking it to terminate"
                    )
                elif overdue > 2.0:
                    process.terminate()
                    self._mark_stopped("cancelled", now, "worker did not stop in time; terminated")
        last = self._state.get("lastHeartbeat")
        if last and now - last > DEAD_SECONDS and (process is None or not process.is_alive()):
            self._on_process_exit(process)
