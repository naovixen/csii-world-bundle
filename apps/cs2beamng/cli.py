"""`cs2beamng` command line: the GUI by default, `build` for headless use.

    cs2beamng                       # open the application (native window or browser)
    cs2beamng build [bundle] --out DIR [--preset high] [--region "X Z R"] [--set key=value ...]
    cs2beamng doctor                # what this machine offers, and what is missing
    cs2beamng budget LEVEL_DIR      # what a built level costs (resident bytes, triangles drawn)
    cs2beamng --version

`main()` calls `multiprocessing.freeze_support()` first. The runner and four stages spawn
workers with the "spawn" context, which re-executes this entry point in the child; frozen (the
PyInstaller build) that re-execution is the *exe*, and without `freeze_support()` every child
opens another application window instead of running its task.
"""

import argparse
import multiprocessing
import sys

from . import __version__


def _parser():
    parser = argparse.ArgumentParser(
        prog="cs2beamng", description="CS2 World Bundle -> BeamNG.drive level"
    )
    parser.add_argument("--version", action="version", version=f"cs2beamng {__version__}")
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser("build", help="build a level headless")
    build.add_argument(
        "bundle",
        nargs="?",
        default=None,
        help="bundle directory (default: the CS2 ModsData export)",
    )
    build.add_argument(
        "--out",
        default=None,
        help="level output directory (default: <BeamNG userfolder>/levels/<name>)",
    )
    build.add_argument(
        "--preset",
        default=None,
        metavar="NAME",
        help="apply a quality preset first: ultra, high, performance, medium, low",
    )
    build.add_argument(
        "--region",
        default=None,
        metavar='"X Z R"',
        help="build a square patch of the city instead of all of it, in bundle metres: "
        "the centre and the HALF-WIDTH",
    )
    build.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting"
    )
    build.add_argument("--settings", default=None, help="settings JSON file")
    build.add_argument(
        "--force",
        action="store_true",
        help="rebuild every stage, ignoring level_state.json (same as --set rebuild=true)",
    )
    gui = sub.add_parser("gui", help="open the application (the default)")
    gui.add_argument(
        "--browser",
        nargs="?",
        const="default",
        default=None,
        metavar="NAME",
        help="open in a browser instead of a native window: chrome, edge, firefox, or default (system); "
        "remembered in settings as ui_browser",
    )
    gui.add_argument(
        "--port",
        type=int,
        default=0,
        help="fixed port for the local UI server (default: ephemeral)",
    )
    gui.add_argument(
        "--no-open",
        action="store_true",
        help="serve the UI and print its URL, but open no window or browser",
    )
    doctor = sub.add_parser("doctor", help="what was detected on this machine, and what is missing")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser(
        "budget", help="measure a built level (pass --help for its own options)", add_help=False
    )
    return parser


def main(argv=None):
    multiprocessing.freeze_support()
    argv = sys.argv[1:] if argv is None else list(argv)
    # `budget` has an argument parser of its own; hand it everything after the subcommand name
    # rather than teaching this parser its twelve options.
    if argv and argv[0] == "budget":
        from .beamng.budget import main as budget_main

        return budget_main(argv[1:])
    args = _parser().parse_args(argv)
    if args.command == "build":
        from .pipeline.runner import build_headless

        return build_headless(args)
    if args.command == "doctor":
        from .doctor import report

        return report(as_json=args.json)
    from .app import launch

    return launch(
        browser=getattr(args, "browser", None),
        port=getattr(args, "port", 0) or 0,
        open_ui=not getattr(args, "no_open", False),
    )


if __name__ == "__main__":
    sys.exit(main())
