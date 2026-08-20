"""The one place the app's settings become `csiibundle.bake` options.

It lives here, and not in `settings.py`, on purpose: `tools/test_app_shell.py` walks the modules
a stage's `run` can reach to check that the stage declares every setting it reads, and it skips
`cs2beamng.settings` (whose source names every key by definition). A mapping parked there would
be invisible to that gate — which is the gate that exists because `paint_kinds` was read but not
declared and cost a 25-minute build shipping the previous bake's untinted albedo.
"""

from csiibundle.bake import ground as bake_ground

from .. import settings as settings_mod


# Product options this app deliberately leaves at the bake's own default, so `bake_options` does
# not carry them. Listing them is the point: the gate in `tools/test_app_shell.py` fails when a
# product declares an option that is neither built here nor named here, so a new option has to be
# wired to a setting or waived on purpose - it can never just go unread.
BAKE_OPTIONS_LEFT_DEFAULT = ("lod_targets", "lod_backend")


def bake_options(settings, jobs=1):
    """The `csiibundle.bake` options dict these settings mean — the ONE place that mapping lives.

    Both the bake stage (which runs the bake) and the census (which reports whether the derived
    cache is fresh) must build this the same way. They did not until 2026-08-20: the census
    passed only `tile`/`diagonal`/`region`, so every product that declares an option beyond those
    compared its recorded `ground: "fine"` (or `paint_kinds`, …) against a missing key and read
    as stale forever. The cache itself was fine — the build skipped those products — but the
    preflight said "stale" and the estimate promised a re-bake that never happened.
    """
    categories = [c.strip() for c in str(settings.get("lane_furniture_categories") or "fence,cable,lane").split(",") if c.strip()]
    return {
        "tile": float(settings.get("tile_size") or 256),
        # MEASURED, not hard-coded conservatism: BeamNG splits SW-NE (audit §73)
        "diagonal": bake_ground.MEASURED_DIAGONAL,
        "region": settings_mod.region_of(settings),
        "jobs": jobs,
        "lane_parts": categories,
        # EMPTY MEANS NONE, and it did not until 2026-08-20: the fallback here was `or "tree"`,
        # so clearing every recolouring box quietly went on recolouring the trees. Nothing on
        # screen said so, and the level shipped 490 tinted textures for a setting a person had
        # just turned off. The state became reachable when E2 made this a set of checkboxes;
        # before that you would have had to type an empty string to find it. A default belongs
        # in `SCHEMA` (it is "tree,prop" there), not in a fallback that fires on a legitimate
        # value and overrules it.
        "paint_kinds": [k.strip() for k in str(settings.get("paint_kinds") or "").split(",") if k.strip()],
        "paint_variants": {"tree": int(settings.get("paint_variants_tree", 2) or 1),
                           "prop": int(settings.get("paint_variants_prop", 1) or 1)},
        "lane_max_tris": int(settings.get("lane_max_tris") or 0) or None,
        "paint_factor_max_error": int(settings.get("paint_factor_max_error", 4) or 0),
        # A tinted albedo the bake writes at 1024 and this build then caps at `texture_max` is
        # 4x the sRGB encode for texels the level never sees — 7 minutes of the bake on the
        # reference city. Bake them at the size that ships. The bake box-filters the SOURCE
        # albedo before tinting either way, so this moves that filter one step earlier rather
        # than adding one; the colour mask is nearest-resampled to the same size, which is where
        # the (measured, sub-percent) difference comes from.
        "variant_max_size": int(settings.get("texture_max_variant")
                                or settings.get("texture_max") or 0) or 1024,
        # the ground the level will have: the fine 1.75 m raster only when the .ter is built from
        # it (terrain_upsample 2 = 8192); the paint must be conformed to the same surface
        "ground": "fine" if int(settings.get("terrain_upsample") or 1) == 2 else "base",
    }
