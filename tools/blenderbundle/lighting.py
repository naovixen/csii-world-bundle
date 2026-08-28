"""The scene light rig, and the physical sky `--sky` swaps in for its uniform ambient.

`sceneprep.clear_scene()` deletes Blender's default light along with everything else, so without
this an imported scene has no lights and no world at all — it only looks lit because the viewport
falls back to its own studio HDRI, whose warm tint then colours everything.
"""

import math

import bpy
import colour_model
from csiibundle import frames
from mathutils import Euler, Vector

from . import shadernodes
from .log import LOG

# The scene light rig. `sceneprep.clear_scene()` removes Blender's default light along with everything
# else, so an imported scene would have no lights and no world at all; these put a plain
# neutral sun and ambient back. White, and deliberately uncalibrated -- the bundle ships
# albedo, and the importer reproduces it 1:1 rather than grading it toward the game's look.
#
# `--colour` never touches this rig or any material: the whole grading chain rides Blender's
# display pipeline instead, for the reasons `_apply_grading` sets out.
#
# Two constants that used to live here have been DELETED rather than tuned, because the model
# they were compensating for was wrong:
#
#   _GROUND_BRIGHTNESS = 3.09 lifted the terrain alone, on the argument that CS2 authors ground
#   albedo 1.63 stops darker than its object textures. The relative measurement is real, but the
#   game renders that dark ground too — the shipped sand albedo reaches the in-game eyedropper
#   colour under the game's own grading at ordinary scene exposure, with no ground-only boost. A
#   gain on the terrain alone therefore does not fix an imbalance, it creates one, and it is the
#   reason everything that was NOT boosted read as too dark once exposure was pulled back.
#
#   _VIEW_GAMMA = 1.3 was a hand-set tone-curve compensation "because Blender has no ACES view
#   transform". Blender 5.2 ships ACES 1.3 and ACES 2.0 as view transforms; the premise was
#   false. The game's contrast is now applied from its own number instead (grading.json's
#   contrast +20, solved through the ACEScc pivot into an exact power law).

# The ambient share of the rig: how much of a fully-lit surface's light comes from the sky rather
# than the sun. A uniform environment of radiance W gives a Lambertian surface outgoing radiance
# albedo*W, so this is directly "a quarter of the light is skylight".
_WORLD_LEVEL = 0.25

# The sun's strength is DERIVED, not chosen, from one requirement: flat, unshadowed, fully-lit
# ground should render at exactly its albedo. For a Lambertian surface under a sun of irradiance
# E at incidence cos(theta) plus a uniform sky of radiance W,
#     outgoing radiance = albedo * (E*cos(theta)/pi + W)
# so radiance == albedo when  E = pi * (1 - W) / cos(theta).
#
# Why bother: it makes "scene-linear" and "albedo" the same number on the ground, which is the
# assumption `tools/colour_model.py` works under. With an arbitrary sun the model's predictions
# and the render can only be compared after re-exposing one to the other, and under `--colour`
# the picture came out near-black — the game's own EV figures are relative to ITS light level,
# not to a rig picked by eye. `_sun_energy_for` computes it from the sun's actual orientation.
_SUN_TILT = (0.5, 0.3, 0.0)


def _sun_from_bundle(bundle_dir):
    """The game's own sun direction, as a Blender-frame vector pointing AT the light.

    `grading.json`'s `lighting.sun.direction` is the vector the light TRAVELS along, in the
    bundle's frame (left-handed, X east, Y up, Z north — §2.1). Blender is right-handed with Z
    up, and the importer maps bundle (x, y, z) to Blender (x, z, y) everywhere else, so the same
    mapping applies here; negating it turns "where the light goes" into "where the light is".

    Worth using rather than the arbitrary default: the placeholder tilt sits at 33 degrees, and
    this city was ripped at noon with the sun at 66. Half the terrain that reads as shadow under
    the placeholder is lit in the game, which is most of "the import looks too dark".

    Returns None when the bundle predates the lighting block, and the placeholder stands.
    """
    if not bundle_dir:
        return None
    grading = colour_model.load(bundle_dir)
    if grading is None:
        return None
    toward = grading.lighting.sun.toward  # None below the horizon: a night rip lights nothing
    if toward is None:
        return None
    return Vector(frames.bundle_to_zup_point(toward)).normalized()


# `--sky` is off by default ON PURPOSE (see `setup_lighting`'s `sky` argument): the default rig
# is a uniform grey world plus a sun whose energy is DERIVED so that flat, fully-lit ground
# renders at exactly its albedo, which is what makes "scene-linear" and "albedo" the same number
# and is the assumption `tools/colour_model.py` works under. A sky replaces that uniform ambient
# with a directional one and breaks the identity — worth it to see reflections, wrong as a
# default for measurement.

# Mean radiance of Blender's atmosphere model over the UPPER hemisphere, measured with a 128x64
# equirectangular Cycles render at the reference elevation below (`--sky` normalises against it;
# see `_build_sky`). Re-measure with the same render if the sky model changes.
_SKY_REFERENCE_MEAN = 14.28
_SKY_REFERENCE_ELEVATION = 66.263


def _build_sky(world, background, bundle_dir):
    """Replace the flat grey world with a physical sky at the bundle's OWN sun position.

    **Why this exists: a mirror needs something to be a mirror OF.** Water at roughness 0.05
    reflects its environment faithfully, and a uniform grey environment reflects as uniform grey
    — which reads as "the ocean has no reflection". The fix is not to change the water.

    What is exact and what is not, in the manner of `csiibundle.colour`:

    * the sun's ELEVATION and AZIMUTH are the game's own, from `grading.json`'s
      `lighting.sun` — so the sky's gradient, its horizon glow and the specular glint on water
      all sit where the game put them.
    * the sky MODEL is Blender's, not CS2's. The game uses HDRP's PhysicallyBasedSky and ships
      only its three tints and an exposure, not a sky texture, so an atmosphere model driven by
      the right sun is the closest honest reconstruction. `groundTint` is applied as the
      below-horizon colour, which is the one tint that is not white on the reference bundle.
    * `sky.exposure` is applied as a strength multiplier on the result.
    """
    grading = colour_model.load(bundle_dir) if bundle_dir else None
    lighting = grading.lighting if grading is not None else None

    tree = world.node_tree
    sky = tree.nodes.new("ShaderNodeTexSky")
    available = sky.bl_rna.properties["sky_type"].enum_items.keys()
    for preferred in ("MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "HOSEK_WILKIE", "PREETHAM"):
        if preferred in available:
            sky.sky_type = preferred
            break

    if lighting is not None and lighting.sun.elevation_degrees is not None:
        sky.sun_elevation = math.radians(float(lighting.sun.elevation_degrees))
        # The bundle's azimuth is degrees clockwise from -Z toward the light; Blender's
        # `sun_rotation` is counter-clockwise from +Y, and bundle -Z maps to Blender -Y.
        sky.sun_rotation = math.radians(180.0 - float(lighting.sun.azimuth_degrees or 0.0))
    if hasattr(sky, "sun_intensity"):
        # The disc itself is the sun LAMP's job; leaving it in double-counts the key light.
        sky.sun_intensity = 0.0

    tree.links.new(sky.outputs["Color"], background.inputs["Color"])
    _normalise_sky(sky, background, lighting)
    return sky


def _normalise_sky(sky, background, lighting):
    """Scale the sky to deliver the SAME mean ambient the grey world did.

    NORMALISE, or the sky bleaches the scene. Blender's atmosphere model emits physical
    radiance: measured with a 128x64 equirectangular render at this bundle's own sun
    elevation, its mean over the UPPER hemisphere is 14.28, against the flat rig's
    _WORLD_LEVEL of 0.25 — 57x. Left alone it blows out every surface and the whole city
    renders pale blue, which is what the first attempt at this did.

    Scaling to the same mean keeps the sun-energy derivation intact — flat lit ground still
    renders at its albedo — and changes only the DIRECTIONALITY of the ambient, which is the
    whole point: a mirror needs something to be a mirror of.

    The elevation term is MODELLED, not measured: sky radiance falls roughly with the sine of
    the sun's height, so the reference measurement is scaled by sin(elev)/sin(reference). At
    this bundle's own elevation it is exact by construction; at another time of day it is
    approximate, which is acceptable for an ambient rig whose job is to be reflected.
    """
    elevation = getattr(sky, "sun_elevation", math.radians(_SKY_REFERENCE_ELEVATION))
    reference = math.sin(math.radians(_SKY_REFERENCE_ELEVATION))
    scale = max(math.sin(elevation), 0.05) / reference
    strength = _WORLD_LEVEL / max(_SKY_REFERENCE_MEAN * scale, 1e-6)
    if lighting is not None:
        strength *= 2.0 ** float(lighting.sky.exposure)
    background.inputs["Strength"].default_value = strength
    LOG.step(
        f"sky:       physical sky at the bundle's own sun ({math.degrees(elevation):.1f}° "
        f"elevation), normalised to the rig's {_WORLD_LEVEL:g} ambient (strength "
        f"{strength:.4f}) so reflections gain a horizon without changing exposure"
    )


def setup_lighting(bundle_dir=None, sky=False):
    """Give the scene a plain neutral sun and ambient.

    `sceneprep.clear_scene()` deletes Blender's default light along with everything else, so without this
    an imported scene has no lights and no world at all — it only looks lit because the viewport
    falls back to its own studio HDRI, whose warm tint then colours everything. A white sun and a
    neutral grey world make what you see the albedo the bundle actually ships, not a studio
    lighting look. The rig is not calibrated against the game and is not meant to be: matching
    CS2's on-screen colour needs its HDR exposure and colour grading, neither of which is in the
    bundle. Set up your own lighting if you want the game's look.
    """
    scene = bpy.context.scene
    if not any(ob.type == "LIGHT" for ob in scene.objects):
        _place_sun(scene, bundle_dir)

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("carto_world")
        scene.world = world
    shadernodes.ensure_nodes(world)
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (_WORLD_LEVEL, _WORLD_LEVEL, _WORLD_LEVEL, 1.0)
        background.inputs["Strength"].default_value = 1.0

    if sky and background is not None:
        _build_sky(world, background, bundle_dir)

    # EEVEE ships with raytracing off, so nothing reflects — water included. Turn it on; a
    # ripped city with flat, mirror-less water reads as plastic.
    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "use_raytracing"):
        eevee.use_raytracing = True

    _configure_viewports()


def _place_sun(scene, bundle_dir):
    """The one sun of the rig, aimed from the bundle where it says so and at the placeholder
    tilt otherwise, with its energy derived from that aim (see `_SUN_TILT` above).

    A Blender sun shines along its local -Z, so the direction TOWARD it is local +Z, and its
    world Z component is the incidence cosine on level ground.
    """
    sun_data = bpy.data.lights.new("carto_sun", type="SUN")
    sun_data.color = (1.0, 1.0, 1.0)
    sun = bpy.data.objects.new("carto_sun", sun_data)
    scene.collection.objects.link(sun)
    toward = _sun_from_bundle(bundle_dir)
    if toward is not None:
        sun.rotation_euler = toward.to_track_quat("Z", "Y").to_euler()
        cos_theta = max(1e-3, toward.z)
        LOG.step(
            f"sun placed from the bundle: {math.degrees(math.asin(cos_theta)):.1f} deg "
            f"above the horizon, as the game had it at rip time"
        )
    else:
        sun.rotation_euler = _SUN_TILT
        cos_theta = max(1e-3, Euler(_SUN_TILT, "XYZ").to_matrix().col[2].z)
    sun_data.energy = math.pi * (1.0 - _WORLD_LEVEL) / cos_theta


def _configure_viewports():
    """Make the viewport obey the scene rig instead of its warm studio HDRI, and REPORT what it
    found. A viewport left in Solid shading draws a uniform grey clay regardless of any
    material, which is indistinguishable from "the import produced no colour" — so say so out
    loud rather than leaving it to be guessed at."""
    seen = []
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                shading = space.shading
                if shading.type in ("SOLID", "WIREFRAME"):
                    shading.type = "MATERIAL"
                if shading.type == "MATERIAL":
                    shading.use_scene_lights = True
                    shading.use_scene_world = True
                seen.append(shading.type)

    if seen:
        LOG.step(f"viewport shading set to {', '.join(sorted(set(seen)))} ({len(seen)} 3D view(s))")
    else:
        LOG.warn(
            "no 3D viewport found to configure (headless run, or the .blend was opened "
            "afterwards) — if the scene looks flat grey, set the viewport to Material "
            "Preview or Rendered: Solid shading ignores materials entirely"
        )
