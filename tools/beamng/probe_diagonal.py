r"""Which way does BeamNG split a terrain square into two triangles? Offline, read-only.

`bake/ground.GroundSurface` conforms area paint to `diagonal="envelope", mode="max"` — the upper
envelope of BOTH ways a heightfield square can be split — because the real answer was never
measured. Its own docstring says so. The envelope is >= both triangulations by construction, so
the paint floats above the ground everywhere by that square's twist, and the car drops through the
visible plaza onto relief the plaza was hiding (A6.5).

**This does not need an in-game probe.** `probe_ter.py` already settled the `.ter` ORIENTATION
offline by the observation that DecalRoad nodes lie on the terrain — median |err| 0.020 m over
3,800 nodes on `small_island`. That is an order of magnitude under the diagonal's own
discriminating power, so the same nodes answer this question too: sample each node inside its
square under each hypothesis and see which reproduces the node's own z.

    python tools/beamng/probe_diagonal.py [path\to\level.zip] [levelname]

Only nodes in a TWISTED square discriminate — where h00 + h11 == h01 + h10 the two triangulations
are the same plane — and only nodes away from the square's edges and its shared diagonal. Both
filters are applied and both counts are printed, because a probe that reports agreement over
samples that cannot disagree is measuring nothing.

The plan's warning is tested too: a per-square ALTERNATING split (checkerboard on `(row + col) &
1`) is a real possibility in Torque-derived terrain, and a probe that only tries two uniform
hypotheses would "confirm" whichever one happened to fit the squares it hit.
"""
import json
import sys
import zipfile

import numpy as np

sys.path.insert(0, __file__.rsplit("probe_diagonal.py", 1)[0])
from probe_ter import read_ter                                          # noqa: E402

# this probe's split name -> the name `csiibundle.drape.HeightfieldSurface` gives the SAME split
BAKE_NAME = {"sw_ne": "ne_sw", "nw_se": "nw_se"}

DEFAULT_ZIP = r"G:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\small_island.zip"

# a square flatter than this (metres of twist) cannot tell the two triangulations apart
MIN_TWIST = 0.10
# how far inside the square a sample must sit, in cell fractions, to be clear of the edges and of
# the diagonal both hypotheses share
EDGE_MARGIN = 0.15


def _corners(hm, ri, ci, size):
    r1 = np.minimum(ri + 1, size - 1)
    c1 = np.minimum(ci + 1, size - 1)
    return hm[ri, ci], hm[ri, c1], hm[r1, ci], hm[r1, c1]      # h00, h01, h10, h11


def _height(kind, h00, h01, h10, h11, u, v):
    """Height inside one square under a given split.

    **Frames differ and the compass names flip between them — this is the trap.** Here `u` rises
    EAST and `v` rises NORTH (`probe_ter.py`: the `.ter`'s row 0 is SOUTH), so `h00` is the SW
    corner and `h11` the NE one. `csiibundle.drape.HeightfieldSurface` has row 0 NORTH, so its
    `v` rises SOUTH and its `nw_se` joins NW to SE. A split named by two corners is therefore the
    OTHER name in the other frame:

        this probe's `sw_ne`  (h00 -> h11)  ==  the bake's `ne_sw`
        this probe's `nw_se`  (h10 -> h01)  ==  the bake's `nw_se`

    Both names below are world-compass, so `bake_diagonal()` translates without ambiguity.
    """
    if kind == "bilinear":
        return (h00 * (1 - u) * (1 - v) + h01 * u * (1 - v)
                + h10 * (1 - u) * v + h11 * u * v)
    if kind == "sw_ne":                       # joins h00 (SW) to h11 (NE)
        lower = v <= u
        return np.where(lower,
                        h00 + u * (h01 - h00) + v * (h11 - h01),
                        h00 + v * (h10 - h00) + u * (h11 - h10))
    if kind == "nw_se":                       # joins h10 (NW) to h01 (SE)
        lower = (u + v) <= 1.0
        return np.where(lower,
                        h00 + u * (h01 - h00) + v * (h10 - h00),
                        h11 + (1 - u) * (h10 - h11) + (1 - v) * (h01 - h11))
    raise ValueError(kind)


def main(argv):
    zip_path = argv[1] if len(argv) > 1 else DEFAULT_ZIP
    z = zipfile.ZipFile(zip_path)
    ter_name = next(n for n in z.namelist() if n.endswith(".ter"))
    _version, size, heights, _layers, _names = read_ter(z.read(ter_name))

    block, nodes = None, []
    for name in z.namelist():
        if not name.endswith("items.level.json"):
            continue
        for line in z.read(name).decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("class") == "TerrainBlock":
                block = obj
            elif obj.get("class") == "DecalRoad":
                nodes.extend(n[:3] for n in obj.get("nodes", []))
    if block is None or not nodes:
        print("no TerrainBlock or no DecalRoad nodes")
        return 1

    pos = block.get("position", [0, 0, 0])
    sq = float(block.get("squareSize") or 1.0)
    hm = heights.astype(np.float64) * (float(block.get("maxHeight", 2048)) / 65536.0) + pos[2]
    nodes = np.asarray(nodes, dtype=np.float64)
    print(f"{zip_path.rsplit(chr(92), 1)[-1]}: {ter_name} size={size} squareSize={sq} "
          f"{len(nodes):,} DecalRoad nodes")

    # `probe_ter.py`'s settled orientation: rows from y, no flips
    r = (nodes[:, 1] - pos[1]) / sq
    c = (nodes[:, 0] - pos[0]) / sq
    inside = (r >= 0) & (r < size - 1) & (c >= 0) & (c < size - 1)
    r, c, z_node = r[inside], c[inside], nodes[inside, 2]
    ri, ci = np.floor(r).astype(int), np.floor(c).astype(int)
    v, u = r - ri, c - ci

    h00, h01, h10, h11 = _corners(hm, ri, ci, size)
    twist = np.abs(h00 + h11 - h01 - h10)
    interior = ((u > EDGE_MARGIN) & (u < 1 - EDGE_MARGIN)
                & (v > EDGE_MARGIN) & (v < 1 - EDGE_MARGIN)
                & (np.abs(u - v) > EDGE_MARGIN) & (np.abs(u + v - 1) > EDGE_MARGIN))
    keep = (twist >= MIN_TWIST) & interior
    print(f"  inside the grid: {len(r):,}   twisted (>= {MIN_TWIST} m) AND clear of both "
          f"diagonals: {int(keep.sum()):,}")
    if int(keep.sum()) < 50:
        print("  NOT ENOUGH DISCRIMINATING SAMPLES — this level cannot answer it")
        return 2

    k = np.where(keep)[0]
    print(f"\n  {'hypothesis':<26}{'median|err|':>12}{'p90':>10}{'p99':>10}")
    results = {}
    for kind in ("bilinear", "sw_ne", "nw_se"):
        err = np.abs(_height(kind, h00[k], h01[k], h10[k], h11[k], u[k], v[k]) - z_node[k])
        results[kind] = float(np.median(err))
        print(f"  {kind:<26}{np.median(err):>12.4f}{np.percentile(err, 90):>10.4f}"
              f"{np.percentile(err, 99):>10.4f}")
    # the envelope the bake currently uses, and the alternating split the plan warns about
    a = _height("sw_ne", h00[k], h01[k], h10[k], h11[k], u[k], v[k])
    b = _height("nw_se", h00[k], h01[k], h10[k], h11[k], u[k], v[k])
    for label, value in (("envelope max (the bake)", np.maximum(a, b)),
                         ("envelope min", np.minimum(a, b))):
        err = np.abs(value - z_node[k])
        results[label] = float(np.median(err))
        print(f"  {label:<26}{np.median(err):>12.4f}{np.percentile(err, 90):>10.4f}"
              f"{np.percentile(err, 99):>10.4f}")
    alt = np.where(((ri[k] + ci[k]) & 1) == 0, a, b)
    err = np.abs(alt - z_node[k])
    results["alternating (checkerboard)"] = float(np.median(err))
    print(f"  {'alternating (checkerboard)':<26}{np.median(err):>12.4f}"
          f"{np.percentile(err, 90):>10.4f}{np.percentile(err, 99):>10.4f}")

    winner = min(results, key=results.get)
    runner = sorted(results.values())[1]
    print(f"\n  winner: {winner} (median {results[winner]:.4f} m); next best {runner:.4f} m")
    if winner in BAKE_NAME:
        print(f"  -> pass `--diagonal {BAKE_NAME[winner]}` to the bake. THE COMPASS NAME FLIPS: "
              f"this probe's row 0 is SOUTH, the bake's is NORTH, so the same split has the "
              f"other two-corner name there.")
    if results[winner] > 0.5 * runner:
        print("  INCONCLUSIVE — the winner is not clearly better than the next hypothesis")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
