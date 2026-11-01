"""The MeshLab backend: texture-aware quadric edge collapse for the meshes the other two
decimators wreck (refactor Phase 10 §8.15, 2026-08-31).

The landmark buildings with no authored LOD chain (`Five_Luxe_JBR_` 243k triangles,
`Kinsman_Plaza` 176k, ...) ship their full mesh out to the far ring because clustering webs
their facades and pyfqmr's position-only collapse pulls metres of error across CS2's unwelded
seams. MeshLab's `meshing_decimation_quadric_edge_collapse_with_texture` weighs UV distortion
in the quadric, and two choices make it safe here, both measured on the landmarks themselves:

* the mesh is WELDED first (`meshing_remove_duplicate_vertices`; wedge texcoords keep the
  seams) — unwelded, every edge reads as a boundary and the collapse refuses to move
  (176,587 -> 176,583);
* `optimalplacement=False`: a collapse lands ON an existing vertex, so the simplified surface
  lies on the original by construction (decimated->original Hausdorff max 0.0 m on
  `EU_ResidentialHighSignature03`, against 18 m with optimal placement's free-floating verts).

What it cannot fix, declared: a perforated facade (rows of open balconies) still webs shut at
these ratios. The bake's contest (`lodchoice.best_levels`) judges every level by its measured
two-sided displacement and the switch rule defers a bad level out to where its error projects
under `lod_switch_error_px` — the same self-protection every generated level has.

`pymeshlab` is an optional dependency (`pip install cs2-world-bundle[lods]`); `available()`
says. The OBJ round-trip exists because `pymeshlab.Mesh(w_tex_coords_matrix=...)` leaves the
per-wedge texture index unset and the filter refuses "faces without texture".
"""

import os
import shutil
import tempfile

import numpy

QUALITY_THRESHOLD = 0.6
BOUNDARY_WEIGHT = 2.0


def available():
    try:
        import pymeshlab  # noqa: F401

        return True
    except Exception:
        return False


def to_ratio(verts, tris, uvs, normals, ratio):
    """`(verts, normals, uvs, tris)` at about `ratio` of the input triangles — the reducer
    interface `decimate.lod_ladder` calls per material run. Normals come back None (the far
    level takes flat normals from `glbwrite`); UVs come back welded per unique (vertex, uv)
    wedge. Anything that fails returns the input unchanged (it then loses the contest)."""
    import pymeshlab

    verts = numpy.asarray(verts, dtype=numpy.float64)
    tris = numpy.asarray(tris, dtype=numpy.int64)
    target = max(1, int(round(len(tris) * ratio)))
    if len(tris) <= 4 or target >= len(tris):
        return verts, normals, uvs, tris
    uv = (
        numpy.zeros((len(verts), 2), dtype=numpy.float64)
        if uvs is None
        else numpy.asarray(uvs, dtype=numpy.float64)
    )
    workdir = tempfile.mkdtemp(prefix="csii_meshlab_")
    try:
        obj = _write_obj(workdir, verts, tris, uv)
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(obj)
        ms.apply_filter("meshing_remove_duplicate_vertices")
        ms.apply_filter(
            "meshing_decimation_quadric_edge_collapse_with_texture",
            targetfacenum=target,
            qualitythr=QUALITY_THRESHOLD,
            preserveboundary=True,
            boundaryweight=BOUNDARY_WEIGHT,
            planarquadric=True,
            optimalplacement=False,
        )
        mesh = ms.current_mesh()
        out_v = numpy.asarray(mesh.vertex_matrix(), dtype=numpy.float64)
        out_t = numpy.asarray(mesh.face_matrix(), dtype=numpy.int64)
        if len(out_t) == 0:
            return verts, normals, uvs, tris
        wedge = numpy.asarray(mesh.wedge_tex_coord_matrix(), dtype=numpy.float64)
    except Exception:
        return verts, normals, uvs, tris
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    # weld wedges back to per-vertex verts/uvs: one output vertex per unique (vertex, uv)
    corner_vertex = out_t.reshape(-1)
    key = numpy.concatenate([out_v[corner_vertex], numpy.round(wedge, 6)], axis=1)
    _, first, inverse = numpy.unique(key.round(6), axis=0, return_index=True, return_inverse=True)
    new_tris = inverse.reshape(-1, 3)
    new_verts = out_v[corner_vertex[first]]
    new_uvs = wedge[first]
    return new_verts, None, new_uvs, new_tris


def _write_obj(workdir, verts, tris, uv):
    """The textured OBJ `to_ratio` hands MeshLab, written into `workdir`. Returns its path."""
    obj = os.path.join(workdir, "prim.obj")
    with open(os.path.join(workdir, "prim.mtl"), "w") as handle:
        handle.write("newmtl m\nmap_Kd dummy.png\n")
    # a 1x1 grey png, written raw so PIL is not a dependency here
    with open(os.path.join(workdir, "dummy.png"), "wb") as handle:
        handle.write(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
                "7753de0000000c49444154789c636868680000030401814bd3d210000000"
                "0049454e44ae426082"
            )
        )
    lines = ["mtllib prim.mtl", "usemtl m"]
    lines += [f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in verts]
    lines += [f"vt {t[0]:.6f} {t[1]:.6f}" for t in uv]
    lines += [f"f {a}/{a} {b}/{b} {c}/{c}" for a, b, c in (tris + 1)]
    with open(obj, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return obj
