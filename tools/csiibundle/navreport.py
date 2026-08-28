"""Navgraph reporting — one line per approximation a build made, with its count.

CLAUDE.md: an undeclared approximation is indistinguishable from a bug once its artefact
appears. `build()` collects the numbers; this module turns them into the lines a consumer is
expected to print, in the order the build made the decisions.
"""


def _lane_notes(stats):
    """What the lane reading and the junction match excluded, and by whose rule."""
    notes = []
    if stats.get("sideConnections"):
        n = stats["sideConnections"]
        notes.append(
            f"navgraph: {n:,} SideConnection driveway stubs excluded from the "
            f"carriageway - CS2's own flag, not a length threshold"
        )
    if stats.get("dividedCarriagewaysAveraged"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['dividedCarriagewaysAveraged']:,} segments "
            f"carry lanes in both directions and become ONE centerline down the middle "
            f"of them; a median strip inside a single segment is not represented"
        )
    if stats.get("endsWithNoJunction"):
        notes.append(
            f"navgraph: {stats['endsWithNoJunction']:,} segment ends weld to no turning "
            f"movement - a dead end, or a join not found"
        )
    if stats.get("connectorsUnmatched"):
        notes.append(
            f"navgraph: {stats['connectorsUnmatched']:,} node-owned car lanes touch a "
            f"drivable segment at one end only (a movement into a lot or a path) and "
            f"are not emitted"
        )
    return notes


def _width_notes(stats, max_width):
    """What the width rules understated, each a declared approximation."""
    notes = []
    if stats.get("widthClamped"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['widthClamped']:,} segments had their "
            f"carriageway width clamped to {max_width} m, so their node radius understates "
            f"the real road"
        )
    if stats.get("speedInferred"):
        notes.append(
            f"navgraph: {stats['speedInferred']:,} segments ship no attrs.limit; the "
            f"consumer is left to auto-calculate a speed for them"
        )
    if stats.get("widthClearanceClamped"):
        notes.append(
            f"navgraph: APPROXIMATION - {stats['widthClearanceClamped']:,} segments had "
            f"their width capped so their node radius stops short of a road stacked "
            f"over or under them (worst {stats.get('widthClearanceWorst', 0)} m); their "
            f"width is understated by that much, and without it a consumer that asks "
            f"'is the car inside this radius' answers with the deck overhead"
        )
    return notes


def _graph_notes(stats):
    """What the simple-graph rule removed, and what the emitted graph is."""
    notes = []
    if stats.get("selfLoopsDropped"):
        notes.append(
            f"navgraph: {stats['selfLoopsDropped']:,} connectors/joints dropped as "
            f"SELF-LOOPS - both ends land on one node once coincident terminal nodes "
            f"merge, so they join nothing (map.lua:459 crashed on the 3-cycle they make)"
        )
    if stats.get("duplicateEdgesDropped"):
        notes.append(
            f"navgraph: {stats['duplicateEdgesDropped']:,} connectors/joints dropped as "
            f"DUPLICATE edges - a second road between one pair of nodes is not a second "
            f"edge; the one carrying the turn's geometry is kept"
        )
    if stats.get("joints"):
        notes.append(
            f"navgraph: {stats['joints']:,} joints where two segments abut with no "
            f"turning movement between them (an asset change or a bridge abutment, not "
            f"a junction)"
        )
    votes = stats.get("forwardLanesRight", 0) + stats.get("forwardLanesLeft", 0)
    if votes:
        side = "LEFT" if stats.get("leftHandTraffic") else "RIGHT"
        notes.append(
            f"navgraph: traffic drives on the {side} - MEASURED, "
            f"{stats['forwardLanesRight']:,} two-way segments put the with-flow lanes "
            f"right and {stats['forwardLanesLeft']:,} put them left"
        )
    frac = stats.get("largestComponentFraction") or 0.0
    notes.append(
        f"navgraph: {stats.get('roads', 0):,} roads / {stats.get('nodes', 0):,} nodes, "
        f"{stats.get('components', 0)} connected components, largest holds "
        f"{frac:.1%} of them"
    )
    return notes


def notes(stats, max_width):
    """One line per approximation this build made, with its count. Print them."""
    return _lane_notes(stats) + _width_notes(stats, max_width) + _graph_notes(stats)
