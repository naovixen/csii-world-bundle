"""Read BeamNG's cooked `.cdae` (Cached Collada v31, a MessagePack stream) and report every
mesh's vertex / index / primitive counts and element sizes.

    python tools/beamng/probe_cdae.py <file.cdae> [more.cdae ...]

Why: the full-city level's junctions tore apart in-game (load #10) while the accepted region
level was clean; the region's largest tile had 62,798 vertices, the city has 59 tiles with a
single geometry of 65,536-167,287. This reads what the compiler actually stored — whether the
mesh survived at that size, and how wide its indices are — from BeamNG's own bytes
(format: https://go.beamng.com/shapeMessagepackFileformat). No third-party msgpack module.
"""

import struct
import sys


class Stream:
    """A minimal MessagePack reader over bytes — only what the cdae body uses."""

    def __init__(self, data, pos=0):
        self.b = data
        self.i = pos

    def _take(self, n):
        v = self.b[self.i : self.i + n]
        if len(v) != n:
            raise EOFError(f"cdae: truncated at {self.i}")
        self.i += n
        return v

    def read(self):
        c = self._take(1)[0]
        if c <= 0x7F:
            return c
        if c >= 0xE0:
            return c - 0x100
        if 0x80 <= c <= 0x8F:
            return {self.read(): self.read() for _ in range(c & 0x0F)}
        if 0x90 <= c <= 0x9F:
            return [self.read() for _ in range(c & 0x0F)]
        if 0xA0 <= c <= 0xBF:
            return self._take(c & 0x1F).decode("utf-8", "replace")
        if c == 0xC0:
            return None
        if c == 0xC2:
            return False
        if c == 0xC3:
            return True
        if c == 0xC4:
            return bytes(self._take(self._take(1)[0]))
        if c == 0xC5:
            return bytes(self._take(struct.unpack(">H", self._take(2))[0]))
        if c == 0xC6:
            return bytes(self._take(struct.unpack(">I", self._take(4))[0]))
        if c == 0xCA:
            return struct.unpack(">f", self._take(4))[0]
        if c == 0xCB:
            return struct.unpack(">d", self._take(8))[0]
        if c == 0xCC:
            return self._take(1)[0]
        if c == 0xCD:
            return struct.unpack(">H", self._take(2))[0]
        if c == 0xCE:
            return struct.unpack(">I", self._take(4))[0]
        if c == 0xCF:
            return struct.unpack(">Q", self._take(8))[0]
        if c == 0xD0:
            return struct.unpack(">b", self._take(1))[0]
        if c == 0xD1:
            return struct.unpack(">h", self._take(2))[0]
        if c == 0xD2:
            return struct.unpack(">i", self._take(4))[0]
        if c == 0xD3:
            return struct.unpack(">q", self._take(8))[0]
        if c == 0xD9:
            return self._take(self._take(1)[0]).decode("utf-8", "replace")
        if c == 0xDA:
            return self._take(struct.unpack(">H", self._take(2))[0]).decode("utf-8", "replace")
        if c == 0xDB:
            return self._take(struct.unpack(">I", self._take(4))[0]).decode("utf-8", "replace")
        if c == 0xDC:
            return [self.read() for _ in range(struct.unpack(">H", self._take(2))[0])]
        if c == 0xDD:
            return [self.read() for _ in range(struct.unpack(">I", self._take(4))[0])]
        if c == 0xDE:
            return {self.read(): self.read() for _ in range(struct.unpack(">H", self._take(2))[0])}
        if c == 0xDF:
            return {self.read(): self.read() for _ in range(struct.unpack(">I", self._take(4))[0])}
        raise ValueError(f"cdae: msgpack marker 0x{c:02x} at {self.i - 1} not handled")

    def vector(self):
        n = self.read()
        es = self.read()
        raw = self.read()
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != n * es:
            raise ValueError(
                f"cdae: pack_vector mismatch n={n} es={es} bin={None if raw is None else len(raw)} at {self.i}"
            )
        return n, es, raw


def read_cdae(path):
    data = open(path, "rb").read()
    version, header_size = struct.unpack("<iI", data[:8])
    header = Stream(data, 8).read()
    body = data[8 + header_size :]
    if header.get("compression"):
        raise SystemExit(f"{path}: body is zstd-compressed; not handled by this probe")
    s = Stream(body)
    info = {
        "version": version & 0xFF,
        "exporter": version >> 16,
        "header": {k: v for k, v in header.items() if k != "info"},
    }
    s.read()
    s.read()
    s.read()
    s.read()  # smallestVisibleSize, DL, radius, tubeRadius
    s.read()
    s.read()  # center [3], bounds [6] (msgpack arrays)
    vectors = {}
    for name in (
        "nodes",
        "objects",
        "subShapeFirstNode",
        "subShapeFirstObject",
        "subShapeNumNodes",
        "subShapeNumObjects",
        "defaultRotations",
        "defaultTranslations",
        "nodeRotations",
        "nodeTranslations",
        "nodeUniformScales",
        "nodeAlignedScales",
        "nodeArbitraryScaleFactors",
        "nodeArbitraryScaleRots",
        "groundTranslations",
        "groundRotations",
        "objectStates",
        "triggers",
        "details",
    ):
        vectors[name] = s.vector()[:2]
    n_names = s.read()
    names = [s.read() for _ in range(n_names)]
    info["names"] = names
    total = s.read()
    meshes = []
    for m in range(total):
        mesh_type = s.read()
        if mesh_type == 4:
            meshes.append({"index": m, "type": "null"})
            continue
        num_frames, num_mat_frames, parent = s.read(), s.read(), s.read()
        s.read()
        s.read()
        s.read()  # mBounds [6], mCenter [3], mRadius
        rec = {
            "index": m,
            "type": {0: "standard", 1: "skin", 2: "decal", 3: "sorted"}.get(mesh_type, mesh_type),
            "frames": num_frames,
            "matFrames": num_mat_frames,
            "parent": parent,
        }
        for name in (
            "verts",
            "tverts",
            "tverts2",
            "colors",
            "norms",
            "encodedNorms",
            "primitives",
            "indices",
            "tangents",
        ):
            n, es, raw = s.vector()
            rec[name] = (n, es)
            if name == "indices":
                rec["indicesRaw"] = raw
            if name == "primitives":
                rec["primitivesRaw"] = raw
        rec["vertsPerFrame"] = s.read()
        rec["meshFlags"] = s.read()
        if mesh_type == 1:
            is_parent = parent < 0
            if is_parent:
                s.vector()
                s.vector()
            s.vector()
            if is_parent:
                for _ in range(4):
                    s.vector()
        meshes.append(rec)
    info["meshes"] = meshes
    return info


def describe(path):
    info = read_cdae(path)
    print(f"{path}")
    print(f"  cdae v{info['version']} exporter {info['exporter']} header {info['header']}")
    print(f"  names: {info['names']}")
    for m in info["meshes"]:
        if m["type"] == "null":
            print(f"  mesh {m['index']}: NullMesh")
            continue
        n_idx, es_idx = m["indices"]
        n_v, es_v = m["verts"]
        line = (
            f"  mesh {m['index']}: {m['type']} verts {n_v}x{es_v}B tverts {m['tverts']} norms {m['norms']} "
            f"primitives {m['primitives']} indices {n_idx}x{es_idx}B tangents {m['tangents']} vertsPerFrame {m['vertsPerFrame']}"
        )
        print(line)
        raw = m["indicesRaw"]
        if n_idx:
            fmt = {1: "B", 2: "H", 4: "I"}.get(es_idx)
            if fmt:
                import array

                arr = array.array(fmt)
                arr.frombytes(raw)
                mx = max(arr)
                print(
                    f"    index range 0..{mx} (verts {n_v}) {'-> INDICES EXCEED VERTEX COUNT' if mx >= n_v else 'ok'}"
                    f"{' -> vertices beyond 65535 are unreachable with 2-byte indices' if es_idx == 2 and n_v > 65536 else ''}"
                )
        praw = m["primitivesRaw"]
        pn, pes = m["primitives"]
        if pn and pes in (8, 12):
            # TSDrawPrimitive {S32 start; S32 numElements; S32 matIndex} (12 B) or the packed 8 B form
            fmt = "<iii" if pes == 12 else "<ii"
            prims = [struct.unpack_from(fmt, praw, k * pes) for k in range(min(pn, 6))]
            print(f"    first primitives: {prims}{' ...' if pn > 6 else ''}")
    return info


if __name__ == "__main__":
    for p in sys.argv[1:]:
        describe(p)
