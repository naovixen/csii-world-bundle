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
    GET  /api/settings            {values, schema, sections, basics, presets, problems, detected, canBrowse}
    POST /api/settings            {values: {key: value}} -> merged, coerced, validated, saved
    POST /api/preset              {name} -> applied and saved; "custom" restores the remembered set
    GET  /api/census              census for the current settings (cached by fingerprint)
    GET  /api/map                 the settings screen's map: georeference, suggestions, points
    GET  /api/map.png             the map picture itself (drawn once per bundle, then cached)
    POST /api/poi                 {points: [...]} -> stored, and `poi_file` pointed at them
    POST /api/poi/snap            {at: [x, z]} -> the nearest drivable point, in bundle metres
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
INDEX_RECHECK_SECONDS = 5.0
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}
CENSUS_KEYS = (
    "bundle",
    "region",
    "tile_size",
    "generate_lods",
    "markings",
    "lane_furniture",
    "areas",
    "skirts",
    "networks",
    "lights",
    "terrain_upsample",
)


class AppServer:
    """The state the handlers share: runner, settings, census cache, token, hooks."""

    def __init__(self, host="127.0.0.1", port=0, runner=None, values=None):
        self.host = host
        self.port = port
        self.runner = runner or Runner()
        self.values = values if values is not None else settings_mod.load()
        self.token = secrets.token_urlsafe(24)
        self.pick_folder = None  # set by window.py: callable(start_dir) -> path | None
        self.on_quit = None  # set by app.py: callable() when /api/quit arrives
        self._census = None
        self._census_key = None
        self._planned = None
        self._estimate = None  # (settings fingerprint, index identity) -> the estimate
        self._index = None  # the mesh index for the current bundle, once scanned
        self._index_bundle = None
        self._index_thread = None
        self._warming = None  # the thread resolving a bundle's texture plan
        self._index_checked = 0.0  # when the in-memory index was last re-validated
        self._map = None  # the map view document for the current bundle
        self._map_bundle = None
        self._map_thread = None
        self._lock = threading.RLock()
        self._httpd = None
        self._thread = None
        self.stopped = threading.Event()

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self):
        try:
            self._httpd = _Server((self.host, self.port), _Handler, self)
        except OSError as exc:
            raise SystemExit(
                f"cs2beamng: cannot listen on {self.host}:{self.port} ({exc}). "
                "Is another cs2beamng already running? Close it, or pick another --port."
            ) from exc
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            daemon=True,
            name="cs2beamng-http",
        )
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
        # `quality_preset` names the preset last applied, or "custom" once the values have been
        # made the user's own. In the second case the base to count badges against comes from
        # the remembered custom snapshot - the preset they started from.
        custom = settings_mod.load_custom()
        base = values.get("quality_preset")
        if base == "custom":
            base = (custom or {}).get("base")
        return {
            "values": values,
            "schema": settings_mod.schema_for_ui(),
            "groups": settings_mod.GROUPS,
            "basic": settings_mod.BASIC,
            # E1: the redesigned screen's own model - the basics strip, the sidebar, and
            # what drives the settings that no longer have a control of their own.
            "basics": settings_mod.BASICS,
            "sections": settings_mod.sections_for_ui(),
            "drivenBy": settings_mod.DRIVEN_BY,
            "presets": settings_mod.PRESETS,
            "subsetOf": settings_mod.SUBSET_OF,
            "parentOff": settings_mod.PARENT_OFF,
            "needs": settings_mod.NEEDS,
            "choiceSets": settings_mod.CHOICE_SETS,
            "units": settings_mod.UNITS,
            "presetBase": base,
            "hasCustom": custom is not None,
            "presetChanged": settings_mod.preset_changes(values, base),
            "presetInfo": settings_mod.PRESET_INFO,
            "problems": settings_mod.validate(values),
            "detected": settings_mod.detected(values),
            "canBrowse": self.pick_folder is not None,
            # E4: what this level will cost the machine, recomputed on every settings change
            # (~50 ms once the bundle's texture plan is warm) and never a placeholder.
            "estimate": self.estimate(values),
            "mod": self.mod_state(),
            "version": __version__,
        }

    # -- the cost estimate ----------------------------------------------------------------------

    def estimate(self, values):
        """The cost estimate for these settings, memoised on them and on the mesh index.

        Two pieces of it are too slow for a request thread and both are done on a background
        one, with the meter saying what it is waiting for meanwhile: the mesh index is a 20 s
        pass over the instance layer (once per bundle), and the texture plan is ~1 s of path
        joins and PNG headers (once per bundle, again whenever a recolouring setting moves).
        Everything else is 50 ms and runs here. A meter that blocked the settings page for
        twenty seconds would be a worse answer than no meter.
        """
        index = self._mesh_index(values.get("bundle"))
        key = (json.dumps(values, sort_keys=True, default=str), id(index))
        with self._lock:
            if self._estimate is not None and self._estimate[0] == key:
                return self._estimate[1]
        out = self._compute_estimate(values, index, key, warm_only=True)
        if out.get("waiting") == "reading":
            self._in_background(self._compute_estimate, values, index, key)
        return out

    def _compute_estimate(self, values, index, key, warm_only=False):
        from .pipeline import estimate as estimate_mod

        try:
            out = estimate_mod.take(values, self._census, index, warm_only=warm_only)
        except Exception as exc:
            out = {"ready": False, "waiting": "error", "error": str(exc), "rows": []}
        if out.get("waiting") != "reading":  # a refusal is not an answer
            with self._lock:
                self._estimate = (key, out)
        return out

    def _in_background(self, target, *args):
        """Run one slow refresh off the request thread; one at a time, newest wins."""
        with self._lock:
            if self._warming is not None and self._warming.is_alive():
                return
            thread = threading.Thread(
                target=target, args=args, daemon=True, name="cs2beamng-estimate"
            )
            self._warming = thread
        thread.start()

    def _mesh_index(self, bundle):
        """The mesh index for this bundle: the stored one, else a background scan and None.

        The one held in memory is re-checked against the bundle's own bytes every few seconds
        rather than on every request - the check is a scandir of two directories, and a re-rip
        into the same folder has to invalidate it or the meter would go on describing the city
        that used to be there.
        """
        from .pipeline import meshindex

        if not bundle or not os.path.isfile(os.path.join(bundle, "bundle.json")):
            return None
        now = time.monotonic()
        with self._lock:
            fresh = self._index is not None and self._index_bundle == bundle
            due = now - self._index_checked > INDEX_RECHECK_SECONDS
            if fresh and not due:
                return self._index
            running = self._index_thread is not None and self._index_thread.is_alive()
        if fresh:
            with self._lock:
                self._index_checked = now
            if meshindex.key_for(bundle) == self._index.get("key"):
                return self._index
            with self._lock:  # the bundle moved under it
                self._index, self._index_bundle = None, None
        if running:
            # a scan is already reading the layer; asking the disk whether it has finished costs
            # a scandir of ~14,000 entries against the very disk it is reading
            return None
        stored = meshindex.load(bundle)
        if stored is not None:
            with self._lock:
                self._index, self._index_bundle = stored, bundle
                self._index_checked = now
            return stored
        thread = threading.Thread(
            target=self._scan_index, args=(bundle,), daemon=True, name="cs2beamng-meshindex"
        )
        with self._lock:
            self._index_thread = thread
        thread.start()
        return None

    def _scan_index(self, bundle):
        from .pipeline import meshindex

        try:
            doc = meshindex.build(bundle, cancelled=self.stopped.is_set)
        except Exception:
            doc = None
        if doc is not None:
            with self._lock:
                self._index, self._index_bundle = doc, bundle
                self._index_checked = time.monotonic()
                self._estimate = None

    # -- the map (E3) ---------------------------------------------------------------------------

    def map_view(self):
        """The map document for the current bundle, or what it is waiting for.

        Drawn once per bundle on a background thread (~16 s: a 4,096 heightmap, the splat, and a
        pass over the lane layer for the roads), then served from the cache. The page needs the
        georeference as well as the picture, because everything it draws on top — a region circle,
        a fast-travel pin — is in bundle metres and has to land on the right pixel.
        """
        from .pipeline import mapview

        with self._lock:
            bundle = self.values.get("bundle")
        if not bundle or not os.path.isfile(os.path.join(bundle, "bundle.json")):
            return {"ready": False, "waiting": "no bundle"}
        with self._lock:
            if self._map is not None and self._map_bundle == bundle:
                return self._map_payload(self._map)
            running = self._map_thread is not None and self._map_thread.is_alive()
        if running:
            return {"ready": False, "waiting": "drawing"}
        stored = mapview.load(bundle)
        if stored is not None:
            with self._lock:
                self._map, self._map_bundle = stored, bundle
            return self._map_payload(stored)
        thread = threading.Thread(
            target=self._draw_map, args=(bundle,), daemon=True, name="cs2beamng-mapview"
        )
        with self._lock:
            self._map_thread = thread
        thread.start()
        return {"ready": False, "waiting": "drawing"}

    def _map_payload(self, doc):
        """The document as the page wants it: no disk paths, and the points it has to draw."""
        from .pipeline import mapview

        with self._lock:
            bundle = self.values.get("bundle")
            region = self.values.get("region")
        return {
            "ready": True,
            "world": doc["world"],
            "suggestions": doc.get("suggestions") or [],
            "image": f"/api/map.png?t={self.token}&k={doc.get('key')}",
            "points": mapview.load_pois(bundle),
            "region": region,
        }

    def _draw_map(self, bundle):
        from .pipeline import mapview

        try:
            doc = mapview.build(bundle, cancelled=self.stopped.is_set)
        except Exception:
            doc = None
        if doc is not None:
            with self._lock:
                self._map, self._map_bundle = doc, bundle

    def map_image(self):
        """The cached PNG's bytes, or None."""
        from .pipeline import mapview

        with self._lock:
            bundle = self.values.get("bundle")
            doc = self._map if self._map_bundle == bundle else None
        doc = doc or (mapview.load(bundle) if bundle else None)
        if not doc:
            return None
        try:
            with open(doc["image"], "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def snap_point(self, at):
        from .pipeline import mapview

        with self._lock:
            bundle = self.values.get("bundle")
        if not bundle or not at or len(at) < 2:
            return {"at": list(at or [0, 0])[:2], "snapped": False}
        return mapview.snap(bundle, at)

    def save_points(self, points):
        """Store the user's fast-travel points and point `poi_file` at them.

        The setting keeps its name and its meaning — a path to a JSON list — so a headless build
        is unchanged and `--set poi_file=...` still overrides. What the app takes over is the
        writing of it (plan §3.7). An empty list clears the setting rather than leaving it
        pointing at an empty file, which the build would read as "you asked for none".
        """
        from .pipeline import mapview

        with self._lock:
            bundle = self.values.get("bundle")
        if not bundle:
            return {"ok": False, "error": "no bundle"}
        path, clean = mapview.save_pois(bundle, points)
        payload = self.update_settings({"poi_file": path if clean else None})
        payload["points"] = clean
        return payload

    def mod_state(self):
        """What the app can tell the user about the producer half: is the ripper mod installed,
        and can this build install it for them (D4.3 — the release exe ships the zip beside
        itself; a source checkout does not)."""
        from . import modinstall

        try:
            return {
                "installed": modinstall.installed_version(),
                "bundled": modinstall.bundled_version(),
                "canInstall": modinstall.available(),
                "dir": modinstall.install_dir(),
            }
        except Exception as exc:
            return {
                "installed": None,
                "bundled": None,
                "canInstall": False,
                "dir": None,
                "error": str(exc),
            }

    def install_mod(self):
        """Unpack the bundled ripper mod into the CS2 mods folder."""
        from . import modinstall

        try:
            target = modinstall.install()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "mod": self.mod_state()}
        return {"ok": True, "dir": target, "mod": self.mod_state()}

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
            # E6: the settings that have no control of their own, set by the one that drives
            # them (`settings.DRIVES`). It happens HERE, in the app, and not in `coerce` or the
            # CLI: `--set` is a person saying exactly what they want and must stay literal.
            for key, value in settings_mod.driven_changes(incoming or {}).items():
                values[key] = value
            # ANY quality setting moving puts the row on Custom - not just the ones a preset
            # happens to name. Before this, changing `terrain_holes` or `tile_size` left the
            # row reading "High" while the level was no longer what High builds. What is NOT a
            # quality choice is the basics strip (naming your level folder is not a quality) and
            # the two keys that only decide where this window is.
            quality = set(settings_mod.quality_keys())
            touched = [k for k in (incoming or {}) if k in quality]
            # the base is what the values descended from BEFORE this change - read it while
            # `quality_preset` still names a preset, or it is "custom" by the time it is asked
            base = self._preset_base()
            if touched and "quality_preset" not in (incoming or {}):
                values["quality_preset"] = "custom"
            self.values = values
            try:
                settings_mod.save(values)
                if touched:
                    settings_mod.save_custom(values, base)
            except OSError as exc:
                errors.append(f"could not save settings: {exc}")
        payload = self.settings_payload()
        payload["errors"] = errors
        return payload

    def _preset_base(self):
        """The named preset the current values descend from; the caller holds the lock."""
        base = self.values.get("quality_preset")
        if base in settings_mod.PRESETS:
            return base
        return (settings_mod.load_custom() or {}).get("base")

    def apply_custom(self):
        """Put the user's own settings back. The preset row offers this at all times, so
        looking at what another preset does costs nothing and loses nothing."""
        errors = []
        with self._lock:
            self.values = settings_mod.apply_custom(self.values)
            try:
                settings_mod.save(self.values)
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
            self.runner.start(
                values, census
            )  # the runner's own lock decides; two racing starts -> one 409
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
        target = {
            "level": level,
            "output": values.get("output"),
            "bundle": values.get("bundle"),
            "derived": os.path.join(values.get("bundle") or "", "derived"),
            "report": os.path.join(level, REPORT_NAME),
        }.get(what)
        if not target or not os.path.exists(target):
            return {"ok": False, "error": f"nothing at {target!r}"}
        try:
            _open_in_file_manager(target)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": target}


def _open_in_file_manager(path):
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


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
                return self._serve_static(path[len("/static/") :])
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
            if path == "/api/map":
                return self._json(self.app.map_view())
            if path == "/api/map.png":
                image = self.app.map_image()
                if image is None:
                    return self._json({"error": "no map yet"}, HTTPStatus.NOT_FOUND)
                # keyed by the bundle's own fingerprint in the query, so a re-rip is a new URL
                return self._bytes(image, "image/png", cache="public, max-age=31536000, immutable")
            if path == "/api/report":
                return self._json({"report": self.app.report()})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:
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
                if name == "custom":
                    return self._json(self.app.apply_custom())
                if name not in settings_mod.PRESETS:
                    return self._json({"error": f"unknown preset {name!r}"}, HTTPStatus.BAD_REQUEST)
                return self._json(self.app.apply_preset(name))
            if path == "/api/poi":
                return self._json(self.app.save_points(body.get("points") or []))
            if path == "/api/poi/snap":
                return self._json(self.app.snap_point(body.get("at") or []))
            if path == "/api/start":
                payload, status = self.app.start_build()
                return self._json(payload, status)
            if path == "/api/cancel":
                return self._json(self.app.cancel_build())
            if path == "/api/open":
                return self._json(self.app.open_path(body.get("what")))
            if path == "/api/browse":
                if self.app.pick_folder is None:
                    return self._json(
                        {"error": "no native folder picker in a browser"},
                        HTTPStatus.NOT_IMPLEMENTED,
                    )
                picked = self.app.pick_folder(body.get("current") or None)
                return self._json({"path": picked})
            if path == "/api/installmod":
                return self._json(self.app.install_mod())
            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self._quit_later, daemon=True).start()
                return None
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except _BadRequest as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception:
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

    def _bytes(self, data, content_type, cache="no-store"):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
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
        self.close_connection = True  # an unbounded body ends when the socket closes
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
        self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
