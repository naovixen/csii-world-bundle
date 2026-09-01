"""Where every setting appears on screen.

The sidebar's sections and their order, the basics strip above them, and which settings the
screen shows before "Show advanced". This is the PLACEMENT authority: a key's section is where
it is listed here, which is what `screen.section_of` reads. The catalog modules are grouped to
match, but only for navigability — this file decides.
"""

# The handful of things a person must actually supply, above the settings and always visible.
# `region` is NOT here: it is a decision about the level, not a path the app needs, so it heads
# the World section where the rest of "what gets built" lives.
BASICS = ["bundle", "output", "beamng_exe", "level_name", "level_title"]


# The sidebar. Adding a section is an entry here and nothing else; a group with one section
# still renders its heading, so the sections still to come land in an established shape.
# Each block is (title or None, keys) - a title only where a section is long enough to need one.
# A key that is a SUBSET of another (see SUBSET_OF) follows its parent, and is indented under it.
SECTIONS = [
    {
        "id": "world",
        "group": "Level",
        "label": "World",
        "tagline": "What you will see",
        "blocks": [
            (
                None,
                [
                    "region",
                    "buildings",
                    "networks",
                    "foliage_density",
                    "prop_density",
                    "lod0_refine",
                    "lod0_refine_cm",
                    "markings",
                    "areas",
                    "skirts",
                    "lane_furniture",
                    "lane_furniture_categories",
                    "lights",
                    "water",
                    "groundcover",
                    "view_distance",
                    "tod_hour",
                ],
            )
        ],
    },
    {
        "id": "collision",
        "group": "Level",
        "label": "Collision",
        "tagline": "What you can hit",
        "blocks": [
            (
                None,
                [
                    "collision_preset",
                    "collision_buildings",
                    "collision_props",
                    "collision_trees",
                    "collision_furniture",
                    "collision_skirts",
                    "collision_paint",
                    "collision_lights",
                    "collision_min_footprint",
                    "collision_max_clearance_m",
                ],
            )
        ],
    },
    {
        "id": "textures",
        "group": "Level",
        "label": "Textures",
        "tagline": "How sharp it looks",
        "blocks": [
            (
                None,
                [
                    "texture_max",
                    "texture_max_normal",
                    "texture_max_data",
                    "texture_dds",
                    "terrain_upsample",
                    "terrain_brightness",
                    "paint_kinds",
                    "paint_variants_tree",
                    "paint_variants_prop",
                    "texture_max_variant",
                ],
            )
        ],
    },
    {
        "id": "navigation",
        "group": "Level",
        "label": "Navigation",
        "tagline": "The minimap, routes and fast travel",
        "blocks": [(None, ["ai_roads", "navgraph_taxiways", "pois", "poi_max", "poi_file"])],
    },
    {
        "id": "advanced",
        "group": "Build",
        "label": "Advanced",
        "tagline": "Build and tuning",
        # The one section that keeps its jargon. Everywhere else a name that reads like a
        # variable is the defect; here it is the point - somebody changing a LOD switch budget
        # needs to know that is what they are changing, and the plain-English paraphrase
        # ("only for things seen beyond") says less, not more. The plain-language gate in
        # `tools/test_app_shell.py` is scoped to the other sections for exactly this reason.
        "technical": True,
        "note": "These name what they actually do, and their defaults were arrived at by "
        "measurement. They are here for building a level for one particular machine.",
        "blocks": [
            (
                "Detail and performance",
                [
                    "generate_lods",
                    "tile_lod",
                    "imposters",
                    "imposter_min_metres",
                    "lod_detail_scale",
                    "lod_detail_scale_prop",
                    "lod_detail_scale_tree",
                    "lod_detail_scale_large",
                    "lod_large_radius_m",
                    "lod_switch_error_px",
                    "tile_refine_cm",
                    "lod0_refine_peak_cm",
                    "authored_lod0_max_error",
                    "lod0_max_error",
                    "lod0_min_triangles",
                    "forest_min_items",
                    "forest_rare_type_min",
                    "forest_rare_keep_radius",
                    "far_detail_scale",
                    "far_ring_metres",
                    "authored_lod1_drop",
                    "authored_lod0_max_tris",
                    "area_conform_cm",
                    "shadow_min_radius",
                    "lane_max_tris",
                    "paint_factor_max_error",
                    "foliage_road_clearance",
                    "forest_shadows",
                    "forest_shadow_null_px",
                    "tree_wind",
                    "tile_size",
                    "terrain_holes",
                    "terrain_blend_sharpen",
                    "lights_max",
                    "navgraph_max_width",
                ],
            ),
            ("The machine this level is for", ["gpu_vram_mib", "render_width", "render_height"]),
            (
                "Build process",
                ["jobs", "cook", "rebuild", "keep_partial", "sweep_temp", "keep_dae", "ui_browser"],
            ),
        ],
    },
]

# The settings the UI shows BEFORE "Show advanced". Everything else is still there, still
# settable from the CLI, still in its group; it is one click away instead of on screen.
#
# What earns a place here: a question a person building their first level has to answer, or one
# whose answer is a preference rather than a measurement.
#
#   * **What to export** — all fourteen of them. These are the first thing anyone wants to
#     change and they are the whole point of the tool; hiding them behind a toggle was wrong.
#     Their PARAMETERS (`lights_max`, `poi_max`, `navgraph_max_width`, …) stay in advanced:
#     "do I want street lights" is a decision, "how many at most" is a tuning number.
#   * **The four `How much` knobs a preset visibly moves.** Not because they need touching, but
#     because a preset that changes nothing you can see reads as a preset that does nothing.
#     The other twenty-nine tuning numbers have measured defaults and stay in advanced.
#
# The ceiling is checked by the test suite: a basic panel that keeps growing is the problem it
# was made to solve, wearing a different hat.
BASIC = [
    # Where
    "bundle",
    "output",
    "level_name",
    "level_title",
    "region",
    # What to export
    "buildings",
    "networks",
    "trees",
    "props",
    "markings",
    "areas",
    "skirts",
    "terrain_cut",
    "lane_furniture",
    "water",
    "lights",
    "ai_roads",
    "pois",
    "groundcover",
    # How much
    "foliage_density",
    "prop_density",
    "texture_max",
    "view_distance",
    "lod0_refine",
    "lod0_refine_cm",
    # Collision / Process
    "collision_preset",
    "cook",
]
BASIC_MAX = 28
