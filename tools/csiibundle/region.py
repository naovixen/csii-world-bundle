"""What "the build region" means, in one place, for every consumer and every stage.

A region is `{"centerX": …, "centerZ": …, "radius": …}` in bundle-frame metres, and it is a
**SQUARE** — the box of half-width `radius` about the centre, not the disc inscribed in it.

## Why this module exists

It was both. On 2026-08-20 `stages/networks.py` was changed from a disc to a square, with a
post-mortem in place: *"a region build kept a square of buildings and a disc of roads and the
corners of the area had houses and no streets"*. That fix landed on one site. Six others were
still discs when refactor Phase 1 counted them —

    csiibundle/areas.py:348        the surface areas themselves
    csiibundle/bake/areas.py       ...and the centre/radius threaded down to it
    csiibundle/bake/roads.py       the road SURFACE that markings and skirts drape onto
    csiibundle/bake/markings.py    markings, and via imports lane geometry and skirts
    csiibundle/bake/lods.py        which meshes get generated LODs at all
    cs2beamng/stages/navgraph.py   the AI road graph

— against five that were squares (`placement.in_region`, `stages/derived`, `stages/water`,
`pipeline/meshindex`, `stages/networks`). So a region build kept a square of buildings, props,
trees, terrain tiles and road meshes and a disc of everything painted on them: in the corners
of the area there were streets with no markings, no skirts, no lane geometry and no AI roads,
and buildings whose LODs had never been generated.

The area of the difference is not small. A square of half-width `r` is `4r²`; the disc inside
it is `πr²`. **21.5 % of a region build was in that gap.**

CLAUDE.md: *a fix that only one consumer gets is in the wrong place.* This is the place.

## Why a square rather than a disc

Because the things a region is compared against are squares. Tiles are squares, the mesh
index's cells are squares, the settings screen's map picker draws a square, and `--region x z r`
reads as a half-width. A disc is only ever the odd one out, and every one of the six sites above
inherited it from the same copied line rather than from a decision.
"""

# What shape a region dict describes. It rides ALONG with the region into the bake's option
# fingerprint (`bake/runner.py`), so a derived cache baked while the rule was a disc is stale
# under the square and re-bakes, while a whole-bundle bake — where the two rules agree, there
# being no boundary — is not disturbed at all.
SHAPE = "square"


def tag(region):
    """The region dict with the rule it was filtered under recorded on it. None passes through."""
    if not region:
        return region
    if region.get("shape") == SHAPE:
        return region
    tagged = dict(region)
    tagged["shape"] = SHAPE
    return tagged


def contains(x, z, region, reach=0.0):
    """Is the bundle-frame point `(x, z)` inside the region box, grown by `reach` metres?

    `region` may be None, which means "the whole bundle" and is always True. `reach` is for a
    filter that must keep a little beyond the window — a segment whose origin sits outside but
    whose geometry a marking at the edge still drapes onto.
    """
    if not region:
        return True
    radius = float(region["radius"]) + float(reach)
    return (
        abs(float(x) - float(region["centerX"])) <= radius
        and abs(float(z) - float(region["centerZ"])) <= radius
    )


def bounds(region, reach=0.0):
    """`(min_x, max_x, min_z, max_z)` of the region box, grown by `reach`. None for no region."""
    if not region:
        return None
    radius = float(region["radius"]) + float(reach)
    cx, cz = float(region["centerX"]), float(region["centerZ"])
    return (cx - radius, cx + radius, cz - radius, cz + radius)


def intersects_box(region, min_x, min_z, max_x, max_z, reach=0.0):
    """Does the region box overlap the axis-aligned box `(min_x, min_z)`-`(max_x, max_z)`?

    For deciding whether a whole TILE is worth building, where `contains` on a single point
    would drop a tile the region clips a corner of.
    """
    if not region:
        return True
    x0, x1, z0, z1 = bounds(region, reach)
    return not (max_x < x0 or min_x > x1 or max_z < z0 or min_z > z1)
