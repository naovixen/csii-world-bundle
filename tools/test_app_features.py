"""cs2beamng A0 gate, part 3 — the app around the build.

Run: `python tools/test_app_features.py`. Split from `test_app_shell.py` (Phase 4); the shell
proper lives there and the incremental build in `test_app_incremental.py`.

The frozen build's streams (D3), the in-app mod installer (D3/D4), the map view's
georeference / points / snap (E3), the shared-state codec, and the three bake-product tables
agreeing.
"""

import inspect
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
from shellcommon import EXAMPLE, REPO, check, run_gate  # noqa: E402


def test_frozen_streams():
    """A double-clicked windowed build has no console: `sys.stdout` is None and the first
    `print()` anywhere in the pipeline raises. `cs2beamng._frozen_streams` gives it a log file
    instead. Tested by simulation, because the failure only exists in a frozen build."""
    import cs2beamng

    print("frozen build: streams with no console")
    real_out, real_err, real_frozen = sys.stdout, sys.stderr, getattr(sys, "frozen", None)
    replaced = wrote = False
    log = os.path.join(os.environ["CS2BEAMNG_HOME"], "cs2beamng.log")
    try:
        # Nothing may call check() in here: it prints, and printing is exactly what is being
        # redirected — the results would land in the log file instead of the terminal.
        sys.stdout = sys.stderr = None
        sys.frozen = True
        cs2beamng._frozen_streams()
        replaced = sys.stdout is not None and sys.stderr is not None
        print("hello from a build with no console", file=sys.stdout)  # must not raise
        sys.stdout.flush()
        wrote = os.path.isfile(log) and "no console" in open(log, encoding="utf-8").read()
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout, sys.stderr = real_out, real_err
        if real_frozen is None:
            del sys.frozen
        else:
            sys.frozen = real_frozen
    check(replaced, "stdout and stderr are replaced when there is no console")
    check(wrote, f"what the pipeline prints lands in {log}")
    # unfrozen it must do nothing at all
    cs2beamng._frozen_streams()
    check(sys.stdout is real_out, "unfrozen, it leaves the streams alone")


def _mod_zips(root):
    """A well-formed release zip and one missing the Burst library."""
    import zipfile

    os.makedirs(root, exist_ok=True)
    good = os.path.join(root, "CartoRipper-9.9.9-cs2-1.6.zip")
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("Carto/Carto.dll", b"dll")
        archive.writestr("Carto/Carto_win_x86_64.dll", b"burst")
        archive.writestr("Carto/HOW-TO-INSTALL.txt", b"read me")
    thin = os.path.join(root, "CartoRipper-thin.zip")
    with zipfile.ZipFile(thin, "w") as archive:
        archive.writestr("Carto/Carto.dll", b"dll")  # no Burst library
    return good, thin


def test_modinstall(tmp):
    """The app installs the ripper mod itself when a release build shipped the zip beside it.
    What is checked here is the contract: a zip missing the Burst library is refused, a bad
    checksum is refused, and a good one replaces the folder rather than merging into it."""
    from cs2beamng import modinstall

    print("mod installer")
    root = os.path.join(tmp, "modinstall")
    good, thin = _mod_zips(root)

    target = os.path.join(root, "Mods", "Carto")
    real_install_dir = modinstall.install_dir
    modinstall.install_dir = lambda: target
    try:
        try:
            modinstall.install(thin)
            check(False, "a zip without the Burst library is refused")
        except ValueError as exc:
            check("Carto_win_x86_64.dll" in str(exc), "a zip without the Burst library is refused")

        with open(good + ".sha256", "w", encoding="utf-8") as handle:
            handle.write("0" * 64 + "  wrong\n")
        try:
            modinstall.install(good)
            check(False, "a zip that does not match its sha256 is refused")
        except ValueError as exc:
            check("sha256" in str(exc), "a zip that does not match its sha256 is refused")
        os.remove(good + ".sha256")

        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "Carto_win_x86_64.dll"), "w", encoding="utf-8") as handle:
            handle.write("a Burst library from an older build")
        with open(os.path.join(target, "stale.dll"), "w", encoding="utf-8") as handle:
            handle.write("left over")

        where = modinstall.install(good, version="9.9.9")
        check(os.path.isfile(os.path.join(where, "Carto.dll")), "the mod is unpacked")
        check(
            not os.path.isfile(os.path.join(where, "stale.dll")),
            "the folder is REPLACED, not merged (a stale Burst library is neither version)",
        )
        check(
            open(os.path.join(where, "Carto_win_x86_64.dll"), encoding="utf-8").read() == "burst",
            "the new Burst library won",
        )
        check(
            modinstall.installed_version() == "9.9.9",
            "the installed version is stamped and read back",
        )
    finally:
        modinstall.install_dir = real_install_dir


# E3 - the map's conversions. Everything the picker produces is read back by something else:
# the region string by `settings.region_of` and every stage's own test, the point list by
# `stages.poisources.read_poi_file` on the far side of a bundle->BeamNG frame change. A pin that
# lands somewhere else in the built level than it sat on the map is the whole feature failing
# silently, so the round trip is the gate and not the pixels.
_WORLD = {"originX": -7166.25, "originZ": 7166.25, "metresPerPixel": 14.0, "pixels": 1024}
_SPAN = _WORLD["pixels"] * _WORLD["metresPerPixel"]


def _to_fraction(x, z):
    return ((x - _WORLD["originX"]) / _SPAN, (_WORLD["originZ"] - z) / _SPAN)


def _to_world(fx, fy):
    return (_WORLD["originX"] + fx * _SPAN, _WORLD["originZ"] - fy * _SPAN)


def test_map_georeference():
    """A pixel is a place and a place is a pixel, and the region string the picker writes is
    the one every stage reads."""
    from cs2beamng import settings as settings_mod

    print("the map: georeference and the region string (E3)")
    north_west = (_WORLD["originX"], _WORLD["originZ"])
    south_east = (_WORLD["originX"] + _SPAN, _WORLD["originZ"] - _SPAN)
    check(
        _to_fraction(*north_west) == (0.0, 0.0), "the map's top-left pixel is the NORTH-WEST corner"
    )
    check(_to_fraction(*south_east) == (1.0, 1.0), "...and its bottom-right is the south-east one")
    middle = _to_world(0.5, 0.5)
    back = _to_fraction(*middle)
    check(
        abs(back[0] - 0.5) < 1e-9 and abs(back[1] - 0.5) < 1e-9,
        "a place round-trips to its own pixel",
    )

    # -- the region string the picker writes is the one every stage reads -----------------------
    written = f"{round(middle[0])} {round(middle[1])} {1500}"
    parsed = settings_mod.region_of({"region": written})
    check(
        parsed and round(parsed["centerX"]) == round(middle[0]) and parsed["radius"] == 1500,
        f"the picker's region string parses to the place it was dragged to ({written})",
    )
    check(
        not settings_mod.validate(
            {
                "bundle": EXAMPLE,
                "output": "x",
                "level_name": "a",
                "foliage_density": 100,
                "prop_density": 100,
                "region": written,
            }
        ),
        "...and passes the validator the CLI uses",
    )


def test_map_points():
    """The points survive the app -> build -> BeamNG frame change, with their names."""
    from cs2beamng.server import mappoints
    from cs2beamng.stages import poisources
    from csiibundle.bake.progress import Progress

    print("the map: the points (E3)")
    home = os.environ["CS2BEAMNG_HOME"]
    points = [
        {"name": "The keys, bottom-left", "at": [-2861.5, -6398.6], "description": "south"},
        {"name": "Docks", "at": [4820.0, 1660.3]},
    ]
    path, clean = mappoints.save_pois(EXAMPLE, points)
    check(
        len(clean) == 2 and path.startswith(home),
        "the app writes the point file into its own folder",
    )
    read = poisources.read_poi_file(path, Progress())
    check(len(read) == 2, f"the build reads back every point the app wrote ({len(read)})")
    # bundle (x, y=up, z=north) -> BeamNG (x, y=north): the pin's X/Z become the map's X/Y, and a
    # transposition here puts every fast-travel point somewhere else in the level.
    check(
        [p["at_beamng"] for p in read] == [(-2861.5, -6398.6), (4820.0, 1660.3)],
        f"...at the same places, through the frame change: {[p['at_beamng'] for p in read]}",
    )
    _p, rounded = mappoints.save_pois(EXAMPLE, [{"name": "n", "at": [1.234567, -2.987654]}])
    check(
        rounded[0]["at"] == [1.2, -3.0], "a point is stored to a tenth of a metre, which is a road"
    )
    check([p["name"] for p in read] == [p["name"] for p in points], "...with their names")
    _path, empty = mappoints.save_pois(EXAMPLE, [{"at": [1, 2]}, {"name": "no place"}])
    check(empty == [], "a point with no name or no place is not stored")
    mappoints.save_pois(EXAMPLE, [])  # leave nothing behind for the next run


def test_map_snap():
    """The snap's samples carry the street name as an index beside them (an off-by-one there
    names every point wrong), and a snap with no map says so instead of throwing."""
    from cs2beamng.server import mappoints, mapstore

    print("the map: the snap (E3)")
    samples = os.path.join(tempfile.mkdtemp(prefix="cs2beamng_samples_"), "s.npy")
    mappoints.save_samples(
        samples,
        [
            (10.0, 0.0, 20.0, "Hickory Lane"),
            (30.0, 0.0, 40.0, None),
            (50.0, 0.0, 60.0, "Empire Street"),
        ],
        ["Hickory Lane", "Empire Street"],
    )
    import numpy

    rows = numpy.load(samples)
    check(
        rows.shape == (3, 3), f"the snap's samples are (N, 3): x, z and a name index {rows.shape}"
    )
    check(
        [int(r[2]) for r in rows] == [0, -1, 1],
        "...with an index per row, and -1 where the road has no name",
    )

    # -- and it says what it is waiting for rather than throwing --------------------------------
    check(
        mapstore.load(os.path.join(REPO, "nowhere")) is None,
        "no map for a folder that is not a bundle",
    )
    nowhere = mappoints.snap(os.path.join(REPO, "nowhere"), (1.0, 2.0))
    check(
        nowhere["snapped"] is False and nowhere["at"] == [1.0, 2.0],
        "a snap with no map leaves the point where it was put, and says it did not snap",
    )


def test_state_codec():
    """The `ctx.shared` payload survives a JSON round trip — sets, tuple-keyed dicts and all —
    and a delta composes the way three stages accumulating into one set need it to."""
    from cs2beamng.pipeline import state as state_mod
    from cs2beamng.pipeline import statepayload

    print("state codec")
    value = {
        "results": {
            ("a", "", None): {"file": "a.dae", "n": 1},
            ("b", "__dead", 2): {"file": "b.dae"},
        },
        "used": {"x", "y"},
        "plain": [1, 2.5, "z", None],
        "nested": {"k": {"p", "q"}},
    }
    back = state_mod.decode(json.loads(json.dumps(state_mod.encode(value))))
    check(back == value, "shared payload round-trips through JSON")
    before = {"materials_used": {"a"}}
    after = {"materials_used": {"a", "b"}}
    delta = statepayload.diff_shared(before, after, ("materials_used",))
    check(
        delta["materials_used"] == {"b"}, "a set delta is what the stage ADDED, not the whole set"
    )
    shared = {"materials_used": {"c"}}
    statepayload.apply_shared(shared, delta)
    check(
        shared["materials_used"] == {"b", "c"}, "replaying a set delta unions rather than replaces"
    )
    variants = statepayload.diff_shared(
        {"v": {"k1": {"m"}}}, {"v": {"k1": {"m", "n"}, "k2": {"o"}}}, ("v",)
    )
    target = {"v": {"k1": {"m"}}}
    statepayload.apply_shared(target, variants)
    check(
        target["v"] == {"k1": {"m", "n"}, "k2": {"o"}}, "a nested dict-of-sets delta merges per key"
    )


def test_bake_product_tables():
    """Every bake product is in all three tables that name products, and nothing else is.

    They hold different things — `PRODUCT_MODULES` is bake ORDER, `census.units` is how many
    units a product will do, `history.DEFAULT_RATES` is how fast it does them — so they cannot be
    one list. But they enumerate the same set, and adding a product means remembering three
    files. `terraincut` was missing from two of them until refactor Phase 3: it contributed
    nothing to the preflight's expected column and its 57 s arrived as time nobody had promised.
    """
    from cs2beamng.pipeline import census as census_mod
    from cs2beamng.pipeline.history import DEFAULT_RATES
    from csiibundle.bake import PRODUCT_MODULES

    print("bake product tables")
    products = set(PRODUCT_MODULES)

    rated = {k.split("/", 1)[1] for k in DEFAULT_RATES if k.startswith("bake/")}
    check(
        not (products - rated),
        f"every product has a shipped rate; missing {sorted(products - rated)}",
    )
    check(not (rated - products), f"every rate names a product; extra {sorted(rated - products)}")

    # `census.units` is built inside `run`, so read the keys out of the source rather than
    # standing up a whole census over a bundle.
    source = inspect.getsource(census_mod)
    counted = set(re.findall(r'"bake/([a-z]+)":', source))
    check(
        not (products - counted),
        f"every product is counted by the census; missing {sorted(products - counted)}",
    )
    check(
        not (counted - products),
        f"every counted key names a product; extra {sorted(counted - products)}",
    )


def _body(tmp):
    test_frozen_streams()
    test_modinstall(tmp)
    test_map_georeference()
    test_map_points()
    test_map_snap()
    test_state_codec()
    test_bake_product_tables()


def main():
    return run_gate(_body)


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_app_features.py`."""
    assert main() in (None, 0)
