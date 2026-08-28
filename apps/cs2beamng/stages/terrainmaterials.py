"""One BeamNG `TerrainMaterial` per CS2 ground.

Each points its three tiers at the ground's own texture, at the game's own three tilings, with
the tier strengths the game's own cross-fade gives. The ground MESH materials — what the bake's
cut meshes and ground tiles bind — are written here too, from the same textures, so a cut face
and the terrain beside it cannot disagree about what the ground looks like.
"""

import os

from csiibundle.bake import terraincut

from .terraintextures import (
    NEUTRAL_MAPS,
    TEXTURE_FALLBACK,
    _height_texture,
    _neutral_map,
    _rgb_texture,
)
from .terraintiling import TIERS, _ground_mesh_maps, _tex_size, _tiling_of

# groundmodel (physics surface) per ground name; BeamNG's names, upper case
GROUNDMODEL = {
    "grass": "GRASS",
    "dirt": "DIRT",
    "rock": "ROCK",
    "extra1": "GRASS",
    "extra2": "GRASS",
    "extra3": "DIRT",
    "extra4": "SAND",
    "roadbed": "DIRT",
}


def terrain_materials(level, bundle, names, gain=1.0, rasters=None, sharpen=1.0, cell=1.0):
    """`{name: TerrainMaterial}` + the texture set; copies the ground textures into
    `art/terrains/`. Every one of the fifteen `*Tex` slots is filled: BeamNG resolves an
    absent slot to `/` and logs `Missing Terrain texture: /` per material, and the terrain
    draws untextured (load #1).

    Returns `(terrain_materials, ground_mesh_materials, notes)` — two dicts, because the two
    classes must NOT share a file (see the comment at the split). `notes` is the counted
    approximation list the stage report prints (audit §47/§48)."""
    surfaces_dir = os.path.join(bundle, "terrain", "surfaces")
    tiling_block = (rasters or {}).get("terrainTiling")
    guessed_tiling, missing_height = [], []
    out = {}
    neutral = _texture_set(out, level)
    _terrain_materials_per_ground(
        out,
        level,
        names,
        surfaces_dir,
        tiling_block,
        cell,
        gain,
        sharpen,
        guessed_tiling,
        missing_height,
        neutral,
    )
    _ground_mesh_materials(out, level, names, surfaces_dir, tiling_block, gain)
    notes = _notes(out, guessed_tiling, missing_height, sharpen, gain)
    ground = {k: v for k, v in out.items() if v.get("class") == "Material"}
    terrain_only = {k: v for k, v in out.items() if v.get("class") != "Material"}
    return terrain_only, ground, notes


def _texture_set(out, level):
    """The `TerrainTextureSet` every material points at."""
    set_name = f"{level.name}_TerrainTextureSet"
    out[set_name] = {
        "name": set_name,
        "class": "TerrainMaterialTextureSet",
        "persistentId": level.pid("terrain:textureset"),
        "baseTexSize": [1024, 1024],
        "detailTexSize": [1024, 1024],
        "macroTexSize": [1024, 1024],
    }
    return {
        (kind, tier): _neutral_map(level, kind, tier=tier)
        for kind in NEUTRAL_MAPS
        for tier in TIERS
    }


def _terrain_materials_per_ground(
    out,
    level,
    names,
    surfaces_dir,
    tiling_block,
    cell,
    gain,
    sharpen,
    guessed_tiling,
    missing_height,
    neutral,
):
    """One `TerrainMaterial` per ground, three tiers each at the game's own tilings."""
    for name in names:
        tiers = _tier_textures(level, surfaces_dir, name, gain, sharpen, missing_height)
        if tiers is None:
            continue
            missing_height.append(name)
        pid = level.pid(f"terrain:material:{name}")
        size, detail_distances, macro_distances, from_game = _tiling_of(tiling_block, name)
        if not from_game:
            guessed_tiling.append(name)
        internal = f"{level.name}_{name}"  # globally unique: see run()
        # The strengths ARE the game's cross-fade, ported exactly rather than guessed. With
        # `_tier_deviation`'s encoding a tier's strength is its lerp weight, so CS2's
        # near -> mid -> far cross-fade is a three-way partition of the two overlays:
        #
        #   detail = 1 - middle                  full near tiling, gone by midBlendEnd
        #   macro  = middle * (1 - far)          rises as the near tier goes, gone by farBlendEnd
        #   base                                 what is left, so the three always sum to 1
        #
        # `calculateStrenth(distanceFactor, 1 - atten, s)` =
        #   lerp(lerp(atten'.x*s.x, s.x, near), lerp(s.y, atten'.y*s.y, far), middle), so
        #   detail: s = [1, 0], atten = [0, 0] -> lerp(1, 0, middle)          = 1 - middle
        #   macro:  s = [0, 1], atten = [0, 1] -> lerp(0, 1 - far, middle)    = middle*(1 - far)
        # and both read the SAME four distances (`blendDetailMacro`: near = d0..d1, middle =
        # d1..d2, far = d2..d3) = [0, midBlendStart, midBlendEnd, farBlendEnd]. That the one
        # vector serves both is the check that the mapping is right.
        #
        # The vanilla `small_island` numbers this replaced (macro 0.6/0.6, detail 0.85/0.3) were
        # an approximation on top of a doubled encoding; together they inflated the albedo's
        # contrast 2.4x and clamped 15.8 % of `extra2` to black. See `_tier_deviation`.
        #
        # DECLARED: BeamNG cuts the detail sample at 250 m and the macro at 1000 m whatever the
        # ramps say (`calculateValue`: `pixelDistance < min(250, ...)` / `min(1000, ...)`), which
        # is short of CS2's midBlendEnd/farBlendEnd. Invisible by construction — both tiers are
        # deviations around mid-gray, so a mip that has converged to the tier's mean contributes
        # exactly zero, and a 14 m repeat is long since mipped flat at 250 m.
        out[f"{internal}-{pid}"] = _material(
            name,
            internal,
            pid,
            size,
            cell,
            detail_distances,
            macro_distances,
            tiers,
            neutral,
        )


def _tier_textures(level, surfaces_dir, name, gain, sharpen, missing_height):
    """`{"albedo": ..., "normal": ..., "height": ...}` per tier, or None with no relief."""
    source = name
    if not os.path.isfile(os.path.join(surfaces_dir, f"{source}.png")):
        source = TEXTURE_FALLBACK.get(name, "grass")
    albedo_src = os.path.join(surfaces_dir, f"{source}.png")
    normal_src = os.path.join(surfaces_dir, f"{source}_normal.png")
    # written as RGB: the theme textures carry a mask in alpha, and BeamNG's terrain
    # texture packer reads PNG alpha as coverage (load #2: the detail tier vanished close
    # up into purple/green). Vanilla terrain textures are colour type 2, no alpha.
    # the BASE tier carries the ground's colour; MACRO and DETAIL are signed deviations around
    # half their deviation from it (`_tier_deviation`) so the overlay reproduces a lerp
    albedo = {
        tier: _rgb_texture(
            level,
            albedo_src,
            f"{level.name}_{source}_{tier}_b.png",
            gain=gain,
            deviation=(tier != "base"),
        )
        for tier in TIERS
    }
    normal = {
        tier: _rgb_texture(level, normal_src, f"{level.name}_{source}_{tier}_nm.png")
        for tier in TIERS
    }
    # the ground's own relief, so BeamNG's height blend has something to interlock with
    height = {
        tier: _height_texture(
            level, normal_src, f"{level.name}_{source}_{tier}_h.png", sharpen=sharpen
        )
        for tier in TIERS
    }
    if height["detail"] is None:
        missing_height.append(name)
    return {"albedo": albedo, "normal": normal, "height": height}


def _material(name, internal, pid, size, cell, detail_distances, macro_distances, tiers, neutral):
    """One `TerrainMaterial`: the three tiers' textures and the game's own cross-fade."""
    albedo, normal, height = tiers["albedo"], tiers["normal"], tiers["height"]
    # DECLARED: BeamNG cuts the detail sample at 250 m and the macro at 1000 m whatever the
    # ramps say (`calculateValue`: `pixelDistance < min(250, ...)` / `min(1000, ...)`), which
    # is short of CS2's midBlendEnd/farBlendEnd. Invisible by construction — both tiers are
    # deviations around mid-gray, so a mip that has converged to the tier's mean contributes
    # exactly zero, and a 14 m repeat is long since mipped flat at 250 m.
    mat = {
        "name": f"{internal}-{pid}",
        "internalName": internal,
        "class": "TerrainMaterial",
        "persistentId": pid,
        "annotation": name.upper(),
        "groundmodelName": GROUNDMODEL.get(name, "DIRT"),
        "detailDistances": detail_distances,
        "detailDistAtten": [0, 0],
        "macroDistances": macro_distances,
        "macroDistAtten": [0, 1],
        "baseColorMacroStrength": [0, 1],
        "baseColorDetailStrength": [1, 0],
        "normalMacroStrength": [0, 1],
        "normalDetailStrength": [1, 0],
        "roughnessMacroStrength": [0, 1],
        "roughnessDetailStrength": [1, 0],
        "heightMacroStrength": [0, 1],
        "heightDetailStrength": [1, 0],
    }
    for tier in TIERS:
        cap = tier.capitalize()
        mat[f"baseColor{cap}Tex"] = (
            albedo[tier] or neutral[("ao", tier)]
        )  # no texture at all: flat white
        mat[f"normal{cap}Tex"] = normal[tier] or neutral[("height", tier)]  # mid gray reads as up
        mat[f"ao{cap}Tex"] = neutral[("ao", tier)]
        # the base tier is the neutral flat 128 by design: height rides the additive
        # overlay, so the relief belongs in macro/detail only (`_height_texture`)
        mat[f"height{cap}Tex"] = (
            neutral[("height", tier)]
            if tier == "base"
            else height[tier] or neutral[("height", tier)]
        )
        mat[f"roughness{cap}Tex"] = neutral[("roughness", tier)]
        for group in ("baseColor", "normal", "ao", "height", "roughness"):
            mat[f"{group}{cap}TexSize"] = _tex_size(size[tier], cell)
    return mat


def _ground_mesh_materials(out, level, names, surfaces_dir, tiling_block, gain):
    # v1.5 Materials for the bake's pre-clipped ground meshes (`terraincut`, bound as
    # ground:<layer> -> <level>_ground_<layer>, one per ground the heightfield paints, plus the
    # three classic names for an older bake): base map at the mesh's own world-planar UV period
    # (csiibundle.bake.terraincut.GROUND_UV_M metres), detail map at the ground's near tiling
    # through `detailScale` — see `_ground_mesh_maps`.
    for gname in sorted({*list(names), "grass", "dirt", "rock"}):
        source = (
            gname
            if os.path.isfile(os.path.join(surfaces_dir, f"{gname}.png"))
            else TEXTURE_FALLBACK.get(gname, "grass")
        )
        metres, _dd, _md, _from_game = _tiling_of(tiling_block, gname)
        albedo, albedo_detail = _ground_mesh_maps(
            level,
            os.path.join(surfaces_dir, f"{source}.png"),
            f"{level.name}_{source}_ground_b",
            gain=gain,
        )
        normal, normal_detail = _ground_mesh_maps(
            level,
            os.path.join(surfaces_dir, f"{source}_normal.png"),
            f"{level.name}_{source}_ground_nm",
            normal=True,
        )
        # how many times the near tiling repeats within one mesh UV period; a UV multiplier, so
        # unlike a baked composite it needs no rounding
        detail_scale = round(terraincut.GROUND_UV_M / max(1e-6, metres["detail"]), 4)
        mname = f"{level.name}_ground_{gname}"
        stage = {"roughnessFactor": 0.95, "metallicFactor": 0.0}
        if albedo:
            stage["baseColorMap"] = albedo
            stage["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]  # blendDetail MULTIPLIES this in
            if albedo_detail:
                stage["detailMap"] = albedo_detail
                stage["detailScale"] = [detail_scale, detail_scale]
                # 1.0 = `base + (near - base)` = the near tiling exactly (`_tier_deviation`)
                stage["detailBaseColorMapStrength"] = 1.0
        else:
            stage["baseColorFactor"] = [0.35, 0.4, 0.25, 1.0]
        if normal:
            stage["normalMap"] = normal
            if normal_detail:
                stage["detailNormalMap"] = normal_detail
                stage["detailNormalMapStrength"] = 1.0
                stage.setdefault("detailScale", [detail_scale, detail_scale])
        # doubleSided: the harness draws its lattice from either side (Blender culls no
        # backfaces) and a sub-cut patch on a portal's cliff can be near-vertical, where "faces
        # up" is a coin toss — both sides must draw
        out[mname] = {
            "name": mname,
            "mapTo": mname,
            "class": "Material",
            "persistentId": level.pid(f"material:ground:{gname}"),
            "version": 1.5,
            "materialTag0": "beamng",
            "materialTag1": "cs2",
            "doubleSided": True,
            "Stages": [stage],
        }


def _notes(out, guessed_tiling, missing_height, sharpen, gain):
    """The counted approximations the stage report prints (audit §47/§48)."""
    # The ground mesh materials go in their OWN file, NOT beside the TerrainMaterials.
    # Measured against every shipped vanilla level: `art/terrains/*.materials.json` holds 191
    # `TerrainMaterial` entries and ZERO plain `Material` entries, and not one vanilla file mixes
    # the two classes. We had our 7 ground mesh materials in there, which is a convention with no
    # precedent in the engine's own content — and the cut meshes rendered as flat untextured grey
    # through loads #19-#21 while three rounds of texture work on them changed nothing visible.
    # `art/cs2/` is out (the materials stage rewrites that file wholesale, after this one runs) and
    # `art/shapes/` is out (the mesh compiler writes placeholder materials files there and the cook
    # stage deletes what it finds), so they get `art/ground/`.
    notes = [
        f"terrain: ground meshes get 2 tiers where the terrain gets 3 (a v1.5 material has "
        f"two texture slots) and their base map runs at the mesh's {terraincut.GROUND_UV_M:.0f} m "
        f"UV period, not the game's mid tiling - APPROXIMATED for "
        f"{sum(1 for v in out.values() if v.get('class') == 'Material')} "
        f"ground mesh material(s); the near tier is exact via detailScale"
    ]
    if guessed_tiling:
        notes.append(
            f"terrain: no rasters.terrainTiling in this rip - {len(guessed_tiling)} ground(s) "
            f"fall back to the pre-audit-47 guesses ({', '.join(sorted(guessed_tiling))})"
        )
    if abs(sharpen - 1.0) > 1e-6:
        notes.append(
            f"terrain: terrain_blend_sharpen x{sharpen:g} - NOT PARITY, a declared deviation: "
            f"the ground relief is {sharpen:g}x what BeamNG's blend derives, to dissolve the "
            f"blocky boundaries our 1.75 m layer grid causes (audit §48.3)"
        )
    if missing_height:
        notes.append(
            f"terrain: {len(missing_height)} ground(s) ship no height map "
            f"({', '.join(sorted(missing_height))}) - flat 128, so BeamNG's height blend "
            f"degenerates to the bilinear ramp for them"
        )
    return notes
