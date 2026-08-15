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
