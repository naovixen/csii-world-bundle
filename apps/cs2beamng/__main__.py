"""`python -m cs2beamng [...]` — same as the `cs2beamng` console script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
