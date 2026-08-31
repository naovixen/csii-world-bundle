"""The far ring — one merged silhouette mesh per cell, from where the buildings' own levels stop
out to the game's own cull (refactor Phase 10, record §4.2 / §8.10).

Every building shape already carries a closed voxel silhouette of its coarsest level
(`shapewrite._far_level`, `lod_vertcol`, coloured per face). Drawn PER BUILDING to the game's
cull that is ~8,700 TSStatics each a draw call for a dozen triangles, and BeamNG's own slow-scene
list starts with draw calls. This stage takes the same silhouettes, places them where the
instances stage placed their buildings, and merges them by cell (`CELL_METRES`) into one shape
per cell with one material — ~60 draw calls for the whole skyline instead of thousands.

The hand-over is one distance for the whole level, `far_ring_metres` (D):

* a building whose detailed levels stop NEARER than D keeps its coarsest level out to D
  instead (its null moves to D) and ships no box of its own; it is a member of its cell's
  ring from D outwards;
* every building, towers included: at the game's own switch points a house's coarsest level
  reaches 16 km, so an exemption for "detail reaching past D" was an exemption for all of
  them. A shape's thresholds are per shape and the shape stands in many cells, so a per-cell
  hand-over is not expressible; one distance is.

So nothing is drawn twice and nothing is absent: the ring cell starts at D (its own `_a<px>`
from the CELL's radius, `lod.group_detail_px`) and stops at the game's cull of its largest
member times `far_detail_scale` — the same reach the per-building level had.

DECLARED: a silhouette is a 1 m voxel hull, so at D the hand-over from a building's coarsest
authored level to its hull is a visible step in shape (not a disappearance). It is the one
switch the per-building far level already made, moved to one distance for everything.
"""

import math
import os

import numpy

from csiibundle import lod

from ..beamng import dae, daemesh
from ..beamng import level as level_mod

CELL_METRES = 512.0
GROUP = "FarRing"
PRODUCT = "farring"
MATERIAL = "lod_vertcol"


class Ring:
    """Accumulates placed silhouettes per cell; `write` merges and ships them."""

    def __init__(self, hand_over_m, far_detail_scale):
        self.d = float(hand_over_m)
        self.scale = float(far_detail_scale or 1.0)
        self.cells = {}  # (cx, cy) -> {"verts": [...], "normals": [...], "colors": [...], "tris": [...], "cull": max metres, "members": n}
        self.members = 0
        self.triangles = 0
        self._payloads = {}  # loaded silhouette .npz files, one per SHAPE (many instances share)

    def add(self, ring, position, rotation, scale, base_dir=None):
        """`ring` is the shape result's `lods.farRing` - the silhouette arrays inline, or a
        `path` to the `.npz` the shapes stage wrote beside its state (D1); the transform is the
        instance's, as `_add_static` wrote it."""
        if "path" in ring:
            path = os.path.join(base_dir or "", "cs2beamng_state", "farring", ring["path"])
            key = ("npz", path)
            cached = self._payloads.get(key)
            if cached is None:
                with numpy.load(path) as data:
                    cached = {name: data[name] for name in data.files}
                cached["cullMetres"] = ring.get("cullMetres")
                self._payloads[key] = cached
            ring = cached
        verts = numpy.asarray(ring["verts"], dtype=numpy.float64)
        if len(verts) == 0:
            return
        normals = numpy.asarray(ring["normals"], dtype=numpy.float64)
        rot = numpy.asarray(rotation, dtype=numpy.float64).reshape(3, 3)
        world = (verts * numpy.asarray(scale, dtype=numpy.float64)) @ rot.T + numpy.asarray(
            position, dtype=numpy.float64
        )
        world_n = normals @ rot.T
        centre = world.mean(axis=0)
        key = (int(math.floor(centre[0] / CELL_METRES)), int(math.floor(centre[1] / CELL_METRES)))
        cell = self.cells.setdefault(
            key, {"verts": [], "normals": [], "colors": [], "tris": [], "cull": 0.0, "members": 0}
        )
        base = sum(len(v) for v in cell["verts"])
        cell["verts"].append(world)
        cell["normals"].append(world_n)
        cell["colors"].append(numpy.asarray(ring["colors"], dtype=numpy.float64))
        cell["tris"].append(numpy.asarray(ring["tris"], dtype=numpy.int64) + base)
        cell["cull"] = max(cell["cull"], float(ring.get("cullMetres") or 0.0))
        cell["members"] += 1
        self.members += 1
        self.triangles += len(ring["tris"])

    def write(self, level, options):
        """One `.dae` + one `TSStatic` per cell. Returns the report dict."""
        out_dir = level.path("art", "shapes", PRODUCT)
        names = set()
        written = 0
        recorded = []
        for (cx, cy), cell in sorted(self.cells.items()):
            verts = numpy.concatenate(cell["verts"])
            normals = numpy.concatenate(cell["normals"])
            colors = numpy.concatenate(cell["colors"])
            tris = numpy.concatenate(cell["tris"])
            ox, oy = cx * CELL_METRES, cy * CELL_METRES
            local = verts - numpy.array([ox, oy, 0.0])
            lo, hi = local.min(axis=0), local.max(axis=0)
            radius = 0.5 * float(numpy.linalg.norm(hi - lo))
            # A Torque detail's size is the pixel size at which it STARTS as the camera
            # approaches (it is used for p >= size, until a larger detail's size is reached), so
            # the ring level's size is its FAR end - the cull - and the empty near detail's size
            # is the hand-over. First build had them the other way round: 117 triangles of ring
            # in an 8 km view (record §8.11).
            cull_m = max(cell["cull"] * self.scale, self.d + 1.0)
            # rounded UP: the ring may stop nearer than the game, never further
            ring_px = max(2, int(math.ceil(lod.group_detail_px(radius, cull_m))))
            near_px = max(ring_px + 1, int(math.ceil(lod.group_detail_px(radius, self.d))))
            null_px = ring_px - 1
            name = f"{PRODUCT}_{cx}_{cy}"
            mesh = dae.Mesh(
                name,
                local.astype(numpy.float32),
                tris,
                [(MATERIAL, len(tris))],
                normals=normals.astype(numpy.float32),
                uvs=numpy.zeros((len(local), 2), dtype=numpy.float32),
                colors=colors.astype(numpy.float32),
            )
            # Torque draws a shape's LARGEST detail for any pixel size above its size, so a
            # one-level cell would be drawn on top of the real buildings whenever the camera is
            # nearer than the hand-over (Naomi's look, 2026-08-30: boxes through the meshes).
            # The near detail is a 1 mm triangle 500 m underground: drawn, and invisible.
            near = dae.Mesh(
                f"{name}_NEAR",
                numpy.array([[0, 0, -500], [0.001, 0, -500], [0, 0.001, -500]], dtype=numpy.float32),
                numpy.array([[0, 1, 2]], dtype=numpy.int64),
                [(MATERIAL, 1)],
                normals=numpy.array([[0, 0, 1]] * 3, dtype=numpy.float32),
                uvs=numpy.zeros((3, 2), dtype=numpy.float32),
                colors=numpy.zeros((3, 4), dtype=numpy.float32),
            )
            os.makedirs(out_dir, exist_ok=True)
            # `split_mesh` caps every geometry at `MAX_GEOMETRY_VERTS`, as every other writer
            # does: BeamNG's per-mesh index buffers wrap past 65,535 and the shape shards
            # (load #10). A cell is the one product that merges the most geometry into one
            # node - a downtown cell of a few hundred hulls at four unshared vertices per face.
            stats = dae.write_shape(
                os.path.join(out_dir, name + ".dae"),
                dae.Shape(
                    name, [(near_px, [near]), (ring_px, daemesh.split_mesh(mesh))], null_px=null_px
                ),
            )
            names.add(name)
            written += 1 if stats.get("changed") else 0
            recorded.append(
                {
                    "name": name,
                    "radius": round(radius, 3),
                    "nearPx": near_px,
                    "ringPx": ring_px,
                    "nullPx": null_px,
                    "members": cell["members"],
                    "cullMetres": round(cull_m, 1),
                    "triangles": int(len(tris)),
                }
            )
            obj = level_mod.tsstatic(
                name,
                level.game_path("art", "shapes", PRODUCT, name + ".dae"),
                (ox, oy, 0.0),
                level_mod.identity_basis(),
                collision="None",
                castShadows=False,
            )
            level.add([GROUP], obj, key=f"farring:{name}")
        orphans = dae.sweep_orphans(out_dir, names)
        return {
            "handOverMetres": self.d,
            "cellCount": len(self.cells),
            "members": self.members,
            "triangles": self.triangles,
            "rewritten": written,
            "orphansRemoved": orphans,
            "cells": recorded,  # what tools/beamng/lod_audit.py pairs the buildings against
        }
