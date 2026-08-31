"""Stage `materials` — the bake's resolved material table → `art/cs2/main.materials.json` +
`art/textures/*.dds` (pre-compressed + mipmapped, capped, suffixed; `.png` without the encoder),
plus the area-surface materials.

* Every CANONICAL material (the bake's alias map collapses identical texture sets: 4,641 → 1,409
  on the reference city) becomes one v1.5 material named `mapto(level, canonical)`; the shapes
  and tiles bind that name (they map raw names through the same alias table), so BeamNG sees
  ~1.4k materials, not 4.6k.
* Textures are exported once per (file, size cap) on a process pool: colour maps as sRGB
  (`.color.dds`, BC7), normals `.normal.dds` (BC5), the bake's split roughness/metallic/AO and
  the albedo alpha as linear `.data.dds` (BC4), the colour mask as linear BC7 — the formats
  BeamNG's own cooker produces, so nothing is cooked at load (`beamng/dds.py`).
* `area:<prefab>` materials come from `derived/areas/materials.json` (`areas.surface_material`):
  the theme texture + normal, `diffuseColor` tint, scalar roughness/metallic; a texture with a
  real alpha channel gets `alphaTest`.
* What no table resolves gets a grey placeholder so a `.dae` never binds a name BeamNG cannot
  find (that renders as the orange "no material" checker).

The stage records `ctx.shared["materials"] = {"aliases", "canonical", "known"}` for the
shape/tile stages: `beamng.materials.mapto_name(shared, level, raw)` is the one function that
turns a bundle material name into the bound name. That binding used to live HERE, and the
`derived` and `networks` stages imported it out of this module — one stage acting as another's
library. It sits beside `mapto`, which it calls.
"""

import json
import os
import time

from csiibundle import png

from ..beamng import matbind, textureplan
from ..beamng import materials as bm
from . import Stage, level_of
from .materialexport import export_all
from .materialledger import (
    file_stamp,
    ledger_key,
    load_ledger,
    partition,
    save_ledger,
)

STAGE_KEY = "materials"


def enabled(settings):
    return True


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("materials") or 1)


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    caps = bm.caps_from_settings(settings)  # per kind: colour / normal / data (A4 2.1)
    # pre-compressed DDS (BC7/BC5/BC4 + mips, the cooker's own formats) unless the encoder is
    # missing or the setting turns it off; PNG means BeamNG cooks on the first load of the name
    use_dds = bool(settings.get("texture_dds", True)) and textureplan.dds_mod.available()
    if settings.get("texture_dds", True) and not textureplan.dds_mod.available():
        progress.log(
            "materials: etcpak not installed - textures go out as PNG and BeamNG cooks them on first load "
            "(pip install etcpak)"
        )
    table_path = os.path.join(ctx.derived_dir, "materials.json")
    if not os.path.isfile(table_path):
        raise RuntimeError(
            "derived/materials.json missing: the bake's materials product has not run"
        )
    with open(table_path, encoding="utf-8") as handle:
        table = json.load(handle)
    materials = table.get("materials") or {}
    aliases = table.get("aliases") or {}
    canonical_all = sorted({v.get("canonical") or k for k, v in materials.items()})
    used = (ctx.shared or {}).get("materials_used")
    if used:
        canonical = sorted(c for c in canonical_all if c in used)
        extra = sorted(
            c for c in used if c not in materials
        )  # names no table knows: placeholders below
    else:
        canonical, extra = canonical_all, []
    progress.log(
        f"materials: {len(materials)} named, {len(canonical_all)} canonical, {len(aliases)} aliases; "
        f"this build binds {len(canonical)}{' (all)' if not used else ''}; texture caps "
        + ", ".join(f"{k} {v or 'full'}" for k, v in sorted(caps.items()))
    )

    # -- texture jobs -----------------------------------------------------------------------
    progress.begin("materials: textures", total=None, unit="textures")
    jobs = {}  # (src, kind, keep_alpha) -> None
    # The slot walk lives in `beamng.materials` because the cost meter runs the same one to SIZE
    # what this stage EXPORTS (`pipeline/estimate`); two copies would be two answers to "which
    # textures does this level ship". No occlusion slot: the mask's G is not AO (audit §54).
    plans = textureplan.plan_canonical(ctx.bundle, ctx.derived_dir, table, canonical)
    for plan in plans.values():
        for job in plan.values():
            jobs.setdefault(job, None)
    # paint variants ride the same export: same plan with the bake's tinted albedo swapped in for
    # the atlas one and the colour mask dropped (the tint is already in the texels). Planned here
    # rather than after the export so their textures land in the orphan-keep set and keep their
    # mtime across builds - BeamNG's cooked cache is keyed on it.
    variant_plans = _variant_plans(ctx, materials, plans)
    for plan in variant_plans.values():
        for job in plan["maps"].values():
            jobs.setdefault(job, None)
    job_list = sorted(jobs)
    job_caps, lod_only = _lod_texture_caps(ctx, plans, caps)
    if lod_only:
        progress.log(
            f"materials: {lod_only} textures are bound only past LOD 0 and export at the level's "
            f"cap ({', '.join(f'LOD{k} {v}' for k, v in sorted(LOD_TEXTURE_CAPS.items()))})"
        )
    progress.set_total(len(job_list))
    # What is already on disk from the same source bytes at the same cap: the encoder is the
    # expensive half and `write_bytes_if_changed` only saved the WRITE, so every build re-encoded
    # all 5,684 textures to discover it had nothing to write (2m17s).
    ledger = load_ledger(ctx.level_dir)
    reusable, todo = partition(level, job_list, ledger, caps, use_dds, job_caps)
    if reusable:
        progress.advance(len(reusable))
        progress.log(
            f"materials: {len(reusable):,} textures unchanged since the last build (source bytes, cap "
            f"and format all the same) - not re-encoded"
        )
    exported = dict(reusable)
    texture_stats = {}
    exported.update(
        export_all(level, todo, caps, ctx.jobs, progress, use_dds, job_caps, stats=texture_stats)
    )
    encode_failed = int(texture_stats.get("encodeFailed", 0))
    missing = sum(1 for v in exported.values() if v is None) - encode_failed
    # art/textures/ is kept across builds so unchanged textures keep their mtime (BeamNG's cooked
    # cache is keyed on it); what this build did not export is an orphan and is swept at the END
    # of the stage, not here. **It used to be swept here**, before the area surfaces were
    # exported a few lines below — so every build deleted all 88 of them and the area block wrote
    # them straight back, which cost BeamNG a re-cook of those 88 on every first load. Found by
    # the no-op-rebuild A/B (A4): 34,965 files identical, 89 mtimes moved, all of them these.
    keep = {os.path.basename(v) for v in exported.values() if v}
    progress.end(
        f"{len(job_list)} textures exported ({len(reusable)} reused, {missing} sources missing, "
        f"{encode_failed} failed to encode, {texture_stats.get('normalsUnpacked', 0)} packed "
        f"normals rebuilt)"
    )
    if encode_failed:
        # a material shipped without its map renders as BeamNG's default and nothing in the
        # level says why; the log names each one
        raise RuntimeError(
            f"{encode_failed} texture(s) failed to encode - see the log lines marked ENCODE FAILED"
        )

    # -- material objects -------------------------------------------------------------------
    progress.begin("materials: write", total=len(canonical), unit="materials")
    out = {}
    for name in canonical:
        rec = materials.get(name) or {}
        maps = {slot: exported.get(job) for slot, job in plans[name].items()}
        bound = matbind.mapto(level.name, name)
        out[bound] = bm.material_v15(level, bound, rec, maps, pid_key=f"material:{name}")
        progress.advance(1)
    # areas
    area_count = 0
    area_exported = {}  # (src, kind, keep_alpha) -> game path
    areas_path = os.path.join(ctx.derived_dir, "areas", "materials.json")
    if os.path.isfile(areas_path):
        with open(areas_path, encoding="utf-8") as handle:
            area_doc = json.load(handle)
        cache = {}
        for raw, rec in (area_doc.get("materials") or {}).items():
            bound = matbind.mapto(level.name, raw)
            maps = {}
            tex = rec.get("texture")
            keep_alpha = False
            if tex and os.path.isfile(os.path.join(ctx.bundle, tex)):
                keep_alpha = _has_alpha(os.path.join(ctx.bundle, tex))
                maps["baseColor"] = _area_texture(
                    level, ctx, tex, "color", caps, cache, use_dds, ledger, area_exported
                )
                if keep_alpha:
                    maps["opacity"] = _area_texture(
                        level, ctx, tex, "opacity", caps, cache, use_dds, ledger, area_exported
                    )
            if rec.get("normal_map") and os.path.isfile(
                os.path.join(ctx.bundle, rec["normal_map"])
            ):
                maps["normal"] = _area_texture(
                    level,
                    ctx,
                    rec["normal_map"],
                    "normal",
                    caps,
                    cache,
                    use_dds,
                    ledger,
                    area_exported,
                    texture_stats,
                )
            arec = {
                "alphaMode": "mask" if keep_alpha else "opaque",
                "alphaCutoff": 0.5,
                "doubleSided": False,
                "roughness": rec.get("roughness"),
                "metallic": rec.get("metallic"),
                "baseColorFactor": rec.get("color") or [1, 1, 1, 1],
            }
            if rec.get("unresolved") or not maps.get("baseColor"):
                out[bound] = bm.placeholder_material(level, bound, (0.55, 0.55, 0.55, 1.0))
            else:
                out[bound] = bm.material_v15(level, bound, arec, maps, pid_key=f"material:{raw}")
            area_count += 1
    variant_count = 0
    variant_kinds = {"texture": 0, "factor": 0}
    for bound, plan in sorted(variant_plans.items()):
        maps = {slot: exported.get(job) for slot, job in plan["maps"].items()}
        out[bound] = bm.material_v15(level, bound, plan["rec"], maps, pid_key=plan["pid"])
        variant_kinds[plan.get("kind", "texture")] = (
            variant_kinds.get(plan.get("kind", "texture"), 0) + 1
        )
        variant_count += 1
    if variant_count:
        progress.log(
            f"materials: {variant_count} paint-variant materials, {variant_kinds['texture']} with a "
            f"tinted albedo and {variant_kinds['factor']} as a baseColorFactor on the canonical one "
            f"(palette unbound in both)"
        )

    for raw in extra:
        if not raw.startswith("area:"):
            bound = matbind.mapto(level.name, raw)
            out.setdefault(bound, bm.placeholder_material(level, bound))
    out[matbind.mapto(level.name, "unmapped")] = bm.placeholder_material(
        level, matbind.mapto(level.name, "unmapped")
    )
    level.write_materials("cs2", out)
    # NOW the orphan sweep: `keep` is everything this build exported, the area surfaces included.
    exported.update(area_exported)
    save_ledger(ctx.level_dir, exported, caps, use_dds, job_caps)
    keep |= {os.path.basename(v) for v in area_exported.values() if v}
    orphans = _sweep_orphans(level, keep)
    progress.end(
        f"{len(out)} materials written ({area_count} area surfaces, {orphans} orphan textures removed)"
    )
    shared = ctx.shared if ctx.shared is not None else {}
    shared["materials"] = {"aliases": aliases, "canonical": set(canonical_all), "known": set(out)}
    return {
        "named": len(materials),
        "canonical": len(canonical),
        "areaMaterials": area_count,
        "paintVariantMaterials": variant_count,
        "paintVariantKinds": variant_kinds,
        "textures": len(job_list),
        "texturesReused": len(reusable),
        "texturesMissing": missing,
        # a packed (AG / RG) normal the level rebuilt itself: zero on a bundle whose producer
        # and bake did their part; non-zero names a rip to look at (B2)
        "normalsUnpacked": int(texture_stats.get("normalsUnpacked", 0)),
        "orphanTexturesRemoved": orphans,
        "textureCaps": caps,
        "texturesLodOnly": lod_only,
        "textureCapsLod": LOD_TEXTURE_CAPS,
        "textureBytes": _texture_bytes(level),
        "format": "dds" if use_dds else "png",
        "approximations": {
            "colorMaskChannelOverlap": {
                "what": "a colour-mask texel where TWO OR MORE of CS2's tint channels are "
                "non-zero is mixed by BeamNG where CS2 multiplies",
                "why": "CS2 applies three INDEPENDENT lerps (bundle §7.2) and BeamNG's "
                "`getColorPalette` normalises the mask RGB into a simplex, so it can "
                "only ever BLEND between the three palette colours. Where one channel "
                "is active the conversion in `beamng.materials._colormask_rgba` is "
                "EXACT (worst error 0.001, which is the alpha's own 8-bit step); "
                "where two are, no alpha can reproduce a product with a mix. §7.2 "
                "states the channels are authored disjoint and MEASUREMENT AGREES: "
                "over 254 masks and 521.7 M texels, 39.0 % carry any tint at all and "
                "only 1.76 % of THOSE have two channels (median mask 0.29 %, 32 masks "
                "none at all)",
                "count": None,
                "value": 0.0176,
            },
        },
        "seconds": round(time.monotonic() - started, 1),
    }


# THE OCCLUSION SLOT IS GONE, and it is not coming back (audit §54, 2026-08-17).
# What stood here was `AO_MIN_MEAN = 96.0` and a per-texture gate: bind the §7.1 mask's G channel
# as `ambientOcclusionMap` only when it "reads like AO — mostly light", because binding it
# unconditionally painted every retaining wall and tunnel interior pitch black in shadow (loads
# #9-#14, "the tunnels are closed off"). That was a heuristic standing in for a rule, and the rule
# is now measured over all 890 masks of the reference city: G means 9/255 with 91 % of texels
# exactly zero, and of the 18 masks that cleared the gate NINE ARE UNIFORMLY WHITE (a no-op) and
# NINE ARE BIMODAL 0/255 SELECTORS (a stencil, not a gradient); only 2 of 890 (0.22 %) read as AO
# on every clause, and both are near-white. So the gate admitted almost only the cases where
# binding changed nothing, and rejected the cases where it would have been wrong — it did no useful
# work, and it read every mask off disk to decide that.
# The bake stopped splitting the channel; BeamNG's own default AO is 1, which is the right answer.


# the LOD caps live with the texture plan (`beamng.textureplan.LOD_TEXTURE_CAPS`): the cost
# meter sizes by the same rule before a shape exists
LOD_TEXTURE_CAPS = textureplan.LOD_TEXTURE_CAPS


def _lod_texture_caps(ctx, plans, caps):
    """`({job: cap}, count)`: a tighter cap for every texture job bound only past LOD 0, at the
    NEAREST level any material binding it is seen — LOD 0 anywhere wins, and the cap never
    exceeds the kind's own."""
    levels = (ctx.shared or {}).get("materials_lod_level") or {}
    nearest = {}
    for name, plan in plans.items():
        level = int(levels.get(name, 0))
        for job in plan.values():
            nearest[job] = min(nearest.get(job, level), level)
    job_caps = {}
    for job, level in nearest.items():
        if level <= 0:
            continue
        kind_cap = int(caps.get(job[1], 0) or 0)
        cap = textureplan.lod_cap(level, kind_cap)
        if cap != kind_cap:
            job_caps[job] = cap
    return job_caps, len(job_caps)


def _variant_plans(ctx, materials, plans):
    """`{boundName: {"rec", "maps", "pid"}}` for every paint variant the shapes stage bound.

    A variant is the canonical material with the bake's tinted albedo in place of the atlas one
    and NO `colorPaletteMap`: the tint is already in the texels, so leaving the palette bound
    would apply §7.2 a second time.
    """
    used = (ctx.shared or {}).get("materials_variants_used") or {}
    if not used:
        return {}
    by_canonical = {}
    for raw, rec in materials.items():
        by_canonical.setdefault(rec.get("canonical") or raw, []).append(raw)
    out = {}
    for vkey, canonicals in used.items():
        for canon in canonicals:
            rel = factor = source = None
            for raw in by_canonical.get(canon, [canon]):
                derived = (materials.get(raw) or {}).get("derived") or {}
                rel = (derived.get("variants") or {}).get(vkey)
                entry = (derived.get("variantFactors") or {}).get(vkey)
                if rel or entry:
                    source = raw
                    factor = (entry or {}).get("factor")
                    break
            path = os.path.join(ctx.derived_dir, rel) if rel else None
            rec = dict(materials.get(source) or {})
            rec.pop("colorMask", None)
            rec["tintable"] = False
            maps = {
                slot: job
                for slot, job in (plans.get(canon) or {}).items()
                if slot not in ("colorMask", "baseColor", "opacity")
            }
            if path and os.path.isfile(path):
                # the exact tint, in the texels
                maps["baseColor"] = (path, "color", False)
                if (rec.get("alphaMode") or "opaque") != "opaque":
                    maps["opacity"] = (path, "opacity", False)
            elif factor:
                # the same tint as ONE MULTIPLY on the canonical albedo — zero textures. The bake
                # only offers this where it measured the per-texel error under its threshold.
                for slot in ("baseColor", "opacity"):
                    job = (plans.get(canon) or {}).get(slot)
                    if job:
                        maps[slot] = job
                base = list(rec.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0])
                base = (base + [1.0] * 4)[:4]
                rec["baseColorFactor"] = [float(base[i]) * float(factor[i]) for i in range(3)] + [
                    float(base[3])
                ]
            else:
                continue  # nothing for this pair; the shape binds the plain name
            bound = matbind.mapto(ctx.settings["level_name"], f"{canon}__v{vkey}")
            out[bound] = {
                "rec": rec,
                "maps": maps,
                "pid": f"material:{canon}__v{vkey}",
                "kind": "texture" if (path and os.path.isfile(path)) else "factor",
            }
    return out


def _texture_bytes(level):
    """What `art/textures/` weighs — the number A4 exists to move (5.72 GiB against
    `west_coast_usa`'s 0.58), so the build reports it rather than leaving it to be discovered
    on an 8 GB card."""
    total = files = 0
    directory = level.path(*textureplan.TEXTURE_SUBDIR)
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            try:
                total += os.path.getsize(os.path.join(directory, name))
                files += 1
            except OSError:
                pass
    return {"files": files, "bytes": total, "mib": round(total / (1 << 20), 1)}


def _has_alpha(path):
    try:
        arr = png.rgba_array(path)
    except ValueError:
        return False
    return bool((arr[::8, ::8, 3] < 250).any())


def _area_texture(level, ctx, rel, kind, caps, cache, use_dds, ledger, exported, stats=None):
    """One §8b area-surface texture, through the same ledger the pooled export uses.

    The area surfaces sat outside it: 88 textures BC7-encoded on every build even when nothing
    about them had changed, and — until the sweep moved to the end of the stage — deleted and
    rewritten each time, so BeamNG re-cooked them on every first load.
    """
    src = os.path.join(ctx.bundle, rel)
    job = (src, kind, False)
    if job in exported:
        return exported[job]
    cap = int(caps.get(kind, 0) or 0)
    entry = ledger.get(ledger_key(src, kind, False, int(caps.get(kind, 0) or 0), use_dds))
    stamp = file_stamp(src)
    name = textureplan.texture_name(src, kind, False, use_dds)
    if (
        entry
        and stamp is not None
        and entry.get("stamp") == stamp
        and os.path.isfile(level.path(*textureplan.TEXTURE_SUBDIR, name))
    ):
        exported[job] = entry["game"]
        return entry["game"]
    game = textureplan.export_texture(level, src, kind, cap, False, cache, dds=use_dds, stats=stats)
    exported[job] = game
    return game


def _sweep_orphans(level, keep):
    """Remove every file in `art/textures/` this build did not export. Returns the count.

    The folder is deliberately NOT cleared at build start (an unchanged texture must keep its
    mtime so BeamNG keeps its cooked copy), so this is what stops a texture from an older build
    lingering — and it has to run after the LAST export, not in the middle.
    """
    directory = level.path(*textureplan.TEXTURE_SUBDIR)
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if name in keep:
            continue
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
    return removed


# `after` is load-bearing: this stage exports only the textures the shape and tile stages
# actually bound (`ctx.shared["materials_used"]`), and that set appears in none of its own
# inputs. Without the declaration a `paint_kinds` change would re-run `shapes` and leave the
# materials of the previous build in place - the exact shape of the 2026-08-18 miss.
INPUTS = (
    "derived:materials.json",
    "derived:areas/materials.json",
    "derived:textures",
    "layer:textures",
)
SETTINGS = ("texture_max", "texture_max_normal", "texture_max_data", "texture_dds")
OUTPUTS = ("art/cs2/main.materials.json", "art/textures")

# version 2: per-kind texture caps (A4 2.1)
# version 4: emissive materials carry `emissiveIntensityNits`, without which the map
#            takes an exposure-relative legacy path and the city never lights up
# version 5: CS2's colour mask is converted into BeamNG's palette semantics and
#            `paletteBaseColor` is set, so the tint is applied at all and an untinted texel is
#            not black (A6.3) - the changelog line said "version 2" and the literal stayed 4, so
#            a rebuild on an unchanged bundle reported `fresh, skipped` and shipped none of it;
#            AND the conversion itself was written into `texture_name` rather than
#            `export_texture`, so every colormask export raised NameError and the level carried
#            zero `colorPaletteMap` (fixed 2026-08-19 with A6.9)
# version 6: an emissive material takes the colour and nits of its mesh's own CS2 colour slot
#            where the bake could join one, instead of white at a flat 200 (A6.9/D5)
# version 7: a joined slot of ZERO nits is honoured rather than falling back to the flat 200 -
#            115 slots say zero and every one is the UNLIT face of a billboard (A6.9/D5)
# version 9: the bake's rebuilt normal (`derived.normalRgb`) reaches the plan (B1); a packed
# normal met at export is rebuilt to RGB (B2); the texture ledger keys on the pipeline version
# (B3); an encode failure fails the stage instead of counting as a missing source (B10)
# version 8: A8/P1 - the colour mask takes the colour cap like every other map. `colormask` was
#            missing from `KIND_CAP_SETTING`, which means UNCAPPED, so 767 of 771 masks shipped
#            at 1024 against everything else's 256: 1,025 MiB, 58 % of all texture bytes
STAGE = Stage(
    STAGE_KEY,
    "Materials and textures",
    enabled,
    units,
    run,
    inputs=INPUTS,
    settings_keys=SETTINGS,
    outputs=OUTPUTS,
    shared_keys=("materials",),
    after=("shapes", "networks", "derived"),
    version=9,
)
