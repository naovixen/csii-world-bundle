"""Stage `bake` — run `csiibundle.bake` for the products this build needs, skipping fresh ones.

The engine-neutral half of the build. Which products run follows the settings (no markings →
no `markings` product, no furniture → no `lanegeometry`, …); `--force` is never implied — the
bake's own per-product staleness decides, so a second build of the same bundle spends nothing
here. Progress: every product's phases arrive through the same `Progress` object, so the UI
shows "bake › markings: drape …" with its own bar.
"""

import re
import time

from csiibundle.bake import runner as bake_runner
from csiibundle.bake.progress import Cancelled, Progress

from ..pipeline.bakeopts import bake_options
from . import Stage

_START = re.compile(r"^(\w+): .*, baking$")
_SKIP = re.compile(r"^(\w+): fresh \(.*\), skipped$")


class _StageProgress(Progress):
    """Forwards everything to the worker's progress and, from the bake's own product-start
    lines and `<product>: <phase>` names, reports a stage-level fraction and remaining time
    weighted by the census's per-product estimates (fresh products weigh 0). Within a product
    the fraction advances with wall-clock against that product's estimate, capped at 95 %, so
    a slow product cannot claim it is done before it is."""

    def __init__(self, inner, weights):
        self.inner = inner
        self.weights = {k: max(0.0, float(v or 0.0)) for k, v in weights.items()}
        self.total = sum(self.weights.values()) or 1.0
        self.done = set()
        self.current = None
        self.current_started = None

    # -- product tracking ----------------------------------------------------------------------

    def _enter(self, name):
        if name == self.current or name not in self.weights:
            return
        if self.current is not None:
            self.done.add(self.current)
        self.current, self.current_started = name, time.monotonic()
        self._report()

    def _report(self):
        done_w = sum(self.weights[n] for n in self.done)
        cur_w = self.weights.get(self.current, 0.0) if self.current else 0.0
        elapsed = (time.monotonic() - self.current_started) if self.current_started else 0.0
        within = min(0.95, elapsed / cur_w) if cur_w > 0 else 0.0
        fraction = min(1.0, (done_w + cur_w * within) / self.total)
        remaining = max(0.0, self.total - done_w - cur_w * within)
        if hasattr(self.inner, "stage_progress"):
            self.inner.stage_progress(fraction, remaining)

    def finish(self):
        self.done.update(self.weights)
        self.current = None
        self._report()

    # -- Progress protocol, forwarded --------------------------------------------------------

    def begin(self, name, total=None, unit="items"):
        head = str(name).split(":", 1)[0].strip()
        self._enter(head)
        self.inner.begin(name, total=total, unit=unit)

    def advance(self, n=1):
        self.inner.advance(n)
        self._report()

    def set_total(self, total):
        self.inner.set_total(total)

    def log(self, message):
        text = str(message)
        m = _START.match(text)
        if m:
            self._enter(m.group(1))
        m = _SKIP.match(text)
        if m and m.group(1) in self.weights:
            self.done.add(m.group(1))
            self._report()
        self.inner.log(message)

    def end(self, summary=None):
        self.inner.end(summary)
        self._report()

    def cancelled(self):
        return self.inner.cancelled()


def _wanted_products(settings):
    wanted = ["materials"]
    if settings.get("generate_lods", True):
        wanted.append("lods")
    if settings.get("markings", True):
        wanted.append("markings")
    if settings.get("lane_furniture", True):
        wanted.append("lanegeometry")
    if settings.get("areas", True):
        wanted.append("areas")
    if settings.get("skirts", True):
        wanted.append("skirts")
    wanted.append("terrainmask")
    if settings.get("terrain_cut", True):
        wanted.append("terraincut")
    return wanted


def enabled(settings):
    return True


def units(census, settings):
    total = 0
    for name in _wanted_products(settings):
        est = (census or {}).get("estimates", {}).get(f"bake/{name}") or {}
        total += est.get("units") or 0
    return total or None


def run(ctx, progress):
    settings = ctx.settings
    only = _wanted_products(settings)
    options = bake_options(settings, jobs=ctx.jobs)
    estimates = (ctx.census or {}).get("estimates", {})
    weights = {name: (estimates.get(f"bake/{name}") or {}).get("seconds") for name in only}
    tracker = _StageProgress(progress, weights)
    outcomes = bake_runner.run(ctx.bundle, derived_dir=ctx.derived_dir, options=options, only=only,
                               force=False, progress=tracker)
    if all(o.status in ("baked", "fresh") for o in outcomes):
        tracker.finish()                               # a cancelled/failed bake must not read as 100 %
    summary = {o.name: {"status": o.status, "reason": o.reason, "seconds": round(o.seconds, 1),
                        "stats": {k: v for k, v in (o.stats or {}).items() if k != "_tiles"}} for o in outcomes}
    if any(o.status == "cancelled" for o in outcomes):
        raise Cancelled()                              # the bake reports a cancel as an outcome; the stage is cancelled
    failed = [o.name for o in outcomes if o.status == "failed"]
    if failed:
        raise RuntimeError(f"bake products failed: {failed}")
    # rates are learnt per product (a "bake" rate over mixed units would be meaningless), and
    # only from full-city builds: a region bake pairs region-limited seconds with city-wide
    # census units and would teach a rate several times too fast
    learnt = {}
    if ctx.region is None:
        for o in outcomes:
            u = (estimates.get(f"bake/{o.name}") or {}).get("units")
            if o.status == "baked" and u and o.seconds > 0:
                learnt[f"bake/{o.name}"] = (u, o.seconds)
    return {"products": summary, "_history": learnt}


STAGE = Stage("bake", "Bake derived layers", enabled, units, run)
