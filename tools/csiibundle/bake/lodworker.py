"""The `lods` product's pool worker: one source `.glb` to its LOD files - the refined shipped
levels first, then, for a shape with no authored chain, the generated ladder.
"""

import os
from collections import Counter

import numpy

from .. import decimate, displacement, glb, glbwrite, png
from .lodchoice import best_levels
from .lodrefine import write_refined
from .lodreuse import REUSE_RULE_VERSION


def _albedo_sampler_factory(bundle_dir, catalogue):
    cache = {}

    def sampler(material_name):
        entry = catalogue.get(material_name) or {}
        rel = entry.get("baseColor")
        if rel:
            if rel not in cache:
                try:
                    img = png.rgba_array(os.path.join(bundle_dir, rel))
                    # a coarse copy is plenty for per-vertex sampling and keeps the cache small
                    step = max(1, img.shape[0] // 128)
                    cache[rel] = img[::step, ::step]
                except Exception:
                    cache[rel] = None
            return cache[rel]
        tint = entry.get("tint")
        if tint:
            return tuple(float(v) for v in tint[:3])
        return None

    return sampler


def _flatten(node):
    """The node's geometry with its own transform applied, and the transform dropped.

    **`glbwrite` writes every node with an IDENTITY transform** (it is the one shape of glTF the
    bake proves it round-trips), so a level written from node-LOCAL vertices is placed at the
    origin by any reader while LOD 0 is placed by the source file's node matrix. On the reference
    bundle 791 of 4,453 mesh-bearing nodes carry a translation over a centimetre and 166 carry a
    rotation — up to 75.9 m — so 670 shapes, 39,565 placements, TELEPORTED the moment they
    dropped a level. Read out of the shipped `.dae` files, `Mangr0be_Tree__elderly`'s LOD1 sat
    **9.0 m** above its LOD 0 and switched at 17.6 m from the camera (10,886 of them);
    `Amazon_Warehouse`'s sat 75.9 m across. **And the colmesh is cut from LOD 1**, so 628 shapes
    carried an invisible collider that far off the building you can see. That is the "still
    floating or misaligned" of A6.7, and it is a defect rather than a tuning question.

    Flattening here rather than teaching the writer about transforms keeps the derived file
    self-contained: every consumer reads it exactly as it reads a producer mesh, and gets the
    same world geometry whichever level it takes. The matrix is rigid on every bundle mesh
    (measured: no node carries a scale), so normals take the same rotation and stay unit.
    """
    if node.get("matrix") is None:
        return node
    matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
    if matrix.shape != (4, 4) or numpy.allclose(matrix, numpy.identity(4)):
        return node
    rotation = matrix[:3, :3]
    out = dict(node)
    out["verts"] = numpy.asarray(node["verts"], dtype=numpy.float64) @ rotation.T + matrix[:3, 3]
    if node.get("normals") is not None:
        normals = numpy.asarray(node["normals"], dtype=numpy.float64) @ rotation.T
        length = numpy.linalg.norm(normals, axis=1, keepdims=True)
        out["normals"] = normals / numpy.where(length > 1e-12, length, 1.0)
    out["matrix"] = numpy.identity(4)
    return out


def _corner_digest(pieces):
    """Order-independent geometry digest: the sorted, rounded corner positions of every triangle
    in `pieces` (each a dict with `verts` and `tris`). Two levels built from the same source in
    a different triangle or material order digest the same; anything else differs."""
    corners = []
    for piece in pieces:
        verts = numpy.asarray(piece["verts"], dtype=numpy.float64)
        tris = numpy.asarray(piece["tris"], dtype=numpy.int64)
        if len(tris):
            corners.append(verts[tris].reshape(-1, 9))
    if not corners:
        return b""
    stack = numpy.concatenate(corners).round(4)
    return stack[numpy.lexsort(stack.T[::-1])].tobytes()


def _nodes_digest(level_nodes):
    """`_corner_digest` over a written level's `{"name", "primitives": [...]}` nodes."""
    return _corner_digest([prim for node in level_nodes for prim in node["primitives"]])


def _authored_is_copy(nodes, lod_nodes):
    """True when every authored level's geometry is a copy of level 0's (order-independent:
    the sorted corner positions per level; the consumer's identical-level rule, §8.13)."""
    digest = _corner_digest
    base = digest([n for n in nodes if glb.lod_level(n["name"])[1] == 0])
    levels = {}
    for node in lod_nodes:
        levels.setdefault(glb.lod_level(node["name"])[1], []).append(node)
    return all(digest(group) == base for group in levels.values())


_WORKER_CATALOGUE = {}


def init_worker(catalogue):
    """Pool initializer: the materials catalogue every mesh's albedo sampler reads - it used
    to ride every argument tuple, pickled once per chunk (validation pass 2026-08-31, D5)."""
    global _WORKER_CATALOGUE
    _WORKER_CATALOGUE = catalogue


def _chain_displacement(by_level):
    """How far the game's own LOD 1 sits from LOD 0, in metres (p95), or None.

    THE NUMBER `authored_lod0_max_tris` NEVER ASKED FOR. That cap drops LOD 0 on a triangle COUNT,
    so it cannot tell a LOD 1 that is indistinguishable from one that is a different building.
    Measured over the reference city's 644 capped shapes the split is stark: p50 0.005 m, p75
    0.025 m, p90 0.072 m - and then `OracleParkSculpture` at 1.088 m (5,974 -> 784 triangles),
    `Planetario_Galileo_Galilei` 0.892 m, `Alvear_Tower_Buenos_Aires` 0.423 m, `Prison01` 0.316 m.
    Those are the shapes that "look awful up close" (Naomi, 2026-09-01), and the existing ratio
    guard (`DEGENERATE_LOD1_RATIO`) misses every one of them: they are moderately reduced and
    geometrically far, where the ratio only sees badly reduced.
    """
    a = by_level.get(0) or []
    b = by_level.get(1) or []
    if not a or not b:
        return None
    base = max(a, key=lambda n: len(n["tris"]))
    lod1 = max(b, key=lambda n: len(n["tris"]))
    if not len(base["tris"]) or not len(lod1["tris"]):
        return None
    try:
        return round(float(displacement.level_displacement(lod1, base)[0]), 4)
    except Exception:
        return None


def process_file(args):
    """Worker: one source glb → its LOD files. Returns (assetId, status, stats)."""
    bundle_dir, meshes_dir, out_dir, asset_id, targets, backend, refine = args
    catalogue = _WORKER_CATALOGUE
    src = os.path.join(meshes_dir, asset_id + ".glb")
    nodes = glb.parse_glb(src)
    if nodes is None:
        return asset_id, "refused", {}
    if not nodes:
        return asset_id, "empty", {}
    lod_nodes = [n for n in nodes if glb.lod_level(n["name"])[1] > 0]
    authored = bool(lod_nodes) and not _authored_is_copy(nodes, lod_nodes)

    # THE REFINEMENT RUNS FIRST, and it runs for an authored shape too. A generated LADDER is
    # refused for a shape whose artist shipped one (their levels have no holes and the game's rule
    # is the authority) - but a refined copy of a level the ARTIST authored is not a generated
    # level: it is that same level with triangles removed where the surface provably did not move.
    # The authored shapes are where the bytes are (196 of the 221 shapes over 20,000 triangles).
    by_level = {}
    for node in nodes:
        by_level.setdefault(glb.lod_level(node["name"])[1], []).append(_flatten(node))
    refined, refine_written = write_refined(by_level, refine, out_dir, asset_id)
    if authored:
        return (
            asset_id,
            "authored",
            {
                "refine": refined or None,
                "files": refine_written,
                # what the consumer's cap guard reads (`_chain_displacement`)
                "chainDisplacement": _chain_displacement(by_level),
            },
        )
    # Only LOD 0 generates a ladder: where every authored level is a byte-copy of LOD 0
    # (Kinsman_Plaza and its kin - the workshop filled the LOD slots with the full mesh) real
    # levels are generated instead, and `by_level[0]` is already the flattened level-0 nodes.
    sampler = _albedo_sampler_factory(bundle_dir, catalogue)
    nodes = by_level.get(0) or []
    return _bake_ladder(
        asset_id, nodes, targets, backend, sampler, out_dir, refined, refine_written
    )


def _bake_ladder(asset_id, nodes, targets, backend, sampler, out_dir, refined, refine_written):
    """The generated ladder of one shape's level-0 nodes: `(assetId, "baked", stats)`, or a
    `failed: ...` status the moment one node's decimator contest raises."""
    per_level_nodes = [[] for _ in targets]
    tri_in = 0
    tri_out = [0] * len(targets)
    backends = [Counter() for _ in targets]
    # every node's samples pool into one distribution per level, so a file's displacement is the
    # percentile over its whole surface rather than the mean of its nodes' percentiles
    spread = [([], []) for _ in targets]
    for node in nodes:
        tri_in += len(node["tris"])
        try:
            levels, measured = best_levels(node, targets, backend)
        except Exception as exc:
            return asset_id, f"failed: {exc!r}", {}
        for li, level in enumerate(levels):
            if len(level["tris"]) == 0:
                continue
            tri_out[li] += len(level["tris"])
            backends[li][level["backend"]] += len(level["tris"])
            distances, weights = measured[li]
            if len(distances):
                spread[li][0].append(distances)
                spread[li][1].append(weights)
            colours = None
            if li == len(levels) - 1:
                colours = decimate.vertex_albedo(level, sampler)
            prims = _ladder_prims(level, colours)
            per_level_nodes[li].append({"name": node["name"], "primitives": prims})
    written, identical = _write_ladder(per_level_nodes, out_dir, asset_id)
    written.extend(refine_written)
    return (
        asset_id,
        "baked",
        {
            "tris": tri_in,
            "levels": tri_out,
            "files": written,
            "identical": identical,
            "targets": list(targets),
            "reuseV": REUSE_RULE_VERSION,
            "refine": refined or None,
            "displacement": _level_displacements(spread),
            # the backend that shaped most of each level's triangles
            "backends": [c.most_common(1)[0][0] if c else None for c in backends],
            "backendTris": [dict(c) for c in backends],
        },
    )


def _ladder_prims(level, colours):
    """One generated level's primitives, one per material run, with `colors` on the last level."""
    prims = []
    cursor = 0
    # one primitive per material run, but glbwrite wants per-primitive vertex arrays:
    # split the level back into runs (compact vertices per run)
    for name, count in level["material_runs"]:
        tris = level["tris"][cursor : cursor + count]
        cursor += count
        used, inverse = numpy.unique(tris.ravel(), return_inverse=True)
        prim = {
            "verts": level["verts"][used],
            "normals": None if level["normals"] is None else level["normals"][used],
            "uvs": None if level["uvs"] is None else level["uvs"][used],
            "tris": inverse.reshape(-1, 3),
            "material": name,
        }
        if colours is not None:
            prim["colors"] = colours[used]
        prims.append(prim)
    return prims


def _write_ladder(per_level_nodes, out_dir, asset_id):
    """Write every generated level that is not a copy of the one above it as
    `<assetId>_LOD<n>.glb`. Returns `(written, identical)`: the file names, and the levels left
    unwritten as copies."""
    written = []
    identical = []
    previous = None
    for li in range(len(per_level_nodes)):
        if not per_level_nodes[li]:
            continue
        # A level whose geometry is the level above it is not a LOD: MeshLab stalls at the same
        # count for two targets on a hard facade (LuxuryTowerResidence: 61,594 twice) and the
        # cluster backend can too. 182 of the reference bake's 2,625 LOD 2 files were byte-copies
        # of their LOD 1 (18.5 MiB). The switch to it would draw the same pixels for the bytes,
        # so it is left unwritten and counted; the consumer's identical-level rule catches the
        # same case in a bake made before this.
        digest = _nodes_digest(per_level_nodes[li])
        if previous is not None and digest == previous:
            identical.append(li + 1)
            continue
        previous = digest
        out = os.path.join(out_dir, f"{asset_id}_LOD{li + 1}.glb")
        glbwrite.write_glb(out, per_level_nodes[li])
        written.append(os.path.basename(out))
    return written, identical


def _level_displacements(spread):
    """Each level's displacement: the weighted percentile over every node's pooled samples, or
    None for a level with none."""
    displacements = []
    for distances, weights in spread:
        if not distances:
            displacements.append(None)
            continue
        displacements.append(
            round(
                displacement.weighted_percentile(
                    numpy.concatenate(distances),
                    numpy.concatenate(weights),
                    displacement.DISPLACEMENT_PERCENTILE,
                ),
                4,
            )
        )
    return displacements
