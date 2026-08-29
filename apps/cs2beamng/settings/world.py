"""The World section: what you will see in the level.

The layers the build puts in the level, the two density sliders, and the two settings that
decide how far you can see and what time of day it is.
"""

from .model import Setting

WORLD_SETTINGS = (
    Setting(
        key="region",
        default=None,
        kind="str",
        label="Area",
        blurb="Build the whole city, or one square patch of it. A smaller piece builds far faster.",
        help="Build one square patch of the city instead of all of it: X Z half-width, in bundle "
        "metres",
        control="region",
    ),
    Setting(
        key="buildings",
        default=True,
        kind="bool",
        label="Buildings",
        blurb="Houses, offices, factories and everything else your city has built.",
        help="Buildings",
    ),
    Setting(
        key="networks",
        default=True,
        kind="bool",
        label="Roads and rails",
        blurb="Road, rail and path surfaces, and the ground opened up at trenches and tunnels.",
        help="Roads, rails and other network meshes",
    ),
    Setting(
        key="markings",
        default=True,
        kind="bool",
        label="Road markings",
        blurb="Lane lines, arrows, crossings and the rest of the paint on the tarmac.",
        help="Road markings and painted decals (pre-draped)",
    ),
    Setting(
        key="lane_furniture",
        default=True,
        kind="bool",
        label="Roadside furniture",
        blurb="Fences, hedges and overhead cables that run alongside the roads.",
        help="Roadside furniture: fences, hedges, cables",
    ),
    Setting(
        key="lane_furniture_categories",
        default="fence,cable,lane",
        kind="str",
        label="Kinds to include",
        blurb="Which sorts of roadside detail get built. Clearing them all leaves none of it.",
        help="Furniture categories to include",
        control="categories",
        choice_set=[
            ["fence", "Fences and hedges"],
            ["cable", "Overhead cables"],
            ["lane", "Kerbside details"],
        ],
        subset_of="lane_furniture",
    ),
    Setting(
        key="areas",
        default=True,
        kind="bool",
        label="Painted surfaces",
        blurb="Car parks, plazas, footpaths and the other flat coloured ground.",
        help="Painted surfaces (parking lots, paths)",
    ),
    Setting(
        key="skirts",
        default=True,
        kind="bool",
        label="Building foundations",
        blurb="The strip of wall that fills the gap where a plot meets sloping ground.",
        help="Building foundation skirts",
    ),
    Setting(
        key="lights",
        default=False,
        kind="bool",
        label="Street and building lights",
        blurb="Lamps, floodlights and lit signs glow after dark. Heavy on your graphics card.",
        help="Street and building lights (dynamic lights are expensive)",
    ),
    Setting(
        key="water",
        default=True,
        kind="bool",
        label="Water",
        blurb="Rivers, lakes and the sea.",
        help="Water planes/blocks",
    ),
    Setting(
        key="groundcover",
        default=False,
        kind="bool",
        label="Grass",
        blurb="Blades of grass over the ground. Dressing for the eye; your city has none.",
        help="Billboard grass over the ground paint. BeamNG dressing, not CS2 fidelity - CS2 has "
        "no grass system",
        detail="Blades of grass standing over the ground, for a camera at driving height. Your "
        "city has nothing of the kind — Cities: Skylines II has no grass system at all — "
        "so this is not reproducing anything, it is dressing, and it is off by default for "
        "that reason.",
    ),
    Setting(
        key="foliage_density",
        default=100,
        kind="int",
        label="Trees",
        blurb="How much of the planting to keep. At zero none of it is placed at all.",
        help="Percent of trees kept (deterministic subset)",
        control="percent",
        range=(0, 100, 1, " %"),
        zero_label="off",
    ),
    Setting(
        key="prop_density",
        default=100,
        kind="int",
        label="Props and fences",
        blurb="How many of the small street objects to keep. At zero none are placed.",
        help="Percent of props kept",
        control="percent",
        range=(0, 100, 1, " %"),
        zero_label="off",
    ),
    Setting(
        key="view_distance",
        default=8000,
        kind="int",
        label="How far you can see",
        blurb="The distance in metres past which nothing is drawn. The biggest lever on speed.",
        help="The distance in metres past which nothing is drawn at all",
        detail="The distance past which nothing is drawn at all. Every shape has its own distance "
        "worked out from what your city does with it; this is the ceiling over all of "
        "them, which makes it the one number that moves the whole cost at once.",
        control="distance",
        range=(500, 8000, 100, " m"),
    ),
    Setting(
        key="tod_hour",
        default=-1.0,
        kind="float",
        label="Time of day",
        blurb="What hour the sun sits at, either your city's own or one you pick.",
        help="Time of day in hours 0-24 for the level's sun (-1 = the in-game clock at rip time)",
        control="clock",
    ),
)
