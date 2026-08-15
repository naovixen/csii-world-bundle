"""`cs2beamng` command line: the GUI by default, `build` for headless use.

    cs2beamng                       # open the application (native window or browser)
    cs2beamng build [bundle] --out DIR [--set key=value ...]
    cs2beamng --version

The subcommands are wired up in A0; this module is the stable entry point the package
declares in pyproject.toml.
"""

import argparse
import sys

from . import __version__


def _parser():
    parser = argparse.ArgumentParser(prog="cs2beamng", description="CS2 World Bundle -> BeamNG.drive level")
    parser.add_argument("--version", action="version", version=f"cs2beamng {__version__}")
    sub = parser.add_subparsers(dest="command")
    build = sub.add_parser("build", help="build a level headless")
    build.add_argument("bundle", nargs="?", default=None, help="bundle directory (default: the CS2 ModsData export)")
    build.add_argument("--out", default=None, help="level output directory (default: <BeamNG userfolder>/levels/<name>)")
    build.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting")
    build.add_argument("--settings", default=None, help="settings JSON file")
    gui = sub.add_parser("gui", help="open the application (the default)")
    gui.add_argument("--browser", nargs="?", const="default", default=None, metavar="NAME",
                     help="open in a browser instead of a native window: chrome, edge, firefox, or default (system); "
                          "remembered in settings as ui_browser")
    gui.add_argument("--port", type=int, default=0, help="fixed port for the local UI server (default: ephemeral)")
    gui.add_argument("--no-open", action="store_true", help="serve the UI and print its URL, but open no window or browser")
    return parser


def main(argv=None):
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "build":
        from .pipeline.runner import build_headless
        return build_headless(args)
    from .app import launch
    return launch(browser=getattr(args, "browser", None), port=getattr(args, "port", 0) or 0,
                  open_ui=not getattr(args, "no_open", False))


if __name__ == "__main__":
    sys.exit(main())
