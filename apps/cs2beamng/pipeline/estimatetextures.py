"""What the textures will weigh, computed exactly rather than calibrated.

The texture plan is the one half of the estimate that is not a guess: every file the build will
write is named, its dimensions read from its PNG header, and its cooked size worked out. It is
also the slow half, which is why it is warmed on a background thread and asked for by
`plan_is_warm`."""

import os

from csiibundle import png

from ..beamng import dds as dds_mod
from ..beamng import materials as bm
from ..beamng import textureplan
from . import history
from .estimatebase import dir_bytes, load_json

# -- the textures, computed ----------------------------------------------------------------------


def texture_estimate(values, bundle, derived_dir):
    """Every texture the build will write, sized from its own header and the caps.

    The enumeration is `beamng.materials`' - the same one `stages/materials` exports from - over
    every CANONICAL material the bake resolved, every paint variant it baked for a kind the
    settings still recolour, and the area surfaces. What it cannot know without running the
    shapes stage is which of those materials the city actually BINDS, so this is an upper bound:
    on the reference city 257 of 7,973 files (16.9 MiB of 558.2) belonged to materials no shape
    asked for, and that gap is declared below rather than tuned away.

    The ground's own textures are the one part that is not this arithmetic: they are re-encoded
    copies of the bundle's surface pack rather than capped albedo, so they follow their source
    at a measured ratio like a merged tile does.
    """
    plan = _texture_plan(bundle, derived_dir, values)
    caps = bm.caps_from_settings(values)
    use_dds = bool(values.get("texture_dds", True))
    total = files = 0
    for kind, keep_alpha, width, height, colour_type, level in plan["jobs"]:
        cap = textureplan.lod_cap(level, int(caps.get(kind, 0) or 0))
        width, height = textureplan.capped_size(width, height, cap)
        total += dds_mod.encoded_bytes(
            width, height, textureplan.dxgi_format(kind, keep_alpha, colour_type in (0, 4))
        )
        files += 1
    ground = _ground_texture_bytes(bundle)
    out = {
        "bytes": total + ground,
        "materialBytes": total,
        "groundBytes": ground,
        "files": files,
        "unsized": plan["unsized"],
        "format": "dds" if use_dds else "png",
        "basis": (
            "computed from your city's own textures at these caps"
            if use_dds
            else "computed from your city's own textures; they ship as PNG and BeamNG "
            "compresses them on the first load, which is what this counts"
        ),
        "variantFiles": plan["variantFiles"],
        "approximations": [
            {
                "what": "every material the bake resolved is counted, including the ones no shape "
                "in your city binds",
                "why": "which materials are bound is decided by the shapes stage, which is the "
                "build. Counting them all makes this an upper bound rather than a guess",
                "measured": "257 of 7,973 files, 16.9 MiB of 558.2, on the reference city",
            },
            {
                "what": "the ground's own textures follow the bundle's surface pack at a measured "
                "ratio instead of being sized one by one",
                "why": "they are re-encoded and split into base, detail and macro copies by the "
                "terrain stage rather than capped, so their sizes are not the source's",
                "measured": "1.28 on the reference build (193.9 MiB of surfaces -> 247.4 MiB of "
                "ground), and about a fifth high on a level whose terrain settings "
                "differ - it is one ratio over a group that is not one thing",
            },
        ],
    }
    return out


# Four plans, not one. Resolving a plan is ~1 s of path joins and header reads and only the
# three recolouring settings can change it, so a single slot would make flipping `paint_kinds`
# on and off pay for it every time; four covers going back to what you had.
_PLAN_CACHE = {}
_PLAN_CACHE_MAX = 4


def plan_is_warm(bundle, derived_dir, values):
    """Whether the texture plan for these settings is already resolved."""
    return _plan_key(bundle, derived_dir, values) in _PLAN_CACHE


def _plan_key(bundle, derived_dir, _values):
    try:
        stamp = os.stat(os.path.join(derived_dir, "materials.json")).st_mtime_ns
    except OSError:
        stamp = 0
    # keyed on the bake's table alone: the recolouring settings change what the NEXT bake
    # produces, and the plan is a reading of the one on disk
    return (bundle, stamp)


def _texture_plan(bundle, derived_dir, values):
    """The texture jobs with their SOURCE dimensions, cached — the expensive half, done once.

    Only the caps change while a person moves sliders, and a cap is arithmetic on a dimension
    this already holds. Resolving the jobs is not: it walks 4,774 material records, joins ~8,000
    paths and reads a PNG header off each. Cached on what would change the job list — the bake's
    table, and the three recolouring settings.
    """
    key = _plan_key(bundle, derived_dir, values)
    hit = _PLAN_CACHE.get(key)
    if hit is not None:
        return hit

    table = load_json(os.path.join(derived_dir, "materials.json")) or {}
    materials = table.get("materials") or {}
    canonical = sorted({rec.get("canonical") or name for name, rec in materials.items()})
    jobs = {}
    # a job's level is the nearest any material binding it is seen at; LOD 0 anywhere wins
    levels = textureplan.material_lod_levels(bundle, table)
    for name, plan in textureplan.plan_canonical(bundle, derived_dir, table, canonical).items():
        level = int(levels.get(name, 0))
        for job in plan.values():
            jobs[job] = min(jobs.get(job) if jobs.get(job) is not None else level, level)
    variant_files = 0
    for job in _variant_jobs(values, derived_dir, table):
        if job not in jobs:
            variant_files += 1
        jobs[job] = 0
    for job in textureplan.plan_areas(bundle, derived_dir, has_alpha=_header_has_alpha):
        jobs[job] = 0

    sized, unsized = [], 0
    for (src, kind, keep_alpha), level in jobs.items():
        info = _png_header(src)
        if info is None:
            unsized += 1
            continue
        sized.append((kind, keep_alpha, info[0], info[1], info[2], int(level or 0)))
    out = {"jobs": sized, "unsized": unsized, "variantFiles": variant_files}
    while len(_PLAN_CACHE) >= _PLAN_CACHE_MAX:
        _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
    _PLAN_CACHE[key] = out
    return out


def _ground_texture_bytes(bundle):
    """What `art/terrains/` and `art/ground/` will weigh: the surface pack, times its ratio."""
    source = dir_bytes(os.path.join(bundle, "terrain", "surfaces"))
    ratio = history.load().get("size/ground_texture_per_source") or 1.0
    return int(source * ratio)


def _variant_jobs(_values, derived_dir, table):
    """The tinted-albedo job for every paint variant the bake baked.

    A variant is another albedo (and, where the material cuts or blends, another opacity map) and
    nothing else - the tint is in the texels, so the mask is dropped and every other slot is
    shared with the canonical material.

    EVERY variant IN THE TABLE, and neither filtered by `paint_kinds` nor capped at
    `paint_variants_tree`. Both of those are BAKE options: they decide which kinds get colour
    centres computed and how many. Nothing in the build consults them afterwards - `stages/shapes`
    reads the bake's `paint_variants.json` and `stages/materials` reads its table, and both take
    what is there - so filtering here would be a rule the pipeline does not have. It was one:
    with the setting cleared this predicted no variant textures at all while the build went on
    exporting the 490 the last bake had produced. Capping per material was wrong for its own
    reason - one albedo serves many prefabs, so a material can carry more variant keys than the
    per-prefab setting names, and the cap lost 10 files on the reference bundle.

    What the setting changes is what the NEXT bake produces. `stale_bake_note` is what says the
    figures on screen are for the last one.
    """
    for _raw, rec in (table.get("materials") or {}).items():
        keys = sorted((rec.get("derived") or {}).get("variants") or {})
        for vkey in keys:
            src = textureplan.variant_albedo(derived_dir, rec, vkey)
            if not src:
                continue
            yield (src, "color", False)
            if (rec.get("alphaMode") or "opaque") != "opaque":
                yield (src, "opacity", False)


_HEADER_CACHE = {}


def _png_header(path):
    """`(width, height, colourType)` from a PNG's IHDR, cached on (path, mtime, size).

    26 bytes per file, read by `csiibundle.png.read_header`. The reference city's texture plan is
    ~8,000 of them and the meter re-runs on every settings change, so the cache is what keeps it
    under a frame — the cache is this function's reason to exist; the parsing never was.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (path, stat.st_mtime_ns, stat.st_size)
    hit = _HEADER_CACHE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    header = png.read_header(path)
    out = None if header is None else (header[0], header[1], header[3])
    _HEADER_CACHE[path] = (key, out)
    return out


def _header_has_alpha(path):
    """Whether an area surface's albedo carries an alpha channel at all.

    The build's own test (`stages/materials._has_alpha`) decodes the image and asks whether any
    texel is actually transparent; this asks the header whether there is a channel to be
    transparent in, which is the same answer wherever the alpha is used and one file read
    instead of a full decode. It can only ever count an opacity map the build will not write -
    0.6 MiB over 14 files on the reference city.
    """
    info = _png_header(path)
    return info is not None and info[2] in (4, 6)
