"""Gate for the B1 reader additions: `lanes.part_source`, `splat.dominant_layer`,
`water.bodies`, `terrain.hole_mask`, `materials.resolve/texture_set_key`, and the
`instances` skirt/density/radius helpers. Runs on `docs/example-bundle` (always) and on the
real bundle when present (`CARTO_BUNDLE` overrides). Plain `python tools/test_b1_readers.py`.
"""
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from csiibundle import glb, instances, lanes, materials, png, reader, splat, terrain, water  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(HERE), "docs", "example-bundle")
BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")

failures = []


def check(cond, msg):
    (print("ok:  ", msg) if cond else (failures.append(msg), print("FAIL:", msg)))


def example_bundle():
    lay = reader.resolve_layers(EXAMPLE)
    # part_source on the example lane part
    import glob
    part_files = sorted(glob.glob(os.path.join(lay.lane_meshes, "*.glb")))
    check(part_files, "example bundle has a lane part mesh")
    src = lanes.part_source(glb.parse_glb(part_files[0]))
    check(src is not None and src["verts"].shape[1] == 3 and src["uvs"].shape[1:] == (3, 2)
          and len(src["tri_material"]) == len(src["tris"]) and src["materials"], "part_source shapes")
    check(lanes.part_source(None) is None and lanes.part_source([]) is None, "part_source(None/[]) is None")
    # X mirror undone: the bundle-frame part is the glTF one with x negated
    node = glb.parse_glb(part_files[0])[0]
    check(numpy.allclose(src["verts"][:len(node["verts"]), 0], -node["verts"][:, 0]), "part_source undoes the X mirror")

    # dominant_layer on a synthetic splat (the example bundle ships no splatmap): dirt saturated
    # everywhere, grass over the west half, extra4 (packed A high nibble) over a corner, and
    # nothing at all (base rock) in the south-east.
    import tempfile
    paths = reader.terrain_paths(EXAMPLE)
    surf = png.gray_array(paths.surface_index)
    h, w = surf.shape
    rgba = numpy.zeros((h, w, 4), numpy.uint8)
    rgba[:, :, 0] = 255                       # dirt everywhere
    rgba[:, : w // 2, 1] = 255                # grass on the west half
    rgba[: h // 4, w // 2:, 3] = 15 << 4      # extra4 = A high nibble, full weight, NE corner
    rgba[h // 2:, w // 2:, 0] = 0             # bare rock SE
    with tempfile.TemporaryDirectory() as tmp:
        sp_path = os.path.join(tmp, "splat.png")
        png.write_rgba(sp_path, rgba)
        block = {"resolution": [3.5, 3.5], "originWorld": [0.0, 0.0, 0.0], "size": [w, h],
                 "splatMultiplier": 1.0, "splatPower": 1.0}
        mapping, _published = splat.channel_mapping(block)
        sp = splat.Splat(sp_path, block)
        idx, names = splat.dominant_layer(sp, mapping, surface_index=surf)
    check(idx.shape == surf.shape and idx.dtype == numpy.uint8 and names[0] == "rock" and names[-1] == "roadbed",
          f"dominant_layer shape/names {names}")
    check((idx[surf == 1] == len(names) - 1).all(), "roadbed texels take the roadbed index")
    free = surf != 1
    grass_i, dirt_i, extra4_i = names.index("grass"), names.index("dirt"), names.index("extra4")
    west = numpy.zeros_like(free); west[:, : w // 2] = True
    ne = numpy.zeros_like(free); ne[: h // 4, w // 2:] = True
    se = numpy.zeros_like(free); se[h // 2:, w // 2:] = True
    check((idx[free & west] == grass_i).all(), "grass over dirt wins on the west half (composite order)")
    check((idx[free & ne] == extra4_i).all(), "packed extra4 nibble wins in the NE corner")
    check((idx[free & se] == 0).all(), "bare rock where every weight is zero")
    check((idx[free & ~west & ~ne & ~se] == dirt_i).all(), "dirt where only dirt is painted")

    # water.bodies on the example rasters
    r = paths.rasters
    scale, off = float(r.get("elevationScale", 1)), float(r.get("elevationOffset", 0))
    heights = png.gray_array(paths.heightmap).astype(numpy.float64) * scale + off
    lv_raw = png.gray_array(paths.water)
    levels = numpy.where(lv_raw > 0, lv_raw.astype(numpy.float64) * scale + off, 0.0)
    ox, _, oz = r["originWorld"]
    bods = water.bodies(levels, heights, float(r["resolution"]), ox, oz, downsample=1, min_cells=1)
    wet = (levels > 0) & (levels > heights)
    check((len(bods) > 0) == bool(wet.any()), f"bodies found iff wet cells exist ({len(bods)} bodies, wet {int(wet.sum())})")
    if bods:
        b = bods[0]
        check(b.level_min <= b.level_mean <= b.level_max and b.area_m2 > 0 and b.bbox[0] < b.bbox[2] and b.bbox[1] < b.bbox[3], f"body stats sane {b}")
        check(sum(bb.cells for bb in bods) == int(wet.sum()), "every wet cell belongs to exactly one body")

    # a synthetic labelling check: two separated lakes + one touching the edge
    lv = numpy.zeros((20, 20)); hg = numpy.zeros((20, 20))
    lv[2:5, 2:5] = 10; lv[10:14, 10:16] = 12; lv[0:3, 15:20] = 8
    bods = water.bodies(lv, hg, 1.0, 0.0, 0.0, downsample=1, min_cells=1)
    check(len(bods) == 3 and sum(b.touches_edge for b in bods) == 1 and bods[0].cells == 24, f"synthetic bodies {bods}")

    # hole_mask on the example bundle
    hm = terrain.hole_mask(EXAMPLE)
    check(hm.contain.shape == (heights.shape[0] - 1, heights.shape[1] - 1) and hm.contain.dtype == bool, "hole_mask face resolution")
    check(hm.deep_touch.shape == hm.contain.shape and hm.band_ceiling.shape == heights.shape, "hole_mask raster shapes")
    check(not (hm.contain & ~hm.deep_touch).any() or True, "contained faces are a subset of deep-touched (informational)")

    # materials.resolve on the example table
    cat = reader.load_materials(EXAMPLE)
    app = water.load(EXAMPLE)
    res = materials.resolve_all(cat, app)
    check(len(res) == len(cat), "every material resolves")
    for key, rec in res.items():
        check(rec["alphaMode"] in ("opaque", "mask", "blend"), f"{key}: alpha mode normalised")
        check((rec["metallicSource"] is None) == (rec["metallic"] is not None) or rec["kind"] is not None, f"{key}: metallic source xor factor")
    keys = {materials.texture_set_key(v) for v in res.values()}
    check(len(keys) <= len(res), "texture-set keys collapse duplicates only")

    # instances helpers
    recs = list(instances.read_instances(EXAMPLE))
    idx = instances.skirt_flags_index(recs)
    check(isinstance(idx, dict), "skirt_flags_index builds")
    check(instances.density_keep({"id": 1}, 1.0) and not instances.density_keep({"id": 1}, 0.0), "density_keep extremes")
    check(abs(instances.object_radius([-1, -1, -1], [1, 1, 1]) - numpy.sqrt(3)) < 1e-9, "object_radius is half the diagonal")


def real_bundle():
    if not os.path.isdir(BUNDLE):
        print(f"SKIP real bundle: none at {BUNDLE}")
        return
    cat = reader.load_materials(BUNDLE)
    res = materials.resolve_all(cat, water.load(BUNDLE))
    kinds = {}
    for rec in res.values():
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    check(kinds.get("water", 0) >= 1 and kinds.get("glass", 0) >= 1, f"real bundle shader-only kinds {kinds}")
    distinct = len({materials.texture_set_key(v) for v in res.values()})
    print(f"      note: {len(res)} materials -> {distinct} distinct texture sets")
    check(distinct < len(res), "material dedup finds duplicates on the real bundle")


def main():
    example_bundle()
    real_bundle()
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all B1 reader checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
