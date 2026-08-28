"""Ray-cast access to imported geometry, for the layers the game paints ONTO a surface.

Two layers need it and need the same thing: lane decals project onto the road, surface areas
project onto the terrain. Both are projector volumes in the game (audit §19, §30), so both are
reconstructed here as geometry that has to be conformed to whatever it paints.
"""

import numpy
from csiibundle import drape
from mathutils import Vector
from mathutils.bvhtree import BVHTree


class ProjectionSurface(drape.ProjectionSurface):
    """Ray-cast access to an imported surface, for the layers the game paints ONTO one.

    Two layers need this and they need the same thing: lane decals project onto the road
    (`build_lanes`), surface areas project onto the terrain (`build_areas`). Both are
    projector volumes in the game (audit §19, §30) and both are reconstructed here as
    geometry that has to be conformed to whatever it paints.

    One BVH over every network mesh would hold ~10M triangles for a full city, most of it
    never queried. Instead the objects are binned into a 128 m grid by bounding box and each
    object's BVH (world space, Blender frame) is built lazily on first query — a
    neighbourhood import builds only the handful it touches.
    """

    CELL = 128.0
    _DOWN = Vector((0.0, 0.0, -1.0))

    def __init__(self, objects):
        self._objects = []
        self._grid = {}
        self._trees = {}
        self.kerb_rejections = 0
        for obj in objects:
            if obj.type != "MESH":
                continue
            index = len(self._objects)
            self._objects.append(obj)
            matrix = obj.matrix_world
            corners = [matrix @ Vector(corner) for corner in obj.bound_box]
            xs = [c.x for c in corners]
            ys = [c.y for c in corners]
            for cx in range(int(min(xs) // self.CELL), int(max(xs) // self.CELL) + 1):
                for cy in range(int(min(ys) // self.CELL), int(max(ys) // self.CELL) + 1):
                    self._grid.setdefault((cx, cy), []).append(index)

    def _tree(self, index):
        if index not in self._trees:
            obj = self._objects[index]
            mesh = obj.data
            if len(mesh.polygons) == 0:
                self._trees[index] = None
                return None
            # foreach_get + one matmul instead of a Python matrix apply per vertex —
            # a full city holds ~19M network vertices and this path builds most of them.
            count = len(mesh.vertices)
            co = numpy.empty(count * 3, dtype=numpy.float64)
            mesh.vertices.foreach_get("co", co)
            matrix = numpy.array(obj.matrix_world, dtype=numpy.float64)
            world = co.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
            mesh.calc_loop_triangles()
            tris = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
            mesh.loop_triangles.foreach_get("vertices", tris)
            self._trees[index] = BVHTree.FromPolygons(world.tolist(), tris.reshape(-1, 3).tolist())
        return self._trees[index]

    def drop(self, x, y, z, half_up, half_down):
        """Road surface hit nearest the expected height z under (x, y), Blender frame, or
        None if nothing falls inside `[z - half_down, z + half_up]`.

        Tier 0 (2026-08-07 amendment): `half_up`/`half_down` come from the marking's OWN
        projector half-height (laneprefabs.json `boundsMin`/`boundsMax` y-extent, median
        1.00 m -> ±0.5 m) instead of the old fixed `probe=3.0, depth=8.0` window, and the
        pick is the hit NEAREST z rather than the HIGHEST hit in range. Highest-wins snapped
        ~2.5% of drape samples onto kerb tops, median walls and bridge railings even inside a
        tightened window: a kerb 0.4 m above the road is still inside a ±0.5 m reach and
        would still win on height alone. Nearest-to-expected cannot make that mistake — the
        road surface is always the candidate closest to the un-draped height the lane curve
        itself encodes, and `kerb_rejections` counts how often the two picks would disagree.

        MEASURED AND LEFT ALONE, 2026-08-11. The regression plan proposed re-casting below
        every hit so each object contributes all its surfaces rather than only its topmost,
        on the theory that a kerb was shadowing the carriageway inside one merged segment
        mesh. `tools/test_drape_pick.py` refutes both halves of that on the shipped bundle:

          * There is nothing to enumerate. Segment meshes are single-layer shells — 24,059 of
            24,060 samples have exactly ONE surface beneath them — so the carriageway is never
            hiding under anything. Enumerating every hit and keeping this same nearest rule
            reproduces this rule's numbers to the digit (0.21% of samples > 2 cm above their
            own road, max +0.298 m, at the city's densest 300 m window).
          * Choosing the highest candidate at or below the lane curve instead is WORSE, not
            better: 0.46% > 2 cm, twice this rule's rate. Nearest-to-expected is the better
            calibrated discriminator, and neighbouring objects — not one object's own kerbs —
            are the only place a choice arises at all (2.1% of samples there, 0% in a plain
            road window).

        So the lift the plan set out to fix is not made here. Do not re-litigate this from
        static reading of the code; run the test.
        """
        origin = Vector((x, y, z + half_up))
        candidates = []
        for index in self._grid.get((int(x // self.CELL), int(y // self.CELL)), ()):
            tree = self._tree(index)
            if tree is None:
                continue
            location = tree.ray_cast(origin, self._DOWN, half_up + half_down)[0]
            if location is not None:
                candidates.append(location.z)
        if not candidates:
            return None
        nearest = min(candidates, key=lambda c: abs(c - z))
        if len(candidates) > 1 and nearest != max(candidates):
            self.kerb_rejections += 1
        return nearest

    def height(self, x, y, ceiling=4000.0):
        """Topmost hit under (x, y) from far above, or None — the plain "what is the ground
        here" query. `drop`'s nearest-to-expected pick exists to keep a lane marking off a
        kerb it happens to pass; an area has no expected height to be near, only the surface
        it is painted on, so this takes the highest hit in a window tall enough to clear any
        terrain in the map."""
        origin = Vector((x, y, ceiling))
        best = None
        for index in self._grid.get((int(x // self.CELL), int(y // self.CELL)), ()):
            tree = self._tree(index)
            if tree is None:
                continue
            location = tree.ray_cast(origin, self._DOWN, 2.0 * ceiling)[0]
            if location is not None and (best is None or location.z > best):
                best = location.z
        return best
