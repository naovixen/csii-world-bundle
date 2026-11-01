"""What the level costs to load, and what it costs to keep in memory.

Two checks. The first is the per-object budgets — calibrated on the vanilla maxima, and over one
of these BeamNG does not fail, it stalls the load or drops frames, which is why they are notes on
the build rather than silence. The second is what the level MOUNTS against what the machine it is
built for can hold; the model behind that is `beamng/machine.py`.
"""

import os
import xml.etree.ElementTree as ET

from ..machine import (
    VANILLA_MAX_RESIDENT_MIB,
    _texture_reduction,
    graphics_api,
    light_manager,
    render_target,
    resident_budget_mib,
    resident_failure_mib,
)


def _budget_notes(by_class, item_data, dae_files, forest_items):
    """`[(name, value, limit_or_None), ...]` — the plan's per-object budgets against this level."""
    return [
        ("forest items", forest_items, None),  # unbounded in practice
        ("forest types", len(item_data), 5000),
        ("TSStatics", len(by_class.get("TSStatic", [])), 25000),
        ("shape files", len(dae_files), 10000),
        # NOT a vanilla maximum, and deliberately not one. Vanilla's biggest level carries 4,053
        # lights, but a light is culled per frame by the engine (frustum, size, occlusion,
        # importance - `clusteredQuality.lua`), not per level, and CS2 does the same with a
        # 2,048/frame cap of its own; what a level pays for one it does not draw is scene-graph
        # cost, measured at 0.19 ms of load each. So the limit is the point past which the load
        # cost and `environment.lua`'s per-transition Lua walk over every registered night light
        # start to be felt, not the point past which it renders wrong. A6.9 raised the placement
        # default to 25,000 (the smallest cap that takes every street light in the reference
        # city); 60,000 is ~11 s of load and where the walk reaches 1/3 of the whole city.
        ("lights", len(by_class.get("PointLight", [])) + len(by_class.get("SpotLight", [])), 60000),
        ("WaterBlocks", len(by_class.get("WaterBlock", [])), 200),
        # NOT a vanilla maximum either. `west_coast_usa` carries 12,752 DecalRoads over a 2,048 m
        # square; ours cover 14,336 m, 49x the area, so per-metre-of-road the comparison is the
        # one that matters and the absolute count is not. What a navgraph road costs is one entry
        # in `map.lua`'s node table and one pass of its merge/T-junction/X-junction resolution at
        # LOAD, all of it O(n log n) on a kd-tree; the limit is where that load pass starts to be
        # felt, not where it renders wrong. 40,000 is ~3x vanilla's biggest.
        ("DecalRoads", len(by_class.get("DecalRoad", [])), 40000),
    ]


def _resident_bytes(level_dir):
    """`{"meshes": bytes, "textures": bytes, "cookedShapes": n, "shapes": n}` — a stat walk.

    What BeamNG mounts, not what is on screen: a shape is loaded whole (every detail level and its
    collision node) and cooked textures sit in VRAM. `.cdae` when the cook has run, because that
    is the file the game reads; the `.dae` beside it is the source and is not loaded. The
    triangles-drawn-at-once half of the budget needs each shape's ladder and lives in
    `tools/level_budget.py`, which is a minute's work on a full city and does not belong in a
    stage that runs every build.
    """
    meshes = textures = 0
    shapes = cooked = 0
    for root, _dirs, files in os.walk(os.path.join(level_dir, "art")):
        names = set(files)
        for name in files:
            path = os.path.join(root, name)
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if ext == ".dae":
                shapes += 1
                if stem + ".cdae" not in names:
                    meshes += size  # uncooked: the XML is what gets parsed and kept
            elif ext == ".cdae":
                cooked += 1
                meshes += size
            elif ext in (".dds", ".png", ".jpg", ".dxt"):
                textures += size
    return {"meshes": meshes, "textures": textures, "shapes": shapes, "cookedShapes": cooked}


def check_budgets(ctx):
    """The per-object counts against the plan's budgets, and the renderer the lights need."""
    # -- budgets -----------------------------------------------------------------------------
    # Calibrated on the vanilla maxima (plan "Budgets"): 663k forest items, 22k TSStatics, 12.7k
    # DecalRoads, 2.6k shapes. Over one of these BeamNG does not fail — it stalls the load or
    # drops frames, which is why they are notes on the build rather than silence.
    # What actually SHIPS as a shape. `keep_dae: false` drops the sources the engine never loads
    # (A8/P7), so counting `.dae` alone would read a level with 8,164 shapes as having none.
    ctx.checks["cookedShapes"] = sum(
        1
        for _r, _d, fs in os.walk(os.path.join(ctx.level_dir, "art"))
        for f in fs
        if f.lower().endswith(".cdae")
    )
    over = _budget_notes(
        ctx.by_class,
        ctx.item_data,
        ctx.dae_files
        if len(ctx.dae_files) >= ctx.checks["cookedShapes"]
        else [(None, None)] * ctx.checks["cookedShapes"],
        ctx.checks["forestItems"],
    )
    ctx.checks["budgets"] = {name: value for name, value, _limit in over}

    _check_light_manager(ctx)
    for name, value, limit in over:
        if limit is not None and value > limit:
            ctx.note(
                f"budget: {value:,} {name} exceeds the plan's ~{limit:,} "
                f"(calibrated on the vanilla levels' own maxima)"
            )
    # A sampled full XML parse (a 10 MB tile takes a second), over whatever sources SURVIVE —
    # `keep_dae: false` drops every one whose `.cdae` is current, and then there is nothing to
    # parse and nothing wrong with that.
    dae_bad = 0
    for _name, path in (
        ctx.dae_files[:: max(1, len(ctx.dae_files) // 8)][:8] if ctx.dae_files else ()
    ):
        try:
            ET.parse(path)
        except ET.ParseError:
            dae_bad += 1
            ctx.note(f"{path}: not well-formed XML")
    ctx.checks["daeParseErrors"] = dae_bad
    ctx.checks["daeFiles"] = len(ctx.dae_files)


def _check_light_manager(ctx):
    """A local light needs the DEFERRED renderer.

    Read the profile the level will actually be loaded on and say so, because nothing about the
    level itself can reveal this (§82).
    """
    placed = len(ctx.by_class.get("PointLight", [])) + len(ctx.by_class.get("SpotLight", []))
    manager, manager_source = light_manager()
    ctx.checks["lightManager"] = {
        "manager": manager,
        "source": manager_source,
        "lights": placed,
    }
    if placed and manager and "basic" in manager.lower():
        ctx.note(
            f"lighting: {placed:,} local lights are placed, and BeamNG is set to "
            f"{manager!r} ({manager_source}) - the BASIC (forward) renderer, which has no "
            f"g-buffer prepass and shades no point or spot light at all. They will cost load "
            f"time and scene memory and light NOTHING. Switch BeamNG to Advanced Lighting, or "
            f"build with `lights false`"
        )


def _ground_bytes(level_dir):
    """The ground's own textures (art/terrains + art/ground), so the estimate's one calibrated
    texture ratio (`size/ground_texture_per_source`) can be scored and learnt like the mesh
    ratios are - it never was, and drifted 32 % from the level (validation pass 2026-08-31)."""
    ground_bytes = 0
    for sub in ("terrains", "ground"):
        folder = os.path.join(level_dir, "art", sub)
        if os.path.isdir(folder):
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    try:
                        ground_bytes += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
    return ground_bytes


def _note_renderer(ctx, api, api_source):
    """Direct3D 11 keeps every texture fully resident, so say so."""
    if api == "DirectX 11":
        ctx.note(
            f"renderer: BeamNG last ran on Direct3D 11 ({api_source}), which does NOT stream "
            f"texture mips - every texture is fully resident there. Vulkan (0.37+) and Direct3D "
            f"12 (0.39+) evict unused mips against the card's budget; 0.39.1 falls back to D3D11 "
            f"on cards with too little VRAM, so check Options > Graphics > Renderer"
        )


def check_residency(ctx):
    """What the level mounts against what this machine is known to hold (audit §64/§65)."""
    # -- what the level MOUNTS (audit §64, recalibrated §65) -----------------------------------------------------
    resident = _resident_bytes(ctx.level_dir)
    reduction, reduction_source = _texture_reduction()
    # the engine drops `reduction` mip levels off every texture, so the bytes it actually holds
    # are 4^-reduction of what the level ships
    texture_mib = resident["textures"] / (1024.0 * 1024.0) / float(4 ** max(0, reduction))
    total_mib = resident["meshes"] / (1024.0 * 1024.0) + texture_mib
    vram_mib, pixels, target_source = render_target(ctx.settings)
    budget_mib = resident_budget_mib(vram_mib, pixels)
    failure_mib = resident_failure_mib(vram_mib, pixels)
    api, api_source = graphics_api()
    ground_bytes = _ground_bytes(ctx.level_dir)
    ctx.checks["resident"] = {
        "graphicsApi": api,
        "graphicsApiSource": api_source,
        "textureStreaming": api in ("DirectX 12", "Vulkan"),
        "meshMiB": round(resident["meshes"] / (1024.0 * 1024.0), 1),
        "textureShippedMiB": round(resident["textures"] / (1024.0 * 1024.0), 1),
        "groundShippedMiB": round(ground_bytes / (1024.0 * 1024.0), 1),
        "textureMiB": round(texture_mib, 1),
        "textureReduction": reduction,
        "textureReductionSource": reduction_source,
        "totalMiB": round(total_mib, 1),
        "budgetMiB": round(budget_mib),
        "failureMiB": round(failure_mib),
        "budgetVramMiB": vram_mib,
        "budgetPixels": pixels,
        "budgetSource": target_source,
        "vanillaMaxMiB": VANILLA_MAX_RESIDENT_MIB,
        "vanillaRatio": round(total_mib / VANILLA_MAX_RESIDENT_MIB, 2),
        "cookedShapes": resident["cookedShapes"],
        "shapes": resident["shapes"],
    }
    _note_renderer(ctx, api, api_source)
    if total_mib > budget_mib:
        where = (
            "AT OR OVER the measured failure point"
            if total_mib >= failure_mib
            else "in the unmeasured band between what is known to fit and what is known to fail"
        )
        ctx.note(
            f"budget: the level mounts {total_mib:,.0f} MiB "
            f"({resident['meshes'] / (1024.0 * 1024.0):,.0f} meshes + "
            f"{texture_mib:,.0f} textures, {reduction_source}) on a "
            f"{vram_mib:,} MiB card at {pixels / 1.0e6:.2f} Mpixel ({target_source}) - "
            f"{where}. Known to fit: {budget_mib:,.0f} MiB. Known to fail: "
            f"{failure_mib:,.0f} MiB, where D3D12 failed 131,073 texture creates in one "
            f"session. This level is {total_mib / VANILLA_MAX_RESIDENT_MIB:.1f}x the "
            f"{VANILLA_MAX_RESIDENT_MIB:,} MiB of west_coast_usa, the biggest level BeamNG "
            f"ships - see tools/level_budget.py for where the bytes are"
        )
