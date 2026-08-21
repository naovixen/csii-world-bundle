"""Render an already-loaded .blend from an oblique (or top-down ortho) camera.

    blender probe.blend --background --python tools/oblique_render.py -- <ABS_OUT.png> cx cy cz dist elev_deg az_deg [ortho] [res]

The camera sits at `target + dist * (cos e * sin az, -cos e * cos az, sin e)` (az 0 = looking
north from the south, clockwise) and looks at `(cx, cy, cz)`. `ortho` makes it a top-down
orthographic camera of width `dist`. Output path must be ABSOLUTE (a relative one silently
writes nothing under --background). EEVEE, Standard view transform, one sun.
"""

import math
import sys

import bpy
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1 :]
out = args[0]
cx, cy, cz, dist, elev, az = (float(v) for v in args[1:7])
ortho = len(args) > 7 and args[7].lower().startswith("ortho")
res = int(args[8]) if len(args) > 8 else 1600

cam_data = bpy.data.cameras.new("probe_cam")
cam_data.clip_end = 20000.0
cam = bpy.data.objects.new("probe_cam", cam_data)
bpy.context.scene.collection.objects.link(cam)
target = Vector((cx, cy, cz))
if ortho:
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = dist
    cam.location = target + Vector((0.0, 0.0, 800.0))
    cam.rotation_euler = (0.0, 0.0, 0.0)
else:
    e, a = math.radians(elev), math.radians(az)
    cam.location = target + dist * Vector(
        (math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e))
    )
    direction = target - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = 24.0

sun_data = bpy.data.lights.new("probe_sun", type="SUN")
sun_data.energy = 4.0
sun = bpy.data.objects.new("probe_sun", sun_data)
bpy.context.scene.collection.objects.link(sun)
sun.rotation_euler = (0.6, 0.4, 0.8)
scene = bpy.context.scene
scene.camera = cam
engines = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
scene.render.resolution_x = res
scene.render.resolution_y = int(res * (9 / 16)) if not ortho else res
scene.render.filepath = out
scene.view_settings.view_transform = "Standard"
world = scene.world or bpy.data.worlds.new("probe_world")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.55, 0.7, 0.9, 1.0)
    bg.inputs[1].default_value = 1.0
bpy.ops.render.render(write_still=True)
print("oblique_render wrote", out)
