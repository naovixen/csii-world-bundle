"""What the lane phase says it did: the geometry summary, the drape, and the skips.

Split from `lanebuild` because it is a different job — every line here is a measurement of
something that already happened, and three of them carry a reason the number is what it is
rather than a description of the number.
"""

from csiibundle import drape

from .log import LOG


def geometry(builders, sources, tally, built, totals, tiles):
    """The phase summary, the tile split, and the two degenerate-face aggregates."""
    LOG.done(
        f"lanes:     {tally['bent']} record(s) bent into {built} object(s), "
        f"{len(sources)} part mesh(es) loaded"
    )
    if tiles["tiles"]:
        baked_tiles = sum(sink.get("tiles_baked", 0) for sink in builders.values())
        LOG.step(
            f"{tiles['tiles']} straight tile(s) instanced across {tiles['instancers']} "
            f"instancer(s) ({tiles['parts']} shared part mesh(es)); "
            f"{baked_tiles} tile(s) baked (curved, tapered, hung or clipped)"
        )
    if tiles["degenerate"]:
        LOG.step(
            f"{tiles['degenerate']} degenerate face(s) dropped from shared part "
            "meshes (authored in the source .glb)"
        )
    dropped_decals = sum(sink.get("decal_dropped", 0) for sink in builders.values())
    if dropped_decals:
        # New line vs pre-§39 baselines (cf. the degenerate-face note below for the A/B gate).
        LOG.step(
            f"{dropped_decals} decal span(s) dropped (tiling clip 0 — the game draws "
            "zero instances on a sub-half-period stub)"
        )
    if totals["faces"]:
        # tools/ab_compare.py's COUNT_MARKERS ("face(s)", "object(s)") match this line, so any
        # two logs being diffed with --counts must have the identical number of degenerate
        # faces/objects to compare equal — a run-to-run change here is a real signal, but note
        # for lane-region regression scripts: this line did not exist before this task, so an
        # otherwise-unchanged lane import will now always show ONE extra matched line versus a
        # pre-this-task baseline (see task-6-report.md "Fix round 1" for how that was handled
        # in this task's own A/B gate).
        LOG.step(
            f"{totals['faces']} zero-area degenerate face(s) dropped across "
            f"{totals['objects']} object(s) — a heavily tiled part's last "
            "tile clipping short of a full section, not a bundle defect"
        )


def drape_markings(counters, road_surface, ground_surface):
    """What the drape did, and the two ways it says it is not doing its job."""
    draped, drape_missed = counters["draped"], counters["missed"]
    drape_grounded = counters["grounded"]
    if draped or drape_missed or drape_grounded:
        LOG.step(
            f"{draped} decal vertex(es) draped onto the road surface"
            + (
                f", {drape_grounded} onto the terrain instead (no road under them)"
                if drape_grounded
                else ""
            )
            + (f", {drape_missed} kept the fixed lift (no surface hit)" if drape_missed else "")
        )
        # A miss is a marking floating or sunk at an authored height with nothing beneath it.
        # A few are expected at a region edge, where the marking is inside the window and the
        # road carrying it is not; a large share means the drape is not doing its job.
        total_drape = draped + drape_grounded + drape_missed
        if total_drape and drape_missed > total_drape * drape.MISS_WARN_FRACTION:
            LOG.warn(
                f"{drape_missed / total_drape * 100:.1f}% of decal vertices found no "
                f"surface within their own projector reach"
                + (
                    ""
                    if ground_surface is not None
                    else " — import --terrain as well and they fall back to the ground"
                )
            )
    elif road_surface is None:
        LOG.warn(
            "networks layer not built — decals keep the fixed lift, expect "
            "markings to z-fight or sink into the crown"
        )
    if road_surface is not None and road_surface.kerb_rejections:
        # A low number here is the truth, not blindness: only 2.1% of drape samples have more
        # than one object under them even at the city's densest junction, and 0% in a plain
        # road window (tools/test_drape_pick.py). Nothing to reject, nothing counted.
        LOG.step(
            f"{road_surface.kerb_rejections} drape sample(s) where the nearest-surface "
            "pick differed from the highest hit in range (Tier 0: kerb/wall snap avoided)"
        )


def skips(counts, tally, lane_mesh_dir):
    """Per-category totals, the two region/limit skips, and the two contract violations."""
    for category, count in sorted(counts.items()):
        LOG.step(f"category {category}: {count}")
    if tally["region"]:
        LOG.step(f"{tally['region']} skipped outside the region")
    if tally["limit"]:
        LOG.step(f"{tally['limit']} skipped by --net-limit")
    if tally["missing_prefab"]:
        LOG.warn(
            f"CONTRACT VIOLATION: {tally['missing_prefab']} record(s) name a prefab "
            f"missing from laneprefabs.json"
        )
    if tally["missing_mesh"]:
        LOG.warn(
            f"CONTRACT VIOLATION: {tally['missing_mesh']} part reference(s) missing "
            f"their .glb under {lane_mesh_dir}"
        )
