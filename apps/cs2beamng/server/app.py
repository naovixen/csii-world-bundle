"""`AppServer` — what the UI can ask for, and the settings it holds while asking.

One object per running app: the settings, the build runner, the launch token, the two hooks the
desktop window fills in, and a `BundleCaches` for everything expensive. Every method here is an
answer to one request, and the HTTP layer knows nothing except which method a path names.
"""

import json
import os
import secrets
import subprocess
import sys
import threading
from http import HTTPStatus

from .. import __version__
from .. import settings as settings_mod
from ..pipeline.runner import Runner
from ..pipeline.worker import REPORT_NAME
from . import mappoints
from .caches import BundleCaches
from .routes import _Handler, _Server


class AppServer:
    """The state the handlers share: runner, settings, derived-data caches, token, hooks."""

    def __init__(self, host="127.0.0.1", port=0, runner=None, values=None):
        self.host = host
        self.port = port
        self.runner = runner or Runner()
        self.values = values if values is not None else settings_mod.load()
        self.token = secrets.token_urlsafe(24)
        self.pick_folder = None  # set by window.py: callable(start_dir) -> path | None
        self.on_quit = None  # set by app.py: callable() when /api/quit arrives
        self.stopped = threading.Event()
        self.caches = BundleCaches(self.stopped)
        self._lock = threading.RLock()
        self._httpd = None
        self._thread = None

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
        return base + f"?t={self.token}" if with_token else base

    def stop(self):
        self.stopped.set()
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def _values(self):
        with self._lock:
            return dict(self.values)

    # -- settings / census ---------------------------------------------------------------------

    def settings_payload(self):
        values = self._values()
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
            "estimate": self.caches.estimate(values),
            "mod": self.mod_state(),
            "version": __version__,
        }

    def census(self, force=False):
        return self.caches.census(self._values(), force=force)

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
            errors.extend(self._save(values, remember=bool(touched), base=base))
        return self._settings_reply(errors)

    def _save(self, values, remember=False, base=None):
        """Persist, returning what went wrong rather than raising. The caller holds the lock.

        `remember` and `base` are separate because `base` is legitimately None — a fresh install
        that has never been on a preset still has its hand-tuned values remembered, against no
        base. Folding the two into one argument would silently stop recording exactly that case.
        """
        try:
            settings_mod.save(values)
            if remember:
                settings_mod.save_custom(values, base)
        except OSError as exc:
            return [f"could not save settings: {exc}"]
        return []

    def _settings_reply(self, errors):
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
        with self._lock:
            self.values = settings_mod.apply_custom(self.values)
            errors = self._save(self.values)
        return self._settings_reply(errors)

    def apply_preset(self, name):
        with self._lock:
            self.values = settings_mod.apply_preset(self.values, name)
            errors = self._save(self.values)
        return self._settings_reply(errors)

    # -- the map (E3) ---------------------------------------------------------------------------

    def map_view(self):
        """The map document for the current bundle, or what it is waiting for.

        The page needs the georeference as well as the picture, because everything it draws on
        top — a region square, a fast-travel pin — is in bundle metres and has to land on the
        right pixel.
        """
        values = self._values()
        bundle = values.get("bundle")
        if not bundle or not os.path.isfile(os.path.join(bundle, "bundle.json")):
            return {"ready": False, "waiting": "no bundle"}
        doc = self.caches.map_document(bundle)
        if doc is None:
            return {"ready": False, "waiting": "drawing"}
        return {
            "ready": True,
            "world": doc["world"],
            "suggestions": doc.get("suggestions") or [],
            "image": f"/api/map.png?t={self.token}&k={doc.get('key')}",
            "points": mappoints.load_pois(bundle),
            "region": values.get("region"),
        }

    def map_image(self):
        """The cached PNG's bytes, or None."""
        doc = self.caches.map_document_now(self._values().get("bundle"))
        if not doc:
            return None
        try:
            with open(doc["image"], "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def snap_point(self, at):
        bundle = self._values().get("bundle")
        if not bundle or not at or len(at) < 2:
            return {"at": list(at or [0, 0])[:2], "snapped": False}
        return mappoints.snap(bundle, at)

    def save_points(self, points):
        """Store the user's fast-travel points and point `poi_file` at them.

        The setting keeps its name and its meaning — a path to a JSON list — so a headless build
        is unchanged and `--set poi_file=...` still overrides. What the app takes over is the
        writing of it (plan §3.7). An empty list clears the setting rather than leaving it
        pointing at an empty file, which the build would read as "you asked for none".
        """
        bundle = self._values().get("bundle")
        if not bundle:
            return {"ok": False, "error": "no bundle"}
        path, clean = mappoints.save_pois(bundle, points)
        payload = self.update_settings({"poi_file": path if clean else None})
        payload["points"] = clean
        return payload

    # -- the producer half -----------------------------------------------------------------------

    def mod_state(self):
        """What the app can tell the user about the producer half: is the ripper mod installed,
        and can this build install it for them (D4.3 — the release exe ships the zip beside
        itself; a source checkout does not)."""
        from .. import modinstall

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
        from .. import modinstall

        try:
            target = modinstall.install()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "mod": self.mod_state()}
        return {"ok": True, "dir": target, "mod": self.mod_state()}

    # -- build ---------------------------------------------------------------------------------

    def start_build(self):
        values = self._values()
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
        self.caches.forget_plan()
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
            state["stages"] = self.caches.planned_stages(self._values())
        return state

    def report(self):
        """The report of the last build in this session; before any build, the report file left
        in the level folder by an earlier session (never a previous build's file for a build
        that died without writing one - the worker removes it at start)."""
        state = self.runner.state()
        if state.get("report") or state["status"] != "idle":
            return state.get("report")
        values = self._values()
        path = os.path.join(values.get("output") or "", values.get("level_name") or "", REPORT_NAME)
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def open_path(self, what):
        values = self._values()
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
