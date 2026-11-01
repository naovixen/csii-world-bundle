"""The `lods` product's per-asset reuse rule: when a previous bake's entry in `lods.json` can
stand, so its asset is not decimated again.
"""

import os

from .lodrefine import REFINE_MIN_TRIS, REFINE_TARGETS

# The reuse rule's own version: bump it when a change would make a PREVIOUS bake's per-asset
# output wrong, so `reusable` stops standing on it. The product version re-bakes everything;
# this re-bakes nothing that has not changed.
REUSE_RULE_VERSION = 2


def refine_key(refine):
    """The refinement inputs an output depends on - a change to any of them invalidates a refined
    level, and only a refined level."""
    if not refine.get("enabled"):
        return None
    return [
        float(refine["maxError"]),
        float(refine.get("maxPeak") or 0.0),
        list(REFINE_TARGETS),
        REFINE_MIN_TRIS,
    ]


def reusable(entry, out_dir, targets, refine):
    """Whether this asset's previous bake can stand as-is, so the worker is not run for it.

    `lods` is the bake's expensive product and staleness is per PRODUCT, so ANY version bump here
    re-decimates every LOD 1 and LOD 2 in the bundle — 4,356 meshes, the step that makes a bake an
    afternoon rather than a coffee. An entry stands when it was baked at the same targets, under
    the same reuse rule, and the files it named are still on disk; anything else is re-run.
    """
    if not entry or entry.get("targets") != list(targets):
        return False
    if entry.get("reuseV") != REUSE_RULE_VERSION:
        return False
    if entry.get("refineKey") != refine_key(refine):
        return False
    for name in entry.get("files") or ():
        if not os.path.isfile(os.path.join(out_dir, name)):
            return False
    return True
