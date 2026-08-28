"""`cs2beamng build` — the same pipeline as the app, with a terminal for a UI."""

import os
import sys
import time

from .. import settings as settings_mod
from . import census as census_mod
from .runner import Runner

# -- headless ---------------------------------------------------------------------------------------


def _values_from_args(args):
    """The saved settings with everything the command line said applied, in order.

    `--settings` first (a whole file), then `--preset`, then the individual overrides, so a
    `--set` on the command line always wins over a preset named beside it.
    """
    values = settings_mod.load()
    if getattr(args, "settings", None):
        import json

        with open(args.settings, encoding="utf-8") as handle:
            document = json.load(handle)
        # Accept the app's own saved shape (`cs2beamng_settings.json`: {"schema", "settings"})
        # as well as a flat dict. The flat-only reader silently applied NOTHING from a saved
        # file — every wrapper key fell outside SCHEMA — and the build ran on the app settings
        # store instead, against another level entirely (found 2026-08-28, refactor Phase 4).
        if isinstance(document.get("settings"), dict):
            document = document["settings"]
        applied = 0
        for key, value in document.items():
            if key in settings_mod.SCHEMA:
                values[key] = value
                applied += 1
        if not applied:
            print(f"settings: nothing in {args.settings} matched a known setting", file=sys.stderr)
            return 2
    if getattr(args, "preset", None):
        try:
            values = settings_mod.apply_preset(values, args.preset)
        except KeyError:
            print(
                f"settings: unknown preset {args.preset!r} "
                f"(one of: {', '.join(sorted(settings_mod.PRESETS))})",
                file=sys.stderr,
            )
            return 2
    if getattr(args, "region", None) is not None:
        values["region"] = args.region
    if getattr(args, "bundle", None):
        values["bundle"] = os.path.abspath(args.bundle)
    if getattr(args, "out", None):
        out = os.path.abspath(args.out)
        values["output"], values["level_name"] = os.path.dirname(out), os.path.basename(out)
    for item in getattr(args, "set", []) or []:
        key, _, raw = item.partition("=")
        values[key] = settings_mod.coerce(key, raw)
    if getattr(args, "force", False):
        values["rebuild"] = True
    return values


def _follow(runner):
    """Print the build's log and one live status line until it stops. True if interrupted.

    The status line is rewritten in place, so it is padded to the previous one's width: a
    shorter line drawn over a longer one leaves the tail of the old one on screen.
    """
    last_seq = 0
    last_line = ""
    try:
        while True:
            state = runner.state()
            for entry in runner.log_since(last_seq):
                sys.stdout.write("\r" + " " * len(last_line) + "\r")
                print(f"  [{entry['stage']}] {entry['text']}")
                last_seq = entry["seq"]
            running = [s for s in state["stages"] if s["status"] == "running"]
            if running:
                s = running[0]
                phase = f" › {s['phase']}" if s.get("phase") else ""
                bar = ""
                if s.get("phaseTotal"):
                    frac = min(1.0, s.get("phaseDone", 0) / s["phaseTotal"])
                    bar = f" [{'#' * int(frac * 20):<20}] {s.get('phaseDone', 0)}/{s['phaseTotal']} {s.get('unit', '')}"
                eta = state["overall"].get("eta")
                line = (
                    f"{s['title']}{phase}{bar}  elapsed {_fmt_seconds(state['overall']['elapsed'])}"
                    f"  eta {'?' if eta is None else _fmt_seconds(eta)}"
                )
                sys.stdout.write("\r" + line[:118].ljust(len(last_line)))
                sys.stdout.flush()
                last_line = line[:118]
            if state["status"] != "running":
                break
            time.sleep(0.5)
        return False
    except KeyboardInterrupt:
        print("\ncancelling...")
        runner.stop()
        return True
    finally:
        runner.kill()  # any other exit path: never leave a worker writing


def build_headless(args):
    """`cs2beamng build ...`: run the pipeline with a terminal renderer."""
    for stream in (sys.stdout, sys.stderr):  # a cp1252 console must not kill a build over an arrow
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    values = _values_from_args(args)
    problems = settings_mod.validate(values)
    if problems:
        for p in problems:
            print("settings:", p, file=sys.stderr)
        return 2
    census = census_mod.take(values)
    est = census.get("totalSeconds")
    print(f"bundle: {values['bundle']}")
    print(f"level:  {os.path.join(values['output'], values['level_name'])}")
    print(f"census: {census['bundle']['counts'] if census.get('bundle') else 'n/a'}")
    print(f"expected: {'unknown' if est is None else _fmt_seconds(est)}")
    runner = Runner()
    runner.start(values, census)
    interrupted = _follow(runner)
    if interrupted:
        return 130
    state = runner.state()
    print()
    for s in state["stages"]:
        print(
            f"  {s['title']:40s} {s['status']:9s} {_fmt_seconds(s.get('elapsed') or 0)}  {s.get('summary') or ''}"
        )
    print(f"status: {state['status']} in {_fmt_seconds(state['overall']['elapsed'])}")
    if state.get("error"):
        print(state["error"])
    return 0 if state["status"] == "done" else 1


def _fmt_seconds(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
