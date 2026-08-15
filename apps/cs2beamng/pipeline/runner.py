"""The runner: owns the worker process, folds its messages into a JSON-able state with ETAs,
and never blocks the caller.

    runner = Runner()
    runner.start(settings)          # spawns the worker; returns at once
    runner.state()                  # {"status", "stages": [...], "overall": {...}, "log": [...]}
    runner.cancel()                 # sets the cancel event; the worker stops within one chunk;
                                    # a worker that ignores it for HARD_CANCEL_SECONDS is killed

State per stage: status (pending/running/done/failed/cancelled/skipped), the current phase, done
/ total / unit, elapsed, live rate (EMA), ETA. Overall: elapsed, ETA (live remaining + the
history-based estimate of every pending stage), fraction. `heartbeatAge` says how long since
the worker last spoke — the UI shows "worker busy" past a few seconds and the runner declares
the worker dead past `DEAD_SECONDS` if the process is also gone.

`build_headless(args)` is the CLI: same runner, a terminal renderer.
"""

import multiprocessing
import os
import sys
import threading
import time
from collections import deque
from queue import Empty

from .. import settings as settings_mod
from ..stages import all_stages
from . import census as census_mod
from . import history
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
        self._reader = None
        self._state = _empty_state()
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
            self._state = _initial_state(self._settings, self._census)
            self._log.clear()
            self._log_seq = 0
            ctx = multiprocessing.get_context("spawn")
            self._queue = ctx.Queue()
            self._cancel = ctx.Event()
            self._process = ctx.Process(target=worker_main, args=(self._settings, self._census, self._queue, self._cancel),
                                        daemon=True, name="cs2beamng-worker")
            self._process.start()
            self._state["status"] = "running"
            self._state["startedAt"] = time.time()
            self._cancel_requested_at = None
            self._reader = threading.Thread(target=self._pump, daemon=True, name="cs2beamng-runner")
            self._reader.start()

    def cancel(self):
        with self._lock:
            if self._process is None or not self._process.is_alive():
                return False
            if self._cancel is not None:
                self._cancel.set()
            self._cancel_requested_at = time.time()
            self._state["cancelRequested"] = True
            self._append_log("system", "cancel requested — finishing the current chunk")
            return True

    def running(self):
        return self._process is not None and self._process.is_alive()

    # -- state ---------------------------------------------------------------------------------

    def state(self):
        with self._lock:
            state = _copy_state(self._state)
            now = time.time()
            state["now"] = now
            state["heartbeatAge"] = (now - state["lastHeartbeat"]) if state.get("lastHeartbeat") else None
            _refresh_estimates(state, self._census, now)
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

    def _pump(self):
        while True:
            try:
                msg = self._queue.get(timeout=0.5)
            except Empty:
                if self._process is None or not self._process.is_alive():
                    self._on_process_exit()
                    return
                continue
            except (EOFError, OSError):
                self._on_process_exit()
                return
            with self._lock:
                self._apply(msg)
                if msg.get("t") in ("done", "error"):
                    self._finish(msg)
                    return

    def _on_process_exit(self):
        with self._lock:
            if self._state["status"] == "running":
                self._state["status"] = "failed"
                self._state["error"] = "worker process exited without reporting"
                self._append_log("system", "worker process exited unexpectedly")
                for st in self._state["stages"]:
                    if st["status"] == "running":
                        st["status"] = "failed"

    def _finish(self, msg):
        if msg["t"] == "error":
            self._state["status"] = "failed"
            self._state["error"] = msg.get("text")
            self._append_log("system", msg.get("text", "error"))
        else:
            self._state["report"] = msg.get("report")
            if not msg.get("ok"):
                self._state["status"] = "cancelled" if any(s["status"] == "cancelled" for s in self._state["stages"]) else "failed"
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
            elif status in ("done", "failed", "cancelled", "skipped"):
                st["finishedAt"] = now
                st["summary"] = msg.get("summary")
                if status == "done" and st.get("total"):
                    st["done"] = st["total"]
                if status == "done":
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
            st["phaseRate"] = rate if st.get("phaseRate") is None else RATE_EMA * rate + (1 - RATE_EMA) * st["phaseRate"]
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
        elif kind == "log":
            self._append_log(msg.get("stage") or "", msg.get("text", ""))

    def _stage(self, key):
        for st in self._state["stages"]:
            if st["key"] == key:
                return st
        return None

    def _watchdog(self, now):
        # hard cancel: the worker ignored the event for too long
        if self._cancel_requested_at and now - self._cancel_requested_at > HARD_CANCEL_SECONDS:
            if self._process is not None and self._process.is_alive():
                self._process.terminate()
                self._append_log("system", "worker did not stop in time; terminated")
                self._state["status"] = "cancelled"
                for st in self._state["stages"]:
                    if st["status"] == "running":
                        st["status"] = "cancelled"
                self._state["finishedAt"] = now
        # dead worker: no heartbeat and no process
        last = self._state.get("lastHeartbeat")
        if last and now - last > DEAD_SECONDS and (self._process is None or not self._process.is_alive()):
            self._on_process_exit()


# -- state helpers ---------------------------------------------------------------------------------

def _empty_state():
    return {"status": "idle", "stages": [], "log": [], "startedAt": None, "finishedAt": None,
            "lastHeartbeat": None, "report": None, "error": None, "cancelRequested": False}


def _initial_state(settings, census):
    state = _empty_state()
    rates = history.load()
    for stage in all_stages():
        enabled = stage.enabled(settings)
        units = stage.units(census, settings) if enabled else 0
        est = history.estimate_seconds(stage.key, units, rates) if units else 0.0
        state["stages"].append({
            "key": stage.key, "title": stage.title, "status": "pending" if enabled else "skipped",
            "units": units, "estimateSeconds": est, "phase": None, "phaseDone": 0, "phaseTotal": None,
            "unit": "", "phaseRate": None, "startedAt": None, "finishedAt": None, "summary": None,
            "fraction": 0.0,
        })
    return state


def _copy_state(state):
    out = dict(state)
    out["stages"] = [dict(s) for s in state["stages"]]
    return out


def _refresh_estimates(state, census, now):
    """Fill elapsed / eta per stage and overall from what is known right now."""
    overall_remaining = 0.0
    known = True
    for st in state["stages"]:
        st["elapsed"] = ((st.get("finishedAt") or now) - st["startedAt"]) if st.get("startedAt") else 0.0
        if st["status"] == "running":
            frac = None
            eta = None
            if st.get("phaseTotal") and st.get("phaseRate"):
                remaining_units = max(0, st["phaseTotal"] - st.get("phaseDone", 0))
                eta = remaining_units / max(st["phaseRate"], 1e-9)
                frac = min(1.0, st.get("phaseDone", 0) / st["phaseTotal"])
            elif st.get("estimateSeconds"):
                eta = max(0.0, st["estimateSeconds"] - st["elapsed"])
                frac = min(0.99, st["elapsed"] / max(st["estimateSeconds"], 1e-9))
            st["eta"] = eta
            st["fraction"] = frac if frac is not None else 0.0
            if eta is None:
                known = False
            else:
                overall_remaining += eta
        elif st["status"] == "pending":
            st["eta"] = st.get("estimateSeconds")
            if st["eta"] is None:
                known = False
            else:
                overall_remaining += st["eta"]
        else:
            st["eta"] = 0.0
    total = len([s for s in state["stages"] if s["status"] != "skipped"]) or 1
    finished = len([s for s in state["stages"] if s["status"] == "done"])
    running_frac = sum(s.get("fraction", 0.0) for s in state["stages"] if s["status"] == "running")
    state["overall"] = {
        "elapsed": (now - state["startedAt"]) if state.get("startedAt") else 0.0,
        "eta": overall_remaining if (known and state["status"] == "running") else None,
        "fraction": (finished + running_frac) / total,
    }


# -- headless ---------------------------------------------------------------------------------------

def build_headless(args):
    """`cs2beamng build ...`: run the pipeline with a terminal renderer."""
    for stream in (sys.stdout, sys.stderr):          # a cp1252 console must not kill a build over an arrow
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    values = settings_mod.load()
    if getattr(args, "settings", None):
        import json
        with open(args.settings, encoding="utf-8") as handle:
            for key, value in json.load(handle).items():
                if key in settings_mod.SCHEMA:
                    values[key] = value
    if getattr(args, "bundle", None):
        values["bundle"] = os.path.abspath(args.bundle)
    if getattr(args, "out", None):
        out = os.path.abspath(args.out)
        values["output"], values["level_name"] = os.path.dirname(out), os.path.basename(out)
    for item in getattr(args, "set", []) or []:
        key, _, raw = item.partition("=")
        values[key] = settings_mod.coerce(key, raw)
    problems = settings_mod.validate(values)
    if problems:
        for p in problems:
            print("settings:", p, file=sys.stderr)
        return 2
    census = census_mod.take(values)
    est = census.get("totalSeconds")
    print(f"bundle: {values['bundle']}")
    print(f"level:  {os.path.join(values['output'], values['level_name'])}")
    print(f"census: {census['bundle']['counts'] if census.get('bundle') else 'n/a'}")
    print(f"expected: {'unknown' if est is None else _fmt_seconds(est)}")
    runner = Runner()
    runner.start(values, census)
    last_seq = 0
    last_line = ""
    try:
        while True:
            state = runner.state()
            for entry in runner.log_since(last_seq):
                sys.stdout.write("\r" + " " * len(last_line) + "\r")
                print(f"  [{entry['stage']}] {entry['text']}")
                last_seq = entry["seq"]
            running = [s for s in state["stages"] if s["status"] == "running"]
            if running:
                s = running[0]
                phase = f" › {s['phase']}" if s.get("phase") else ""
                bar = ""
                if s.get("phaseTotal"):
                    frac = min(1.0, s.get("phaseDone", 0) / s["phaseTotal"])
                    bar = f" [{'#' * int(frac * 20):<20}] {s.get('phaseDone', 0)}/{s['phaseTotal']} {s.get('unit', '')}"
                eta = state["overall"].get("eta")
                line = (f"{s['title']}{phase}{bar}  elapsed {_fmt_seconds(state['overall']['elapsed'])}"
                        f"  eta {'?' if eta is None else _fmt_seconds(eta)}")
                sys.stdout.write("\r" + line[:118].ljust(len(last_line)))
                sys.stdout.flush()
                last_line = line[:118]
            if state["status"] != "running":
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\ncancelling…")
        runner.cancel()
        while runner.running():
            time.sleep(0.2)
        return 130
    state = runner.state()
    print()
    for s in state["stages"]:
        print(f"  {s['title']:40s} {s['status']:9s} {_fmt_seconds(s.get('elapsed') or 0)}  {s.get('summary') or ''}")
    print(f"status: {state['status']} in {_fmt_seconds(state['overall']['elapsed'])}")
    if state.get("error"):
        print(state["error"])
    return 0 if state["status"] == "done" else 1


def _fmt_seconds(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
