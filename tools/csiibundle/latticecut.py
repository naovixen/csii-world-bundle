"""Cutting lattice faces against the clip prisms, exactly.

The game hides terrain by discarding the pixels inside the clip volume, so the hole's boundary
is the prism's own surface (audit §46). `terrainclip.clip_triangle` reproduces that on
geometry: on one triangle the prism's side walls, floor and roof are all affine in (x, z), so
the discarded part is an intersection of half-planes and the kept part is a convex subtraction.
Each face's two triangles go in and its surviving polygons come out.

Watertight without any snap: every constraint is a function of (x, z) alone except the height,
and two triangles sharing an edge interpolate the same endpoint heights along it, so they cut
it at the same points. The weld gives those coincident cut points one vertex.
"""

from collections import namedtuple

import numpy

from . import terrainclip

# The lattice geometry after the cut, plus what the cut did: how many parent faces still draw
# something (`faces_kept`), how many polygons came back (`pieces`), the plan area removed, and
# `terrainclip`'s sliver count.
CutResult = namedtuple(
    "CutResult", "verts loops sizes face_class face_cell faces_kept pieces removed_m2 slivers"
)


def _cut_one(r, c, local, ground_y, grid, window, clip_stats):
    """One face's two triangles against its prisms: `(kept polygons, plan area removed)`."""
    cell = grid.cell
    x0 = grid.origin_x + (window[1] + c) * cell
    x1 = x0 + cell
    z0 = grid.origin_z - (window[0] + r) * cell
    z1 = z0 - cell
    nw = (x0, float(ground_y[r, c]), z0)
    ne = (x1, float(ground_y[r, c + 1]), z0)
    sw = (x0, float(ground_y[r + 1, c]), z1)
    se = (x1, float(ground_y[r + 1, c + 1]), z1)
    kept = []
    for tri in ((nw, ne, se), (nw, se, sw)):
        kept.extend(terrainclip.clip_triangle(tri, local, clip_stats))
    area = sum(abs(terrainclip.polygon_area([(q[0], q[2]) for q in p])) for p in kept)
    return kept, max(0.0, cell * cell - area)


def _append_pieces(verts, loops, sizes, face_class, face_cell, pieces):
    """Concatenate the cut polygons onto the lattice arrays."""
    cut_loops, cut_class, cut_cell, cut_extra = pieces
    if cut_extra:
        verts = numpy.concatenate((verts, numpy.asarray(cut_extra, dtype=numpy.float64)))
    loops = numpy.concatenate(
        (loops, numpy.asarray([i for poly in cut_loops for i in poly], dtype=numpy.int64))
    )
    sizes = numpy.concatenate(
        (sizes, numpy.asarray([len(p) for p in cut_loops], dtype=numpy.int64))
    )
    face_class = numpy.concatenate((face_class, numpy.asarray(cut_class, dtype=face_class.dtype)))
    face_cell = numpy.concatenate((face_cell, numpy.asarray(cut_cell, dtype=numpy.int64)))
    return verts, loops, sizes, face_class, face_cell


def _prune_unreferenced(verts, loops):
    """A lattice corner used only by a face that the cut removed entirely is referenced by
    nothing now; prune so the mesh carries no stray vertex."""
    referenced = numpy.zeros(len(verts), dtype=bool)
    referenced[loops] = True
    if not referenced.all():
        verts = verts[referenced]
        loops = (numpy.cumsum(referenced) - 1)[loops]
    return verts, loops


def _collect_pieces(f, prisms, ground_y, grid, window, base):
    """Every surviving polygon of every reached face: `(cut_loops, cut_class, cut_cell,
    cut_extra, faces_kept, removed, clip_stats)`, with vertex ids welded from `base` up."""
    cut_loops, cut_class, cut_cell, cut_extra = [], [], [], []
    faces_kept = 0
    removed = 0.0
    clip_stats = {}
    weld = {}

    def vertex(x, y, z):
        """One vertex id per position, so cut pieces share their edges."""
        key = (round(x, 6), round(z, 6), round(y, 6))
        found = weld.get(key)
        if found is None:
            cut_extra.append((x, z, y))  # the lattice frame is (x, bundle z, height)
            found = base + len(cut_extra) - 1
            weld[key] = found
        return found

    for pick in numpy.nonzero(f.sub_face)[0]:
        r, c = int(f.cell_rr[pick]), int(f.cell_cc[pick])
        local = [prisms[i] for i in f.by_face.get((r, c), ())]
        if not local:
            continue
        kept, gone = _cut_one(r, c, local, ground_y, grid, window, clip_stats)
        removed += gone
        if kept:
            faces_kept += 1
        parent = int(f.refined_class_all[pick])
        for poly in kept:
            cut_loops.append([vertex(px, py, pz) for px, py, pz in poly])
            cut_class.append(parent)
            cut_cell.append((r, c))
    return cut_loops, cut_class, cut_cell, cut_extra, faces_kept, removed, clip_stats


def cut_prism_faces(verts, loops, sizes, face_class, face_cell, f, ground_y, prisms, grid, window):
    """Cut every lattice face a prism reaches and return the updated geometry as a `CutResult`.

    The faces named by `f.sub_face` (`latticefaces.Families`) were withheld from the quad
    families; each comes back here as the polygons of a convex subtraction, classed as its
    parent was and keyed to its parent's cell in `face_cell`.
    """
    if not (f.sub_face.any() and prisms):
        return CutResult(verts, loops, sizes, face_class, face_cell, 0, 0, 0.0, 0)

    cut_loops, cut_class, cut_cell, cut_extra, faces_kept, removed, clip_stats = _collect_pieces(
        f, prisms, ground_y, grid, window, len(verts)
    )
    if cut_loops:
        verts, loops, sizes, face_class, face_cell = _append_pieces(
            verts, loops, sizes, face_class, face_cell, (cut_loops, cut_class, cut_cell, cut_extra)
        )
    verts, loops = _prune_unreferenced(verts, loops)
    return CutResult(
        verts,
        loops,
        sizes,
        face_class,
        face_cell,
        faces_kept,
        len(cut_loops),
        removed,
        clip_stats.get("slivers", 0),
    )
