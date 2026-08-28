"""The §4.1 foundation skirt: who gets one, its two rings, and the drop to the ground.

A building resting on the ground places `<assetId>__base.glb` at its own transform — the plinth
that hides the gap between a flat footprint and sloping terrain. It ships as an authored 1 m
band; dropping its bottom ring to the terrain is the CONSUMER's job, and the whole rule set
lives here (`csiibundle.instances`'s module docstring tells the story).
"""

# The suffix the producer gives a building's foundation skirt (§4.1). CONTRACT.
SKIRT_SUFFIX = "__base"

# The only `elevationFlags` value that still counts as resting on the ground. An empty or absent
# list means the same thing. CONTRACT.
ON_GROUND = "OnGround"


def wants_skirt(record):
    """Whether this record should also place its `__base` foundation skirt (§4.1).

    Buildings only, and only those resting on the ground. A building whose `elevationFlags` say
    anything else — elevated, on water, in a tunnel — must not get one.
    """
    if record.get("kind") != "building":
        return False
    flags = (record.get("attrs") or {}).get("elevationFlags")
    return not flags or flags == [ON_GROUND]


def skirt_asset_id(record):
    """The skirt's assetId for this record, or `None` if it should not have one.

    The mesh may still be absent — not every building prefab has a skirt — so the caller checks
    the file as usual and treats absence as "no skirt", not as a missing mesh.
    """
    return record["assetId"] + SKIRT_SUFFIX if wants_skirt(record) else None


# The authored band is 1 m tall (§4.1). Anything in the lower half of the band's own height is
# the bottom ring; the tolerance is generous because the ring is found by GEOMETRY, not by UV —
# §4.1 says the bottom ring is at `v = 0` but §6 says the producer flips V into glTF's top-left
# origin, and which of the two a given reader sees depends on its loader. Height does not.
SKIRT_RING_FRACTION = 0.5


def skirt_rings(verts, up_axis=1):
    """`(bottom_mask, top_mask)` over the vertices of a `__base` skirt band, by height.

    `verts` is `(N, 3)` in any frame whose `up_axis` column is up (1 for bundle/glTF Y-up
    arrays straight from `glb.parse_glb`, 2 for a Z-up world array). Every vertex is one or the
    other: the band is two rings and nothing else.
    """
    import numpy

    h = numpy.asarray(verts, dtype=numpy.float64)[:, up_axis]
    lo, hi = float(h.min()), float(h.max())
    if hi - lo < 1e-6:
        bottom = numpy.zeros(len(h), dtype=bool)
        return bottom, ~bottom
    bottom = h < lo + (hi - lo) * SKIRT_RING_FRACTION
    return bottom, ~bottom


def drop_skirt(world_verts, bottom_mask, ground_height, up_axis=2):
    """Move the bottom ring of a placed skirt to the ground: §4.1's "moves every `v = 0` vertex
    down to the terrain surface at its world X/Z (never up above the top ring)".

    `world_verts` `(N, 3)` in the consumer's frame with `up_axis` up; `ground_height` `(N,)`
    heights sampled at each vertex's own (x, y) — NaN where the ground is unknown, in which
    case that vertex keeps its authored height. The new height is `min(ground, top ring)` so a
    ground rising above the base level can never invert the band. Returns a new array.
    """
    import numpy

    out = numpy.array(world_verts, dtype=numpy.float64, copy=True)
    bottom = numpy.asarray(bottom_mask, dtype=bool)
    ground = numpy.asarray(ground_height, dtype=numpy.float64)
    if not bottom.any():
        return out
    top_level = (
        float(out[~bottom, up_axis].min()) if (~bottom).any() else float(out[:, up_axis].max())
    )
    target = numpy.minimum(ground[bottom], top_level - 1e-3)
    keep = numpy.isnan(target)
    current = out[bottom, up_axis]
    out[bottom, up_axis] = numpy.where(keep, current, target)
    return out


def skirt_flags_index(records):
    """`{id: elevationFlags}` for every BUILDING record — the parent lookup §4.1 asks for.

    §4.1: "for a building extension — a record with `parent` — read the parent's flags". A
    consumer streams `instances.jsonl` twice: once through this (buildings only, so the index is
    thousands of entries, not a million), then once placing skirts with `wants_skirt(record,
    parents=index)`.
    """
    index = {}
    for record in records:
        if record.get("kind") == "building" and "id" in record:
            index[record["id"]] = (record.get("attrs") or {}).get("elevationFlags")
    return index


def wants_skirt_with_parents(record, parents):
    """`wants_skirt` that reads an extension's PARENT flags when the record has a `parent` that
    resolves in `parents` (from `skirt_flags_index`). Falls back to the record's own flags."""
    if record.get("kind") != "building":
        return False
    parent = record.get("parent")
    if parent is not None and parent in parents:
        flags = parents[parent]
    else:
        flags = (record.get("attrs") or {}).get("elevationFlags")
    return not flags or flags == [ON_GROUND]
