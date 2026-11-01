"""cs2beamng A3 gate, part 2 — the BeamNG-writer rules.

Run: `python tools/test_a3_rules.py`. Split from `test_a3_objects.py` (Phase 4); the stage
tests live there and the real-city sweeps in `test_a3_city.py`. The colour palette, the LOD
ladder and the residency chain are split further, into `test_a3_palette.py`, `test_a3_lods.py`
and `test_a3_residency.py`.

* **rotationMatrix** is the object's basis vectors in groups of three, anchored against
  BeamNG's own shipped levels rather than our writers;
* **a thin, oversized shape** ships no collider rather than a padded slab;
* **the A8 performance predicates** — every texture kind capped, the footprint rule;
* **the level-dir guard** — the worker never builds into the levels root.
"""

import json
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import check, finish, start  # noqa: E402

BEAMNG_INSTALL = r"G:\SteamLibrary\steamapps\common\BeamNG.drive"


def test_rotation_matrix_is_basis_vectors():
    """`rotationMatrix` holds the object's OWN AXES in groups of three, not a row-major matrix.

    The only non-circular gate in the rotation chain: it asks BeamNG's own shipped levels, not
    our writers. A tunnel's spot light points at the road, so reading the field as groups must
    put a vanilla SpotLight's +Y DOWN — and reading it row-major puts it up, which is what our
    level shipped for months (A6.8, audit §78)."""
    print("rotationMatrix is the object's basis vectors (vanilla-anchored)")
    from cs2beamng.beamng import frame

    # 1. the serialisation itself, on a rotation whose axes are unmistakable: a +90 deg yaw
    yaw = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]  # columns are the axes
    flat = frame.basis_flat(yaw)
    check(flat[0:3] == [0.0, 1.0, 0.0], f"group 0 is the object's X axis ({flat[0:3]})")
    check(flat[3:6] == [-1.0, 0.0, 0.0], f"group 1 is its Y axis ({flat[3:6]})")
    check(flat[6:9] == [0.0, 0.0, 1.0], f"group 2 is its Z axis ({flat[6:9]})")
    east = frame.heading_matrix(1.0, 0.0)
    check(east[3:6] == [1.0, 0.0, 0.0], f"an object facing east has +Y east ({east[3:6]})")

    # 2. the convention, against BeamNG's own levels
    levels = os.path.join(BEAMNG_INSTALL, "content", "levels")
    if not os.path.isdir(levels):
        print("  ..  vanilla levels absent, the anchor is skipped")
        return
    import zipfile

    groups_down = columns_down = total = 0
    for name in sorted(f for f in os.listdir(levels) if f.endswith(".zip"))[:6]:
        with zipfile.ZipFile(os.path.join(levels, name)) as z:
            for entry in [n for n in z.namelist() if n.endswith(".level.json")]:
                for line in z.read(entry).splitlines():
                    line = line.strip()
                    if not line.startswith(b"{") or b'"SpotLight"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    m = obj.get("rotationMatrix")
                    if obj.get("class") != "SpotLight" or not m or len(m) != 9:
                        continue
                    total += 1
                    groups_down += int(m[5] <= -0.9)  # group 1 (+Y) points down
                    columns_down += int(m[7] <= -0.9)  # column 1 (+Y) points down
    if not total:
        print("  ..  no vanilla SpotLights found, the anchor is skipped")
        return
    check(total > 100, f"{total:,} vanilla SpotLights read from the first levels on disk")
    check(
        groups_down > 10 * max(columns_down, 1),
        f"read as GROUPS {groups_down:,} of {total:,} point down; read row-major {columns_down:,} do",
    )


def test_a8_performance_rules():
    """A8 — the two new predicates, both of which remove something a viewer can see."""
    print("A8 performance rules")
    from cs2beamng.beamng import materials as bm
    from cs2beamng.stages import instanceforest as forest
    from cs2beamng.stages import instances as inst
    from cs2beamng.stages import shapelods

    # P1: EVERY exported kind must be capped. A kind missing from the table reads as 0 = uncapped,
    # which is how 767 colour masks shipped at 1024 beside 256-pixel albedos for 1,025 MiB.
    caps = bm.caps_from_settings(
        {"texture_max": 256, "texture_max_normal": 128, "texture_max_data": 64}
    )
    check(
        caps.get("colormask") == 256,
        f"the colour mask takes the colour cap ({caps.get('colormask')})",
    )
    missing = [
        k
        for k in ("color", "normal", "data", "opacity", "colormask")
        if k not in bm.KIND_CAP_SETTING
    ]
    check(not missing, f"every exported texture kind has a cap setting (missing: {missing})")

    # P4: the footprint predicate. Height alone passed a hydrant (0.46 m across, 0.97 m tall).
    hydrant = {"boundsMin": [-0.23, 0.0, -0.23], "boundsMax": [0.23, 0.97, 0.23]}
    sign = {"boundsMin": [-0.27, 0.0, -0.05], "boundsMax": [0.27, 2.13, 0.05]}
    kiosk = {"boundsMin": [-1.2, 0.0, -1.2], "boundsMax": [1.2, 2.4, 1.2]}
    check(forest._shape_footprint(hydrant) < 0.8, "a fire hydrant is under the footprint threshold")
    check(forest._shape_footprint(sign) < 0.8, "a road sign is under it, though it is 2.13 m TALL")
    check(
        forest._shape_height(sign) > inst.FOREST_COLLIDABLE_MIN_HEIGHT,
        "...and the HEIGHT rule alone would have kept its collider - the defect A8 fixes",
    )
    check(forest._shape_footprint(kiosk) >= 0.8, "a 2.4 m kiosk keeps its collider")
    check(
        forest._shape_footprint({}) == float("inf"),
        "an unmeasurable shape NEVER loses its collider",
    )

    # P2: promotion is gated on the MEASURED displacement, and vegetation is exempt from it for
    # the same reason it is exempt from the switch rule (a lamp-post LOD measures 0.06 m and is
    # visually total, A6.7).
    check(shapelods.LOD0_MAX_ERROR_DEFAULT > 0, "LOD0 promotion has a default threshold")
    check(
        "tree" not in shapelods.DISPLACEMENT_RULE_KINDS,
        "vegetation is exempt from the displacement rule, so it never loses LOD 0",
    )


def test_level_dir_guard():
    """The worker refuses to build into the levels ROOT (twice it wrote a level's insides
    beside every other level - 6.5 GB of art/ and forest/ under levels/ itself)."""
    print("the level-dir guard")
    import os
    import tempfile

    from cs2beamng.pipeline.worker import guard_level_dir

    root = tempfile.mkdtemp(prefix="cs2guard_")
    os.makedirs(os.path.join(root, "some_level"))
    open(os.path.join(root, "some_level", "info.json"), "w").write("{}")

    def refuses(settings, level_dir):
        try:
            guard_level_dir(settings, level_dir)
            return False
        except RuntimeError:
            return True

    check(refuses({"output": root, "level_name": ""}, root), "an empty level_name is refused")
    check(refuses({"output": root, "level_name": "x"}, root), "level_dir == output is refused")
    check(
        refuses({"output": os.path.dirname(root), "level_name": os.path.basename(root)}, root),
        "a dir already holding other levels is refused",
    )
    ok_dir = os.path.join(root, "new_level")
    check(
        not refuses({"output": root, "level_name": "new_level"}, ok_dir), "a fresh level dir passes"
    )


def test_thin_oversized_shape_has_no_collider():
    """The 20 m box guard covers the THIN path too: a shape under the colmesh thickness that is
    wider than a padded box may be gets no collider, not a slab. `Baseball_Field__2_revised2`
    (a 138 m field plane 0.1 m tall) shipped as a padded box with a 20 cm step round the whole
    outfield (validation pass 2026-08-31, A4); a small thin sign keeps its box."""
    print("a thin, oversized shape ships no collider")
    from cs2beamng.beamng import dae
    from cs2beamng.stages import instanceforest, shapecollider

    def plane(size):
        h = size / 2.0
        verts = numpy.array(
            [
                [-h, -h, 0],
                [h, -h, 0],
                [h, h, 0],
                [-h, h, 0],
                [-h, -h, 0.05],
                [h, -h, 0.05],
                [h, h, 0.05],
                [-h, h, 0.05],
            ],
            dtype=numpy.float32,
        )
        tris = numpy.array([[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6]], dtype=numpy.int64)
        return dae.Mesh("plane", verts, tris, [("mat", len(tris))])

    options = {
        "collide": {"prop": True},
        "colmesh_max_tris": 2000,
        "collision_preset": "simplified",
    }
    big = plane(138.0)
    meshes, status = shapecollider.collider("field", big, big, "prop", options, is_tree=False)
    check(
        status == "oversizedThin" and meshes == (),
        f"a 138 m plane 5 cm tall: {status!r}, no colmesh",
    )
    check(
        status in instanceforest.NO_COLLIDER, "and the instances stage reads that as 'no collision'"
    )
    small = plane(2.0)
    meshes, status = shapecollider.collider("sign", small, small, "prop", options, is_tree=False)
    check(
        status == "box" and len(meshes) == 1,
        f"a 2 m plane 5 cm tall keeps its padded box: {status!r}",
    )
    check(
        shapecollider.BOX_COLLIDER_MAX_METRES == 20.0,
        "the guard is the declared 20 m (the constant's first test)",
    )


def main():
    start()
    test_rotation_matrix_is_basis_vectors()
    test_thin_oversized_shape_has_no_collider()
    test_a8_performance_rules()
    test_level_dir_guard()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_rules.py`."""
    assert main() in (None, 0)
