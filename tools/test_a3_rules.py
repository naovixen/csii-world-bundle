"""cs2beamng A3 gate, part 2 — the BeamNG-writer rules.

Run: `python tools/test_a3_rules.py`. Split from `test_a3_objects.py` (Phase 4); the stage
tests live there and the real-city sweeps in `test_a3_city.py`.

* **rotationMatrix** is the object's basis vectors in groups of three, anchored against
  BeamNG's own shipped levels rather than our writers;
* **LOD switch points** may only ever DEFER a generated level, never advance it (A6.7);
* **the A8 performance predicates** — every texture kind capped, the footprint rule;
* **the colour palette** (A6.3) — CS2's §7.2 mask under BeamNG's own shadergen semantics,
  the alpha carrying tint coverage, and the export path that must not drop it.
"""

import itertools
import json
import os
import sys
import tempfile

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


def _switch_fixture(tmp):
    """An 8-triangle box mesh plus its two pre-written generated levels (4 and 2 triangles)."""
    from cs2beamng.beamng import dae
    from csiibundle import glbwrite

    box_v = numpy.array(
        [[0.0, 0, 0], [4, 0, 0], [4, 0, 6], [0, 0, 6], [0, 3, 0], [4, 3, 0], [4, 3, 6], [0, 3, 6]]
    )
    box_t = numpy.array(
        [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [2, 3, 7], [2, 7, 6]]
    )
    os.makedirs(os.path.join(tmp, "lods"), exist_ok=True)
    for level in (1, 2):
        glbwrite.write_glb(
            os.path.join(tmp, "lods", f"src_LOD{level}.glb"),
            [
                {
                    "name": "box",
                    "primitives": [
                        {
                            "verts": box_v,
                            "normals": None,
                            "uvs": None,
                            "tris": box_t[: 4 if level == 1 else 2],
                            "material": "mat",
                        }
                    ],
                }
            ],
        )
    return dae.Mesh("box", box_v, box_t, [("mat", len(box_t))])


def test_switch_points_only_ever_defer():
    """The A6.7 regression, as a gate: a measured displacement may push a generated level FURTHER
    out than the ladder puts it, never nearer.

    Allowing both directions shipped 2,586 of 2,903 prop shapes onto their decimated geometry
    closer than the ladder ever had — a streetlight's 24-triangle level from 13 m — and the
    level came back "cranes rotated and floating, highrises all diagonal". Surface distance is
    not visual error for thin geometry; the ladder's size-keyed spacing protects it."""
    print("LOD switch points defer, never advance")
    from cs2beamng.stages import shapelods, shapewrite  # noqa: F401

    with tempfile.TemporaryDirectory() as tmp:
        mesh = _switch_fixture(tmp)
        options = {"generate_lods": True, "switch_error_px": 6.0}
        moved = 0
        for null_px in (3.0, 9.0, 40.0):
            for metres in (None, 0.0, 0.001, 0.5, 5.0):
                _levels, stats, meshes = shapelods.lod_levels(
                    tmp,
                    "src",
                    "shape",
                    mesh,
                    null_px,
                    8.0,
                    [metres, metres],
                    "prop",
                    {},
                    {},
                    "level",
                    options,
                )
                sizes = stats["detailSizes"]
                ladder = [
                    max(2, int(round(null_px * r)))
                    for r in reversed(shapelods.LADDER_RATIOS[: len(meshes)])
                ]
                check(len(meshes) == 3, f"nullPx {null_px}: the two generated levels loaded")
                check(
                    all(got <= want + 0.5 for got, want in zip(sizes, ladder)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} never above the ladder {ladder}",
                )
                check(
                    all(a > b for a, b in itertools.pairwise(sizes)),
                    f"nullPx {null_px}, displacement {metres}: {sizes} strictly decreases",
                )
                if any(got < want - 0.5 for got, want in zip(sizes, ladder)):
                    moved += 1
        check(moved > 0, f"and a level that IS out of shape is deferred ({moved} of 15 cases)")
        # generated levels are for PROPS only: a tree is LOD 0 then the billboard (the game's
        # own trees' LOD 1 is an impostor card), a building LOD 0 / its chain and the far ring
        # (a tree loads them for their switch SIZES; `shapewrite._vegetation_imposter` then
        # replaces every one with the billboard at the first one's size)
        _levels, bld, _m = shapelods.lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "building", {}, {}, "level", options
        )
        check(bld["levels"] == 1, f"a building loads no generated level ({bld['levels']})")
        _levels, veg, _m = shapelods.lod_levels(
            tmp, "src", "shape", mesh, 9.0, 8.0, [5.0, 5.0], "tree", {}, {}, "level", options
        )
        check(veg["levels"] == 3, f"a tree still loads them for their sizes ({veg['levels']})")


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


def _cs2_tint(base, mask, colours):
    """§7.2 verbatim: each colour lerps the running tint by its own mask channel."""
    t = numpy.asarray(base, dtype=numpy.float64)
    for i in range(3):
        t = t * (1.0 - mask[i]) + (t * numpy.asarray(colours[i])) * mask[i]
    return t


def _beamng_palette(base, mask_rgba, colours):
    """`shadergen.h.hlsl::getColorPalette` verbatim: RGB normalised to a selector, ALPHA is the
    tint amount, and the untinted remainder passes the base through."""
    s = numpy.asarray(mask_rgba, dtype=numpy.float64).copy()
    s[:3] = s[:3] / (s[0] + s[1] + s[2] + 1e-10)
    a = s[3]
    s[:3] *= a
    palette = sum(s[i] * numpy.asarray(colours[i]) for i in range(3)) + (1.0 - a)
    return numpy.asarray(base, dtype=numpy.float64) * palette


class _Level:
    name = "lvl"

    def game_path(self, *p):
        return "/" + "/".join(p)

    def pid(self, k):
        return k


def test_color_palette():
    """A6.3 — CS2's colour mask converted into BeamNG's palette semantics.

    Both sides implemented literally from their own sources: CS2 from `docs/bundle-schema.md`
    §7.2, BeamNG from `shaders/common/material/shadergen/shadergen.h.hlsl::getColorPalette`.
    The old export forced the mask's alpha to 255, which is why an untinted texel rendered BLACK.
    """
    print("colour palette (A6.3)")
    from cs2beamng.beamng.textureplan import _colormask_rgba

    base = [0.55, 0.50, 0.47]
    colours = [[0.90, 0.30, 0.20], [0.20, 0.55, 0.85], [0.0, 0.0, 0.0]]

    worst = 0.0
    for m in (
        [0, 0, 0],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
        [0.3, 0, 0],
        [0, 0.75, 0],
        [0, 0, 0.5],
        [0.05, 0, 0],
    ):
        mask = numpy.asarray(m, dtype=numpy.float64)
        conv = _colormask_rgba((mask * 255).round().astype(numpy.uint8).reshape(1, 1, 3), "rgb")
        worst = max(
            worst,
            float(
                numpy.abs(
                    _beamng_palette(base, conv[0, 0] / 255.0, colours)
                    - _cs2_tint(base, mask, colours)
                ).max()
            ),
        )
    check(
        worst < 0.002,
        f"a DISJOINT mask reproduces CS2's tint exactly (worst {worst:.5f}, the alpha's own step)",
    )

    # the bug this replaces: alpha forced to 255 makes an untinted texel BLACK
    black = _beamng_palette(base, [0.0, 0.0, 0.0, 1.0], colours)
    check(
        float(numpy.abs(black).max()) < 1e-9,
        "the OLD alpha-255 export renders an untinted texel pure black (the reported defect)",
    )
    conv = _colormask_rgba(numpy.zeros((1, 1, 3), numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 0, "an untinted texel now carries alpha 0 - no tint, not black")
    conv = _colormask_rgba(numpy.array([[[255, 0, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "a fully tinted texel carries alpha 255")
    conv = _colormask_rgba(numpy.array([[[200, 100, 0]]], numpy.uint8), "rgb")
    check(int(conv[0, 0, 3]) == 255, "alpha saturates rather than wrapping")


def test_palette_material():
    """The material must actually switch the palette on, and the joined emissive slot's nits
    reach it — including a slot authored to emit NOTHING (A6.9/D5)."""
    print("colour palette: the material")
    from cs2beamng.beamng import materials as bm

    stage = bm.material_v15(_Level(), "m", {"tintable": True}, {"colorMask": "/t.dds"})
    stage0 = stage["Stages"][0] if "Stages" in stage else stage
    check(
        stage0.get("paletteBaseColor") is True,
        "a tintable material sets paletteBaseColor - without it the palette applies to nothing",
    )

    # A6.9/D5: the emissive nits come from the joined slot, INCLUDING when the slot says zero.
    # 115 of the reference city's slots do, and every one is the unlit face of a billboard; a
    # falsy test would light all of them at the flat fallback.
    lit = bm.material_v15(_Level(), "m", {"emissiveNits": 5000.0}, {"emissive": "/e.dds"})
    lit0 = lit["Stages"][0] if "Stages" in lit else lit
    check(lit0.get("emissiveIntensityNits") == 5000.0, "a joined slot's nits are used")
    off = bm.material_v15(_Level(), "m", {"emissiveNits": 0.0}, {"emissive": "/e.dds"})
    off0 = off["Stages"][0] if "Stages" in off else off
    check(
        off0.get("emissiveIntensityNits") == 0.0,
        "a slot authored to emit NOTHING stays at zero, not the flat fallback",
    )
    none = bm.material_v15(_Level(), "m", {}, {"emissive": "/e.dds"})
    none0 = none["Stages"][0] if "Stages" in none else none
    check(
        none0.get("emissiveIntensityNits") == bm.EMISSIVE_NITS
        and none0.get("emissiveFactor") == [1.0, 1.0, 1.0],
        "a material no slot reaches takes the declared fallback, white at 200 nits",
    )


def test_colormask_export():
    """THE MASK MUST REACH THE LEVEL. `199f084` wrote the conversion into `texture_name`, the
    NAMING function, where `arr` does not exist: every `colormask` export raised NameError, all
    724 masks were dropped, and the built level carried ZERO `colorPaletteMap` — so the whole of
    A6.3 was in the build report and nowhere else. `material_v15` alone cannot see that."""
    print("colour palette: the export path")
    from cs2beamng.beamng import textureplan

    try:
        name = textureplan.texture_name("/src/abc.png", "colormask", use_dds=False)
        ok = name.endswith(".colormask.png")
    except Exception as exc:
        name, ok = repr(exc), False
    check(ok, f"texture_name('colormask') returns a name rather than raising ({name})")

    written = {}

    class _LW(_Level):
        def write_bytes_if_changed(self, data, *parts):
            written[parts[-1]] = data

    src = os.path.join(tempfile.mkdtemp(), "mask.png")
    from csiibundle import png as _png

    with open(src, "wb") as h:
        h.write(_png.encode_rgb(numpy.array([[[200, 100, 0], [0, 0, 0]]], numpy.uint8)))
    out = textureplan.export_texture(_LW(), src, "colormask", dds=False)
    check(bool(out) and written, f"a colormask actually exports ({out})")
    if written:
        tmp = os.path.join(tempfile.mkdtemp(), "out.png")
        with open(tmp, "wb") as h:
            h.write(next(iter(written.values())))
        back = _png.rgba_array(tmp)
        check(
            back.shape[2] == 4 and int(back[0, 0, 3]) == 255 and int(back[0, 1, 3]) == 0,
            "the exported mask carries the tint coverage in ALPHA (255 tinted, 0 untinted)",
        )


def _grid_parts(n_quads, material="m", scale=1.0):
    """One mesh part of `2 * n_quads` triangles: a strip of unit quads along x."""
    verts, tris = [], []
    for i in range(n_quads):
        b = len(verts)
        verts += [[i, 0, 0], [i + 1, 0, 0], [i + 1, 1, 0], [i, 1, 0]]
        tris += [[b, b + 1, b + 2], [b, b + 2, b + 3]]
    verts = numpy.array(verts, dtype=numpy.float32) * scale
    normals = numpy.tile(numpy.array([[0, 0, 1]], dtype=numpy.float32), (len(verts), 1))
    uvs = numpy.zeros((len(verts), 2), dtype=numpy.float32)
    return [(verts, normals, uvs, None, numpy.array(tris), [(material, len(tris))])]


def test_residency_chain_rules():
    """Phase 10 residency: an authored level byte-identical to LOD 0 is not shipped, and over
    `authored_lod0_max_tris` the game's LOD 1 becomes the nearest level (LOD 0 left out)."""
    print("residency: the authored chain")
    from cs2beamng.beamng import daemesh, level as level_mod
    from cs2beamng.stages import shapelods

    size = numpy.array([40.0, 1.0, 10.0])
    area, radius, bias = 400.0, 20.0, 0.0
    base_parts = _grid_parts(15000)  # 30,000 triangles: a heavy building
    base = daemesh.group_by_material(daemesh.merge_meshes("Heavy", base_parts))
    copy = _grid_parts(15000)  # the LOD 1 slot filled with a copy of the full mesh
    real1 = _grid_parts(6000)  # a real LOD 1 at 40 %
    lod2 = _grid_parts(500)
    opts = {"level_of_detail": 0.7, "authored_lods": True}

    chain = shapelods.authored_chain(
        "Heavy", [(1, copy, size), (2, lod2, size)], size, area, bias, radius, 1.0, opts, base
    )
    check(chain["identicalLevels"] == [1], "a LOD 1 identical to LOD 0 is left out")
    check(len(chain["meshes"]) == 1 and len(chain["meshes"][0].tris) == 1000, "...and LOD 2 stays")
    stats = {}
    _fixed, stats, meshes = shapelods._authored_ladder(base, chain, stats)
    check(meshes[0] is base and len(meshes) == 2, "with no real LOD 1 the ladder keeps LOD 0")

    opts_drop = dict(opts, authored_lod0_max_tris=20000, authored_lod1_drop=True)
    chain = shapelods.authored_chain(
        "Heavy", [(1, real1, size), (2, lod2, size)], size, area, bias, radius, 1.0, opts_drop, base
    )
    check(chain["lod0Dropped"] and not chain["lod1Dropped"], "over the cap LOD 0 goes and LOD 1 is kept")
    check(len(chain["thresholds"]) == len(chain["meshes"]) == 2, "one threshold per shipped level")
    check(chain["colliderMesh"] is chain["meshes"][0], "the collider still comes off LOD 1")
    stats = {}
    _fixed, stats, meshes = shapelods._authored_ladder(base, chain, stats)
    check(
        len(meshes) == 2 and len(meshes[0].tris) == 12000 and stats["lod0Dropped"],
        "the ladder starts at the game's LOD 1 (12,000 triangles), LOD 0 is not in the shape",
    )
    check(stats["lod0Triangles"] == 30000, "...and the report knows what was left out")

    chain = shapelods.authored_chain(
        "Light", [(1, real1, size), (2, lod2, size)], size, area, bias, radius, 1.0,
        dict(opts, authored_lod0_max_tris=50000, authored_lod1_drop=True), base,
    )
    check(
        not chain["lod0Dropped"] and chain["lod1Dropped"] and len(chain["meshes"]) == 1,
        "under the cap the old rule stands: LOD 0 kept, LOD 1 dropped, LOD 2 keeps its band",
    )

    plain = level_mod.tsstatic("a", "/s.dae", [0, 0, 0], [1, 0, 0, 0, 1, 0, 0, 0, 1])
    tinted = level_mod.tsstatic("b", "/s.dae", [0, 0, 0], [1, 0, 0, 0, 1, 0, 0, 0, 1], instanced=True)
    check(plain["useInstanceRenderData"] is False, "a TSStatic with no colour has no instance data")
    check(tinted["useInstanceRenderData"] is True, "...one carrying a colour asks for it")


def test_authored_lod_limits_worked_example():
    """`lodchain.authored_lod_limits` against the game's loop, worked by hand for one house.

    `RequiredBatchesSystem.InitializeBatchGroup`: walk the chain COARSEST first, give each level
    the limit in hand, then raise it to `clamp(CalculateLodLimit(size of the level coarser than
    it) + 1, x + 1, 255 - j)`; LOD 0 takes what is left. So the coarsest level's limit is the
    whole mesh's own `m_MinLod`, every nearer level's limit is decided by the level COARSER than
    it, and the limits fall with the index (the lod index falls with distance).
    """
    print("authored LOD limits: the worked example")
    import numpy
    from csiibundle import lod, lodchain
    from cs2beamng.stages import farring
    from cs2beamng.beamng import level as level_mod

    size = numpy.array([24.0, 12.0, 18.0])  # a 24 x 12 x 18 m house
    levels = [(size, 6000 * 3), (size, 800 * 3)]  # LOD 1 6,000 triangles, LOD 2 800
    limits = lodchain.authored_lod_limits(size, levels, 0.0)
    check(limits == [142, 133, 103], f"the house's limits are [142, 133, 103] (got {limits})")
    check(
        limits[-1] == int(lod.mesh_min_lod(size, 0.0, 0.0)),
        "the coarsest level's limit is the mesh's own m_MinLod - it draws to the game's cull",
    )
    check(all(a > b for a, b in zip(limits, limits[1:])), "limits fall with the index")
    metres = [round(lod.cull_distance(l)) for l in limits]
    check(metres == [181, 512, 16389], f"...which is LOD 0 to 181 m, LOD 1 to 512 m, LOD 2 to 16 km ({metres})")
    check(
        lodchain.authored_lod_limits(size, levels[:1], 0.0) == [142, 103],
        "with one authored level LOD 0 keeps the same limit and the level takes the mesh's",
    )
    check(
        lodchain.authored_lod_limits(size, levels, 0.5) == [145, 136, 106],
        "a bias of 0.5 lifts every limit by three (half an octave = three sixths)",
    )

    # the far ring: two boxes in one 512 m cell, one merged shape, sizes strictly decreasing
    import os, tempfile
    d = tempfile.mkdtemp(prefix="cs2ring_")
    lv = level_mod.Level(os.path.join(d, "demo"), "demo", "Demo")
    lv.set_stage("instances")
    box = numpy.array(
        [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [0, 0, 20], [10, 0, 20], [10, 10, 20], [0, 10, 20]],
        dtype=float,
    )
    tris = [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4]]
    ring_data = {
        "verts": box.tolist(),
        "normals": [[0, 0, 1]] * 8,
        "colors": [[0.5, 0.5, 0.5, 1]] * 8,
        "tris": tris,
        "cullMetres": 9000.0,
    }
    ring = farring.Ring(2500.0, 1.0)
    ring.add(ring_data, [100, 100, 50], [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [1, 1, 1])
    ring.add(ring_data, [300, 200, 50], [[0, -1, 0], [1, 0, 0], [0, 0, 1]], [2, 2, 2])
    report = ring.write(lv, {})
    check(len(report["cells"]) == 1 and report["members"] == 2, "two boxes 200 m apart share one 512 m cell")
    cell = report["cells"][0]
    check(cell["nearPx"] > cell["ringPx"] > cell["nullPx"] >= 1, "the cell's sizes: near > ring > null")
    shape = os.path.join(d, "demo", "art", "shapes", "farring", "farring_0_0.dae")
    text = open(shape, encoding="utf-8").read()
    import re
    start = int(re.search(r"_a(\d+)", text).group(1))
    null = int(re.search(r"nulldetail(\d+)", text).group(1))
    check(start > null >= 1, f"the cell starts at {start} px and stops at {null} px - a band exists")
    check("COLOR" in text, "the merged mesh carries its vertex colours (lod_vertcol)")


def test_torque_detail_selection():
    """The rule every ladder here is written against, stated once: Torque draws the detail with
    the LARGEST size not above the projected pixel size, so a level is drawn for p >= its size
    and a level's own size is where it stops. A null under the coarsest level stops nothing
    nearer than that level's size; a null ABOVE a level's size hides that level."""
    print("Torque's detail selection")
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    sel = audit.select
    check(sel([44, 3], 2, None, 100) == "mesh:0", "p=100: the 44 px level")
    check(sel([44, 3], 2, None, 10) == "mesh:1", "p=10: the 3 px level (drawn down to 3 px)")
    check(sel([44, 3], 2, None, 2.5) == "null", "p=2.5: the null - nothing drawn")
    check(sel([44], 3, None, 10) == "null", "a null ABOVE the coarsest size hides everything under 44 px")
    check(sel([44, 3], 2, 2.5, 2.7) == "billboard", "a billboard between the coarsest level and the null")
    check(sel([200, 20], 5, None, 4) == "none", "under every size: nothing")


def test_budget_detail_at_matches_the_audit():
    """Two implementations of Torque's one rule - `budget.detail_at` (what the budget instrument
    counts) and `lod_audit.select` (what the audit checks) - must agree on every ladder, null
    and billboard, or one of them is not measuring the engine (E5)."""
    print("budget.detail_at agrees with lod_audit.select")
    import importlib.util
    import random

    from cs2beamng.beamng import budget

    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    rng = random.Random(7)
    disagreements = 0
    for _ in range(300):
        sizes = sorted({rng.randint(2, 800) for _ in range(rng.randint(1, 3))}, reverse=True)
        null_px = rng.choice([None, rng.randint(1, sizes[-1] + 5)])
        bb = rng.choice([None, rng.randint(1, sizes[-1] + 5)])
        levels = {s: 100 * (i + 1) for i, s in enumerate(sizes)}
        for p in (0.5, 1, 3, 10, 40, 200, 900):
            picked = audit.select(sizes, null_px, bb, p)
            expect = (
                0
                if picked in ("null", "none")
                else budget.BILLBOARD_TRIANGLES
                if picked == "billboard"
                else levels[sizes[int(picked.split(":")[1])]]
            )
            # `detail_at` takes a radius/distance; feed it a pair that projects to exactly p px
            got = budget.detail_at(levels, p, budget.screen_px(1.0, 1.0), null_px=null_px, billboard_px=bb)
            disagreements += int(got != expect)
    check(disagreements == 0, f"the two selections agree on 2,100 cases ({disagreements} differ)")


def test_billboard_runs_to_the_null():
    """A billboard detail's SIZE is its far stop, like any Torque detail - vanilla's
    pine_radiata_forest.dae ships meshes a800/a350/a150 and `bb_autobillboard25`, the card one
    step over nothing. Written at the takeover size instead (v30-v33), the band [null, bb)
    selected the null: SpruceTree01 (mesh 655, bb 164, null 27) drew NOTHING from 99 m to
    601 m while every level and the billboard individually looked legal (audit R8 pins this)."""
    print("the billboard runs to the null")
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "lod_audit", os.path.join(os.path.dirname(__file__), "beamng", "lod_audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    sel = audit.select
    # the defect shape: billboard at the takeover size - the mid-range selects the null
    check(sel([655], 27, 164, 700) == "mesh:0", "near: LOD 0")
    check(sel([655], 27, 164, 300) == "billboard", "the takeover band still draws the card")
    check(sel([655], 27, 164, 100) == "null", "the defect: [null, bb) draws NOTHING mid-range")
    # the fix: billboard adjacent to the null - the card covers the whole band to the cull
    check(sel([655], 27, 28, 300) == "billboard", "fixed: the card at 300 px")
    check(sel([655], 27, 28, 100) == "billboard", "fixed: the card at 100 px")
    check(sel([655], 27, 28, 27.5) == "null", "the null is still the far stop")


def test_level_dir_guard():
    """The worker refuses to build into the levels ROOT (twice it wrote a level's insides
    beside every other level - 6.5 GB of art/ and forest/ under levels/ itself)."""
    print("the level-dir guard")
    import os, tempfile
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
    check(not refuses({"output": root, "level_name": "new_level"}, ok_dir), "a fresh level dir passes")


def test_coarsest_level_stops_one_pixel_over_the_null():
    """A Torque detail's size is where it STOPS, so the coarsest generated level sits ONE pixel
    over the null - the same rule as the tree billboard (`bb = null + 1`, R8). At 1.5x the null
    (the ladder's old last rung) every generated-ladder shape stopped at D_null / 1.5 while the
    report read D_null off the null, so `lod_detail_scale` 4 behaved as 6 and the draw distance
    the build printed was 1.5x optimistic (validation pass 2026-08-31, A5)."""
    print("the coarsest generated level stops one pixel over the null")
    from cs2beamng.stages import shapelods

    with tempfile.TemporaryDirectory() as tmp:
        mesh = _switch_fixture(tmp)
        options = {"generate_lods": True, "switch_error_px": 6.0}
        for null_px in (3.0, 9.0, 40.0):
            _levels, stats, meshes = shapelods.lod_levels(
                tmp, "src", "shape", mesh, null_px, 8.0, [None, None], "prop", {}, {}, "level", options
            )
            sizes = stats["detailSizes"]
            check(
                len(meshes) == 3 and sizes[-1] == int(round(null_px + 1)),
                f"nullPx {null_px}: the coarsest level is at {sizes[-1]} px (null + 1), ladder {sizes}",
            )


def test_tree_with_one_generated_level_keeps_its_billboard():
    """A tree hands from LOD 0 straight to the engine billboard; the band the card needs is
    between LOD 0's own size (where it stops) and the null. The guard used to read the first
    GENERATED level's size instead - with that level one pixel over the null (A5) it read "no
    band" and 28 trees shipped a bare mid-level and no card (audit R3, Batch 1 rebuild)."""
    print("a tree with one generated level at the floor still gets its billboard")
    from types import SimpleNamespace

    from cs2beamng.beamng import dae
    from cs2beamng.stages import shapewrite

    def job():
        return SimpleNamespace(kind="tree", options={"imposters": True})

    tri = numpy.array([[0, 1, 2]], dtype=numpy.int64)
    verts = numpy.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=numpy.float32)
    lod0 = dae.Mesh("lod0", verts, tri, [("m", 1)])
    lod1 = dae.Mesh("lod1", verts, tri, [("m", 1)])
    null = 10.0
    levels = [(50, [lod0]), (int(null) + 1, [lod1])]  # LOD 0 stops at 50 px; LOD 1 at null + 1
    stats = {}
    out, bb = shapewrite._vegetation_imposter(job(), levels, None, null, stats)
    check(len(out) == 1 and out[0][0] == 50, f"LOD 0 alone remains ({[p for p, _m in out]})")
    check(bb == null + 1, f"and the card runs from LOD 0's stop to the null ({bb} px)")
    check(stats.get("imposter", {}).get("handoverPx") == 50, "the hand-over recorded is LOD 0's stop")
    # a runtime billboard is for vegetation only (C3): a prop or a building never earns one,
    # whatever its reach - the engine bakes one atlas per shape at play time and never frees it
    opts = {"imposters": True, "imposter_min_metres": 0.0, "level_of_detail": 0.7}
    size = numpy.array([30.0, 30.0, 60.0])
    bb, _null = shapewrite._imposter_thresholds(20.0, 4.0, size, 0.0, opts, None, kind="prop")
    check(bb is None, "a prop gets no runtime billboard")
    bb, _null = shapewrite._imposter_thresholds(20.0, 4.0, size, 0.0, opts, None, kind="building")
    check(bb is None, "a building gets no runtime billboard")
    bb, null_out = shapewrite._imposter_thresholds(20.0, 4.0, size, 0.0, opts, None, kind="tree")
    check(bb is not None and null_out < 20.0, f"a tree does ({bb} px, null {null_out:.1f})")
    # no band at all: LOD 0 itself stops at the null - nothing to replace
    out, bb = shapewrite._vegetation_imposter(job(), [(int(null) + 1, [lod0]), (int(null) + 1, [lod1])], None, null, {})
    check(bb is None and len(out) == 2, "a tree whose LOD 0 already stops at the null gets no card")


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
            [[-h, -h, 0], [h, -h, 0], [h, h, 0], [-h, h, 0],
             [-h, -h, 0.05], [h, -h, 0.05], [h, h, 0.05], [-h, h, 0.05]],
            dtype=numpy.float32,
        )
        tris = numpy.array([[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6]], dtype=numpy.int64)
        return dae.Mesh("plane", verts, tris, [("mat", len(tris))])

    options = {"collide": {"prop": True}, "colmesh_max_tris": 2000, "collision_preset": "simplified"}
    big = plane(138.0)
    meshes, status = shapecollider.collider("field", big, big, "prop", options, is_tree=False)
    check(status == "oversizedThin" and meshes == (), f"a 138 m plane 5 cm tall: {status!r}, no colmesh")
    check(status in instanceforest.NO_COLLIDER, "and the instances stage reads that as 'no collision'")
    small = plane(2.0)
    meshes, status = shapecollider.collider("sign", small, small, "prop", options, is_tree=False)
    check(status == "box" and len(meshes) == 1, f"a 2 m plane 5 cm tall keeps its padded box: {status!r}")
    check(
        shapecollider.BOX_COLLIDER_MAX_METRES == 20.0,
        "the guard is the declared 20 m (the constant's first test)",
    )


def test_far_ring_cell_is_split_at_the_vertex_cap():
    """A ring cell merges more geometry into one node than any other product, and a geometry
    past `MAX_GEOMETRY_VERTS` shards in-game (load #10) - so it goes through `split_mesh` like
    every other writer's does (validation pass 2026-08-31, B5)."""
    print("a far-ring cell over the vertex cap is split")
    from cs2beamng.beamng import daemesh
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.stages import farring

    d = tempfile.mkdtemp(prefix="cs2ringcap_")
    lv = level_mod.Level(os.path.join(d, "demo"), "demo", "Demo")
    lv.set_stage("instances")
    box = numpy.array(
        [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [0, 0, 20], [10, 0, 20], [10, 10, 20], [0, 10, 20]],
        dtype=float,
    )
    ring_data = {
        "verts": box.tolist(),
        "normals": [[0, 0, 1]] * 8,
        "colors": [[0.5, 0.5, 0.5, 1]] * 8,
        "tris": [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]],
        "cullMetres": 9000.0,
    }
    ring = farring.Ring(2500.0, 1.0)
    members = daemesh.MAX_GEOMETRY_VERTS // 8 + 50  # just over the cap, all inside one 512 m cell
    for i in range(members):
        ring.add(ring_data, [100 + (i % 40), 100 + i // 40, 50], numpy.eye(3).tolist(), [1, 1, 1])
    report = ring.write(lv, {})
    check(
        report["cellCount"] == 1 and len(report["cells"]) == 1,
        f"one cell, counted under its own key ({report['cellCount']} / {len(report['cells'])})",
    )
    text = open(os.path.join(d, "demo", "art", "shapes", "farring", "farring_0_0.dae"), encoding="utf-8").read()
    import re
    # the writer names the first part `<name>_a<px>` and every further one `<name>_p<i>_a<px>`
    parts = re.findall(r'<node id="farring_0_0_p\d+_a\d+"', text)
    check(len(parts) >= 1, f"the cell's geometry is split into {len(parts) + 1} parts under the vertex cap")


def main():
    start()
    test_rotation_matrix_is_basis_vectors()
    test_switch_points_only_ever_defer()
    test_coarsest_level_stops_one_pixel_over_the_null()
    test_tree_with_one_generated_level_keeps_its_billboard()
    test_thin_oversized_shape_has_no_collider()
    test_far_ring_cell_is_split_at_the_vertex_cap()
    test_a8_performance_rules()
    test_residency_chain_rules()
    test_authored_lod_limits_worked_example()
    test_torque_detail_selection()
    test_billboard_runs_to_the_null()
    test_level_dir_guard()
    test_color_palette()
    test_palette_material()
    test_colormask_export()
    return finish()


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_a3_rules.py`."""
    assert main() in (None, 0)
