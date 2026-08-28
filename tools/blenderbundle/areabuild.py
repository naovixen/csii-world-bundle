"""Place the §8b surface areas: driveways, parking-lot pavement, park paths.

These are the painted patches `AreaBatchSystem` draws from area triangles — the visible ground
of every meshless prefab (parking lots, sports courts, campsites) and of house driveways.

The conformance rules are `csiibundle.areas`: an area triangle is a FOOTPRINT the game projects
onto the terrain, never a plane, and that module's docstring says what follows from that. What
is here is the Blender side — the PBR material each catalogue entry describes, the objects, and
the report.
"""

import json
import os

import bpy
from csiibundle import areas as area_math
from csiibundle import materials
from csiibundle.reader import read_jsonl

from . import shadernodes
from .log import LOG

UV_SCALE_FALLBACK = area_math.UV_SCALE_FALLBACK


class _Materials:
    """One material per area prefab, built on first use and remembered.

    `areaprefabs.json` carries `normalMap`, `maskMap`, `metallic` and `smoothness` beside
    the diffuse; before it did, pavement and plazas rendered flat-lit with guessed specular
    (audit #40 P11). The maskMap packs §7's channels, so roughness is `1 - smoothness` and
    metallic is R — the same two `csiibundle.materials` wires for object materials, and for
    the same reason.
    """

    def __init__(self, bundle_dir, catalogue):
        self.bundle_dir = bundle_dir
        self.catalogue = catalogue
        self.packings = {}
        self._built = {}

    def get(self, prefab):
        """This prefab's material, built once."""
        if prefab not in self._built:
            self._built[prefab] = self._build(prefab)
        return self._built[prefab]

    def _build(self, prefab):
        surface = area_math.surface_material(self.catalogue.get(prefab))
        colour = surface["color"] or (0.42, 0.40, 0.38, 1.0)
        material = shadernodes.ground_material(
            f"area_{prefab}",
            os.path.join(self.bundle_dir, surface["texture"]) if surface["texture"] else None,
            (colour[0], colour[1], colour[2], 1.0),
        )
        tree = material.node_tree
        bsdf = tree.nodes.get("Principled BSDF")

        # Scalars first, so an entry with no maskMap still gets its authored values instead of
        # the generic ground roughness.
        if surface["metallic"] is not None:
            bsdf.inputs["Metallic"].default_value = surface["metallic"]
        if surface["roughness"] is not None:
            bsdf.inputs["Roughness"].default_value = surface["roughness"]

        self._wire_normal(tree, bsdf, self._path(surface["normal_map"]))
        self._wire_mask(tree, bsdf, self._path(surface["mask_map"]))
        return material

    def _path(self, relative):
        """An absolute path into the bundle, or None when the entry names no such map."""
        if not relative:
            return None
        path = os.path.join(self.bundle_dir, relative)
        return path if os.path.isfile(path) else None

    def _wire_normal(self, tree, bsdf, path):
        """The authored normal map, rebuilt to §7.1 when the bundle ships it packed.

        An area normal map is not reliably three-channel. Feeding a packed one straight into a
        Normal Map node points every shading normal sideways or into the surface, and the
        surface renders BLACK — see `materials.detect_normal_packing`. The producer rebuilds
        these to §7.1 as of that change; until a bundle is re-ripped, this is what keeps the
        ground lit.
        """
        if path is None:
            return
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = shadernodes.unpremultiply(bpy.data.images.load(path, check_existing=True))
        tex.image.colorspace_settings.name = "Non-Color"  # set BEFORE sampling the pixels

        packing = materials.detect_normal_packing(shadernodes.sample_rgba(tex.image))
        self.packings[packing] = self.packings.get(packing, 0) + 1

        nmap = tree.nodes.new("ShaderNodeNormalMap")  # CS2 authors OpenGL +Y, as Blender wants
        tree.links.new(
            shadernodes.tangent_normal_colour(tree, tex, packing, bsdf.location),
            nmap.inputs["Color"],
        )
        tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    def _wire_mask(self, tree, bsdf, path):
        """R is metallic and A is smoothness, so roughness is `1 - A` — §7's channels."""
        if path is None:
            return
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = shadernodes.unpremultiply(bpy.data.images.load(path, check_existing=True))
        tex.image.colorspace_settings.name = "Non-Color"
        sep = tree.nodes.new("ShaderNodeSeparateColor")
        tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
        tree.links.new(sep.outputs["Red"], bsdf.inputs["Metallic"])
        invert = tree.nodes.new("ShaderNodeMath")
        invert.operation = "SUBTRACT"
        invert.inputs[0].default_value = 1.0
        tree.links.new(tex.outputs["Alpha"], invert.inputs[1])  # A is smoothness
        tree.links.new(invert.outputs[0], bsdf.inputs["Roughness"])


def _read_catalogue(bundle_dir):
    """areaprefabs.json, or an empty catalogue for a bundle that predates it."""
    catalogue_path = os.path.join(bundle_dir, "areaprefabs.json")
    if not os.path.isfile(catalogue_path):
        return {}
    with open(catalogue_path, encoding="utf-8") as handle:
        return json.load(handle)


def _build_objects(builders, collection, area_materials, catalogue):
    """One object per area prefab, with world-planar UVs at the prefab's own authored rate.

    `colossal_UVScale` is set straight from `RenderedArea.m_UVScale` (AreaBatchSystem line
    2109) and multiplies the world position. `UV_SCALE_FALLBACK` is the game's own field
    default, for a bundle ripped before §8b carried the value.
    """
    built = 0
    for prefab, sink in builders.items():
        if not sink["faces"]:
            continue
        mesh = bpy.data.meshes.new(f"area_{prefab}")
        mesh.from_pydata(sink["verts"], [], sink["faces"])
        mesh.materials.append(area_materials.get(prefab))
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_scale = float((catalogue.get(prefab) or {}).get("uvScale") or UV_SCALE_FALLBACK)
        for loop in mesh.loops:
            x, y, _ = mesh.vertices[loop.vertex_index].co
            uv_layer.data[loop.index].uv = (x * uv_scale, y * uv_scale)
        mesh.validate()
        obj = bpy.data.objects.new(f"area_{prefab}", mesh)
        collection.objects.link(obj)
        built += 1
    return built


def _report_packings(packings):
    """Say it out loud when a bundle ships normal maps the contract does not describe: rebuilt
    here they light correctly, but the fix belongs in the producer, and a silent rebuild is how
    a consumer written against §7.1 alone inherits black ground without ever knowing why."""
    packed = {k: v for k, v in packings.items() if k != materials.NORMAL_RGB}
    if not packed:
        return
    LOG.warn(
        f"{sum(packed.values())} of {sum(packings.values())} area normal map(s) "
        f"are two-channel packed ({', '.join(f'{n}x {k}' for k, n in sorted(packed.items()))}) "
        "rather than the tangent RGB of contract §7.1 — rebuilt here (they would "
        "otherwise shade black); re-rip to ship them unpacked"
    )


def _report_grey(builders, catalogue):
    """A prefab with neither texture nor colour renders the neutral grey fallback — say why,
    per prefab, instead of leaving grey boxes as a riddle. `unresolved` is the producer's
    flag for "the save references an asset (pack) that was not loaded in the rip session":
    the game itself drew those areas grey (Missing Area fallback), and the cure is enabling
    the pack in-game and re-ripping, not anything on this side."""
    grey = [
        prefab
        for prefab in builders
        if not (catalogue.get(prefab) or {}).get("texture")
        and not (catalogue.get(prefab) or {}).get("color")
    ]
    if not grey:
        return
    unresolved = [p for p in grey if (catalogue.get(p) or {}).get("unresolved")]
    for prefab in sorted(unresolved):
        name = (catalogue.get(prefab) or {}).get("name", prefab)
        LOG.warn(
            f"area prefab did not resolve at rip time (asset pack not loaded "
            f"or version mismatch): {name}"
        )
    if unresolved:
        LOG.warn(
            f"{len(unresolved)} area prefab(s) render grey because their asset "
            "was missing in the rip session — the game drew them grey too "
            "(Missing Area); enable the packs, load the save, re-rip"
        )
    for prefab in sorted(set(grey) - set(unresolved)):
        LOG.warn(f"area prefab carries no colour and no texture: {prefab}")


def _report_conformance(builders, stats, ground):
    """How much of the painted area followed the terrain, and how much did not."""
    if ground is None:
        LOG.warn(
            "terrain layer not built — surface areas stay flat, expect the ground to "
            "cut through pavement on any slope (audit §30)"
        )
        return
    counters = stats["counters"]
    total = sum(len(sink["faces"]) for sink in builders.values())
    LOG.step(
        f"{int(stats['source_tris'])} area triangle(s) conformed to the terrain as "
        f"{total} piece(s)"
        + (f", {stats['missed']} node(s) found no ground" if stats["missed"] else "")
    )
    if counters["unconformed"]:
        share = 100.0 * counters["unconformed_area"] / max(stats["painted_area"], 1e-9)
        LOG.step(
            f"{counters['unconformed_area']:.0f} m2 of {stats['painted_area']:.0f} m2 painted "
            f"({share:.2f}%) stayed unconformed — ground up to "
            f"{counters['worst'] * 100:.0f} cm above it, over "
            f"{counters['unconformed']} piece(s)"
        )


def build_areas(bundle_dir, collection, center, radius, ground=None, cell=3.5):
    """Place the §8b surface areas: driveways, parking-lot pavement, park paths.

    One object per area prefab, world-anchored UVs, catalogue texture or colour.

    `ground` is a `raysurface.ProjectionSurface` over the terrain grid and `cell` its spacing.
    The game paints an area onto the terrain rather than laying a plane on it (audit §30), so
    with a ground to conform to, each triangle is split until it follows — see
    `area_math.conform_triangle`. Without one (`--areas` and no `--terrain`) the flat triangles
    are all there is, and the ground will cut through them wherever it is not level.
    """
    path = os.path.join(bundle_dir, "areas.jsonl")
    if not os.path.isfile(path):
        LOG.warn("no areas.jsonl — skipping surface areas (bundle predates §8b)")
        return 0

    catalogue = _read_catalogue(bundle_dir)
    area_materials = _Materials(bundle_dir, catalogue)

    # The ground each area is painted onto (audit §30), memoised — the conformance re-samples
    # the same points constantly. Without the terrain layer there is nothing to conform to and
    # the flat triangles are all this can draw.
    ground_at = area_math.memoised_sampler(ground)

    # The whole loop is `csiibundle.areas.build_pieces`: region filter, frame change, winding
    # reversal, and the adaptive split. What comes back is Z-up triangle soup per prefab.
    builders, stats = area_math.build_pieces(
        read_jsonl(path),
        cell,
        ground_at,
        center=center,
        radius=radius,
        progress=lambda n: LOG.progress("areas conformed", n, None),
        catalogue=catalogue,
    )

    built = _build_objects(builders, collection, area_materials, catalogue)
    LOG.done(
        f"areas:     {stats['placed']} surface area(s) into {built} object(s) "
        f"over {len(builders)} prefab(s)"
        + (f", {stats['skipped_region']} outside the region" if stats["skipped_region"] else "")
    )
    _report_packings(area_materials.packings)
    _report_grey(builders, catalogue)
    _report_conformance(builders, stats, ground)
    if stats["dropped_bad"]:
        LOG.warn(f"CONTRACT VIOLATION: {stats['dropped_bad']} triangle(s) index out of range")
    return built
