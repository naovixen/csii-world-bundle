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
import time

from . import __version__
from . import window as window_mod
from .server import AppServer


def launch(browser=False, host="127.0.0.1", port=0):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    server = AppServer(host=host, port=port)
    url = server.start()
    print(f"cs2beamng {__version__}: {url}", flush=True)     # also the way to reach a native window's server
    try:
        code = window_mod.show(url, server, force_browser=browser)
    finally:
        _shutdown(server)
    return code


def _shutdown(server):
    runner = server.runner
    if runner.running():
        runner.cancel()
        deadline = time.time() + 20.0
        while runner.running() and time.time() < deadline:
            runner.state()          # the watchdog lives in state(): it hard-kills a stubborn worker
            time.sleep(0.2)
    runner.kill()                   # a non-daemon worker would otherwise keep the process alive
    server.stop()
