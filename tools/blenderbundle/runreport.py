"""What an import says it did, from the selection survey to the closing recap.

The counts on these lines are what `tools/ab_compare.py --counts` gates on, so their wording is
load-bearing in both directions: a line that gains a COUNT_MARKERS substring becomes gated, and
one that loses it stops being.
"""

from . import shadernodes
from .log import LOG


def selection(where):
    """The bounds, centroid and densest cell of the selection, before anything is placed."""
    if where is None:
        return
    min_x, max_x, min_z, max_z = where["bounds"]
    LOG.step(f"selected:  {where['count']} instance(s)")
    LOG.step(f"           X {min_x:.0f} .. {max_x:.0f}   Z {min_z:.0f} .. {max_z:.0f}")
    LOG.step(
        f"           centroid X {where['centroid'][0]:.0f} Z {where['centroid'][1]:.0f}"
        f"   densest 250 m cell X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f}"
        f" ({where['densest_count']} there)"
    )


def announce(opts, layers):
    """What this run is about to do, in the four lines the log has always opened with."""
    LOG.step(f"bundle:    {opts.bundle_dir}")
    LOG.step(f"meshes:    {layers.meshes}")
    LOG.step(
        f"kinds:     {sorted(opts.kinds) or 'none'}   "
        f"limit: {'none' if opts.limit is None else opts.limit}"
    )
    if opts.assets is not None:
        LOG.step(f"assets:    matching {opts.assets}")
    if opts.networks:
        LOG.step(f"networks:  {layers.networks}")
        LOG.step(
            f"           categories: "
            f"{'all' if opts.net_categories is None else sorted(opts.net_categories)}"
        )


def placed(opts, placer, where, counts):
    """The closing recap: the timing line, the repack, the per-layer counts and the two notes."""
    terrain_objects, area_objects, elapsed = counts
    # No phase is open at this point — Instances closed right after its loop, and whichever
    # of Networks/Lanes/Terrain/Areas ran last closed itself too — so LOG.done() adds no
    # elapsed suffix of its own here; `elapsed` (measured from before the instance loop,
    # across the whole pipeline) is the only timing this line carries.
    LOG.done(
        f"placed {placer.counts['placed']} instance(s) from {len(placer.prefabs)} prefab(s) "
        f"in {elapsed:.1f} s"
    )
    # After every glTF import, because that importer loads its own textures and leaves them
    # premultiplying colour by alpha. See `shadernodes.unpremultiply` — worth several stops.
    repacked = shadernodes.unpremultiply_all()
    if repacked:
        LOG.step(
            f"{repacked} texture(s) set to channel-packed alpha so their colour is not "
            f"multiplied by packed alpha data"
        )
    if opts.skirts:
        LOG.step(
            f"placed {placer.counts['skirts']} foundation skirt(s) (__base, as-authored 1 m "
            f"bands — the terrain drop is the consumer rule, contract §4.1)"
        )
    if opts.terrain:
        LOG.step(f"placed {terrain_objects} terrain object(s) (ground grid + water; §8)")
    if opts.areas:
        LOG.step(f"placed {area_objects} surface-area object(s) (driveways, lots, paths; §8b)")
    LOG.step(
        f"skipped: {placer.counts['kind']} by kind, {placer.counts['asset']} by --assets, "
        f"{placer.counts['region']} outside the region"
    )
    _report_empty_region(opts, placer, where)
    _report_missing(placer.missing)


def _report_empty_region(opts, placer, where):
    """An empty-looking scene is nearly always the region filter, not the bundle. Say so, with
    the coordinates that would have worked. The absolute floor matters as much as the ratio: a
    deliberate 500 m window on a 14 km city keeps well under a twentieth of it by design, and
    tens of thousands of instances is not an empty scene — pointing that run at `--center auto`
    would have moved it from downtown to whichever field has the most trees."""
    placed = placer.counts["placed"]
    if opts.radius is None or where is None:
        return
    if placed >= where["count"] / 20 or placed >= 500:
        return
    LOG.warn(
        f"NOTE: the region kept {placed} of {where['count']} selected instance(s). "
        f"The densest 250 m cell is at X {where['densest'][0]:.0f} Z {where['densest'][1]:.0f} — "
        f"try --center auto, --center downtown, a larger --radius, or drop them."
    )


def _report_missing(missing):
    """Every assetId with no mesh, and the ones warnings.jsonl never declared.

    A gap the producer did not declare is a contract violation, not a missing file: the
    consumer had no way to know the asset was coming up short.
    """
    if not missing:
        return
    undeclared = [a for a, r in missing.items() if r == "NOT DECLARED in warnings.jsonl"]
    LOG.warn(f"{len(missing)} assetId(s) had no mesh ({len(undeclared)} of them undeclared):")
    for asset_id, reason in list(missing.items())[:15]:
        LOG.step(f"- {asset_id}: {reason}")
    if undeclared:
        LOG.warn(f"CONTRACT VIOLATION: {len(undeclared)} undeclared: {undeclared[:10]}")
