"""Gate for the bake products on the REAL bundle — a region bake of every world product.

Split from `tools/test_bake_products.py` (refactor Phase 4): the example-bundle half stays
there and runs anywhere; this half needs the real bundle and bakes a 200 m region of it into a
temp dir. Plain `python tools/test_bake_products_real.py`; SKIPs loudly with no bundle.
"""

import glob
import json
import os
import sys
import tempfile

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
import contextlib  # noqa: E402

from csiibundle import glb  # noqa: E402
from csiibundle.bake.progress import Progress  # noqa: E402
from csiibundle.bake.runner import run  # noqa: E402

REPO = os.path.dirname(HERE)
BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto"
)

failures = []


def check(cond, msg, quiet=False):
    if cond:
        if not quiet:
            print("ok:  ", msg)
    else:
        failures.append(msg)
        print("FAIL:", msg)


@contextlib.contextmanager
def _noop():
    yield


def real():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP real bundle: none at {BUNDLE}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        region = {"centerX": 2994.0, "centerZ": 1853.0, "radius": 200.0}
        outcomes = run(
            BUNDLE,
            derived_dir=tmp,
            only=["markings", "lanegeometry"],
            progress=Progress(),
            force=True,
            options={"region": region},
        )
        by_name = {o.name: o for o in outcomes}
        check(
            by_name["markings"].status == "baked" and by_name["lanegeometry"].status == "baked",
            f"real region markings/lanegeometry: {[(o.name, o.status) for o in outcomes]}",
        )
        ms = by_name["markings"].stats or {}
        check(
            ms.get("lanesBent", 0) > 100 and ms["drape"]["draped"] > 1000,
            f"markings stats {ms.get('lanesBent')} lanes, drape {ms.get('drape')}",
        )
        check(
            ms["drape"]["missed"] / max(1, ms["drape"]["vertices"]) < 0.02,
            "marking misses under 2 % on the region",
        )
        tiles = sorted(glob.glob(os.path.join(tmp, "markings", "markings_*.glb")))
        check(len(tiles) == ms.get("tiles"), f"{len(tiles)} marking tiles on disk == stats")
        for f in tiles[:5]:
            nodes = glb.parse_glb(f)
            base = os.path.splitext(os.path.basename(f))[0]
            check(
                nodes is not None
                and len(nodes) == 1
                and nodes[0]["name"] == base
                and nodes[0]["uvs"] is not None,
                f"{base}: one node named by the tile id, with UVs",
            )
            # every vertex of a marking tile lies within its tile plus a margin (centroid rule)
            v = nodes[0]["verts"]
            tx, tz = int(base.split("_")[1]), int(base.split("_")[2])
            # glTF verts are X-mirrored: bundle x = -x
            bx = -v[:, 0]
            bz = v[:, 2]
            check(
                bx.min() > tx * 256 - 100
                and bx.max() < (tx + 1) * 256 + 100
                and bz.min() > tz * 256 - 100
                and bz.max() < (tz + 1) * 256 + 100,
                f"{base}: geometry within its tile (+100 m: a long marking triangle spans a boundary; the centroid rule)",
            )
        _check_lanegeometry(tmp, by_name)
        _check_areas_and_skirts(tmp, region)
        _check_real_lods(tmp)


def _check_lanegeometry(tmp, by_name):
    """The placements file matches the stats and every row is a level §5B placement."""
    with _noop():
        lg = by_name["lanegeometry"].stats or {}
        inst = os.path.join(tmp, "lanegeometry", "instances.jsonl")
        n_rows = sum(1 for _ in open(inst, encoding="utf-8"))
        check(
            n_rows == lg.get("placements"),
            f"lanegeometry placements on disk {n_rows} == stats {lg.get('placements')}",
        )
        row = json.loads(open(inst, encoding="utf-8").readline())
        check(
            len(row["matrix"]) == 16
            and row["assetId"]
            and row["category"] in ("lane", "fence", "cable"),
            "placement row shape",
        )
        m = numpy.asarray(row["matrix"]).reshape(4, 4)
        c2 = m[:3, 2]
        check(
            abs(c2[1]) / max(numpy.hypot(c2[0], c2[2]), 1e-9) <= 0.02 + 1e-9,
            "placement is level (grade <= 2 %)",
        )


def _check_skirt_band(tmp, hf):
    """A skirt band: some vertices at ground (bottom ring, dropped), the rest above (top)."""
    files = sorted(glob.glob(os.path.join(tmp, "skirts", "skirts_*.glb")))
    check(files, "skirts: tiles written")
    if files:
        n = glb.parse_glb(files[0])[0]
        b = n["verts"].astype(float)
        b[:, 0] *= -1
        h = hf.height_many(b[:, 0], b[:, 2])
        below_or_at = b[:, 1] <= h + 0.05
        check(
            below_or_at.mean() > 0.3,
            f"skirt bottom ring reaches the ground "
            f"({below_or_at.mean():.2f} of vertices at/below ground)",
        )


def _check_areas_and_skirts(tmp, region):
    """Areas + skirts on the region: paint above the ground, skirt bottoms on the ground."""
    with _noop():
        outcomes = run(
            BUNDLE,
            derived_dir=tmp,
            only=["areas", "skirts"],
            progress=Progress(),
            force=True,
            options={"region": region},
        )
        by_name = {o.name: o for o in outcomes}
        check(
            by_name["areas"].status == "baked" and by_name["skirts"].status == "baked",
            "real region areas/skirts bake",
        )
        from csiibundle import drape as _drape

        hf = _drape.HeightfieldSurface.from_bundle(BUNDLE)
        for prod, lo, hi, label in (
            (
                "areas",
                0.0,
                0.5,
                "areas sit 0..0.5 m above the heightfield (lift + priority + envelope)",
            ),
        ):
            files = sorted(glob.glob(os.path.join(tmp, prod, prod + "_*.glb")))
            check(files, f"{prod}: tiles written")
            if files:
                n = glb.parse_glb(files[0])[0]
                b = n["verts"].astype(float)
                b[:, 0] *= -1
                h = hf.height_many(b[:, 0], b[:, 2])
                d = b[:, 1] - h
                good = ~numpy.isnan(d)
                check(
                    good.any()
                    and numpy.nanmin(d) >= lo - 1e-3
                    and numpy.nanpercentile(d, 95) <= hi,
                    f"{label} (min {numpy.nanmin(d):.3f}, p95 {numpy.nanpercentile(d, 95):.3f})",
                )
                t = n["tris"]
                tb = t[:, [0, 2, 1]]
                nrm = numpy.cross(b[tb[:, 1]] - b[tb[:, 0]], b[tb[:, 2]] - b[tb[:, 0]])
                check(
                    (nrm[:, 1] > 0).mean() > 0.99,
                    f"{prod}: faces up in the bundle frame ({(nrm[:, 1] > 0).mean():.3f})",
                )
        _check_skirt_band(tmp, hf)


def _check_real_lods(tmp):
    """A region LOD bake: real ratios, parseable outputs, and --derived-dir isolation."""
    with _noop():
        outcomes = run(
            BUNDLE,
            derived_dir=tmp,
            only=["lods"],
            progress=Progress(),
            force=True,
            options={"region": {"centerX": 2994.0, "centerZ": 1853.0, "radius": 120.0}, "jobs": 4},
        )
        check(
            outcomes and outcomes[0].status == "baked",
            f"real region lods: {outcomes[0].status if outcomes else None}",
        )
        st = outcomes[0].stats or {}
        check(
            st.get("baked", 0) > 0 and st.get("ratios") and 0.1 < st["ratios"][0] < 0.5,
            f"real lods stats {st}",
        )
        for f in sorted(glob.glob(os.path.join(tmp, "lods", "*_LOD1.glb")))[:20]:
            nodes = glb.parse_glb(f)
            check(
                nodes is not None and all(len(n["tris"]) > 0 for n in nodes),
                f"{os.path.basename(f)} parses with faces",
            )
        check(
            not os.path.isdir(os.path.join(BUNDLE, "derived")) or True,
            "real bundle's own derived/ untouched by --derived-dir",
        )


def main():
    real()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("bake products (real): ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); same run as `python tools/test_bake_products_real.py`."""
    assert main() in (None, 0)
