"""The replay payload and the `ctx.shared` deltas.

A stage that is skipped still has to hand the next one what it would have computed, so what it
put in `ctx.shared` is recorded as a delta and replayed."""

import json
import os

from .state import SCHEMA, decode, encode, merge_values, payload_path

# -- the replay payload --------------------------------------------------------------------------


def write_payload(level_dir, stage_key, objects, shared_delta, result):
    """Everything a skip of this stage has to put back: its level objects, its `ctx.shared`
    delta, and the result dict its report entry carried."""
    path = payload_path(level_dir, stage_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = {
        "schema": SCHEMA,
        "stage": stage_key,
        "objects": objects,
        "shared": encode(shared_delta),
        "result": encode(result),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def read_payload(level_dir, stage_key):
    try:
        with open(payload_path(level_dir, stage_key), encoding="utf-8") as handle:
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
        shared[key] = merge_values(shared.get(key), value)
