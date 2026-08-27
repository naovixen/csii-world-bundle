"""Does a built level draw ground exactly where the game would?

The game hides terrain by rendering each clip volume into a stencil and discarding the pixels
inside it (`Game.Simulation.TerrainSystem`, audit §46), so the question has a crisp answer: for a
point on the terrain, "is it inside a DEEP clip prism at the terrain's own height?" This samples
points over a window and compares that predicate with what the level actually draws — an unpunched
`.ter` square, or a `terraincut` triangle over a punched one.

    python tools/check_terrain_vs_game.py <derived_dir> <level_dir> <cx> <cz> <half> [samples]

This is the validation to reach for when a consumer's ground looks wrong: it scores against the
GAME, not against the other consumer (CLAUDE.md, "When output looks wrong, find the game's rule").
Needs a built level, so it is a probe rather than a gate; `tools/test_terrainclip.py` is the unit
gate for the cut itself.
"""

import math
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))
from cs2beamng.beamng import ter as ter_mod  # noqa: E402
from csiibundle import bezier, glb, reader, terrain  # noqa: E402
from csiibundle.bake.locate import default_bundle_dir  # noqa: E402

derived, level_dir = sys.argv[1], sys.argv[2]
cx, cz, half = (float(v) for v in sys.argv[3:6])
samples = int(sys.argv[6]) if len(sys.argv) > 6 else 20000
BUNDLE = default_bundle_dir()

field = reader.load_heightfield(BUNDLE, prefer_fine=True)
cell, ox, oz = field.cell, field.origin_x, field.origin_z
scale, offset = field.elevation_scale, field.elevation_offset
name = os.path.basename(level_dir)
_sz, h16, layers, _names = ter_mod.read_ter(os.path.join(level_dir, f"{name}.ter"))
H = h16.shape[0]
ter_h = h16.astype(numpy.float64) * scale + offset  # .ter row (H-1-r) is bundle row r
holes = layers == ter_mod.HOLE

# -- the game's own prisms, deep ones only (what this product cuts) ------------------------------
prisms = []
for record in reader.read_jsonl(reader.layer_path(BUNDLE, "clipVolumes")):
    bottom = float(record["bottom"])
    top = float(record["top"]) if record.get("top") is not None else float("inf")
    if not (top > terrain.CLIP_DEEP_BAND_TOP):
        continue
    if record.get("kind") == "area":
        nodes = record.get("nodes") or []
        for tri in record.get("triangles") or []:
            if any(not 0 <= i < len(nodes) for i in tri):
                continue
            pts = [tuple(float(v) for v in nodes[i]) for i in tri]
            lane = sum(p[1] for p in pts) / 3.0
            prisms.append(([(p[0], lane, p[2]) for p in pts], bottom, top))
        continue
    left, right = record.get("left"), record.get("right")
    if not left or not right:
        continue
    span = float(
        numpy.linalg.norm(
            numpy.diff(numpy.asarray(left, dtype=numpy.float64), axis=0), axis=1
        ).sum()
    )
    steps = int(min(1024, max(1, math.ceil(max(span, 1.0) / cell))))
    t = numpy.linspace(0.0, 1.0, steps + 1)
    lp, rp = bezier.evaluate(left, t), bezier.evaluate(right, t)
    for i in range(1, steps + 1):
        prisms.append(([tuple(lp[i - 1]), tuple(rp[i - 1]), tuple(rp[i])], bottom, top))
        prisms.append(([tuple(lp[i - 1]), tuple(rp[i]), tuple(lp[i])], bottom, top))
prisms = [
    p
    for p in prisms
    if min(q[0] for q in p[0]) <= cx + half
    and max(q[0] for q in p[0]) >= cx - half
    and min(q[2] for q in p[0]) <= cz + half
    and max(q[2] for q in p[0]) >= cz - half
]
print(f"{len(prisms):,} deep prism triangles in the window")


def bary(tri, x, z):
    (x0, _, z0), (x1, _, z1), (x2, _, z2) = tri
    d = (z1 - z2) * (x0 - x2) + (x2 - x1) * (z0 - z2)
    if abs(d) < 1e-12:
        return None
    l1 = ((z1 - z2) * (x - x2) + (x2 - x1) * (z - z2)) / d
    l2 = ((z2 - z0) * (x - x2) + (x0 - x2) * (z - z2)) / d
    l3 = 1.0 - l1 - l2
    return l1, l2, l3


def discarded_by_game(x, z, h):
    for tri, bottom, top in prisms:
        b = bary(tri, x, z)
        if b is None or min(b) < -1e-9:
            continue
        lane = b[0] * tri[0][1] + b[1] * tri[1][1] + b[2] * tri[2][1]
        if lane + bottom - 1e-9 <= h <= lane + top + 1e-9:
            return True
    return False


# -- what we draw --------------------------------------------------------------------------------
mesh_tris = []
tile_dir = os.path.join(derived, "terraincut")
for tile in sorted(n for n in os.listdir(tile_dir) if n.endswith(".glb")):
    for node in glb.parse_glb(os.path.join(tile_dir, tile)):
        v = numpy.asarray(node["verts"], dtype=numpy.float64)
        m = numpy.asarray(node["matrix"])
        v = v @ m[:3, :3].T + m[:3, 3]
        p = numpy.stack((-v[:, 0], v[:, 1], v[:, 2]), axis=-1)  # §2.2 mirror -> bundle frame
        for t in numpy.asarray(node["tris"]):
            tri = [tuple(p[i]) for i in t]
            if (
                min(q[0] for q in tri) <= cx + half
                and max(q[0] for q in tri) >= cx - half
                and min(q[2] for q in tri) <= cz + half
                and max(q[2] for q in tri) >= cz - half
            ):
                mesh_tris.append(tri)
print(f"{len(mesh_tris):,} mesh triangles in the window")

rng = numpy.random.default_rng(11)
agree = 0
sky = 0  # we draw nothing where the game draws ground
extra = 0  # we draw ground where the game discards it
tested = 0
for _ in range(samples):
    x = float(rng.uniform(cx - half, cx + half))
    z = float(rng.uniform(cz - half, cz + half))
    r = int(math.floor((oz - z) / cell))
    c = int(math.floor((x - ox) / cell))
    if not (0 <= r < H - 1 and 0 <= c < H - 1):
        continue
    tested += 1
    # the terrain height at the point: bilinear over the .ter square, in bundle rows
    h00 = ter_h[H - 1 - r, c]
    h01 = ter_h[H - 1 - r, c + 1]
    h10 = ter_h[H - 1 - (r + 1), c]
    h11 = ter_h[H - 1 - (r + 1), c + 1]
    fu = (x - (ox + c * cell)) / cell
    fv = ((oz - r * cell) - z) / cell
    h = (h00 * (1 - fu) + h01 * fu) * (1 - fv) + (h10 * (1 - fu) + h11 * fu) * fv
    # BeamNG's square for bundle face (r, c) is layer row (H-1)-(r+1)
    punched = bool(holes[H - 1 - (r + 1), c])
    drawn_by_us = not punched
    if punched:
        for tri in mesh_tris:
            b = bary(tri, x, z)
            if b is not None and min(b) >= -1e-9:
                drawn_by_us = True
                break
    drawn_by_game = not discarded_by_game(x, z, h)
    if drawn_by_us == drawn_by_game:
        agree += 1
    elif drawn_by_game:
        sky += 1
    else:
        extra += 1
print(f"{tested:,} samples over {2 * half:.0f} x {2 * half:.0f} m at ({cx:.0f}, {cz:.0f})")
print(f"  agree with the game            {agree:,}  ({100.0 * agree / max(tested, 1):.3f} %)")
print(f"  WE SHOW SKY, the game ground   {sky:,}")
print(f"  WE SHOW GROUND, the game none  {extra:,}")
