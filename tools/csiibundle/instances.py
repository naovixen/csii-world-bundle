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

**A `.glb` is one STATE of a prefab, and the instance says which state it is in.** This is the
rule most likely to be missed, because ignoring it produces a scene that is entirely plausible
and quietly wrong. A prefab that can be drawn several ways — a tree at five ages, a stockpile at
four fill levels — exports its base state as `<assetId>.glb` and its other states as derived
files alongside (§6.1, §9). `attrs.treeState` and `attrs.fullness` say which one this instance
is actually in, and `mesh_candidates` resolves that to a filename.

On the reference city that is **153,991 trees of 697,256 in a non-Adult state** — 119,237
Elderly and 33,581 Teen. Drawn from the base file they are all healthy adults. The producer
shipped these variants in August 2026 precisely to close that gap; a consumer that does not
join on `treeState` leaves it open on its own side.

**`attrs.growthScale` is a uniform scale and is NOT folded into `scale`.** A tree record has no
`scale` field at all — the growth scale is separate, and 299,972 trees on the reference city
carry one below 1.0. Ignoring it draws every sapling at full height. There is no `__child`
variant and there will not be one: a child tree is a `Teen` mesh below growth 128 scaled by
this factor, so the scale IS the child state.

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


# `attrs.treeState` to the derived file's suffix (§6.1). `Adult` is the BASE state and has no
# suffix — a healthy adult tree *is* `<assetId>.glb`. CONTRACT.
TREE_STATE_SUFFIX = {
    "Teen": "__teen",
    "Elderly": "__elderly",
    "Dead": "__dead",
    "Stump": "__stump",
}

# `attrs.fullness` (0..100) to the derived file's suffix, following the game's own steps:
# `>66` full, `>33` partial2, `>0` partial1, and 0 is the empty BASE state. The step SET is
# per-prefab, which is why `mesh_candidates` walks downward through them rather than demanding
# the exact file — a two-step prefab simply has no `__partial2`. CONTRACT.
FULLNESS_STEPS = ((66, "__full"), (33, "__partial2"), (0, "__partial1"))


def read_instances(bundle_dir):
    """Stream `instances.jsonl`. A generator — a full city is over a million records."""
    return reader.read_jsonl(reader.layer_path(bundle_dir, "instances"))


def mesh_candidates(record):
    """The assetIds to try for this record's mesh, **best first**, base state last.

    Use it as "take the first one that exists on disk":

        for candidate in instances.mesh_candidates(record):
            path = os.path.join(mesh_dir, candidate + ".glb")
            if os.path.isfile(path):
                break

    The list is ordered rather than a single answer because **a variant file exists only where
    the prefab flags that state** (§6.1) — the step set is per-prefab, so a two-step stockpile
    has no `__partial2` and a tree prefab with no elderly form has no `__elderly`. Absence means
    the game has no such variant either, and the next candidate down is the right answer. Always
    ends with the bare `assetId`, so this never returns nothing.

    `treeState` and `fullness` are independent and no prefab carries both, so whichever is
    present drives the list.
    """
    asset_id = record["assetId"]
    attrs = record.get("attrs") or {}
    candidates = []

    suffix = TREE_STATE_SUFFIX.get(attrs.get("treeState"))
    if suffix:
        candidates.append(asset_id + suffix)

    fullness = attrs.get("fullness")
    if isinstance(fullness, (int, float)):
        # Walk DOWN from the step this instance is at, so a prefab missing the exact step falls
        # to the next one it does have rather than all the way to empty.
        for threshold, step_suffix in FULLNESS_STEPS:
            if fullness > threshold:
                candidates.append(asset_id + step_suffix)

    candidates.append(asset_id)
    return candidates


def growth_scale(record):
    """The uniform scale to apply on top of the record's own transform — 1.0 for most things.

    `attrs.growthScale` is how the game draws a tree that has not finished growing, and it is
    NOT folded into `scale` (a tree record has no `scale` at all). `instance_matrix` applies it;
    this is here for a consumer composing its own transform.
    """
    value = (record.get("attrs") or {}).get("growthScale")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0.0 else 1.0


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
    """This record's world matrix, with `growth_scale` and `placement_lift` already applied.

    `zup`/`mesh_basis` are handed to `frames.zup_instance_matrix` for a Z-up consumer; the
    default is the contract's own bundle-frame matrix. This is the function to call rather than
    `frames.instance_matrix` directly, because both of the corrections it folds in are silent
    when missed: a forgotten decal lift flickers, and a forgotten growth scale draws 299,972
    saplings at full height without anything looking obviously broken.
    """
    matrix = (frames.zup_instance_matrix(record, mesh_basis) if zup
              else frames.instance_matrix(record))
    scale = growth_scale(record)
    if scale != 1.0:
        # Uniform, and pre-multiplied into the linear part only — it scales the MESH about the
        # instance's own origin, so the position must not move with it.
        matrix = matrix.copy()
        matrix[:3, :3] *= scale
    lift = placement_lift(record)
    if lift:
        # The up axis differs between the two frames: Z for a Z-up consumer, Y in the bundle.
        matrix = matrix if scale != 1.0 else matrix.copy()
        matrix[2 if zup else 1, 3] += lift
    return matrix
