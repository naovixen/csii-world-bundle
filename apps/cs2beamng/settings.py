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
    "bundle": (None, "path", "Bundle directory (default: the CS2 ModsData export for this OS)"),
    "output": (None, "path", "Levels directory to write into (default: the BeamNG userfolder levels/)"),
    "level_name": ("cs2_city", "str", "Level folder name (lowercase, underscores)"),
    "level_title": ("CS2 City", "str", "Level title shown in BeamNG"),
    "region": (None, "str", "Region as 'X Z R' in bundle metres, or empty for the whole city"),
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
    "terrain_cut": (True, "bool", "Pre-clipped ground meshes at trenches and tunnel portals (the heightfield is punched there and the bake's cut lattice placed in the hole - the Blender cut, in-game)"),
    "lights": (False, "bool", "Street and building lights (dynamic lights are expensive)"),
    # Needs BeamNG on ADVANCED lighting. Basic Lighting has no g-buffer prepass and shades no
    # point or spot light at all, so on that profile every placed light is pure cost (audit §82);
    # `validate` reads the user's own `game-settings.json` and says so.
    "lights_max": (25000, "int", "Maximum dynamic lights (needs Advanced Lighting in BeamNG)"),
    "water": (True, "bool", "Water planes/blocks"),
    "groundcover": (False, "bool", "Billboard grass over the ground paint. NOT a fidelity feature - CS2 has no grass, detail-mesh or groundcover system at all, so the blades are synthesised BeamNG dressing for a camera at driving height (audit 62)"),
    "ai_roads": (True, "bool", "The AI NAVGRAPH: invisible DecalRoads built from the lane graph. NOT cosmetic and not experimental - `map.lua` builds `map.getMap().nodes` from DecalRoads and BeamNGWaypoints and NOTHING ELSE, so with this off the minimap app draws nothing, a big-map waypoint produces no route and the ground-marker arrows never appear (audit A7.0). west_coast_usa ships 1,299 of these on `road_invisible`"),
    "navgraph_max_width": (0.0, "float", "Clamp a navgraph node's road width, in metres (0 = the width measured from CS2's own lane offsets). BeamNG merges two navgraph nodes when they are closer than the LARGER of their radii, and that test is 3-D (map.lua:1188), so a 24 m road's node reaches 12 m downwards and can weld a bridge deck into the street beneath it. `tools/check_navgraph_vs_game.py` counts what would weld; on the reference city nothing does at 0"),
    "navgraph_taxiways": (False, "bool", "Put airport taxiways in the navgraph too, at drivability 0.3 (roads and highways are always in it; pathways, trams, rails, waterways, cables and pipes never are)"),
    "pois": (True, "bool", "Fast-travel points - the big map's \"Quicktravel Points\" group. Derived from the densest road cells unless `poi_file` names your own list"),
    "poi_file": (None, "path", "JSON list of your own fast-travel points: [{\"name\": \"Vice City Beach\", \"at\": [4820, 1660], \"description\": \"...\"}]. `at` is BeamNG-frame X/Y (what the big map shows); add \"frame\": \"bundle\" for bundle X/Z. Each is snapped onto the road network"),
    "poi_max": (8, "int", "How many fast-travel points to derive when `poi_file` names fewer. Vanilla levels ship 3 (italy) to 9 (west_coast_usa)"),
    # how much
    "foliage_density": (100, "int", "Percent of trees kept (deterministic subset)"),
    "prop_density": (100, "int", "Percent of props kept"),
    "texture_max": (512, "choice:256|512|1024|2048|0", "COLOUR texture edge cap in pixels (0 = full size). Calibrated on BeamNG's own biggest level: 185 of west_coast_usa's 256 colour maps are 512 or smaller. Also the cap for the emissive and opacity maps, which are the albedo's own resolution"),
    "texture_max_normal": (512, "choice:256|512|1024|2048|0", "NORMAL map edge cap in pixels (0 = full size). Vanilla runs 512-1024"),
    "texture_max_data": (256, "choice:128|256|512|1024|0", "DATA map edge cap in pixels (roughness, metallic, the CS2 colour mask). 391 of west_coast_usa's 470 data maps are 256 or smaller - a roughness gradient carries almost no high-frequency detail"),
    "texture_dds": (True, "bool", "Ship textures pre-compressed as mipmapped DDS (BC7/BC5/BC4, what BeamNG's cooker would make) so the game never cooks them on first load; off = PNG"),
    "generate_lods": (True, "bool", "Generate LOD chains for every shape"),
    "tile_lod": (True, "bool", "Give the merged world tiles (roads, markings, areas, skirts, furniture) a real LOD chain. Each level still holds exactly what CS2 draws at its distance, but the switch distances are anchored to `view_distance` instead of the game's own null distance - which for a road is 46 km, so every switch fell outside any view distance we ship and 82.5 % of a networks tile's triangles survived to its far level. Off = the A4 ladder"),
    "imposters": (True, "bool", "Engine-generated billboard imposters (bb_autobillboard)"),
    "imposter_min_metres": (0, "int", "Only give an imposter to a shape this level still DRAWS beyond this distance; 0 derives it as three quarters of `view_distance`. BeamNG builds an imposter atlas at RUNTIME, by rendering the shape from every angle the first time it comes into view - so one per shape is one atlas held in memory and a stall when you pan. A4/A5 gave one to EVERY object shape - 4,245 of 4,245 - against west_coast_usa's 82 of 2,608 (3.1 %), italy's 102 (7.2 %) and johnson_valley's 22 (4.4 %); `beamng.log` showed 2,652 of them still being generated 14 minutes into the session, and the memory climbing with them. A billboard is worth an atlas for a landmark still drawn at the edge of the view distance and not for a mailbox culled at 80 m"),
    "lod_detail_scale": (6.0, "float", "How many times CLOSER than CS2 an object is culled. Every shape's threshold is the game's own answer for that mesh (bounds + triangle area, csiibundle.lod); this is the one global multiplier on it. 1 = CS2 exactly, which draws a median prop to 1 km and a median building to 7.3 km over 1.2M objects"),
    "lod_detail_scale_prop": (0.0, "float", "The same multiplier as `lod_detail_scale` but for PROPS only; 0 = use the global one. WHY IT IS SEPARATE (A6.2): CS2 draws a median prop to 1,024 m and a median building to 13,008 m - a factor of 12.7 - and one global divisor preserves that ratio while destroying the RELATIONSHIP. At the game's 1,024 m a window frame is sub-pixel and losing it costs nothing; at 1,024/10 = 105 m it is plainly visible, and the building it is bolted to is still drawn to 1,332 m, so facades read as construction sites. 472,141 of 504,116 props (94 %) are parented to something. DECLARED TUNING: CS2 has no per-kind divisor, so this has no counterpart in the game - it exists because BeamNG cannot draw what CS2 draws and the two kinds must be given up at different rates"),
    "lod_switch_error_px": (8.0, "float", "How many PIXELS of surface error a generated LOD level may show at the moment it takes over. Each level's displacement from LOD 0 is measured by the bake (p95 of the distance from its surface back to the original's, in metres) and the switch threshold is derived from it, so a level that is geometrically identical keeps its place to the horizon and a level that is a metre out waits until a metre stops showing. WHY IT IS NOT 1 (A6.7): the ladder used to be fixed multiples of the shape's CULL threshold, which is a statement about its SIZE - and a level's error is not. A LOD1's displacement runs from 0 m on 603 of 4,261 shapes to 37 m on the worst (p50 0.018, p99 6.167), so one multiple cannot serve both ends and the coarse end reads as a roof moving under props that have not moved. Deriving from the error fixes the RATIO for nothing; the absolute level is a triangle budget. MEASURED over 1.16M placements from the four densest viewpoints: 1 px costs +384 % to +630 % drawn triangles, 4 px +1 % to +61 %, 6 px +0 % to +16 %, 8 px -2 % to +2 % - a CS2 residential block is 3,295 triangles of balconies and window reveals that no decimator keeps at 25 %, so a tight budget buys the buildings back at full price. DECLARED TUNING: CS2 has no LOD chain for these prefabs at all (lodCap 0), so there is no game rule to port. Smaller = more faithful and more triangles: MEASURED against the ladder over 1.16M placements, 8 px costs +16 % to +21 % drawn triangles in the two densest DOWNTOWN neighbourhoods and 0 % in the two densest forest ones, 6 px +30 % to +39 %, 4 px +51 % to +67 %. VEGETATION IS EXEMPT and keeps the ladder - the rule's input is a surface distance and a canopy is a cloud of alpha cards, so it measured 262 of 268 tree shapes as needing full geometry twice as far out, +24 % triangles in a forest, for a defect nobody reported. ONE DIRECTION ONLY: this may push a level FURTHER out than the vanilla ladder puts it, never nearer. The first cut allowed both, and a level measuring close to LOD 0 came NEARER on 2,586 of 2,903 prop shapes - a streetlight's 24-triangle level from 13.1 m - which loaded as 'cranes rotated and floating, highrises all diagonal'. A surface distance is not a visual error for thin geometry: flattening a lamp post's tube moves its surface 0.06 m and destroys the object"),
    "paint_kinds": ("tree,prop", "str", "Which record kinds get baked colorSet variants (comma separated). A forest item carries no per-instance colour, so a kind left out of this draws its prefab's UNTINTED albedo - and CS2 authors every tintable surface desaturated, so that is visibly wrong, not subtle: the reference city's prop centres sit a median 0.534 from white in their strongest channel over 448k instances. Costs bake time and textures"),
    "paint_variants_tree": (2, "int", "How many baked colorSet centres a TREE prefab gets. Every extra centre is another shape file and another set of materials, and a forest item is drawn at its nearest centre either way: on the reference city the variant system alone makes 6,759 of the level's 8,220 materials and 879 MiB of its shape files. 4 was the A3 value; 2 keeps the split between the light and dark half of a prefab's palette, which is what reads at distance"),
    "paint_variants_prop": (1, "int", "How many baked colorSet centres a PROP prefab gets. 1 is the prefab's mean colour — props are small, seen at distance and in bulk, and their palettes are narrower than the trees'"),
    "texture_max_variant": (256, "choice:128|256|512|1024|0", "COLOUR cap for a PAINT VARIANT's tinted albedo, separately from `texture_max`. A variant is the same texture as its prefab's canonical albedo with a colour multiplied through it, and it is only ever seen on foliage and props in bulk; the buildings and roads that are looked at up close keep `texture_max`"),
    "forest_min_items": (10, "int", "A forest TYPE holding fewer items than this is merged into its prefab's untinted type instead of getting a shape, a material set and an entry of its own. BeamNG loads every type whole whether it has one item or ten thousand; on the reference city 2,864 of 4,951 types hold 10 items or fewer, 7,641 items between them - 0.7 % of the forest, for 58 % of the types. 0 keeps every type"),
    "lane_max_tris": (0, "int", "Triangle budget for BAKED lane furniture (the sheared fence/cable tiles that cannot be instanced), 0 = no budget. What goes first is what CS2 itself culls FIRST - the drop order is the game's own per-lane `m_MinLod`, so the budget spends itself on the furniture the game draws furthest"),
    "paint_factor_max_error": (4, "int", "How far a paint variant may drift, in 8-bit sRGB channels, before it gets a texture of its own instead of a baseColorFactor. CS2's colorSet tint is a pure per-texel multiply, so where the colour mask is uniform ONE constant reproduces it exactly and costs no texture at all; where the mask selects three different colours across the surface it cannot, and the bake writes the tinted albedo. 0 keeps every texture (the A3 behaviour). MEASURED over all 7,592 (material, variant) pairs of the reference city: 31.4 % are EXACTLY reproducible by a constant, 32.9 % within 4, 39.3 % within 16, 54.5 % within 32. tools/measure_paint_factor.py prints the distribution for your own city"),
    "foliage_road_clearance": (2.5, "float", "DECLARED DEVIATION, 0 = off. Drop a TREE standing within this many metres of a drivable lane centre, so the city can be driven. CS2 has no such rule - it clears objects when a road is BUILT and whatever survives is what the city contains - so this is playability, not fidelity, and it is counted in the report. Trees only: a bush or a flower pot is driven straight through once it stops colliding (A6.1), so removing it would cost fidelity and buy nothing. 2.5 m clears the CARRIAGEWAY, not the vehicle: a lane is ~3.5 m wide so its edge is 1.75 m off the centre, and a trunk radius of up to ~0.5 m reaches it at ~2.25 m. The first cut used 1.5 m - the vehicle envelope - and left trunks standing inside the lane edge. The sweep found no natural threshold to prefer: trees per 0.25 m band are almost flat from 0.5 m out to 5 m, so this is a judgement and is documented as one. On the reference city 2.5 m drops 6,389 items - 2.06 % of trees - against 3,466 at 1.5 m. IT CLIPS BY TRUNK POSITION, NOT CANOPY: an OakTree01 has an 8.11 m canopy half-width, so branches still overhang a road whose trunk line is clear. Clipping every overlapping canopy would take 25,743 trees (8.3 %) and is a deliberate non-choice"),
    "gpu_vram_mib": (0, "int", "Dedicated VRAM of the card the level is FOR, in MiB (0 = read it from your own beamng.log, and fall back to 8018). What a level may mount is a function of this and of the render resolution, not a constant - see beamng.validate_level.resident_budget_mib"),
    "render_width": (0, "int", "Render width the level is FOR (0 = read it from beamng.log, fall back to 1920). Every pixel above 1080p takes VRAM back off the level for the deferred G-buffer and the post chain"),
    "render_height": (0, "int", "Render height the level is FOR (0 = read it from beamng.log, fall back to 1080)"),
    "tile_size": (256, "int", "World tile size for roads/markings/areas (metres)"),
    "view_distance": (4000, "int", "LevelInfo.visibleDistance: the global far cull in metres"),
    "tod_hour": (-1.0, "float", "Time of day in hours 0-24 for the level's sun (-1 = the in-game clock at rip time)"),
    "terrain_brightness": (1.0, "float", "Extra gain on the ground textures (1 = calibrated: CS2 grass to BeamNG's typical grass brightness)"),
    "terrain_upsample": (2, "choice:1|2", "Terrain grid: 2 = 8192 @ 1.75 m (the rip's fine terrainFine raster when present, else bilinear; the game's own near-camera grading, loaded fine on load #13 once the level's VRAM footprint was trimmed); 1 = 4096 @ 3.5 m (the base heightmap, lighter). There is deliberately no 4: 0.875 m is 16384, exactly D3D12's max 2D texture dimension, 768 MiB of terrain textures against 192, and no vanilla level exceeds 4096 - audit 52 has the numbers and does not recommend it"),
    "terrain_holes": ("contain", "choice:contain|majority|none", "Which clip cells become terrain holes"),
    # NOT A PARITY SETTING - see stages/terrain.HEIGHT_SIGMA and audit §48.3. 1.0 is the value
    # BeamNG's own blend arithmetic derives; above that it is a deliberate deviation that trades
    # fidelity for hiding an artefact the engine's texel-sized constants cause on our coarse grid.
    "terrain_blend_sharpen": (1.0, "float", "Ground-boundary interlock, x the derived amount (1 = faithful; 2-3 dissolves the blocky material boundaries our 1.75 m layer grid causes, at the cost of a rougher blend than the game's)"),
    "forest_shadows": (True, "bool", "Forest items cast shadows"),
    "tree_wind": (False, "bool", "Synthesise wind vertex colours for trees"),
    # A8/P2: metres of measured surface error at which LOD 1 replaces LOD 0 outright. 0 = keep
    # every LOD 0. See `stages.shapes.LOD0_MAX_ERROR_DEFAULT` for the measurement behind 0.10.
    "lod0_max_error": (0.10, "float", "Drop LOD0 where LOD1 is within this many metres of it"),
    "lod0_min_triangles": (1000, "int", "...but never where that would leave the shape SHIPPING fewer triangles than this, whatever its measured displacement says. A p95 SURFACE distance cannot see that a decimation opened an object up: flattening a 5 cm sign post moves its surface 3 mm, so NA_ParkingSign01 measured 0.0032 m against the 0.10 m threshold and shipped as 4 triangles - a panel with no post, at every distance - and NA_CommercialLow01_L5_4x3 shipped 381 triangles of facade you can see through. THE RATIO SAYS NOTHING: Five_Luxe_JBR_ keeps 24 % of its triangles and that building keeps 25 %; 59,389 triangles is still a tower and 381 is a hole. MEASURED over the reference city's 3,338 promoted shapes: a floor of 1,000 shipped triangles protects 2,941 of them for 26 % of the triangles the promotion saved, and every heavy shape it exists for is still promoted. 0 = no floor, the A8/P2 behaviour"),
    # A8/P7. `sweep_temp` is free and on. `keep_dae` is ON BY DEFAULT and that is deliberate:
    # dropping the sources saves 5.4 GB of DISK and no RAM, and the NEXT build has to rewrite
    # every `.dae` it deleted, which makes the cook recompile all of them (~9 min on the full
    # city). Turn it off for a build you are shipping, not for one you are iterating on.
    "sweep_temp": (True, "bool", "Delete cooked shapes in BeamNG's temp/ that this level no longer has"),
    "keep_dae": (True, "bool", "Keep .dae sources beside the cooked .cdae (off saves ~5 GB, forces a full recook next build)"),

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
    "collision_lights": (False, "bool", "Street lights, lampposts and every other LIGHT-BEARING prop collide. Off by default: a lamppost is 0.4-3.2 m across so the footprint rule does not catch it, and a 10 m pole that stops a car dead is the one piece of street furniture you hit most. WHICH PREFABS: the ones CS2's own `lights.json` gives a photometric light to - 218 of the 1,186 entries in it, being 44 lamps (Lightpole/WallLight/Streetlight/GardenLight), 115 floodlit billboards, the vehicles, and none of the 12 traffic lights - so this is the game's own answer to 'is this a lamp' and not a name match. The billboards ride along with the lamps and are declared in audit 88 rather than exempted by name"),
    # process
    "jobs": (0, "int", "Worker processes for parallel stages (0 = CPU count - 1)"),
    "rebuild": (False, "bool", "Rebuild every stage from scratch, ignoring level_state.json (what `cs2beamng build --force` sets). Off = a stage whose declared inputs and settings are unchanged is skipped and its objects replayed, which is what makes a no-op rebuild seconds instead of 14 minutes"),
    "keep_partial": (False, "bool", "Keep a partially built level after cancel"),
    "cook": (True, "bool", "Compile the level's shapes to .cdae with BeamNG's headless mesh compiler after the build (skipped when BeamNG is running or not found)"),
    "beamng_exe": (None, "path", "BeamNG executable (auto-detected when empty)"),
    "quality_preset": ("high", "choice:ultra|high|performance|medium|low|custom", "Quality preset last applied"),
    "ui_browser": ("window", "choice:window|chrome|edge|firefox|default", "Where the UI opens: a native window, a named browser, or the system default"),
}

# How the UI groups the knobs; every SCHEMA key appears exactly once (checked by test_app_shell).
GROUPS = [
    ("Where", ["bundle", "output", "level_name", "level_title", "region"]),
    ("What", ["buildings", "networks", "trees", "props", "markings", "areas", "skirts", "terrain_cut", "lane_furniture",
              "lane_furniture_categories", "water", "groundcover", "lights", "lights_max", "ai_roads", "navgraph_max_width",
              "navgraph_taxiways", "pois", "poi_file", "poi_max"]),
    ("How much", ["foliage_density", "prop_density", "texture_max", "texture_max_normal", "texture_max_data",
                  "texture_max_variant", "texture_dds", "generate_lods", "tile_lod", "imposters",
                  "imposter_min_metres", "lod_detail_scale", "lod_detail_scale_prop",
                  "lod_switch_error_px", "lod0_max_error", "lod0_min_triangles", "sweep_temp", "keep_dae",
                  "paint_kinds", "paint_factor_max_error", "paint_variants_tree", "paint_variants_prop",
                  "forest_min_items", "lane_max_tris", "foliage_road_clearance",
                  "view_distance", "tod_hour", "terrain_brightness", "tile_size", "terrain_upsample", "terrain_holes", "terrain_blend_sharpen", "forest_shadows", "tree_wind"]),
    ("Collision", ["collision_preset", "collision_buildings", "collision_props", "collision_trees",
                   "collision_furniture", "collision_skirts", "collision_paint",
                   "collision_min_footprint", "collision_lights"]),
    ("Target machine", ["gpu_vram_mib", "render_width", "render_height"]),
    ("Process", ["jobs", "rebuild", "keep_partial", "cook", "beamng_exe", "ui_browser", "quality_preset"]),
]

PRESETS = {
    "ultra": {"foliage_density": 100, "prop_density": 100, "texture_max": 0, "texture_max_normal": 0,
              "texture_max_data": 0, "texture_max_variant": 0, "generate_lods": True,
              "paint_variants_tree": 4, "paint_variants_prop": 2, "forest_min_items": 0,
              "lane_max_tris": 0, "tile_lod": True,
              "lane_furniture": True, "lights": True, "groundcover": False, "forest_shadows": True,
              "collision_preset": "exact", "lod_detail_scale": 3.0, "lod_switch_error_px": 4.0,
              "view_distance": 8000},
    "high": {"foliage_density": 100, "prop_density": 100, "texture_max": 512, "texture_max_normal": 512,
             "texture_max_data": 256, "texture_max_variant": 256, "generate_lods": True,
             "paint_variants_tree": 2, "paint_variants_prop": 1, "forest_min_items": 10,
             "lane_max_tris": 0, "tile_lod": True,
             "lane_furniture": True, "lights": False, "groundcover": False, "forest_shadows": True,
             "collision_preset": "simplified", "lod_detail_scale": 6.0, "lod_switch_error_px": 6.0,
             "view_distance": 4000},
    # Everything `high` ships, plus the knobs that cost residency rather than geometry — see
    # `docs/cs2-api-audit.md` §64 and §65. The two texture caps came down to 256 in A6.0: at 512
    # this preset mounted 5,224 MiB, which is the MEASURED point at which an 8 GB card at
    # 3440x1440 starts refusing allocations (131,073 of them in one session). 256 takes 497 MiB
    # off — 282 colour, 215 normal, measured over all 7,661 DDS — without touching a triangle,
    # which is why it goes first. It does put colour below vanilla practice (185 of
    # west_coast_usa's 256 colour maps are 512 or smaller, so some ARE 512); the geometry is
    # worth more than the texel, and `high` still ships 512 for looking at things up close.
    "performance": {"foliage_density": 100, "prop_density": 100, "texture_max": 256,
                    "texture_max_normal": 256, "texture_max_data": 256, "texture_max_variant": 256,
                    "generate_lods": True, "paint_variants_tree": 2, "paint_variants_prop": 1,
                    "forest_min_items": 10, "lane_max_tris": 4000000, "tile_lod": True,
                    "lane_furniture": True, "lane_furniture_categories": "fence",
                    "lights": True, "groundcover": False, "forest_shadows": False,
                    "collision_furniture": False, "collision_preset": "simplified",
                    "lod_detail_scale": 10.0, "lod_detail_scale_prop": 4.0,
                    "lod_switch_error_px": 8.0,
                    "view_distance": 4000},
    "medium": {"foliage_density": 60, "prop_density": 70, "texture_max": 256, "texture_max_normal": 256,
               "texture_max_data": 256, "texture_max_variant": 256, "generate_lods": True,
               "paint_variants_tree": 2, "paint_variants_prop": 1, "forest_min_items": 25,
               "lane_max_tris": 3000000, "tile_lod": True,
               "lane_furniture": True, "lane_furniture_categories": "fence", "lights": False,
               "groundcover": False, "forest_shadows": False, "collision_preset": "simplified",
               "lod_detail_scale": 10.0, "lod_switch_error_px": 12.0, "view_distance": 3000},
    "low": {"foliage_density": 30, "prop_density": 40, "texture_max": 256, "texture_max_normal": 256,
            "texture_max_data": 128, "texture_max_variant": 128, "generate_lods": True,
            "paint_kinds": "tree", "paint_variants_tree": 1, "paint_variants_prop": 1,
            "forest_min_items": 50, "lane_max_tris": 1000000, "tile_lod": True,
            "lane_furniture": False, "lights": False, "groundcover": False, "forest_shadows": False,
            "collision_preset": "simplified", "lod_detail_scale": 16.0,
            "lod_switch_error_px": 20.0, "markings": True,
            "areas": False, "skirts": False, "view_distance": 2000},
}


def app_dir():
    base = os.environ.get("CS2BEAMNG_HOME") or os.path.join(os.path.expanduser("~"), APP_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def defaults():
    out = {key: spec[0] for key, spec in SCHEMA.items()}
    out["bundle"] = default_bundle_dir()
    out["output"] = default_levels_dir()
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
        json.dump({k: v for k, v in values.items() if k in SCHEMA}, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def apply_preset(values, name):
    if name not in PRESETS:
        raise KeyError(name)
    out = dict(values)
    out.update(PRESETS[name])
    out["quality_preset"] = name
    return out


def coerce(key, raw):
    """A string from the CLI/UI into the setting's type. Raises ValueError with a readable reason."""
    if key not in SCHEMA:
        raise ValueError(f"unknown setting {key!r}")
    default, kind, _help = SCHEMA[key]
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
    if not values.get("bundle") or not os.path.isfile(os.path.join(values["bundle"], "bundle.json")):
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
        return [os.path.join(local, "BeamNG", "BeamNG.drive", "current"),
                os.path.join(local, "BeamNG.drive", "current"),
                os.path.join(local, "BeamNG.drive")]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "BeamNG.drive", "current")]
    roots = [os.path.join(home, ".local", "share", "BeamNG.drive", "current"),
             os.path.join(home, ".local", "share", "BeamNG", "BeamNG.drive", "current")]
    for steam in (os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam"),
                  os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam")):
        roots.append(os.path.join(steam, "steamapps", "compatdata", BEAMNG_APP_ID, "pfx", "drive_c", "users",
                                  "steamuser", "AppData", "Local", "BeamNG", "BeamNG.drive", "current"))
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
        libraries = [os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam")]
        vdf = os.path.join(libraries[0], "steamapps", "libraryfolders.vdf")
        libraries += _steam_libraries(vdf)
        for lib in libraries:
            out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BeamNG.drive.exe"))
    elif sys.platform.startswith("linux"):
        home = os.path.expanduser("~")
        for steam in (os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam")):
            libraries = [steam] + _steam_libraries(os.path.join(steam, "steamapps", "libraryfolders.vdf"))
            for lib in libraries:
                out.append(os.path.join(lib, "steamapps", "common", "BeamNG.drive", "BinLinux", "BeamNG.drive.x64"))
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
    return {"bundles": bundles, "levelsDirs": levels, "beamngExe": find_beamng_exe(values),
            "platform": sys.platform, "appDir": app_dir()}


def schema_for_ui():
    """SCHEMA as a list the UI can render: [{key, kind, default, help, group, choices}]."""
    group_of = {key: title for title, keys in GROUPS for key in keys}
    out = []
    for key, (default, kind, help_text) in SCHEMA.items():
        entry = {"key": key, "kind": kind.split(":", 1)[0], "default": default, "help": help_text,
                 "group": group_of.get(key, "Other")}
        if kind.startswith("choice:"):
            entry["choices"] = kind.split(":", 1)[1].split("|")
        out.append(entry)
    return out
