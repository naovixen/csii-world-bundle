"""cs2beamng A0 gate — the HTTP shell: token, API routes and the SSE stream.

Run: `python tools/test_app_server.py`  (needs `pip install -e .`; ~10 s, no pywebview). Stub
stages come from `tools/cs2beamng_stub_stages.py` through the `CS2BEAMNG_STAGE_MODULE` hook, so
the real bake never runs here.

Part of the A0 gate beside `test_app_shell.py` (runner and headless), `test_app_prose.py`,
`test_app_settings.py`, `test_app_incremental.py` and `test_app_features.py`; tally shared
through `shellcommon`.

The server is what the UI talks to and the only thing between a browser and the build, so what
is pinned is the boundary: an unauthenticated request is refused, every documented route
answers, and the event stream delivers progress in order and closes cleanly.
"""

import http.client
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
from shellcommon import EXAMPLE, check, run_gate, stub_settings, wait_until  # noqa: E402


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
    """The HTTP boundary, in the order a browser meets it."""
    from cs2beamng import settings as settings_mod
    from cs2beamng.server import AppServer

    print("server")
    server = AppServer(values=stub_settings(tmp, "slow"))
    url = server.start()
    client = Client(url, server.token)
    try:
        _serves_the_page(client, server, settings_mod, tmp)
        _refuses_without_a_token(client, server, settings_mod, tmp)
        _reports_its_state(client, server, settings_mod, tmp)
        _applies_settings(client, server, settings_mod, tmp)
        _caches_the_census(client, server, settings_mod, tmp)
        _drives_dependent_settings(client, server, settings_mod, tmp)
        _starts_one_build(client, server, settings_mod, tmp)
        _streams_progress(client, server, settings_mod, tmp)
    finally:
        try:
            server.stop()
        except Exception:
            pass


def _serves_the_page(client, server, settings_mod, tmp):
    """GET / carries the token, and every module the page imports is served — a missing one
    is a blank screen, not an error."""
    status, page = client.call("GET", "/", token=False)
    check(
        status == 200 and server.token in page and "app.js" in page,
        "GET / serves the UI with the token injected",
    )
    # every module the page imports, not just the entry: a missing one is a blank screen
    served = {}
    for name in (
        "app.js",
        "core.js",
        "settings.js",
        "cost.js",
        "detected.js",
        "controls.js",
        "map.js",
        "mapcontrols.js",
        "progress.js",
        "app.css",
        "settings.css",
        "cost.css",
        "map.css",
        "progress.css",
    ):
        served[name] = client.call("GET", "/static/" + name, token=False)[0]
    check(
        all(v == 200 for v in served.values()),
        f"every static module is served: {sorted(k for k, v in served.items() if v != 200)}",
    )


def _refuses_without_a_token(client, server, settings_mod, tmp):
    """Traversal, missing token, and a rejected POST that must not poison the socket it came in
    on."""
    status, _ = client.call("GET", "/static/../server.py", token=False)
    check(status == 404, "static path traversal refused")
    status, _ = client.call("GET", "/api/state", token=False)
    check(status == 403, "API without token -> 403")
    status, _ = client.call("POST", "/api/start", token=False)
    check(status == 403, "POST without token -> 403")
    # keep-alive: a rejected POST with a body must not poison the next request on the socket
    conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
    conn.request(
        "POST",
        "/api/start",
        body=b'{"values": {"trees": false}}',
        headers={"Content-Type": "application/json"},
    )
    r1 = conn.getresponse()
    r1.read()
    try:
        conn.request("GET", "/api/state", headers={"X-Token": server.token})
        r2 = conn.getresponse()
        r2.read()
        second = r2.status
    except (http.client.HTTPException, OSError):
        second = "reconnect"  # the server closed the connection: also fine
    conn.close()
    check(
        r1.status == 403 and second in (200, "reconnect"),
        f"keep-alive survives a rejected POST ({second})",
    )


def _reports_its_state(client, server, settings_mod, tmp):
    """What the UI reads before a person touches anything: detection, capability, cost."""
    status, payload = client.call("GET", "/api/settings")
    check(
        status == 200 and payload["values"]["bundle"] == EXAMPLE and payload["schema"],
        "GET /api/settings",
    )
    check(payload["canBrowse"] is False, "no folder picker outside a native window")
    status, payload = client.call(
        "POST", "/api/settings", {"values": {"foliage_density": "42", "nope": 1}}
    )
    check(
        payload["values"]["foliage_density"] == 42
        and payload["values"]["quality_preset"] == "custom",
        "POST /api/settings coerces, and ANY quality change puts the row on Custom",
    )
    check(
        payload["presetBase"] == "high" and payload["presetChanged"] == ["foliage_density"],
        f"the payload keeps the base and says what moved off it ({payload['presetChanged']})",
    )
    check(
        payload["hasCustom"] is True,
        "the custom settings are remembered the moment there are any",
    )
    check(
        [s["id"] for s in payload["sections"]] == [s["id"] for s in settings_mod.SECTIONS],
        "E2: the payload carries the sidebar",
    )
    # naming your level folder is not a quality choice and must not move the row
    _s, named = client.call("POST", "/api/settings", {"values": {"level_title": "Whatever"}})
    check(
        named["values"]["quality_preset"] == "custom",
        "a basics-strip change leaves the row where it is",
    )
    check(any("nope" in e for e in payload["errors"]), "unknown key reported")


def _applies_settings(client, server, settings_mod, tmp):
    """Applying values, the preset round trip, and the two ways a bad request is refused."""
    status, payload = client.call("POST", "/api/settings", raw_body=b"{not json")
    check(status == 400, "malformed body -> 400, not silently ignored")
    status, payload = client.call("POST", "/api/preset", {"name": "low"})
    check(
        payload["values"]["quality_preset"] == "low" and payload["values"]["foliage_density"] == 30,
        "preset applied",
    )
    check(payload["presetChanged"] == [], "a freshly applied preset has nothing moved off it")
    # ...and Custom comes back to what the user had, which is the point of it always being there
    status, back = client.call("POST", "/api/preset", {"name": "custom"})
    check(
        status == 200
        and back["values"]["quality_preset"] == "custom"
        and back["values"]["foliage_density"] == 42,
        f"Custom restores the remembered settings ({back['values']['foliage_density']})",
    )
    status, payload = client.call("POST", "/api/preset", {"name": "bogus"})
    check(status == 400, "unknown preset -> 400")


def _caches_the_census(client, server, settings_mod, tmp):
    """The census is expensive and does not change under the app, so it is computed once."""
    status, census = client.call("GET", "/api/census")
    check(
        status == 200 and census["bundle"] and census["bundle"]["counts"],
        "census of the example bundle",
    )
    t1 = time.monotonic()
    status, _census2 = client.call("GET", "/api/census")
    check(status == 200 and (time.monotonic() - t1) < 0.5, "census is cached")


def _drives_dependent_settings(client, server, settings_mod, tmp):
    """E6: the settings with no control of their own, set by the one that drives them. The rule
    lives in the APP, so it is asserted through the app's own API."""
    # -- E6: the settings with no control of their own, set by the one that drives them -----
    # The rule lives in the APP, so it is asserted through the app's own API. `--set` does
    # not pass through here and stays literal.
    _status, payload = client.call("POST", "/api/settings", {"values": {"networks": False}})
    check(
        payload["values"]["terrain_cut"] is False,
        "roads off takes the ground cut with it - nothing to see in the hole",
    )
    _status, payload = client.call("POST", "/api/settings", {"values": {"networks": True}})
    check(payload["values"]["terrain_cut"] is True, "...and roads back on brings it back")
    _status, payload = client.call("POST", "/api/settings", {"values": {"foliage_density": 0}})
    check(
        payload["values"]["trees"] is False,
        "a density slider at zero turns its kind off, rather than keeping none of it",
    )
    _status, payload = client.call("POST", "/api/settings", {"values": {"foliage_density": 40}})
    check(payload["values"]["trees"] is True, "...and above zero turns it back on")
    _status, payload = client.call("POST", "/api/settings", {"values": {"prop_density": 0}})
    check(
        payload["values"]["props"] is False and payload["values"]["trees"] is True,
        "each slider drives its own kind and no other",
    )
    _status, payload = client.call(
        "POST", "/api/settings", {"values": {"networks": False, "terrain_cut": True}}
    )
    check(
        payload["values"]["terrain_cut"] is True,
        "naming both halves means both halves: the rule fills in, it does not overrule",
    )
    client.call("POST", "/api/settings", {"values": {"networks": True, "prop_density": 100}})


def _starts_one_build(client, server, settings_mod, tmp):
    """Exactly one start wins, however many arrive at once, and the losers get 409 rather than
    500."""
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
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(sorted(codes) == [200, 409, 409], f"racing starts -> one 200, rest 409 ({codes})")
    wait_until(lambda: server.runner.running(), 2.0)


def _streams_progress(client, server, settings_mod, tmp):
    """The SSE stream delivers ordered progress and a log delta, and cancel stops the build."""
    status, ctype, seen = client.sse()
    check(status == 200 and ctype.startswith("text/event-stream"), "SSE stream")
    check(
        "state" in seen and seen["state"]["status"] == "running" and "log" not in seen["state"],
        "state event (without log)",
    )
    check("log" in seen and seen["log"] and "seq" in seen["log"][0], "log delta event")
    status, payload = client.call("POST", "/api/cancel")
    check(payload["ok"], "POST /api/cancel")
    done = wait_until(lambda: not server.runner.running(), 5.0)
    check(done is not None, "build stopped after cancel")
    status, state = client.call("GET", "/api/state")
    check(
        state["status"] == "cancelled" and isinstance(state["log"], list),
        "GET /api/state after cancel carries the log",
    )
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
    check(
        state2["log"] and state2["log"][0]["seq"] > last_seq,
        "log seq is monotonic across builds",
    )
    status, payload = client.call("POST", "/api/open", {"what": "nowhere"})
    check(payload["ok"] is False, "open of nothing is refused politely")
    status, payload = client.call("POST", "/api/browse", {"key": "bundle"})
    check(status == 501, "browse without a window -> 501")
    # a second server on the same port must fail loudly (Windows SO_REUSEADDR would let it bind)
    from cs2beamng.server import AppServer

    try:
        AppServer(port=server.port, values=stub_settings(tmp, "ok")).start()
        check(False, "second server on the same port refused")
    except SystemExit:
        check(True, "second server on the same port refused")
    status, payload = client.call("POST", "/api/quit")
    check(status == 200, "POST /api/quit")
    stopped = wait_until(lambda: server.stopped.is_set(), 3.0)
    check(stopped is not None, "server stops after quit")


def main():
    return run_gate(test_server)


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_app_server.py`."""
    assert main() in (None, 0)
