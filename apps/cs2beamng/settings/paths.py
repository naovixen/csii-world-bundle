"""The basics strip: where the bundle is, where the level goes, and what it is called.

The handful of things a person must actually supply, shown above the settings and always
visible. `region` is NOT here: it is a decision about the level, not a path the app needs, so it
heads the World section where the rest of "what gets built" lives.
"""

from .model import Setting

BASICS_SETTINGS = (
    Setting(
        key="bundle",
        default=None,
        kind="path",
        label="Bundle folder",
        blurb="The folder Cities: Skylines II wrote when you ripped your city.",
        help="Bundle directory. The app finds the ripper mod's own export folder by itself; this "
        "overrides it",
    ),
    Setting(
        key="output",
        default=None,
        kind="path",
        label="Levels folder",
        blurb="Where BeamNG keeps its levels. Your finished level is written in here.",
        help="Levels directory to write into (default: the BeamNG userfolder levels/)",
    ),
    Setting(
        key="level_name",
        default="cs2_city",
        kind="str",
        label="Folder name",
        blurb="The name of the folder your level is written into. Letters, digits and dashes.",
        help="Level folder name (lowercase, underscores)",
    ),
    Setting(
        key="level_title",
        default="CS2 City",
        kind="str",
        label="Level title",
        blurb="The name shown in the level list once it is built.",
        help="Level title shown in BeamNG",
    ),
    Setting(
        key="beamng_exe",
        default=None,
        kind="path",
        label="BeamNG",
        blurb="Where the game is installed. Filled in for you when it was found on this machine.",
        help="BeamNG executable (auto-detected when empty)",
        needs="cook",
    ),
)
