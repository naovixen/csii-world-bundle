"""Gate for `csiibundle.glbwrite`: parse(write(x)) == x, in both frames, with and without UVs
and vertex colours; and a written tile validates like a producer mesh through
`tools/validate_bundle.py`'s GLB reader. Plain `python tools/test_glbwrite.py`.
"""
import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from csiibundle import glb, glbwrite  # noqa: E402
import validate_bundle  # noqa: E402


def _quad(offset=(0.0, 0.0, 0.0)):
    verts = numpy.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], dtype=numpy.float32) + numpy.asarray(offset, numpy.float32)
    tris = numpy.array([[0, 1, 2], [0, 2, 3]], dtype=numpy.int64)
    uvs = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=numpy.float32)
    normals = numpy.tile(numpy.array([[0, 1, 0]], numpy.float32), (4, 1))
    colors = numpy.array([[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [1, 1, 1, 0.5]], numpy.float32)
    return verts, normals, uvs, colors, tris


def main():
    failures = []

    def check(cond, msg):
        (print("ok:  ", msg) if cond else (failures.append(msg), print("FAIL:", msg)))

    with tempfile.TemporaryDirectory() as tmp:
        v, n, uv, c, t = _quad()
        # 1. glTF-space round trip, two primitives, one with colours and no uvs
        nodes = [{"name": "tile_0_0", "primitives": [
            {"verts": v, "normals": n, "uvs": uv, "tris": t, "material": "paint_a"},
            {"verts": v + 2, "normals": n, "colors": c, "tris": t, "material": "lod_vertcol"},
        ]}]
        # a mixed-UV mesh is refused by parse_glb (mixed UV presence) - so use two nodes instead
        nodes = [{"name": "tile_0_0", "primitives": [nodes[0]["primitives"][0]]},
                 {"name": "tile_0_0_vc", "primitives": [nodes[0]["primitives"][1]]}]
        path = os.path.join(tmp, "a.glb")
        glbwrite.write_glb(path, nodes, materials={"paint_a": {"baseColor": "../textures/x.png", "alphaMode": "BLEND", "doubleSided": True}})
        parsed = glb.parse_glb(path)
        check(parsed is not None and len(parsed) == 2, "parse_glb reads the written file (2 nodes)")
        a, b = parsed
        check(a["name"] == "tile_0_0" and numpy.allclose(a["verts"], v) and numpy.allclose(a["uvs"], uv)
              and numpy.array_equal(a["tris"], t) and a["material_runs"] == [("paint_a", 2)], "node A round-trips")
        check(b["colors"] is not None and numpy.allclose(b["colors"], c) and b["uvs"] is None
              and b["material_runs"] == [("lod_vertcol", 2)], "node B carries COLOR_0 and no UVs")
        check(numpy.allclose(a["normals"], n), "normals round-trip")

        # 2. bundle-frame input is mirrored on X with winding reversed, so that
        #    frames.instance_matrix (which composes diag(-1,1,1)) restores world positions
        path2 = os.path.join(tmp, "b.glb")
        glbwrite.write_glb(path2, [{"name": "t", "primitives": [{"verts": v, "normals": n, "uvs": uv, "tris": t, "material": "m"}]}],
                           from_bundle_frame=True)
        p2 = glb.parse_glb(path2)[0]
        check(numpy.allclose(p2["verts"][:, 0], -v[:, 0]) and numpy.allclose(p2["verts"][:, 1:], v[:, 1:]), "X mirrored")
        check(numpy.array_equal(p2["tris"], t[:, [0, 2, 1]]), "winding reversed")
        # face normal orientation is preserved through mirror + swap
        def face_normal(verts, tri):
            e1, e2 = verts[tri[1]] - verts[tri[0]], verts[tri[2]] - verts[tri[0]]
            nrm = numpy.cross(e1, e2)
            return nrm / numpy.linalg.norm(nrm)
        world_n = face_normal(v, t[0])
        stored_n = face_normal(p2["verts"], p2["tris"][0])
        stored_n_back = stored_n * numpy.array([-1, 1, 1])
        check(numpy.allclose(world_n, stored_n_back), "face orientation survives the frame change")

        # 3. the repo validator's GLB reader accepts it (same checks as a producer mesh)
        try:
            doc, _ = validate_bundle.read_glb(validate_bundle.Path(path))
            check(doc["asset"]["version"] == "2.0" and doc["materials"][0]["name"] == "paint_a", "validate_bundle.read_glb parses it")
        except Exception as exc:  # noqa: BLE001
            check(False, f"validate_bundle.read_glb failed: {exc!r}")

        # 4. flat normals for a caller that has none
        path3 = os.path.join(tmp, "c.glb")
        glbwrite.write_glb(path3, [{"name": "t", "primitives": [{"verts": v, "tris": t, "material": "m"}]}])
        p3 = glb.parse_glb(path3)[0]
        check(numpy.allclose(numpy.abs(p3["normals"][:, 1]), 1.0), "flat normals computed when absent")

        # 5. empty primitives are dropped, empty file still parses as zero nodes
        path4 = os.path.join(tmp, "d.glb")
        glbwrite.write_glb(path4, [{"name": "t", "primitives": [{"verts": v[:0], "tris": t[:0], "material": "m"}]}])
        check(glb.parse_glb(path4) == [], "empty primitive yields a parseable file with no nodes")

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all glbwrite checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
