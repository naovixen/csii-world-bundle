"""The census: what this bundle holds and what the build will cost, before anything runs.

Cheap by design (reads `bundle.json`, `derived/derived.json`, and stats file sizes): the
preflight screen shows it in under a second and the estimates it produces seed every stage's
ETA. `take()` returns a JSON-able dict:

    {"bundle": {path, city, generatedAt, schema, mapSize, counts, layers, sizeBytes},
     "derived": {"present": bool, "products": {name: {"status": fresh|stale|absent, "reason"}}},
     "region": {...} | None, "scale": fraction of the map the region covers (1.0 for all),
     "estimates": {stage: {"units": n, "seconds": s | None}},
     "totalSeconds": s | None, "warnings": [...]}
"""

import math
import os

from csiibundle import reader
from csiibundle.bake import manifest as bake_manifest
from csiibundle.bake import products as bake_products

from .. import settings as settings_mod
from . import history


_SIZE_CACHE = {}


def _bundle_size(bundle, layers):
    """Bytes on disk over the manifest's layers. 20k files on the reference bundle: a scandir
    walk with DirEntry.stat is ~0.1 s; cached per (bundle, bundle.json mtime) because the UI
    asks for a census after every settings change."""
    try:
        key = (bundle, os.path.getmtime(os.path.join(bundle, "bundle.json")))
    except OSError:
        key = (bundle, None)
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    total = 0
    for rel in layers.values():
        path = os.path.join(bundle, rel)
        try:
            if os.path.isdir(path):
                total += _dir_size(path)
            elif os.path.isfile(path):
                total += os.path.getsize(path)
        except OSError:
            pass
    _SIZE_CACHE.clear()
    _SIZE_CACHE[key] = total
    return total


def _dir_size(path):
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def take(values):
    bundle = values.get("bundle") or ""
    out = {"bundle": None, "derived": {"present": False, "products": {}}, "region": None, "scale": 1.0,
           "estimates": {}, "totalSeconds": None, "warnings": []}
    manifest = reader.load_manifest(bundle) if bundle else None
    if manifest is None:
        out["warnings"].append(f"no bundle.json at {bundle!r}")
        return out
    counts = dict(manifest.get("counts") or {})
    layers = dict(manifest.get("layers") or {})
    size_bytes = _bundle_size(bundle, layers)
    out["bundle"] = {"path": bundle, "city": manifest.get("city"), "generatedAt": manifest.get("generatedAt"),
                     "generator": manifest.get("generator"), "schema": manifest.get("schema"),
                     "mapSize": manifest.get("mapSize"), "counts": counts, "layers": sorted(layers),
                     "sizeBytes": size_bytes}
    region = settings_mod.region_of(values)
    out["region"] = region
    map_size = float(manifest.get("mapSize") or 14336.0)
    if region:
        out["scale"] = min(1.0, math.pi * region["radius"] ** 2 / (map_size * map_size))

    # derived cache freshness per product (cheap fingerprints)
    derived = bake_manifest.derived_dir(bundle)
    doc = bake_manifest.load(derived)
    out["derived"]["present"] = doc is not None
    if os.path.isdir(bundle):
        try:
            options = {"tile": float(values.get("tile_size") or 256), "diagonal": "envelope", "region": region}
            report = bake_manifest.status_report(doc, bake_products(), bundle, options)
            out["derived"]["products"] = {k: {"status": s, "reason": r} for k, (s, r) in report.items()}
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append(f"derived cache check failed: {exc!r}")

    # per-stage units and seconds
    scale = out["scale"]
    rates = history.load()
    units = {
        "bake/materials": counts.get("materials", 0),
        "bake/lods": counts.get("meshes", 0) if values.get("generate_lods", True) else 0,
        "bake/markings": counts.get("laneGeometry", 0) if values.get("markings", True) else 0,
        "bake/lanegeometry": counts.get("laneGeometry", 0) if values.get("lane_furniture", True) else 0,
        "bake/areas": counts.get("areas", 0) * scale if values.get("areas", True) else 0,
        "bake/skirts": counts.get("instances", 0) if values.get("skirts", True) else 0,
        "bake/terrainmask": 1,
        "terrain": 4096 * 4096 * (values.get("terrain_upsample", 1) or 1) ** 2,
        "water": 1 if values.get("water", True) else 0,
        "scene": counts.get("lanes", 0) or 1,
        "materials": counts.get("materials", 0),
        "shapes": counts.get("meshes", 0),
        "networks": counts.get("networkSegments", 0) * scale if values.get("networks", True) else 0,
        "derived": 3000 * scale,
        "instances": counts.get("instances", 0),
        "lights": 654 if values.get("lights") else 0,
        "validate": 1,
    }
    for key, u in units.items():
        status = out["derived"]["products"].get(key.split("/", 1)[1], {}).get("status") if key.startswith("bake/") else None
        if status == "fresh":
            u = 0
        seconds = history.estimate_seconds(key, u, rates) if u else 0.0
        out["estimates"][key] = {"units": u, "seconds": seconds}
    # the total counts only the stages that exist and are enabled, so the preflight number and
    # the Build card's stage list say the same thing
    from ..stages import all_stages  # noqa: WPS433 - lazy: stages import numpy-heavy code

    total = 0.0
    known = True
    for stage in all_stages():
        if not stage.enabled(values):
            continue
        stage_units = stage.units(out, values)
        if not stage_units:
            continue
        seconds = history.stage_estimate(stage.key, stage_units, out["estimates"], rates)
        if seconds is None:
            known = False
        else:
            total += seconds
    out["totalSeconds"] = total if known else None
    return out
