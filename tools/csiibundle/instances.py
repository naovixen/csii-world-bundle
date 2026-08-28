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

**`attrs.state` is a RENDER branch, not metadata**, and one of its values means "draw nothing".
Every rule below is `BatchInstanceSystem`'s own branch:

* `normal` / `condemned` — draw it. `Condemned` has no reader in the rendering namespace at all;
  it is a simulation flag about eviction.
* `abandoned` — draw it normally. The game's abandoned colour and décor variant are **already
  resolved into the shipped `colorSet`**, so nothing extra is needed.
* `construction` — **do not draw the building.** The game skips it. The site is not empty: the
  crane and site fence are real entities with their own prefabs and ship as ordinary instance
  records, so the bundle already contains everything visible there. Drawing the building puts a
  finished tower on a construction site.
* `destroyed` — neither the collapse pose nor rubble is shipped (the collapse is a runtime
  deformation, and no rubble mesh exists as a prefab). Draw as `normal` geometry or skip it; do
  not expect ruins.

`should_draw()` implements that. On the reference city it changes nothing — 8,889 `normal` and
7 `condemned`, none of the other three — which is why this is a documented rule rather than a
measured one, and why it must come from the contract instead of from what one save happens to
contain.

**`attrs.elevation` is how far the object stands ABOVE the ground, not the ground's height.**
Read as terrain height it gives a city where everything sits at a couple of metres. Ground level
under an instance is `pos[1] - elevation`, and it is SIGNED: on the 2026-08-12 city, of 483,351
records carrying it, 970 are negative, down to −86.3 m — the underground parts of transit
stations, and a scatter of buried props and decals. (Re-measured 2026-08-14; this said "29,610
of 517,398", which were the 2026-08-08 rip's figures. The −86.3 m floor is unchanged. What a
consumer needs is the sign rule, which is not in question.)

**A missing mesh is not automatically a defect.** Check `reader.read_declared_gaps` first: the
producer declares what it could not deliver (§10), and re-reporting a declared gap as an error
makes the error count a function of the producer's honesty. An UNDECLARED missing mesh is real.
"""

import os

from . import decals, frames, reader
from .density import (  # noqa: F401  (re-export: the density subset rule)
    DENSITY_WEIGHT,
    density_keep,
    density_rank,
    object_radius,
)
from .orphans import absent_prefabs, orphans_of_absent_owners  # noqa: F401  (re-export)
from .skirt import (  # noqa: F401  (re-export: the §4.1 skirt rules)
    ON_GROUND,
    SKIRT_RING_FRACTION,
    SKIRT_SUFFIX,
    drop_skirt,
    skirt_asset_id,
    skirt_flags_index,
    skirt_rings,
    wants_skirt,
    wants_skirt_with_parents,
)

# §4's closed `kind` set. Listed rather than derived so a v2 addition shows up as a deliberate
# edit instead of silently joining a wildcard. CONTRACT.
KINDS = ("building", "prop", "tree", "fence", "decal", "poi")

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


def read_instances(bundle_dir, kind=None):
    """Stream `instances.jsonl`. A generator — a full city is over a million records.

    `kind` narrows the stream to one `kind` ("building", "decal", "tree", "prop") the cheap way:
    a line that does not contain that word cannot be a record of that kind, so it is never
    parsed. The caller must still test `record["kind"]` — the prefilter is a superset (a prop
    whose brand is the word "building" survives it), which is exactly what keeps it exact.
    """
    return reader.read_jsonl(
        reader.layer_path(bundle_dir, "instances"), contains=None if kind is None else f'"{kind}"'
    )


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


def first_mesh_state(record, mesh_dir, probed):
    """The first of `mesh_candidates` with a `.glb` in `mesh_dir`, or `None` if none has one.

    The "take the first one that exists on disk" loop the docstring above describes, written
    once. It was written three times instead — `stages/shapes._state_suffix`,
    `stages/instances._state_suffix` and `pipeline/meshindex._state_stem` — and they did not
    agree on what to return: two sliced the prefab off (`candidate[len(asset):]`, the state
    SUFFIX) and one returned the whole stem. This returns the **candidate**, which is what
    `mesh_candidates` yields; a caller that wants the suffix slices it, at the call site, where
    the asset it is slicing off is in scope.

    `probed` is a caller-owned `{candidate: bool}` dict, required rather than optional because
    every caller runs this over a million records and one file probe per distinct name is the
    difference between a pass and a stall. Share it across the whole walk.
    """
    for candidate in mesh_candidates(record):
        have = probed.get(candidate)
        if have is None:
            have = probed[candidate] = os.path.isfile(os.path.join(mesh_dir, candidate + ".glb"))
        if have:
            return candidate
    return None


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


def is_tree(record):
    """Whether CS2 considers this record a TREE, as opposed to any other planting.

    **The `tree` kind is all vegetation, not trees.** On the reference city it covers 686,641
    items over 75 prefabs, and 376,187 of those items are `pd_Long_Grass`, `FlowerBushCity02`,
    `PlantPot01`, `Flowerbed01` and friends. A consumer that treats the kind as "tree" gives a
    kerb planter a trunk and makes a hedge solid.

    THE GAME'S OWN RULE, read from `Game.Prefabs` rather than inferred:

        TreeObject.GetArchetypeComponents  -> Plant, Tree, Color, UpdateFrame, Decoration
        PlantObject.GetArchetypeComponents -> Plant, UpdateFrame

    Every tree is a plant; only a tree gets `Game.Objects.Tree`, which is the struct holding
    `m_State` and `m_Growth`. The producer writes `attrs.growth` inside exactly that component's
    `TryGetComponent` (`InstanceRipSystem`), so **the key's presence is the game's own
    discriminator** — not a heuristic, and not a name match.

    Do NOT match names, and do NOT test the value. A name match gets
    `VFP_EvergreenBushWildLarge` (8,806 items) backwards — it HAS growth, so CS2 treats it as a
    tree whatever it is called. And a sapling's growth is 0.0, which is falsy, so a truth test
    would quietly reclassify the youngest trees as shrubs.
    """
    return "growth" in (record.get("attrs") or {})


def vegetation_kind(record):
    """`"tree"`, `"plant"`, or `None` for a record that is not vegetation at all.

    The three-way answer both consumers want: `kind == "tree"` says "this is planting", and
    `is_tree` splits it the way the game does.
    """
    if record.get("kind") != "tree":
        return None
    return "tree" if is_tree(record) else "plant"


# `attrs.state` values whose buildings the game does not draw at all. CONTRACT (§4, D4).
NOT_DRAWN_STATES = ("construction",)


def should_draw(record):
    """Whether the game draws this record's own mesh at all.

    False for two reasons:

    * `state: "construction"`, where CS2 skips the building and draws the site's own crane and
      fence entities instead — which the bundle ships separately as ordinary records. See the
      module docstring for every state's rule.
    * `attrs.overridden`, which is CS2's `Game.Common.Overridden`: the mark on a tree, prop or
      lane that something else was built over. `Game.Rendering.PreCullingSystem` leaves such an
      entity's culling mask at `BoundsMask.Debug` and never ORs in the prefab's real mesh
      layers, at both of the two sites that test it — and `BoundsMask` carries an explicit
      `NotOverridden` bit. The entity still exists and still has a transform; the game simply
      does not render it. This is where the tree standing in a carriageway comes from.

    REQUIRES A RIP CARRYING `attrs.overridden` (producer, 2026-08-18). An older bundle has no
    such key, so this reduces to the construction rule and nothing changes — which is correct,
    not a silent fallback: the older bundle genuinely does not know.
    """
    attrs = record.get("attrs") or {}
    if attrs.get("overridden"):
        return False
    return attrs.get("state") not in NOT_DRAWN_STATES


def ground_level(record):
    """World height of the ground under this instance, or `None` when it is not recorded.

    `pos[1] - attrs.elevation`. Returns `None` rather than `pos[1]` when `elevation` is absent,
    because absent means "sitting flat on the ground" for most records but says nothing for the
    rest, and a consumer should decide that itself rather than inherit a guess.
    """
    elevation = (record.get("attrs") or {}).get("elevation")
    if elevation is None:
        return None
    return float(record["pos"][1]) - float(elevation)


def decal_layers(record):
    """Which surfaces a `decal` instance may paint onto — `("Terrain",)`, `("Roads",)`, ….

    CS2 targets a projection at named layers, and honouring them is what keeps a road marking
    off the wall behind it: a decal whose layers are `["Roads"]` must not be painted across
    terrain or buildings. Empty tuple when the record does not say.
    """
    return tuple((record.get("attrs") or {}).get("decalLayers") or ())


def decal_height(record):
    """Height in metres of the projector volume this decal was projected through (§4.2a).

    The reach a consumer building a real projector needs; one draping the quad as geometry does
    not. `None` when absent.
    """
    value = (record.get("attrs") or {}).get("decalHeight")
    return float(value) if value is not None else None


def placement_lift(record):
    """Metres to raise this record along the up axis before drawing it, from the lift ladder.

    Non-zero only for `decal` instances, which are coplanar paint on a surface they would
    otherwise z-fight. Everything else is 0.0 and needs no special case.
    """
    return decals.LIFT["instance_decal"] if record.get("kind") == "decal" else 0.0


def decal_priority(record):
    """`attrs.decalPriority` — the game's sort key for overlapping decals, or `None`.

    `None` means the prefab carries no `DecalProperties` at all, which the contract distinguishes
    from priority zero; `decals.instance_priority_lift` treats it as "no opinion".
    """
    value = (record.get("attrs") or {}).get("decalPriority")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def instance_matrix(record, mesh_basis=None, zup=False):
    """This record's world matrix, with `growth_scale` and `placement_lift` already applied.

    `zup`/`mesh_basis` are handed to `frames.zup_instance_matrix` for a Z-up consumer; the
    default is the contract's own bundle-frame matrix. This is the function to call rather than
    `frames.instance_matrix` directly, because both of the corrections it folds in are silent
    when missed: a forgotten decal lift flickers, and a forgotten growth scale draws 299,972
    saplings at full height without anything looking obviously broken.
    """
    matrix = (
        frames.zup_instance_matrix(record, mesh_basis) if zup else frames.instance_matrix(record)
    )
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
