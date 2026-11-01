"""The HTTP shell: one route table, the token check, and the ways a reply is written.

Nothing here knows what a census or a preset is. A route says whether it needs the token and
names what answers it; everything else — reading the body, writing JSON, refusing without a
token, turning an exception into a status — is the same for every route and is written once.

It used to be two `if`/`elif` chains, nineteen branches between them, each branch repeating the
call to `self._json`. Two things about that shape were structural rather than bad luck: the token
check was a statement in the middle of the GET chain, so whether a new route was authorised
depended on where it happened to be pasted, and a route answering with a status as well as a
payload had to remember to unpack the pair. Both are columns of the table now.
"""

import json
import os
import secrets
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
STATE_INTERVAL = 0.5
STATIC_PREFIX = "/static/"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class BadRequest(Exception):
    """A malformed request body; answered with 400 rather than silently treated as empty."""


class Route:
    """One endpoint. `answer(handler, query, body)` writes the reply itself.

    `auth` is the whole of the token policy: the two routes that serve the page and its assets
    are the two a browser reaches before it has been given a token, and they are the two that say
    so here rather than by sitting above a check in a chain.
    """

    __slots__ = ("answer", "auth")

    def __init__(self, answer, auth=True):
        self.answer = answer
        self.auth = auth


def _json_of(method_name, *arg_makers):
    """A route that calls one `AppServer` method and writes its return value as JSON."""

    def answer(handler, query, body):
        args = [make(query, body) for make in arg_makers]
        return handler.reply_json(getattr(handler.app, method_name)(*args))

    return answer


def _json_with_status(method_name):
    """A route whose method answers `(payload, status)` rather than a payload."""

    def answer(handler, _query, _body):
        payload, status = getattr(handler.app, method_name)()
        return handler.reply_json(payload, status)

    return answer


def _map_png(handler, _query, _body):
    image = handler.app.map_image()
    if image is None:
        return handler.reply_json({"error": "no map yet"}, HTTPStatus.NOT_FOUND)
    # keyed by the bundle's own fingerprint in the query, so a re-rip is a new URL
    return handler.reply_bytes(image, "image/png", cache="public, max-age=31536000, immutable")


def _preset(handler, _query, body):
    from .. import settings as settings_mod

    name = body.get("name")
    if name == "custom":
        return handler.reply_json(handler.app.apply_custom())
    if name not in settings_mod.PRESETS:
        return handler.reply_json({"error": f"unknown preset {name!r}"}, HTTPStatus.BAD_REQUEST)
    return handler.reply_json(handler.app.apply_preset(name))


def _browse(handler, _query, body):
    if handler.app.pick_folder is None:
        return handler.reply_json(
            {"error": "no native folder picker in a browser"}, HTTPStatus.NOT_IMPLEMENTED
        )
    return handler.reply_json({"path": handler.app.pick_folder(body.get("current") or None)})


def _quit(handler, _query, _body):
    handler.reply_json({"ok": True})
    handler.quit_later()
    return None


GET = {
    "/": Route(lambda h, _q, _b: h.serve_index(), auth=False),
    STATIC_PREFIX: Route(lambda h, _q, _b: h.serve_static(h.path_after(STATIC_PREFIX)), auth=False),
    "/events": Route(lambda h, q, _b: h.events(int((q.get("since") or ["0"])[0]))),
    "/api/state": Route(_json_of("state", lambda _q, _b: True)),
    "/api/settings": Route(_json_of("settings_payload")),
    "/api/census": Route(_json_of("census", lambda q, _b: "force" in q)),
    "/api/map": Route(_json_of("map_view")),
    "/api/map.png": Route(_map_png),
    "/api/report": Route(lambda h, _q, _b: h.reply_json({"report": h.app.report()})),
}

POST = {
    "/api/settings": Route(_json_of("update_settings", lambda _q, b: b.get("values") or {})),
    "/api/preset": Route(_preset),
    "/api/poi": Route(_json_of("save_points", lambda _q, b: b.get("points") or [])),
    "/api/poi/snap": Route(_json_of("snap_point", lambda _q, b: b.get("at") or [])),
    "/api/start": Route(_json_with_status("start_build")),
    "/api/cancel": Route(_json_of("cancel_build")),
    "/api/open": Route(_json_of("open_path", lambda _q, b: b.get("what"))),
    "/api/browse": Route(_browse),
    "/api/installmod": Route(_json_of("install_mod")),
    "/api/quit": Route(_quit),
}


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
        self._dispatch(GET, read_body=False)

    def do_POST(self):
        self._dispatch(POST, read_body=True)

    def _dispatch(self, table, read_body):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            route = table.get(url.path)
            if route is None and url.path.startswith(STATIC_PREFIX):
                route = table.get(STATIC_PREFIX)
            if route is None:
                return self.reply_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if route.auth and not self._authorised(query):
                return self.reply_json({"error": "missing or wrong token"}, HTTPStatus.FORBIDDEN)
            return route.answer(self, query, self._body() if read_body else {})
        except BadRequest as exc:
            return self.reply_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:
            return self.reply_json(
                {"error": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def path_after(self, prefix):
        return urlparse(self.path).path[len(prefix) :]

    # -- the request ---------------------------------------------------------------------------

    def _authorised(self, query):
        token = self.headers.get("X-Token") or (query.get("t") or [None])[0]
        ok = token is not None and secrets.compare_digest(token, self.app.token)
        if not ok:
            # an unread body must not become the next request on a keep-alive socket
            self.close_connection = True
        return ok

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise BadRequest(f"body is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    # -- the reply -----------------------------------------------------------------------------

    def reply_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, default=str).encode("utf-8")
        self._write(data, CONTENT_TYPES[".json"], status=status)

    def reply_bytes(self, data, content_type, cache="no-store"):
        self._write(data, content_type, cache=cache)

    def _write(self, data, content_type, status=HTTPStatus.OK, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def serve_index(self):
        with open(os.path.join(UI_DIR, "index.html"), "rb") as handle:
            html = handle.read().decode("utf-8")
        html = html.replace("__TOKEN__", self.app.token).replace("__VERSION__", __version__)
        self._write(html.encode("utf-8"), CONTENT_TYPES[".html"])

    def serve_static(self, name):
        safe = os.path.basename(name)
        path = os.path.join(UI_DIR, safe)
        ext = os.path.splitext(safe)[1].lower()
        if safe != name or not os.path.isfile(path) or ext not in CONTENT_TYPES:
            return self.reply_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with open(path, "rb") as handle:
            self._write(handle.read(), CONTENT_TYPES[ext])

    def events(self, since):
        """The SSE stream. One `state` event every STATE_INTERVAL (without the log, which can be
        800 lines), `log` events with only the lines the client has not seen, and `bye` when the
        server stops."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True  # an unbounded body ends when the socket closes
        seq = since
        app = self.app
        while not app.stopped.is_set():
            self._event("state", app.state(with_log=False))
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
        self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())

    def quit_later(self):
        """Answer first, then stop: a server that closes the socket before writing 200 leaves the
        page unable to tell a clean quit from a crash."""
        import threading

        threading.Thread(target=self._quit_now, daemon=True).start()

    def _quit_now(self):
        time.sleep(0.2)
        if self.app.on_quit is not None:
            self.app.on_quit()
        else:
            self.app.stop()
