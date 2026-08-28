"""What textures this level already has, and which of them this build must rewrite.

The ledger is what makes a second build fast: a texture whose source, size and settings have not
moved is left exactly where it is."""

import json
import os

from ..beamng import textureplan

# -- the texture ledger ----------------------------------------------------------------------------
#
# `(source path, source mtime+size, kind, cap, keep_alpha, format)` -> the game path that came
# out. It lives beside `level_state.json` because it describes THIS level's `art/textures/`, and
# every reuse is checked against the destination file still being there — a ledger entry alone
# is never enough to claim a texture is exported.

LEDGER_NAME = "textures.ledger.json"
LEDGER_SCHEMA = "cs2beamng.texture_ledger/1"


def ledger_path(level_dir):
    from ..pipeline import state as state_mod

    return os.path.join(level_dir, state_mod.STATE_DIR, LEDGER_NAME)


def load_ledger(level_dir):
    try:
        with open(ledger_path(level_dir), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if document.get("schema") != LEDGER_SCHEMA:
        return {}
    return document.get("entries") or {}


def save_ledger(level_dir, exported, caps, use_dds):
    entries = {}
    for (src, kind, keep_alpha), game in exported.items():
        if not game:
            continue
        stamp = file_stamp(src)
        if stamp is None:
            continue
        entries[ledger_key(src, kind, keep_alpha, caps, use_dds)] = {"stamp": stamp, "game": game}
    path = ledger_path(level_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "schema": LEDGER_SCHEMA,
                    "caps": caps,
                    "format": "dds" if use_dds else "png",
                    "entries": entries,
                },
                handle,
                separators=(",", ":"),
            )
        os.replace(tmp, path)
    except OSError:
        pass  # a ledger that cannot be written costs time, not correctness


def ledger_key(src, kind, keep_alpha, caps, use_dds):
    cap = int(caps.get(kind, 0) or 0)
    return f"{src}|{kind}|{int(bool(keep_alpha))}|{cap}|{'dds' if use_dds else 'png'}"


def file_stamp(src):
    try:
        st = os.stat(src)
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns)]


def partition(level, jobs, ledger, caps, use_dds):
    """`(reusable, todo)`: the jobs whose source, cap and format are unchanged AND whose exported
    file is still on disk, and the rest."""
    reusable, todo = {}, []
    for job in jobs:
        src, kind, keep_alpha = job
        entry = ledger.get(ledger_key(src, kind, keep_alpha, caps, use_dds))
        stamp = file_stamp(src)
        if not entry or stamp is None or entry.get("stamp") != stamp:
            todo.append(job)
            continue
        name = textureplan.texture_name(src, kind, keep_alpha, use_dds)
        if not os.path.isfile(level.path(*textureplan.TEXTURE_SUBDIR, name)):
            todo.append(job)  # the ledger remembers, the folder does not
            continue
        reusable[job] = entry["game"]
    return reusable, todo
