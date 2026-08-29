"""cs2beamng A3 gate — the FRAME: bundle to BeamNG, and the two corrections inside it.

Run: `python tools/test_a3_frame.py`  (needs `pip install -e .`; instant, example bundle only).

Part of the A3 gate beside `test_a3_objects.py` (the stages end to end), `test_a3_rules.py`
(the writer rules) and `test_a3_city.py` (classification plus the real-bundle sweeps). Tally
shared through `a3common`, so a run reads the same however it is started.

What is pinned here is the transform and nothing else: `beamng.frame.record_transform`
reproducing `csiibundle.frames`' own `P . T . R . S . P^-1` to machine precision, the §4.2a
decal lift, and the quaternion the app must NOT renormalise — a stored non-unit quaternion is a
signal about the producer, and correcting it away silently destroys the signal.
"""

import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
from a3common import check, finish, start  # noqa: E402


def test_frame():
    print("the instance transform is the contract's own composition")
    import random

    from cs2beamng.beamng import frame as frame_mod
    from csiibundle import frames

    random.seed(20260818)
    worst = 0.0
    for _ in range(300):
        quat = [random.uniform(-1, 1) for _ in range(4)]
        norm = sum(v * v for v in quat) ** 0.5
        record = {
            "pos": [random.uniform(-4000, 4000) for _ in range(3)],
            "rot": [v / norm for v in quat],
            "scale": [random.uniform(0.3, 3.0) for _ in range(3)],
        }
        # the .dae bakes `P . diag(-1,1,1)` into its vertices, so that basis divides out
        basis = frames.P_ZUP @ frames.MIRROR_X
        reference = frames.zup_instance_matrix(record, mesh_basis=basis)
        position, rotation, scale = frame_mod.record_transform(record)
        mine = numpy.eye(4)
        mine[:3, :3] = numpy.array(rotation) @ numpy.diag(scale)
        mine[:3, 3] = position
        worst = max(worst, float(numpy.abs(mine - reference).max()))
    check(worst < 1e-9, f"P.T.R.S.P^-1 reproduced to {worst:.2e}")

    # a rotation must stay a rotation: no second mirror anywhere
    _p, rot, _s = frame_mod.record_transform(
        {"pos": [0, 0, 0], "rot": [0.0, 0.3826834, 0.0, 0.9238795]}
    )
    det = float(numpy.linalg.det(numpy.array(rot)))
    check(abs(det - 1.0) < 1e-9, f"the rotation has determinant +1 ({det:.6f})")

    # a tree's growthScale is uniform and multiplies the linear part only
    _p, _r, scaled = frame_mod.record_transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(
        all(abs(v - 0.5) < 1e-9 for v in scaled), "growthScale reaches the scale, not the position"
    )
    pos, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "attrs": {"growthScale": 0.5}})
    check(pos == [1.0, 3.0, 2.0], "the position is the Y/Z swap and nothing else")


def test_decal_lift_and_the_quaternion():
    print("the transform folds in the §4.2a lift, and lets a non-unit quaternion through")
    from cs2beamng.beamng import frame as frame_mod

    # §4.2a: a decal instance is coplanar paint and MUST be lifted clear of the surface it
    # paints over, or it z-fights with it. No decal reaches `record_transform` today —
    # `placement.BAKED_KINDS` sends them to `bake/markings`, which applies the lift as the
    # drape's epsilon — so this pins a LATENT trap: the day a decal does reach here, it arrives
    # lifted, because the transform is the shared one rather than a copy that forgot.
    from csiibundle import decals

    lift = decals.LIFT["instance_decal"]
    lifted, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "kind": "decal"})
    check(
        abs(lifted[2] - (2.0 + lift)) < 1e-12 and lifted[:2] == [1.0, 3.0],
        f"a decal is lifted by exactly {lift} m, on the up axis only",
    )
    plain, _r, _s = frame_mod.record_transform({"pos": [1, 2, 3], "kind": "prop"})
    check(plain == [1.0, 3.0, 2.0], "and nothing else is lifted at all")
    check(
        lift > decals.LIFT["area"] > decals.LIFT["lane_marking"],
        "the ladder still puts decal instances above the areas they stand on",
    )

    # A stored non-unit quaternion is a signal, not noise: `frames.quat_matrix` deliberately
    # does not renormalise, and the app's old `_quat_rows` did, which discarded it. It should
    # arrive as a scale off 1.0.
    k = 1.001
    half = 0.7071067811865476
    _p, _r, off = frame_mod.record_transform(
        {"pos": [0, 0, 0], "rot": [0.0, half * k, 0.0, half * k]}
    )
    # The deviation lands on the two axes the turn acts on, at k^2 - 1, and not on the third.
    check(
        abs(max(off) - k**2) < 1e-5 and abs(min(off) - 1.0) < 1e-12,
        f"a non-unit quaternion reaches the scale rather than being renormalised away ({off})",
    )


def test_placement():
    print("forest vs TSStatic")
    from cs2beamng.beamng import placement

    settings = {
        "trees": True,
        "props": True,
        "buildings": True,
        "foliage_density": 100,
        "prop_density": 100,
    }
    decal = {"id": 1, "assetId": "a", "kind": "decal", "pos": [0, 0, 0]}
    tree = {"id": 2, "assetId": "a", "kind": "tree", "pos": [0, 0, 0]}
    building = {"id": 3, "assetId": "a", "kind": "building", "pos": [0, 0, 0]}
    scaled = {"id": 4, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [1.0, 2.0, 1.0]}
    uniform = {"id": 5, "assetId": "a", "kind": "prop", "pos": [0, 0, 0], "scale": [2.0, 2.0, 2.0]}
    site = {
        "id": 6,
        "assetId": "a",
        "kind": "building",
        "pos": [0, 0, 0],
        "attrs": {"state": "construction"},
    }

    check(not placement.keep(decal, settings), "a decal is placed by the markings bake, not here")
    check(
        not placement.keep(site, settings),
        "a building under construction is not drawn (should_draw)",
    )
    check(placement.wants_forest(tree), "a tree is a forest item")
    check(not placement.wants_forest(building), "a building is a TSStatic")
    check(not placement.wants_forest(scaled), "a non-uniformly scaled prop is a TSStatic")
    check(placement.wants_forest(uniform), "a uniformly scaled prop stays a forest item")
    check(
        abs(placement.uniform_scale(uniform) - 2.0) < 1e-9,
        "the forest scalar is the record's scale",
    )
    check(
        not placement.wants_forest(tree, forest_allowed=False),
        "the forest can be switched off wholesale",
    )

    settings_off = dict(settings, trees=False)
    check(not placement.keep(tree, settings_off), "the trees toggle is honoured")
    region = {"centerX": 500.0, "centerZ": 500.0, "radius": 100.0}
    check(not placement.keep(tree, settings, region), "a record outside the region is dropped")
    inside = dict(tree, pos=[520.0, 0.0, 480.0])
    check(placement.keep(inside, settings, region), "a record inside the region is kept")

    print("density thinning is monotone and stable")
    records = [{"id": i, "assetId": "a", "kind": "tree", "pos": [0, 0, 0]} for i in range(4000)]
    at_full = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=100))}
    at_half = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=50))}
    at_tenth = {r["id"] for r in records if placement.keep(r, dict(settings, foliage_density=10))}
    check(at_tenth <= at_half <= at_full, "a lower density is a subset of a higher one")
    check(400 < len(at_half) - len(at_tenth), "the sliders actually differ")


def main():
    start()
    test_frame()
    test_decal_lift_and_the_quaternion()
    test_placement()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_frame.py`."""
    assert main() in (None, 0)
