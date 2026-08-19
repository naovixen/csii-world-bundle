"""Which BeamNG object an instance record becomes, and the shape it binds.

CS2 has no counterpart to this decision — it batches every placed thing through one
`BatchInstanceSystem` path and never distinguishes a tree from a bollard from a tower block
(audit §62, row "forest vs TSStatic"). So **this rule is ours**, a BeamNG-side one, and it is
written down here rather than spread over the two stages that ask it.

BeamNG offers two ways to place a static:

* **`Forest`** — one line per item in `forest/<type>.forest4.json`, drawn through the forest
  renderer with its own batching and imposters. Vanilla levels place up to 663k of them. The
  cost is what an item can say: `pos`, a 3x3 `rotationMatrix`, **one scalar `scale`**, and a
  `type` naming a `TSForestItemData`. No per-item colour, no non-uniform scale.
* **`TSStatic`** — a full scene object: `instanceColor`/`1`/`2`, a 3-vector `scale`, its own
  collision setting. Vanilla levels top out near 22k of them, and the plan budgets 25k.

A city has 1.2M placed objects, so almost everything must be a forest item; the interesting
part is what may not be:

| condition | placement | why |
|---|---|---|
| `kind == "decal"` | neither | the bake already draped it into a `markings` tile |
| `attrs.state == "construction"` | neither | `instances.should_draw` — CS2 draws the site, not the building |
| `kind == "building"` | TSStatic | 7.8k of them, each wants its own paint and a real collider |
| non-uniform `scale` | TSStatic | a forest item carries one scalar, and 7,008 props are scaled per axis |
| otherwise | forest | trees and props |

Per-instance paint survives the forest through the bake's **variant** shapes: `paint_variants.json`
clusters each prefab's `colorSet`s into K centres and the `materials` bake writes a tinted albedo
per centre, so a forest type is `(assetId, state, variant)` and an instance picks its own by
`tints.variant_index`. That is a quantisation of the game's continuous per-instance colour, and
it is the approximation this file exists to keep countable — see `variant_of`.
"""

from csiibundle import instances as bundle_instances
from csiibundle import tints

# Kinds the bake has already turned into world-space tiles; the object stages must not place
# them a second time. CONTRACT with `csiibundle.bake.markings`.
BAKED_KINDS = ("decal",)

# Kinds that always become a TSStatic, whatever else is true of the record.
TSSTATIC_KINDS = ("building",)


def is_baked(record):
    """Whether another stage already placed this record (so the object stages skip it)."""
    return record.get("kind") in BAKED_KINDS


def has_nonuniform_scale(record, tolerance=1e-4):
    """Whether the record's `scale` cannot be expressed as a forest item's single scalar."""
    scale = record.get("scale")
    if not scale:
        return False
    values = [float(v) for v in scale[:3]]
    return (max(values) - min(values)) > tolerance * max(1.0, max(abs(v) for v in values))


def uniform_scale(record):
    """The one scalar a forest item can carry: the record's scale (mean if it is not quite
    uniform) times the tree's `growthScale`. 1.0 for the overwhelming majority."""
    scale = record.get("scale")
    base = (sum(float(v) for v in scale[:3]) / 3.0) if scale else 1.0
    return base * bundle_instances.growth_scale(record)


def wants_forest(record, forest_allowed=True):
    """Whether this record should be a forest item rather than a TSStatic. See the table above."""
    if not forest_allowed:
        return False
    if record.get("kind") in TSSTATIC_KINDS:
        return False
    return not has_nonuniform_scale(record)


def variant_of(record, centres):
    """Which baked paint variant this record's `colorSet` belongs to (`tints.variant_index`).

    Measured against the bake's own k-means assignment over the reference city: **99.9987 %** of
    1,138,589 coloured instances land on the centre the bake counted them under (15 records on a
    tie). The residual approximation is the clustering itself, not this lookup — an instance is
    drawn in its centre's colour, not its own.
    """
    return tints.variant_index(record.get("attrs") or {}, centres)


def shape_key(asset_id, state_suffix="", variant=None):
    """The name of the `.dae` (and of the forest type) for one mesh state and paint variant.

    `variant=None` is the UNTINTED shape — the prefab's own atlas materials — and every tinted
    variant is suffixed, **index 0 included**. Letting variant 0 share the untinted name looked
    tidier and lost its tint: a second variant with no baked albedo also falls back to the plain
    name, and whichever was written last won (region build 2026-08-18, 21 unbound materials).
    """
    name = asset_id + (state_suffix or "")
    if variant is not None:
        name = f"{name}_v{int(variant)}"
    return name


def in_region(record, region):
    """Whether the record's position falls inside a `{centerX, centerZ, radius}` region box.
    The same square test the tile stages use, so a region build is consistent across stages."""
    if not region:
        return True
    pos = record["pos"]
    return (abs(float(pos[0]) - float(region["centerX"])) <= float(region["radius"])
            and abs(float(pos[2]) - float(region["centerZ"])) <= float(region["radius"]))


def kind_enabled(kind, settings):
    """Whether the settings ask for this record kind at all (`trees` / `props` / `buildings`)."""
    if kind == "tree":
        return bool(settings.get("trees", True))
    if kind == "building":
        return bool(settings.get("buildings", True))
    if kind == "prop":
        return bool(settings.get("props", True))
    return True


def density_fraction(kind, settings):
    """The density slider that governs this kind, as a fraction."""
    key = "foliage_density" if kind == "tree" else "prop_density"
    return max(0.0, min(1.0, float(settings.get(key, 100) or 0) / 100.0))


def admit(record, settings, region=None, clipped=None, orphans=None):
    """`None` if this record is placed, else the short reason it is not.

    The whole admission test in one place: baked elsewhere, drawn at all, wanted by the settings,
    inside the region, surviving its density slider, clear of the carriageway. Pure, so the
    shapes stage and the instances stage reach the same answer without carrying a list of 1.2
    million decisions between them — and one function, so the two passes cannot drift apart.

    `orphans` is the set of record ids whose owner the producer declared ABSENT
    (`csiibundle.instances.orphans_of_absent_owners`) — a building we could not deliver, whose
    props we would otherwise place in mid-air. Not a CS2 rule: an obligation against a declared
    gap (§10).

    `clipped` is the set of record ids `foliage_road_clearance` removed, computed ONCE per build
    (`csiibundle.roadclear`) and handed to both passes. A set rather than a live query because
    the spatial test is vectorised over the whole layer — asking it 1.2 million times one record
    at a time would be the same answer several orders of magnitude slower.
    """
    if is_baked(record):
        return "baked"
    if not bundle_instances.should_draw(record):
        return "state"
    kind = record.get("kind")
    if not kind_enabled(kind, settings):
        return "kind"
    if not in_region(record, region):
        return "region"
    if kind in ("tree", "prop") and not bundle_instances.density_keep(
            record, density_fraction(kind, settings)):
        return "density"
    if clipped and record.get("id") in clipped:
        return "roadclear"
    if orphans and record.get("id") in orphans:
        return "orphan"
    return None


def keep(record, settings, region=None, clipped=None, orphans=None):
    """Whether this record is placed at all — `admit` as a yes/no."""
    return admit(record, settings, region, clipped, orphans) is None


def road_clipped(bundle, settings, region=None):
    """The set of record ids `foliage_road_clearance` removes, plus the counts for the report.

    Returns `(ids, stats)`; an empty set and a zeroed stats block when the setting is 0 (off),
    without reading the lane layer at all. DECLARED DEVIATION — see `csiibundle.roadclear`.
    """
    from csiibundle import reader, roadclear

    clearance = float(settings.get("foliage_road_clearance") or 0.0)
    stats = {"clearance": clearance, "laneStations": 0, "trees": 0, "clipped": 0}
    if clearance <= 0.0:
        return set(), stats
    lanes_path = reader.layer_path(bundle, "lanes")
    if not lanes_path:
        return set(), stats
    points = roadclear.lane_points(reader.read_jsonl(lanes_path))
    stats["laneStations"] = int(len(points))
    if not len(points):
        return set(), stats
    grid = roadclear.PointGrid(points, clearance)

    ids = set()
    batch = []
    for record in bundle_instances.read_instances(bundle, kind="tree"):
        if record.get("kind") != "tree" or not bundle_instances.is_tree(record):
            continue
        stats["trees"] += 1
        batch.append(record)
        if len(batch) >= 100000:
            _absorb_clipped(batch, grid, clearance, ids)
            batch = []
    _absorb_clipped(batch, grid, clearance, ids)
    stats["clipped"] = len(ids)
    return ids, stats


def _absorb_clipped(batch, grid, clearance, ids):
    from csiibundle import roadclear

    if not batch:
        return
    hit = roadclear.obstructing(batch, grid, clearance)
    for record, blocked in zip(batch, hit):
        if blocked and record.get("id") is not None:
            ids.add(record["id"])
