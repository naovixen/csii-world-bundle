"""Reading one network tile's segment meshes into placed pieces, for `stages/networks`.

The worker half's first step: each segment's `.glb` parsed, its nodes placed in bundle WORLD
(mirror undone, `pos` added) and tagged with the segment's own draw-distance limit.
"""

import os

import numpy
from csiibundle import glb

from .networklod import segment_min_lod


def _load_parts(net_meshes, recs):
    """`{"pieces": [(verts_bundle_world, normals_glb, uvs, tris, runs, origin_xz)], "refused": n}`
    with verts still in glTF axes but placed in bundle WORLD (mirror undone, `pos` added)."""
    pieces = []
    refused = 0
    missing = 0
    for rec in recs:
        path = os.path.join(net_meshes, rec["assetId"] + ".glb")
        if not os.path.isfile(path):
            # a segment record with no mesh file: the road is simply absent from the level, and
            # an undeclared missing mesh is a real error (`csiibundle.instances`), so it is
            # counted and logged rather than skipped in silence (B9)
            missing += 1
            continue
        nodes = glb.parse_glb(path)
        if nodes is None:
            refused += 1
            continue
        pos = numpy.asarray(rec["pos"], dtype=numpy.float64)
        min_lod = segment_min_lod(rec, nodes)
        for node in nodes:
            if len(node["tris"]) == 0:
                continue
            m = numpy.asarray(node["matrix"], dtype=numpy.float64)
            v = numpy.asarray(node["verts"], dtype=numpy.float64) @ m[:3, :3].T + m[:3, 3]
            n = node["normals"]
            if n is not None:
                n = numpy.asarray(n, dtype=numpy.float64) @ numpy.linalg.inv(m[:3, :3])
            # glTF axes with the mirror still applied: undoing the mirror is x -> -x, and the
            # bundle offset applies in bundle axes; keep everything in "mirrored glTF" form and
            # add the offset mirrored too, so daemesh.glb_to_local does the whole conversion once
            v[:, 0] -= pos[0]  # bundle +x is glTF -x
            v[:, 1] += pos[1]
            v[:, 2] += pos[2]
            pieces.append(
                (
                    v,
                    n,
                    node["uvs"],
                    node["tris"],
                    node["material_runs"],
                    (rec["pos"][0], rec["pos"][2]),
                    min_lod,
                )
            )
    return {"pieces": pieces, "refused": refused, "missing": missing}
