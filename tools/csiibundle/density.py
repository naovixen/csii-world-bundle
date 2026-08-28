"""Density subsets and object size — which instances survive as the foliage slider comes down.

Deterministic per record `id`, monotone in the fraction (a lower fraction is always a subset of
a higher one), and weighted so mature trees outlive saplings. `density_rank` is the same rule
seen from the other end, which is what lets a consumer settle a whole prefab's fate for every
slider position in one pass (`cs2beamng.pipeline.meshindex`).
"""

# Weights that decide which instances survive FIRST as the density slider comes down. A lower
# fraction is always a subset of a higher one (keep iff hash < fraction * weight), so thinning
# is stable and monotone. Trees: mature states over young ones. TUNED.
DENSITY_WEIGHT = {
    "Adult": 1.0,
    "Elderly": 1.0,
    None: 1.0,
    "Teen": 0.75,
    "Child": 0.5,
    "Dead": 0.9,
    "Stump": 0.4,
}


def _stable_unit(value):
    """A stable float in [0, 1) from an int id (or any repr) — the same on every platform,
    unlike Python's salted `hash`."""
    import hashlib

    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") / float(1 << 64)


def density_rank(record):
    """The density fraction this record survives at: it is kept iff `rank < fraction`.

    The subset rule turned inside out. `density_keep` answers "is this one in at 60 %"; this
    answers "at what setting does this one come in", which is what lets a consumer decide a
    whole prefab's fate for EVERY slider position from one pass over the layer instead of one
    pass per position (`cs2beamng.pipeline.meshindex`: a shape ships iff its lowest-ranked
    instance is below the slider). Monotone, so the two are the same rule seen from either end.
    """
    state = (record.get("attrs") or {}).get("treeState")
    weight = DENSITY_WEIGHT.get(state, 1.0)
    return _stable_unit(record.get("id")) / weight


def density_keep(record, fraction):
    """Whether to keep this record at foliage/prop density `fraction` (0..1).

    Deterministic per record `id`, monotone in `fraction`, and weighted so that mature trees
    survive longer than saplings. `fraction >= 1` keeps everything; `<= 0` keeps nothing.
    """
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    return density_rank(record) < fraction


def object_radius(bounds_min, bounds_max, scale=None):
    """Half the bounding-box diagonal of a mesh, times its largest scale factor — the size a
    consumer uses to pick imposter/cull thresholds and collider simplicity per object."""
    import numpy

    lo = numpy.asarray(bounds_min, dtype=numpy.float64)
    hi = numpy.asarray(bounds_max, dtype=numpy.float64)
    radius = 0.5 * float(numpy.linalg.norm(hi - lo))
    if scale is not None:
        s = numpy.asarray(scale, dtype=numpy.float64).ravel()
        radius *= float(numpy.abs(s).max()) if s.size else 1.0
    return radius
