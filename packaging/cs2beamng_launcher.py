"""Entry point for the frozen build (PyInstaller cannot use a package's `__main__.py`, which
imports relatively).

`freeze_support()` must be the first call under the `__main__` guard: the runner and four stages
spawn workers with the "spawn" context, and in a frozen build a spawn re-executes THIS
executable. Without it, each worker would open another application window instead of running
its task, and each of those would spawn more.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from cs2beamng.cli import main

    sys.exit(main())
