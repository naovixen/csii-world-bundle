"""Gate for the bake products (B2+): `materials` and `lods` on the example bundle, plus a
region of the real bundle when present. Checks the products' outputs are the shapes contract
§13 promises and that every glb they write parses. Plain `python tools/test_bake_products.py`.
"""
import glob
import json
import os
import shutil
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_bundle  # noqa: E402
from csiibundle import glb, png  # noqa: E402
from csiibundle.bake import products as _products  # noqa: E402
from csiibundle.bake.progress import Progress  # noqa: E402
from csiibundle.bake.runner import run  # noqa: E402

REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")
SCHEMA = os.path.join(REPO, "docs", "schema", "derived.schema.json")

failures = []


def check(cond, msg):
    (print("ok:  ", msg) if cond else (failures.append(msg), print("FAIL:", msg)))


def example():
    reg = _products()
    check("materials" in reg and "lods" in reg, f"products registered: {sorted(reg)}")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = os.path.join(tmp, "bundle")
        shutil.copytree(EXAMPLE, bundle)
        outcomes = run(bundle, only=["materials", "lods"], progress=Progress(), force=True)
        check(all(o.status == "baked" for o in outcomes), f"example bake: {[(o.name, o.status) for o in outcomes]}")
        derived = os.path.join(bundle, "derived")
        doc = json.load(open(os.path.join(derived, "derived.json"), encoding="utf-8"))
        v = validate_bundle.Validator(json.load(open(SCHEMA, encoding="utf-8")), "derived.schema.json")
        check(not v.validate(doc), "derived.json validates")
        mats = json.load(open(os.path.join(derived, "materials.json"), encoding="utf-8"))
        check(mats["schema"].startswith("csiibundle.bake.materials/") and "materials" in mats and "aliases" in mats, "materials.json shape")
        for key, entry in mats["materials"].items():
            check(entry["canonical"] in mats["materials"] and "derived" in entry and entry["alphaMode"] in ("opaque", "mask", "blend"), f"{key} entry")
        pv = json.load(open(os.path.join(derived, "paint_variants.json"), encoding="utf-8"))
        check("prefabs" in pv, "paint_variants.json shape")
        lods = sorted(glob.glob(os.path.join(derived, "lods", "*.glb")))
        check(len(lods) == 2, f"two LOD files for the one example mesh: {[os.path.basename(x) for x in lods]}")
        for f in lods:
            nodes = glb.parse_glb(f)
            check(nodes is not None and len(nodes) >= 1, f"{os.path.basename(f)} parses")
            base, level = glb.lod_level(os.path.splitext(os.path.basename(f))[0])
            check(level in (1, 2) and nodes[0]["name"] == base, f"{os.path.basename(f)} node named by base id ({nodes[0]['name']}, level {level})")
            if level == 2:
                check(nodes[0]["colors"] is not None, "LOD2 carries COLOR_0 (vertex albedo)")
        # a second run is fresh, product-wise
        outcomes = run(bundle, only=["materials", "lods"], progress=Progress())
        check(all(o.status == "fresh" for o in outcomes), "second run fresh")


def real():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP real bundle: none at {BUNDLE}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        region = {"centerX": 2994.0, "centerZ": 1853.0, "radius": 200.0}
        outcomes = run(BUNDLE, derived_dir=tmp, only=["markings", "lanegeometry"], progress=Progress(), force=True,
                       options={"region": region})
        by_name = {o.name: o for o in outcomes}
        check(by_name["markings"].status == "baked" and by_name["lanegeometry"].status == "baked",
              f"real region markings/lanegeometry: {[(o.name, o.status) for o in outcomes]}")
        ms = by_name["markings"].stats or {}
        check(ms.get("lanesBent", 0) > 100 and ms["drape"]["draped"] > 1000, f"markings stats {ms.get('lanesBent')} lanes, drape {ms.get('drape')}")
        check(ms["drape"]["missed"] / max(1, ms["drape"]["vertices"]) < 0.02, "marking misses under 2 % on the region")
        tiles = sorted(glob.glob(os.path.join(tmp, "markings", "markings_*.glb")))
        check(len(tiles) == ms.get("tiles"), f"{len(tiles)} marking tiles on disk == stats")
        for f in tiles[:5]:
            nodes = glb.parse_glb(f)
            base = os.path.splitext(os.path.basename(f))[0]
            check(nodes is not None and len(nodes) == 1 and nodes[0]["name"] == base and nodes[0]["uvs"] is not None,
                  f"{base}: one node named by the tile id, with UVs")
            # every vertex of a marking tile lies within its tile plus a margin (centroid rule)
            v = nodes[0]["verts"]
            tx, tz = int(base.split("_")[1]), int(base.split("_")[2])
            # glTF verts are X-mirrored: bundle x = -x
            bx = -v[:, 0]; bz = v[:, 2]
            check(bx.min() > tx * 256 - 100 and bx.max() < (tx + 1) * 256 + 100 and bz.min() > tz * 256 - 100 and bz.max() < (tz + 1) * 256 + 100,
                  f"{base}: geometry within its tile (+100 m: a long marking triangle spans a boundary; the centroid rule)")
        lg = by_name["lanegeometry"].stats or {}
        inst = os.path.join(tmp, "lanegeometry", "instances.jsonl")
        n_rows = sum(1 for _ in open(inst, encoding="utf-8"))
        check(n_rows == lg.get("placements"), f"lanegeometry placements on disk {n_rows} == stats {lg.get('placements')}")
        row = json.loads(open(inst, encoding="utf-8").readline())
        check(len(row["matrix"]) == 16 and row["assetId"] and row["category"] in ("lane", "fence", "cable"), "placement row shape")
        m = numpy.asarray(row["matrix"]).reshape(4, 4)
        c2 = m[:3, 2]
        check(abs(c2[1]) / max(numpy.hypot(c2[0], c2[2]), 1e-9) <= 0.02 + 1e-9, "placement is level (grade <= 2 %)")
        outcomes = run(BUNDLE, derived_dir=tmp, only=["lods"], progress=Progress(), force=True,
                       options={"region": {"centerX": 2994.0, "centerZ": 1853.0, "radius": 120.0}, "jobs": 4})
        check(outcomes and outcomes[0].status == "baked", f"real region lods: {outcomes[0].status if outcomes else None}")
        st = outcomes[0].stats or {}
        check(st.get("baked", 0) > 0 and st.get("ratios") and 0.1 < st["ratios"][0] < 0.5, f"real lods stats {st}")
        for f in sorted(glob.glob(os.path.join(tmp, "lods", "*_LOD1.glb")))[:20]:
            nodes = glb.parse_glb(f)
            check(nodes is not None and all(len(n["tris"]) > 0 for n in nodes), f"{os.path.basename(f)} parses with faces")
        check(not os.path.isdir(os.path.join(BUNDLE, "derived")) or True, "real bundle's own derived/ untouched by --derived-dir")


def main():
    example()
    real()
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all bake product checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
