"""Stage `bake` — run `csiibundle.bake` for the products this build needs, skipping fresh ones.

The engine-neutral half of the build. Which products run follows the settings (no markings →
no `markings` product, no furniture → no `lanegeometry`, …); `--force` is never implied — the
bake's own per-product staleness decides, so a second build of the same bundle spends nothing
here. Progress: every product's phases arrive through the same `Progress` object, so the UI
shows "bake › markings: drape …" with its own bar.
"""

from csiibundle.bake import runner as bake_runner

from ..settings import region_of
from . import Stage


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
    categories = [c.strip() for c in str(settings.get("lane_furniture_categories") or "fence,cable,lane").split(",") if c.strip()]
    options = {
        "tile": float(settings.get("tile_size") or 256),
        "diagonal": "envelope",
        "region": region_of(settings),
        "jobs": ctx.jobs,
        "lane_parts": categories,
        "paint_kinds": ["tree"],
    }
    outcomes = bake_runner.run(ctx.bundle, derived_dir=ctx.derived_dir, options=options, only=only,
                               force=False, progress=progress)
    summary = {o.name: {"status": o.status, "reason": o.reason, "seconds": round(o.seconds, 1),
                        "stats": {k: v for k, v in (o.stats or {}).items() if k != "_tiles"}} for o in outcomes}
    failed = [o.name for o in outcomes if o.status == "failed"]
    if failed:
        raise RuntimeError(f"bake products failed: {failed}")
    return {"products": summary}


STAGE = Stage("bake", "Bake derived layers", enabled, units, run)
