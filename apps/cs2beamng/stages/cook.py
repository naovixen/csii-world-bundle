"""Stage `cook` — BeamNG's own headless mesh compiler over the level we just wrote, so the
first in-game load does not spend minutes parsing COLLADA.

What the game does on a cold load of our level: it parses every `.dae` (2,832 tiles, 2.1 GB of
XML on the full city) into its binary `.cdae` (`temp/levels/<name>/art/shapes/...`) — 210 s
of "objects" in `beamng.log`'s `Level loaded in` line, all of it stall — then cooks the
textures. `BeamNG.drive.exe -compilemeshes` (`lua/ge/extensions/util/compileMeshes.lua`)
does the first half without a window: it walks `-compileMeshesCustomPaths "levels/<name>/"`
in the game's virtual file system, compiles each `.dae` to a `.cdae` BESIDE it (which the
runtime loader prefers over compiling), and quits with `shutdown()`. `-compileMeshesNoCheck`
keeps it from failing the run because every vanilla `.dae` in the game's zips is "missed".
The texture-cache warmer the plan mentions (`-LoadSingleLevelForTextureCache`) is not in
0.39's shipped Lua (`extensions/test/singleLevel.lua` is absent), so textures still cook on
the first load — 260 textures in ~2 minutes of on-demand work, none of it a stall.

Progress is the `.cdae` files appearing under the level's `art/shapes/`, polled every
`POLL_SECONDS`; the compiler logs to the same `beamng.log` but overwriting it is its business,
not ours. Cancel kills the process. The stage SKIPS (never fails the build) when: the setting
is off, no executable is found (`beamng_exe` or the Steam libraries), the level does not live
under a BeamNG userfolder (the compiler can only see the game's virtual FS), or a BeamNG
process is already running (one instance per userfolder). Each skip is a log line and a
`skipped` reason in the report.
"""

import os
import subprocess
import sys
import time

from csiibundle.bake.progress import Cancelled

from .. import settings as settings_mod
from . import Stage

STAGE_KEY = "cook"
POLL_SECONDS = 2.0
STARTUP_GRACE_SECONDS = 180.0      # the engine boots (~20-40 s) before the first .cdae lands
STALL_SECONDS = 600.0              # no new .cdae for this long -> give up on the compiler
SHAPE_DIRS = ("art", "shapes")


def enabled(settings):
    return bool(settings.get("cook", False))


def units(census, settings):
    return None


def run(ctx, progress):
    started = time.monotonic()
    level_dir = os.path.abspath(ctx.level_dir)
    level_name = ctx.settings["level_name"]

    exe = _engine_exe(settings_mod.find_beamng_exe(ctx.settings))
    if not exe:
        return _skip(progress, "no BeamNG executable found (set beamng_exe)", started)
    userfolder = _userfolder_of(level_dir)
    if userfolder is None:
        return _skip(progress, f"{level_dir} is not under a BeamNG userfolder's levels/; the compiler only sees the game's own file system", started)
    if _beamng_running():
        return _skip(progress, "BeamNG is already running (one instance per userfolder); close it and rebuild, or load the level and let it compile", started)

    shapes_dir = os.path.join(level_dir, *SHAPE_DIRS)
    daes = _find(shapes_dir, ".dae")
    if not daes:
        return _skip(progress, "no .dae shapes in the level", started)
    # a stale .cdae beside a rewritten .dae would be reused: remove ours before compiling
    removed = 0
    for path in _find(shapes_dir, ".cdae"):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass

    # -compileMeshesNoCache: the compiler otherwise copies every .cdae into <userfolder>/
    # collada_cache/<sha1>.cdae as well — a second 1.6 GB of the same bytes on a full city.
    args = [exe, "-headless", "-nosteam", "-compilemeshes",
            "-compileMeshesCustomPaths", f"levels/{level_name}/", "-compileMeshesNoCheck", "-compileMeshesNoCache"]
    progress.log(f"cook: {os.path.basename(exe)} -compilemeshes levels/{level_name}/ ({len(daes)} shapes"
                 f"{', ' + str(removed) + ' stale .cdae removed' if removed else ''})")
    progress.begin("cook: compiling shapes to .cdae", total=len(daes), unit="shapes")
    creation = {}
    if sys.platform.startswith("win"):
        creation["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(args, cwd=os.path.dirname(exe), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **creation)
    except OSError as exc:
        return _skip(progress, f"could not start {exe}: {exc}", started)

    done = 0
    last_change = time.monotonic()
    try:
        while True:
            try:
                progress.check()
            except Cancelled:
                _kill(proc)
                raise
            now_done = len(_find(shapes_dir, ".cdae"))
            if now_done != done:
                progress.advance(now_done - done)
                done = now_done
                last_change = time.monotonic()
            code = proc.poll()
            if code is not None:
                break
            waited = time.monotonic() - last_change
            if (done == 0 and waited > STARTUP_GRACE_SECONDS) or (done > 0 and waited > STALL_SECONDS):
                _kill(proc)
                progress.end(f"compiler stalled after {done}/{len(daes)} shapes; killed")
                return {"compiled": done, "shapes": len(daes), "skipped": "stalled", "exe": exe,
                        "seconds": round(time.monotonic() - started, 1)}
            time.sleep(POLL_SECONDS)
    finally:
        if proc.poll() is None:
            _kill(proc)
    done = len(_find(shapes_dir, ".cdae"))
    progress.end(f"{done}/{len(daes)} shapes compiled, exit code {code}")
    return {"compiled": done, "shapes": len(daes), "exitCode": code, "exe": exe,
            "seconds": round(time.monotonic() - started, 1)}


def _engine_exe(found):
    """The engine binary itself. The Steam-facing `BeamNG.drive.exe` in the install root is a
    launcher that starts `Bin64/BeamNG.drive.x64.exe` and returns, which would end our wait
    at once; drive the engine directly when it is there."""
    if not found:
        return None
    root = os.path.dirname(found)
    if sys.platform.startswith("win"):
        engine = os.path.join(root, "Bin64", "BeamNG.drive.x64.exe")
        if os.path.basename(found).lower() == "beamng.drive.exe" and os.path.isfile(engine):
            return engine
    return found


def _skip(progress, reason, started):
    progress.log(f"cook: skipped: {reason}")
    return {"skipped": reason, "seconds": round(time.monotonic() - started, 1)}


def _find(root, suffix):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(suffix):
                out.append(os.path.join(dirpath, name))
    return out


def _userfolder_of(level_dir):
    """The BeamNG userfolder `level_dir` lives in (`<userfolder>/levels/<name>`), or None."""
    parent = os.path.dirname(level_dir)
    if os.path.basename(parent).lower() != "levels":
        return None
    userfolder = os.path.dirname(parent)
    known = [os.path.abspath(p) for p in settings_mod.candidate_userfolders()]
    if os.path.abspath(userfolder) in known:
        return userfolder
    # a userfolder we did not guess but which is one: it holds the game's settings tree
    if os.path.isdir(os.path.join(userfolder, "settings")) or os.path.isfile(os.path.join(userfolder, "beamng.log")):
        return userfolder
    return None


def _beamng_running():
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BeamNG.drive.x64.exe", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return "BeamNG.drive.x64.exe" in out
        out = subprocess.run(["pgrep", "-f", "BeamNG.drive"], capture_output=True, text=True, timeout=15).stdout
        return bool(out.strip())
    except Exception:  # noqa: BLE001 - if we cannot tell, try anyway
        return False


def _kill(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


STAGE = Stage(STAGE_KEY, "Cook (BeamNG mesh compiler)", enabled, units, run)
