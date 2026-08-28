"""Advanced: the machine the level is for, and how the build runs.

Settings about the build rather than about the level. The machine block feeds the residency
budget the cost estimate and the validator both read; the process block decides what the build
does on the way through and what it leaves behind.
"""

from .model import Setting

BUILD_SETTINGS = (
    Setting(
        key="gpu_vram_mib",
        default=0,
        kind="int",
        label="Dedicated VRAM",
        blurb="How much memory the card this level is built for has. 0 reads it from your own "
        "BeamNG log.",
        help="Dedicated VRAM of the card the level is for, in MiB (0 = read it from your own "
        "beamng.log)",
        detail="How much memory the graphics card this level is built for has. 0 reads it from "
        "your own BeamNG log. It changes nothing about the level; it is what the cost "
        "meter measures against, so set it if you are building for a machine that is not "
        "this one.",
        unit="MiB",
    ),
    Setting(
        key="render_width",
        default=0,
        kind="int",
        label="Render width",
        blurb="Horizontal render resolution this level is built for. 0 reads it from your own "
        "BeamNG log.",
        help="Render width the level is for (0 = read it from beamng.log)",
        detail="The width the level is built for. 0 reads it from your own BeamNG log. Every "
        "pixel above 1080p takes memory back off the level for the game's own render "
        "targets, which is why the cost meter asks.",
        unit="px",
    ),
    Setting(
        key="render_height",
        default=0,
        kind="int",
        label="Render height",
        blurb="Vertical render resolution this level is built for. 0 reads it from your own "
        "BeamNG log.",
        help="Render height the level is FOR (0 = read it from beamng.log, fall back to 1080)",
        unit="px",
    ),
    Setting(
        key="jobs",
        default=0,
        kind="int",
        label="Worker processes",
        blurb="Parallel worker processes for the multiprocess stages. 0 leaves one core free.",
        help="Worker processes for parallel stages (0 = CPU count - 1)",
        unit="processes",
    ),
    Setting(
        key="cook",
        default=True,
        kind="bool",
        label="Cook shapes after the build",
        blurb="Run BeamNG's headless mesh compiler over the level so the first load does not have "
        "to.",
        help="Compile the shapes with BeamNG's own compiler after the build, so the first load is "
        "fast",
        detail="Compiles the level's shapes into the form BeamNG loads, at the end of the build "
        "instead of the first time you play. It adds minutes here and saves more than that "
        "later. Skipped automatically if BeamNG is running or cannot be found.",
    ),
    Setting(
        key="rebuild",
        default=False,
        kind="bool",
        label="Force a full rebuild",
        blurb="Ignore the recorded stage state and run every stage from scratch.",
        help="Rebuild every stage from scratch, ignoring what the last build recorded",
        detail="Runs every stage again from nothing instead of skipping the ones whose inputs "
        "have not changed. Skipping is what makes a second build of the same city take "
        "seconds rather than a quarter of an hour, so leave this off unless you have "
        "reason to distrust what is on disk. `cs2beamng build --force` is the same thing.",
    ),
    Setting(
        key="keep_partial",
        default=False,
        kind="bool",
        label="Keep a cancelled level",
        blurb="Leave a partially built level on disk after a cancel instead of removing it.",
        help="Keep a partially built level after cancel",
    ),
    # A8/P7. `sweep_temp` is free and on. `keep_dae` is ON BY DEFAULT and that is deliberate:
    # dropping the sources saves 5.4 GB of DISK and no RAM, and the NEXT build has to rewrite
    # every `.dae` it deleted, which makes the cook recompile all of them (~9 min on the full
    # city). Turn it off for a build you are shipping, not for one you are iterating on.
    Setting(
        key="sweep_temp",
        default=True,
        kind="bool",
        label="Sweep BeamNG's temp folder",
        blurb="Delete cooked shapes in BeamNG's temp folder that this level no longer references.",
        help="Delete cooked shapes in BeamNG's temp/ that this level no longer has",
    ),
    Setting(
        key="keep_dae",
        default=True,
        kind="bool",
        label="Keep the COLLADA sources",
        blurb="The .dae sources stay beside the cooked .cdae. Off saves disk and forces a full "
        "recook.",
        help="Keep .dae sources beside the cooked .cdae (off saves ~5 GB, forces a full recook "
        "next build)",
    ),
    Setting(
        key="ui_browser",
        default="window",
        kind="choice:window|chrome|edge|firefox|default",
        label="UI host",
        blurb="Where this window opens: a native window, a named browser, or the system default.",
        help="Where the UI opens: a native window, a named browser, or the system default",
    ),
)
