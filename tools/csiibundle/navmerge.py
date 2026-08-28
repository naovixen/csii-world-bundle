"""The consumer's node merge, and the graph rules built on it.

When are two roads' terminal nodes THE SAME NODE? Not a question this package gets to answer by
itself: a consumer keys its edges on node ids, and the ids come from ITS merge of coincident
terminal nodes. `NODE_MERGE_DISTANCE`/`NODE_MERGE_RADIUS` are the tightest such merge in
evidence — BeamNG's `mergeOverlappingNodes(endNodesOnly)`, `lua/ge/map.lua:1209-1211`, which
fuses two terminal nodes when they are within 0.1 m in 3-D **and** their radii agree to within
0.1 m. A looser consumer merge only fuses more, and `simple_graph` is sound under any superset
of this.

Everything that asks "is this one node or two?" — the simple-graph rule, the component count —
has to ask it of the SAME merge, or the two answers disagree about the same graph.
"""

from collections import defaultdict

import numpy

NODE_MERGE_DISTANCE = 0.1
NODE_MERGE_RADIUS = 0.1


def terminal_clusters(roads, distance=NODE_MERGE_DISTANCE, radius=NODE_MERGE_RADIUS):
    """`[(a, b), ...]` — the node each road's first and last node belongs to, one entry per road.

    The node identity a consumer will have: coincident terminal nodes fused, transitively, by
    the merge documented in the module docstring.
    """
    from scipy.spatial import cKDTree

    points = numpy.asarray(
        [node[:3] for road in roads for node in (road.nodes[0], road.nodes[-1])],
        dtype=numpy.float64,
    )
    radii = numpy.asarray(
        [0.5 * road.nodes[i][3] for road in roads for i in (0, -1)], dtype=numpy.float64
    )
    parent = list(range(len(points)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, j in cKDTree(points).query_pairs(distance):
        if abs(radii[i] - radii[j]) < radius:
            ra, rb = find(i), find(j)
            if ra != rb:
                parent[rb] = ra
    return [(find(2 * i), find(2 * i + 1)) for i in range(len(roads))]


def simple_graph(roads, distance=NODE_MERGE_DISTANCE, radius=NODE_MERGE_RADIUS):
    """Make the emitted set a SIMPLE graph on the nodes a consumer will actually have.

    `(roads, stats)`. Terminal nodes are clustered by the consumer merge documented in the
    module docstring — within `distance` in 3-D and with radii within `radius`, transitively —
    and then:

    * a **connector or joint whose two ends land in one cluster** is a self-loop. It joins a node
      to itself, which is not an edge in any graph a consumer keys on node pairs, and it is what
      turns into a closed 3-cycle the moment the merge runs. Dropped.
    * **a second road between one pair of clusters** is not a second edge. Whichever arrives last
      replaces the first in `links`, and the pair of them (one direct, one through an interior
      node) is the triangle whose middle node BeamNG's `optimizeNodes` walks into
      `map.lua:459`. One is kept — the one carrying the geometry — and the rest dropped.

    **A segment road is never dropped**: it is the road network itself, a segment CS2 modelled as
    a closed ring legitimately begins and ends at one node, and two segments sharing a node pair
    are a divided carriageway, not a duplicate. Connectors and joints are what this thins, and a
    dropped one always leaves its two nodes joined by the road that kept the turn's shape.
    """
    stats = {"selfLoopsDropped": 0, "duplicateEdgesDropped": 0}
    if not roads:
        return roads, stats
    ends = terminal_clusters(roads, distance, radius)

    # A segment first, then the road carrying the most geometry: where a stub and a real turning
    # path join the same two nodes, the turn's shape is the one worth keeping.
    order = sorted(
        range(len(roads)),
        key=lambda i: (roads[i].kind != "segment", -len(roads[i].nodes), roads[i].key),
    )
    taken = {}
    keep = [False] * len(roads)
    for i in order:
        road = roads[i]
        a, b = ends[i]
        if road.kind != "segment":
            if a == b:
                stats["selfLoopsDropped"] += 1
                continue
            pair = (min(a, b), max(a, b))
            if pair in taken:
                stats["duplicateEdgesDropped"] += 1
                continue
            taken[pair] = road.key
        else:
            taken.setdefault((min(a, b), max(a, b)), road.key)
        keep[i] = True
    return [road for i, road in enumerate(roads) if keep[i]], stats


def components(roads):
    """Connected-component sizes over the emitted graph.

    Two roads are joined where their terminal nodes are THE SAME NODE — `terminal_clusters`, the
    consumer's own merge, and not a coordinate match. The difference is not academic: on the
    reference city a coordinate match calls this graph 96 % connected and the graph BeamNG builds
    out of it stood in 692 pieces, because a 3.5 m connector and the 6.5 m road it ended on were
    two nodes at one coordinate. A navgraph in many pieces routes nowhere, and the size of the
    largest component is the one number that says whether the junction join worked at all.
    """
    if not roads:
        return {"components": 0, "largestComponent": 0, "largestComponentFraction": 0.0}
    ends = terminal_clusters(roads)
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in ends:
        union(a, b)
    sizes = defaultdict(int)
    for a, _b in ends:
        sizes[find(a)] += 1
    ordered = sorted(sizes.values(), reverse=True)
    total = sum(ordered)
    return {
        "components": len(ordered),
        "largestComponent": ordered[0] if ordered else 0,
        "largestComponentFraction": round(ordered[0] / total, 4) if total else 0.0,
    }
