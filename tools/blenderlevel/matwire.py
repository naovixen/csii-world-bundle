"""The level's v1.5 materials, wired onto the placeholder slots the shape reader named.

`baseColorMap` (+ `opacityMap` as alpha, `alphaTest`/`translucent` -> blend), `diffuseColor`
when there is no map. Everything else the level records is left alone: this is an oracle for
geometry and coverage, not a renderer.
"""

import os

import bpy

from . import levelread, log


def wire_materials(materials, level_dir):
    """Turn the COLLADA importer's placeholder materials into the level's v1.5 look."""
    wired = missing = 0
    cache = {}
    for m in bpy.data.materials:
        if m.name.startswith("ter:") or m.name.startswith("probe"):
            continue
        key = m.name
        rec = materials.get(key)
        if rec is None:
            missing += 1
            continue
        stage = (rec.get("Stages") or [{}])[0]
        m.use_nodes = True
        nodes = m.node_tree.nodes
        links = m.node_tree.links
        bsdf = nodes.get("Principled BSDF")
        if bsdf is None:
            continue
        bsdf.inputs["Roughness"].default_value = float(stage.get("roughnessFactor", 0.9))
        bsdf.inputs["Metallic"].default_value = float(stage.get("metallicFactor", 0.0))
        color = stage.get("baseColorMap")
        if color:
            img = _image(cache, levelread.resolve(level_dir, color), "sRGB")
            if img is not None:
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = img
                links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        elif stage.get("diffuseColor"):
            c = [float(v) for v in stage["diffuseColor"]]
            bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)
        opacity = stage.get("opacityMap")
        translucent = bool(rec.get("translucent") or rec.get("alphaTest"))
        if opacity and translucent:
            img = _image(cache, levelread.resolve(level_dir, opacity), "Non-Color")
            if img is not None:
                tex = nodes.new("ShaderNodeTexImage")
                tex.image = img
                links.new(tex.outputs["Color"], bsdf.inputs["Alpha"])
                if "opacityFactor" in stage:
                    pass
        elif translucent and "opacityFactor" in stage:
            bsdf.inputs["Alpha"].default_value = float(stage["opacityFactor"])
        if translucent:
            _blend(m, "HASHED" if rec.get("alphaTest") else "BLEND")
        wired += 1
    log(f"materials: {wired} wired from the level, {missing} unknown to it")


def _blend(m, mode):
    # Blender 4.2+ removed blend_method for EEVEE Next in favour of surface_render_method
    if hasattr(m, "surface_render_method"):
        m.surface_render_method = "DITHERED" if mode == "HASHED" else "BLENDED"
    if hasattr(m, "blend_method"):
        try:
            m.blend_method = mode
        except TypeError:
            pass
    if hasattr(m, "use_backface_culling"):
        m.use_backface_culling = False


def _image(cache, path, colorspace):
    if path in cache:
        return cache[path]
    if not os.path.isfile(path):
        cache[path] = None
        return None
    img = bpy.data.images.load(path)
    try:
        img.colorspace_settings.name = colorspace
    except TypeError:
        pass
    cache[path] = img
    return img
