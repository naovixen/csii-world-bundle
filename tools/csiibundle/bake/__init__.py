"""`csiibundle.bake` — the offline bake: engine-neutral derivatives, once per rip.

The in-game rip is deliberately raw and fast (its single writer thread gates the whole rip), and
some of what every consumer needs can only be computed AFTER the rip — draping a marking needs
the assembled road surface, conforming an area needs the terrain, a skirt needs the ground under
its instance. The bake computes those once into `<bundle>/derived/` (contract §13) so that a
BeamNG level builder, the Blender adapter and any other consumer read the same result instead of
each re-deriving it.

    python -m csiibundle.bake <bundle> [--only markings,areas] [--region X Z R] [--force]

Three rules this package keeps:

* **It never writes into a producer layer.** Everything lands under the derived directory, and
  deleting that directory loses nothing that a re-bake cannot rebuild.
* **Staleness is per product.** `manifest.py` fingerprints every input layer a product was
  computed from; a product whose inputs changed is stale and is re-baked, the others are not.
  This is what makes the bake safe on a mixed-vintage bundle (§1).
* **It never imports `bpy`, and it never blocks without reporting.** Every product reports
  progress through the small `Progress` protocol in `progress.py`, chunked so a caller (a CLI
  bar, an application) hears from it at least every half second and can cancel between chunks.

Products register themselves in `PRODUCTS` (name → `Product`). B0 ships the framework with an
empty registry; B2–B4 add `materials`, `lods`, `markings`, `lanegeometry`, `areas`, `skirts` and
`terrain`.
"""

from collections import namedtuple

# Version of the bake's own outputs, recorded in `derived.json`. Bump when any product's bytes
# would change for the same bundle and options — a consumer treats a different bakeVersion as
# stale across the board. CONTRACT (for the derived cache, §13).
BAKE_VERSION = 1

# One registered product: the layer keys it reads (its staleness inputs), the path it writes
# under the derived directory (a directory ends in '/'), and the callable that bakes it:
#     run(context, progress) -> dict of stats
# `version` is the product's own output version: bump it when a product's outputs change shape
# (a new raster, a new field) so only THAT product re-bakes — BAKE_VERSION re-bakes everything.
# `options` names the bake options THIS product's outputs depend on beyond the shaping ones
# (tile/diagonal/region, which reshape every product): a change to one of them re-bakes this
# product alone — `ground` for the products that conform to the terrain, not for materials/lods.
Product = namedtuple("Product", "name inputs path run version options", defaults=(1, ()))

# name -> Product. Filled by the product modules on import (see `products()`), kept empty at
# import time so `import csiibundle.bake` stays cheap.
PRODUCTS = {}


def register(product):
    """Add a `Product` to the registry (idempotent by name)."""
    PRODUCTS[product.name] = product
    return product


# Product modules, in bake order. Imported lazily by `products()` so a consumer that only wants
# `manifest` (to ask "is this derived cache fresh?") never pays for the numpy-heavy imports.
PRODUCT_MODULES = ("materials", "lods", "markings", "lanegeometry", "areas", "skirts", "terrainmask", "terraincut")


def products():
    """The registry, after importing every product module. Order is bake order."""
    import importlib
    ordered = {}
    for name in PRODUCT_MODULES:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
            continue                                   # a phase not built yet
    for name in PRODUCT_MODULES:
        for key, product in PRODUCTS.items():
            if key not in ordered and (key == name or product.name == name):
                ordered[key] = product
    for key, product in PRODUCTS.items():
        ordered.setdefault(key, product)
    return ordered


__all__ = ["BAKE_VERSION", "Product", "PRODUCTS", "register", "products"]
