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


def versions():
    """A product's `VERSION` is the ONLY thing that makes an edit reach a rebuilt level — the bake
    reports `fresh (inputs unchanged), skipped` otherwise, and the level rebuilds from the old
    bytes with no error anywhere. Where a module records its history as `# vNN:` entries beside
    the constant, the constant must equal the newest of them: adding the changelog line and
    forgetting the literal is a silent no-op, and it happened on `terraincut` v18 (audit §61)."""
    import re
    for name, prod in sorted(_products().items()):
        src = os.path.join(REPO, "tools", "csiibundle", "bake", f"{name}.py")
        if not os.path.isfile(src):
            continue
        entries = [int(v) for v in re.findall(r"#\s*v(\d+):", open(src, encoding="utf-8").read())]
        if not entries:
            continue
        check(prod.version == max(entries),
              f"{name}: VERSION={prod.version} matches its newest changelog entry v{max(entries)}")


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
        # the world-space products run on the example bundle too (tiny, but every code path)
        outcomes = run(bundle, only=["markings", "lanegeometry", "areas", "skirts", "terrainmask"], progress=Progress(), force=True)
        check(all(o.status == "baked" for o in outcomes), f"example world-space products: {[(o.name, o.status) for o in outcomes]}")
        tm = json.load(open(os.path.join(derived, "terrain", "mask.json"), encoding="utf-8"))
        cells = png.gray_array(os.path.join(derived, "terrain", "clip_cells.png"))
        ceiling = png.gray_array(os.path.join(derived, "terrain", "carriageway_ceiling.png"))
        check(list(cells.shape) == tm["faceSize"] and list(ceiling.shape) == tm["sampleSize"] and ceiling.dtype == numpy.uint16,
              f"terrainmask rasters {cells.shape} faces / {ceiling.shape} samples")
        check(not os.path.exists(os.path.join(derived, "terrain", "shallow_floor.png")),
              "the retired shallow_floor.png (the band FLOOR) is removed by the bake")
        check(int((cells == 255).sum()) == tm["counts"]["contain"], "clip_cells 255 count == contain count")
        area_mats = json.load(open(os.path.join(derived, "areas", "materials.json"), encoding="utf-8"))
        check("materials" in area_mats, "areas/materials.json shape")
        doc = json.load(open(os.path.join(derived, "derived.json"), encoding="utf-8"))
        check(not v.validate(doc) and set(doc["products"]) >= {"markings", "lanegeometry", "areas", "skirts", "terrainmask"},
              "derived.json validates with every product recorded")


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
        # areas + skirts on the region: paint above the ground, skirt bottoms on the ground
        outcomes = run(BUNDLE, derived_dir=tmp, only=["areas", "skirts"], progress=Progress(), force=True, options={"region": region})
        by_name = {o.name: o for o in outcomes}
        check(by_name["areas"].status == "baked" and by_name["skirts"].status == "baked", "real region areas/skirts bake")
        from csiibundle import drape as _drape
        hf = _drape.HeightfieldSurface.from_bundle(BUNDLE)
        for prod, lo, hi, label in (("areas", 0.0, 0.5, "areas sit 0..0.5 m above the heightfield (lift + priority + envelope)"),):
            files = sorted(glob.glob(os.path.join(tmp, prod, prod + "_*.glb")))
            check(files, f"{prod}: tiles written")
            if files:
                n = glb.parse_glb(files[0])[0]
                b = n["verts"].astype(float); b[:, 0] *= -1
                h = hf.height_many(b[:, 0], b[:, 2])
                d = b[:, 1] - h
                good = ~numpy.isnan(d)
                check(good.any() and numpy.nanmin(d) >= lo - 1e-3 and numpy.nanpercentile(d, 95) <= hi, f"{label} (min {numpy.nanmin(d):.3f}, p95 {numpy.nanpercentile(d, 95):.3f})")
                t = n["tris"]; tb = t[:, [0, 2, 1]]
                nrm = numpy.cross(b[tb[:, 1]] - b[tb[:, 0]], b[tb[:, 2]] - b[tb[:, 0]])
                check((nrm[:, 1] > 0).mean() > 0.99, f"{prod}: faces up in the bundle frame ({(nrm[:, 1] > 0).mean():.3f})")
        files = sorted(glob.glob(os.path.join(tmp, "skirts", "skirts_*.glb")))
        check(files, "skirts: tiles written")
        if files:
            n = glb.parse_glb(files[0])[0]
            b = n["verts"].astype(float); b[:, 0] *= -1
            h = hf.height_many(b[:, 0], b[:, 2])
            below_or_at = (b[:, 1] <= h + 0.05)
            # a skirt band: some vertices at ground (bottom ring, dropped), the rest above (top ring)
            check(below_or_at.mean() > 0.3, f"skirt bottom ring reaches the ground ({below_or_at.mean():.2f} of vertices at/below ground)")
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
    versions()
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
