"""What every test in this directory can assume, in one place.

Before this file each test file opened with its own bootstrap — 35 `sys.path.insert` calls in
nine different spellings — and each one that wanted the real bundle spelled the path out again,
in two different ways, seven of them in a form that cannot resolve off Windows. The paths are
now a fixture and the bootstrap is here.

    pytest tools                     # everything (~10 min)
    pytest tools -m "not slow"       # the fast 39 files (~2.5 min) — what to run while iterating
    pytest tools -m "not needs_rip"  # only what runs without a rip on disk
    pytest tools/test_a1_level.py    # one file
    python tools/test_a1_level.py    # still works: every file keeps its script entry point

**What pytest collects here is `gate()`, not `test_*`.** These files predate pytest and use
`test_<claim>()` for their own internal helpers, most of which take positional arguments that
are not fixtures. `python_functions = ["gate"]` in pyproject.toml means one collected test per
file — the file's own entry point — and leaves the helper naming alone. Refactor Phase 4 splits
the four oversized test files into real per-claim pytest functions; this is the seam for that.

**`slow` is measured, not guessed.** Timed per file on 2026-08-28: the suite is 635 s and
**nine files are 494 s of it (78 %)**, every one of them a pass over the real bundle or a real
built level. The other 39 come to 141 s. So `-m "not slow"` is the loop to iterate in and the
full run is the gate to land on — refactor Phase 5 ran the full suite fourteen times to find
failures that `not slow` plus `tools/check_references.py` would have found in seconds.

Adding a file here is a decision about where it belongs in that loop, so the list is here rather
than as a `pytestmark` in each file: one place shows the whole split, and the test files keep
working as standalone scripts with no pytest import.

**`needs_rip` is for tests that cannot run at all without a bundle**, not for tests that merely
have more to say when one is present. Several files deliberately REPORT "awaiting a rip" instead
of skipping — `test_terrain_fine.py` says so in its docstring — because a silent skip is how a
gate stops being a gate. Do not convert those to skips.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# `csiibundle` and `cs2beamng` come from `pip install -e .` — which is the documented way to work
# on this repo — but a bare checkout should still be able to run the suite, so put both package
# roots on the path if the install is not there. `tools/` itself pytest prepends for us.
for path in (HERE, os.path.join(REPO, "apps")):
    if path not in sys.path:
        sys.path.insert(0, path)


# The nine files that are 78 % of the suite's wall time, measured 2026-08-28 (seconds each on
# the reference machine). Every one walks the real bundle or a real built level; nothing here is
# slow by accident, and nothing else in the suite is over 21 s.
SLOW = {
    "test_terrain_fine.py": 128.5,
    "test_node_fan.py": 122.7,
    "test_a1_level.py": 47.1,
    "test_mask_channels.py": 41.9,
    "test_bake_products_real.py": 37.7,
    "test_soup_surface.py": 33.1,
    "test_decal_rows.py": 31.7,
    "test_navgraph_map.py": 27.3,
    "test_a3_city.py": 24.4,
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_rip: requires a real CS2 World Bundle on disk; skipped when there is none. "
        "Only for tests that cannot run at all without one.",
    )
    config.addinivalue_line(
        "markers",
        "slow: one of the nine files that are 78 % of the suite's time (see SLOW in conftest). "
        'Run `-m "not slow"` while iterating; run everything before landing.',
    )


def pytest_collection_modifyitems(items):
    """Mark the measured-slow files, and skip `needs_rip` when no bundle is on disk."""
    for item in items:
        if os.path.basename(str(item.fspath)) in SLOW:
            item.add_marker(pytest.mark.slow)
    if _bundle_dir() is not None:
        return
    skip = pytest.mark.skip(reason=f"no bundle on disk (looked in {_where_we_looked()})")
    for item in items:
        if "needs_rip" in item.keywords:
            item.add_marker(skip)


def _candidates():
    """Every place a bundle might be, most likely first — the app's own answer, not a second one.

    `CS2_BUNDLE_DIR` overrides, which is how CI and a copied bundle get pointed at. That override
    lives in `locate` now rather than here: this file used to apply it and the tools did not, so
    setting it moved the tests onto a bundle and left the tools on another.
    """
    from csiibundle.bake import locate

    return list(locate.candidate_bundle_dirs())


def _where_we_looked():
    return "; ".join(_candidates()) or "nowhere — no candidate path on this OS"


def _bundle_dir():
    for path in _candidates():
        if os.path.isfile(os.path.join(path, "bundle.json")):
            return path
    return None


@pytest.fixture(scope="session")
def repo_root():
    """The checkout root."""
    return REPO


@pytest.fixture(scope="session")
def example_bundle():
    """`docs/example-bundle` — the hand-authored contract artefact, always present."""
    return os.path.join(REPO, "docs", "example-bundle")


@pytest.fixture(scope="session")
def bundle_dir():
    """The real ripped bundle, or None. Ask for `needs_rip` too if None is not survivable."""
    return _bundle_dir()
