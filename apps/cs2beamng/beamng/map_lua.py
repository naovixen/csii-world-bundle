"""A port of the part of BeamNG's `lua/ge/map.lua` that turns `DecalRoad`s into `map.getMap()`.

Not a model of the whole navgraph pipeline — only enough of it to answer one question offline:
**would this level's roads crash `map.lua` on load, or reach its optimiser in a shape it cannot
represent?** The answer used to arrive as a `*** FATAL LUA ERROR` on Naomi's screen after a
fourteen-minute build; it now arrives from the build's own validate stage.

The three stages ported, in the order `linkMap()` runs them (map.lua:2140-2170):

1. `loadJsonDecalMap` (map.lua:721-777) — the `useSubdivisions: false` branch, which is the one
   our roads take: one map node per road node, named `<roadName><index>`, `radius` half the
   node's width, and one edge per consecutive pair carrying `drivability`, `oneWay`, `lanes`,
   `speedLimit` and the in/out node positions and radii it was built from. `lanes` is the string
   `map.lua` composes from `lanesLeft`/`lanesRight` when `autoLanes` is false.
2. `mergeOverlappingNodes(true)` (map.lua:1187-1268) — the END-NODE merge, and the only one that
   has run by the time the optimiser first walks the graph: two terminal nodes fuse when they
   are within 0.1 m in 3-D **and** their radii agree to within 0.1 m.
3. The one branch of `optimizeNodes` (map.lua:1969-2000) that can raise. When it drops a
   degree-2 node whose two neighbours are ALREADY linked, it builds the replacement edge `e2`
   without a `lanes` field and hands it to `edgeCompare`, whose `lanes` test — alone among its
   ten — carries no `or ''` guard:

       elseif a.lanes ~= b.lanes then
         return a.lanes < b.lanes            -- map.lua:459

   So `e2.lanes` is nil, the chord's is a string, and the load dies with "attempt to compare nil
   with string" the moment every earlier field ties. That is a BeamNG bug and we cannot patch the
   game's Lua; what a level CAN do is never hand it that configuration, which is what
   `csiibundle.navgraph`'s simple-graph rule guarantees and what `crash_risks` re-checks on the
   built artefact (the rule holding in the builder is not the same claim as it reaching the
   level).
"""

from collections import defaultdict

MERGE_DISTANCE = 0.1  # map.lua:1211, sqDist < 0.01
MERGE_RADIUS = 0.1  # map.lua:1211, abs(r1 - r2) < 0.1


def _is_one_way(lanes):
    return ("-" not in lanes) or ("+" not in lanes)


def _flip_lanes(lanes):
    return "".join("-" if c == "+" else ("+" if c == "-" else "0") for c in reversed(lanes))


def build_nodes(roads, right_hand_drive=False):
    """`loadJsonDecalMap`'s DecalRoad branch. `roads` are the level's `DecalRoad` objects."""
    nodes = {}

    def name_node(prefix, idx):
        name = f"{prefix}{idx}"
        if name in nodes:  # map.lua:135 nameNode
            base, k = name + "_", 1
            while base + str(k) in nodes:
                k += 1
            name = base + str(k)
        return name

    for road in roads:
        if float(road.get("drivability") or 0) <= 0:
            continue
        points = road.get("nodes") or []
        if len(points) < 2:
            continue
        prefix = road.get("name") or ""
        drivability = float(road["drivability"])
        one_way = bool(road.get("oneWay", False))
        speed = road.get("speedLimit")
        speed = float(speed) if speed not in (None, "") else None
        if speed is not None and speed <= 0:
            speed = None  # "auto calculate", map.lua:613
        flip = bool(road.get("flipDirection", False))
        no_merge = not road.get("autoJunction", True)
        lanes = None
        if not road.get("autoLanes", False):  # map.lua:627
            left = max(0, int(road.get("lanesLeft") or 0))
            right = max(0, int(road.get("lanesRight") or 0))
            lanes = ("+" * left + "-" * right) if right_hand_drive else ("-" * left + "+" * right)
            one_way = _is_one_way(lanes)
            if one_way and lanes[:1] == "-":
                lanes, flip = _flip_lanes(lanes), not flip
        previous = name_node(prefix, 1)
        nodes[previous] = {
            "pos": tuple(points[0][:3]),
            "radius": 0.5 * points[0][3],
            "links": {},
            "endNode": True,
            "noMerge": no_merge,
            "road": prefix,
        }
        for i in range(1, len(points)):
            current = name_node(prefix, i + 1)
            nodes[current] = {
                "pos": tuple(points[i][:3]),
                "radius": 0.5 * points[i][3],
                "links": {},
                "endNode": False,
                "noMerge": no_merge,
                "road": prefix,
            }
            in_node = current if flip else previous
            out_node = previous if in_node == current else current
            edge = {
                "drivability": drivability,
                "oneWay": one_way,
                "lanes": lanes,
                "speedLimit": speed,
                "inNode": in_node,
                "outNode": out_node,
                "inPos": nodes[in_node]["pos"],
                "outPos": nodes[out_node]["pos"],
                "inRadius": nodes[in_node]["radius"],
                "outRadius": nodes[out_node]["radius"],
                "road": prefix,
            }
            nodes[current]["links"][previous] = edge
            nodes[previous]["links"][current] = edge
            previous = current
        nodes[previous]["endNode"] = True
    return nodes


def merge_end_nodes(nodes, distance=MERGE_DISTANCE, radius=MERGE_RADIUS):
    """`mergeOverlappingNodes(true)`: terminal nodes within `distance` whose radii agree."""
    import numpy
    from scipy.spatial import cKDTree

    ids = [k for k, v in nodes.items() if v["endNode"]]
    if not ids:
        return 0
    points = numpy.asarray([nodes[k]["pos"] for k in ids], dtype=numpy.float64)
    parent = {k: k for k in ids}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, j in cKDTree(points).query_pairs(distance):
        a, b = ids[i], ids[j]
        if abs(nodes[a]["radius"] - nodes[b]["radius"]) < radius:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups = defaultdict(list)
    for k in ids:
        groups[find(k)].append(k)
    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        merged += len(group) - 1
        keep = group[0]
        for other in group[1:]:
            first, second = nodes[keep], nodes[other]
            first["links"].pop(other, None)  # the edge between them is a self-edge; dropped
            second["links"].pop(keep, None)
            for other_id, edge in list(second["links"].items()):
                neighbour = nodes.get(other_id)
                if neighbour is None:
                    continue
                neighbour["links"].pop(other, None)
                if other_id not in first["links"]:
                    edge["inNode"] = keep if edge["inNode"] == other else other_id
                    edge["outNode"] = other_id if edge["inNode"] == keep else keep
                    neighbour["links"][keep] = edge
                    first["links"][other_id] = edge
            del nodes[other]
    return merged


def _sort_edge_nodes(edge):
    """map.lua:399 sortEdgeNodes."""
    low = (edge["inPos"], edge["inRadius"])
    high = (edge["outPos"], edge["outRadius"])
    for axis in range(3):
        if low[0][axis] != high[0][axis]:
            return (low, high) if low[0][axis] < high[0][axis] else (high, low)
    if low[1] != high[1]:
        return (low, high) if low[1] < high[1] else (high, low)
    return (low, high)


def _reaches_lanes_test(a, b):
    """Whether `edgeCompare(a, b)` falls through to the unguarded `a.lanes < b.lanes`."""
    a_low, a_high = _sort_edge_nodes(a)
    b_low, b_high = _sort_edge_nodes(b)
    for one, two in ((a_low, b_low), (a_high, b_high)):
        if tuple(one[0]) != tuple(two[0]) or one[1] != two[1]:
            return False
    if a["drivability"] != b["drivability"]:
        return False
    if bool(a["oneWay"]) != bool(b["oneWay"]):
        return False
    if (a["speedLimit"] or 0) != (b["speedLimit"] or 0):
        return False
    return a["lanes"] != b["lanes"]


def crash_risks(nodes):
    """Every configuration that would take `optimizeNodes` into `map.lua:459` and end the load.

    A degree-2 node whose two neighbours are already linked to each other: the optimiser drops it
    and compares its replacement edge — which it builds WITHOUT `lanes` — against the chord.
    Returns `(risks, chords)`, `risks` being `[{node, between, road, chord}, ...]`.

    `chords` counts every such node whether or not the ten fields tie, and it is the number worth
    gating on. Which node of a merge group survives, and in which order `next()` walks a table,
    are Lua hash-order details this port does not reproduce — they move a tie between two edges
    from one node to its neighbour, so a `risks` of 0 with a `chords` of 300 means "not this
    time" rather than "not possible". A `chords` of 0 means the branch is never entered at all.
    """
    out = []
    chords = 0
    for node_id, node in nodes.items():
        if len(node["links"]) != 2:
            continue
        (first_id, d1), (second_id, d2) = list(node["links"].items())
        if first_id == second_id:
            continue
        first, second = nodes.get(first_id), nodes.get(second_id)
        if first is None or second is None:
            continue
        chord = first["links"].get(second_id)
        if chord is None:
            continue
        chords += 1
        in_node = second_id if d1["inNode"] == node_id else first_id
        out_node = second_id if d1["outNode"] == node_id else first_id

        # map.lua:1989-1992, transcribed rather than tidied. The radius lines test `d2.outNode`
        # where the position lines test `d2.inNode`; that asymmetry is theirs, and a predicate
        # about their code has to reproduce it to mean anything.
        in_pos = (
            d1["inPos" if d1["inNode"] == first_id else "outPos"]
            if in_node == first_id
            else d2["inPos" if d2["inNode"] == second_id else "outPos"]
        )
        out_pos = (
            d1["outPos" if d1["outNode"] == first_id else "inPos"]
            if out_node == first_id
            else d2["outPos" if d2["outNode"] == second_id else "inPos"]
        )
        in_radius = (
            d1["inRadius" if d1["inNode"] == first_id else "outRadius"]
            if in_node == first_id
            else d2["inRadius" if d2["outNode"] == second_id else "outRadius"]
        )
        out_radius = (
            d1["outRadius" if d1["outNode"] == first_id else "inRadius"]
            if out_node == first_id
            else d2["outRadius" if d2["outNode"] == second_id else "inRadius"]
        )
        merged = {
            "drivability": min(d1["drivability"], d2["drivability"]),
            "speedLimit": (
                min(d1["speedLimit"], d2["speedLimit"])
                if None not in (d1["speedLimit"], d2["speedLimit"])
                else None
            ),
            "oneWay": d1["oneWay"],
            "lanes": None,
            "inPos": in_pos,
            "inRadius": in_radius,
            "outPos": out_pos,
            "outRadius": out_radius,
        }
        if chord["lanes"] is not None and _reaches_lanes_test(merged, chord):
            out.append(
                {
                    "node": node_id,
                    "between": (first_id, second_id),
                    "road": d1.get("road"),
                    "chord": chord.get("road"),
                }
            )
    return out, chords


def check(roads, right_hand_drive=False):
    """`(stats, risks)` for a level's `DecalRoad` objects."""
    nodes = build_nodes(roads, right_hand_drive=right_hand_drive)
    merged = merge_end_nodes(nodes)
    risks, chords = crash_risks(nodes)
    return (
        {
            "mapNodes": len(nodes),
            "endNodesMerged": merged,
            "chords": chords,
            "crashRisks": len(risks),
        },
        risks,
    )
