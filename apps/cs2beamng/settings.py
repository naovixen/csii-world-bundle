"""Settings — every knob the app exposes, with defaults, validation, presets and per-OS paths.

One flat dict, persisted at `~/.cs2beamng/settings.json`, edited by the UI and by
`cs2beamng build --set key=value`. Everything the pipeline reads comes from here, so a headless
build and a UI build with the same settings produce the same level.

Defaults lean towards **fidelity with performance** (the "High" preset): every layer on,
foliage at 100 %, textures capped at 1024, generated LODs on, collision on for everything
physical, paint collision off. Presets are whole-dict overrides applied on top of the defaults;
a user's explicit changes after picking a preset stick.
"""

import json
import os
import sys

from csiibundle.bake.locate import default_bundle_dir

APP_DIR_NAME = ".cs2beamng"
SETTINGS_FILE = "settings.json"

# key -> (default, kind, help). kind: bool | int | float | str | path | choice:a|b|c
SCHEMA = {
    # where
    "bundle": (
        None,
        "path",
        "Bundle directory. The app finds the ripper mod's own export folder by itself; this overrides it",
    ),
    "output": (
        None,
        "path",
        "Levels directory to write into (default: the BeamNG userfolder levels/)",
    ),
    "level_name": ("cs2_city", "str", "Level folder name (lowercase, underscores)"),
    "level_title": ("CS2 City", "str", "Level title shown in BeamNG"),
    "region": (
        None,
        "str",
        "Build one square patch of the city instead of all of it: X Z half-width, in bundle metres",
    ),
    # what
    "trees": (True, "bool", "Trees (forest)"),
    "props": (True, "bool", "Props, fences, points of interest"),
    "buildings": (True, "bool", "Buildings"),
    "networks": (True, "bool", "Roads, rails and other network meshes"),
    "markings": (True, "bool", "Road markings and painted decals (pre-draped)"),
    "lane_furniture": (True, "bool", "Roadside furniture: fences, hedges, cables"),
    "lane_furniture_categories": ("fence,cable,lane", "str", "Furniture categories to include"),
    "areas": (True, "bool", "Painted surfaces (parking lots, paths)"),
    "skirts": (True, "bool", "Building foundation skirts"),
    "terrain_cut": (
        True,
        "bool",
        "Cut the ground open at trenches and tunnel portals and drop the exact cut mesh into the hole",
    ),
    "lights": (False, "bool", "Street and building lights (dynamic lights are expensive)"),
    # Needs BeamNG on ADVANCED lighting. Basic Lighting has no g-buffer prepass and shades no
    # point or spot light at all, so on that profile every placed light is pure cost (audit §82);
    # `validate` reads the user's own `game-settings.json` and says so.
    "lights_max": (25000, "int", "Maximum dynamic lights (needs Advanced Lighting in BeamNG)"),
    "water": (True, "bool", "Water planes/blocks"),
    "groundcover": (
        False,
        "bool",
        "Billboard grass over the ground paint. BeamNG dressing, not CS2 fidelity - CS2 has no grass system",
    ),
    "ai_roads": (
        True,
        "bool",
        "The invisible road network the AI, the minimap and route-finding use. Off = no minimap and no routes",
    ),
    "navgraph_max_width": (
        0.0,
        "float",
        "Clamp a navgraph node's road width, in metres (0 = the width CS2's own lane offsets give)",
    ),
    "navgraph_taxiways": (
        False,
        "bool",
        "Let routes run across the apron and the taxiways too, slowly",
    ),
    "pois": (
        True,
        "bool",
        "Fast-travel points on the big map, derived from the densest road cells",
    ),
    "poi_file": (
        None,
        "path",
        "The file holding the fast-travel points you named, instead of the ones picked for you",
    ),
    "poi_max": (
        8,
        "int",
        "How many busy places to pick out for you when you have named fewer of your own",
    ),
    # how much
    "foliage_density": (100, "int", "Percent of trees kept (deterministic subset)"),
    "prop_density": (100, "int", "Percent of props kept"),
    "texture_max": (
        512,
        "choice:256|512|1024|2048|0",
        "Colour texture edge cap in pixels (0 = full size). Also caps the emissive and opacity maps",
    ),
    "texture_max_normal": (
        512,
        "choice:256|512|1024|2048|0",
        "How large a surface-bumpiness picture may be, in pixels (0 = as large as it came)",
    ),
    "texture_max_data": (
        256,
        "choice:128|256|512|1024|0",
        "Edge cap for data maps in pixels: roughness, metallic and the CS2 colour mask",
    ),
    "texture_dds": (
        True,
        "bool",
        "Compress the textures during the build, so the game never has to on the first load",
    ),
    "generate_lods": (True, "bool", "Generate LOD chains for every shape"),
    "tile_lod": (
        True,
        "bool",
        "Give the merged world tiles (roads, markings, areas, skirts, furniture) a real LOD chain",
    ),
    "imposters": (True, "bool", "Engine-generated billboard imposters (bb_autobillboard)"),
    "imposter_min_metres": (
        0,
        "int",
        "Only give an imposter to a shape still drawn beyond this distance; 0 = 3/4 of the view distance",
    ),
    "lod_detail_scale": (
        6.0,
        "float",
        "How many times closer than CS2 an object is culled. 1 = exactly what the game draws",
    ),
    "lod_detail_scale_prop": (
        0.0,
        "float",
        "The same multiplier as lod_detail_scale but for PROPS only; 0 = use the global one",
    ),
    "lod_switch_error_px": (
        8.0,
        "float",
        "How many pixels of surface error a generated LOD may show when it takes over. Smaller = truer, heavier",
    ),
    "paint_kinds": (
        "tree,prop",
        "str",
        "Which sorts of object take their colour from your city; the rest draw an untinted one",
    ),
    "paint_variants_tree": (
        2,
        "int",
        "How many shades each kind of plant comes in. Every shade is another copy to draw",
    ),
    "paint_variants_prop": (
        1,
        "int",
        "How many shades each kind of small object comes in. One is its average colour",
    ),
    "texture_max_variant": (
        256,
        "choice:128|256|512|1024|0",
        "Colour cap for a paint variant's tinted albedo, separately from the main colour cap",
    ),
    "forest_min_items": (
        10,
        "int",
        "Merge a forest type holding fewer items than this into its prefab's plain type. 0 keeps every type",
    ),
    "lane_max_tris": (
        0,
        "int",
        "Triangle budget for baked lane furniture, 0 = no budget. What goes first is what CS2 culls first",
    ),
    "paint_factor_max_error": (
        4,
        "int",
        "How far a paint variant may drift, in 8-bit channels, before it gets a texture of its own",
    ),
    "foliage_road_clearance": (
        2.5,
        "float",
        "Declared deviation: drop a tree standing this close to a lane centre so the city can be driven. 0 = off",
    ),
    "gpu_vram_mib": (
        0,
        "int",
        "Dedicated VRAM of the card the level is for, in MiB (0 = read it from your own beamng.log)",
    ),
    "render_width": (0, "int", "Render width the level is for (0 = read it from beamng.log)"),
    "render_height": (
        0,
        "int",
        "Render height the level is FOR (0 = read it from beamng.log, fall back to 1080)",
    ),
    "tile_size": (256, "int", "World tile size for roads/markings/areas (metres)"),
    "view_distance": (4000, "int", "The distance in metres past which nothing is drawn at all"),
    "tod_hour": (
        -1.0,
        "float",
        "Time of day in hours 0-24 for the level's sun (-1 = the in-game clock at rip time)",
    ),
    "terrain_brightness": (
        1.0,
        "float",
        "Lighten or darken the ground. One leaves it as your city has it",
    ),
    "terrain_upsample": (
        2,
        "choice:1|2",
        "Terrain grid: 2 = 8192 at 1.75 m (the game's own near-camera detail); 1 = 4096 at 3.5 m, lighter",
    ),
    "terrain_holes": (
        "contain",
        "choice:contain|majority|none",
        "Which clip cells become terrain holes",
    ),
    # NOT A PARITY SETTING - see stages/terrain.HEIGHT_SIGMA and audit §48.3. 1.0 is the value
    # BeamNG's own blend arithmetic derives; above that it is a deliberate deviation that trades
    # fidelity for hiding an artefact the engine's texel-sized constants cause on our coarse grid.
    "terrain_blend_sharpen": (
        1.0,
        "float",
        "Ground-boundary interlock, x the derived amount. 1 = faithful; 2-3 hides the blocky material edges",
    ),
    "forest_shadows": (True, "bool", "Forest items cast shadows"),
    "tree_wind": (False, "bool", "Synthesise wind vertex colours for trees"),
    # A8/P2: metres of measured surface error at which LOD 1 replaces LOD 0 outright. 0 = keep
    # every LOD 0. See `stages.shapes.LOD0_MAX_ERROR_DEFAULT` for the measurement behind 0.10.
    "lod0_max_error": (0.10, "float", "Drop LOD0 where LOD1 is within this many metres of it"),
    "lod0_min_triangles": (
        1000,
        "int",
        "...but never where that leaves the shape shipping fewer triangles than this. 0 = no floor",
    ),
    # A8/P7. `sweep_temp` is free and on. `keep_dae` is ON BY DEFAULT and that is deliberate:
    # dropping the sources saves 5.4 GB of DISK and no RAM, and the NEXT build has to rewrite
    # every `.dae` it deleted, which makes the cook recompile all of them (~9 min on the full
    # city). Turn it off for a build you are shipping, not for one you are iterating on.
    "sweep_temp": (
        True,
        "bool",
        "Delete cooked shapes in BeamNG's temp/ that this level no longer has",
    ),
    "keep_dae": (
        True,
        "bool",
        "Keep .dae sources beside the cooked .cdae (off saves ~5 GB, forces a full recook next build)",
    ),
    # collision
    "collision_preset": ("simplified", "choice:exact|simplified|off", "Collision detail"),
    "collision_buildings": (True, "bool", "Buildings collide"),
    "collision_props": (True, "bool", "Props collide"),
    "collision_trees": (True, "bool", "Trees collide (trunk)"),
    "collision_furniture": (True, "bool", "Fences and lane furniture collide"),
    "collision_skirts": (True, "bool", "Foundation skirts collide"),
    "collision_paint": (False, "bool", "Markings and painted surfaces collide (not recommended)"),
    # A8/P4: below this horizontal footprint a forest type ships collidable:false whatever its
    # height. See `stages.instances.FOREST_COLLIDABLE_MIN_FOOTPRINT`.
    "collision_min_footprint": (0.8, "float", "Props narrower than this (metres) do not collide"),
    "collision_lights": (
        False,
        "bool",
        "Street lights and every other light-bearing prop collide. Off: a 10 m pole stops a car dead",
    ),
    # process
    "jobs": (0, "int", "Worker processes for parallel stages (0 = CPU count - 1)"),
    "rebuild": (
        False,
        "bool",
        "Rebuild every stage from scratch, ignoring what the last build recorded",
    ),
    "keep_partial": (False, "bool", "Keep a partially built level after cancel"),
    "cook": (
        True,
        "bool",
        "Compile the shapes with BeamNG's own compiler after the build, so the first load is fast",
    ),
    "beamng_exe": (None, "path", "BeamNG executable (auto-detected when empty)"),
    "quality_preset": (
        "high",
        "choice:ultra|high|performance|medium|low|custom",
        "Quality preset last applied",
    ),
    "ui_browser": (
        "window",
        "choice:window|chrome|edge|firefox|default",
        "Where the UI opens: a native window, a named browser, or the system default",
    ),
    # Replaces the retired `ui_advanced`: the sidebar remembers which section you were in, not
    # whether a second tier of settings was unfolded. There is no second tier any more.
    # its choices are filled in from SECTIONS below, so a new section needs no edit here
    "ui_section": (
        "world",
        "choice:world",
        "The settings section the UI opens on (remembered between sessions)",
    ),
}

# The long form of the help for the settings whose answer needed an argument: what was measured,
# what it cost, and why the default is the default. The UI shows `help` always and this behind a
# "why" disclosure, and `docs/settings.md` is generated from it. Moved here verbatim in D2.2 -
# 31 of the 78 help strings had grown into audit notes of up to 2,320 characters, which is the
# right amount of detail in the wrong place: every control printed its essay, always.
DETAILS = {
    "ai_roads": "The road network the game's own systems drive on: the minimap, the route "
    "line to a waypoint, the arrows on the ground and the traffic all read it and "
    "nothing else. With it off the minimap is blank, a big-map waypoint produces "
    "no route, and nothing but you uses the roads. It is invisible either way — "
    "it is not what you see, it is what the game knows.",
    "collision_lights": "Lampposts, floodlights and every other prop that carries a light "
    "become solid. It is off by default because a lamppost is thin enough "
    "to slip past the narrow-object rule, and a 10 m pole that stops a car "
    "dead is the one piece of street furniture you hit most. Which props "
    "count is the game's own answer: the ones your city gives a real "
    "light to.",
    "cook": "Compiles the level's shapes into the form BeamNG loads, at the end of the build "
    "instead of the first time you play. It adds minutes here and saves more than "
    "that later. Skipped automatically if BeamNG is running or cannot be found.",
    "foliage_road_clearance": "Removes a tree whose trunk stands within this distance of the "
    "middle of a drivable lane, so you can actually drive the city. "
    "Your city has no such rule — it clears the ground when a road "
    "is BUILT and lives with whatever grew back — so this is "
    "playability rather than accuracy, and the build report counts "
    "what it removed. 2.5 m clears the carriageway rather than the "
    "car: a lane is about 3.5 m wide and a trunk can be half a "
    "metre thick. It goes by the trunk, not the branches, so a "
    "canopy can still overhang a road. Trees only — you drive "
    "through a bush without noticing. 0 turns it off.",
    "forest_min_items": "A kind of plant with fewer than this many placed is folded in with "
    "its plain, untinted version instead of being its own kind. BeamNG "
    "loads a kind whole whether it has one member or ten thousand, and a "
    "city ends up with thousands of kinds holding a handful each — more "
    "than half of them, for well under a percent of the planting. 0 keeps "
    "every one.",
    "gpu_vram_mib": "How much memory the graphics card this level is built for has. 0 reads "
    "it from your own BeamNG log. It changes nothing about the level; it is "
    "what the cost meter measures against, so set it if you are building for "
    "a machine that is not this one.",
    "groundcover": "Blades of grass standing over the ground, for a camera at driving height. "
    "Your city has nothing of the kind — Cities: Skylines II has no grass "
    "system at all — so this is not reproducing anything, it is dressing, and "
    "it is off by default for that reason.",
    "imposter_min_metres": "A shape this level stops drawing nearer than this gets no "
    "billboard. 0 works it out as three quarters of how far you can "
    "see. It matters because BeamNG builds each billboard while you "
    "play, by rendering the shape from every angle the first time it "
    "comes into view — so every one is a pause when you pan and a "
    "picture held in memory afterwards. Worth it for a landmark still "
    "drawn at the edge of the view; not for a mailbox that disappears "
    "at 80 m.",
    "lane_max_tris": "A ceiling on the fences and cables that have to be built into the road "
    "tiles rather than placed as copies. What goes first is what your city "
    "itself gives up first, so the budget is spent on the furniture the game "
    "draws furthest away. 0 is no ceiling.",
    "lod0_min_triangles": "A floor under the promotion: never drop the full-detail "
    "version if that would leave the shape with fewer triangles than "
    "this. The measurement the promotion uses is how far the surface "
    "moved, and that cannot tell that a reduction has opened an object "
    "up — flattening a sign's post moves its surface a few millimetres "
    "and leaves a panel floating with no post. A floor protects the "
    "thin things without giving back the saving on the heavy ones. 0 "
    "removes it.",
    "lod_detail_scale": "Your city draws a typical small object to about a kilometre and a "
    "typical building to several. Faithful, and far more than BeamNG will "
    "carry. This divides every shape's own distance by the same amount; 1 "
    "draws exactly what the game draws.",
    "lod_detail_scale_prop": "The same divisor for small objects alone; 0 makes them share "
    "the global one. It is separate because your city draws "
    "buildings roughly twelve times further than props, and one "
    "divisor keeps that ratio while ruining what it means: a window "
    "frame is invisible at the distance the game drops it and "
    "plainly missing at a tenth of that, while the building it "
    "belongs to is still there. Most props are attached to "
    "something, so the two have to be given up at different rates.",
    "lod_switch_error_px": "How wrong a reduced version of a shape may look, in pixels on "
    "screen, at the moment it takes over from the one before it. Each "
    "reduced version is measured against the original, so one that is "
    "identical keeps its place to the horizon and one that is a metre "
    "out waits until a metre stops showing. Smaller is truer and costs "
    "drawn triangles — and it costs them downtown, where the card is "
    "already working hardest. Plants are exempt and keep a fixed "
    "ladder: the measurement is a surface distance and a canopy is a "
    "cloud of leaves, so it asks for full detail twice as far out for "
    "a problem nobody reported.",
    "navgraph_max_width": "How wide the game may consider a road when it joins one road to "
    "another. 0 uses the width your own lanes give. It exists because "
    "BeamNG joins two road points when they are closer than the wider "
    "of the two, and that test includes height — so a very wide road "
    "reaches downwards and can weld a bridge deck into the street "
    "underneath it. In a typical city nothing does at 0.",
    "navgraph_taxiways": "Puts airport taxiways into the road network too, at a low speed so "
    "routes prefer real roads. Roads and highways are always in it; "
    "footpaths, trams, rails, waterways, cables and pipes never are.",
    "paint_factor_max_error": "How far a recoloured copy may drift from the real thing before "
    "it is given a texture of its own rather than a single colour "
    "multiplied over the original. Where a surface is tinted all "
    "over, one colour reproduces it exactly and costs no texture at "
    "all; where the tint picks out three different parts, it "
    "cannot. About a third of a city's recolourings come out "
    "free at this setting. 0 gives every one its own texture.",
    "paint_kinds": "Your city paints its own objects — the same bench in four colours, the "
    "same tree in a dozen greens. This chooses which sorts get that treatment. "
    "A sort left out draws the untinted original, and because the game authors "
    "those washed out on purpose, leaving one out is visibly wrong rather than "
    "subtle. It costs build time and textures.",
    "paint_variants_prop": "How many shades each kind of small object comes in. 1 is its "
    "average colour, which is usually enough: props are small, seen in "
    "bulk and at distance, and their colours vary less than the "
    "planting's.",
    "paint_variants_tree": "How many shades each kind of plant comes in. Every extra shade is "
    "another copy of the shape and another set of textures, and it is "
    "the single biggest source of both in a finished level — the "
    "shades alone can be most of its materials. 2 keeps the light and "
    "the dark half of a plant's range apart, which is what you can "
    "actually see at distance.",
    "poi_file": "The file the fast-travel points you place are kept in. The app writes it for "
    "you as you place them on the map; the setting is still a path, so you can "
    "point it at a list of your own or share one. Each point is put onto the "
    "nearest road wherever it started.",
    "poi_max": "How many busy places to pick out for you when you have named fewer of your "
    "own. The levels BeamNG ships carry between three and nine; a big map with "
    "forty markers on it is an index rather than a menu.",
    "pois": "The places you can jump straight to from the big map. Yours if you have named "
    "any, otherwise the busiest parts of your road network, named after the street "
    "they are on.",
    "rebuild": "Runs every stage again from nothing instead of skipping the ones whose inputs "
    "have not changed. Skipping is what makes a second build of the same city take "
    "seconds rather than a quarter of an hour, so leave this off unless you have "
    "reason to distrust what is on disk. `cs2beamng build --force` is the same "
    "thing.",
    "render_width": "The width the level is built for. 0 reads it from your own BeamNG log. "
    "Every pixel above 1080p takes memory back off the level for the game's "
    "own render targets, which is why the cost meter asks.",
    "terrain_blend_sharpen": "How sharply one ground meets another. 1 is what the game's own "
    "blend works out. Higher hides the blocky edges our coarser "
    "ground grid can leave, at the price of a harder join than the "
    "game would draw.",
    "terrain_brightness": "Lightens or darkens the ground. 1 is not neutral — it is the "
    "amount that lands your city's grounds where BeamNG's lighting "
    "expects them, worked out against the grounds BeamNG's own levels "
    "ship. Anything else is your eye rather than either game's.",
    "terrain_cut": "Where a road runs in a trench or into a tunnel mouth, the ground is "
    "opened up and a piece of ground cut to fit is dropped into the hole. "
    "Without it the terrain closes over the trench and the road disappears "
    "into a hill.",
    "terrain_upsample": "How finely the ground is shaped. Fine is the same detail the game "
    "itself uses near the camera and is worth having if the memory is "
    "there; standard is half as fine in each direction and a quarter of "
    "the cost. There is deliberately no finer option: the next step up is "
    "the largest texture the graphics API allows, four times the memory, "
    "and finer than any level BeamNG ships.",
    "texture_dds": "Compresses the textures during the build, in the format the game would "
    "have used. Without it the game compresses them itself the first time you "
    "load the level, one at a time, on the main thread — which is a minute or "
    "more of stuttering on a big city. Leave it on.",
    "texture_max": "The largest a colour picture may be. It is the main cost your card "
    "carries, and the cost meter moves most when this does. 512 is in line "
    "with what BeamNG's own biggest level ships, where most colour maps are "
    "512 or smaller. It also caps the glow and cut-out maps, which are the "
    "same picture's own resolution.",
    "texture_max_data": "The largest a picture describing shine, roughness or metal may be. "
    "These carry almost no fine detail — a roughness gradient is smooth "
    "by nature — and most of the ones in BeamNG's own biggest level are "
    "256 or smaller. This is the cheapest place to save memory.",
    "texture_max_normal": "The largest a surface-bumpiness picture may be. This is the tier "
    "that carries brick, plank and groove relief, and it is the one "
    "worth spending on after the colour map; BeamNG's own levels run "
    "these between 512 and 1024.",
    "texture_max_variant": "The largest a recoloured copy may be, separately from the colour "
    "cap. A recoloured copy is the same picture with a colour "
    "multiplied through it and it is only ever seen on planting and "
    "props in bulk, so it can be smaller than the buildings and roads "
    "you look at up close.",
    "tile_lod": "Gives the merged road, marking, area, foundation and furniture tiles reduced "
    "versions of their own. Each one still holds exactly what your city draws at "
    "that distance, but the distances are measured against how far you can see "
    "rather than against your city's own — which for a road is tens of "
    "kilometres, so without this almost none of a road tile is ever given up.",
    "view_distance": "The distance past which nothing is drawn at all. Every shape has its "
    "own distance worked out from what your city does with it; this is the "
    "ceiling over all of them, which makes it the one number that moves the "
    "whole cost at once.",
}


# -- E1: the model the UI is built from ----------------------------------------------------------
#
# `SCHEMA` is the contract (name, type, default) and `GROUPS`/`DETAILS` are what the reference
# page is generated from. Neither is a description of a *screen*: the groups are filing
# categories, the help strings are written for a page with no labels beside them, and `DETAILS`
# is the argument for a default rather than an explanation of a control.
#
# What follows is the screen. Every key gets a plain `label`, a one-line plain `blurb` and a
# control kind; every key lands in exactly one place - the basics strip, one sidebar section, or
# the driven list, which is the settings that still exist for `--set` and the docs but no longer
# have a control of their own because something else on screen now sets them. The test suite
# checks the partition, the length and the language (no identifiers, no research figures, no
# section numbers) - see `tools/test_app_shell.py`.

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
                    "lod_switch_error_px",
                    "lod0_max_error",
                    "lod0_min_triangles",
                    "forest_min_items",
                    "lane_max_tris",
                    "paint_factor_max_error",
                    "foliage_road_clearance",
                    "forest_shadows",
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

# Adding a section is an entry in SECTIONS and nothing else, which includes the setting that
# remembers which one you were in: its choices are the sections that exist.
SCHEMA["ui_section"] = (
    SECTIONS[0]["id"],
    "choice:" + "|".join(s["id"] for s in SECTIONS),
    SCHEMA["ui_section"][2],
)

# A setting that only means anything while another one is on. The UI indents it under its
# parent and greys it out when the parent is off, so "which of these belong to which" is
# answered by the layout rather than by reading. Turning the last of a parent's switches off
# turns the parent off too - a parent with nothing left under it is off, and pretending
# otherwise is how a level ships an empty layer.
SUBSET_OF = {
    "lane_furniture_categories": "lane_furniture",
    "navgraph_taxiways": "ai_roads",
    "poi_max": "pois",
    "imposter_min_metres": "imposters",
    "collision_buildings": "collision_preset",
    "collision_props": "collision_preset",
    "collision_trees": "collision_preset",
    "collision_furniture": "collision_preset",
    "collision_skirts": "collision_preset",
    "collision_paint": "collision_preset",
    "collision_lights": "collision_preset",
    "collision_min_footprint": "collision_preset",
    "paint_variants_tree": "paint_kinds",
    "paint_variants_prop": "paint_kinds",
    "texture_max_variant": "paint_kinds",
}

# The value a parent takes when the last thing under it is switched off.
PARENT_OFF = {"collision_preset": "off"}

# Gated by another setting but NOT OWNED by it. Two reasons a pair lands here rather than in
# SUBSET_OF: the two live in different sections (a tuning ceiling belongs with the tuning numbers
# even though it does nothing while the layer it caps is off), or the child is not one of the
# parent's SWITCHES and so cannot be the last of them - emptying your own list of fast-travel
# points asks for the derived ones, not for no fast travel at all. These grey out; they do not
# indent, and they never switch a parent off.
NEEDS = {"lights_max": "lights", "beamng_exe": "cook", "poi_file": "pois"}

# Settings with no control of their own, and the thing on screen that now sets each. They keep
# their `--set` name and their entry in the reference page; what they lose is a box nobody could
# answer without already knowing the answer.
DRIVEN_BY = {
    "quality_preset": "the preset buttons above the settings",
    "terrain_cut": "the Roads and rails switch",
    "trees": "the Trees slider, which turns them off at zero",
    "props": "the Props slider, which turns them off at zero",
    "ui_section": "the sidebar, which remembers where you were",
}

# ...and the rule that drives them (plan §6). `DRIVEN_BY` above is the SENTENCE; this is the
# behaviour, in one place, so the two cannot say different things.
#
#   * `terrain_cut` follows `networks`. Cutting the ground open at a trench or a tunnel mouth is
#     not a thing a person wants separately - it is how the roads there get their ground - and
#     with the roads off the hole has nothing in it.
#   * A density slider at zero turns its kind OFF rather than keeping none of it. "0 % of trees"
#     and "no trees" are the same level; saying it twice lets the two disagree, and a report that
#     says a stage kept 0 of 376,187 items has done the work of deciding that 376,187 times.
#
# `{key: (driven, rule)}`, applied by `server.update_settings` - which is the APP. `--set` never
# passes through it and stays literal, so a headless build does exactly and only what it was
# told, and `--set terrain_cut=true networks=false` is still a thing a person may ask for.
DRIVES = {
    "networks": ("terrain_cut", bool),
    "foliage_density": ("trees", lambda v: int(v or 0) > 0),
    "prop_density": ("props", lambda v: int(v or 0) > 0),
}


def driven_changes(incoming):
    """The extra `{key: value}` these changes imply, minus anything the caller set itself.

    A caller that names both halves means both halves: the rule fills in the one you did not
    mention, it does not overrule the one you did.
    """
    out = {}
    for key, value in (incoming or {}).items():
        rule = DRIVES.get(key)
        if rule is None:
            continue
        driven, decide = rule
        if driven not in incoming:
            out[driven] = decide(value)
    return out


# What a key looks like on screen where its type alone does not say. Everything absent falls
# back on its SCHEMA kind (see `control_of`).
CONTROLS = {
    "region": "region",  # a patch dragged on the map of the city
    "foliage_density": "percent",  # zero reads "off", and turns the stage off
    "prop_density": "percent",
    "view_distance": "distance",
    "tod_hour": "clock",
    "lane_furniture_categories": "categories",  # a fixed set of answers is a set of checkboxes
    "paint_kinds": "categories",  # ...and not a comma-separated string to guess at
    "collision_preset": "segmented",
    "poi_file": "poi",  # the map, the list and the snap (E3)
}

# Every free-text box whose set of valid answers is FIXED AND KNOWN, as that set. A box a person
# cannot answer without already knowing the answer is a design failure, and both of these were
# one: nothing on screen said that furniture categories are exactly these three, or that the
# recolouring list is drawn from the bundle's own record kinds.
CHOICE_SETS = {
    "lane_furniture_categories": [
        ("fence", "Fences and hedges"),
        ("cable", "Overhead cables"),
        ("lane", "Kerbside details"),
    ],
    "paint_kinds": [
        ("tree", "Trees and plants"),
        ("prop", "Props"),
        ("fence", "Fences"),
        ("building", "Buildings"),
        ("decal", "Road markings"),
        ("poi", "Landmarks"),
    ],
}

# (min, max, step, suffix) for the slider controls, and what a slider prints at its floor.
RANGES = {
    "foliage_density": (0, 100, 1, " %"),
    "prop_density": (0, 100, 1, " %"),
    "view_distance": (500, 8000, 100, " m"),
}
ZERO_LABEL = {"foliage_density": "off", "prop_density": "off"}

# What a number is measured in, printed beside the box. A box reading "0.8" with nothing after
# it is a box that has not been answered - metres? feet? a fraction? - and every number control
# that is not a bare count has one.
UNITS = {
    "collision_min_footprint": "m",
    "imposter_min_metres": "m",
    "foliage_road_clearance": "m",
    "lod0_max_error": "m",
    "navgraph_max_width": "m",
    "tile_size": "m",
    "lod_switch_error_px": "px",
    "render_width": "px",
    "render_height": "px",
    "lod0_min_triangles": "triangles",
    "lane_max_tris": "triangles",
    "forest_min_items": "items",
    "lights_max": "lights",
    "poi_max": "points",
    "paint_variants_tree": "variants",
    "paint_variants_prop": "variants",
    "paint_factor_max_error": "of 255",
    "gpu_vram_mib": "MiB",
    "jobs": "processes",
    "lod_detail_scale": "×",
    "lod_detail_scale_prop": "×",
    "terrain_brightness": "×",
    "terrain_blend_sharpen": "×",
}

# Plain names. A label says WHAT the thing is; the blurb says what it does and what it costs.
# Two rules the test suite enforces: a blurb never opens by repeating its own label, and no
# label or blurb REFERS TO ANOTHER CONTROL ("the number above", "the same again", "the slider
# sets it"). A setting whose text only makes sense after reading its neighbour's is a setting
# that breaks the moment it is reached through search, which is now a first-class route into it.
LABELS = {
    # the basics strip
    "bundle": "Bundle folder",
    "output": "Levels folder",
    "beamng_exe": "BeamNG",
    "level_name": "Folder name",
    "level_title": "Level title",
    # world
    "region": "Area",
    "buildings": "Buildings",
    "networks": "Roads and rails",
    "foliage_density": "Trees",
    "prop_density": "Props and fences",
    "markings": "Road markings",
    "areas": "Painted surfaces",
    "skirts": "Building foundations",
    "lane_furniture": "Roadside furniture",
    "lane_furniture_categories": "Kinds to include",
    "lights": "Street and building lights",
    "water": "Water",
    "groundcover": "Grass",
    "view_distance": "How far you can see",
    "tod_hour": "Time of day",
    # collision
    "collision_preset": "Detail",
    "collision_buildings": "Buildings",
    "collision_props": "Props",
    "collision_trees": "Trees",
    "collision_furniture": "Fences and roadside detail",
    "collision_skirts": "Foundations",
    "collision_paint": "Markings and painted ground",
    "collision_lights": "Lampposts",
    "collision_min_footprint": "Ignore objects narrower than",
    # textures
    "texture_max": "Texture detail",
    "texture_max_normal": "Surface bumpiness detail",
    "texture_max_data": "Shine and roughness detail",
    "texture_dds": "Prepare textures during the build",
    "terrain_upsample": "Ground detail",
    "terrain_brightness": "Ground brightness",
    "paint_kinds": "What gets recoloured",
    "paint_variants_tree": "Shades per kind of plant",
    "paint_variants_prop": "Shades per kind of prop",
    "texture_max_variant": "Recoloured texture detail",
    # navigation
    "ai_roads": "Minimap and routing",
    "navgraph_taxiways": "Include airport taxiways",
    "pois": "Fast-travel points",
    "poi_max": "How many to suggest",
    # advanced - detail and performance (jargon on purpose; see SECTIONS)
    "generate_lods": "Generate LOD chains",
    "tile_lod": "LOD chains for world tiles",
    "imposters": "Billboard imposters",
    "imposter_min_metres": "Imposter minimum cull distance",
    "lod_detail_scale": "Cull distance divisor",
    "lod_detail_scale_prop": "Cull distance divisor, props",
    "lod_switch_error_px": "LOD switch error budget",
    "lod0_max_error": "LOD 0 promotion threshold",
    "lod0_min_triangles": "LOD 0 promotion floor",
    "forest_min_items": "Forest type merge threshold",
    "lane_max_tris": "Baked lane furniture budget",
    "paint_factor_max_error": "colorSet factor tolerance",
    "foliage_road_clearance": "Lane clearance for trees",
    "forest_shadows": "Forest shadow casting",
    "tree_wind": "Synthesised wind vertex colours",
    "tile_size": "World tile size",
    "terrain_holes": "Terrain hole rule",
    "terrain_blend_sharpen": "Ground blend interlock",
    "lights_max": "Dynamic light ceiling",
    "navgraph_max_width": "Navgraph node radius clamp",
    # advanced - the machine
    "gpu_vram_mib": "Dedicated VRAM",
    "render_width": "Render width",
    "render_height": "Render height",
    # advanced - build process
    "jobs": "Worker processes",
    "cook": "Cook shapes after the build",
    "rebuild": "Force a full rebuild",
    "keep_partial": "Keep a cancelled level",
    "sweep_temp": "Sweep BeamNG's temp folder",
    "keep_dae": "Keep the COLLADA sources",
    "ui_browser": "UI host",
    # driven
    "quality_preset": "Quality preset",
    "terrain_cut": "Cut the ground at tunnels",
    "trees": "Trees on or off",
    "props": "Props on or off",
    "poi_file": "Your own fast-travel points",
    "ui_section": "Last settings section",
}

# One line each. Outside the Advanced section, in the words of somebody who has never read this
# repository; inside it, in the words of the thing itself. What changes in the world first, then
# what it costs. The long form - what was tried, what it cost, why the default is the default -
# is neither softened nor deleted; it stays in `DETAILS` and goes to the reference page.
BLURBS = {
    # the basics strip
    "bundle": "The folder Cities: Skylines II wrote when you ripped your city.",
    "output": "Where BeamNG keeps its levels. Your finished level is written in here.",
    "beamng_exe": "Where the game is installed. Filled in for you when it was found on this machine.",
    "level_name": "The name of the folder your level is written into. Letters, digits and dashes.",
    "level_title": "The name shown in the level list once it is built.",
    # world
    "region": "Build the whole city, or one square patch of it. A smaller piece builds far faster.",
    "buildings": "Houses, offices, factories and everything else your city has built.",
    "networks": "Road, rail and path surfaces, and the ground opened up at trenches and tunnels.",
    "foliage_density": "How much of the planting to keep. At zero none of it is placed at all.",
    "prop_density": "How many of the small street objects to keep. At zero none are placed.",
    "markings": "Lane lines, arrows, crossings and the rest of the paint on the tarmac.",
    "areas": "Car parks, plazas, footpaths and the other flat coloured ground.",
    "skirts": "The strip of wall that fills the gap where a plot meets sloping ground.",
    "lane_furniture": "Fences, hedges and overhead cables that run alongside the roads.",
    "lane_furniture_categories": "Which sorts of roadside detail get built. Clearing them all leaves none of it.",
    "lights": "Lamps, floodlights and lit signs glow after dark. Heavy on your graphics card.",
    "water": "Rivers, lakes and the sea.",
    "groundcover": "Blades of grass over the ground. Dressing for the eye; your city has none.",
    "view_distance": "The distance in metres past which nothing is drawn. The biggest lever on speed.",
    "tod_hour": "What hour the sun sits at, either your city's own or one you pick.",
    # collision
    "collision_preset": "Exact shapes, simple boxes, or nothing solid at all. Boxes are far lighter.",
    "collision_buildings": "Cars stop at the walls instead of driving through them.",
    "collision_props": "Benches, bins and signs are solid.",
    "collision_trees": "Trunks stop a car. The leaves never do.",
    "collision_furniture": "Barriers, hedges and cable poles stop a car.",
    "collision_skirts": "The strip under a plot is solid, so you cannot drive into the gap.",
    "collision_paint": "Paint is solid too. Usually a mistake: it sits just above the road surface.",
    "collision_lights": "A pole stops a car dead. Off by default because it is the thing you hit most.",
    "collision_min_footprint": "Anything thinner than this across is driven straight through.",
    # textures
    "texture_max": "The largest a colour picture may be, in pixels. The main cost on your card.",
    "texture_max_normal": "How finely bricks, planks and grooves are described. Lower is flatter and lighter.",
    "texture_max_data": "How finely wet, matt and metal surfaces are described. Rarely needs to be large.",
    "texture_dds": "Do the compression now instead of the first time you play. Leave this on.",
    "terrain_upsample": "How finely the ground is shaped. Higher is sharper underfoot and costs memory.",
    "terrain_brightness": "Lighten or darken the grass, sand and dirt. One leaves them as your city has them.",
    "paint_kinds": "Which sorts of object take their colour from the city. The rest draw an untinted one.",
    "paint_variants_tree": "How many shades each kind of plant comes in. More is prettier and heavier.",
    "paint_variants_prop": "How many shades each kind of small object comes in.",
    "texture_max_variant": "How large the recoloured copies may be, in pixels.",
    # navigation
    "ai_roads": "Lets the minimap, route markers and traffic use the roads. Off leaves the map blank.",
    "navgraph_taxiways": "Let routes run across the apron and the taxiways, slowly.",
    "pois": "Places you can jump straight to from the big map.",
    "poi_max": "How many busy spots to pick out for you when you have named none of your own.",
    # advanced - detail and performance
    "generate_lods": "Build reduced-triangle LOD levels for every shape. Off ships LOD 0 at every distance.",
    "tile_lod": "Give the merged road, marking, area, skirt and furniture tiles an LOD chain of their own.",
    "imposters": "Let BeamNG render a distant shape to a billboard atlas. Cheap to draw, and visibly flat.",
    "imposter_min_metres": "A shape culled nearer than this gets no billboard atlas. 0 derives it from the far cull.",
    "lod_detail_scale": "Divide every shape's CS2 cull distance by this. 1 reproduces what the game itself draws.",
    "lod_detail_scale_prop": "The cull divisor applied to props alone. 0 makes props share the global divisor.",
    "lod_switch_error_px": "Screen-space error in pixels a generated LOD may show as it takes over. Lower is truer.",
    "lod0_max_error": "Drop LOD 0 where LOD 1's p95 surface deviation is within this many metres. 0 keeps every one.",
    "lod0_min_triangles": "Never promote where that would leave the shape shipping fewer triangles than this.",
    "forest_min_items": "A forest type holding fewer items than this is merged into its prefab's untinted type.",
    "lane_max_tris": "Triangle ceiling for the sheared fence and cable tiles, spent in CS2's own cull order.",
    "paint_factor_max_error": "How far a colorSet variant may drift, in 8-bit channels, before it gets its own texture.",
    "foliage_road_clearance": "Declared deviation: drop a tree whose trunk is within this of a drivable lane centre.",
    "forest_shadows": "Forest items cast shadows. One of the heaviest per-frame costs a dense canopy carries.",
    "tree_wind": "Write wind weights into tree vertex colours so BeamNG animates them. CS2 records no wind.",
    "tile_size": "Edge length of the merged road, marking, area and skirt tiles.",
    "terrain_holes": "Which clip-volume cells are punched out of the heightfield as terrain holes.",
    "terrain_blend_sharpen": "Multiplier on BeamNG's derived layer-blend sharpness. 1 is faithful, higher is neater.",
    "lights_max": "Ceiling on punctual lights placed, ranked by BeamNG's own importance metric.",
    "navgraph_max_width": "Clamp a navgraph node's road width. 0 uses the width CS2's own lane offsets give.",
    # advanced - the machine
    "gpu_vram_mib": "How much memory the card this level is built for has. 0 reads it from your own BeamNG log.",
    "render_width": "Horizontal render resolution this level is built for. 0 reads it from your own BeamNG log.",
    "render_height": "Vertical render resolution this level is built for. 0 reads it from your own BeamNG log.",
    # advanced - build process
    "jobs": "Parallel worker processes for the multiprocess stages. 0 leaves one core free.",
    "cook": "Run BeamNG's headless mesh compiler over the level so the first load does not have to.",
    "rebuild": "Ignore the recorded stage state and run every stage from scratch.",
    "keep_partial": "Leave a partially built level on disk after a cancel instead of removing it.",
    "sweep_temp": "Delete cooked shapes in BeamNG's temp folder that this level no longer references.",
    "keep_dae": "The .dae sources stay beside the cooked .cdae. Off saves disk and forces a full recook.",
    "ui_browser": "Where this window opens: a native window, a named browser, or the system default.",
    # driven
    "quality_preset": "The named quality level the settings were last set from.",
    "terrain_cut": "Open the ground at trenches and tunnel mouths and drop the cut mesh into the hole.",
    "trees": "Whether any planting is placed at all.",
    "props": "Whether any small street objects are placed at all.",
    "poi_file": "Places you name and put on the map yourself, instead of the ones picked for you.",
    "ui_section": "Which part of the settings you had open. Remembered between sessions.",
}

_CONTROL_BY_KIND = {
    "bool": "switch",
    "choice": "choice",
    "path": "path",
    "str": "text",
    "int": "number",
    "float": "number",
}


def control_of(key):
    """The control kind for a key: its entry in `CONTROLS`, else what its type implies."""
    if key in CONTROLS:
        return CONTROLS[key]
    kind = SCHEMA[key][1].split(":", 1)[0]
    return _CONTROL_BY_KIND.get(kind, "text")


def section_of(key):
    """`"basics"`, a section id, or `"driven"` - the one place on screen this key is shown."""
    if key in BASICS:
        return "basics"
    for section in SECTIONS:
        if any(key in keys for _title, keys in section["blocks"]):
            return section["id"]
    return "driven"


def is_technical(key):
    """Whether this key lives in a section that keeps its jargon (see SECTIONS)."""
    where = section_of(key)
    return any(s.get("technical") and s["id"] == where for s in SECTIONS)


def quality_keys():
    """The keys whose value is a QUALITY choice, so that changing one puts you on `custom`.

    Everything in a sidebar section, which is every setting that changes the level that gets
    built - minus the two that only decide where the window is. The basics strip is not in it:
    naming your level folder is not a change of quality.
    """
    ui_only = {"ui_section", "ui_browser"}
    return sorted({k for s in SECTIONS for _t, keys in s["blocks"] for k in keys} - ui_only)


def sections_for_ui():
    """`SECTIONS` as the page wants it: blocks as dicts, holding only keys the app knows."""
    out = []
    for section in SECTIONS:
        entry = {k: v for k, v in section.items() if k != "blocks"}
        entry["blocks"] = [
            {"title": title, "keys": [k for k in keys if k in SCHEMA]}
            for title, keys in section["blocks"]
        ]
        out.append(entry)
    return out


# How the UI groups the knobs; every SCHEMA key appears exactly once (checked by test_app_shell).
GROUPS = [
    ("Where", ["bundle", "output", "level_name", "level_title", "region"]),
    (
        "What",
        [
            "buildings",
            "networks",
            "trees",
            "props",
            "markings",
            "areas",
            "skirts",
            "terrain_cut",
            "lane_furniture",
            "lane_furniture_categories",
            "water",
            "groundcover",
            "lights",
            "lights_max",
            "ai_roads",
            "navgraph_max_width",
            "navgraph_taxiways",
            "pois",
            "poi_file",
            "poi_max",
        ],
    ),
    (
        "How much",
        [
            "foliage_density",
            "prop_density",
            "texture_max",
            "texture_max_normal",
            "texture_max_data",
            "texture_max_variant",
            "texture_dds",
            "generate_lods",
            "tile_lod",
            "imposters",
            "imposter_min_metres",
            "lod_detail_scale",
            "lod_detail_scale_prop",
            "lod_switch_error_px",
            "lod0_max_error",
            "lod0_min_triangles",
            "sweep_temp",
            "keep_dae",
            "paint_kinds",
            "paint_factor_max_error",
            "paint_variants_tree",
            "paint_variants_prop",
            "forest_min_items",
            "lane_max_tris",
            "foliage_road_clearance",
            "view_distance",
            "tod_hour",
            "terrain_brightness",
            "tile_size",
            "terrain_upsample",
            "terrain_holes",
            "terrain_blend_sharpen",
            "forest_shadows",
            "tree_wind",
        ],
    ),
    (
        "Collision",
        [
            "collision_preset",
            "collision_buildings",
            "collision_props",
            "collision_trees",
            "collision_furniture",
            "collision_skirts",
            "collision_paint",
            "collision_min_footprint",
            "collision_lights",
        ],
    ),
    ("Target machine", ["gpu_vram_mib", "render_width", "render_height"]),
    (
        "Process",
        [
            "jobs",
            "rebuild",
            "keep_partial",
            "cook",
            "beamng_exe",
            "ui_browser",
            "ui_section",
            "quality_preset",
        ],
    ),
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
    # Collision / Process
    "collision_preset",
    "cook",
]
BASIC_MAX = 28

# One line per preset, for the segmented control. What it costs, not what it contains — the
# numbers come from `pipeline/history.py`'s measured stage rates on the reference city.
PRESET_INFO = {
    "ultra": "Everything, full-size textures, longest view. Hours to build, and only an ultra card runs it.",
    "high": "Everything at 512 px. The default, and what the reference city is validated at.",
    "performance": "Everything, smaller textures and fewer drawn triangles. For an 8 GB card at high resolution.",
    "medium": "60 % of the trees, 70 % of the props, 3 km view. A mid-range card.",
    "low": "Roads, buildings and a third of the foliage. What runs on a laptop.",
}

PRESETS = {
    "ultra": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 0,
        "texture_max_normal": 0,
        "texture_max_data": 0,
        "texture_max_variant": 0,
        "generate_lods": True,
        "paint_variants_tree": 4,
        "paint_variants_prop": 2,
        "forest_min_items": 0,
        "lane_max_tris": 0,
        "tile_lod": True,
        "lane_furniture": True,
        "lights": True,
        "groundcover": False,
        "forest_shadows": True,
        "collision_preset": "exact",
        "lod_detail_scale": 3.0,
        "lod_switch_error_px": 4.0,
        "view_distance": 8000,
    },
    # 8.0 for `lod_switch_error_px`, not the 6.0 this shipped with until 2026-08-20. `high` is
    # what the defaults are, and the defaults are 8.0 - which is the value the reference city was
    # built and validated at through loads #11-#17, while 6.0 was never built at all. The two
    # disagreed silently for as long as nothing compared them; the sidebar's badge showed a fresh
    # install as "High, with 1 setting of your own", and `test_app_shell` now gates it.
    "high": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 512,
        "texture_max_normal": 512,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 10,
        "lane_max_tris": 0,
        "tile_lod": True,
        "lane_furniture": True,
        "lights": False,
        "groundcover": False,
        "forest_shadows": True,
        "collision_preset": "simplified",
        "lod_detail_scale": 6.0,
        "lod_switch_error_px": 8.0,
        "view_distance": 4000,
    },
    # Everything `high` ships, plus the knobs that cost residency rather than geometry — see
    # `docs/cs2-api-audit.md` §64 and §65. The two texture caps came down to 256 in A6.0: at 512
    # this preset mounted 5,224 MiB, which is the MEASURED point at which an 8 GB card at
    # 3440x1440 starts refusing allocations (131,073 of them in one session). 256 takes 497 MiB
    # off — 282 colour, 215 normal, measured over all 7,661 DDS — without touching a triangle,
    # which is why it goes first. It does put colour below vanilla practice (185 of
    # west_coast_usa's 256 colour maps are 512 or smaller, so some ARE 512); the geometry is
    # worth more than the texel, and `high` still ships 512 for looking at things up close.
    "performance": {
        "foliage_density": 100,
        "prop_density": 100,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 10,
        "lane_max_tris": 4000000,
        "tile_lod": True,
        "lane_furniture": True,
        "lane_furniture_categories": "fence",
        "lights": True,
        "groundcover": False,
        "forest_shadows": False,
        "collision_furniture": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 10.0,
        "lod_detail_scale_prop": 4.0,
        "lod_switch_error_px": 8.0,
        "view_distance": 4000,
    },
    "medium": {
        "foliage_density": 60,
        "prop_density": 70,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 256,
        "texture_max_variant": 256,
        "generate_lods": True,
        "paint_variants_tree": 2,
        "paint_variants_prop": 1,
        "forest_min_items": 25,
        "lane_max_tris": 3000000,
        "tile_lod": True,
        "lane_furniture": True,
        "lane_furniture_categories": "fence",
        "lights": False,
        "groundcover": False,
        "forest_shadows": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 10.0,
        "lod_switch_error_px": 12.0,
        "view_distance": 3000,
    },
    "low": {
        "foliage_density": 30,
        "prop_density": 40,
        "texture_max": 256,
        "texture_max_normal": 256,
        "texture_max_data": 128,
        "texture_max_variant": 128,
        "generate_lods": True,
        "paint_kinds": "tree",
        "paint_variants_tree": 1,
        "paint_variants_prop": 1,
        "forest_min_items": 50,
        "lane_max_tris": 1000000,
        "tile_lod": True,
        "lane_furniture": False,
        "lights": False,
        "groundcover": False,
        "forest_shadows": False,
        "collision_preset": "simplified",
        "lod_detail_scale": 16.0,
        "lod_switch_error_px": 20.0,
        "markings": True,
        "areas": False,
        "skirts": False,
        "view_distance": 2000,
    },
}


def app_dir():
    base = os.environ.get("CS2BEAMNG_HOME") or os.path.join(os.path.expanduser("~"), APP_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def defaults():
    out = {key: spec[0] for key, spec in SCHEMA.items()}
    out["bundle"] = default_bundle_dir()
    out["output"] = default_levels_dir()
    # ...and the executable, which was detected but never written down, so the box sat empty
    # beside a line saying it had been found. The three paths the app finds for you now behave
    # the same way: found once, shown, and yours to change.
    out["beamng_exe"] = find_beamng_exe()
    return out


def load():
    """Saved settings merged over the defaults; unknown keys are kept (forward compatible)."""
    out = defaults()
    path = os.path.join(app_dir(), SETTINGS_FILE)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in SCHEMA:
                        out[key] = value
        except (OSError, ValueError):
            pass
    return out


def save(values):
    path = os.path.join(app_dir(), SETTINGS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {k: v for k, v in values.items() if k in SCHEMA}, handle, indent=1, sort_keys=True
        )
    os.replace(tmp, path)


CUSTOM_FILE = "custom.json"


def save_custom(values, base):
    """Remember the settings the user has made their own, and the preset they started from.

    The preset row shows Custom at all times and pressing it comes back HERE, so a person who
    has spent ten minutes tuning can look at what `Performance` does and return without losing
    any of it. `base` is what the badges are counted against - "what have I moved off the
    preset" has no answer at all once the preset name has been overwritten with "custom", which
    is why it is kept beside the values rather than in place of them.
    """
    path = os.path.join(app_dir(), CUSTOM_FILE)
    tmp = path + ".tmp"
    payload = {"base": base, "values": {k: v for k, v in values.items() if k in SCHEMA}}
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_custom():
    """`{"base": name, "values": {...}}`, or None when nothing has been made custom yet."""
    path = os.path.join(app_dir(), CUSTOM_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("values"), dict):
        return None
    base = saved.get("base")
    return {
        "base": base if base in PRESETS else None,
        "values": {k: v for k, v in saved["values"].items() if k in SCHEMA},
    }


def apply_custom(values):
    """The remembered custom settings back over the current ones. Unchanged if there are none."""
    saved = load_custom()
    if not saved:
        return dict(values)
    out = dict(values)
    out.update(saved["values"])
    out["quality_preset"] = "custom"
    return out


def apply_preset(values, name):
    if name not in PRESETS:
        raise KeyError(name)
    out = dict(values)
    out.update(PRESETS[name])
    out["quality_preset"] = name
    return out


def preset_changes(values, base=None):
    """The keys whose value has been moved off the preset last applied, sorted.

    `quality_preset` stays the name of the preset that was applied; whether the values still
    match it is derived here rather than stored as a third state ("custom") that has to be kept
    in step with them. It is also what the sidebar's badges count, which is the third answer to
    "the presets don't seem to do anything": after applying one, the sidebar shows where it
    changed things, and after hand-editing it shows what you have moved.

    `base` names the preset to compare against; without it, the one `quality_preset` holds.
    Once the values have been made custom that name is "custom", and the base comes from the
    remembered custom snapshot instead. `None` where there is nothing to compare against at all.
    """
    preset = PRESETS.get(base or values.get("quality_preset"))
    if preset is None:
        return None
    return sorted(key for key, want in preset.items() if values.get(key) != want)


def coerce(key, raw):
    """A string from the CLI/UI into the setting's type. Raises ValueError with a readable reason."""
    if key not in SCHEMA:
        raise ValueError(f"unknown setting {key!r}")
    _default, kind, _help = SCHEMA[key]
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
    else:
        text = raw
    if kind == "bool":
        if isinstance(text, bool):
            return text
        low = str(text).lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key}: expected true/false, got {raw!r}")
    if kind == "int":
        try:
            return int(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: expected a whole number, got {raw!r}") from None
    if kind == "float":
        try:
            return float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: expected a number, got {raw!r}") from None
    if kind.startswith("choice:"):
        choices = kind.split(":", 1)[1].split("|")
        s = str(text)
        if s not in choices:
            raise ValueError(f"{key}: expected one of {choices}, got {raw!r}")
        return int(s) if all(c.lstrip("-").isdigit() for c in choices) else s
    if kind in ("str", "path"):
        return None if text in ("", None) else str(text)
    return text


def validate(values):
    """Problems as a list of strings (empty = ok). Does not touch the filesystem beyond exists()."""
    problems = []
    if not values.get("bundle") or not os.path.isfile(
        os.path.join(values["bundle"], "bundle.json")
    ):
        problems.append(f"bundle: no bundle.json at {values.get('bundle')!r}")
    if not values.get("output"):
        problems.append("output: no output directory")
    name = values.get("level_name") or ""
    if not name or any(c for c in name if not (c.isalnum() or c in "_-")):
        problems.append("level_name: letters, digits, '_' and '-' only")
    for key in ("foliage_density", "prop_density"):
        v = values.get(key)
        if not isinstance(v, int) or not 0 <= v <= 100:
            problems.append(f"{key}: 0..100")
    region = values.get("region")
    if region:
        try:
            parts = [float(p) for p in str(region).replace(",", " ").split()]
            if len(parts) != 3 or parts[2] <= 0:
                raise ValueError
        except ValueError:
            problems.append("region: 'X Z R' with R > 0, or empty")
    return problems


def region_of(values):
    region = values.get("region")
    if not region:
        return None
    x, z, r = [float(p) for p in str(region).replace(",", " ").split()]
    return {"centerX": x, "centerZ": z, "radius": r}


# -- per-OS defaults -----------------------------------------------------------------------------

BEAMNG_APP_ID = "284160"


def candidate_userfolders():
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return [
            os.path.join(local, "BeamNG", "BeamNG.drive", "current"),
            os.path.join(local, "BeamNG.drive", "current"),
            os.path.join(local, "BeamNG.drive"),
        ]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "BeamNG.drive", "current")]
    roots = [
        os.path.join(home, ".local", "share", "BeamNG.drive", "current"),
        os.path.join(home, ".local", "share", "BeamNG", "BeamNG.drive", "current"),
    ]
    for steam in (
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
    ):
        roots.append(
            os.path.join(
                steam,
                "steamapps",
                "compatdata",
                BEAMNG_APP_ID,
                "pfx",
                "drive_c",
                "users",
                "steamuser",
                "AppData",
                "Local",
                "BeamNG",
                "BeamNG.drive",
                "current",
            )
        )
    return roots


def default_levels_dir():
    for folder in candidate_userfolders():
        if os.path.isdir(folder):
            return os.path.join(folder, "levels")
    return os.path.join(os.path.expanduser("~"), "cs2beamng", "levels")


def candidate_beamng_exes():
    """Likely BeamNG executables on this machine (Steam libraries), most likely first."""
    out = []
    if sys.platform.startswith("win"):
        libraries = [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam")
        ]
        vdf = os.path.join(libraries[0], "steamapps", "libraryfolders.vdf")
        libraries += _steam_libraries(vdf)
        for lib in libraries:
            out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BeamNG.drive.exe"))
    elif sys.platform.startswith("linux"):
        home = os.path.expanduser("~")
        for steam in (
            os.path.join(home, ".steam", "steam"),
            os.path.join(home, ".local", "share", "Steam"),
        ):
            libraries = [
                steam,
                *_steam_libraries(os.path.join(steam, "steamapps", "libraryfolders.vdf")),
            ]
            for lib in libraries:
                out.append(
                    os.path.join(
                        lib, "steamapps", "common", "BeamNG.drive", "BinLinux", "BeamNG.drive.x64"
                    )
                )
    return out


def _steam_libraries(vdf_path):
    libs = []
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith('"path"'):
                    parts = line.split('"')
                    if len(parts) >= 4:
                        libs.append(parts[3].replace("\\\\", "\\"))
    except OSError:
        pass
    return libs


def find_beamng_exe(values=None):
    explicit = (values or {}).get("beamng_exe")
    if explicit and os.path.isfile(explicit):
        return explicit
    for cand in candidate_beamng_exes():
        if os.path.isfile(cand):
            return cand
    return None


def detected(values=None):
    """What this machine offers, for the UI's path pickers: bundles that exist, levels folders that
    exist, the BeamNG executable if any. Never raises."""
    from csiibundle.bake.locate import candidate_bundle_dirs

    bundles = [p for p in candidate_bundle_dirs() if os.path.isfile(os.path.join(p, "bundle.json"))]
    levels = [os.path.join(f, "levels") for f in candidate_userfolders() if os.path.isdir(f)]
    out = {
        "bundles": bundles,
        "levelsDirs": levels,
        "beamngExe": find_beamng_exe(values),
        "platform": sys.platform,
        "appDir": app_dir(),
    }
    # The machine the level is FOR, read the same way the validator reads it, so the UI's
    # "target machine" line and the build's own budget can never disagree. Never raises: this
    # runs on every settings request and a missing BeamNG log must not blank the page.
    try:
        from .beamng import validate_level

        vram, pixels, source = validate_level.render_target(values)
        out["budget"] = {
            "vramMiB": vram,
            "pixels": pixels,
            "source": source,
            "residentMiB": round(validate_level.resident_budget_mib(vram, pixels)),
            "lighting": validate_level.light_manager()[0],
        }
    except Exception:
        out["budget"] = None
    return out


def schema_for_ui():
    """SCHEMA as a list the UI can render: [{key, kind, default, help, detail, tier, group, choices}].

    `help` is always shown and is one sentence; `detail` is the long form and is shown only when
    the user asks for it. `tier` is "basic" or "advanced" — the basic ones are the default panel.
    """
    group_of = {key: title for title, keys in GROUPS for key in keys}
    basic = set(BASIC)
    out = []
    for key, (default, kind, help_text) in SCHEMA.items():
        entry = {
            "key": key,
            "kind": kind.split(":", 1)[0],
            "default": default,
            "help": help_text,
            "group": group_of.get(key, "Other"),
            "tier": "basic" if key in basic else "advanced",
            # E1: what the redesigned screen needs. `label`/`blurb` are the plain words,
            # `control` is how it is drawn, `section` is the one place it appears.
            "label": LABELS[key],
            "blurb": BLURBS[key],
            "control": control_of(key),
            "section": section_of(key),
        }
        if key in RANGES:
            low, high, step, suffix = RANGES[key]
            entry["range"] = {"min": low, "max": high, "step": step, "suffix": suffix}
        if key in ZERO_LABEL:
            entry["zeroLabel"] = ZERO_LABEL[key]
        if key in DRIVEN_BY:
            entry["drivenBy"] = DRIVEN_BY[key]
        if key in UNITS:
            entry["unit"] = UNITS[key]
        if key in SUBSET_OF:
            entry["subsetOf"] = SUBSET_OF[key]
        if key in NEEDS:
            entry["needs"] = NEEDS[key]
        if key in CHOICE_SETS:
            entry["choiceSet"] = [{"id": cid, "label": label} for cid, label in CHOICE_SETS[key]]
        entry["technical"] = is_technical(key)
        if key in DETAILS:
            entry["detail"] = DETAILS[key]
        if kind.startswith("choice:"):
            entry["choices"] = kind.split(":", 1)[1].split("|")
        out.append(entry)
    return out
