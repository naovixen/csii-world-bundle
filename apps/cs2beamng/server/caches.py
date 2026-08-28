"""The expensive things the settings page needs, computed once and kept.

Five answers derive from the bundle and the current settings, and none of them can be computed
on a request thread: the mesh index is a 20 s pass over the instance layer, the map is ~16 s of
rasters and a walk of the lane layer, the census and the cost estimate are seconds, and the stage
plan is served on every state tick. So each is memoised, three of them behind a background
thread, and each returns "what it is waiting for" rather than blocking.

`BundleCaches` holds them and nothing else. It was nine mutable fields on `AppServer` beside the
runner, the token and the window hooks, which meant every one of those fields was reachable from
every method and the invalidation rules were spread across the class. Here the rules sit with the
data: a re-rip into the same folder invalidates the index, a new index invalidates the estimate,
and a settings change invalidates the plan.
"""

import json
import os
import threading
import time

from .. import settings as settings_mod
from ..pipeline import census as census_mod
from ..pipeline.runner import Runner
from . import mapstore, mapview

INDEX_RECHECK_SECONDS = 5.0

#: The settings a census depends on. Anything else moving leaves the counts alone, so the census
#: survives a change of level name or texture size.
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


class BundleCaches:
    """Memoised derived data for one app. Every method is safe to call from a request thread."""

    def __init__(self, stopped):
        self.stopped = stopped  # set when the server is shutting down; cancels the slow scans
        self._lock = threading.RLock()
        self._census = None
        self._census_key = None
        self._planned = None
        self._estimate = None  # (settings fingerprint, index identity) -> the estimate
        self._index = None  # the mesh index for the current bundle, once scanned
        self._index_bundle = None
        self._index_thread = None
        self._index_checked = 0.0  # when the in-memory index was last re-validated
        self._warming = None  # the thread resolving a bundle's texture plan
        self._map = None  # the map view document for the current bundle
        self._map_bundle = None
        self._map_thread = None

    # -- the census ------------------------------------------------------------------------------

    def census(self, values, force=False):
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

    def forget_plan(self):
        """A build has started, so the plan the Build card was showing is no longer the next one."""
        with self._lock:
            self._planned = None

    def planned_stages(self, values):
        """Before the first build: the stage list the current settings would run, with the
        history-based estimate per stage, so the Build card is never an empty box. Memoised on
        (settings, census identity): it is served on every state tick."""
        with self._lock:
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

    # -- the cost estimate -----------------------------------------------------------------------

    def estimate(self, values):
        """The cost estimate for these settings, memoised on them and on the mesh index.

        Two pieces of it are too slow for a request thread and both are done on a background
        one, with the meter saying what it is waiting for meanwhile: the mesh index is a 20 s
        pass over the instance layer (once per bundle), and the texture plan is ~1 s of path
        joins and PNG headers (once per bundle, again whenever a recolouring setting moves).
        Everything else is 50 ms and runs here. A meter that blocked the settings page for
        twenty seconds would be a worse answer than no meter.
        """
        index = self.mesh_index(values.get("bundle"))
        key = (json.dumps(values, sort_keys=True, default=str), id(index))
        with self._lock:
            if self._estimate is not None and self._estimate[0] == key:
                return self._estimate[1]
        out = self._compute_estimate(values, index, key, warm_only=True)
        if out.get("waiting") == "reading":
            self._in_background(self._compute_estimate, values, index, key)
        return out

    def _compute_estimate(self, values, index, key, warm_only=False):
        from ..pipeline import estimate as estimate_mod

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

    # -- the mesh index --------------------------------------------------------------------------

    def mesh_index(self, bundle):
        """The mesh index for this bundle: the stored one, else a background scan and None.

        The one held in memory is re-checked against the bundle's own bytes every few seconds
        rather than on every request - the check is a scandir of two directories, and a re-rip
        into the same folder has to invalidate it or the meter would go on describing the city
        that used to be there.
        """
        from ..pipeline import meshindex

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
        from ..pipeline import meshindex

        try:
            doc = meshindex.build(bundle, cancelled=self.stopped.is_set)
        except Exception:
            doc = None
        if doc is not None:
            with self._lock:
                self._index, self._index_bundle = doc, bundle
                self._index_checked = time.monotonic()
                self._estimate = None

    # -- the map ---------------------------------------------------------------------------------

    def map_document(self, bundle):
        """The map document for this bundle, or None while it is being drawn.

        Drawn once per bundle on a background thread (~16 s: a 4,096 heightmap, the splat, and a
        pass over the lane layer for the roads), then served from the cache.
        """
        with self._lock:
            if self._map is not None and self._map_bundle == bundle:
                return self._map
            running = self._map_thread is not None and self._map_thread.is_alive()
        if running:
            return None
        stored = mapstore.load(bundle)
        if stored is not None:
            with self._lock:
                self._map, self._map_bundle = stored, bundle
            return stored
        thread = threading.Thread(
            target=self._draw_map, args=(bundle,), daemon=True, name="cs2beamng-mapview"
        )
        with self._lock:
            self._map_thread = thread
        thread.start()
        return None

    def _draw_map(self, bundle):
        try:
            doc = mapview.build(bundle, cancelled=self.stopped.is_set)
        except Exception:
            doc = None
        if doc is not None:
            with self._lock:
                self._map, self._map_bundle = doc, bundle

    def map_document_now(self, bundle):
        """The drawn map if there is one, in memory or on disk, and never a background scan."""
        with self._lock:
            doc = self._map if self._map_bundle == bundle else None
        return doc or (mapstore.load(bundle) if bundle else None)
