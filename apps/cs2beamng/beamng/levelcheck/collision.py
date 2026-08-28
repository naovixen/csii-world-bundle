"""Does each static's `collisionType` match what its shape actually carries?

`Collision Mesh` uses the shape's `Colmesh_` node; `Visible Mesh*` rasterises the rendered
triangles instead, which BeamNG's own docs call "expensive" and "use only when required". Both
halves of the pairing are worth asserting: a static that HAS a colmesh and does not use it is
the cost we came to remove, and one that asks for a colmesh it does not have collides with
nothing at all.
"""

import os

COLMESH_TAIL_BYTES = 64 * 1024


def _has_colmesh(path):
    """Whether this `.dae` declares a `Colmesh_` node, from the tail of the file.

    The visual scene is written after every `<geometry>`, so the collision nodes are always in
    the last few hundred bytes; reading the tail keeps this check at a seek per shape instead of
    a full parse of 2.1 GB of COLLADA.
    """
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - COLMESH_TAIL_BYTES))
            return b"Colmesh_" in handle.read()
    except OSError:
        return False


def check_collision_types(ctx):
    """Every TSStatic's collider is the one its shape can actually provide (A4 2.2)."""
    # -- collisionType must match what the shape actually carries (A4 2.2) --------------------
    # `Collision Mesh` uses the shape's `Colmesh_` node; `Visible Mesh*` rasterises the rendered
    # triangles instead — BeamNG's docs call that "expensive" and "use only when required". A3
    # wrote `Visible Mesh Final` on all 14,658 object statics while writing a Colmesh_ node into
    # every one of them, so both halves of the pairing are worth asserting: an object that has a
    # colmesh and does not use it is the cost we came to remove, and one that asks for a colmesh
    # it does not have collides with NOTHING.
    colmesh_cache = {}
    mismatched_expensive = []
    mismatched_missing = []
    for _rel, _n, obj in ctx.objects:
        if obj["class"] != "TSStatic":
            continue
        kind = str(obj.get("collisionType") or "")
        path = ctx.resolve(obj.get("shapeName"))
        if not path or not os.path.isfile(path) or not path.lower().endswith(".dae"):
            continue
        if path not in colmesh_cache:
            colmesh_cache[path] = _has_colmesh(path)
        if colmesh_cache[path] and kind.startswith("Visible Mesh"):
            mismatched_expensive.append(obj["name"])
        elif not colmesh_cache[path] and kind == "Collision Mesh":
            mismatched_missing.append(obj["name"])
    ctx.checks["collisionTypeMismatch"] = {
        "visibleMeshWithColmesh": len(mismatched_expensive),
        "collisionMeshWithoutColmesh": len(mismatched_missing),
    }
    if mismatched_expensive:
        ctx.note(
            f"{len(mismatched_expensive):,} TSStatic(s) use a Visible Mesh collider although their shape "
            f"carries a Colmesh_ node (e.g. {', '.join(mismatched_expensive[:3])}) - the expensive path, "
            f"with the cheap one already in the file"
        )
    for bad in mismatched_missing[:10]:
        ctx.problems.append(
            f"TSStatic {bad}: collisionType 'Collision Mesh' but the shape has no Colmesh_ node, "
            f"so it collides with nothing"
        )
