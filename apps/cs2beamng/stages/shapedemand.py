"""What shapes this build actually needs.

One pass over the instance layer deciding, per record, which `(assetId, mesh state, paint
variant)` triples are placed at all. Everything downstream is keyed on that set, so a shape that
is written and never placed is pure build time and pure residency.
"""

import json
import os

from csiibundle import instances as bundle_instances

from ..beamng import placement


def units(census, _settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("instances") or 0) or None


# -- the demand scan ------------------------------------------------------------------------------


def scan_demand(ctx, progress):
    """One pass over `instances.jsonl`: which `(assetId, state)` the build places, how many of
    each, which paint variants are wanted, and whether the placement is forest or TSStatic.

    Returns `(demand, stats, dropped)` where `demand` is
    `{(asset_id, suffix): {"kind", "count", "forest", "variants": {index: count}}}` and `dropped`
    is the union of the record ids the road-clearance deviation removed and those orphaned by an
    absent owner — both are per-RECORD decisions the later passes must reach identically, and one
    set is enough because `admit` tests membership either way.
    """
    settings = ctx.settings
    region = ctx.region
    mesh_dir = os.path.join(ctx.bundle, "meshes")
    have = {}  # asset+suffix -> bool, cached file probes
    variants = paint_variants(ctx)
    forest_allowed = True
    demand = {}
    stats = _empty_stats()
    # A6.6 — records whose owner the producer declared ABSENT (§10). Computed once here beside
    # the road clearance and handed to the other passes through `shared`.
    orphans, orphan_stats = bundle_instances.orphans_of_absent_owners(ctx.bundle)
    stats["orphans"] = orphan_stats
    # The road-clearance deviation, computed ONCE here and handed to the instances stage through
    # `shared` — one spatial pass over the lane layer instead of two, and the two passes cannot
    # reach different answers because they consult the same set.
    clipped, clip_stats = placement.road_clipped(ctx.bundle, settings, region)
    stats["roadclear"] = clip_stats
    progress.begin(
        "shapes: which meshes the city places", total=units(ctx.census, settings), unit="instances"
    )
    for record in bundle_instances.read_instances(ctx.bundle):
        stats["records"] += 1
        if stats["records"] % 25000 == 0:
            progress.advance(25000)
            progress.check()
        # ONE admission test for both passes: `placement.admit` names the reason so this stage
        # can break the count down without restating the rule (they drifted apart the moment
        # they were two functions)
        reason = placement.admit(record, settings, region, clipped, orphans)
        if reason is not None:
            stats["skipped" + reason.capitalize()] += 1
            continue
        kind = record.get("kind")
        asset = record["assetId"]
        suffix = _state_suffix(record, asset, mesh_dir, have)
        if suffix is None:
            stats["noMesh"] += 1
            continue
        forest = placement.wants_forest(record, forest_allowed)
        _count_record(demand, record, kind, asset, suffix, forest, variants)
        stats["kept"] += 1
    progress.end(
        f"{stats['kept']:,} of {stats['records']:,} records over {len(demand):,} mesh states"
    )
    return demand, stats, clipped | orphans


def _empty_stats():
    """The counters `scan_demand` fills. The `skipped*` keys are `placement.admit`'s reasons."""
    return {
        "records": 0,
        "kept": 0,
        "noMesh": 0,
        "skippedBaked": 0,
        "skippedState": 0,
        "skippedKind": 0,
        "skippedDensity": 0,
        "skippedRegion": 0,
        "skippedRoadclear": 0,
        "skippedOrphan": 0,
    }


def _count_record(demand, record, kind, asset, suffix, forest, variants):
    """Add one admitted record to its `(asset, suffix)` demand entry."""
    entry = demand.get((asset, suffix))
    if entry is None:
        entry = demand[(asset, suffix)] = {
            "kind": kind,
            "count": 0,
            "forest": False,
            "static": False,
            "variants": {},
            "tree": False,
        }
    # `kind == "tree"` is ALL vegetation; only `is_tree` is the game's own tree (see
    # `csiibundle.instances.is_tree`). Measured per prefab on the reference city, no asset
    # is ever classified both ways, so one flag per demand entry is exact rather than a
    # majority vote.
    if kind == "tree" and bundle_instances.is_tree(record):
        entry["tree"] = True
    entry["count"] += 1
    entry["forest"] = entry["forest"] or forest
    entry["static"] = entry["static"] or not forest
    if forest:
        centres = (variants.get(asset) or {}).get("variants") or []
        if centres:
            index = placement.variant_of(record, centres)
            entry["variants"][index] = entry["variants"].get(index, 0) + 1


def _state_suffix(record, asset, mesh_dir, have):
    """The STATE SUFFIX of the first `mesh_candidates` entry on disk, or None if none is.

    The walk is `csiibundle.instances.first_mesh_state`; the slice is here because `asset` is
    what is being sliced off and it is in scope here, not there. Memoised in `have` on the
    three fields `mesh_candidates` reads — the same collapse `stages/instances._state_suffix`
    does, which this pass was missing: a candidate list per record, 1.28 M times.
    """
    attrs = record.get("attrs") or {}
    key = (asset, attrs.get("treeState"), attrs.get("fullness"))
    if key in have:
        return have[key]
    candidate = bundle_instances.first_mesh_state(record, mesh_dir, have)
    have[key] = None if candidate is None else candidate[len(asset) :]
    return have[key]


def paint_variants(ctx):
    path = os.path.join(ctx.derived_dir, "paint_variants.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("prefabs") or {}
    except (OSError, ValueError):
        return {}
