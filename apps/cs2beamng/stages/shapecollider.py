"""What a shape collides with: its own geometry, a box, or nothing.

The choice is per shape and it is a trade the level pays for at every contact. A padded box is a
trap above a certain size — the airport's was 1.3 km across — and the real geometry is not always
affordable, so the rule reads the shape's own bounds and its kind.
"""

from csiibundle import collision, decimate

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
    # Over the triangle cap the collider is DECIMATED to it, never boxed. The cap used to be
    # the point at which a shape fell back to the box (with the oversized guard above keeping
    # the airport off that path), and at 20,000 it was never reached: "simplified" shipped the
    # full LOD 1 hull on every building, a residential block at 20,700 collision triangles and
    # the world at 25 M from object statics alone (refactor Phase 10 survey §1). Clustering the
    # hull to the cap keeps the building's shape for the car to touch and bounds what the
    # collision system loads.
    status = "mesh"
    cap = int(options["colmesh_max_tris"] or 0)
    if options["collision_preset"] != "exact" and cap > 0 and len(tris) > cap:
        verts, tris = _decimated(verts, tris, cap)
        status = "mesh_decimated"
    elif oversized and cap > 0 and len(tris) > cap:
        status = "mesh_oversized"
    cut = _from_geometry(shape_name, base_mesh, verts, tris, status)
    if cut is not None:
        return cut
    if oversized:
        # A shape that is THIN (under the 0.3 m a colmesh needs, so `_from_geometry` refused
        # it) AND wider than a box may be gets NO collider. The guard above only ever covered
        # the triangle-cap path; this path boxed unconditionally, and shipped
        # `Baseball_Field__2_revised2` - a 138 x 138 m field plane 0.1 m tall - as a padded
        # slab with a 20 cm step round the whole outfield, and a 30 m roundabout the same way
        # (validation pass 2026-08-31, A4). A large flat prop lies on terrain that already
        # collides; a small thin one (a sign, a fence panel) keeps the padded box that holds a
        # wheel. Counted per shape as `oversizedThin`.
        return (), "oversizedThin"
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


def _decimated(verts, tris, cap):
    """`(verts, tris)` clustered to about `cap` triangles — the same vertex clustering the LOD
    ladder uses, so a collider is the coarsest level of the shape it belongs to."""
    mesh = decimate.cluster_mesh(
        {"verts": verts, "tris": tris, "material_runs": [(None, len(tris))]},
        cap / float(len(tris)),
    )
    return mesh["verts"], mesh["tris"]


def _from_geometry(shape_name, base_mesh, verts, tris, status):
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
    ), status
