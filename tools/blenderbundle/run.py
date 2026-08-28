"""The import itself: parse the options, resolve the region, run every layer, report.

The phase order is load-bearing in one place. Terrain comes BEFORE lanes (since 2026-08-11): it
depends on neither networks nor lanes, and putting it first is what lets a lane marking that
finds no road under it fall back to the ground rather than keeping an authored height with
nothing beneath it (§8b drape). Areas still follow both, as they always have.
"""

import os
import sys
import time

import bpy
from csiibundle import reader

from . import (
    areabuild,
    colourgrade,
    glbimport,
    instancebuild,
    lanebuild,
    lighting,
    materiallayer,
    netbuild,
    raysurface,
    runreport,
    sceneprep,
    survey,
    terrainbuild,
)
from . import (
    options as options_mod,
)
from .log import LOG


def _new_collection(name):
    """One new collection, linked under the scene's master collection."""
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


class _Scene:
    """The four collections an import fills, and the parking that keeps placement cheap.

    Sources live in their own collection, excluded from the view layer, so the templates do not
    render on top of the instances placed from them. Both the instances and the decals are
    parked out of the view layer for the placement pass, for the reason given on
    `sceneprep.parked_out_of_view_layer`: every prefab import deselects the whole view layer,
    and the instances placed so far are in it.
    """

    def __init__(self):
        self.sources = _new_collection("bundle_sources")
        self.instances = _new_collection("bundle_instances")
        self.decals = _new_collection("bundle_decals")
        self.networks = None
        self.lanes = None
        self._parks = [
            sceneprep.parked_out_of_view_layer(self.decals),
            sceneprep.parked_out_of_view_layer(self.instances),
            sceneprep.parked_out_of_view_layer(self.sources),
        ]

    def park(self):
        """Take the placement collections out of the view layer."""
        for park in self._parks:
            park.__enter__()

    def unpark(self):
        """Put them back. Deliberately after the networks: an instance parked here is an
        instance the segment imports do not have to deselect, and there are two orders of
        magnitude more of them than there are segments. Un-parking before the network pass put
        40,500 empties back in front of every segment import and took each one from 0.06 s to
        1.5 s."""
        for park in self._parks:
            park.__exit__()


def _resolve_centre(opts, instances_path, networks_path, where):
    """Turn `--center auto/downtown/interchange` into coordinates, or return an exit code.

    Downtown is decided by buildings whatever is being placed. Centring on the densest cell of
    a selection that includes trees finds a forest, and of one that includes props finds a car
    park; and with `--kinds none` there is nothing to survey at all, though a networks-only run
    still wants to look at the middle of the city.
    """
    if opts.center == "auto":
        if where is None:
            LOG.warn("--center auto needs instances to survey; drop it or add --kinds")
            return 2
        opts.center = where["densest"]
        LOG.step(
            f"           --center auto resolved to X {opts.center[0]:.0f} Z {opts.center[1]:.0f}"
        )
    elif opts.center == "downtown":
        skyline = survey.locate(instances_path, {"building"}, weight=survey.skyline_weight)
        if skyline is None:
            LOG.warn(
                "--center downtown needs buildings in the bundle; "
                "use --center auto, or give explicit X Z"
            )
            return 2
        opts.center = skyline["densest"]
        LOG.step(
            f"           --center downtown resolved to X {opts.center[0]:.0f} "
            f"Z {opts.center[1]:.0f}   (tallest 250 m cell of {skyline['count']} building(s))"
        )
    elif opts.center == "interchange":
        found = survey.interchange(instances_path, networks_path)
        if found is None:
            LOG.warn(
                "--center interchange needs highway or elevated network records with buildings "
                "near them; use --center downtown, or give explicit X Z"
            )
            return 2
        opts.center = found
        LOG.step(
            f"           --center interchange resolved to X {opts.center[0]:.0f} "
            f"Z {opts.center[1]:.0f}   (tallest skyline with a highway junction in it)"
        )
    if opts.radius is not None:
        if opts.center is None:
            LOG.warn("--radius needs --center (or --center auto)")
            return 2
        LOG.step(
            f"region:    within {opts.radius} m of X {opts.center[0]:.0f} Z {opts.center[1]:.0f}"
        )
    return 0


def _build_networks(opts, layers, scene):
    """The §5 network layer, in its own collection and parked while it imports."""
    if not opts.networks:
        return 0
    LOG.phase("Networks")
    scene.networks = _new_collection("bundle_networks")
    with sceneprep.parked_out_of_view_layer(scene.networks):
        count = netbuild.build_networks(
            layers.networks,
            opts.net_categories,
            scene.networks,
            layers.net_meshes,
            center=opts.center,
            radius=opts.radius,
            limit=opts.net_limit,
        )
    LOG.end_phase()  # build_networks() may have early-returned before its own done()
    return count


def _build_terrain(opts):
    """The §8 terrain, and the ground surface every later layer drapes onto."""
    if not opts.terrain:
        return 0, None
    LOG.phase("Terrain & water")
    collection = _new_collection("bundle_terrain")
    placed, ground = terrainbuild.build_terrain(
        opts.bundle_dir,
        collection,
        opts.center,
        opts.radius,
        clip_floor=opts.clip_floor,
        coarse=opts.coarse_terrain,
    )
    LOG.end_phase()  # build_terrain() may have early-returned before its own done()
    return placed, ground


def _build_lanes(opts, layers, scene, network_count, terrain_ground):
    """Contract §5B v2: lanemeshes.jsonl carries bend parameters, laneprefabs.json the part
    catalogue, lanemeshes/ the straight part meshes stored once. The bend runs in `lanebuild` —
    this importer is the contract's reference implementation of it. `--lane-markings` bends the
    decal parts only; a full `--lanes` wins when both are given."""
    if not (opts.lanes or opts.lane_markings):
        return 0
    if not os.path.isfile(layers.lane_geometry):
        LOG.warn(f"no lanemeshes.jsonl at {layers.lane_geometry} — skipping lane geometry")
        return 0
    scene.lanes = _new_collection("bundle_lanemeshes")
    LOG.phase(
        "Lane geometry (lanemeshes.jsonl)"
        if opts.lanes
        else "Lane markings (lanemeshes.jsonl, decal parts)"
    )
    road_surface = raysurface.ProjectionSurface(scene.networks.objects) if network_count else None
    ground_surface = (
        raysurface.ProjectionSurface([terrain_ground[0]]) if terrain_ground is not None else None
    )
    with sceneprep.parked_out_of_view_layer(scene.lanes):
        count = lanebuild.build_lanes(
            layers.lane_geometry,
            layers.lane_prefabs,
            layers.lane_meshes,
            scene.lanes,
            center=opts.center,
            radius=opts.radius,
            limit=opts.net_limit,
            road_surface=road_surface,
            markings_only=not opts.lanes,
            ground_surface=ground_surface,
        )
    LOG.end_phase()  # build_lanes() may have early-returned before its own done()
    return count


def _build_areas(opts, terrain_ground):
    """The §8b surface areas, painted onto the terrain built above."""
    if not opts.areas:
        return 0
    LOG.phase("Surface areas")
    collection = _new_collection("bundle_areas")
    ground_surface, ground_cell = (None, 3.5)
    if terrain_ground is not None:
        ground_surface = raysurface.ProjectionSurface([terrain_ground[0]])
        ground_cell = terrain_ground[1]
    count = areabuild.build_areas(
        opts.bundle_dir,
        collection,
        opts.center,
        opts.radius,
        ground=ground_surface,
        cell=ground_cell,
    )
    LOG.end_phase()  # build_areas() may have early-returned before its own done()
    return count


def _apply_material_layer(bundle_dir):
    """Every .glb is in, so every material exists: apply §7 and the §7.2 paint rule."""
    masked = materiallayer.apply_pbr_masks(bundle_dir)
    shader_water, shader_glass, shader_tint = materiallayer.apply_shader_only_materials(bundle_dir)
    if masked:
        LOG.step(
            f"pbr:       {masked} material(s) took metallic + roughness from their §7 mask "
            "(the .glb ships neither — it hard-codes roughness 0.9)"
        )
    if shader_water or shader_glass or shader_tint:
        LOG.step(
            f"shaders:   {shader_water} water, {shader_glass} glass, {shader_tint} tinted "
            "material(s) given an appearance — CS2 draws these with a shader and no "
            "texture, so the .glb leaves them white"
        )
    tinted = materiallayer.apply_color_tints(bundle_dir)
    if tinted:
        LOG.step(
            f"tinted:    {tinted} material(s) wired to per-instance colorSet via their colorMask"
        )


def _tidy(scene, placer, network_count, lane_count):
    """Drop the scratch collection, hide the templates, and aim the viewports.

    The scratch collection is empty by construction — every import unlinks what it produced —
    so drop it rather than leave an empty entry in the outliner. Aiming falls back through the
    layers because with `--kinds none --networks` the instance collection is empty, and aiming
    at an empty collection leaves the viewport at the origin, which is where a city almost never
    is.
    """
    scratch = bpy.data.collections.get(glbimport.SCRATCH_NAME)
    if scratch is not None:
        if scratch.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(scratch)
        bpy.data.collections.remove(scratch)

    layer_child = bpy.context.view_layer.layer_collection.children.get(scene.sources.name)
    if layer_child is not None:
        layer_child.exclude = True

    sceneprep.show_materials()
    sceneprep.aim_viewports(
        scene.instances
        if placer.counts["placed"]
        else (scene.networks if network_count else (scene.lanes if lane_count else scene.instances))
    )


def _save(save_path):
    """Write the .blend. `relative_remap=False` because the textures stay where the rip put
    them, under ModsData on the system drive, and a .blend saved anywhere else cannot express
    that as a relative path. Blender's default is to try anyway and warn once per image —
    "cannot be made relative" for every one of a few thousand textures, which buries the
    import's own output. The paths are absolute either way; only the noise differs."""
    bpy.ops.wm.save_as_mainfile(filepath=save_path, relative_remap=False)
    LOG.step(f"saved {save_path}")


def _prepare_scene(opts):
    """Wipe, pin the view transform, light, grade — in that order, and before anything is placed.

    The view transform is pinned unconditionally, including under `--keep`, and BEFORE
    `--colour` gets its say: `--keep` is exactly the path where a graded scene from a previous
    run would otherwise survive and make the next import look like it changed something it did
    not.
    """
    if not opts.keep:
        sceneprep.clear_scene()
    sceneprep.pin_view_transform()
    lighting.setup_lighting(opts.bundle_dir, sky=opts.sky)
    if opts.colour:
        colourgrade.apply_grading(opts.bundle_dir)


def main(usage):
    """Run one import. Returns the process exit code; `usage` is the entry script's docstring."""
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    try:
        opts = options_mod.parse(argv)
    except options_mod.Unknown as unknown:
        LOG.warn(f"unknown option {str(unknown)!r}")
        return 2
    if not os.path.isdir(opts.bundle_dir):
        print(usage)
        print(f"\nNo bundle directory given and the default does not exist:\n  {opts.bundle_dir}")
        return 1
    if not (argv and not argv[0].startswith("--")):
        print(f"bundle: {opts.bundle_dir} (default; pass a directory after -- to override)")

    layers = reader.resolve_layers(opts.bundle_dir)
    if opts.kinds and not os.path.isfile(layers.instances):
        LOG.warn(f"no instances.jsonl at {layers.instances}")
        return 2
    declared_gaps = reader.read_declared_gaps(opts.bundle_dir)
    runreport.announce(opts, layers)
    return _import(opts, layers, declared_gaps)


def _import(opts, layers, declared_gaps):
    """Everything after the options are settled: survey, prepare, place, build, report."""
    LOG.phase("Instances")
    glbimport.counts_reset()
    # Always survey before placing. Knowing where the selection is turns "nothing appeared" from
    # a mystery into an arithmetic error the user can see.
    where = survey.locate(layers.instances, opts.kinds) if opts.kinds else None
    if opts.kinds and where is None:
        LOG.warn(f"no instances of kinds {sorted(opts.kinds)} in this bundle")
        return 2
    runreport.selection(where)
    code = _resolve_centre(opts, layers.instances, layers.networks, where)
    if code:
        return code
    LOG.step(f"declared gaps in warnings.jsonl: {len(declared_gaps)}")
    materiallayer.report_materials(opts.bundle_dir)
    _prepare_scene(opts)

    scene = _Scene()
    scene.park()
    started = time.time()
    placer = instancebuild.place_instances(
        opts,
        layers.instances,
        layers.meshes,
        (scene.sources, scene.instances, scene.decals),
        declared_gaps,
    )

    network_count = _build_networks(opts, layers, scene)
    terrain_objects, terrain_ground = _build_terrain(opts)
    lane_count = _build_lanes(opts, layers, scene, network_count, terrain_ground)
    area_objects = _build_areas(opts, terrain_ground)

    scene.unpark()
    _apply_material_layer(opts.bundle_dir)
    _tidy(scene, placer, network_count, lane_count)
    runreport.placed(opts, placer, where, (terrain_objects, area_objects, time.time() - started))
    if opts.save_path:
        _save(opts.save_path)
    return 0
