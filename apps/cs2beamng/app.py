"""`launch()` — the application: start the local server, show the UI, wait, clean up.

    cs2beamng                 # native window if pywebview is installed, else the browser
    cs2beamng gui --browser   # always the browser
    python -m cs2beamng       # the same

The UI process never computes: the server thread answers requests, the runner's reader thread
folds worker messages into state, and the worker *process* does the build. Quitting with a
build running cancels it (the worker stops within a chunk; a stubborn one is terminated by the
runner's watchdog) so no orphan keeps writing into the level folder.
"""

import sys

from . import __version__
from . import window as window_mod
from .server import AppServer


def launch(browser=None, host="127.0.0.1", port=0, open_ui=True):
    """`browser`: None = the saved `ui_browser` setting; "window" = native; "chrome" / "edge" /
    "firefox" / "default" = that browser (and the choice is saved)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    server = AppServer(host=host, port=port)
    if browser:
        server.update_settings({"ui_browser": browser})       # remembered for next time
    mode = server.values.get("ui_browser") or "window"
    url = server.start()
    print(f"cs2beamng {__version__}: {url}", flush=True)     # also the way to reach a native window's server
    try:
        code = window_mod.show(url, server, mode=mode, open_ui=open_ui)
    finally:
        _shutdown(server)
    return code


def _shutdown(server):
    server.runner.stop()            # cancel, grace period, then kill: a non-daemon worker must not outlive us
    server.stop()
