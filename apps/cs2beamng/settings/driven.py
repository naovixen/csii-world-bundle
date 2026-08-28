"""The settings something else on screen now sets.

Each of these keeps its `--set` name and its entry in the reference page; what it loses is a box
nobody could answer without already knowing the answer. `driven_by` is the sentence shown in
their place; `values.DRIVES` is the rule that does the driving, so the two cannot disagree.
"""

from .model import Setting

DRIVEN_SETTINGS = (
    Setting(
        key="trees",
        default=True,
        kind="bool",
        label="Trees on or off",
        blurb="Whether any planting is placed at all.",
        help="Trees (forest)",
        driven_by="the Trees slider, which turns them off at zero",
    ),
    Setting(
        key="props",
        default=True,
        kind="bool",
        label="Props on or off",
        blurb="Whether any small street objects are placed at all.",
        help="Props, fences, points of interest",
        driven_by="the Props slider, which turns them off at zero",
    ),
    Setting(
        key="terrain_cut",
        default=True,
        kind="bool",
        label="Cut the ground at tunnels",
        blurb="Open the ground at trenches and tunnel mouths and drop the cut mesh into the hole.",
        help="Cut the ground open at trenches and tunnel portals and drop the exact cut mesh into "
        "the hole",
        detail="Where a road runs in a trench or into a tunnel mouth, the ground is opened up and "
        "a piece of ground cut to fit is dropped into the hole. Without it the terrain "
        "closes over the trench and the road disappears into a hill.",
        driven_by="the Roads and rails switch",
    ),
    Setting(
        key="quality_preset",
        default="high",
        kind="choice:ultra|high|performance|medium|low|custom",
        label="Quality preset",
        blurb="The named quality level the settings were last set from.",
        help="Quality preset last applied",
        driven_by="the preset buttons above the settings",
    ),
    # Replaces the retired `ui_advanced`: the sidebar remembers which section you were in, not
    # whether a second tier of settings was unfolded. There is no second tier any more. Its
    # `kind` and `default` are filled in from SECTIONS by `catalog.build_settings`, so a new
    # section needs no edit here.
    Setting(
        key="ui_section",
        default="world",
        kind="choice:",
        label="Last settings section",
        blurb="Which part of the settings you had open. Remembered between sessions.",
        help="The settings section the UI opens on (remembered between sessions)",
        driven_by="the sidebar, which remembers where you were",
    ),
)
