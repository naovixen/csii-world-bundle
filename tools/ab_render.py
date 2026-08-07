"""Top-down ortho A/B render of an already-loaded .blend. Argv: out.png cx cy cam_z."""
import sys
import bpy
from mathutils import Vector

out, cx, cy, cz = sys.argv[sys.argv.index("--") + 1:][:4]
cx, cy, cz = float(cx), float(cy), float(cz)

cam_data = bpy.data.cameras.new("ab_topdown")
cam_data.type = "ORTHO"
cam_data.ortho_scale = 400.0
cam_data.clip_end = 2000.0
cam = bpy.data.objects.new("ab_topdown", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = Vector((cx, cy, cz + 400.0))
cam.rotation_euler = (0.0, 0.0, 0.0)

sun_data = bpy.data.lights.new("ab_sun", type="SUN")
sun_data.energy = 4.0
sun = bpy.data.objects.new("ab_sun", sun_data)
bpy.context.scene.collection.objects.link(sun)
sun.rotation_euler = (0.5, 0.3, 0.0)

scene = bpy.context.scene
scene.camera = cam
# Blender renamed the EEVEE-Next engine id back to "BLENDER_EEVEE" once legacy
# EEVEE was removed (Blender 5.x); fall back for older builds that still use
# the "_NEXT" suffix.
_available_engines = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in _available_engines else "BLENDER_EEVEE"
scene.render.resolution_x = scene.render.resolution_y = 1600
scene.render.filepath = out
scene.view_settings.view_transform = "Standard"
bpy.ops.render.render(write_still=True)
print("ab_render wrote", out)
