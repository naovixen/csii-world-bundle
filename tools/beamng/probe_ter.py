"""Determine BeamNG `.ter` row/column orientation from a shipped level, offline (read-only).

Roads (DecalRoad nodes) lie on the terrain, so of the eight ways a square heightmap can be
indexed, the one whose sampled heights match the nodes' own z is the right one. Run once
against any vanilla level zip; the answer is hard-coded in `cs2beamng.beamng.ter` and this
script exists so the claim can be re-checked in ten seconds on a new BeamNG version.

    python tools/beamng/probe_ter.py [path\\to\\level.zip] [levelname]

Result on BeamNG 0.39.2 `small_island` (3,800 nodes): rows_from=y, no flips — i.e.
`heights[row=(y - pos.y)/squareSize][col=(x - pos.x)/squareSize]`, row 0 SOUTH, col 0 WEST,
`h = stored * maxHeight / 65536 + position.z`; median |err| 0.020 m, every other hypothesis
> 4 m.
"""

import json
import struct
import sys
import zipfile

import numpy as np

DEFAULT_ZIP = r"G:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\small_island.zip"


def read_ter(data):
    """Parse a v9 .ter: (version, size, heights u16[size,size], layers u8[size,size], names)."""
    version = data[0]
    size = struct.unpack_from("<I", data, 1)[0]
    off = 5
    heights = np.frombuffer(data, dtype="<u2", count=size * size, offset=off).reshape(size, size)
    off += size * size * 2
    layers = np.frombuffer(data, dtype="u1", count=size * size, offset=off).reshape(size, size)
    off += size * size
    count = struct.unpack_from("<I", data, off)[0]
    off += 4
    names = []
    for _ in range(count):
        # Torque string: u8 length + bytes (long names use a different prefix; not seen here)
        length = data[off]
        off += 1
        names.append(data[off : off + length].decode("utf-8", "replace"))
        off += length
    return version, size, heights, layers, names


def main(argv):
    zip_path = argv[1] if len(argv) > 1 else DEFAULT_ZIP
    argv[2] if len(argv) > 2 else zip_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1][:-4]
    z = zipfile.ZipFile(zip_path)
    ter_name = next(n for n in z.namelist() if n.endswith(".ter"))
    version, size, heights, layers, names = read_ter(z.read(ter_name))
    print(
        f"{ter_name}: version={version} size={size} materials={len(names)} holes={int((layers == 255).sum())}"
    )

    terrain_block = None
    nodes = []
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
                terrain_block = obj
            elif obj.get("class") == "DecalRoad":
                nodes.extend(n[:3] for n in obj.get("nodes", []))
    if terrain_block is None or not nodes:
        print("no TerrainBlock or no DecalRoad nodes found")
        return 1
    pos = terrain_block.get("position", [0, 0, 0])
    max_h = float(terrain_block.get("maxHeight", 2048))
    sq = float(terrain_block.get("squareSize") or 1.0)
    print(
        "TerrainBlock:", {k: terrain_block.get(k) for k in ("position", "maxHeight", "squareSize")}
    )
    nodes = np.asarray(nodes, dtype=np.float64)
    print("decal road nodes:", len(nodes))

    hm = heights.astype(np.float64) * (max_h / 65536.0)
    gx = (nodes[:, 0] - pos[0]) / sq
    gy = (nodes[:, 1] - pos[1]) / sq
    best = None
    for rows_from in ("y", "x"):
        for flip_r in (False, True):
            for flip_c in (False, True):
                r, c = (gy, gx) if rows_from == "y" else (gx, gy)
                if flip_r:
                    r = (size - 1) - r
                if flip_c:
                    c = (size - 1) - c
                ri = np.clip(np.round(r).astype(int), 0, size - 1)
                ci = np.clip(np.round(c).astype(int), 0, size - 1)
                err = hm[ri, ci] + pos[2] - nodes[:, 2]
                med = float(np.median(np.abs(err)))
                print(
                    f"rows_from={rows_from} flip_r={flip_r!s:5} flip_c={flip_c!s:5}: "
                    f"median|err|={med:.3f} m  p90={np.percentile(np.abs(err), 90):.3f}"
                )
                if best is None or med < best[0]:
                    best = (med, rows_from, flip_r, flip_c)
    print(
        f"\nwinner: rows_from={best[1]} flip_r={best[2]} flip_c={best[3]} (median {best[0]:.3f} m)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
