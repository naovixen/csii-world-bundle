"""Exporting the textures, in the worker pool."""

import os

from csiibundle import parallel

from ..beamng import textureplan
from ..beamng.level import Level
from .materialledger import effective_cap

# -- pool ---------------------------------------------------------------------------------------


def _export_chunk(args):
    level_dir, level_name, caps, use_dds, job_caps, chunk = args
    level = Level(level_dir, level_name, level_name)
    cache = {}
    stats = {}
    out = []
    for src, kind, keep_alpha in chunk:
        try:
            cap = effective_cap((src, kind, keep_alpha), caps, job_caps)
            out.append(
                (
                    (src, kind, keep_alpha),
                    textureplan.export_texture(
                        level, src, kind, cap, keep_alpha, cache, dds=use_dds, stats=stats
                    ),
                )
            )
        except Exception as exc:
            # counted apart from a MISSING source: a material shipped without this map because
            # the encoder failed, not because the bundle had nothing (B10)
            stats["encodeFailed"] = stats.get("encodeFailed", 0) + 1
            out.append(((src, kind, keep_alpha), None))
            out.append(
                (
                    ("__log__", str(src), ""),
                    f"texture {os.path.basename(src)}: ENCODE FAILED {exc!r}",
                )
            )
    for key, value in stats.items():
        out.append((("__stat__", key, ""), value))
    return out


def export_all(level, jobs, caps, workers, progress, use_dds=True, job_caps=None, stats=None):
    """`{job: game path or None}`; `stats` (a dict) collects the workers' counters -
    `encodeFailed`, `normalsUnpacked`."""
    exported = {}
    if not jobs:
        return exported
    chunk_size = 16
    chunks = [jobs[i : i + chunk_size] for i in range(0, len(jobs), chunk_size)]
    job_caps = job_caps or {}
    args = [(level.dir, level.name, caps, use_dds, job_caps, c) for c in chunks]
    # The jobs are already chunked by the caller, so chunksize stays 1 and one chunk is enough
    # to be worth a pool; `_absorb` advances by the number of textures the chunk carried.
    for results in parallel.each(
        _export_chunk, args, workers or 1, progress, min_parallel=1, spawn=True, advance=False
    ):
        _absorb(results, exported, progress, stats)
    return exported


def _absorb(results, exported, progress, stats=None):
    for key, value in results:
        if key[0] == "__log__":
            progress.log(value)
            continue
        if key[0] == "__stat__":
            if stats is not None:
                stats[key[1]] = stats.get(key[1], 0) + int(value)
            continue
        exported[key] = value
        progress.advance(1)
