"""`level_state.json` — what each stage was built from, so an unchanged stage is skipped.

A build is 25 minutes and a *no-op rebuild* used to be 14 of them, because no stage knew
whether its inputs had changed. The bake solved the same problem years earlier for its own
products (`csiibundle.bake.manifest`: per-product input fingerprints + declared options), and
this module is that machinery pointed at the application's stages. It reuses
`manifest.fingerprint_path` / `fingerprint_layers` rather than growing a second fingerprint
format.

## What a stage declares

`Stage` (see `..stages`) carries four optional fields beside `enabled` / `units` / `run`:

* `inputs` — what it reads: `"layer:<manifestKey>"` for a bundle layer, `"derived:<relpath>"`
  for a bake product. `None` (the default) means **not incremental**: the stage always runs.
* `settings_keys` — the settings whose values change its output.
* `outputs` — level-relative paths that must still exist for a skip to be legal (a user who
  deleted `art/shapes/` gets a rebuild, not a level referencing files that are gone).
* `shared_keys` — the `ctx.shared` entries it publishes for later stages; a skipped stage
  replays them, so `instances` still sees `shared["shapes"]` when `shapes` was skipped.

## What a skip has to restore

Skipping is not just "don't do the work": every stage appends to the one `beamng.level.Level`
and each writes the accumulated object tree, so a skipped stage whose objects vanished would
ship a level missing 14,658 statics. Two things are therefore cached per stage under
`cs2beamng_state/<stage>.state.json` and replayed on a skip:

1. the objects it added to the level tree (group path + the object dict, persistentId included);
2. the delta it made to `ctx.shared` — a union for a set, a recursive merge for a dict, an
   assignment otherwise, so three stages accumulating into `materials_used` compose in any
   mixture of run and skipped.

## The failure mode this must not have

**A silent stale skip is worse than a slow build.** `paint_kinds` cost a 25-minute build on
2026-08-18 by being read but not declared, so three guards stand here:

* the worker wraps `ctx.settings` in `Recorder` while a stage runs and refuses to record state
  for a stage that read a key it did not declare (it re-runs next build, loudly);
* `tools/test_app_shell.py` fails the build's own test suite on the same condition, statically;
* a stage that raises is never recorded, so a failure always re-runs.

`rebuild` (the setting) and `--force` bypass the whole mechanism.
"""

import hashlib
import json
import os
import time

from csiibundle.bake import manifest as bake_manifest

STATE_NAME = "level_state.json"
STATE_DIR = "cs2beamng_state"
SCHEMA = "cs2beamng.level_state/1"

# Bumped when the state format or the replay contract changes; every stage is stale across it.
STATE_VERSION = 1


# -- the document --------------------------------------------------------------------------------

def state_path(level_dir):
    return os.path.join(level_dir, STATE_NAME)


def load(level_dir):
    """`level_state.json` as a dict, or None when there is none (or it is unreadable)."""
    try:
        with open(state_path(level_dir), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("stateVersion") != STATE_VERSION:
        return None
    return document


def new_document(settings):
    return {"schema": SCHEMA, "stateVersion": STATE_VERSION, "level": settings.get("level_name"),
            "bundle": settings.get("bundle"), "generatedAt": _now(), "stages": {}}


def save(level_dir, document):
    document["generatedAt"] = _now()
    os.makedirs(level_dir, exist_ok=True)
    tmp = state_path(level_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=1, sort_keys=True, default=str)
        handle.write("\n")
    os.replace(tmp, state_path(level_dir))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- signatures ----------------------------------------------------------------------------------

def incremental(stage):
    """Whether this stage declared enough to be skippable at all."""
    return getattr(stage, "inputs", None) is not None


def signature(stage, ctx, manifest=None, document=None):
    """`{version, settings, inputs, after}` for one stage against the bundle as it is right now.

    Fingerprints are the bake's (size + mtime for a file; count + bytes + newest mtime + a hash
    of the sorted names for a directory) — cheap on purpose, because "is it fresh?" runs before
    every stage of every build.
    """
    settings = {key: _plain(ctx.settings.get(key)) for key in (stage.settings_keys or ())}
    inputs = {}
    layer_keys = [ref.split(":", 1)[1] for ref in (stage.inputs or ()) if ref.startswith("layer:")]
    if layer_keys:
        try:
            fps = bake_manifest.fingerprint_layers(ctx.bundle, layer_keys, manifest)
        except (OSError, ValueError):
            fps = {key: {"kind": "unreadable"} for key in layer_keys}
        for key in layer_keys:
            inputs["layer:" + key] = fps.get(key, {"kind": "missing"})
    for ref in (stage.inputs or ()):
        if ref.startswith("derived:"):
            inputs[ref] = bake_manifest.fingerprint_path(os.path.join(ctx.derived_dir, ref.split(":", 1)[1]))
        elif ref == "level:":
            inputs[ref] = level_fingerprint(ctx.level_dir)
        elif not ref.startswith("layer:"):
            raise ValueError(f"{stage.key}: input {ref!r} must start with 'layer:', 'derived:' or be 'level:'")
    # An upstream stage this one consumes: its signature IS the input. `materials` exports only
    # what `shapes`/`networks`/`derived` bound, and none of that shows up in its own layers.
    after = {}
    for key in (stage.after or ()):
        entry = ((document or {}).get("stages") or {}).get(key) or {}
        after[key] = entry.get("signature")
    return {"version": int(getattr(stage, "version", 1) or 1), "settings": settings, "inputs": inputs,
            "after": after}


# What the build writes about ITSELF rather than into the level: excluded from `level:` so the
# validator's own input does not change on every build just by being reported on.
LEVEL_FINGERPRINT_SKIP = (STATE_NAME, "cs2beamng_report.json")


def level_fingerprint(level_dir):
    """A fingerprint of everything the build put in the level folder — the input of the one
    stage that reads the whole level rather than the bundle (`validate`).

    Same shape as the bake's directory fingerprint (count + bytes + newest mtime + a hash of the
    sorted names), which is what makes "did anything about this level move?" a stat walk rather
    than a re-read of 5 GiB. `.dae`/texture writes are content-compared, so an unchanged build
    leaves every mtime alone and this compares equal.
    """
    count = total = newest = 0
    names = []
    base = os.path.abspath(level_dir)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != STATE_DIR]
        for name in files:
            if root == base and name in LEVEL_FINGERPRINT_SKIP:
                continue
            if name.endswith(".tmp"):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total += int(st.st_size)
            newest = max(newest, int(st.st_mtime_ns))
            names.append(os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/"))
    digest = hashlib.sha256(chr(10).join(sorted(names)).encode("utf-8")).hexdigest()
    return {"kind": "level", "count": count, "bytes": total, "maxMtimeNs": newest, "names": digest}


def _plain(value):
    """Settings values are JSON scalars already; a stray tuple/set would not compare equal after
    a round trip, so normalise the two containers that can occur."""
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def check(document, stage, sig, level_dir):
    """`(fresh, reason)` for one stage: the recorded signature against `sig`, plus the stage's
    declared outputs still being on disk."""
    if document is None:
        return False, "no level state"
    entry = (document.get("stages") or {}).get(stage.key)
    if not entry:
        return False, "never recorded"
    recorded = entry.get("signature") or {}
    if recorded.get("version") != sig["version"]:
        return False, f"stage version {recorded.get('version')} != {sig['version']}"
    for key, value in sig["settings"].items():
        if (recorded.get("settings") or {}).get(key) != value:
            return False, f"setting {key!r} changed"
    for key in (recorded.get("settings") or {}):
        if key not in sig["settings"]:
            return False, f"setting {key!r} no longer declared"
    for key, value in sig["inputs"].items():
        if (recorded.get("inputs") or {}).get(key) != value:
            return False, f"input {key!r} changed"
    for key in (recorded.get("inputs") or {}):
        if key not in sig["inputs"]:
            return False, f"input {key!r} no longer declared"
    if (recorded.get("after") or {}) != (sig.get("after") or {}):
        changed = sorted(k for k in set(recorded.get("after") or {}) | set(sig.get("after") or {})
                         if (recorded.get("after") or {}).get(k) != (sig.get("after") or {}).get(k))
        return False, f"upstream stage(s) {changed} re-ran"
    # Outputs are checked against what was actually there when the stage was recorded, not
    # against the declaration: `networks` writes no `art/shapes/networks/` on a bundle with no
    # network meshes, and demanding a directory the stage had no reason to create re-ran it on
    # every build. What this does catch is the case it is for — a user who deleted the folder.
    for rel in (entry.get("outputs") or ()):
        if not os.path.exists(os.path.join(level_dir, *rel.split("/"))):
            return False, f"output {rel!r} is missing"
    if not os.path.isfile(_payload_path(level_dir, stage.key)):
        return False, "no replay payload"
    return True, "inputs and settings unchanged"


# -- the replay payload --------------------------------------------------------------------------

def _payload_path(level_dir, stage_key):
    return os.path.join(level_dir, STATE_DIR, f"{stage_key}.state.json")


def write_payload(level_dir, stage_key, objects, shared_delta, result):
    """Everything a skip of this stage has to put back: its level objects, its `ctx.shared`
    delta, and the result dict its report entry carried."""
    path = _payload_path(level_dir, stage_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = {"schema": SCHEMA, "stage": stage_key, "objects": objects,
                "shared": encode(shared_delta), "result": encode(result)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def read_payload(level_dir, stage_key):
    try:
        with open(_payload_path(level_dir, stage_key), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def replay(level_dir, stage_key, level, shared):
    """Put a skipped stage's contribution back. Returns its recorded result dict, or None when
    the payload is unreadable — in which case the caller must run the stage."""
    document = read_payload(level_dir, stage_key)
    if not document:
        return None
    try:
        level.replay(stage_key, document.get("objects") or [])
        apply_shared(shared, decode(document.get("shared") or {}))
        return decode(document.get("result") or {})
    except (ValueError, KeyError, TypeError):
        return None


# -- `ctx.shared` deltas ---------------------------------------------------------------------------

def diff_shared(before, after, keys):
    """What this stage added to `ctx.shared`, per declared key.

    A set yields the members it gained, a dict the entries it added or changed (recursively),
    anything else its final value. That is what lets `materials_used` be accumulated by three
    stages and still compose when any subset of them is skipped.
    """
    out = {}
    for key in keys or ():
        if key not in after:
            continue
        out[key] = _diff(before.get(key), after.get(key))
    return out


def _diff(old, new):
    if isinstance(new, (set, frozenset)) and isinstance(old, (set, frozenset)):
        return set(new) - set(old)
    if isinstance(new, dict) and isinstance(old, dict):
        return {k: _diff(old.get(k), v) for k, v in new.items() if k not in old or old[k] != v}
    return new


def apply_shared(shared, delta):
    for key, value in (delta or {}).items():
        shared[key] = _merge(shared.get(key), value)


def _merge(old, new):
    if isinstance(new, (set, frozenset)):
        merged = set(old) if isinstance(old, (set, frozenset)) else set()
        merged.update(new)
        return merged
    if isinstance(new, dict) and isinstance(old, dict):
        out = dict(old)
        for k, v in new.items():
            out[k] = _merge(out.get(k), v)
        return out
    return new


# -- JSON codec for the shared payload -------------------------------------------------------------
#
# `ctx.shared` holds sets (`materials_used`) and dicts keyed by tuples (`shapes["results"]` is
# keyed by `(assetId, suffix, variant)`), neither of which survives JSON. Both are tagged with a
# `$`-prefixed marker; a real key starting with `$` is refused rather than silently mangled.

SET_TAG = "$set"
TUPLE_TAG = "$tuple"
PAIRS_TAG = "$pairs"


def encode(value):
    if isinstance(value, (set, frozenset)):
        return {SET_TAG: [encode(v) for v in sorted(value, key=_sort_key)]}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            for k in value:
                if k.startswith("$"):
                    raise ValueError(f"shared key {k!r} collides with the state codec's markers")
            return {k: encode(v) for k, v in value.items()}
        return {PAIRS_TAG: [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {TUPLE_TAG: [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if SET_TAG in value and len(value) == 1:
            return {decode(v) for v in value[SET_TAG]}
        if TUPLE_TAG in value and len(value) == 1:
            return tuple(decode(v) for v in value[TUPLE_TAG])
        if PAIRS_TAG in value and len(value) == 1:
            return {decode(k): decode(v) for k, v in value[PAIRS_TAG]}
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def _sort_key(value):
    return (type(value).__name__, repr(value))


# -- the recording settings view --------------------------------------------------------------------

class Recorder(dict):
    """`ctx.settings` while a stage runs: a plain dict that remembers which keys were read.

    A dict subclass rather than a wrapper because the settings object is handed straight to
    helpers (`beamng.placement.admit`, `settings.region_of`) and pickled into worker pools; it
    has to behave exactly like the dict it replaces. It catches what a static source scan cannot:
    `density_fraction` reads `foliage_density` through a variable, and `derived` reads its five
    product toggles through one.
    """

    def __init__(self, values):
        super().__init__(values)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __reduce__(self):
        return (dict, (dict(self),))          # a pool worker gets a plain dict, not a recorder


# Settings that cannot make a RECORDED stage stale, so reading one undeclared is not a fault:
# `level_name`/`bundle`/`output` decide which level directory — and therefore which
# `level_state.json` — is even consulted, and the rest are process knobs a stage's output does
# not depend on. `tools/test_app_shell.py` imports this same set, so the static gate and the
# runtime recorder cannot drift apart (they did on the first full build: the materials stage
# reads `level_name` to name a paint-variant material, the static gate exempted it, the recorder
# did not, and the stage could never have been skipped).
NOT_FIDELITY_SETTINGS = frozenset({
    "level_name", "level_title", "bundle", "output", "jobs", "rebuild", "cook", "keep_partial",
    "beamng_exe", "quality_preset", "ui_browser",
})


def undeclared(recorder, stage, known):
    """The settings this stage actually read that it did not declare, restricted to real
    settings keys (a stage may probe a key that is not in the schema; that is not a fidelity
    input) and minus `NOT_FIDELITY_SETTINGS`. Sorted, so a report line is stable."""
    read = (set(recorder.read) & set(known)) - NOT_FIDELITY_SETTINGS
    return sorted(read - set(stage.settings_keys or ()))
