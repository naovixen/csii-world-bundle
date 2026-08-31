"""The level validator, as a registry of checks over one shared context.

`check(level_dir, bundle_dir, progress, settings) -> (checks, problems)` runs `CHECKS` in order
against a `LevelContext`. It was one 549-line function; every check below was a labelled section
inside it, sharing eleven locals with every other section and depending on all of them having
run first. The context makes that dependency a written thing, and the order a list.

The order is the contract. Each check may read what the ones before it put on the context —
`objects` and `by_class` come from the first, `dae_files` is filled by two, and `deferred`
reports what three of them gathered — so this is a sequence, not a set.
"""

from . import collision, deferred, info, inventory, materials, residency, shapes, terrain
from .context import LevelContext

#: Every check, in the order they run. `check(ctx)` each, which is what makes this a registry
#: rather than a call sequence with nine different argument lists.
CHECKS = (
    inventory.check_objects,
    inventory.check_referenced_files,
    inventory.check_forest,
    residency.check_budgets,
    residency.check_residency,
    collision.check_collision_types,
    materials.check_materials,
    shapes.check_shape_geometry,
    shapes.check_cooked_shapes,
    deferred.check_deferred,
    terrain.check_terrain,
    info.check_info,
    terrain.check_ground,
)


def check(level_dir, bundle_dir, progress=None, settings=None):
    """Validate a built level. `(checks, problems)` — the measurements, and what is wrong."""
    ctx = LevelContext(level_dir, bundle_dir, progress=progress, settings=settings)
    for one in CHECKS:
        one(ctx)
    ctx.checks["fatal"] = list(ctx.fatal_problems)
    return ctx.checks, ctx.problems


__all__ = ["CHECKS", "LevelContext", "check"]
