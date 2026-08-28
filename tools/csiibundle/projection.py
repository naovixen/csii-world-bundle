"""The surface protocol a drape targets — two queries, no geometry access.

`ProjectionSurface` is deliberately small. A consumer with a scene graph implements it over
whatever spatial index it already has (a BVH, a physics collider, a depth pre-pass); a consumer
with nothing uses `csiibundle.heightsurface.HeightfieldSurface` (the §8 heightmap) or
`csiibundle.meshsurface.TriangleSoupSurface` (the network meshes). Why the two queries are not
the same question — and why substituting one for the other is a real defect — is
`csiibundle.drape`'s module docstring.
"""

# How far up `height()` looks from. Surfaces above this are not candidates, which matters only
# for a MESH surface — a heightfield has one surface per (x, y) and nothing to cut off.
#
# This was a `ceiling=4000.0` parameter on eight signatures across two modules, and not one
# caller in either consumer ever passed a value: every call took the default, so the knob was
# eight restatements of one number. CS2's terrain sits near y=500 and its tallest towers are
# tens of metres, so 4 km is far above anything a city contains while staying finite — the
# point is to be above the world, not to be tuned. TUNED (the value) / KISS (that it is one).
HEIGHT_CEILING = 4000.0


class ProjectionSurface:
    """The interface `drape_block` and the area conformance need from a surface.

    Implement both methods over whatever spatial structure you already have. All coordinates are
    Z-up (X east, Y north, Z up) and all heights are world metres.

    Subclassing is optional — anything with these two methods will do.
    """

    def drop(self, x, y, z, half_up, half_down):
        """The surface height nearest `z` under `(x, y)`, or `None` if nothing is in reach.

        Only surfaces within `[z - half_down, z + half_up]` count, and among those the one
        NEAREST `z` wins — not the highest. See the module docstring for why that distinction
        is load-bearing. The window comes from the projector's own bounds, not from a constant.
        """
        raise NotImplementedError

    def height(self, x, y):
        """The topmost surface under `(x, y)`, or `None` if nothing is there.

        The plain "what is the ground here" query, for a layer with no expected height.
        Surfaces above `HEIGHT_CEILING` are not considered — see that constant for why the
        cut-off is a module constant and not a parameter.
        """
        raise NotImplementedError
