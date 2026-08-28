"""The incremental bookkeeping: what the last build left, and what this one must redo.

A second build is fast because it can tell what has not moved. This is where that is recorded
and read back — the manifest, the shared-payload snapshot, the settings the level was built
with, and the report."""

import json
import os
import shutil
import time

from csiibundle import reader

from ..settings import SCHEMA as SETTINGS_SCHEMA
from ..settings import app_dir
from ..stages import level_of
from . import state, statepayload

REPORT_NAME = "cs2beamng_report.json"

SETTINGS_NAME = "cs2beamng_settings.json"  # written first, never removed: see write_settings


def manifest(bundle_dir):
    """`bundle.json` read once per build; every stage's layer fingerprints resolve through it."""
    try:
        return reader.load_manifest(bundle_dir)
    except (OSError, ValueError):
        return None


def shared_snapshot(shared, keys):
    """A shallow copy of the declared `ctx.shared` entries before a stage runs, so its delta can
    be worked out afterwards. Sets and dicts are copied one level deep because stages mutate
    them in place (`materials_used.add(...)`)."""
    out = {}
    for key in keys or ():
        value = (shared or {}).get(key)
        if isinstance(value, (set, frozenset)):
            out[key] = set(value)
        elif isinstance(value, dict):
            out[key] = {
                k: (set(v) if isinstance(v, (set, frozenset)) else v) for k, v in value.items()
            }
        elif value is not None:
            out[key] = value
    return out


def record(level_dir, document, stage, sig, recorder, ctx, before, result, seconds, queue, report):
    """Record what this stage was built from — unless it read a setting it did not declare, in
    which case it must re-run next build rather than be trusted (that exact miss cost a
    25-minute build on 2026-08-18)."""
    missed = state.undeclared(recorder, stage, SETTINGS_SCHEMA)
    if missed:
        text = (
            f"{stage.key}: reads settings it does not declare {missed} - not recorded, so it re-runs "
            f"next build. Add them to the stage's settings_keys."
        )
        queue.put({"t": "log", "stage": stage.key, "text": text, "at": time.time()})
        report.setdefault("stateWarnings", []).append(text)
        return
    try:
        delta = statepayload.diff_shared(before, ctx.shared or {}, stage.shared_keys)
        statepayload.write_payload(
            level_dir, stage.key, level_of(ctx).stage_objects(stage.key), delta, result
        )
    except (OSError, ValueError, TypeError) as exc:
        text = (
            f"{stage.key}: could not write its replay payload ({exc!r}); it will re-run next build"
        )
        queue.put({"t": "log", "stage": stage.key, "text": text, "at": time.time()})
        report.setdefault("stateWarnings", []).append(text)
        return
    present = [
        rel
        for rel in (stage.outputs or ())
        if os.path.exists(os.path.join(level_dir, *rel.split("/")))
    ]
    document["stages"][stage.key] = {
        "signature": sig,
        "seconds": round(seconds, 1),
        "outputs": present,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# Folders `--force` / `rebuild` empties before starting. **Only on force** since A4: clearing
# `art/shapes/` on every build threw away every `.cdae` too, and BeamNG's own compiler is built
# to reuse them (`compileMeshes.lua`: "existing .cdae will be reused. Delete them beforehand if
# a clean state is required") — that one `rmtree` was the whole 6m26s cook on a rebuild that
# changed no geometry. The shape-writing stages now sweep their own orphans at the end instead,
# which is the pattern art/textures/ already used and whose comment explains why: an unchanged
# file keeps its mtime, so BeamNG keeps its cooked copy in temp/ (load #11 re-cooked all 260
# textures on every first load because the folder was cleared and rewritten each build).
GENERATED_DIRS = (("art", "shapes"),)


def clear_generated(level_dir, queue):
    for parts in GENERATED_DIRS:
        path = os.path.join(level_dir, *parts)
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                queue.put(
                    {
                        "t": "log",
                        "stage": "",
                        "text": f"cleared {'/'.join(parts)}/ from the previous build",
                        "at": time.time(),
                    }
                )
            except OSError as exc:
                queue.put(
                    {
                        "t": "log",
                        "stage": "",
                        "text": f"could not clear {'/'.join(parts)}/: {exc}",
                        "at": time.time(),
                    }
                )


def write_settings(level_dir, settings, queue):
    """`cs2beamng_settings.json` — every schema key this build ran with, written FIRST.

    The report carries the settings too, but it is written last and deleted at the start of the
    next build, so an interrupted or killed run leaves a level nobody can reproduce. It has
    happened twice: on 2026-08-19 the settings behind `cs2_city_fine` had to be reconstructed out
    of the per-stage results (terrain size, texture caps, collider counts), which recovers most of
    them and guesses the rest. This file is small, it is written before any stage runs, and it is
    never removed - a level folder now says what made it whatever else happens to the build.
    """
    try:
        os.makedirs(level_dir, exist_ok=True)
        recorded = {
            key: settings.get(key, default)
            for key, (default, _kind, _help) in SETTINGS_SCHEMA.items()
        }
        payload = {
            "schema": "cs2beamng.settings/1",
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "settings": recorded,
        }
        paths = [os.path.join(level_dir, SETTINGS_NAME)]
        # ...and a copy outside the level, so deleting or rebuilding the folder cannot take the
        # last record of it with them
        keep = os.path.join(app_dir(), "levels")
        os.makedirs(keep, exist_ok=True)
        paths.append(os.path.join(keep, f"{settings.get('level_name')}.json"))
        for path in paths:
            # Unchanged settings must not rewrite the file: `validate` fingerprints the level
            # FOLDER, so a new mtime here alone would re-run it on every no-op build.
            if _same_settings(path, recorded):
                continue
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=1, sort_keys=True, default=str)
    except OSError as exc:
        queue.put(
            {
                "t": "log",
                "stage": "system",
                "text": f"could not write {SETTINGS_NAME}: {exc}",
                "at": time.time(),
            }
        )


def _same_settings(path, recorded):
    try:
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(existing, dict) and existing.get("settings") == recorded


def remove_stale_report(level_dir):
    try:
        os.remove(os.path.join(level_dir, REPORT_NAME))
    except FileNotFoundError:
        pass
    except OSError:
        pass


def write_report(level_dir, report, queue):
    """The build report lives beside the level; the worker writes it last so every stage — the
    validator included — has its timing and result in it. A cancelled or failed build gets one
    too (with `status` saying so), which is what makes a partial level explainable later."""
    try:
        os.makedirs(level_dir, exist_ok=True)
        path = os.path.join(level_dir, REPORT_NAME)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=1, sort_keys=True, default=str)
    except OSError as exc:
        queue.put(
            {
                "t": "log",
                "stage": "system",
                "text": f"could not write {REPORT_NAME}: {exc}",
                "at": time.time(),
            }
        )


def merge_report(report, result):
    """Move a stage's `_report` delta into the report proper, and out of its result.

    A stage that writes straight into `ctx.report` loses that write the moment it is SKIPPED: the
    replay puts back its objects, its `shared` delta and its result, and nothing else. `validate`
    did exactly that with `report["validation"]`, so an incremental rebuild dropped the level's
    verdict from the report while still claiming the stage was fresh. Returning the delta instead
    makes it part of the replayed payload, which is the one thing a skip is guaranteed to restore.
    """
    if isinstance(result, dict):
        # `get`, not `pop`: the replay payload is written from this same dict a few lines later,
        # and popping the delta out of it is exactly how the verdict failed to survive a skip the
        # first time. It costs one duplicate copy under `stages.<key>.result`, which is what makes
        # the payload self-describing.
        delta = result.get("_report")
        if isinstance(delta, dict):
            report.update(delta)


def summary(result):
    if not isinstance(result, dict):
        return None
    parts = []
    for key, value in list(result.items())[:4]:
        if isinstance(value, (int, float, str)):
            parts.append(f"{key}={value}")
    return ", ".join(parts) or None
