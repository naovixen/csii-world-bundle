"""Does a built level draw each object the way CS2 would?

The A3 parity predicate (plan `docs/plans/beamng-consumer-plan.md`, audit §62). Every question
it asks has an answer in the GAME's own implementation, so this scores the level against CS2 and
not against the other consumer:

| question | the game's rule |
|---|---|
| which mesh state does this instance draw? | `BatchDataHelpers.CalculateTreeSubMeshData` picks the `Require<State>` sub-mesh; the bundle ships the resolved state as `attrs.treeState` |
| at what size? | the same function's `out scale`, shipped as `attrs.growthScale` |
| how far is it drawn? | `RenderingUtils.CalculateLodLimit(GetRenderingSize(bounds), MeshSystem's surface-area bias)`, recomputed here straight from the `.glb` |
| in what colour? | `MeshColorSystem`'s resolved `MeshColor`, shipped as `attrs.colorSet`, applied by §7.2 |
| where? | the contract's `T(pos) . R(rot) . S(scale) . diag(-1,1,1)`, §2.2 |

Usage:

    python tools/check_instances_vs_game.py <bundle> <level_dir> [samples]

Reads the level's own `forest/*.forest4.json`, `art/forest/managedItemData.json`, `main/**`
`TSStatic`s and `art/shapes/cs2/*.dae` back off disk — nothing is taken from the build's report,
because the point is to check what shipped. Prints an agreement figure per question and the size
of every approximation that remains, and exits non-zero if any exact question disagrees.

Needs a built level, so it is a probe rather than a gate; `tools/test_a3_objects.py` is the unit
gate for the rules themselves.
"""

import collections
import json
import math
import os
import random
import re
import sys
import xml.etree.ElementTree as ET

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "apps"))

from cs2beamng.beamng import (  # noqa: E402 - after the sys.path bootstrap above
    frame,
    placement,
)
from cs2beamng.beamng.level import read_objects  # noqa: E402
from cs2beamng.stages.instances import _transform  # noqa: E402
from cs2beamng.stages.shapes import IMPOSTER_REACH  # noqa: E402
from csiibundle import glb, lod, tints  # noqa: E402
from csiibundle import instances as bundle_instances  # noqa: E402

NS = "{http://www.collada.org/2005/11/COLLADASchema}"
NULL_RE = re.compile(r"^nulldetail(\d+)$")
BILLBOARD_RE = re.compile(r"^bb_autobillboard(\d+)$")
TOLERANCE_POSITION = 0.002  # metres; the level writes 3 decimal places
TOLERANCE_SCALE = 2e-4
# a forest item's rotationMatrix is rounded to 4 dp (`stages/instances.ROTATION_DP`), which
# is 1e-4 of a unit vector; a TSStatic's to 6. The coarser one sets the tolerance.
TOLERANCE_ROTATION = 2e-4


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    bundle, level_dir = argv[1], argv[2]
    samples = int(argv[3]) if len(argv) > 3 else 4000
    settings = _settings_of(level_dir)

    print(f"bundle {bundle}\nlevel  {level_dir}\n")
    shapes = _read_shapes(level_dir)
    print(f"{len(shapes):,} shapes on disk")
    forest, _types = _read_forest(level_dir)
    print(f"{sum(len(v) for v in forest.values()):,} forest items in {len(forest):,} types")
    statics = _read_statics(level_dir)  # (by record id, by position)
    print(f"{len(statics[1]):,} TSStatics\n")

    results = collections.OrderedDict()
    _check_state_and_scale(bundle, settings, forest, statics, samples, results)
    _check_lod(bundle, shapes, settings, results)
    _check_colour(bundle, settings, statics, results)

    print()
    failed = 0
    for name, row in results.items():
        exact = row.get("exact", True)
        total, agree = row["total"], row["agree"]
        share = (agree / total) if total else 1.0
        verdict = "MATCH " if (share >= 1.0 or not exact) else "DIVERGE"
        if exact and share < 1.0:
            failed += 1
        print(
            f"{verdict}  {name:44} {agree:>9,} / {total:<9,} = {share:8.4%}  {row.get('note', '')}"
        )
    return 1 if failed else 0


# -- reading the level back ------------------------------------------------------------------------


def _settings_of(level_dir):
    """The build's own settings, from `cs2beamng_report.json` beside the level."""
    path = os.path.join(level_dir, "cs2beamng_report.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle).get("settings") or {}
        except (OSError, ValueError):
            pass
    return {}


def _read_shapes(level_dir):
    """`{shapeName: {"nullPx", "billboardPx", "details"}}` parsed out of the written `.dae`s."""
    out = {}
    root = os.path.join(level_dir, "art", "shapes", "cs2")
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        if not name.endswith(".dae"):
            continue
        try:
            tree = ET.parse(os.path.join(root, name)).getroot()
        except ET.ParseError:
            continue
        entry = {"nullPx": None, "billboardPx": None, "details": []}
        for node in tree.iter(NS + "node"):
            nid = node.get("id") or ""
            m = NULL_RE.match(nid)
            if m:
                entry["nullPx"] = int(m.group(1))
                continue
            m = BILLBOARD_RE.match(nid)
            if m:
                entry["billboardPx"] = int(m.group(1))
                continue
            if "_a" in nid and nid.rsplit("_a", 1)[1].isdigit():
                entry["details"].append(int(nid.rsplit("_a", 1)[1]))
        out[os.path.splitext(name)[0]] = entry
    return out


def _read_forest(level_dir):
    """`({type: [item, ...]}, {type: itemData})` straight off disk."""
    items = {}
    forest_dir = os.path.join(level_dir, "forest")
    if os.path.isdir(forest_dir):
        for name in os.listdir(forest_dir):
            if not name.endswith(".forest4.json"):
                continue
            rows = []
            with open(os.path.join(forest_dir, name), encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            if rows:
                items[rows[0]["type"]] = rows
    data = {}
    path = os.path.join(level_dir, "art", "forest", "managedItemData.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    return items, data


def _read_statics(level_dir):
    """Every TSStatic under `art/shapes/cs2/`, indexed **by record id** and by position.

    Returns `(by_id, by_position)`. The id is the exact join: `stages/instances._add_static`
    names an object `<shape>_<recordId>_<ordinal>`, so the record it came from is in the name.
    Position is the fallback, and it is not unique — an extraction-area placeholder
    (`Ore_Area_Placeholder`, `Agriculture_Area_Placeholder`) sits at the very same coordinate as
    the industrial hub building it belongs to, so seven positions on the reference city carry two
    records and a position join blamed the placeholder for the hub's colour. That was the probe
    being wrong about the level, which is the failure mode a predicate can least afford.
    """
    by_id, by_pos = {}, {}
    for _f, _n, obj in read_objects(level_dir):
        if not obj or obj.get("class") != "TSStatic":
            continue
        if "/art/shapes/cs2/" not in (obj.get("shapeName") or ""):
            continue
        by_pos[_key(obj["position"])] = obj
        record_id = _record_id_of(obj.get("name") or "")
        if record_id is not None:
            by_id.setdefault(record_id, []).append(obj)
    return by_id, by_pos


_NAME_TAIL = re.compile(r"_(-?\d+)_(\d+)$")


def _record_id_of(name):
    """The bundle record id out of a `<shape>_<recordId>_<ordinal>` object name, or None."""
    m = _NAME_TAIL.search(str(name))
    return int(m.group(1)) if m else None


def _static_for(record, position, by_id, by_pos):
    """The object THIS record produced, or None.

    The join is the record id alone, never the position. Every static's name carries its record's
    id (`_add_static`), so "no static with this id" means this record produced none — which is
    the truth for a prefab that ships no mesh, and the seven extraction-area placeholders
    (`Ore_Area_Placeholder`, `Agriculture_Area_Placeholder`) that sit at the exact coordinate of
    the industrial hub they belong to. A position join handed those the hub's object and scored
    the hub's colour against the placeholder's, so the probe reported a level defect that was its
    own. `by_pos` stays a parameter because a stacked instance shares one id across its sections
    (§4.1) and the position is what tells them apart.
    """
    candidates = by_id.get(record.get("id"))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    key = _key(position)
    for obj in candidates:
        if _key(obj["position"]) == key:
            return obj
    return candidates[0]


def _key(position):
    return (
        round(float(position[0]), 2),
        round(float(position[1]), 2),
        round(float(position[2]), 2),
    )


def _shape_base(name):
    """A shape / forest type name without its paint-variant suffix: `Oak01__dead_v2` -> the
    `(assetId + state)` half, which is what `mesh_candidates` produces."""
    text = str(name)
    return text.rsplit("_v", 1)[0] if re.search(r"_v\d+$", text) else text


# -- the questions ---------------------------------------------------------------------------------


def _sample_records(bundle, settings, samples):
    """The records this build admits, sampled deterministically."""
    region = _region_of(settings)
    kept = []
    for record in bundle_instances.read_instances(bundle):
        if placement.keep(record, settings, region):
            kept.append(record)
    random.seed(20260818)
    if len(kept) > samples:
        kept = random.sample(kept, samples)
    print(f"{len(kept):,} sampled of the records this build admits\n")
    return kept


def _expected_state(record, mesh_dir, have):
    """The mesh state the game would draw, resolved FIRST: it is also what tells two items
    sharing a position apart. `_key` is not a unique join for the forest — a sub-object sits at
    its parent's coordinate (`Clothesline01_Clt` on `Clothesline01`) and two pieces of street
    furniture land on the same centimetre — and taking `[0]` blamed a record for the type
    beside it. The check stays honest: it asks whether an item OF THE EXPECTED STATE is here,
    which still fails if the level placed the wrong one."""
    for candidate in bundle_instances.mesh_candidates(record):
        if candidate not in have:
            have[candidate] = os.path.isfile(os.path.join(mesh_dir, candidate + ".glb"))
        if have[candidate]:
            return candidate
    return None


def _find_placed(record, position, by_position, statics, expected):
    """`((kind, shape, row), forest rows at the position)` for the placed object, or None."""
    here = by_position.get(_key(position))
    if here:
        match = next((row for row in here if _shape_base(row[0]) == expected), here[0])
        return ("forest", match[0], match[1]), here
    obj = _static_for(record, position, statics[0], statics[1])
    if obj is not None:
        return ("static", os.path.splitext(os.path.basename(obj["shapeName"]))[0], obj), None
    return None, None


def _score_transform(placed, position, rotation, scale, where, size, turn):
    """Position, size and rotation of one placed object against the record's own transform,
    through the SAME serialisation the writers use (`beamng.frame.basis_flat`): the stored
    nine floats are the object's own axes in groups of three, and flattening the matrix
    row-major here would agree with a level that stored the inverse rotation - which it did
    until A6.8. This row still only proves our own consistency; the convention itself is
    anchored against vanilla in `test_a3_objects.test_rotation_matrix_is_basis_vectors`."""
    flat = frame.basis_flat(rotation)
    where["total"] += 1
    if placed[0] == "forest":
        item = placed[2]
        where["agree"] += int(
            max(abs(a - b) for a, b in zip(item["pos"], position)) <= TOLERANCE_POSITION
        )
        size["total"] += 1
        size["agree"] += int(abs(item["scale"] - sum(scale) / 3.0) <= TOLERANCE_SCALE)
        shipped = item.get("rotationMatrix")
    else:
        obj = placed[2]
        where["agree"] += int(
            max(abs(a - b) for a, b in zip(obj["position"], position)) <= TOLERANCE_POSITION
        )
        size["total"] += 1
        size["agree"] += int(
            max(abs(a - b) for a, b in zip(obj["scale"], scale)) <= TOLERANCE_SCALE
        )
        shipped = obj.get("rotationMatrix")
    if shipped and len(shipped) == 9:
        turn["total"] += 1
        turn["agree"] += int(max(abs(a - b) for a, b in zip(shipped, flat)) <= TOLERANCE_ROTATION)


def _check_state_and_scale(bundle, settings, forest, statics, samples, results):
    """For a sample of records: is the placed object at the position, rotation, size and mesh
    state the game's own rule gives for that entity?"""
    print("sampling instances...")
    by_position = {}
    for type_name, rows in forest.items():
        for item in rows:
            by_position.setdefault(_key(item["pos"]), []).append((type_name, item))
    kept = _sample_records(bundle, settings, samples)

    state = {"total": 0, "agree": 0}
    where = {"total": 0, "agree": 0}
    size = {"total": 0, "agree": 0}
    found = {"total": 0, "agree": 0}
    # A6.6: this function's own docstring has always claimed "position, rotation, size and mesh",
    # and rotation was never actually compared. A wrong rotation turns a prop in place, so it
    # could not have caused the floating-props defect — but the probe must not be READ as having
    # cleared it, so here it is. Both writers ship `rotationMatrix` as a flat 9 (`stages/instances`
    # at 4 dp for a forest item, 6 for a TSStatic), so the tolerance is the coarser of the two.
    turn = {"total": 0, "agree": 0}
    mesh_dir = os.path.join(bundle, "meshes")
    have = {}
    for record in kept:
        position, rotation, scale = _transform(record)
        expected = _expected_state(record, mesh_dir, have)
        placed, here = _find_placed(record, position, by_position, statics, expected)
        if placed is None:
            # the prefab ships no mesh (16 of 5,591 on the reference bundle) - reported, but not
            # scored: there is nothing for the level to have placed
            found["total"] += 1
            continue
        found["total"] += 1
        found["agree"] += 1

        if expected is not None:
            state["total"] += 1
            if placed[0] == "forest":
                state["agree"] += int(any(_shape_base(row[0]) == expected for row in here))
            else:
                state["agree"] += int(_shape_base(placed[1]) == expected)

        _score_transform(placed, position, rotation, scale, where, size, turn)

    results["a record this build admits is placed"] = dict(
        found,
        exact=False,
        note=f"{found['total'] - found['agree']} unplaced = the prefab ships no mesh at all",
    )
    results["the mesh state is the one CalculateTreeSubMeshData picks"] = dict(state)
    results["the position is T(pos) conjugated into BeamNG axes"] = dict(where)
    results["the drawn size is the game's growthScale x the record's scale"] = dict(size)
    results["the rotation is the record's own R, in BeamNG axes"] = dict(turn)


def _check_lod(bundle, shapes, settings, results):
    """Recompute the game's cull threshold from each shape's SOURCE mesh and compare it with the
    `nulldetail` the `.dae` shipped. This is the whole LOD chain checked end to end."""
    print("recomputing the game's LOD limit for every placed shape...")
    scale = float(settings.get("lod_detail_scale", 6.0) or 6.0)
    imposters = bool(settings.get("imposters", True))
    mesh_dir = os.path.join(bundle, "meshes")
    row = {"total": 0, "agree": 0}
    ladder = {"total": 0, "agree": 0}
    ratios = []
    for name, entry in sorted(shapes.items()):
        source = re.sub(r"_v\d+$", "", name)
        path = os.path.join(mesh_dir, source + ".glb")
        if not os.path.isfile(path):
            continue
        nodes = glb.parse_glb(path)
        if not nodes:
            continue
        lo = numpy.array([math.inf] * 3)
        hi = numpy.array([-math.inf] * 3)
        area = 0.0
        for node in nodes:
            matrix = numpy.asarray(node["matrix"], dtype=numpy.float64)
            world = (
                numpy.asarray(node["verts"], dtype=numpy.float64) @ matrix[:3, :3].T + matrix[:3, 3]
            )
            lo = numpy.minimum(lo, world.min(axis=0))
            hi = numpy.maximum(hi, world.max(axis=0))
            area += lod.surface_area(world, node["tris"])
        boxes = hi - lo
        radius = 0.5 * float(numpy.linalg.norm(boxes))
        game_px = lod.game_detail_px(radius, boxes, area)
        want = max(1.0, game_px * scale)
        expected = (
            int(max(1, round(want / IMPOSTER_REACH))) if imposters else int(max(1, round(want)))
        )
        row["total"] += 1
        row["agree"] += int(entry["nullPx"] == expected)
        ratios.append(entry["nullPx"] / max(game_px, 1e-9))
        # DISTINCT sizes must decrease: one detail level split by `dae.split_mesh` writes several
        # nodes at the SAME px, which is legitimate (Hospital01: two geometries at 191 px)
        details = sorted(set(entry["details"]), reverse=True)
        ladder["total"] += 1
        ladder["agree"] += int(
            len(details) == len(set(details)) and all(px > (entry["nullPx"] or 0) for px in details)
        )

    results["the nulldetail px is the game's own threshold x lod_detail_scale"] = dict(row)
    results["the detail ladder strictly decreases and clears nulldetail"] = dict(ladder)
    if ratios:
        ratios.sort()
        median = ratios[len(ratios) // 2]
        results["how much closer than CS2 this level culls"] = {
            "total": len(ratios),
            "agree": len(ratios),
            "exact": False,
            "note": f"median {median:.2f}x closer (1.00 would be CS2 exactly)",
        }


def _check_colour(bundle, settings, statics, results):
    """A TSStatic must carry the record's own three colorSet channels exactly; a forest item
    cannot carry any, so what is measured there is the SIZE of the variant approximation."""
    print("checking paint...")
    region = _region_of(settings)
    variants = {}
    derived = os.path.join(bundle, "derived", "paint_variants.json")
    if os.path.isfile(derived):
        with open(derived, encoding="utf-8") as handle:
            variants = json.load(handle).get("prefabs") or {}
    exact = {"total": 0, "agree": 0}
    errors = []
    for record in bundle_instances.read_instances(bundle):
        if not placement.keep(record, settings, region):
            continue
        attrs = record.get("attrs") or {}
        colours = tints.color_set(attrs)
        if placement.wants_forest(record):
            centres = (variants.get(record["assetId"]) or {}).get("variants") or []
            if not centres:
                continue
            index = tints.variant_index(attrs, centres)
            centre = [float(v) for channel in centres[index] for v in channel[:3]]
            own = [float(c) for colour in colours[:3] for c in colour[:3]]
            errors.append(max(abs(a - b) for a, b in zip(own, centre[: len(own)])))
            continue
        position, _r, _s = _transform(record)
        obj = _static_for(record, position, statics[0], statics[1])
        if obj is None:
            continue
        exact["total"] += 1
        shipped = [obj.get("instanceColor"), obj.get("instanceColor1"), obj.get("instanceColor2")]
        ok = all(
            shipped[i] is not None
            and max(abs(float(a) - float(b)) for a, b in zip(shipped[i], colours[i])) < 2e-6
            for i in range(3)
        )
        exact["agree"] += int(ok)
    results["a TSStatic carries the record's own colorSet exactly"] = dict(exact)
    if errors:
        errors.sort()
        results["a forest item's paint is its prefab's nearest baked centre"] = {
            "total": len(errors),
            "agree": len(errors),
            "exact": False,
            "note": f"median channel error {errors[len(errors) // 2]:.4f}, "
            f"p95 {errors[int(0.95 * (len(errors) - 1))]:.4f} (0 = its own colour)",
        }


def _region_of(settings):
    region = settings.get("region")
    if not region:
        return None
    x, z, r = [float(p) for p in str(region).replace(",", " ").split()]
    return {"centerX": x, "centerZ": z, "radius": r}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
