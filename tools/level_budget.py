"""Compatibility shim — the module moved into the app package.

`level_budget` measures a built BeamNG level (resident bytes, triangles drawn) and it reads the
level through `cs2beamng.beamng.level`, so it belongs beside that code and not in the
engine-neutral `csiibundle`. It now lives at `cs2beamng/beamng/budget.py` and ships in the
wheel; `cs2beamng budget <level_dir>` is the supported way to run it.

This file stays so the command lines in `docs/cs2-api-audit.md` and the plan documents keep
working from a checkout, installed or not.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")
)

from cs2beamng.beamng.budget import *  # noqa: F403 - re-export the whole module
from cs2beamng.beamng.budgetreport import main  # the CLI entry moved with the report (E1)

if __name__ == "__main__":
    sys.exit(main())
