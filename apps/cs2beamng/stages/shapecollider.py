"""What a shape collides with: its own geometry, a box, or nothing.

The choice is per shape and it is a trade the level pays for at every contact. A padded box is a
trap above a certain size — the airport's was 1.3 km across — and the real geometry is not always
affordable, so the rule reads the shape's own bounds and its kind.
"""

from csiibundle import collision

from ..beamng import dae, daemesh

# How wide a padded BOX collider may be before it is a trap rather than an approximation.
# OURS: CS2 has no collision for a placed object, so there is no rule to port. 20 m is about the
# footprint of a single building, which is the largest thing a box still resembles; measured over
# the reference city, 1,373 of 1,407 boxes are under it (median 1.6 m, p90 6.5 m) and the 34 above
# it are the airport, the harbour, the power plant and a handful of stadiums.
BOX_COLLIDER_MAX_METRES = 20.0

# A trunk collider wider than this share of the shape's own canopy is not a trunk. OURS — CS2
# has no collision for a planted object at all — and it is a REPORTING threshold only: nothing
# behaves differently either side of it, it decides when the build says so out loud.
TRUNK_WIDE_RATIO = 0.5


def collider(shape_name, base_mesh, lod_mesh, kind, options, is_tree=True):
    """The collision policy, in the plan's order: trunk cylinder for a tree, the decimated LOD
    when it is thick enough, a padded box otherwise. `()` when this class does not collide.

    `base_mesh` is LOD 0 — the trunk search wants its material runs — and `lod_mesh` is what the
    colmesh is cut from (LOD 1 when the bake generated one, else LOD 0).

    `is_tree` splits the `tree` kind, which is ALL vegetation: 376,187 of the reference city's
    686,641 planted items are grass, bushes, flower pots and garden beds, and CS2 builds them
    from `PlantObject`, not `TreeObject` (`csiibundle.instances.is_tree`). A plant does not
    collide — you drive through a hedge and over long grass — and it certainly does not get a
    trunk, which is what fitting `trunk_cylinder` to a thing with no trunk was doing.
    """
    if not options["collide"].get(kind, False):
        return (), "none"
    if kind == "tree" and not is_tree:
        return (), "plant"
    verts = lod_mesh.verts
    tris = lod_mesh.tris
    if kind == "tree":
        return _trunk(shape_name, base_mesh)
    lo, hi = collision.bounds(base_mesh.verts)  # the box is LOD 0's, never a decimated hull's
    footprint = max(float(hi[0] - lo[0]), float(hi[1] - lo[1]))
    # A PADDED BOX IS ONLY A STAND-IN WHILE IT IS SMALL. Over the triangle cap the collider used
    # to fall back to the shape's bounding box unconditionally, and on the median shape that box
    # is 1.6 m across and perfectly reasonable. On `InternationalAirport01` it is
    # 1,344 x 1,245 x 82 m — an invisible solid block a kilometre wide, and driving onto the
    # airport puts the car inside it. That is the reported "the car gets stuck in the airport",
    # and 34 shapes carry a box over 20 m: the harbour, the power plant, a stadium, a ballpark.
    # Past that size a box stops approximating the building and becomes a wall round a district,
    # so the triangle cap gives way instead: a few thousand collision triangles cost far less
    # than a trap.
    oversized = footprint > BOX_COLLIDER_MAX_METRES
    if (
        options["collision_preset"] == "exact"
        or len(tris) <= options["colmesh_max_tris"]
        or oversized
    ):
        cut = _from_geometry(shape_name, base_mesh, verts, tris, oversized, options)
        if cut is not None:
            return cut
    box_v, box_t = collision.colbox(lo, hi, daemesh.MIN_COLLIDER_THICKNESS)
    return (
        dae.Mesh(f"Colmesh_{shape_name}", box_v, box_t, [(base_mesh.runs[0][0], len(box_t))]),
    ), "box"


def _trunk(shape_name, base_mesh):
    """A cylinder round the trunk, from the MERGED mesh.

    In the same BeamNG-local frame the visible geometry is written in, so the trunk group is
    found across every sub-mesh (a tree is trunk + leaves, and reading only the first node picks
    whichever of the two the file happens to list first).
    """
    cyl_v, cyl_t = collision.trunk_cylinder(
        {"verts": base_mesh.verts, "tris": base_mesh.tris, "material_runs": base_mesh.runs},
        up_axis=2,
    )
    return (
        dae.Mesh(f"Colmesh_{shape_name}", cyl_v, cyl_t, [(base_mesh.runs[0][0], len(cyl_t))]),
    ), "trunk"


def _from_geometry(shape_name, base_mesh, verts, tris, oversized, options):
    """The shape's own triangles as a colmesh, or None when there is nothing to cut."""
    mesh = collision.colmesh_from_lod(
        {"verts": verts, "tris": tris}, daemesh.MIN_COLLIDER_THICKNESS
    )
    if mesh is None:
        return None
    return (
        dae.Mesh(
            f"Colmesh_{shape_name}",
            mesh["verts"],
            mesh["tris"],
            [(base_mesh.runs[0][0], len(mesh["tris"]))],
        ),
    ), ("mesh_oversized" if oversized and len(tris) > options["colmesh_max_tris"] else "mesh")
