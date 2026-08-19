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


def read_instances(bundle_dir, kind=None):
    """Stream `instances.jsonl`. A generator — a full city is over a million records.

    `kind` narrows the stream to one `kind` ("building", "decal", "tree", "prop") the cheap way:
    a line that does not contain that word cannot be a record of that kind, so it is never
    parsed. The caller must still test `record["kind"]` — the prefilter is a superset (a prop
    whose brand is the word "building" survives it), which is exactly what keeps it exact.
    """
    return reader.read_jsonl(reader.layer_path(bundle_dir, "instances"),
                             contains=None if kind is None else f'"{kind}"')


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


def absent_prefabs(bundle_dir):
    """assetIds the producer declared **absent** — the prefab itself did not resolve (§10).

    NOT the same as "declared undeliverable". `InstanceRipSystem.InstanceDeclarations` emits
    `kind: "prefab"` for two different gaps, and only one means the object is not there:

    | kind | reason | what it means |
    |---|---|---|
    | `prefab` | *managed prefab did not resolve* | **absent** — an unowned asset pack; assetId is a synthetic `unresolved_<n>` |
    | `prefab` | *prefab has no SubMesh buffer* | it EXISTS and simply has no mesh of its own |
    | `mesh` | *none of N sub-mesh(es) yielded geometry* | same — it exists |

    Measured on the reference city: 87 absent against 117 that exist without geometry. Keying on
    `kind` alone would also drop the children of prefabs that are genuinely there — and those
    children are real: the second and third rows are `ParkingLot02..14`, `CampfireSite01/02`,
    `AmericanFootballField01`, whose paving ships in the `areas` layer while their lamps, benches
    and planting ship here. `read_declared_gaps` cannot answer this because it collapses to
    `{assetId: reason}`, last line wins, and an absent prefab carries a mesh warning too.
    """
    from . import reader

    path = reader.layer_path(bundle_dir, "warnings")
    absent = set()
    if not path or not os.path.isfile(path):
        return absent
    for record in reader.read_jsonl(path):
        asset = record.get("assetId")
        if not isinstance(asset, str):
            continue
        if record.get("kind") == "prefab" and "did not resolve" in (record.get("reason") or ""):
            absent.add(asset)
    return absent


def orphans_of_absent_owners(bundle_dir):
    """`(ids, stats)` — records whose owner chain reaches a prefab the producer declared absent.

    **A record whose owner was not placed because the producer could not deliver it is not
    placeable either.** This is not a CS2 rule to port — the game had those prefabs and drew the
    buildings — it is a consumer's obligation against a DECLARED gap (§10). Without it we drop
    the building and place its props anyway, and on the reference city that is 6,714 records
    hanging in clear sky, up to 292 m up, 695 of them belonging to one absent tower.

    Only the absent class is dropped (`absent_prefabs`). A prefab that exists without geometry
    keeps its children — 5,806 of them, the lamps and benches of car parks and sports pitches
    whose own surface ships in the `areas` layer — and a construction site keeps its children
    too, which measurement says are the `ConstructionCrane01` and the site's benches: §4 states
    the crane and site fence are real entities, and they are exactly the tall ones.

    Ownership is transitive and shallow — depth 4 at most on the reference city, 96 % of it depth
    2 — but a sub-object of a sub-object is legal, so the walk follows `parent` to the root.
    """
    absent = absent_prefabs(bundle_dir)
    stats = {"absentPrefabs": len(absent), "records": 0, "withParent": 0,
             "orphans": 0, "owners": 0}
    if not absent:
        return set(), stats

    parent_of, absent_ids = {}, set()
    for record in read_instances(bundle_dir):
        rid = record.get("id")
        if rid is None:
            continue
        stats["records"] += 1
        if record.get("assetId") in absent:
            absent_ids.add(rid)
        parent = record.get("parent")
        if parent is not None:
            parent_of[rid] = parent
            stats["withParent"] += 1

    ids, owners = set(), set()
    for rid in parent_of:
        cur, seen = parent_of.get(rid), set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in absent_ids:
                ids.add(rid)
                owners.add(cur)
                break
            cur = parent_of.get(cur)
    stats["orphans"] = len(ids)
    stats["owners"] = len(owners)
    return ids, stats


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


# -- skirts: rings, parent flags, the drop -----------------------------------------------------

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
    top_level = float(out[~bottom, up_axis].min()) if (~bottom).any() else float(out[:, up_axis].max())
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


# -- density subsets and object size -----------------------------------------------------------

# Weights that decide which instances survive FIRST as the density slider comes down. A lower
# fraction is always a subset of a higher one (keep iff hash < fraction * weight), so thinning
# is stable and monotone. Trees: mature states over young ones. TUNED.
DENSITY_WEIGHT = {
    "Adult": 1.0, "Elderly": 1.0, None: 1.0,
    "Teen": 0.75, "Child": 0.5, "Dead": 0.9, "Stump": 0.4,
}


def _stable_unit(value):
    """A stable float in [0, 1) from an int id (or any repr) — the same on every platform,
    unlike Python's salted `hash`."""
    import hashlib
    digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") / float(1 << 64)


def density_keep(record, fraction):
    """Whether to keep this record at foliage/prop density `fraction` (0..1).

    Deterministic per record `id`, monotone in `fraction`, and weighted so that mature trees
    survive longer than saplings. `fraction >= 1` keeps everything; `<= 0` keeps nothing.
    """
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    state = (record.get("attrs") or {}).get("treeState")
    weight = DENSITY_WEIGHT.get(state, 1.0)
    return _stable_unit(record.get("id")) < fraction * weight


def object_radius(bounds_min, bounds_max, scale=None):
    """Half the bounding-box diagonal of a mesh, times its largest scale factor — the size a
    consumer uses to pick imposter/cull thresholds and collider simplicity per object."""
    import numpy
    lo = numpy.asarray(bounds_min, dtype=numpy.float64)
    hi = numpy.asarray(bounds_max, dtype=numpy.float64)
    radius = 0.5 * float(numpy.linalg.norm(hi - lo))
    if scale is not None:
        s = numpy.asarray(scale, dtype=numpy.float64).ravel()
        radius *= float(numpy.abs(s).max()) if s.size else 1.0
    return radius
