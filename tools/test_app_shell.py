"""cs2beamng A0 gate — the application shell: runner (cancel, hard-cancel, dead worker, failure),
server (token, API, SSE), settings schema/groups, and the headless CLI end-to-end.

Run: `python tools/test_app_shell.py`  (needs `pip install -e .`; ~25 s; no bundle beyond
`docs/example-bundle`, no pywebview). Stub stages come from `tools/cs2beamng_stub_stages.py`
through the `CS2BEAMNG_STAGE_MODULE` hook, so the real bake never runs here except in the final
headless check.
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")

FAILS = []
CHECKS = 0


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def wait_until(pred, timeout, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(step)
    return None


def stub_settings(tmp, mode):
    from cs2beamng import settings as settings_mod

    values = settings_mod.defaults()
    values.update({"bundle": EXAMPLE, "output": os.path.join(tmp, "levels"), "level_name": "stub_" + mode,
                   "stub_mode": mode})
    return values


# -- settings ------------------------------------------------------------------------------------

def test_settings():
    from cs2beamng import settings as settings_mod

    print("settings")
    grouped = [k for _t, keys in settings_mod.GROUPS for k in keys]
    check(sorted(grouped) == sorted(settings_mod.SCHEMA), "every SCHEMA key is in exactly one UI group")
    ui = settings_mod.schema_for_ui()
    check(len(ui) == len(settings_mod.SCHEMA) and all(e["group"] != "Other" for e in ui), "schema_for_ui covers all keys")
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


def test_bake_stage_cancel():
    """The bake reports a cancelled product as an outcome; the stage must turn that into
    `Cancelled` (a cancel once ended as DONE, with validate running on a half-baked cache)."""
    from collections import namedtuple

    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Cancelled, Progress
    from cs2beamng.stages import BuildContext, bake as bake_stage

    print("bake stage: cancelled outcome")
    Outcome = namedtuple("Outcome", "name status reason seconds stats")
    real = bake_runner.run
    bake_runner.run = lambda *a, **k: [Outcome("materials", "cancelled", "cancelled by caller", 1.0, None)]
    try:
        ctx = BuildContext(settings={"tile_size": 256}, bundle=EXAMPLE, level_dir="", derived_dir="", census={},
                           region=None, report={}, jobs=1)
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
        ready = work is not None and work["status"] == "running" and (not act_on_progress or work.get("phaseDone", 0) > 0)
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
    check(all(by[k]["status"] == "done" for k in ("prep", "work", "wrap")), "all enabled stages done")
    check(state.get("report") and "work" in state["report"]["stages"], "report carries the stage results")
    check(any("hello from the stub" in e["text"] for e in state["log"]), "stage log reached the runner")
    check(state["overall"]["fraction"] == 1.0, "overall fraction 1.0 when done")
    check(slow < 100, f"state() never blocked (slowest {slow:.1f} ms)")

    print("runner: cancel mid-stage")
    state, latency, _ = run_mode(tmp, "slow", action=lambda r: r.cancel())
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "cancelled", f"status cancelled ({state['status']})")
    check(by["work"]["status"] == "cancelled" and by["wrap"]["status"] == "pending", "work cancelled, wrap never ran")
    check(latency is not None and latency < 1.0, f"cancel returned within 1 s ({latency})")

    print("runner: worker ignores cancel -> hard kill")
    old = runner_mod.HARD_CANCEL_SECONDS
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(tmp, "ignore_cancel", action=lambda r: r.cancel(), act_on_progress=False)
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(state["status"] == "cancelled", f"stubborn worker ends cancelled ({state['status']})")
    check(latency is not None and latency < 4.0, f"terminated within HARD_CANCEL_SECONDS + slack ({latency})")
    check(any("terminated" in e["text"] for e in state["log"]), "log says the worker was terminated")

    print("runner: repeated cancel does not postpone the hard kill; hard-killed stage has finishedAt")
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        def cancel_twice(r):
            r.cancel()
            threading.Timer(1.0, r.cancel).start()          # a second cancel 1 s later must not reset the deadline
        state, latency, _ = run_mode(tmp, "ignore_cancel", action=cancel_twice, act_on_progress=False)
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(state["status"] == "cancelled" and latency is not None and latency < 4.5,
          f"second cancel did not extend the deadline ({latency})")
    work = next(s for s in state["stages"] if s["key"] == "work")
    check(work.get("finishedAt") is not None and work.get("elapsed") == state["stages"][1]["elapsed"], "hard-killed stage carries finishedAt")
    check(sum(1 for e in state["log"] if "cancel requested" in e["text"]) == 1, "one 'cancel requested' log line, not two")

    print("runner: hard cancel with a live pool")
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(tmp, "pool_stubborn", action=lambda r: r.cancel(), act_on_progress=False, timeout=40)
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(state["status"] == "cancelled" and latency is not None and latency < 6.0,
          f"worker with a stubborn pool ends cancelled in time ({state['status']}, {latency})")

    print("runner: worker dies")
    state, _, slow = run_mode(tmp, "crash")
    check(state["status"] == "failed", f"dead worker -> failed ({state['status']})")
    check("exited" in (state.get("error") or ""), f"error names the exit ({state.get('error')})")
    check(slow < 100, f"state() stayed responsive while the worker died (slowest {slow:.1f} ms)")

    print("runner: stage spawns a pool (non-daemon worker)")
    state, _, _ = run_mode(tmp, "children")
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "done" and by["work"]["status"] == "done", f"stage with a child pool runs ({state['status']}: {by['work'].get('summary')})")
    check((state.get("report") or {}).get("stages", {}).get("work", {}).get("result", {}).get("squares") == 30, "pool result came back")

    print("runner: stage raises")
    state, _, _ = run_mode(tmp, "fail")
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "failed" and by["work"]["status"] == "failed", "raising stage -> failed")
    check(any("Traceback" in e["text"] for e in state["log"]), "traceback is in the log")
    check(state.get("report") and state["report"].get("failed", {}).get("stage") == "work", "report names the failed stage")


# -- server ---------------------------------------------------------------------------------------

class Client:
    def __init__(self, url, token):
        u = urlparse(url)
        self.host, self.port, self.token = u.hostname, u.port, token

    def call(self, method, path, body=None, token=True, raw_body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"X-Token": self.token} if token else {}
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return resp.status, raw.decode("utf-8", "replace")

    def sse(self, since=0, want=("state", "log"), timeout=5.0):
        """Read the event stream until every wanted event name has arrived once."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.request("GET", f"/events?t={self.token}&since={since}")
        resp = conn.getresponse()
        seen = {}
        name = None
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < timeout and not all(w in seen for w in want):
                line = resp.readline().decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    seen.setdefault(name, json.loads(line[6:]))
                    name = None
        finally:
            conn.close()
        return resp.status, resp.getheader("Content-Type"), seen


def test_server(tmp):
    from cs2beamng.server import AppServer

    print("server")
    server = AppServer(values=stub_settings(tmp, "slow"))
    url = server.start()
    client = Client(url, server.token)
    try:
        status, page = client.call("GET", "/", token=False)
        check(status == 200 and server.token in page and "app.js" in page, "GET / serves the UI with the token injected")
        status, _ = client.call("GET", "/static/app.js", token=False)
        check(status == 200, "static app.js served")
        status, _ = client.call("GET", "/static/../server.py", token=False)
        check(status == 404, "static path traversal refused")
        status, _ = client.call("GET", "/api/state", token=False)
        check(status == 403, "API without token -> 403")
        status, _ = client.call("POST", "/api/start", token=False)
        check(status == 403, "POST without token -> 403")
        # keep-alive: a rejected POST with a body must not poison the next request on the socket
        conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
        conn.request("POST", "/api/start", body=b'{"values": {"trees": false}}', headers={"Content-Type": "application/json"})
        r1 = conn.getresponse(); r1.read()
        try:
            conn.request("GET", "/api/state", headers={"X-Token": server.token})
            r2 = conn.getresponse(); r2.read()
            second = r2.status
        except (http.client.HTTPException, OSError):
            second = "reconnect"                        # the server closed the connection: also fine
        conn.close()
        check(r1.status == 403 and second in (200, "reconnect"), f"keep-alive survives a rejected POST ({second})")
        status, payload = client.call("GET", "/api/settings")
        check(status == 200 and payload["values"]["bundle"] == EXAMPLE and payload["schema"], "GET /api/settings")
        check(payload["canBrowse"] is False, "no folder picker outside a native window")
        status, payload = client.call("POST", "/api/settings", {"values": {"foliage_density": "42", "nope": 1}})
        check(payload["values"]["foliage_density"] == 42 and payload["values"]["quality_preset"] == "custom",
              "POST /api/settings coerces and marks the preset custom")
        check(any("nope" in e for e in payload["errors"]), "unknown key reported")
        status, payload = client.call("POST", "/api/settings", raw_body=b"{not json")
        check(status == 400, "malformed body -> 400, not silently ignored")
        status, payload = client.call("POST", "/api/preset", {"name": "low"})
        check(payload["values"]["quality_preset"] == "low" and payload["values"]["foliage_density"] == 30, "preset applied")
        status, payload = client.call("POST", "/api/preset", {"name": "bogus"})
        check(status == 400, "unknown preset -> 400")
        status, census = client.call("GET", "/api/census")
        check(status == 200 and census["bundle"] and census["bundle"]["counts"], "census of the example bundle")
        t1 = time.monotonic()
        status, census2 = client.call("GET", "/api/census")
        check(status == 200 and (time.monotonic() - t1) < 0.5, "census is cached")

        status, payload = client.call("POST", "/api/start")
        check(status == 200 and payload["ok"], "POST /api/start")
        status, payload = client.call("POST", "/api/start")
        check(status == 409, "second start while running -> 409")
        # concurrent starts after this one finishes: exactly one wins, the other gets 409 (never 500)
        server.runner.cancel()
        wait_until(lambda: not server.runner.running(), 5.0)
        codes = []
        def _start():
            codes.append(client.call("POST", "/api/start")[0])
        threads = [threading.Thread(target=_start) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        check(sorted(codes) == [200, 409, 409], f"racing starts -> one 200, rest 409 ({codes})")
        wait_until(lambda: server.runner.running(), 2.0)
        status, ctype, seen = client.sse()
        check(status == 200 and ctype.startswith("text/event-stream"), "SSE stream")
        check("state" in seen and seen["state"]["status"] == "running" and "log" not in seen["state"], "state event (without log)")
        check("log" in seen and seen["log"] and "seq" in seen["log"][0], "log delta event")
        status, payload = client.call("POST", "/api/cancel")
        check(payload["ok"], "POST /api/cancel")
        done = wait_until(lambda: not server.runner.running(), 5.0)
        check(done is not None, "build stopped after cancel")
        status, state = client.call("GET", "/api/state")
        check(state["status"] == "cancelled" and isinstance(state["log"], list), "GET /api/state after cancel carries the log")
        status, payload = client.call("GET", "/api/report")
        check(status == 200 and "report" in payload, "GET /api/report")
        # a second build on the same connection: log seq must keep climbing or the client drops lines
        last_seq = state["log"][-1]["seq"] if state["log"] else 0
        client.call("POST", "/api/settings", {"values": {"level_name": "stub_ok2"}})
        server.values["stub_mode"] = "ok"
        status, payload = client.call("POST", "/api/start")
        check(status == 200 and payload["ok"], "second build starts")
        wait_until(lambda: not server.runner.running(), 10.0)
        status, state2 = client.call("GET", "/api/state")
        check(state2["log"] and state2["log"][0]["seq"] > last_seq, "log seq is monotonic across builds")
        status, payload = client.call("POST", "/api/open", {"what": "nowhere"})
        check(payload["ok"] is False, "open of nothing is refused politely")
        status, payload = client.call("POST", "/api/browse", {"key": "bundle"})
        check(status == 501, "browse without a window -> 501")
        # a second server on the same port must fail loudly (Windows SO_REUSEADDR would let it bind)
        try:
            AppServer(port=server.port, values=stub_settings(tmp, "ok")).start()
            check(False, "second server on the same port refused")
        except SystemExit:
            check(True, "second server on the same port refused")
        status, payload = client.call("POST", "/api/quit")
        check(status == 200, "POST /api/quit")
        stopped = wait_until(lambda: server.stopped.is_set(), 3.0)
        check(stopped is not None, "server stops after quit")
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass


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
        check("bake" in doc["stages"] and "validate" in doc["stages"], "report lists bake + validate")


# -- the incremental build (A4) --------------------------------------------------------------------

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
                name = value.__name__ if isinstance(value, types.ModuleType) else getattr(value, "__module__", None)
                if not isinstance(name, str) or not name.startswith("cs2beamng."):
                    continue
                # `cs2beamng.settings` DEFINES the schema (its source names every key once) and
                # the `cs2beamng.stages` package namespace holds every stage module as an
                # attribute once they are imported, so following either makes every stage look
                # like it reads everything. A setting reached through ANOTHER STAGE's helper is
                # therefore outside this scan — the runtime recorder in pipeline/state.py is
                # what catches that case, and it is fail-closed.
                if name in ("cs2beamng.settings", "cs2beamng.stages") or \
                        (name.startswith("cs2beamng.stages.") and name != root.__name__):
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
    import inspect
    import re

    from cs2beamng import settings as settings_mod
    from cs2beamng.pipeline import state as state_mod

    print("stage declarations")
    reads = re.compile(r'settings(?:\.get\(\s*"([a-z_0-9]+)"|\[\s*"([a-z_0-9]+)"\s*\])')
    for key, (stage, modules) in sorted(_stage_modules().items()):
        if not state_mod.incremental(stage):
            continue                                    # not skippable: it always runs, so nothing to declare
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
        undeclared = sorted((found & set(settings_mod.SCHEMA)) - set(stage.settings_keys)
                            - state_mod.NOT_FIDELITY_SETTINGS)
        check(not undeclared, f"stage {key!r} declares every setting it reads"
              + (f" - reads {undeclared}" if undeclared else ""))
    # and the fields themselves have to be well formed, or a signature raises mid-build
    for key, (stage, _mods) in sorted(_stage_modules().items()):
        bad = [ref for ref in (stage.inputs or ())
               if not (ref.startswith("layer:") or ref.startswith("derived:") or ref == "level:")]
        check(not bad, f"stage {key!r} inputs are all layer:/derived:/level: refs" + (f" - {bad}" if bad else ""))
        unknown = [k for k in stage.settings_keys if k not in settings_mod.SCHEMA]
        check(not unknown, f"stage {key!r} declares only real settings" + (f" - {unknown}" if unknown else ""))


def test_state_codec():
    """The `ctx.shared` payload survives a JSON round trip — sets, tuple-keyed dicts and all —
    and a delta composes the way three stages accumulating into one set need it to."""
    from cs2beamng.pipeline import state as state_mod

    print("state codec")
    value = {"results": {("a", "", None): {"file": "a.dae", "n": 1}, ("b", "__dead", 2): {"file": "b.dae"}},
             "used": {"x", "y"}, "plain": [1, 2.5, "z", None], "nested": {"k": {"p", "q"}}}
    back = state_mod.decode(json.loads(json.dumps(state_mod.encode(value))))
    check(back == value, "shared payload round-trips through JSON")
    before = {"materials_used": {"a"}}
    after = {"materials_used": {"a", "b"}}
    delta = state_mod.diff_shared(before, after, ("materials_used",))
    check(delta["materials_used"] == {"b"}, "a set delta is what the stage ADDED, not the whole set")
    shared = {"materials_used": {"c"}}
    state_mod.apply_shared(shared, delta)
    check(shared["materials_used"] == {"b", "c"}, "replaying a set delta unions rather than replaces")
    variants = state_mod.diff_shared({"v": {"k1": {"m"}}}, {"v": {"k1": {"m", "n"}, "k2": {"o"}}}, ("v",))
    target = {"v": {"k1": {"m"}}}
    state_mod.apply_shared(target, variants)
    check(target["v"] == {"k1": {"m", "n"}, "k2": {"o"}}, "a nested dict-of-sets delta merges per key")


def _level_objects(level_dir):
    from cs2beamng.beamng.level import read_objects

    return sorted((rel, json.dumps(obj, sort_keys=True)) for rel, _n, obj in read_objects(level_dir) if obj)


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

    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1"])
    check(code == 0, f"no-op rebuild succeeds ({code})")
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    ran = sorted(k for k, v in report["stages"].items() if not v.get("skipped"))
    check(set(ran) <= {"bake", "cook"},
          f"no-op rebuild skips every incremental stage, the validator included (ran {ran})")
    check(_level_objects(out) == first_objects, "a skipped stage's objects are replayed identically")
    unchanged = [rel for rel, stamp in dae_stamps.items() if _mtime(out, rel) == stamp]
    check(len(unchanged) == len(dae_stamps),
          f"every .dae keeps its mtime, so BeamNG keeps its .cdae ({len(unchanged)}/{len(dae_stamps)})")

    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1", "--set", "lod_detail_scale=9.0"])
    check(code == 0, f"rebuild with a changed setting succeeds ({code})")
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    ran = sorted(k for k, v in report["stages"].items() if not v.get("skipped"))
    check("shapes" in ran, f"the stage that declares lod_detail_scale re-runs (ran {ran})")
    check("instances" in ran, "a stage that declares `after` on it re-runs too")
    check("materials" in ran, "so does the materials stage, which exports what the shapes bound")
    check("terrain" not in ran, "a stage that does not read it stays skipped")
    check("validate" in ran, "the validator re-runs because the level folder moved")
    check(len(_level_objects(out)) == len(first_objects), "the object tree is still complete after a partial rebuild")

    code = cli_main(["build", EXAMPLE, "--out", out, "--set", "jobs=1", "--force"])
    with open(os.path.join(out, "cs2beamng_report.json"), encoding="utf-8") as handle:
        report = json.load(handle)
    check(code == 0 and not any(v.get("skipped") for v in report["stages"].values()),
          "--force runs every stage")
    check(not report.get("stateWarnings"), f"no stage read an undeclared setting {report.get('stateWarnings')}")


def _mtime(root, rel):
    try:
        return os.stat(os.path.join(root, rel)).st_mtime_ns
    except OSError:
        return None


def main():
    tmp = tempfile.mkdtemp(prefix="cs2beamng_shell_")
    os.environ["CS2BEAMNG_HOME"] = os.path.join(tmp, "home")
    os.environ["CS2BEAMNG_STAGE_MODULE"] = "cs2beamng_stub_stages"
    sys.path.insert(0, HERE)
    try:
        test_settings()
        test_stage_declarations()
        test_state_codec()
        test_bake_stage_cancel()
        test_runner(tmp)
        test_server(tmp)
        test_headless(tmp)
        test_incremental(tmp)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILS.append("exception")
    finally:
        # the example bundle's derived/ is a build artefact of the headless step; leave the repo clean
        shutil.rmtree(os.path.join(EXAMPLE, "derived"), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        for f in FAILS:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
