"""Is the geometry on disk something BeamNG can actually draw?

Two shapes of file that load without an error and render nothing: a detail level declared with
size 0 (BeamNG logs "Shape with invalid detail levels" and draws nothing at all, load #6), and a
single geometry past the 65,535-vertex index cap, which shards in-game (load #10).
"""

import os
import re

from .. import dae as dae_mod


def check_shape_geometry(ctx):
    """Detail-level sizes are positive and no one geometry passes the per-mesh vertex cap."""
    # detail sizes must be positive: BeamNG logs "Shape with invalid detail levels" and draws nothing (load #6)
    bad_detail = 0
    detail_re = re.compile(r'<node id="[^"]*_a(\d+)"')
    for name, path in ctx.dae_files:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 262144))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        sizes = [int(v) for v in detail_re.findall(tail)]
        if sizes and min(sizes) <= 0:
            bad_detail += 1
            if bad_detail <= 5:
                ctx.note(f"{name}: detail level size 0 (BeamNG: invalid detail levels)")
    ctx.checks["invalidDetailShapes"] = bad_detail
    # no geometry over the per-mesh vertex cap: the load #10 junction shards were exactly the tiles
    # whose one geometry passed 65,535 vertices (dae.MAX_GEOMETRY_VERTS, dae.split_mesh)
    oversize = 0
    positions_re = re.compile(rb'<float_array id="[^"]*-positions-array" count="(\d+)"')
    # A geometry over the cap needs > 60,000 x 3 floats of positions text (~1.2 MB at the
    # writer's 4-decimal format), so smaller files cannot hold one and are not read at all;
    # the few large tiles are scanned in 1 MB chunks (with a small overlap for a tag on the
    # boundary) rather than slurped — a full-city level is ~2 GB of COLLADA.
    min_bytes = dae_mod.MAX_GEOMETRY_VERTS * 3 * 6
    for name, path in ctx.dae_files:
        try:
            if os.path.getsize(path) < min_bytes:
                continue
            counts = []
            with open(path, "rb") as handle:
                tail = b""
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    buf = tail + chunk
                    counts.extend(int(c) for c in positions_re.findall(buf))
                    tail = buf[-256:]
        except OSError:
            continue
        worst = max((c // 3 for c in counts), default=0)
        if worst > dae_mod.MAX_GEOMETRY_VERTS:
            oversize += 1
            if oversize <= 5:
                ctx.note(
                    f"{name}: a geometry with {worst:,} vertices exceeds the {dae_mod.MAX_GEOMETRY_VERTS:,} cap "
                    "(shards in-game: BeamNG's per-mesh index buffers wrap past 65,535)"
                )
    ctx.checks["oversizeGeometries"] = oversize
