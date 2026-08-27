"""The cost estimate — what this level will ask of the machine, before it is built.

A settings screen whose "frame" takes an hour and a half to render owes the person moving the
sliders a live answer to "will this run": every game that ships a graphics menu puts a video
memory bar under it, and this app is a graphics menu with a very slow preview. The number that
decides whether a BeamNG level runs is what it MOUNTS - every shape and texture, whether or not
it is on screen - which is what `beamng.validate_level.resident_budget_mib` is expressed in and
what the build's own verdict is measured against.

So this module predicts exactly the quantity the validator measures (`_resident_bytes`: the
`.cdae` beside every shape plus every texture under `art/`), out of two halves that are honest
about themselves in different ways:

**Textures — computed.** Every texture the build will write is enumerated through
`beamng.materials.plan_canonical` / `plan_areas`, the same walk the materials stage exports
from; each one's source dimensions come out of the PNG header (33 bytes), the cap is the
setting, and the file size of a block-compressed mipmapped DDS is arithmetic
(`beamng.dds.encoded_bytes`). This is not a model of the exporter - it is the exporter's own
sizing. MEASURED against a real build 2026-08-20: 7,716 of 7,716 shared files byte-for-byte,
541.3 MiB predicted against 541.3 MiB written.

**Meshes — calibrated.** WHICH shapes ship is exact, out of `meshindex` (one permissive pass
over the instance layer, read back at any settings). What each one WEIGHS as a cooked `.cdae` is
its source glTF times a ratio measured from real builds - 1.02 for a building, 0.65 for a prop,
1.03 for a tree on the reference city - blended towards the user's own results after every build
(`history.observe`). A ratio is a ratio and not a guess, but it is not arithmetic either, and
the meter says which figure is which.

Everything the estimate cannot do exactly is listed in `approximations` and shown on screen, and
after every build the report prints the estimate beside the measurement. An estimator nobody
scores is an estimator nobody can trust.
"""

import json
import os

from csiibundle import png

from .. import settings as settings_mod
from ..beamng import dds as dds_mod
from ..beamng import materials as bm
from . import history, meshindex

MIB = 1024.0 * 1024.0

# The kinds `meshindex` records, and the settings section whose controls move each one - what
# the meter's "What should I change?" link points at.
KIND_SECTION = {"building": "world", "prop": "world", "tree": "world"}

# The merged world tiles: (settings switch, the bake product or bundle layer they are built from,
# the level's shape group). Their bytes follow their INPUT rather than an instance count, because
# a tile is one shape however many records went into it.
#
# The region is a SQUARE in every stage that reads one (`beamng.placement.in_region`,
# `stages/derived._tile_in_region`, `stages/water._intersects`, and `stages/networks` since
# 2026-08-20, when the map picker had to draw it and found a circle there).
TILE_GROUPS = [
    ("networks", ("networks",), "networks"),
    ("areas", ("derived", "areas"), "areas"),
    ("markings", ("derived", "markings"), "markings"),
    ("skirts", ("derived", "skirts"), "skirts"),
    ("terrain_cut", ("derived", "terraincut"), "terraincut"),
]


def take(values, census=None, index=None, warm_only=False):
    """The whole estimate for these settings, or a `ready: False` document saying what is missing.

    `index` is a `meshindex` document; without one the mesh half cannot be scoped to the kind
    switches and is not offered at all, because a video memory figure that does not move when
    you turn the buildings off is worse than no figure.

    `warm_only` refuses to do the one slow piece - resolving the bundle's texture plan, a second
    of path joins and header reads - and asks to be called again off the request thread instead.
    """
    bundle = values.get("bundle") or ""
    out = {"ready": False, "rows": [], "approximations": [], "waiting": None}
    if not os.path.isfile(os.path.join(bundle, "bundle.json")):
        out["waiting"] = "no bundle"
        return out
    derived_dir = os.path.join(bundle, "derived")
    if not os.path.isfile(os.path.join(derived_dir, "materials.json")):
        # The first build's bake writes it. Until then there is nothing to size - and saying so
        # is the honest state, not a number with a shrug attached.
        out["waiting"] = "bake"
        return out
    # The index first: it is the slower of the two things being waited for by an order of
    # magnitude, and saying "measuring your textures" while a twenty-second pass over the
    # instance layer is what is actually holding the number up would be the wrong answer to
    # "how long is this going to take".
    if not index:
        out["waiting"] = "index"
        return out
    if warm_only and not plan_is_warm(bundle, derived_dir, values):
        out["waiting"] = "reading"
        return out

    textures = texture_estimate(values, bundle, derived_dir)
    meshes = mesh_estimate(values, bundle, index, census)

    rates = history.load()
    resident = textures["bytes"] + meshes["bytes"]
    budget = _budget(values)
    out.update(
        {
            "ready": True,
            "textures": textures,
            "meshes": meshes,
            "residentBytes": resident,
            "residentMiB": round(resident / MIB, 1),
            "diskBytes": resident + _terrain_file_bytes(values, bundle),
            # The `.dae` sources are NOT in it and the basis says so rather than guessing at them:
            # nothing has measured what they weigh against the cooked files, and a made-up multiple
            # of several gigabytes is the kind of number this meter exists not to print.
            "diskBasis": (
                "shapes, textures and the heightfield; the COLLADA sources you keep are on top"
                if values.get("keep_dae", True)
                else "shapes, textures and the heightfield"
            ),
            "budget": budget,
            "approximations": (
                textures["approximations"] + meshes["approximations"] + _stale_bake_note(census)
            ),
            "coefficients": {
                k: round(v, 4) for k, v in sorted(rates.items()) if k.startswith("size/")
            },
        }
    )
    out["rows"] = _rows(out, values)
    out["overSections"] = sorted(
        {row["section"] for row in out["rows"] if row.get("band") == "over" and row.get("section")}
    )
    return out


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
    for kind, keep_alpha, width, height, colour_type in plan["jobs"]:
        width, height = bm.capped_size(width, height, int(caps.get(kind, 0) or 0))
        total += dds_mod.encoded_bytes(
            width, height, bm.dxgi_format(kind, keep_alpha, colour_type in (0, 4))
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


def _plan_key(bundle, derived_dir, values):
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

    table = _load_json(os.path.join(derived_dir, "materials.json")) or {}
    materials = table.get("materials") or {}
    canonical = sorted({rec.get("canonical") or name for name, rec in materials.items()})
    jobs = {}
    for plan in bm.plan_canonical(bundle, derived_dir, table, canonical).values():
        for job in plan.values():
            jobs[job] = None
    variant_files = 0
    for job in _variant_jobs(values, derived_dir, table):
        if job not in jobs:
            variant_files += 1
        jobs[job] = None
    for job in bm.plan_areas(bundle, derived_dir, has_alpha=_header_has_alpha):
        jobs[job] = None

    sized, unsized = [], 0
    for src, kind, keep_alpha in jobs:
        info = _png_header(src)
        if info is None:
            unsized += 1
            continue
        sized.append((kind, keep_alpha, info[0], info[1], info[2]))
    out = {"jobs": sized, "unsized": unsized, "variantFiles": variant_files}
    while len(_PLAN_CACHE) >= _PLAN_CACHE_MAX:
        _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
    _PLAN_CACHE[key] = out
    return out


def _ground_texture_bytes(bundle):
    """What `art/terrains/` and `art/ground/` will weigh: the surface pack, times its ratio."""
    source = _dir_bytes(os.path.join(bundle, "terrain", "surfaces"))
    ratio = history.load().get("size/ground_texture_per_source") or 1.0
    return int(source * ratio)


def _variant_jobs(values, derived_dir, table):
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

    What the setting changes is what the NEXT bake produces. `_stale_bake_note` is what says the
    figures on screen are for the last one.
    """
    for _raw, rec in (table.get("materials") or {}).items():
        keys = sorted((rec.get("derived") or {}).get("variants") or {})
        for vkey in keys:
            src = bm.variant_albedo(derived_dir, rec, vkey)
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


# -- the meshes, calibrated -----------------------------------------------------------------------


def mesh_estimate(values, bundle, index, census=None):
    """What the level's `.cdae` files will weigh: the exact shape list, times measured ratios."""
    rates = history.load()
    map_size = float(((census or {}).get("bundle") or {}).get("mapSize") or 0) or _map_size(bundle)
    selected = meshindex.select(index, values)
    groups = {}
    total = 0
    for kind, bucket in sorted(selected.items()):
        ratio = rates.get(f"size/cdae_per_source_{kind}") or 1.0
        size = int(bucket["bytes"] * ratio)
        groups[kind] = {
            "shapes": bucket["shapes"],
            "sourceBytes": bucket["bytes"],
            "bytes": size,
            "ratio": round(ratio, 3),
            "section": KIND_SECTION.get(kind, "world"),
        }
        total += size
    scale = _region_scale(values, map_size)
    for key, parts, group in TILE_GROUPS:
        if not values.get(key, True):
            continue
        source = _dir_bytes(os.path.join(bundle, *parts))
        if not source:
            continue
        ratio = rates.get(f"size/cdae_per_source_{group}") or 1.0
        size = int(source * scale * ratio)
        groups[group] = {
            "shapes": None,
            "sourceBytes": int(source * scale),
            "bytes": size,
            "ratio": round(ratio, 3),
            "section": "world",
        }
        total += size
    return {
        "bytes": total,
        "groups": groups,
        "basis": _mesh_basis(rates),
        "approximations": [
            {
                "what": "a cooked shape's bytes are its source glTF times a measured ratio, not the "
                "cooked file itself",
                "why": "the cook is BeamNG's own compiler and only running it says what it produces",
                "measured": "0.97 over the reference city's 4,272 object shapes, per kind 1.02 "
                "buildings / 0.65 props / 1.03 trees",
            },
            {
                "what": "a road, marking, area or foundation tile is sized from the bake product it "
                "is merged out of, scaled by how much of the map the region covers",
                "why": "how the merge divides into tiles is the build's own tiling, and a tile is "
                "one shape however many records went into it",
                "measured": "1.31 networks / 1.52 areas / 1.59 markings / 1.53 foundations",
            },
            {
                "what": "a recoloured prefab is counted once per colour the bake gave it, including "
                "the copies too thinly placed to be worth their own type",
                "why": "which copies survive is decided by `forest_min_items` against the item count "
                "per colour, which is the build's own count and not the bake's",
                "measured": "17 of 4,272 shapes and 5.9 MiB of 2,068 on the reference city (+5.7 % "
                "of the tree group, +0.3 % of all shapes)",
            },
            {
                "what": "trees standing in the road are still counted",
                "why": "the clearance rule is a spatial pass over the lane layer and it has never "
                "removed a whole shape",
                "measured": "1,806 of 1,276,060 records on the reference city, 0 shapes",
            },
        ],
    }


def _stale_bake_note(census):
    """Both halves are sized from the bake's own output, so a bake these settings have moved past
    is a figure for the LAST one. The census already knows which products are stale; saying so
    here is the difference between an estimate that is behind and an estimate that is wrong."""
    products = ((census or {}).get("derived") or {}).get("products") or {}
    stale = sorted(
        name
        for name, info in products.items()
        if info.get("status") in ("stale", "absent")
        and name in ("materials", "lods", "areas", "markings", "skirts", "terraincut")
    )
    if not stale:
        return []
    return [
        {
            "what": f"the figures are for the last bake; {len(stale)} of its products "
            f"({', '.join(stale)}) will be rebuilt with these settings",
            "why": "both halves are sized from what the bake produced, and what the next bake "
            "produces is what the build will ship",
            "measured": None,
        }
    ]


def _region_scale(values, map_size):
    """How much of the map a region keeps — the square every stage that reads one tests."""
    region = settings_mod.region_of(values)
    if not region or not map_size:
        return 1.0
    radius = float(region["radius"])
    return min(1.0, 4.0 * radius * radius / (map_size * map_size))


def _map_size(bundle):
    manifest = _load_json(os.path.join(bundle, "bundle.json")) or {}
    try:
        return float(manifest.get("mapSize") or 0)
    except (TypeError, ValueError):
        return 0.0


def _mesh_basis(rates):
    builds = int(rates.get("size/builds") or 0)
    if builds >= 1:
        return f"from your own last {builds} build{'s' if builds > 1 else ''}"
    return "from the reference city"


def _dir_bytes(path, _cache={}):  # noqa: B006 - process-lived cache
    """Bytes under a directory, cached on its entry count and newest mtime."""
    try:
        stamp = (os.path.getmtime(path), len(os.listdir(path)))
    except OSError:
        return 0
    hit = _cache.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    _cache[path] = (stamp, total)
    return total


def _terrain_file_bytes(values, bundle):
    """The heightfield on disk: three bytes a sample plus a header.

    MEASURED on the reference level - an 8,192 grid wrote 201,326,743 bytes, which is
    8192 x 8192 x 3 + 151. It is not resident the way a shape is (BeamNG streams the `.ter`), so
    it is in the disk figure and out of the video memory one.
    """
    size = 4096 * max(1, int(values.get("terrain_upsample") or 1))
    return size * size * 3 + 151


# -- the rows the meter draws ----------------------------------------------------------------------


def _budget(values):
    """What this machine's card is known to hold, read exactly the way the validator reads it, so
    the meter and the build's own verdict cannot disagree."""
    try:
        from ..beamng import validate_level

        vram, pixels, source = validate_level.render_target(values)
        return {
            "vramMiB": vram,
            "pixels": pixels,
            "source": source,
            "residentMiB": round(validate_level.resident_budget_mib(vram, pixels)),
            "failureMiB": round(validate_level.resident_failure_mib(vram, pixels)),
        }
    except Exception:
        return None


def _rows(est, values):
    """The bars. One figure with a budget it can exceed, and it warns rather than refuses: a
    person may be building deliberately for a machine that is not this one, which is what the
    three target-machine settings are for."""
    budget = est.get("budget")
    mib = est["residentMiB"]
    row = {
        "key": "vram",
        "label": "Video memory",
        "value": f"{mib:,.0f} MiB",
        "basis": _resident_basis(est),
        "section": _biggest_lever(est),
    }
    if budget:
        fit, fail = float(budget["residentMiB"]), float(budget["failureMiB"])
        row["of"] = f"of {fit:,.0f} MiB known to fit"
        row["fraction"] = mib / max(fail, 1.0)
        row["markFraction"] = fit / max(fail, 1.0)
        if mib >= fail:
            row["band"] = "over"
            row["advice"] = (
                "This is past the point where this card has been measured to run "
                "out. The level will stutter and get worse the longer you play."
            )
        elif mib > fit:
            row["band"] = "close"
            row["advice"] = (
                "This is more than your card is known to hold, and less than it is "
                "known to fail at. It may run; nobody has measured it."
            )
        else:
            row["band"] = "fits"
    else:
        row["fraction"] = 0.0
        row["band"] = "unknown"
        row["advice"] = "Run BeamNG once so it writes down what card you have."
    return [row]


def _resident_basis(est):
    meshes = est["meshes"]["bytes"] / MIB
    textures = est["textures"]["bytes"] / MIB
    return (
        f"{meshes:,.0f} MiB of shapes {est['meshes']['basis']}, "
        f"{textures:,.0f} MiB of textures computed exactly"
    )


def _biggest_lever(est):
    """The section holding the control that would move this figure most - what the meter's
    "What should I change?" jumps to. The textures are their own section; everything else in the
    mesh half is chosen in World."""
    groups = est["meshes"]["groups"]
    heaviest = max(groups.values(), key=lambda g: g["bytes"], default=None)
    if heaviest is None or est["textures"]["bytes"] > heaviest["bytes"]:
        return "textures"
    return heaviest.get("section") or "world"


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# -- scoring it against the build ------------------------------------------------------------------

# Below this much shipped geometry a build says nothing about a ratio: a `.cdae` carries a
# header and a material table whatever it holds, so a level of a handful of shapes measures that
# overhead and not the compression. The example bundle's one-shape level is exactly this case,
# and without the floor a test build would rewrite the user's calibration with its own noise.
LEARN_FLOOR_MIB = 64.0


def score(estimated, measured):
    """`estimated vs actual` for the build report and the screen after it.

    The one line that keeps the estimator honest. `measured` is the validator's `resident` block,
    which counts the same bytes this predicted.
    """
    if not estimated or not measured:
        return None
    if float(measured.get("totalMiB") or 0) < LEARN_FLOOR_MIB:
        return None
    out = {}
    for name, got in (
        ("mesh", measured.get("meshMiB")),
        ("texture", measured.get("textureShippedMiB")),
        ("total", measured.get("totalMiB")),
    ):
        want = {
            "mesh": estimated.get("meshes", {}).get("bytes", 0) / MIB,
            "texture": estimated.get("textures", {}).get("bytes", 0) / MIB,
            "total": estimated.get("residentBytes", 0) / MIB,
        }[name]
        if got is None:
            continue
        out[name] = {
            "estimatedMiB": round(want, 1),
            "actualMiB": round(float(got), 1),
            "errorPercent": round(100.0 * (want - float(got)) / max(float(got), 1e-6), 1),
        }
    return out


def observe(estimated, measured, index=None, values=None):
    """Blend one build's measured bytes back into the per-kind ratios.

    Only the halves that are RATIOS move: the texture half is arithmetic and has nothing to
    learn. The per-kind ratio cannot be separated out of a single total, so what is blended is
    the one factor that carries the whole mesh half - and `size/builds` counts how many of the
    user's own builds are in it, which is what the meter's basis line reports.
    """
    if not estimated or not measured:
        return
    actual = measured.get("meshMiB")
    predicted = estimated.get("meshes", {}).get("bytes", 0) / MIB
    if not actual or predicted <= 0 or float(actual) < LEARN_FLOOR_MIB:
        return
    correction = float(actual) / predicted
    rates = history.load()
    for key in list(rates):
        if key.startswith("size/cdae_per_source_"):
            history.observe(key, rates[key] * correction)
    history.observe("size/builds", min(8, int(rates.get("size/builds") or 0) + 1), blend=False)
