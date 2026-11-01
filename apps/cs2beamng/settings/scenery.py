"""Advanced: detail and performance, continued.

The measured tuning numbers past the LOD ladder `tuning` declares: painted surfaces, shadows,
baked lane furniture and paint variants, trees, tiles, the terrain, lights and the navgraph. The
same block on screen, and the same rule - each keeps its jargon, and the argument for its default
is in the setting's `detail`.
"""

from .model import Setting

SCENERY_SETTINGS = (
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
        key="forest_shadow_null_px",
        default=40,
        kind="int",
        label="Forest shadow far stop",
        blurb="The pixel size at which a forest item stops casting — vanilla ships 40. 0 = no far "
        "stop.",
        help="Stop a forest item's shadow at this pixel size (vanilla uses 40); 0 = never",
        detail="Shadow casting is one of the most expensive things a scene does, and with the "
        "shadows switch on every one of half a million forest items was in the shadow "
        "pass at any distance — no far stop at all. This gives a tree's shadow the same "
        "kind of vanishing point its mesh has: near trees keep theirs, the far tail "
        "leaves the pass. 40 is the number BeamNG's own levels use. Larger stops "
        "shadows sooner and buys more; 0 restores no far stop.",
        unit="px",
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
