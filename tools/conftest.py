"""What every test in this directory can assume, in one place.

Before this file each test file opened with its own bootstrap — 35 `sys.path.insert` calls in
nine different spellings — and each one that wanted the real bundle spelled the path out again,
in two different ways, seven of them in a form that cannot resolve off Windows. The paths are
now a fixture and the bootstrap is here.

    pytest tools                     # everything
    pytest tools -m "not needs_rip"  # only what runs without a rip on disk
    pytest tools/test_a1_level.py    # one file
    python tools/test_a1_level.py    # still works: every file keeps its script entry point

**What pytest collects here is `gate()`, not `test_*`.** These files predate pytest and use
`test_<claim>()` for their own internal helpers, most of which take positional arguments that
are not fixtures. `python_functions = ["gate"]` in pyproject.toml means one collected test per
file — the file's own entry point — and leaves the helper naming alone. Refactor Phase 4 splits
the four oversized test files into real per-claim pytest functions; this is the seam for that.

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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_rip: requires a real CS2 World Bundle on disk; skipped when there is none. "
        "Only for tests that cannot run at all without one.",
    )


def pytest_collection_modifyitems(items):
    """Skip `needs_rip` tests when no bundle is on disk, naming where we looked."""
    if _bundle_dir() is not None:
        return
    skip = pytest.mark.skip(reason=f"no bundle on disk (looked in {_where_we_looked()})")
    for item in items:
        if "needs_rip" in item.keywords:
            item.add_marker(skip)


def _candidates():
    """Every place a bundle might be, most likely first — the app's own answer, not a second one.

    `CS2_BUNDLE_DIR` overrides, which is how CI and a copied bundle get pointed at.
    """
    override = os.environ.get("CS2_BUNDLE_DIR")
    if override:
        return [override]
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
