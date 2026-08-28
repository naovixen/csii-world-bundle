"""The §8 raster grid — PNG headers, bit depths, and the manifest's geo-reference blocks.

Every raster is a single-channel PNG on the one grid the manifest's `rasters` block
geo-references: elevation and the water surface level at bit depth 16 (the game's native
sample width, value * scale + offset = metres; the water level shares the elevation encoding,
0 = dry) and the surface index at bit depth 8 (ids, not colours). The world rasters
(`worldTerrain`/`worldWater`) cover a larger area at their own size, so only the playable-area
rasters are held to `rasters.size`.
"""

from __future__ import annotations

from .report import Context, load_json_layer, png_header

# The layer keys that make the manifest's `rasters` block mandatory when any is declared.
RASTER_LAYER_KEYS = (
    "terrain",
    "terrainFine",
    "worldTerrain",
    "surfaceIndex",
    "splatmap",
    "userSplatmap",
    "water",
    "worldWater",
)


def _check_grayscale(ctx: Context, rasters: dict) -> None:
    """The grayscale rasters: bit depth, colour type, the playable grid, and the scale keys."""
    report = ctx.report
    size = rasters.get("size")
    expectations = (
        ("terrain", 16, True, ("elevationScale", "elevationOffset")),
        ("surfaceIndex", 8, True, ()),
        ("water", 16, True, ("elevationScale", "elevationOffset")),
        ("worldTerrain", 16, False, ("elevationScale", "elevationOffset")),
        ("worldWater", 16, False, ("elevationScale", "elevationOffset")),
        ("terrainFine", 16, False, ("elevationScale", "elevationOffset")),
    )
    for key, depth, playable_grid, needed_keys in expectations:
        rel = ctx.layers.get(key)
        if rel is None:
            continue
        path = ctx.bundle_dir / rel
        if not path.is_file():
            continue  # the existence pass has already reported it
        header = png_header(path)
        if header is None:
            report.error(f"layers.{key}: {rel!r} is not a PNG")
            continue
        width, height, bit_depth, colour_type = header
        if colour_type != 0:
            report.error(
                f"layers.{key}: {rel!r} colour type is {colour_type}, "
                "contract §8 requires 0 (grayscale)"
            )
        if bit_depth != depth:
            report.error(
                f"layers.{key}: {rel!r} bit depth is {bit_depth}, contract §8 requires {depth}"
            )
        if playable_grid and isinstance(size, list) and len(size) == 2 and [width, height] != size:
            report.error(
                f"layers.{key}: {rel!r} is {width}x{height}, but rasters.size is "
                f"{size[0]}x{size[1]}"
            )
        for needed in needed_keys:
            if needed not in rasters:
                report.error(f"layers.{key} is declared but rasters.{needed} is missing")


def _check_splats(ctx: Context) -> None:
    """The splat weight maps are RGBA8, not grayscale (§8)."""
    report = ctx.report
    for key in ("splatmap", "userSplatmap"):
        rel = ctx.layers.get(key)
        if rel is None:
            continue
        path = ctx.bundle_dir / rel
        if not path.is_file():
            continue  # the existence pass has already reported it
        header = png_header(path)
        if header is None:
            report.error(f"layers.{key}: {rel!r} is not a PNG")
            continue
        _width, _height, bit_depth, colour_type = header
        if colour_type != 6 or bit_depth != 8:
            report.error(
                f"layers.{key}: {rel!r} is colour type {colour_type} at depth {bit_depth}, "
                "contract §8 requires RGBA8 (colour type 6, depth 8)"
            )


def _check_georef_blocks(ctx: Context, rasters: dict) -> None:
    """Rasters on their own grid carry their own geo-referencing sub-block (§3), whose `file`
    and `size` must agree with the layer it describes."""
    report = ctx.report
    for key in ("splatmap", "userSplatmap", "worldTerrain", "worldWater", "terrainFine"):
        rel = ctx.layers.get(key)
        if rel is None:
            continue
        block = rasters.get(key)
        if not isinstance(block, dict):
            report.error(f"layers.{key} is declared but rasters.{key} is missing")
            continue
        if block.get("file") != rel:
            report.error(
                f"rasters.{key}.file is {block.get('file')!r}, but layers.{key} is {rel!r}"
            )
        path = ctx.bundle_dir / rel
        header = png_header(path) if path.is_file() else None
        block_size = block.get("size")
        if header is not None and isinstance(block_size, list) and len(block_size) == 2:
            width, height = header[0], header[1]
            if [width, height] != block_size:
                report.error(
                    f"layers.{key}: {rel!r} is {width}x{height}, "
                    f"but rasters.{key}.size is {block_size[0]}x{block_size[1]}"
                )


def _check_surfaces_index(ctx: Context) -> None:
    """The `surfaces/` catalogue: an index.json whose texture references resolve."""
    report = ctx.report
    surfaces_dir = ctx.layers.get("surfaces")
    if surfaces_dir is None:
        return
    index_path = ctx.bundle_dir / surfaces_dir / "index.json"
    if not index_path.is_file():
        report.error(f"layers.surfaces: {surfaces_dir!r} has no index.json")
        return
    label = f"{surfaces_dir}index.json"
    index = load_json_layer(index_path, ctx.schemas["surfaceindex"], label, report)
    for surface_id, entry in (index or {}).items():
        if not isinstance(entry, dict):
            continue
        # `texture` may be null when the theme texture could not be read (§8), which is
        # a stated absence rather than a broken reference.
        texture = entry.get("texture")
        if texture is not None and not (ctx.bundle_dir / texture).is_file():
            report.error(f"{label}[{surface_id}]: texture {texture!r} does not exist")


def check_rasters(ctx: Context) -> None:
    """The terrain-layer invariants of contract §8, when any raster layer is declared."""
    raster_layers = [k for k in RASTER_LAYER_KEYS if k in ctx.layers]
    if not raster_layers:
        return
    if "rasters" not in ctx.manifest:
        ctx.report.error(
            f"bundle.json declares raster layers {raster_layers} but has no 'rasters' block"
        )
        return
    rasters = ctx.manifest.get("rasters", {})
    _check_grayscale(ctx, rasters)
    _check_splats(ctx)
    _check_georef_blocks(ctx, rasters)
    _check_surfaces_index(ctx)
