"""Instances (§4) — placing the 1.3 million things, and the three rules that are not obvious.

`instances.jsonl` is the biggest layer and the simplest to read: one record per placed object,
with an `assetId` naming a mesh under `meshes/`, a `pos`/`rot`/`scale` transform, a `kind`, and
an `attrs` bag. `frames.instance_matrix` turns the transform into a matrix and `glb.parse_glb`
reads the mesh; that is most of a city.

What is NOT obvious, and is the whole reason this module exists:

**A building on the ground has a second mesh.** `<assetId>__base.glb` is its foundation skirt
(§4.1) — the plinth that hides the gap between a flat building footprint and sloping terrain.
It is placed at the SAME transform as the building. But only for buildings actually resting on
the ground: `attrs.elevationFlags` absent or exactly `["OnGround"]`. A building on stilts or
over water has no skirt and placing one puts a concrete band in mid-air. `skirt_asset_id` and
`wants_skirt` are that rule.

**The skirt is authored, not fitted.** It ships as a 1 m band. Dropping its `v = 0` ring down to
the terrain is the CONSUMER's job — the producer cannot know your ground. Left as authored on a
slope it will show a gap under one side, and that is expected rather than a defect.

**A `decal` instance is paint, not surface.** These are bay markings, arrows, manhole covers —
flat quads the game paints through a projector volume. Two consequences. They need the
`instance_decal` rung of `decals.LIFT_LADDER` or they z-fight the carriageway they lie on. And
they must **never become collision geometry**: a car should drive over a painted arrow, not into
it. Keeping them in their own group is what makes that one command away.

**A missing mesh is not automatically a defect.** Check `reader.read_declared_gaps` first: the
producer declares what it could not deliver (§10), and re-reporting a declared gap as an error
makes the error count a function of the producer's honesty. An UNDECLARED missing mesh is real.
"""

from . import decals, frames, reader

# §4's closed `kind` set. Listed rather than derived so a v2 addition shows up as a deliberate
# edit instead of silently joining a wildcard. CONTRACT.
KINDS = ("building", "prop", "tree", "fence", "decal", "poi")

# The suffix the producer gives a building's foundation skirt (§4.1). CONTRACT.
SKIRT_SUFFIX = "__base"

# The only `elevationFlags` value that still counts as resting on the ground. An empty or absent
# list means the same thing. CONTRACT.
ON_GROUND = "OnGround"


def read_instances(bundle_dir):
    """Stream `instances.jsonl`. A generator — a full city is over a million records."""
    return reader.read_jsonl(reader.layer_path(bundle_dir, "instances"))


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


def placement_lift(record):
    """Metres to raise this record along the up axis before drawing it, from the lift ladder.

    Non-zero only for `decal` instances, which are coplanar paint on a surface they would
    otherwise z-fight. Everything else is 0.0 and needs no special case.
    """
    return decals.LIFT["instance_decal"] if record.get("kind") == "decal" else 0.0


def is_paint(record):
    """Whether this record is paint rather than a solid — i.e. must not become collision.

    Currently exactly the `decal` kind. Written as a question about the record rather than a
    comparison at the call site, so a future paint-like kind is added here once.
    """
    return record.get("kind") == "decal"


def instance_matrix(record, mesh_basis=None, zup=False):
    """This record's world matrix, with `placement_lift` already applied.

    `zup`/`mesh_basis` are handed to `frames.zup_instance_matrix` for a Z-up consumer; the
    default is the contract's own bundle-frame matrix. This is the function to call rather than
    `frames.instance_matrix` directly, because forgetting the decal lift is silent until
    something flickers.
    """
    matrix = (frames.zup_instance_matrix(record, mesh_basis) if zup
              else frames.instance_matrix(record))
    lift = placement_lift(record)
    if lift:
        # The up axis differs between the two frames: Z for a Z-up consumer, Y in the bundle.
        matrix = matrix.copy()
        matrix[2 if zup else 1, 3] += lift
    return matrix
