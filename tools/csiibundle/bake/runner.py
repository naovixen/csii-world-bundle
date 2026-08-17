"""The bake driver: decide what is stale, run those products, record what happened.

`run()` is the one entry point both the CLI (`__main__`) and an application call. It:

1. quick-checks the bundle (`manifest.quick_check`),
2. loads or creates `derived/derived.json`,
3. asks `manifest.product_status` which requested products are stale (all, with `force`),
4. runs each stale product through a `Context` and the caller's `Progress`,
5. records fingerprints + stats and saves the manifest after EVERY product, so a cancel or a
   crash leaves the finished products fresh and the unfinished one absent.

Products see a `Context`: the bundle directory, its parsed manifest, the derived directory, the
options dict, and helpers to resolve layer paths and to build the tile grid. They never touch
`derived.json` themselves.
"""

import os
import time
from collections import namedtuple

from .. import reader
from . import BAKE_VERSION, products as _registry
from . import manifest as _manifest
from .progress import Cancelled, Progress

# The options a bake is shaped by, with the defaults every product may rely on. `region` is
# None (whole bundle) or {"centerX", "centerZ", "radius"} in bundle-frame metres.
DEFAULT_OPTIONS = {
    "tile": 256.0,
    "diagonal": "envelope",
    "region": None,
    "jobs": 1,
    # Which §8 raster is "the ground": "fine" = layers.terrainFine (1.75 m) when the bundle has
    # it, "base" = the 3.5 m heightmap. A consumer that feeds its engine the 4096 base map must
    # bake against it too, or the paint floats over / sinks under a ground the level does not have.
    "ground": "fine",
}


Context = namedtuple("Context", "bundle_dir manifest derived options layer_path")


class Outcome(namedtuple("Outcome", "name status reason seconds stats")):
    """What happened to one product this run: status is fresh / baked / failed / cancelled /
    absent-unknown."""


def make_context(bundle_dir, derived, options):
    manifest = reader.load_manifest(bundle_dir)

    def layer_path(key):
        return reader.layer_path(bundle_dir, key, manifest)

    return Context(os.path.abspath(bundle_dir), manifest, derived, dict(options), layer_path)


def plan(bundle_dir, derived, options, only=None, force=False, registry=None):
    """`[(product, status, reason)]` in bake order for the requested products."""
    registry = registry if registry is not None else _registry()
    names = list(registry) if not only else [n for n in only if n in registry]
    unknown = [n for n in (only or []) if n not in registry]
    if unknown:
        raise KeyError(f"unknown product(s) {unknown}; known: {sorted(registry)}")
    document = _manifest.load(derived)
    result = []
    for name in names:
        product = registry[name]
        if force:
            status, reason = "stale", "forced"
        else:
            status, reason = _manifest.product_status(document, name, product.inputs, bundle_dir, options,
                                                      version=product.version, option_keys=product.options)
        result.append((product, status, reason))
    return result


def run(bundle_dir, derived_dir=None, options=None, only=None, force=False, progress=None,
        registry=None, check_only=False):
    """Bake what is stale. Returns `[Outcome]`, one per requested product.

    `progress` implements `progress.Progress` (default: silent). `check_only` reports statuses
    without baking anything. Raises `ValueError` when the bundle fails the quick check.
    """
    progress = progress or Progress()
    options = dict(DEFAULT_OPTIONS, **(options or {}))
    bundle_dir = os.path.abspath(bundle_dir)
    derived = _manifest.derived_dir(bundle_dir, derived_dir)

    problems = _manifest.quick_check(bundle_dir)
    if problems:
        raise ValueError("bundle failed the quick check:\n  " + "\n  ".join(problems))

    todo = plan(bundle_dir, derived, options, only=only, force=force, registry=registry)
    outcomes = []
    if check_only:
        for product, status, reason in todo:
            outcomes.append(Outcome(product.name, status, reason, 0.0, None))
        return outcomes

    document = _manifest.load(derived)
    if document is None or document.get("bakeVersion") != BAKE_VERSION \
            or not _same_shaping(document.get("options", {}), options):
        # A different bake version or shaping options is a different cache: start over rather
        # than mixing products baked under two regimes.
        document = _manifest.new_document(bundle_dir, options)
    else:
        document["bundle"] = _manifest.bundle_identity(bundle_dir)
        document["options"] = dict(options)
    _manifest.save(derived, document)

    context = make_context(bundle_dir, derived, options)
    for product, status, reason in todo:
        if status == "fresh":
            progress.log(f"{product.name}: fresh ({reason}), skipped")
            outcomes.append(Outcome(product.name, "fresh", reason, 0.0, None))
            continue
        progress.log(f"{product.name}: {status} ({reason}), baking")
        started = time.monotonic()
        # Fingerprint BEFORE baking, so a layer that changes under us is caught as stale next
        # time rather than being recorded as if it had been read in its new state.
        inputs = _manifest.fingerprint_layers(bundle_dir, product.inputs, context.manifest)
        try:
            stats = product.run(context, progress) or {}
        except Cancelled:
            outcomes.append(Outcome(product.name, "cancelled", "cancelled by caller",
                                    time.monotonic() - started, None))
            _manifest.save(derived, document)
            break
        except Exception as exc:  # noqa: BLE001 - one product failing must not lose the others
            progress.log(f"{product.name}: FAILED: {exc!r}")
            outcomes.append(Outcome(product.name, "failed", repr(exc), time.monotonic() - started, None))
            _manifest.save(derived, document)
            continue
        seconds = time.monotonic() - started
        tiles = stats.pop("_tiles", None) if isinstance(stats, dict) else None
        _manifest.record_product(document, product.name, product.path, inputs, seconds,
                                 tiles=tiles, stats=stats or None, version=product.version,
                                 options={k: options.get(k) for k in product.options})
        _manifest.save(derived, document)
        outcomes.append(Outcome(product.name, "baked", reason, seconds, stats))
    return outcomes


def _same_shaping(recorded, wanted):
    return all(recorded.get(k) == wanted.get(k) for k in _manifest.SHAPING_OPTIONS)
