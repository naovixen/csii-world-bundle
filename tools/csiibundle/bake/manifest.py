"""`derived/derived.json` — what was baked, from what, and is it still fresh (contract §13).

The whole point of this file is the per-product **fingerprint** of every input layer. A bundle
is mixed-vintage by design: a partial re-rip refreshes some layers and leaves others alone, so
"the bundle's `generatedAt` changed" says nothing about whether the terrain-derived products
need re-baking. Comparing each product's recorded input fingerprints against the layers on disk
does. Fingerprints are cheap on purpose (size + mtime for a file; count + bytes + newest mtime +
a hash of the sorted names for a directory) — hashing 4.7 GiB of content on every check would
make "is it fresh?" cost minutes.

Two ways to be stale that fingerprints do not cover, and that this module also checks:
`bakeVersion` (the bake's own outputs changed shape) and `options` (a bake at another tile size
or region is a different cache). Both are compared exactly.
"""

import datetime as _dt
import hashlib
import json
import os

from .. import reader
from . import BAKE_VERSION

DERIVED_DIRNAME = "derived"
DERIVED_MANIFEST = "derived.json"


# -- location -----------------------------------------------------------------------------------

def derived_dir(bundle_dir, override=None):
    """Where the derived cache lives: `<bundle>/derived/` unless the caller redirected it."""
    return os.path.abspath(override) if override else os.path.join(os.path.abspath(bundle_dir), DERIVED_DIRNAME)


def manifest_path(derived):
    return os.path.join(derived, DERIVED_MANIFEST)


# -- fingerprints -------------------------------------------------------------------------------

def fingerprint_path(path):
    """A `fingerprint` object (§13 / derived.schema.json) for a file or directory on disk."""
    if os.path.isfile(path):
        st = os.stat(path)
        return {"kind": "file", "size": int(st.st_size), "mtimeNs": int(st.st_mtime_ns)}
    if os.path.isdir(path):
        count = 0
        total = 0
        newest = 0
        names = []
        base = os.path.abspath(path)
        for root, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                count += 1
                total += int(st.st_size)
                newest = max(newest, int(st.st_mtime_ns))
                names.append(os.path.relpath(full, base).replace(os.sep, "/"))
        digest = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()
        return {"kind": "dir", "count": count, "bytes": total, "maxMtimeNs": newest, "names": digest}
    return {"kind": "missing"}


def fingerprint_layers(bundle_dir, layer_keys, manifest=None):
    """`{layerKey: fingerprint}` for the given manifest layer keys, resolved through the
    manifest (never hard-coded paths, §1)."""
    if manifest is None:
        manifest = reader.load_manifest(bundle_dir)
    result = {}
    for key in layer_keys:
        try:
            path = reader.layer_path(bundle_dir, key, manifest)
        except KeyError:
            result[key] = {"kind": "missing"}
            continue
        result[key] = fingerprint_path(path)
    return result


def bundle_identity(bundle_dir):
    """The `bundle` block: sha256 of `bundle.json` bytes plus its own generatedAt/generator/city."""
    path = os.path.join(bundle_dir, reader.MANIFEST_NAME)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no {reader.MANIFEST_NAME} in {bundle_dir}")
    with open(path, "rb") as handle:
        raw = handle.read()
    document = json.loads(raw.decode("utf-8"))
    block = {"sha256": hashlib.sha256(raw).hexdigest(),
             "generatedAt": str(document.get("generatedAt", ""))}
    if document.get("generator"):
        block["generator"] = str(document["generator"])
    if document.get("city"):
        block["city"] = str(document["city"])
    return block


# -- the document -------------------------------------------------------------------------------

def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(derived):
    """The derived manifest as a dict, or None when the cache does not exist."""
    path = manifest_path(derived)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def new_document(bundle_dir, options):
    """A fresh manifest with no products."""
    return {
        "bakeVersion": BAKE_VERSION,
        "csiibundleSchema": _schema_version(),
        "generatedAt": now_iso(),
        "bundle": bundle_identity(bundle_dir),
        "options": dict(options),
        "products": {},
    }


def save(derived, document):
    os.makedirs(derived, exist_ok=True)
    document["generatedAt"] = now_iso()
    tmp = manifest_path(derived) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, manifest_path(derived))


def record_product(document, name, path, inputs, seconds, tiles=None, stats=None, version=1, options=None):
    """Write one product's entry into `document["products"]`."""
    entry = {"path": path, "generatedAt": now_iso(), "seconds": round(float(seconds), 3), "inputs": inputs,
             "version": int(version)}
    if options:
        entry["options"] = dict(options)
    if tiles is not None:
        entry["tiles"] = [[int(a), int(b)] for a, b in tiles]
    if stats:
        entry["stats"] = stats
    document["products"][name] = entry
    return entry


# -- staleness ----------------------------------------------------------------------------------

# Options whose change makes EVERY product a different cache. Others (e.g. jobs) do not, and
# `region` is deliberately not one of them any more (2026-08-18): it was, and so a build that
# crossed the region/full-city boundary re-baked `materials` too — 7.5 minutes of work on a
# product that walks all materials and all instances and has never read `region` in its life.
# A product that DOES read it declares it in its own `Product.options`, and the gate in
# `tools/test_bake_manifest.py` fails the suite if one reads it without declaring it.
SHAPING_OPTIONS = ("tile", "diagonal")


def _same_options(recorded, wanted):
    for key in SHAPING_OPTIONS:
        if recorded.get(key) != wanted.get(key):
            return False
    return True


def product_status(document, name, product_inputs, bundle_dir, wanted_options, manifest=None, version=1,
                   option_keys=()):
    """One of 'fresh', 'stale', 'absent' for a product, with a reason string.

    `product_inputs` are the layer keys the product reads (its registry entry). A product is
    stale when the bake version differs, a shaping option differs, the bundle manifest bytes
    changed AND any input fingerprint differs, or any input fingerprint differs on its own.
    (The bundle sha alone is not decisive: a partial re-rip rewrites `bundle.json` even when
    this product's inputs are untouched.)
    """
    if document is None or name not in document.get("products", {}):
        return "absent", "not baked"
    if document.get("bakeVersion") != BAKE_VERSION:
        return "stale", f"bakeVersion {document.get('bakeVersion')} != {BAKE_VERSION}"
    if not _same_options(document.get("options", {}), wanted_options):
        return "stale", "options differ"
    recorded_version = document["products"][name].get("version", 1)
    if recorded_version != version:
        return "stale", f"product version {recorded_version} != {version}"
    recorded_opts = document["products"][name].get("options", {})
    for key in option_keys:
        if recorded_opts.get(key) != wanted_options.get(key):
            return "stale", f"option {key!r} changed ({recorded_opts.get(key)!r} -> {wanted_options.get(key)!r})"
    recorded = document["products"][name].get("inputs", {})
    current = fingerprint_layers(bundle_dir, product_inputs, manifest)
    for key in product_inputs:
        if recorded.get(key) != current.get(key):
            return "stale", f"input {key!r} changed"
    return "fresh", "inputs unchanged"


def status_report(document, registry, bundle_dir, wanted_options):
    """`{name: (status, reason)}` for every registered product."""
    manifest = reader.load_manifest(bundle_dir)
    return {name: product_status(document, name, prod.inputs, bundle_dir, wanted_options, manifest, prod.version, prod.options)
            for name, prod in registry.items()}


# -- a fast sanity check on the bundle before baking -------------------------------------------

def quick_check(bundle_dir):
    """Cheap preconditions (a full `tools/validate_bundle.py` run takes minutes on a city and
    is the caller's choice): the manifest exists, parses, declares a schema this package reads,
    and every declared layer path exists. Returns a list of problems (empty = ok)."""
    problems = []
    manifest = reader.load_manifest(bundle_dir)
    if manifest is None:
        return [f"no {reader.MANIFEST_NAME} in {bundle_dir}"]
    schema = manifest.get("schema")
    if schema != _schema_version():
        problems.append(f"bundle schema {schema!r} but this bake reads {_schema_version()}")
    for key, rel in (manifest.get("layers") or {}).items():
        path = os.path.join(bundle_dir, rel)
        if not (os.path.isfile(path) or os.path.isdir(path)):
            problems.append(f"layer {key!r} declared at {rel!r} but nothing is there")
    return problems


def _schema_version():
    from .. import SCHEMA_VERSION
    return SCHEMA_VERSION
