"""Is the geometry on disk something BeamNG can actually draw?

Two shapes of file that load without an error and render nothing: a detail level declared with
size 0 (BeamNG logs "Shape with invalid detail levels" and draws nothing at all, load #6), and a
single geometry past the 65,535-vertex index cap, which shards in-game (load #10).
"""

import json
import os
import re

from .. import daemesh, matbind

# The collider statuses the instances stage writes as `collisionType: None`
# (`stages/instanceforest.NO_COLLIDER`, kept here without an upward import).
NO_COLLIDER_STATUSES = ("none", "lightNone", "oversizedThin", "plant")


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
    # whose one geometry passed 65,535 vertices (daemesh.MAX_GEOMETRY_VERTS, daemesh.split_mesh)
    oversize = 0
    positions_re = re.compile(rb'<float_array id="[^"]*-positions-array" count="(\d+)"')
    # A geometry over the cap needs > 60,000 x 3 floats of positions text (~1.2 MB at the
    # writer's 4-decimal format), so smaller files cannot hold one and are not read at all;
    # the few large tiles are scanned in 1 MB chunks (with a small overlap for a tag on the
    # boundary) rather than slurped — a full-city level is ~2 GB of COLLADA.
    min_bytes = daemesh.MAX_GEOMETRY_VERTS * 3 * 6
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
        if worst > daemesh.MAX_GEOMETRY_VERTS:
            oversize += 1
            if oversize <= 5:
                ctx.note(
                    f"{name}: a geometry with {worst:,} vertices exceeds the {daemesh.MAX_GEOMETRY_VERTS:,} cap "
                    "(shards in-game: BeamNG's per-mesh index buffers wrap past 65,535)"
                )
    ctx.checks["oversizeGeometries"] = oversize


def check_cooked_shapes(ctx):
    """The checks a dropped `.dae` blinds, from the shapes stage's own record.

    `keep_dae: false` - the shipping configuration - deletes every object shape's COLLADA once
    its `.cdae` is current, and `check_shape_geometry`, `_check_bindings` and
    `check_collision_types` all iterate `ctx.dae_files`: on a cooked level every one of them
    passed unconditionally (validation pass 2026-08-31, E2). The shapes stage recorded, per
    shape, the detail sizes and thresholds it wrote, the canonical materials it bound and the
    collider it cut (`cs2beamng_state/shapes.state.json`); this asks those the same questions
    for every shape whose source is gone. Tiles (networks, derived) have no such record; their
    thresholds are guaranteed at the writer (`dae.Shape`) instead.
    """
    path = os.path.join(ctx.level_dir, "cs2beamng_state", "shapes.state.json")
    try:
        with open(path, encoding="utf-8") as handle:
            results = json.load(handle)["shared"]["shapes"]["results"]
    except (OSError, ValueError, KeyError, TypeError):
        ctx.checks["cookedShapesChecked"] = 0
        return
    pairs = results["$pairs"] if isinstance(results, dict) and "$pairs" in results else results
    rows = [row for _key, row in pairs] if pairs and isinstance(pairs[0], list) else list(pairs)
    with_source = {os.path.basename(p).lower() for _n, p in ctx.dae_files}
    static_collision = {}
    for _rel, _n, obj in ctx.objects:
        if obj.get("class") == "TSStatic" and obj.get("shapeName"):
            static_collision.setdefault(
                os.path.basename(str(obj["shapeName"])).lower(), str(obj.get("collisionType") or "")
            )
    checked = bad_detail = bad_null = 0
    mismatched = []
    unbound = {}
    for row in rows:
        file = str(row.get("file") or "")
        if not file or file.lower() in with_source:
            continue  # the text checks covered it
        checked += 1
        lods = row.get("lods") or {}
        sizes = [int(s) for s in (lods.get("detailSizes") or []) if s is not None]
        if sizes and min(sizes) <= 0:
            bad_detail += 1
            if bad_detail <= 5:
                ctx.note(f"{file}: detail level size 0 (BeamNG: invalid detail levels)")
        null_px = int(row.get("nullPx") or 0)
        if sizes and null_px and null_px >= min(sizes):
            bad_null += 1
            if bad_null <= 5:
                ctx.note(
                    f"{file}: nulldetail{null_px} at or over the coarsest detail ({min(sizes)} px) - "
                    "Torque draws nothing where that level should be"
                )
        if ctx.mapto:
            for name in row.get("materials") or ():
                bound = matbind.mapto(ctx.level_name, name).lower()
                if bound not in ctx.mapto and str(name).lower() not in ctx.mapto:
                    unbound.setdefault(name, file)
        kind = static_collision.get(file.lower())
        if kind is not None:
            expected = (
                "None" if row.get("collider", "none") in NO_COLLIDER_STATUSES else "Collision Mesh"
            )
            if kind != expected:
                mismatched.append((file, kind, expected))
    ctx.checks["cookedShapesChecked"] = checked
    ctx.checks["invalidDetailShapes"] = int(ctx.checks.get("invalidDetailShapes") or 0) + bad_detail
    ctx.checks["cookedNullAboveCoarsest"] = bad_null
    ctx.checks["cookedUnboundMaterials"] = len(unbound)
    for name, file in list(unbound.items())[:10]:
        ctx.note(
            f"{file}: binds material {name!r} which no materials file maps (BeamNG's orange checker)"
        )
    mismatch = ctx.checks.get("collisionTypeMismatch")
    if isinstance(mismatch, dict):
        mismatch["cookedMismatch"] = len(mismatched)
    for file, kind, expected in mismatched[:10]:
        ctx.note(
            f"TSStatic of {file}: collisionType {kind!r} but the shapes stage cut "
            f"{'no collider' if expected == 'None' else 'a Colmesh_'} for it (expected {expected!r})"
        )
