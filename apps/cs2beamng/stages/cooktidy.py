"""Everything the cook does around the compiler: finding it, telling whether it is running, and
tidying up after it.

The compiler writes its own placeholder materials beside a shape and leaves cooked files behind
for shapes the level no longer has, so a build that did not clean up would ship both."""

import json
import os
import subprocess
import sys

from .. import settings as settings_mod

# The ladder index is written in the format `budget` reads, and importing it is how the two stay
# in step. It used to be `tools/level_budget.py` reached through a __file__ climb, which meant a
# non-editable `pip install .` could not import this stage at all.
from ..beamng import budget as level_budget


def tidy(ctx, level_dir, level_name, shapes_dir, progress, rescan=()):
    """A8/P7 — the two disk sweeps, both after the cook so nothing they remove is still needed."""
    out = {}
    settings = getattr(ctx, "settings", None) or {}
    # BEFORE any sweep: the ladder index is read from the `.dae` sources one of them deletes
    try:
        out["shapeLaddersIndexed"], out["shapeLaddersScanned"] = write_ladder_index(
            level_dir, shapes_dir, rescan
        )
    except (OSError, ValueError) as exc:
        progress.log(
            f"cook: could not write the shape ladder index ({exc}); "
            f"`level_budget` will not be able to count triangles if the sources go"
        )
    if settings.get("sweep_temp", True):
        files, freed = _sweep_temp(level_dir, level_name, shapes_dir)
        out["tempCdaeRemoved"] = files
        out["tempMiBFreed"] = round(freed / 1048576.0, 1)
        if files:
            progress.log(
                f"cook: swept {files:,} stale .cdae ({freed / 1048576:,.0f} MiB) out of BeamNG's "
                f"temp/ — cooked shapes this level no longer has a source for"
            )
    if not settings.get("keep_dae", False):
        files, freed = _drop_dae_sources(shapes_dir)
        out["daeSourcesRemoved"] = files
        out["daeMiBFreed"] = round(freed / 1048576.0, 1)
        if files:
            progress.log(
                f"cook: removed {files:,} .dae sources ({freed / 1048576:,.0f} MiB) that have a "
                f"current .cdae — the engine loads the cooked file, never the source "
                f"(`keep_dae` keeps them)"
            )
    return out


def _sweep_temp(level_dir, level_name, shapes_dir):
    """Delete cooked shapes in BeamNG's `temp/` that the level no longer has a source for.

    **BeamNG never prunes it.** `temp/levels/<name>/` accumulates a `.cdae` for every shape any
    build ever wrote, so a folder that has been rebuilt for a fortnight holds every shape that has
    ever existed: measured 2026-08-19, 9,047 cooked files where the level had 4,289 — 4,998 MiB, of
    which more than half was for shapes that no longer exist. It costs no RAM (the engine loads
    only what the level references) and it is 2.5 GB of disk.

    Only files under THIS level's temp folder whose `.dae`/`.cdae` counterpart is gone from the
    level are removed. Returns `(files, bytes)`.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(level_dir)))  # <userfolder>/levels/..
    temp = os.path.join(root, "temp", "levels", level_name)
    if not os.path.isdir(temp) or not os.path.isdir(shapes_dir):
        return 0, 0
    keep = set()
    for path in find_process(shapes_dir, ".dae") + find_process(shapes_dir, ".cdae"):
        keep.add(os.path.splitext(os.path.relpath(path, level_dir))[0].replace("\\", "/").lower())
    files = freed = 0
    for base, _dirs, names in os.walk(temp):
        for name in names:
            if not name.lower().endswith(".cdae"):
                continue
            path = os.path.join(base, name)
            stem = os.path.splitext(os.path.relpath(path, temp))[0].replace("\\", "/").lower()
            if stem in keep:
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            files += 1
            freed += size
    return files, freed


def _drop_dae_sources(shapes_dir):
    """Delete every `.dae` that has a `.cdae` at least as new beside it. Returns `(files, bytes)`.

    **The engine loads the cooked file and never the source**, and this has vanilla's own
    precedent: 182 of BeamNG's 6,361 shipped `.cdae` have no `.dae` at all (74 in `Industrial.zip`
    alone), while 0 shipped `.dae` lack a `.cdae`. On the reference city the sources are 5,452 MiB
    against the cooked 4,173.

    A `.dae` with no current `.cdae` is NEVER removed — that would delete the only copy of a shape
    the cook did not reach.
    """
    files = freed = 0
    for path in find_process(shapes_dir, ".dae"):
        cdae = os.path.splitext(path)[0] + ".cdae"
        try:
            if not os.path.isfile(cdae) or os.path.getmtime(cdae) < os.path.getmtime(path):
                continue
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            continue
        files += 1
        freed += size
    return files, freed


def remove_autogenerated(shapes_dir):
    """Delete every `*.materials.json` under `shapes_dir` — none is ours (see the module
    docstring). Returns `[(path, [material names])]` for the log; a file that cannot be read
    still counts, with no names."""
    removed = []
    for path in find_process(shapes_dir, ".materials.json"):
        names = []
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            if isinstance(doc, dict):
                names = [str(v.get("name") or k) for k, v in doc.items() if isinstance(v, dict)]
        except (OSError, ValueError):
            pass
        try:
            os.remove(path)
        except OSError:
            continue
        removed.append((path, names))
    return removed


def find_process(root, suffix):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(suffix):
                out.append(os.path.join(dirpath, name))
    return out


def userfolder_of(level_dir):
    """The BeamNG userfolder `level_dir` lives in (`<userfolder>/levels/<name>`), or None."""
    parent = os.path.dirname(level_dir)
    if os.path.basename(parent).lower() != "levels":
        return None
    userfolder = os.path.dirname(parent)
    known = [os.path.abspath(p) for p in settings_mod.candidate_userfolders()]
    if os.path.abspath(userfolder) in known:
        return userfolder
    # a userfolder we did not guess but which is one: it holds the game's settings tree
    if os.path.isdir(os.path.join(userfolder, "settings")) or os.path.isfile(
        os.path.join(userfolder, "beamng.log")
    ):
        return userfolder
    return None


def beamng_running():
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq BeamNG.drive.x64.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
            return "BeamNG.drive.x64.exe" in out
        out = subprocess.run(
            ["pgrep", "-f", "BeamNG.drive"], capture_output=True, text=True, timeout=15
        ).stdout
        return bool(out.strip())
    except Exception:
        return False


def kill_process(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


def write_ladder_index(level_dir, shapes_dir, rescan=()):
    """`shape_ladders.json` — every shape's detail ladder and collision count, read out of the
    `.dae` sources while they are still there. Returns `(shapes, rescanned)`.

    **`keep_dae: false` deletes the only place those numbers live.** `level_budget` derives the
    triangles a level actually draws by walking each shape's `<geometry>` blocks, so a level whose
    sources were dropped could report its bytes and not one triangle — the gate this project has
    used since A5 would silently read zero.

    So the index is **merged, never rewritten from scratch**, on two counts:

    * `scan_dae` reads each file whole and regexes it — 4.4 GiB and ~12 s of it on the reference
      city, paid on every build including one that compiled nothing. Only the shapes this cook is
      about to compile can have changed (`_needs_compile` is exactly "the `.dae` moved"), so every
      other entry is carried over from the file that is already there.
    * a shape whose `.dae` a previous `keep_dae: false` build removed keeps its entry as long as
      its `.cdae` is still in the level. Rewriting the file from the surviving sources dropped
      every one of those numbers — the very case the index exists for.
    """
    out = os.path.join(level_dir, level_budget.LADDER_INDEX)
    try:
        with open(out, encoding="utf-8") as handle:
            previous = (json.load(handle) or {}).get("shapes") or {}
    except (OSError, ValueError):
        previous = {}
    rescan = {os.path.abspath(p) for p in rescan}
    shapes = {}
    rescanned = 0
    for path in find_process(shapes_dir, ".dae"):
        rel = os.path.relpath(path, level_dir).replace("\\", "/")
        carried = previous.get(rel)
        if carried is not None and os.path.abspath(path) not in rescan:
            shapes[rel] = carried
            continue
        levels, collision = level_budget.scan_dae(path)
        shapes[rel] = {
            "levels": {str(px): int(t) for px, t in (levels or {}).items()},
            "collision": int(collision or 0),
        }
        rescanned += 1
    for rel, entry in previous.items():  # source gone, cooked shape still there
        if rel in shapes:
            continue
        cdae = os.path.join(level_dir, *(os.path.splitext(rel)[0] + ".cdae").split("/"))
        if os.path.isfile(cdae):
            shapes[rel] = entry
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"schema": "cs2beamng.shape_ladders/1", "shapes": shapes}, handle, sort_keys=True)
    return len(shapes), rescanned


# version 2: A8/P7 — after cooking, sweep BeamNG's `temp/` of cooked shapes the level no longer
# has a source for (it never prunes them itself), and optionally drop the `.dae` sources the
# engine never loads
# version 3: the stage is INCREMENTAL. It is the one stage that is a function of the written
# shapes rather than of the bundle, so it declares `shapes:` — a stat walk of the level's
# `.dae`/`.cdae` — and a build where no shape moved skips it whole, ladder index and sweeps
# included, and reports `unchanged` like every other stage instead of `done`.
