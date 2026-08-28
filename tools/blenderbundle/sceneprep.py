"""Scene-level Blender state the import sets up around the layers themselves.

Wiping, view transforms, viewport shading and aim, and the collection parking that keeps the
glTF importer's per-file deselect from becoming quadratic across a city.
"""

import bpy
from mathutils import Vector

from .log import LOG


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def find_layer_collection(collection, root=None):
    """The LayerCollection for `collection` in the active view layer, or None."""
    root = root or bpy.context.view_layer.layer_collection
    if root.collection is collection:
        return root
    for child in root.children:
        found = find_layer_collection(collection, child)
        if found is not None:
            return found
    return None


class parked_out_of_view_layer:  # noqa: N801 - used as a context
    """Keep a collection out of the view layer for the duration of a bulk import.

    The glTF importer ends every file with `bpy.ops.object.select_all(action='DESELECT')`, which
    walks *every object in the view layer* — so once a few thousand segments have landed, each
    further import pays for all of them. Profiled over 500 network meshes it was 70 s of 197 s,
    and it grows with the scene. Objects in an excluded collection are not in the view layer, so
    parking the destination there makes that deselect walk only the handful the current file just
    produced. Everything is linked back at the end and nothing is lost — exclusion hides a
    collection from the view layer, it does not unlink it.
    """

    def __init__(self, collection):
        self.collection = collection
        self.layer = None

    def __enter__(self):
        self.layer = find_layer_collection(self.collection)
        if self.layer is not None:
            self.was_excluded = self.layer.exclude
            self.layer.exclude = True
        return self

    def __exit__(self, *exc):
        if self.layer is not None:
            self.layer.exclude = self.was_excluded
        return False


def show_materials():
    """Put every 3D viewport into material preview, so 'is it textured?' is a question you can
    answer by looking. Solid shading is the default and would hide the entire Phase 4 result."""
    for area in getattr(bpy.context.screen, "areas", []):
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.shading.type = "MATERIAL"


def pin_view_transform():
    """Force the Standard view transform.

    Blender defaults to AgX, a filmic transform that rolls colour off toward a desaturated warm
    neutral. These are ripped albedo textures, not scene-referred HDR, so Standard — which
    displays the values as they are — is the honest choice for a 1:1 import.
    """
    scene = bpy.context.scene
    try:
        scene.view_settings.view_transform = "Standard"
        if hasattr(scene.view_settings, "look"):
            scene.view_settings.look = "None"
    except (AttributeError, TypeError) as exc:
        LOG.warn(
            f"could not pin the Standard view transform ({exc}); "
            f"ground colour will read desaturated under AgX"
        )


def aim_viewports(collection):
    """Point every 3D viewport at what was just placed, and move the far clip plane past it.

    A city is not near Blender's origin and is much bigger than Blender expects. An instance
    at bundle X 1375 Z -1625 lands at Blender (1375, -1625, h), some 2.1 km out, while the
    viewport's far clip defaults to 1000 m — so the import is complete, correct, and entirely
    invisible. Frame All does not rescue it either: framing pushes the camera further back,
    which clips the content harder. Both have to change together, so both are set here rather
    than left as two manual steps to rediscover on every run.

    No-ops under `-b`, where there are no screens to aim.
    """
    objects = list(collection.objects)
    if not objects:
        return

    # Instances are empties, so their origin *is* their position. A network ribbon is one big
    # mesh whose vertices are already world-space, so its origin sits at (0,0,0) and aiming at
    # that would point the camera at the map corner. Use the bounding box where there is one.
    points = []
    for obj in objects:
        if obj.type == "MESH" and len(obj.data.vertices):
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        else:
            points.append(obj.matrix_world.translation)
    if not points:
        return
    centre = Vector(
        (
            sum(p.x for p in points) / len(points),
            sum(p.y for p in points) / len(points),
            sum(p.z for p in points) / len(points),
        )
    )
    span = max((p - centre).length for p in points)

    aimed = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                # Far enough for a whole city; near plane lifted off 0.1 m to keep some depth
                # precision at that range, but still well under building scale.
                space.clip_start = 0.5
                space.clip_end = 100000.0
                region = space.region_3d
                if region is not None:
                    region.view_location = centre
                    region.view_distance = max(span * 2.5, 100.0)
                aimed += 1

    if aimed:
        LOG.step(
            f"aimed {aimed} viewport(s) at X {centre.x:.0f} Y {centre.y:.0f} Z {centre.z:.0f}, "
            f"clip end 100000"
        )
