"""The command line after the `--`, as one record.

Every flag the entry script's docstring documents is parsed here and nowhere else. Two of them
are not plain booleans and both are the reason this is a record rather than a dict:
`--everything R` is a BASE that any explicitly named option overrides, and `--center` takes
either a pair of coordinates or one of three names the survey resolves later.
"""

import os
from dataclasses import dataclass

from csiibundle import networks

# The sentinel for "not named on the command line", so `--everything` can supply a default for
# an option while an explicit flag still wins. `None` cannot serve: it is a legal value for
# `--limit` and for `--net-categories`.
_UNSET = object()


@dataclass
class Options:
    """One parsed command line. `center` is `(x, z)` or one of the three survey names."""

    bundle_dir: str = ""
    kinds: set = _UNSET
    limit: int | None = _UNSET
    assets: list | None = None
    center: object = None
    radius: float | None = None
    keep: bool = False
    save_path: str | None = None
    networks: bool = False
    net_categories: set | None = _UNSET
    net_limit: int | None = None
    lanes: bool = False
    lane_markings: bool = False
    skirts: bool = False
    terrain: bool = False
    areas: bool = False
    clip_floor: float | None = None
    coarse_terrain: bool = False
    colour: bool = False
    sky: bool = False


class Unknown(Exception):
    """An option the parser does not know. Carries the flag as written."""


def _bundle_dir(argv):
    """`(bundle_dir, rest)` — the optional leading directory, or this machine's default.

    Since 2026-08-09 the producer writes straight into ModsData/Carto (the development-era
    spike/ subfolder is retired), so that is the default. An explicit first argument that is not
    an option still wins. Not spelled out here: `locate` knows Windows, Proton and macOS and
    honours `CS2_BUNDLE_DIR`, and this was one of four places that had its own Windows-only copy.
    """
    if argv and not argv[0].startswith("--"):
        return argv[0], argv[1:]
    from csiibundle.bake.locate import default_bundle_dir

    return default_bundle_dir(), argv


def _flag(opts, arg):
    """Set one no-argument flag. Returns False when `arg` is not one."""
    flags = {
        "--networks": "networks",
        "--lanes": "lanes",
        "--lane-markings": "lane_markings",
        "--skirts": "skirts",
        "--terrain": "terrain",
        "--colour": "colour",
        "--color": "colour",
        "--sky": "sky",
        "--coarse-terrain": "coarse_terrain",
        "--areas": "areas",
        "--keep-existing": "keep",
    }
    name = flags.get(arg)
    if name is None:
        return False
    setattr(opts, name, True)
    return True


def _valued(opts, arg, argv, i):
    """Set one option that takes arguments. Returns how many argv entries it consumed, or 0."""
    if arg == "--kinds":
        kinds = {k.strip() for k in argv[i + 1].split(",") if k.strip()}
        opts.kinds = set() if kinds == {"none"} else kinds
        return 2
    if arg == "--clip-floor":
        opts.terrain = True
        opts.clip_floor = float(argv[i + 1])
        return 2
    if arg == "--net-limit":
        opts.networks = True
        opts.net_limit = int(argv[i + 1])
        return 2
    if arg == "--net-categories":
        opts.networks = True
        value = argv[i + 1].strip()
        opts.net_categories = (
            None if value == "all" else {c.strip() for c in value.split(",") if c.strip()}
        )
        return 2
    if arg == "--limit":
        opts.limit = int(argv[i + 1])
        return 2
    if arg == "--assets":
        opts.assets = [a.strip().lower() for a in argv[i + 1].split(",") if a.strip()]
        return 2
    if arg == "--radius":
        opts.radius = float(argv[i + 1])
        return 2
    if arg == "--save":
        opts.save_path = os.path.abspath(argv[i + 1])
        return 2
    if arg == "--center":
        if argv[i + 1] in ("auto", "downtown", "interchange"):
            opts.center = argv[i + 1]
            return 2
        opts.center = (float(argv[i + 1]), float(argv[i + 2]))
        return 3
    return 0


def _apply_everything(opts, radius):
    """`--everything R` is a base, not an override: anything named explicitly still wins, so
    `--everything 400 --kinds building` is a legal way to say "that region, buildings only"."""
    opts.networks = True
    opts.lanes = True
    opts.skirts = True
    opts.terrain = True
    opts.areas = True
    if opts.kinds is _UNSET:
        opts.kinds = set(networks.KINDS)
    if opts.net_categories is _UNSET:
        opts.net_categories = None  # every category
    if opts.limit is _UNSET:
        opts.limit = None  # the radius is the limit
    if opts.center is None:
        opts.center = "downtown"
    if opts.radius is None:
        opts.radius = radius


def parse(argv):
    """Parse the arguments after the `--`. Raises `Unknown` for a flag this does not know."""
    opts = Options()
    opts.bundle_dir, argv = _bundle_dir(argv)
    everything = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--everything":
            everything = float(argv[i + 1])
            i += 2
            continue
        if _flag(opts, arg):
            i += 1
            continue
        step = _valued(opts, arg, argv, i)
        if not step:
            raise Unknown(arg)
        i += step

    if everything is not None:
        _apply_everything(opts, everything)
    if opts.kinds is _UNSET:
        opts.kinds = {"building"}
    if opts.net_categories is _UNSET:
        opts.net_categories = set(networks.DRIVABLE)
    if opts.limit is _UNSET:
        opts.limit = 20000
    return opts
