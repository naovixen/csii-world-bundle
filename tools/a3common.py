"""Shared tally and fixtures for the A3 gate's three files.

The A3 gate is `test_a3_objects.py` (the stages, end to end on the example bundle),
`test_a3_rules.py` (the BeamNG-writer rules) and `test_a3_city.py` (the game's classification
rules plus the real-bundle sweeps). Each file is its own pytest gate and its own script; they
share this tally so a run reads the same either way.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
NS = "{http://www.collada.org/2005/11/COLLADASchema}"

FAILS = []
COUNT = [0]


def check(cond, what, quiet=False):
    COUNT[0] += 1
    if not quiet or not cond:
        print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def start():
    """Reset the tally — each file's `main()` begins here, so one pytest process running all
    three gates does not leak one file's failures into the next file's summary."""
    FAILS.clear()
    COUNT[0] = 0


def finish():
    print()
    if FAILS:
        print(f"{len(FAILS)} of {COUNT[0]} FAILED:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print(f"{COUNT[0]}/{COUNT[0]} checks passed")
    return 0


def real_bundle():
    from csiibundle.bake.locate import default_bundle_dir

    path = default_bundle_dir()
    return path if path and os.path.isfile(os.path.join(path, "instances.jsonl")) else None
