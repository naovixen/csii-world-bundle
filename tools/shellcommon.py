"""Shared tally and environment for the A0 app-shell gate's three files.

The A0 gate is `test_app_shell.py` (settings, runner, server, headless),
`test_app_incremental.py` (the A4 incremental build) and `test_app_features.py` (the frozen
build, the mod installer, the map, the product tables). Each file is its own pytest gate and
its own script; `run_gate` is the harness they share — a scratch home, the stub-stage hook,
the tally, and the cleanup of what a real-stage build leaves in the example bundle.
"""

import os
import shutil
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")

FAILS = []
COUNT = [0]


def check(cond, what):
    COUNT[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def run_gate(body):
    """Run one file's checks under the shared environment and report the tally."""
    FAILS.clear()
    COUNT[0] = 0
    tmp = tempfile.mkdtemp(prefix="cs2beamng_shell_")
    os.environ["CS2BEAMNG_HOME"] = os.path.join(tmp, "home")
    os.environ["CS2BEAMNG_STAGE_MODULE"] = "cs2beamng_stub_stages"
    try:
        body(tmp)
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    finally:
        # the example bundle's derived/ is a build artefact of any real-stage step here;
        # leave the repo clean
        shutil.rmtree(os.path.join(EXAMPLE, "derived"), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{COUNT[0] - len(FAILS)}/{COUNT[0]} checks passed")
    if FAILS:
        for f in FAILS:
            print("  FAIL:", f)
        return 1
    return 0
