"""The local web server behind the UI: static files, a JSON API and one Server-Sent-Events
stream. Standard library only (`http.server.ThreadingHTTPServer`), bound to 127.0.0.1 on an
ephemeral port, so it is the same on Windows, Linux and macOS and needs no build step.

    server = AppServer()            # owns a Runner, the settings, and a census cache
    url = server.start()            # "http://127.0.0.1:<port>/?t=<token>"
    ...
    server.stop()

Endpoints (all under the token — see below):

    GET  /                        the UI (index.html with the token injected)
    GET  /static/<file>           app.css, app.js
    GET  /events?since=<seq>      SSE: `state` every 0.5 s (no log), `log` deltas, `bye` on quit
    GET  /api/state               the runner state incl. the log (polling fallback)
    GET  /api/settings            {values, schema, groups, presets, problems, detected, canBrowse}
    POST /api/settings            {values: {key: value}} -> merged, coerced, validated, saved
    POST /api/preset              {name} -> preset applied and saved
    GET  /api/census              census for the current settings (cached by fingerprint)
    POST /api/start               start a build with the current settings
    POST /api/cancel              cancel the running build
    GET  /api/report              the last build's report (or the report file in the level dir)
    POST /api/open                {what: level|output|bundle|derived|report} -> file manager
    POST /api/browse              {key, current} -> native folder picker (only in a window)
    POST /api/quit                stop the server (the window/browser flow calls this)

Why a token: the server listens on localhost, and a web page from anywhere can *send* requests
to localhost. Every `/api` and `/events` request must carry the launch token (`X-Token` header,
or `?t=` for `EventSource`, which cannot set headers). A hostile page cannot read the token —
`/` is same-origin only (no CORS headers) — and cannot send the header without a preflight we
never answer, so it cannot start, cancel or reconfigure a build.
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import settings as settings_mod
from .pipeline import census as census_mod
from .pipeline.runner import Runner
from .pipeline.worker import REPORT_NAME

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STATE_INTERVAL = 0.5
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}
CENSUS_KEYS = ("bundle", "region", "tile_size", "generate_lods", "markings", "lane_furniture", "areas",
               "skirts", "networks", "lights", "terrain_upsample")


class AppServer:
    """The state the handlers share: runner, settings, census cache, token, hooks."""

    def __init__(self, host="127.0.0.1", port=0, runner=None, values=None):
        self.host = host
        self.port = port
        self.runner = runner or Runner()
        self.values = values if values is not None else settings_mod.load()
        self.token = secrets.token_urlsafe(24)
        self.pick_folder = None            # set by window.py: callable(start_dir) -> path | None
        self.on_quit = None                # set by app.py: callable() when /api/quit arrives
        self._census = None
        self._census_key = None
        self._planned = None
        self._lock = threading.RLock()
        self._httpd = None
        self._thread = None
        self.stopped = threading.Event()

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self):
        try:
            self._httpd = _Server((self.host, self.port), _Handler, self)
        except OSError as exc:
            raise SystemExit(f"cs2beamng: cannot listen on {self.host}:{self.port} ({exc}). "
                             "Is another cs2beamng already running? Close it, or pick another --port.") from exc
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        daemon=True, name="cs2beamng-http")
        self._thread.start()
        return self.url()

    def url(self, with_token=True):
        base = f"http://{self.host}:{self.port}/"
        return f"{base}?t={self.token}" if with_token else base

    def stop(self):
        """Idempotent and safe from any thread (`/api/quit`'s thread and the main thread's
        shutdown both call it)."""
        self.stopped.set()
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # -- settings / census ---------------------------------------------------------------------

    def settings_payload(self):
        with self._lock:
            values = dict(self.values)
        return {"values": values, "schema": settings_mod.schema_for_ui(), "groups": settings_mod.GROUPS,
                "presets": settings_mod.PRESETS, "problems": settings_mod.validate(values),
                "detected": settings_mod.detected(values), "canBrowse": self.pick_folder is not None,
                "version": __version__}

    def update_settings(self, incoming):
        errors = []
        with self._lock:
            values = dict(self.values)
            for key, raw in (incoming or {}).items():
                if key not in settings_mod.SCHEMA:
                    errors.append(f"unknown setting {key!r}")
                    continue
                try:
                    values[key] = settings_mod.coerce(key, raw)
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
            if "quality_preset" not in (incoming or {}) and any(k in settings_mod.PRESETS["high"] for k in incoming or {}):
                values["quality_preset"] = "custom"
            self.values = values
            try:
                settings_mod.save(values)
            except OSError as exc:
                errors.append(f"could not save settings: {exc}")
        payload = self.settings_payload()
        payload["errors"] = errors
        return payload

    def apply_preset(self, name):
        errors = []
        with self._lock:
            self.values = settings_mod.apply_preset(self.values, name)
            try:
                settings_mod.save(self.values)
            except OSError as exc:
                errors.append(f"could not save settings: {exc}")
        payload = self.settings_payload()
        payload["errors"] = errors
        return payload

    def census(self, force=False):
        with self._lock:
            values = dict(self.values)
        key = tuple(json.dumps(values.get(k), sort_keys=True) for k in CENSUS_KEYS)
        for rel in ("bundle.json", os.path.join("derived", "derived.json")):
            path = os.path.join(values.get("bundle") or "", rel)
            try:
                key += (os.path.getmtime(path),)
            except OSError:
                key += (None,)
        with self._lock:
            if not force and self._census is not None and self._census_key == key:
                return self._census
        census = census_mod.take(values)
        with self._lock:
            self._census, self._census_key = census, key
        return census

    # -- build ---------------------------------------------------------------------------------

    def start_build(self):
        with self._lock:
            values = dict(self.values)
        problems = settings_mod.validate(values)
        if problems:
            return {"ok": False, "problems": problems}, HTTPStatus.BAD_REQUEST
        census = self.census()
        try:
            self.runner.start(values, census)          # the runner's own lock decides; two racing starts -> one 409
        except RuntimeError as exc:
            return {"ok": False, "problems": [str(exc)]}, HTTPStatus.CONFLICT
        except ValueError as exc:
            return {"ok": False, "problems": [str(exc)]}, HTTPStatus.BAD_REQUEST
        self._planned = None
        return {"ok": True, "state": self.state(with_log=True)}, HTTPStatus.OK

    def cancel_build(self):
        return {"ok": self.runner.cancel()}

    def state(self, with_log=False):
        state = self.runner.state()
        log = state.pop("log", [])
        state["logSeq"] = log[-1]["seq"] if log else 0
        if with_log:
            state["log"] = log
        if state["status"] == "idle" and not state["stages"]:
            state["stages"] = self._planned_stages()
        return state

    def _planned_stages(self):
        """Before the first build: the stage list the current settings would run, with the
        history-based estimate per stage, so the Build card is never an empty box. Memoised on
        (settings, census identity): it is served on every state tick."""
        with self._lock:
            values = dict(self.values)
            census = self._census
            key = (json.dumps(values, sort_keys=True, default=str), id(census))
            if self._planned is not None and self._planned[0] == key:
                return self._planned[1]
        if census is None or settings_mod.validate(values):
            stages = []
        else:
            stages = Runner.plan(values, census)
        with self._lock:
            self._planned = (key, stages)
        return stages

    def report(self):
        """The report of the last build in this session; before any build, the report file left
        in the level folder by an earlier session (never a previous build's file for a build
        that died without writing one - the worker removes it at start)."""
        state = self.runner.state()
        if state.get("report") or state["status"] != "idle":
            return state.get("report")
        with self._lock:
            values = dict(self.values)
        path = os.path.join(values.get("output") or "", values.get("level_name") or "", REPORT_NAME)
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def open_path(self, what):
        with self._lock:
            values = dict(self.values)
        level = os.path.join(values.get("output") or "", values.get("level_name") or "")
        target = {"level": level, "output": values.get("output"), "bundle": values.get("bundle"),
                  "derived": os.path.join(values.get("bundle") or "", "derived"),
                  "report": os.path.join(level, REPORT_NAME)}.get(what)
        if not target or not os.path.exists(target):
            return {"ok": False, "error": f"nothing at {target!r}"}
        try:
            _open_in_file_manager(target)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": target}


def _open_in_file_manager(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - the user asked to open their own folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S603,S607


# -- HTTP plumbing ---------------------------------------------------------------------------------

class _BadRequest(Exception):
    """A malformed request body; answered with 400 rather than silently treated as empty."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a SECOND process bind the same port and steal requests
    # (two app instances, two tokens, "wrong token" at random); there it must stay off. On
    # POSIX it only skips TIME_WAIT after a restart, which is what we want.
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, address, handler, app):
        self.app = app
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = f"cs2beamng/{__version__}"
    protocol_version = "HTTP/1.1"

    # quiet by default; the runner log is the log that matters
    def log_message(self, fmt, *args):
        if os.environ.get("CS2BEAMNG_HTTP_LOG"):
            sys.stderr.write("http: " + fmt % args + "\n")

    @property
    def app(self):
        return self.server.app

    # -- routing -------------------------------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        try:
            if path == "/":
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if not self._authorised(query):
                return self._json({"error": "missing or wrong token"}, HTTPStatus.FORBIDDEN)
            if path == "/events":
                return self._events(int((query.get("since") or ["0"])[0]))
            if path == "/api/state":
                return self._json(self.app.state(with_log=True))
            if path == "/api/settings":
                return self._json(self.app.settings_payload())
            if path == "/api/census":
                return self._json(self.app.census(force="force" in query))
            if path == "/api/report":
                return self._json({"report": self.app.report()})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:  # noqa: BLE001 - report to the client, never kill the server thread silently
            return self._json({"error": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if not self._authorised(parse_qs(url.query)):
                return self._json({"error": "missing or wrong token"}, HTTPStatus.FORBIDDEN)
            body = self._body()
            if path == "/api/settings":
                return self._json(self.app.update_settings(body.get("values") or {}))
            if path == "/api/preset":
                name = body.get("name")
                if name not in settings_mod.PRESETS:
                    return self._json({"error": f"unknown preset {name!r}"}, HTTPStatus.BAD_REQUEST)
                return self._json(self.app.apply_preset(name))
            if path == "/api/start":
                payload, status = self.app.start_build()
                return self._json(payload, status)
            if path == "/api/cancel":
                return self._json(self.app.cancel_build())
            if path == "/api/open":
                return self._json(self.app.open_path(body.get("what")))
            if path == "/api/browse":
                if self.app.pick_folder is None:
                    return self._json({"error": "no native folder picker in a browser"}, HTTPStatus.NOT_IMPLEMENTED)
                picked = self.app.pick_folder(body.get("current") or None)
                return self._json({"path": picked})
            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self._quit_later, daemon=True).start()
                return None
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except _BadRequest as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:  # noqa: BLE001
            return self._json({"error": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _quit_later(self):
        time.sleep(0.2)
        if self.app.on_quit is not None:
            self.app.on_quit()
        else:
            self.app.stop()

    # -- helpers -------------------------------------------------------------------------------

    def _authorised(self, query):
        token = self.headers.get("X-Token") or (query.get("t") or [None])[0]
        ok = token is not None and secrets.compare_digest(token, self.app.token)
        if not ok:
            self.close_connection = True       # an unread body must not become the next request on a keep-alive socket
        return ok

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise _BadRequest(f"body is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise _BadRequest("body must be a JSON object")
        return body

    def _json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_index(self):
        path = os.path.join(UI_DIR, "index.html")
        with open(path, "rb") as handle:
            html = handle.read().decode("utf-8")
        html = html.replace("__TOKEN__", self.app.token).replace("__VERSION__", __version__)
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", CONTENT_TYPES[".html"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, name):
        safe = os.path.basename(name)
        path = os.path.join(UI_DIR, safe)
        ext = os.path.splitext(safe)[1].lower()
        if safe != name or not os.path.isfile(path) or ext not in CONTENT_TYPES:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with open(path, "rb") as handle:
            data = handle.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", CONTENT_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _events(self, since):
        """The SSE stream. One `state` event every STATE_INTERVAL (without the log, which can be
        800 lines), `log` events with only the lines the client has not seen, a comment as a
        keep-alive, and `bye` when the server stops."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True       # an unbounded body ends when the socket closes
        seq = since
        app = self.app
        while not app.stopped.is_set():
            state = app.state(with_log=False)
            self._event("state", state)
            fresh = app.runner.log_since(seq)
            if fresh:
                seq = fresh[-1]["seq"]
                self._event("log", fresh)
            self.wfile.flush()
            app.stopped.wait(STATE_INTERVAL)
        self._event("bye", {})
        self.wfile.flush()

    def _event(self, name, payload):
        data = json.dumps(payload, default=str)
        self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
