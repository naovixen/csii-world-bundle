"""Finding things in a bundle: the manifest, the layer paths, the record streams.

Implements the parts of `docs/bundle-schema.md` §1 (the manifest), §3 (the record layers) and
§10 (declared gaps) that every consumer needs before it can do anything else.

Three ideas are worth reading once:

* **Every layer path comes from `bundle.json`.** The conventional file names are only a
  fallback, and a consumer that hard-codes them will break on the first bundle whose producer
  renames one. `resolve_layers()` and `terrain_paths()` do the lookup and hand back absolute
  paths.
* **The record layers are JSON Lines**, one self-contained object per line, so a full city's
  1.3M instances stream in constant memory. `read_jsonl()` is a generator on purpose; a
  consumer that calls `list()` on `instances.jsonl` is asking for several gigabytes.
* **`warnings.jsonl` is part of the contract, not a log.** It names the assets the producer
  knows it could not deliver (§10). A missing mesh that appears there is a declared gap; one
  that does not is a bundle defect. `read_declared_gaps()` is how a consumer tells them apart
  instead of reporting the producer's own honesty back as errors.
"""

import json
import os
from collections import namedtuple

# The manifest file name is fixed; everything inside it is looked up. CONTRACT.
MANIFEST_NAME = "bundle.json"

# Fallback layer paths, used only when a bundle has no manifest. These are the conventional
# names the producer has always written and are what a pre-manifest bundle actually has on
# disk. CONTRACT (as the documented default layout), but never preferred over the manifest.
DEFAULT_LAYERS = {
    "instances": "instances.jsonl",
    "networks": "networks.jsonl",
    "lanes": "lanes.jsonl",
    "laneGeometry": "lanemeshes.jsonl",
    "lanePrefabs": "laneprefabs.json",
    "materials": "materials.json",
    "colors": "colors.json",
    "lights": "lights.json",
    "meshes": "meshes/",
    "networkMeshes": "networks/",
    "laneMeshes": "lanemeshes/",
    "textures": "textures/",
    "terrain": "terrain/heightmap.png",
    "terrainFine": "terrain/heightmap_fine.png",
    "surfaceIndex": "terrain/surface_index.png",
    "splatmap": "terrain/splatmap.png",
    "userSplatmap": "terrain/user_splatmap.png",
    "surfaces": "terrain/surfaces/",
    "water": "terrain/water_level.png",
    "areas": "areas.jsonl",
    "areaPrefabs": "areaprefabs.json",
    "districts": "districts.jsonl",
    "clipVolumes": "clipvolumes.jsonl",
    "grading": "grading.json",
    "warnings": "warnings.jsonl",
}


# -- the manifest -----------------------------------------------------------------------------

def load_manifest(bundle_dir):
    """`bundle.json` as a dict, or `None` for a bundle written before the manifest existed."""
    path = os.path.join(bundle_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def layer_path(bundle_dir, key, manifest=None):
    """The absolute path of one layer, by its manifest key (`"instances"`, `"meshes"`, …).

    Falls back to `DEFAULT_LAYERS` when the manifest is absent or does not name the key. The
    result is a path whether or not anything is there — `os.path.isfile`/`isdir` is the
    caller's check, because an absent optional layer is normal (§1: a partial rip leaves the
    stages it did not run alone).
    """
    if manifest is None:
        manifest = load_manifest(bundle_dir)
    layers = (manifest or {}).get("layers", {})
    rel = layers.get(key, DEFAULT_LAYERS.get(key))
    if rel is None:
        raise KeyError(f"unknown bundle layer {key!r}")
    return os.path.join(bundle_dir, rel)


Layers = namedtuple("Layers", "meshes instances networks net_meshes lane_geometry "
                              "lane_meshes lane_prefabs")


def resolve_layers(bundle_dir):
    """The record and mesh-directory layers a geometry consumer needs, as absolute paths.

    Returns a `Layers` namedtuple, so both `layers.instances` and the positional unpack work.

    Note which key is which: `laneGeometry` is the per-instance RECORD layer (`lanemeshes.jsonl`)
    and `laneMeshes` is the part-mesh DIRECTORY (`lanemeshes/`). Reading `laneMeshes` for the
    records hands a consumer a directory where it expects a `.jsonl` — a mistake this repo has
    already made once, and one a pre-manifest bundle hides because the fallback happens to be
    right.
    """
    manifest = load_manifest(bundle_dir)
    return Layers(
        meshes=layer_path(bundle_dir, "meshes", manifest),
        instances=layer_path(bundle_dir, "instances", manifest),
        networks=layer_path(bundle_dir, "networks", manifest),
        net_meshes=layer_path(bundle_dir, "networkMeshes", manifest),
        lane_geometry=layer_path(bundle_dir, "laneGeometry", manifest),
        lane_meshes=layer_path(bundle_dir, "laneMeshes", manifest),
        lane_prefabs=layer_path(bundle_dir, "lanePrefabs", manifest),
    )


TerrainPaths = namedtuple("TerrainPaths", "heightmap surface_index water surfaces splatmap rasters heightmap_fine")


def terrain_paths(bundle_dir):
    """The §8 raster layers plus the `rasters` geo-reference block.

    `rasters` is the block that says where each raster sits in world space (origin, resolution,
    height range) — without it the heightmap is a picture, not terrain. It lives in the manifest;
    for a pre-manifest bundle the producer wrote the same block to `terrain/rasters.json`, which
    is the fallback here. `rasters` is `None` when neither exists.

    `heightmap_fine` is the path of the optional `layers.terrainFine` raster (§8, 2026-08-16) —
    the game's near-camera ground at 1.75 m — or `None` when the manifest does not declare it.
    Consumers that mesh the ground go through `load_heightfield`, which prefers it.
    """
    manifest = load_manifest(bundle_dir)
    rasters = (manifest or {}).get("rasters")
    if rasters is None:
        sidecar = os.path.join(bundle_dir, "terrain", "rasters.json")
        if os.path.isfile(sidecar):
            with open(sidecar, encoding="utf-8") as handle:
                rasters = json.load(handle)

    declared = (manifest or {}).get("layers", {})
    return TerrainPaths(
        heightmap=layer_path(bundle_dir, "terrain", manifest),
        surface_index=layer_path(bundle_dir, "surfaceIndex", manifest),
        water=layer_path(bundle_dir, "water", manifest),
        surfaces=layer_path(bundle_dir, "surfaces", manifest),
        splatmap=layer_path(bundle_dir, "splatmap", manifest),
        rasters=rasters,
        heightmap_fine=layer_path(bundle_dir, "terrainFine", manifest) if "terrainFine" in declared else None,
    )


Heightfield = namedtuple("Heightfield", "raw cell origin_x origin_z elevation_scale elevation_offset fine path")


def load_heightfield(bundle_dir, prefer_fine=True):
    """The ground raster a consumer should mesh: `raw` uint16 rows north-first (row 0 toward −Z),
    `cell` metres, `(origin_x, origin_z)` the top-left texel centre, the elevation decode, and
    `fine` — whether this is `layers.terrainFine` (the game's near-camera 1.75 m ground: the
    same heightmap with the road/lot/area grading rasterised finer, §8) or the 3.5 m base map.

    Contract §8: when the manifest declares `terrainFine` the file MUST exist — a declared layer
    that is missing is a contract violation, never a fallback. A manifest that does not declare
    it (a rip older than 2026-08-16, or one whose capture failed and said so in warnings.jsonl)
    is served the base map, and `fine` is False so the caller can say which ground it built.
    Returns `None` when the bundle has no terrain layer at all.
    """
    from . import png
    paths = terrain_paths(bundle_dir)
    if paths.rasters is None or not os.path.isfile(paths.heightmap):
        return None
    rasters = paths.rasters
    scale = float(rasters.get("elevationScale", 1.0))
    offset = float(rasters.get("elevationOffset", 0.0))
    if prefer_fine and paths.heightmap_fine is not None:
        block = rasters.get("terrainFine")
        if not os.path.isfile(paths.heightmap_fine) or not isinstance(block, dict):
            raise RuntimeError(f"CONTRACT VIOLATION: bundle.json declares layers.terrainFine but "
                               f"{paths.heightmap_fine} or rasters.terrainFine is missing (§8)")
        raw = png.gray_array(paths.heightmap_fine)
        res = block["resolution"]
        cell = float(res[0]) if isinstance(res, (list, tuple)) else float(res)
        origin_x, _, origin_z = (float(v) for v in block["originWorld"])
        size = block.get("size")
        if size and (int(size[0]), int(size[1])) != (raw.shape[1], raw.shape[0]):
            raise RuntimeError(f"CONTRACT VIOLATION: {paths.heightmap_fine} is {raw.shape[1]}x{raw.shape[0]}, "
                               f"rasters.terrainFine.size says {size}")
        return Heightfield(raw, cell, origin_x, origin_z, scale, offset, True, paths.heightmap_fine)
    raw = png.gray_array(paths.heightmap)
    origin_x, _, origin_z = (float(v) for v in rasters["originWorld"])
    return Heightfield(raw, float(rasters["resolution"]), origin_x, origin_z, scale, offset, False, paths.heightmap)


def resample_nearest(raster, shape):
    """`raster` (rows, cols[, c]) resampled to `shape` (rows, cols) by nearest sample — for the
    3.5 m companions of the fine heightfield (surface index, water level) that stay on the base
    grid by contract and are read by world position; both grids share the same top-left texel
    centre... up to half a coarse cell (§8: the fine texel centres sit at the coarse cell's
    quarter points), which is what the `+ 0.5` below expresses."""
    import numpy
    src_h, src_w = raster.shape[:2]
    dst_h, dst_w = int(shape[0]), int(shape[1])
    if (src_h, src_w) == (dst_h, dst_w):
        return raster
    rows = numpy.minimum(((numpy.arange(dst_h) + 0.5) * (src_h / dst_h)).astype(numpy.int64), src_h - 1)
    cols = numpy.minimum(((numpy.arange(dst_w) + 0.5) * (src_w / dst_w)).astype(numpy.int64), src_w - 1)
    return raster[rows][:, cols]


def schema_version(bundle_dir_or_manifest):
    """The bundle's declared schema version, or `None` for a pre-manifest bundle."""
    manifest = bundle_dir_or_manifest
    if isinstance(manifest, str):
        manifest = load_manifest(manifest)
    if not manifest:
        return None
    return manifest.get("schema")


# -- record streams ---------------------------------------------------------------------------

def read_jsonl(path, contains=None):
    """Stream one JSON Lines layer, yielding a dict per record.

    Blank lines are skipped; nothing is buffered. A full city's `instances.jsonl` is over a
    million records, so this stays a generator all the way to the caller.

    `contains` is a raw-text prefilter: a line without that substring is skipped without being
    parsed. It is a **superset** filter — every line that could match is still decoded, and the
    caller still applies its own test to what comes back — so it changes what this costs and
    never what it yields. Decoding 1.2 M records to keep the buildings was 22 s of the skirts
    product; the substring test is a hundredth of that.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if contains is not None and contains not in line:
                continue
            line = line.strip()
            if line:
                yield json.loads(line)


def read_declared_gaps(bundle_dir):
    """`{assetId: reason}` for everything the producer declared it could not deliver (§10).

    A consumer that finds no mesh for an assetId should look here before calling the bundle
    broken: a declared gap is the producer being honest about a piece the game would not hand
    over (an unresolved asset pack, a non-resident geometry buffer), and re-reporting it as an
    error makes the error count a function of that honesty. An assetId that is missing and
    *not* in here is a genuine defect.

    Missing `warnings.jsonl` is not itself an error — a clean rip may write none.
    """
    path = layer_path(bundle_dir, "warnings")
    gaps = {}
    if not os.path.isfile(path):
        return gaps
    for record in read_jsonl(path):
        asset_id = record.get("assetId")
        if isinstance(asset_id, str):
            gaps[asset_id] = record.get("reason", "")
    return gaps


# -- catalogues -------------------------------------------------------------------------------
#
# The `.json` layers are small tables keyed by id, unlike the streamed `.jsonl` record layers.
# Loading one whole is the intended use. Each returns `{}` when its layer is absent, because a
# partial rip legitimately ships without it (§1) and a consumer's answer to "no materials" is
# to draw untextured, not to fail.

def load_catalogue(bundle_dir, key):
    """One of the `.json` table layers by manifest key, or `{}` when it is not there."""
    path = layer_path(bundle_dir, key)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_materials(bundle_dir):
    """`materials.json` (§7): `{materialId: {baseColor, normal, mask, colorMask, …}}`.

    Texture values are bundle-relative paths. `os.path.join(bundle_dir, rel)` resolves them.

    **Most of this table is also inside the `.glb` files, and a glTF-reading consumer gets it
    for free.** Verified against the shipped bundle: across 117 material uses the `.glb`'s own
    `alphaMode` and `doubleSided` agree with this table exactly (46 BLEND / 71 MASK, 47
    double-sided). So do not build a second material pipeline off this layer — read it for the
    things glTF has no place for, namely `colorMask` (the §7.2 per-instance paint selector, see
    `csiibundle.tints`) and `tint`, and let your glTF loader do the rest.
    """
    return load_catalogue(bundle_dir, "materials")


def load_colors(bundle_dir):
    """`colors.json` (§7.2): the per-prefab colour sets a `colorMask` selects between."""
    return load_catalogue(bundle_dir, "colors")


def load_lights(bundle_dir):
    """`lights.json` (§4.3): photometry per light-bearing prefab."""
    return load_catalogue(bundle_dir, "lights")


def load_lane_prefabs(bundle_dir):
    """`laneprefabs.json` (§5B): the part list each lane record assembles."""
    return load_catalogue(bundle_dir, "lanePrefabs")


def load_area_prefabs(bundle_dir):
    """`areaprefabs.json` (§8b): the surface material each area kind paints with."""
    return load_catalogue(bundle_dir, "areaPrefabs")


def load_grading(bundle_dir):
    """`grading.json` (§11) as a raw dict. `colour.load()` turns it into applicable constants."""
    return load_catalogue(bundle_dir, "grading")
