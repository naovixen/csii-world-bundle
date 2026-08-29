"""Running a per-record pass over the laneGeometry layer across worker processes.

Two bake products walk `lanemeshes.jsonl` record by record — `lanegeometry` bends the solid
parts, `markings` the decal parts — and each walk was a single-core loop of forty seconds while
the other seven cores idled (refactor Phase 10, 2026-08-29: `lanegeometry: bend` 37 s,
`markings: bend` 44 s clean-room). Every record is independent of every other, so the layer is
cut into consecutive chunks and each chunk goes to a worker, which returns what it produced.

**Order is preserved.** `parallel.each(ordered=True)` hands chunks back in the order they were
cut, and each caller extends its lists in that order, so the blocks reach the tile sink in the
same sequence a serial loop produced — and the tiles come out byte-identical.

A worker keeps its own part-source cache (`part_cache`) for the life of the process; the parent
never sees it. Parsing the layer stays in the parent — 27 MB, a few seconds — because a chunk of
parsed records pickles to less than the lines it came from.
"""

from .. import parallel, reader

CHUNK_RECORDS = 2500

# per-process caches, keyed by the bundle's lane-mesh directory
_PART_CACHES = {}


def part_cache(lay):
    """`(sources, prefab_lods)` dicts for this worker process — grown across every chunk it sees."""
    return _PART_CACHES.setdefault(lay.lane_meshes, ({}, {}))


def each_chunk(worker, bundle, extra, jobs, progress, chunk=CHUNK_RECORDS):
    """Yield `worker((records, bundle, *extra))` for consecutive chunks of the layer, in order.

    Every result must carry `"seen"` — the records the chunk held — which is what advances
    `progress`. The caller's progress total is records.
    """
    lay = reader.resolve_layers(bundle)
    records = list(reader.read_jsonl(lay.lane_geometry))
    batches = [
        (records[start : start + chunk], bundle, *extra) for start in range(0, len(records), chunk)
    ]
    for result in parallel.each(
        worker, batches, jobs, progress, ordered=True, min_parallel=2, advance=False
    ):
        progress.advance(result["seen"])
        yield result
