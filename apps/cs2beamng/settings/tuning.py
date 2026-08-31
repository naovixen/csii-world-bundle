"""Advanced: detail and performance.

The measured tuning numbers. This is the one block that keeps its jargon — everywhere else a
name that reads like a variable is the defect; here it is the point, because somebody changing a
LOD switch budget needs to know that is what they are changing. Nearly every default here was
arrived at by measurement, and the argument for it is in the setting's `detail`.
"""

from .model import Setting

TUNING_SETTINGS = (
    Setting(
        key="generate_lods",
        default=True,
        kind="bool",
        label="Generate LOD chains",
        blurb="Build reduced-triangle LOD levels for every shape. Off ships LOD 0 at every "
        "distance.",
        help="Generate LOD chains for every shape",
    ),
    Setting(
        key="tile_lod",
        default=True,
        kind="bool",
        label="LOD chains for world tiles",
        blurb="Give the merged road, marking, area, skirt and furniture tiles an LOD chain of "
        "their own.",
        help="Give the merged world tiles (roads, markings, areas, skirts, furniture) a real LOD "
        "chain",
        detail="Gives the merged road, marking, area, foundation and furniture tiles reduced "
        "versions of their own. Each one still holds exactly what your city draws at that "
        "distance, but the distances are measured against how far you can see rather than "
        "against your city's own — which for a road is tens of kilometres, so without this "
        "almost none of a road tile is ever given up.",
    ),
    Setting(
        key="imposters",
        default=True,
        kind="bool",
        label="Billboard imposters",
        blurb="Let BeamNG render a distant shape to a billboard atlas. Cheap to draw, and visibly "
        "flat.",
        help="Engine-generated billboard imposters (bb_autobillboard)",
    ),
    Setting(
        key="imposter_min_metres",
        default=0,
        kind="int",
        label="Imposter minimum cull distance",
        blurb="A shape culled nearer than this gets no billboard atlas. 0 derives it from the far "
        "cull.",
        help="Only give an imposter to a shape still drawn beyond this distance; 0 = 3/4 of the "
        "view distance",
        detail="A shape this level stops drawing nearer than this gets no billboard. 0 works it "
        "out as three quarters of how far you can see. It matters because BeamNG builds "
        "each billboard while you play, by rendering the shape from every angle the first "
        "time it comes into view — so every one is a pause when you pan and a picture held "
        "in memory afterwards. Worth it for a landmark still drawn at the edge of the "
        "view; not for a mailbox that disappears at 80 m.",
        unit="m",
        subset_of="imposters",
    ),
    Setting(
        key="lod_detail_scale",
        default=6.0,
        kind="float",
        label="Cull distance divisor",
        blurb="Divide every shape's CS2 cull distance by this. 1 reproduces what the game itself "
        "draws.",
        help="How many times closer than CS2 an object is culled. 1 = exactly what the game draws",
        detail="Your city draws a typical small object to about a kilometre and a typical "
        "building to several. Faithful, and far more than BeamNG will carry. This divides "
        "every shape's own distance by the same amount; 1 draws exactly what the game "
        "draws.",
        unit="×",
    ),
    Setting(
        key="lod_detail_scale_prop",
        default=0.0,
        kind="float",
        label="Cull distance divisor, props",
        blurb="The cull divisor applied to props alone. 0 makes props share the global divisor.",
        help="The same multiplier as lod_detail_scale but for PROPS only; 0 = use the global one",
        detail="The same divisor for small objects alone; 0 makes them share the global one. It "
        "is separate because your city draws buildings roughly twelve times further than "
        "props, and one divisor keeps that ratio while ruining what it means: a window "
        "frame is invisible at the distance the game drops it and plainly missing at a "
        "tenth of that, while the building it belongs to is still there. Most props are "
        "attached to something, so the two have to be given up at different rates.",
        unit="×",
    ),
    Setting(
        key="lod_switch_error_px",
        default=8.0,
        kind="float",
        label="LOD switch error budget",
        blurb="Screen-space error in pixels a generated LOD may show as it takes over. Lower is "
        "truer.",
        help="How many pixels of surface error a generated LOD may show when it takes over. "
        "Smaller = truer, heavier",
        detail="How wrong a reduced version of a shape may look, in pixels on screen, at the "
        "moment it takes over from the one before it. Each reduced version is measured "
        "against the original, so one that is identical keeps its place to the horizon and "
        "one that is a metre out waits until a metre stops showing. Smaller is truer and "
        "costs drawn triangles — and it costs them downtown, where the card is already "
        "working hardest. Plants are exempt and keep a fixed ladder: the measurement is a "
        "surface distance and a canopy is a cloud of leaves, so it asks for full detail "
        "twice as far out for a problem nobody reported.",
        unit="px",
    ),
    # A8/P2: metres of measured surface error at which LOD 1 replaces LOD 0 outright. 0 = keep
    # every LOD 0. See `stages.shapes.LOD0_MAX_ERROR_DEFAULT` for the measurement behind 0.10.
    Setting(
        key="lod0_max_error",
        default=0.1,
        kind="float",
        label="LOD 0 promotion threshold",
        blurb="Drop LOD 0 where LOD 1's p95 surface deviation is within this many metres. 0 keeps "
        "every one.",
        help="Drop LOD0 where LOD1 is within this many metres of it",
        unit="m",
    ),
    Setting(
        key="lod0_min_triangles",
        default=1000,
        kind="int",
        label="LOD 0 promotion floor",
        blurb="Never promote where that would leave the shape shipping fewer triangles than this.",
        help="...but never where that leaves the shape shipping fewer triangles than this. 0 = no "
        "floor",
        detail="A floor under the promotion: never drop the full-detail version if that would "
        "leave the shape with fewer triangles than this. The measurement the promotion "
        "uses is how far the surface moved, and that cannot tell that a reduction has "
        "opened an object up — flattening a sign's post moves its surface a few "
        "millimetres and leaves a panel floating with no post. A floor protects the thin "
        "things without giving back the saving on the heavy ones. 0 removes it.",
        unit="triangles",
    ),
    Setting(
        key="forest_min_items",
        default=10,
        kind="int",
        label="Forest type merge threshold",
        blurb="A forest type holding fewer items than this is merged into its prefab's untinted "
        "type.",
        help="Merge a forest type holding fewer items than this into its prefab's plain type. 0 "
        "keeps every type",
        detail="A kind of plant with fewer than this many placed is folded in with its plain, "
        "untinted version instead of being its own kind. BeamNG loads a kind whole whether "
        "it has one member or ten thousand, and a city ends up with thousands of kinds "
        "holding a handful each — more than half of them, for well under a percent of the "
        "planting. 0 keeps every one.",
        unit="items",
    ),
    Setting(
        key="far_detail_scale",
        default=1.0,
        kind="float",
        label="Silhouette reach",
        blurb="How far a building's far silhouette is drawn, as a multiple of CS2's own reach.",
        help="A building's far silhouette runs to CS2's own cull distance times this; 0 disables it",
        detail="Past its detailed levels a building is drawn as a closed, blocky outline in its "
        "own colours, out to the distance the game itself draws it (times this). It is what "
        "lets you see the skyline from the other side of the map for a few triangles per "
        "building. 0 draws nothing past the detailed levels, as before.",
        unit="×",
    ),
    Setting(
        key="lod_detail_scale_tree",
        default=10.0,
        kind="float",
        label="Tree detail reach",
        blurb="How many times nearer than the game a tree turns into its flat billboard.",
        help="Trees' own cull/billboard scale (like lod_detail_scale_prop); 0 = the global scale",
        detail="A tree is its full mesh up close and a flat picture of itself beyond. The game "
        "keeps the mesh out to about a kilometre; that is sixteen million triangles of "
        "canopy in view on a large city. At 10 the picture takes over around a hundred to "
        "two hundred metres, which is where it stops being noticeable.",
        unit="×",
    ),
    Setting(
        key="forest_rare_type_min",
        default=0,
        kind="int",
        label="Rarest planting floor",
        blurb="A kind of tree or prop placed fewer times than this is left out. 0 keeps all.",
        help="Drop a forest type placed fewer than this many times (big one-offs stay); 0 = keep "
        "every kind",
        detail="Every kind of planting is held in memory whole, for one member or ten "
        "thousand, and a city carries a long tail of kinds placed once or twice. At 3 only "
        "that tail is left out; anything landmark-sized stays whatever its count. Higher "
        "floors were tried and thinned prop families visibly.",
        unit="items",
    ),
    Setting(
        key="forest_rare_keep_radius",
        default=5.0,
        kind="float",
        label="Rare planting keep size",
        blurb="A rare kind at least this big (bounding radius) is kept whatever the floor says.",
        help="A rare forest type with a shape radius at or above this is never dropped",
        detail="The floor exists for the small one-off clutter. A one-off the size of a "
        "harbour crane or a floodlight tower is a landmark, and dropping it empties the "
        "skyline. Anything this big stays.",
        unit="m",
        subset_of="forest_rare_type_min",
    ),
    Setting(
        key="lod_detail_scale_large",
        default=0.0,
        kind="float",
        label="Big building detail reach",
        blurb="Big buildings' own version of the detail scale; 0 uses the buildings' scale.",
        help="Detail scale for buildings at or over lod_large_radius_m; 0 = lod_detail_scale",
        detail="The game keeps a big building sharp for kilometres, in proportion to its size, "
        "and at the scale that suits the houses a tower or a mall keeps its full mesh for "
        "too long. This is the same kind of number as the detail scale, applied to the big "
        "ones only. 4.5 is two thirds of the distance the houses get at 3.",
        unit="×",
    ),
    Setting(
        key="lod_large_radius_m",
        default=30.0,
        kind="float",
        label="Big building size",
        blurb="A building whose bounding radius is at least this counts as big.",
        help="Radius from which a building takes lod_detail_scale_large",
        detail="Half the diagonal of the building's box. Thirty metres is a mid-rise block; a "
        "detached house is six to ten.",
        unit="m",
    ),
    Setting(
        key="far_ring_metres",
        default=2500.0,
        kind="float",
        label="Skyline hand-over",
        blurb="Beyond this distance the skyline is one merged outline per block; 0 = one per building.",
        help="Distance at which buildings hand over to a merged per-cell silhouette; 0 = "
        "per-building silhouettes only",
        detail="Past its detailed levels a building is drawn as a blocky outline in its own "
        "colours out to the distance the game itself draws it. Beyond this distance those "
        "outlines are merged into one mesh per half-kilometre block — a few dozen draw calls "
        "for the whole skyline instead of one per building. Towers whose detail reaches "
        "further than this keep their own outline.",
        unit="m",
    ),
    Setting(
        key="authored_lod1_drop",
        default=False,
        kind="bool",
        label="Drop the game's LOD 1",
        blurb="Ship a building's LOD 0 and LOD 2 only; LOD 2 takes over where LOD 1 did.",
        help="Do not ship the game's LOD 1 for buildings that also have a LOD 2; frees ~1 GB",
        detail="BeamNG keeps every level of every shape in memory whether or not anything is "
        "near it, and the game's middle level is close to half the size of the full one. "
        "Dropping it hands its band — roughly fifty to a hundred and fifty metres — to the "
        "coarse level, which is visible up close in that band, and takes about a gigabyte "
        "off what the card has to hold.",
    ),
    Setting(
        key="authored_lod0_max_tris",
        default=0,
        kind="int",
        label="Full detail up to",
        blurb="Buildings over this many triangles show the game's second level up close. 0 = all full.",
        help="A building with more than this many triangles ships the game's LOD 1 as its "
        "nearest level; 0 = keep LOD 0 for all",
        detail="BeamNG holds every building's full mesh in memory for the whole session, and the "
        "few hundred heaviest ones — stadiums, hospitals, towers of a hundred thousand "
        "triangles and more — are most of what an 8 GB card runs out of. Above this count a "
        "building is drawn with the game's own second level instead, which has under half the "
        "triangles and is what the game itself shows from a street away. At 20,000 that is "
        "about 190 buildings and 600 MB less on the card; at 5,000 about 600 buildings and "
        "850 MB.",
        unit="triangles",
    ),
    Setting(
        key="area_conform_cm",
        default=1.0,
        kind="float",
        label="Paint fit",
        blurb="How closely painted surfaces follow the ground, in centimetres. Bigger is fewer "
        "triangles.",
        help="Painted surfaces are split until the ground is within this many cm; 1 = exact",
        detail="A painted surface is cut into pieces until the ground stands no higher than "
        "this above any piece. At one centimetre a city's paint is six million triangles; "
        "at five it is a fraction of that and the paint dips into a slope now and then, "
        "which shows as a faint patch where the ground shows through.",
        unit="cm",
    ),
    Setting(
        key="shadow_min_radius",
        default=3.0,
        kind="float",
        label="Smallest shadow caster",
        blurb="A placed object smaller than this (bounding radius, metres) casts no shadow.",
        help="TSStatics with a shape radius under this ship castShadows:false; 0 lets everything "
        "cast",
        detail="A bin or a bollard's shadow is a few pixels and its shadow-map pass is not. "
        "CS2's own renderer stops small and sparse things' shadows before their meshes "
        "(its per-level shadow LOD); BeamNG has only the switch, so this is the switch.",
        unit="m",
    ),
    Setting(
        key="lane_max_tris",
        default=0,
        kind="int",
        label="Baked lane furniture budget",
        blurb="Triangle ceiling for the sheared fence and cable tiles, spent in CS2's own cull "
        "order.",
        help="Triangle budget for baked lane furniture, 0 = no budget. What goes first is what "
        "CS2 culls first",
        detail="A ceiling on the fences and cables that have to be built into the road tiles "
        "rather than placed as copies. What goes first is what your city itself gives up "
        "first, so the budget is spent on the furniture the game draws furthest away. 0 is "
        "no ceiling.",
        unit="triangles",
    ),
    Setting(
        key="paint_factor_max_error",
        default=4,
        kind="int",
        label="colorSet factor tolerance",
        blurb="How far a colorSet variant may drift, in 8-bit channels, before it gets its own "
        "texture.",
        help="How far a paint variant may drift, in 8-bit channels, before it gets a texture of "
        "its own",
        detail="How far a recoloured copy may drift from the real thing before it is given a "
        "texture of its own rather than a single colour multiplied over the original. "
        "Where a surface is tinted all over, one colour reproduces it exactly and costs no "
        "texture at all; where the tint picks out three different parts, it cannot. About "
        "a third of a city's recolourings come out free at this setting. 0 gives every one "
        "its own texture.",
        unit="of 255",
    ),
    Setting(
        key="foliage_road_clearance",
        default=2.5,
        kind="float",
        label="Lane clearance for trees",
        blurb="Declared deviation: drop a tree whose trunk is within this of a drivable lane "
        "centre.",
        help="Declared deviation: drop a tree standing this close to a lane centre so the city "
        "can be driven. 0 = off",
        detail="Removes a tree whose trunk stands within this distance of the middle of a "
        "drivable lane, so you can actually drive the city. Your city has no such rule — "
        "it clears the ground when a road is BUILT and lives with whatever grew back — so "
        "this is playability rather than accuracy, and the build report counts what it "
        "removed. 2.5 m clears the carriageway rather than the car: a lane is about 3.5 m "
        "wide and a trunk can be half a metre thick. It goes by the trunk, not the "
        "branches, so a canopy can still overhang a road. Trees only — you drive through a "
        "bush without noticing. 0 turns it off.",
        unit="m",
    ),
    Setting(
        key="forest_shadows",
        default=True,
        kind="bool",
        label="Forest shadow casting",
        blurb="Forest items cast shadows. One of the heaviest per-frame costs a dense canopy "
        "carries.",
        help="Forest items cast shadows",
    ),
    Setting(
        key="tree_wind",
        default=False,
        kind="bool",
        label="Synthesised wind vertex colours",
        blurb="Write wind weights into tree vertex colours so BeamNG animates them. CS2 records "
        "no wind.",
        help="Synthesise wind vertex colours for trees",
    ),
    Setting(
        key="tile_size",
        default=256,
        kind="int",
        label="World tile size",
        blurb="Edge length of the merged road, marking, area and skirt tiles.",
        help="World tile size for roads/markings/areas (metres)",
        unit="m",
    ),
    Setting(
        key="terrain_holes",
        default="contain",
        kind="choice:contain|majority|none",
        label="Terrain hole rule",
        blurb="Which clip-volume cells are punched out of the heightfield as terrain holes.",
        help="Which clip cells become terrain holes",
    ),
    # NOT A PARITY SETTING - see stages/terrain.HEIGHT_SIGMA and audit §48.3. 1.0 is the value
    # BeamNG's own blend arithmetic derives; above that it is a deliberate deviation that trades
    # fidelity for hiding an artefact the engine's texel-sized constants cause on our coarse grid.
    Setting(
        key="terrain_blend_sharpen",
        default=1.0,
        kind="float",
        label="Ground blend interlock",
        blurb="Multiplier on BeamNG's derived layer-blend sharpness. 1 is faithful, higher is "
        "neater.",
        help="Ground-boundary interlock, x the derived amount. 1 = faithful; 2-3 hides the blocky "
        "material edges",
        detail="How sharply one ground meets another. 1 is what the game's own blend works out. "
        "Higher hides the blocky edges our coarser ground grid can leave, at the price of "
        "a harder join than the game would draw.",
        unit="×",
    ),
    # Needs BeamNG on ADVANCED lighting. Basic Lighting has no g-buffer prepass and shades no
    # point or spot light at all, so on that profile every placed light is pure cost (audit §82);
    # `validate` reads the user's own `game-settings.json` and says so.
    Setting(
        key="lights_max",
        default=25000,
        kind="int",
        label="Dynamic light ceiling",
        blurb="Ceiling on punctual lights placed, ranked by BeamNG's own importance metric.",
        help="Maximum dynamic lights (needs Advanced Lighting in BeamNG)",
        unit="lights",
        needs="lights",
    ),
    Setting(
        key="navgraph_max_width",
        default=0.0,
        kind="float",
        label="Navgraph node radius clamp",
        blurb="Clamp a navgraph node's road width. 0 uses the width CS2's own lane offsets give.",
        help="Clamp a navgraph node's road width, in metres (0 = the width CS2's own lane offsets "
        "give)",
        detail="How wide the game may consider a road when it joins one road to another. 0 uses "
        "the width your own lanes give. It exists because BeamNG joins two road points "
        "when they are closer than the wider of the two, and that test includes height — "
        "so a very wide road reaches downwards and can weld a bridge deck into the street "
        "underneath it. In a typical city nothing does at 0.",
        unit="m",
    ),
)
