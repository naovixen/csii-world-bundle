"""The Textures section: how sharp the level looks.

The three texture ceilings, the terrain raster settings, and the recolouring (paint) knobs.
"""

from .model import Setting

TEXTURE_SETTINGS = (
    Setting(
        key="texture_max",
        default=512,
        kind="choice:256|512|1024|2048|0",
        label="Texture detail",
        blurb="The largest a colour picture may be, in pixels. The main cost on your card.",
        help="Colour texture edge cap in pixels (0 = full size). Also caps the emissive and "
        "opacity maps",
        detail="The largest a colour picture may be. It is the main cost your card carries, and "
        "the cost meter moves most when this does. 512 is in line with what BeamNG's own "
        "biggest level ships, where most colour maps are 512 or smaller. It also caps the "
        "glow and cut-out maps, which are the same picture's own resolution.",
    ),
    Setting(
        key="texture_max_normal",
        default=512,
        kind="choice:256|512|1024|2048|0",
        label="Surface bumpiness detail",
        blurb="How finely bricks, planks and grooves are described. Lower is flatter and lighter.",
        help="How large a surface-bumpiness picture may be, in pixels (0 = as large as it came)",
        detail="The largest a surface-bumpiness picture may be. This is the tier that carries "
        "brick, plank and groove relief, and it is the one worth spending on after the "
        "colour map; BeamNG's own levels run these between 512 and 1024.",
    ),
    Setting(
        key="texture_max_data",
        default=256,
        kind="choice:128|256|512|1024|0",
        label="Shine and roughness detail",
        blurb="How finely wet, matt and metal surfaces are described. Rarely needs to be large.",
        help="Edge cap for data maps in pixels: roughness, metallic and the CS2 colour mask",
        detail="The largest a picture describing shine, roughness or metal may be. These carry "
        "almost no fine detail — a roughness gradient is smooth by nature — and most of "
        "the ones in BeamNG's own biggest level are 256 or smaller. This is the cheapest "
        "place to save memory.",
    ),
    Setting(
        key="texture_dds",
        default=True,
        kind="bool",
        label="Prepare textures during the build",
        blurb="Do the compression now instead of the first time you play. Leave this on.",
        help="Compress the textures during the build, so the game never has to on the first load",
        detail="Compresses the textures during the build, in the format the game would have used. "
        "Without it the game compresses them itself the first time you load the level, one "
        "at a time, on the main thread — which is a minute or more of stuttering on a big "
        "city. Leave it on.",
    ),
    Setting(
        key="paint_kinds",
        default="tree,prop",
        kind="str",
        label="What gets recoloured",
        blurb="Which sorts of object take their colour from the city. The rest draw an untinted "
        "one.",
        help="Which sorts of object take their colour from your city; the rest draw an untinted one",
        detail="Your city paints its own objects — the same bench in four colours, the same tree "
        "in a dozen greens. This chooses which sorts get that treatment. A sort left out "
        "draws the untinted original, and because the game authors those washed out on "
        "purpose, leaving one out is visibly wrong rather than subtle. It costs build time "
        "and textures.",
        control="categories",
        choice_set=[
            ["tree", "Trees and plants"],
            ["prop", "Props"],
            ["fence", "Fences"],
            ["building", "Buildings"],
            ["decal", "Road markings"],
            ["poi", "Landmarks"],
        ],
    ),
    Setting(
        key="paint_variants_tree",
        default=2,
        kind="int",
        label="Shades per kind of plant",
        blurb="How many shades each kind of plant comes in. More is prettier and heavier.",
        help="How many shades each kind of plant comes in. Every shade is another copy to draw",
        detail="How many shades each kind of plant comes in. Every extra shade is another copy of "
        "the shape and another set of textures, and it is the single biggest source of "
        "both in a finished level — the shades alone can be most of its materials. 2 keeps "
        "the light and the dark half of a plant's range apart, which is what you can "
        "actually see at distance.",
        unit="variants",
        subset_of="paint_kinds",
    ),
    Setting(
        key="paint_variants_prop",
        default=1,
        kind="int",
        label="Shades per kind of prop",
        blurb="How many shades each kind of small object comes in.",
        help="How many shades each kind of small object comes in. One is its average colour",
        detail="How many shades each kind of small object comes in. 1 is its average colour, "
        "which is usually enough: props are small, seen in bulk and at distance, and their "
        "colours vary less than the planting's.",
        unit="variants",
        subset_of="paint_kinds",
    ),
    Setting(
        key="texture_max_variant",
        default=256,
        kind="choice:128|256|512|1024|0",
        label="Recoloured texture detail",
        blurb="How large the recoloured copies may be, in pixels.",
        help="Colour cap for a paint variant's tinted albedo, separately from the main colour cap",
        detail="The largest a recoloured copy may be, separately from the colour cap. A "
        "recoloured copy is the same picture with a colour multiplied through it and it is "
        "only ever seen on planting and props in bulk, so it can be smaller than the "
        "buildings and roads you look at up close.",
        subset_of="paint_kinds",
    ),
    Setting(
        key="terrain_brightness",
        default=1.0,
        kind="float",
        label="Ground brightness",
        blurb="Lighten or darken the grass, sand and dirt. One leaves them as your city has them.",
        help="Lighten or darken the ground. One leaves it as your city has it",
        detail="Lightens or darkens the ground. 1 is not neutral — it is the amount that lands "
        "your city's grounds where BeamNG's lighting expects them, worked out against the "
        "grounds BeamNG's own levels ship. Anything else is your eye rather than either "
        "game's.",
        unit="×",
    ),
    Setting(
        key="terrain_upsample",
        default=2,
        kind="choice:1|2",
        label="Ground detail",
        blurb="How finely the ground is shaped. Higher is sharper underfoot and costs memory.",
        help="Terrain grid: 2 = 8192 at 1.75 m (the game's own near-camera detail); 1 = 4096 at "
        "3.5 m, lighter",
        detail="How finely the ground is shaped. Fine is the same detail the game itself uses "
        "near the camera and is worth having if the memory is there; standard is half as "
        "fine in each direction and a quarter of the cost. There is deliberately no finer "
        "option: the next step up is the largest texture the graphics API allows, four "
        "times the memory, and finer than any level BeamNG ships.",
    ),
)
