"""Place the §4 instances: one collection-instance empty per record, one mesh per prefab.

A city has thousands of trees and a handful of tree prefabs, so every record becomes an empty
pointing at a shared collection — one object in the depsgraph, one copy of the geometry in
memory, and a multi-part prefab keeps its node hierarchy and per-part offsets for free.
"""

import math
import os

import bpy
from csiibundle import instances
from csiibundle.reader import read_jsonl

from . import frame, glbimport, materiallayer
from .log import LOG


class _Placer:
    """The per-record work and the tallies it keeps, over one pass of instances.jsonl."""

    def __init__(self, options, mesh_dir, collections, declared_gaps):
        self.options = options
        self.mesh_dir = mesh_dir
        self.sources, self.placed_collection, self.decal_collection = collections
        self.declared_gaps = declared_gaps
        self.prefabs = {}
        self.skirt_prefabs = {}
        self.missing = {}
        self.counts = dict.fromkeys(("placed", "skirts", "kind", "region", "asset", "state"), 0)

    def _collection(self, cache, asset_id):
        """The prefab collection for one asset id, imported once and remembered (None if absent)."""
        if asset_id not in cache:
            path = os.path.join(self.mesh_dir, asset_id + ".glb")
            cache[asset_id] = (
                glbimport.load_prefab_collection(asset_id, self.mesh_dir, self.sources)
                if os.path.isfile(path)
                else None
            )
        return cache[asset_id]

    def _skip(self, record):
        """The name of the filter that rejects this record, or None to place it."""
        if record.get("kind") not in self.options.kinds:
            return "kind"
        # Substring rather than exact match: a family like the elevated-road supports spans
        # Vertical_Pillar, Pillar_Top_16, Horizontal_Pillar_12 …, and `--assets pillar` should
        # bring the whole family without having to enumerate it.
        if self.options.assets is not None:
            lowered = record["assetId"].lower()
            if not any(a in lowered for a in self.options.assets):
                return "asset"
        pos = record["pos"]
        center, radius = self.options.center, self.options.radius
        if radius is not None and center is not None:
            if math.hypot(pos[0] - center[0], pos[2] - center[1]) > radius:
                return "region"
        # §4 D4: `state: "construction"` means the game draws NO building here — the site's
        # crane and fence are separate records the bundle already carries.
        if not instances.should_draw(record):
            return "state"
        return None

    def _state_collection(self, record):
        """The collection for the state this record is in, or None with the gap recorded.

        §6.1/§9: a `.glb` is ONE STATE of a prefab, and this record says which state it is
        in. `mesh_candidates` orders the derived files best-first (a tree's `__elderly`, a
        stockpile's `__partial2`) with the base state last, and a variant file exists only
        where the prefab flags that state — so this takes the first one that is actually on
        disk. Without it 153,991 of this city's 697,256 trees draw as healthy adults.
        """
        asset_id = record["assetId"]
        for candidate in instances.mesh_candidates(record):
            if self._collection(self.prefabs, candidate) is not None:
                return candidate, self.prefabs[candidate]
        self.missing[asset_id] = self.declared_gaps.get(asset_id, "NOT DECLARED in warnings.jsonl")
        return asset_id, self.prefabs.get(asset_id)

    def _empty(self, name, collection, record):
        """One collection-instance empty at the record's own transform, carrying its paint."""
        empty = bpy.data.objects.new(name, None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = collection
        empty.empty_display_size = 0.01
        empty.matrix_world = frame.instance_matrix(record)
        materiallayer.set_paint(empty, record.get("attrs"))
        return empty

    def _place_skirt(self, record):
        """The §4.1 foundation skirt: `<assetId>__base.glb` at the same transform.

        The band is shown as authored (a 1 m plinth); dropping its v=0 ring to the terrain
        raster is a consumer's job, and this harness has no terrain — so slopes will still show
        a gap below the band, and that is expected.
        """
        if not (self.options.skirts and instances.wants_skirt(record)):
            return
        base_id = instances.skirt_asset_id(record)
        collection = self._collection(self.skirt_prefabs, base_id)
        if collection is None:
            return
        name = f"{base_id}.{record.get('id', self.counts['placed'])}"
        self.placed_collection.objects.link(self._empty(name, collection, record))
        self.counts["skirts"] += 1

    def place(self, record):
        """One record: filter it, place its empty and its skirt, or count why not."""
        skipped = self._skip(record)
        if skipped is not None:
            self.counts[skipped] += 1
            return
        asset_id, collection = self._state_collection(record)
        if collection is None:
            return

        empty = self._empty(
            f"{asset_id}.{record.get('id', self.counts['placed'])}", collection, record
        )
        self._place_skirt(record)
        # Decals go in their own collection. The producer ships a decal as the flat quad it
        # paints rather than as the projector volume it paints through, so these now lie on the
        # carriageway and read as the road markings they are — but they are still paint, not
        # surface, and must never become collision geometry in anything built from this.
        # Separating them keeps that one command away: hide the collection and the markings go,
        # and nothing else moves.
        if record.get("kind") == "decal":
            self.decal_collection.objects.link(empty)
        else:
            self.placed_collection.objects.link(empty)
        self.counts["placed"] += 1
        LOG.progress("placed", self.counts["placed"], self.options.limit)


def place_instances(options, instances_path, mesh_dir, collections, declared_gaps):
    """Walk instances.jsonl once, placing what the options select. Returns the `_Placer`."""
    placer = _Placer(options, mesh_dir, collections, declared_gaps)
    limit = options.limit
    for record in read_jsonl(instances_path) if options.kinds else ():
        if limit is not None and placer.counts["placed"] >= limit:
            break
        placer.place(record)
    # Not a gate-marked line (no "instance(s)"/"prefab(s)"): the closing summary the count
    # gate actually checks is the "placed N instance(s) …" line at the very end of the run,
    # which already existed before `Log` and must stay exactly where it was, after every
    # other phase's own summary.
    LOG.done(f"{placer.counts['placed']} placed, {len(placer.prefabs)} mesh source(s) loaded")
    glbimport.counts_log()
    return placer
