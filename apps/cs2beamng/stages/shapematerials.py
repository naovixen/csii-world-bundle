"""Which BeamNG material name a shape binds for each of its paint variants — the shapes
stage's view of the bake's `materials.json`, and the one place the variant-collapse rule lives.
"""

import json
import os

from ..beamng import matbind


def material_tables(ctx, level):
    """`(aliases, {variantKey: {rawMaterial: boundName}}, {(canonical, variantKey): boundKey})`
    from the bake's `materials.json`.

    A variant's tinted albedo is a different TEXTURE for the same material, so it needs its own
    BeamNG material; the materials stage writes it under this name when it sees it bound.

    **One BeamNG material per DISTINCT variant, not per variant key.** A key is a paint centre
    of one prefab, and the same advertising-board material is bound by dozens of prefabs whose
    centres all resolved to the same tinted albedo or the same factor — 1,240 of the reference
    level's 6,123 materials were byte-identical apart from their name, 42 copies of one billboard
    (refactor Phase 10 survey §7; the consumer plan's B2). So a key whose derived output matches
    an earlier key's, for the same canonical material, binds the EARLIER key's name, and the
    third table says which — the materials stage must write the material under the key the
    shapes actually bound.
    """

    path = os.path.join(ctx.derived_dir, "materials.json")
    aliases, by_variant, bound_key = {}, {}, {}
    if not os.path.isfile(path):
        return aliases, by_variant, bound_key
    with open(path, encoding="utf-8") as handle:
        table = json.load(handle)
    aliases = table.get("aliases") or {}
    representative = {}  # (canonical, derived-output signature) -> the first key carrying it
    for name, entry in sorted((table.get("materials") or {}).items()):
        derived = entry.get("derived") or {}
        canonical = entry.get("canonical") or name
        # `variantFactors` covers EVERY variant the bake measured; `variants` only the ones whose
        # per-texel tint a single `baseColorFactor` could not stand in for (A4 §2.4). A shape binds
        # a variant material either way — the difference is whether that material carries its own
        # tinted albedo or just a multiplier on the canonical one.
        for key in sorted(derived.get("variantFactors") or derived.get("variants") or {}):
            signature = json.dumps(
                [
                    (derived.get("variants") or {}).get(key),
                    (derived.get("variantFactors") or {}).get(key),
                ],
                sort_keys=True,
            )
            first = representative.setdefault((canonical, signature), key)
            bound_key[(canonical, key)] = first
            by_variant.setdefault(key, {})[name] = matbind.mapto(
                level.name, f"{canonical}__v{first}"
            )
    return aliases, by_variant, bound_key
