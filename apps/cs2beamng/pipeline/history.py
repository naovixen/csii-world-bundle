"""Per-stage throughput history — what makes the FIRST estimate on a new machine honest.

`~/.cs2beamng/history.json` keeps an exponential moving average of units-per-second per stage
(and per phase, keyed `stage/phase`), updated after every completed stage. Before a build, the
census's unit counts × these rates give the "expected" column of the preflight screen; during a
build the live rate takes over. Shipped defaults (`DEFAULT_RATES`) are the numbers measured on
the development machine (2026-08-15) so a fresh install still shows a first estimate.
"""

import json
import os

from ..settings import app_dir

HISTORY_FILE = "history.json"
EMA = 0.5

# units per second, measured on the reference city (see docs/plans/beamng-consumer-plan.md log)
DEFAULT_RATES = {
    "bake/materials": 4641 / 453.0,           # materials
    "bake/lods": 5575 / 200.0,                # meshes
    "bake/markings": 117374 / 90.0,           # lane records
    "bake/lanegeometry": 117374 / 142.0,
    "bake/areas": 24938 / 90.0,
    "bake/skirts": 1226046 / 60.0,            # instances scanned
    "bake/terrainmask": 1 / 45.0,
    "terrain": 4096 * 4096 / 30.0,            # samples
    "water": 1 / 20.0,
    "scene": 641639 / 25.0,                   # lanes streamed for the spawn pick
    "materials": 4641 / 120.0,
    "shapes": 5575 / 300.0,
    "networks": 8259 / 120.0,
    "derived": 3000 / 120.0,                  # tiles
    "instances": 1226046 / 240.0,
    "lights": 654 / 5.0,
    "validate": 1 / 30.0,
}

# The SIZE coefficients, in the same file and blended the same way — what a source byte becomes
# once BeamNG's own compiler has cooked it. MEASURED on the reference city's 2026-08-20 build
# (`cs2_city_fine`: 4,272 object shapes and 3,527 merged tiles, 3,466.5 MiB of `.cdae`) by
# matching every cooked file back to the glTF it came from:
#
#   building 1,166 shapes  1,662.6 MiB source -> 1,702.5 MiB cooked
#   prop     2,853 shapes    296.9            ->   193.8
#   tree       253 shapes    102.7            ->   105.8
#   networks 1,024 tiles     602.2            ->   787.1
#   areas      746 tiles     252.3            ->   383.1
#   markings 1,114 tiles      81.9            ->   130.0
#   skirts     641 tiles     103.1            ->   158.1
#   terraincut  82 tiles       3.3            ->     6.1
#
# The four tile ratios run high because `tile_lod` builds their LOD chain at BUILD time, so the
# cooked file carries levels the source glTF does not. `size/builds` is how many of the user's
# own builds have been blended in; the meter says "from the reference city" until it is 1.
DEFAULT_SIZES = {
    "size/cdae_per_source_building": 1.024,
    "size/cdae_per_source_prop": 0.653,
    "size/cdae_per_source_tree": 1.030,
    "size/cdae_per_source_networks": 1.307,
    "size/cdae_per_source_areas": 1.518,
    "size/cdae_per_source_markings": 1.586,
    "size/cdae_per_source_skirts": 1.533,
    "size/cdae_per_source_terraincut": 1.860,
    # The ground's textures against the bundle's surface pack: 193.9 MiB of surfaces became
    # 235.3 MiB in `art/terrains/` plus 12.1 MiB of blend maps in `art/ground/`. They are
    # re-encoded and split into base/detail/macro copies rather than capped, so they follow
    # their source rather than the texture caps.
    "size/ground_texture_per_source": 1.276,
    "size/builds": 0.0,
}
DEFAULT_RATES.update(DEFAULT_SIZES)


def _path():
    return os.path.join(app_dir(), HISTORY_FILE)


def load():
    rates = dict(DEFAULT_RATES)
    try:
        with open(_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            for key, value in saved.items():
                if isinstance(value, (int, float)) and value > 0:
                    rates[key] = float(value)
    except (OSError, ValueError):
        pass
    return rates


def update(key, units, seconds):
    """Blend one completed stage/phase's throughput into the history."""
    if units is None or units <= 0 or seconds <= 0:
        return
    rates = load()
    rate = units / seconds
    rates[key] = rate if key not in rates else (EMA * rate + (1 - EMA) * rates[key])
    _write(rates)


def observe(key, value, blend=True):
    """Blend (or set) one measured coefficient — the size half of what `update` does for rates.

    `update` takes a numerator and a denominator because a throughput is one; a size ratio is
    already the number, and a counter (`size/builds`) must not be averaged at all.
    """
    if value is None or value <= 0:
        return
    rates = load()
    rates[key] = float(value) if not blend or key not in rates else (
        EMA * float(value) + (1 - EMA) * rates[key])
    _write(rates)


def _write(rates):
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(rates, handle, indent=1, sort_keys=True)
        os.replace(tmp, _path())
    except OSError:
        pass


def estimate_seconds(key, units, rates=None):
    rates = rates or load()
    rate = rates.get(key)
    if not rate or units is None:
        return None
    return float(units) / rate


def stage_estimate(key, units, estimates=None, rates=None):
    """Seconds for one stage. Prefers the census's per-key estimates (`estimates[key]` and every
    `estimates[key + "/..."]` — the bake stage is the sum of its products, and a fresh product
    costs 0); falls back to `units / rate` for the stage key itself. None when unknown."""
    if estimates:
        parts = [v for k, v in estimates.items() if k == key or k.startswith(key + "/")]
        if parts:
            if any(p.get("seconds") is None for p in parts):
                return None
            return float(sum(p.get("seconds") or 0.0 for p in parts))
    return estimate_seconds(key, units, rates)
