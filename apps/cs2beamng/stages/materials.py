"""Stage `materials` — the bake's resolved material table → `art/cs2/main.materials.json` +
`art/textures/*.png` (capped, suffixed), plus the area-surface materials.

* Every CANONICAL material (the bake's alias map collapses identical texture sets: 4,641 → 1,409
  on the reference city) becomes one v1.5 material named `mapto(level, canonical)`; the shapes
  and tiles bind that name (they map raw names through the same alias table), so BeamNG sees
  ~1.4k materials, not 4.6k.
* Textures are exported once per (file, size cap) on a process pool: colour maps as sRGB
  (`.color.png`, alpha kept only for mask/blend materials), normals `.normal.png`, the bake's
  split roughness/metallic/AO and the colour mask as linear `.data.png`.
* `area:<prefab>` materials come from `derived/areas/materials.json` (`areas.surface_material`):
  the theme texture + normal, `diffuseColor` tint, scalar roughness/metallic; a texture with a
  real alpha channel gets `alphaTest`.
* What no table resolves gets a grey placeholder so a `.dae` never binds a name BeamNG cannot
  find (that renders as the orange "no material" checker).

The stage records `ctx.shared["materials"] = {"aliases", "canonical", "known"}` for the
shape/tile stages: `mapto_name(shared, level, raw)` is the one function that turns a bundle
material name into the bound name.
"""

import json
import multiprocessing
import os
import time

from csiibundle import png

from ..beamng import materials as bm
from ..beamng.level import Level
from . import Stage, level_of

STAGE_KEY = "materials"


def enabled(settings):
    return True


def units(census, settings):
    counts = ((census or {}).get("bundle") or {}).get("counts") or {}
    return int(counts.get("materials") or 1)


def mapto_name(shared, level, raw):
    """Bound material name for a bundle material name (alias → canonical → prefixed), and a
    record that this build binds it (the materials stage exports only what was bound)."""
    info = _aliases(shared, level)
    canonical = info["aliases"].get(raw, raw)
    if shared is not None:
        shared.setdefault("materials_used", set()).add(canonical)
    return bm.mapto(level.name, canonical)


def _aliases(shared, level):
    """The bake's alias table, loaded once per build (before the materials stage runs)."""
    if shared is not None and "materials_aliases" in shared:
        return shared["materials_aliases"]
    aliases = {}
    derived_dir = getattr(level, "derived_dir", None)
    if derived_dir:
        path = os.path.join(derived_dir, "materials.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    aliases = json.load(handle).get("aliases") or {}
            except (OSError, ValueError):
                aliases = {}
    info = {"aliases": aliases}
    if shared is not None:
        shared["materials_aliases"] = info
    return info


def run(ctx, progress):
    started = time.monotonic()
    level = level_of(ctx)
    settings = ctx.settings
    max_size = int(settings.get("texture_max") or 0)
    table_path = os.path.join(ctx.derived_dir, "materials.json")
    if not os.path.isfile(table_path):
        raise RuntimeError("derived/materials.json missing: the bake's materials product has not run")
    with open(table_path, encoding="utf-8") as handle:
        table = json.load(handle)
    materials = table.get("materials") or {}
    aliases = table.get("aliases") or {}
    rebuilt = table.get("normalsRebuilt") or {}
    canonical_all = sorted({v.get("canonical") or k for k, v in materials.items()})
    used = (ctx.shared or {}).get("materials_used")
    if used:
        canonical = sorted(c for c in canonical_all if c in used)
        extra = sorted(c for c in used if c not in materials)      # names no table knows: placeholders below
    else:
        canonical, extra = canonical_all, []
    progress.log(f"materials: {len(materials)} named, {len(canonical_all)} canonical, {len(aliases)} aliases; "
                 f"this build binds {len(canonical)}{' (all)' if not used else ''}; texture cap {max_size or 'full'}")

    # -- texture jobs -----------------------------------------------------------------------
    progress.begin("materials: textures", total=None, unit="textures")
    jobs = {}                                          # (src, kind, keep_alpha) -> None
    plans = {}                                         # canonical -> {slot: (src, kind, keep_alpha)}
    for name in canonical:
        rec = materials.get(name) or {}
        mode = rec.get("alphaMode") or "opaque"
        plan = {}
        if rec.get("baseColor"):
            plan["baseColor"] = (os.path.join(ctx.bundle, rec["baseColor"]), "color", False)
            if mode != "opaque":
                plan["opacity"] = (os.path.join(ctx.bundle, rec["baseColor"]), "opacity", False)
        normal_rel = rebuilt.get(name) or rec.get("normal")
        if normal_rel:
            base = ctx.derived_dir if normal_rel.endswith("_normal_rgb.png") else ctx.bundle
            plan["normal"] = (os.path.join(base, normal_rel), "normal", False)
        for slot in ("roughness", "metallic", "ao"):
            rel = (rec.get("derived") or {}).get(slot)
            if rel:
                plan[slot] = (os.path.join(ctx.derived_dir, rel), "data", False)
        if rec.get("emissive"):
            plan["emissive"] = (os.path.join(ctx.bundle, rec["emissive"]), "color", False)
        if rec.get("colorMask") and rec.get("tintable"):
            plan["colorMask"] = (os.path.join(ctx.bundle, rec["colorMask"]), "data", False)
        plans[name] = plan
        for job in plan.values():
            jobs.setdefault(job, None)
    job_list = sorted(jobs)
    progress.set_total(len(job_list))
    exported = _export_all(level, job_list, max_size, ctx.jobs, progress)
    missing = sum(1 for v in exported.values() if v is None)
    progress.end(f"{len(job_list)} textures exported ({missing} sources missing)")

    # -- material objects -------------------------------------------------------------------
    progress.begin("materials: write", total=len(canonical), unit="materials")
    out = {}
    for name in canonical:
        rec = materials.get(name) or {}
        maps = {slot: exported.get(job) for slot, job in plans[name].items()}
        bound = bm.mapto(level.name, name)
        out[bound] = bm.material_v15(level, bound, rec, maps, pid_key=f"material:{name}")
        progress.advance(1)
    # areas
    area_count = 0
    areas_path = os.path.join(ctx.derived_dir, "areas", "materials.json")
    if os.path.isfile(areas_path):
        with open(areas_path, encoding="utf-8") as handle:
            area_doc = json.load(handle)
        cache = {}
        for raw, rec in (area_doc.get("materials") or {}).items():
            bound = bm.mapto(level.name, raw)
            maps = {}
            tex = rec.get("texture")
            keep_alpha = False
            if tex and os.path.isfile(os.path.join(ctx.bundle, tex)):
                keep_alpha = _has_alpha(os.path.join(ctx.bundle, tex))
                maps["baseColor"] = bm.export_texture(level, os.path.join(ctx.bundle, tex), "color", max_size, False, cache)
                if keep_alpha:
                    maps["opacity"] = bm.export_texture(level, os.path.join(ctx.bundle, tex), "opacity", max_size, False, cache)
            if rec.get("normal_map") and os.path.isfile(os.path.join(ctx.bundle, rec["normal_map"])):
                maps["normal"] = bm.export_texture(level, os.path.join(ctx.bundle, rec["normal_map"]), "normal", max_size, False, cache)
            arec = {"alphaMode": "mask" if keep_alpha else "opaque", "alphaCutoff": 0.5, "doubleSided": False,
                    "roughness": rec.get("roughness"), "metallic": rec.get("metallic"),
                    "baseColorFactor": rec.get("color") or [1, 1, 1, 1]}
            if rec.get("unresolved") or not maps.get("baseColor"):
                out[bound] = bm.placeholder_material(level, bound, (0.55, 0.55, 0.55, 1.0))
            else:
                out[bound] = bm.material_v15(level, bound, arec, maps, pid_key=f"material:{raw}")
            area_count += 1
    for raw in extra:
        if not raw.startswith("area:"):
            bound = bm.mapto(level.name, raw)
            out.setdefault(bound, bm.placeholder_material(level, bound))
    out[bm.mapto(level.name, "unmapped")] = bm.placeholder_material(level, bm.mapto(level.name, "unmapped"))
    level.write_materials("cs2", out)
    progress.end(f"{len(out)} materials written ({area_count} area surfaces)")
    shared = ctx.shared if ctx.shared is not None else {}
    shared["materials"] = {"aliases": aliases, "canonical": set(canonical_all), "known": set(out)}
    return {"named": len(materials), "canonical": len(canonical), "areaMaterials": area_count,
            "textures": len(job_list), "texturesMissing": missing, "seconds": round(time.monotonic() - started, 1)}


def _has_alpha(path):
    try:
        arr = png.rgba_array(path)
    except ValueError:
        return False
    return bool((arr[::8, ::8, 3] < 250).any())


# -- pool ---------------------------------------------------------------------------------------

def _export_chunk(args):
    level_dir, level_name, max_size, chunk = args
    level = Level(level_dir, level_name, level_name)
    cache = {}
    out = []
    for src, kind, keep_alpha in chunk:
        try:
            out.append(((src, kind, keep_alpha), bm.export_texture(level, src, kind, max_size, keep_alpha, cache)))
        except Exception as exc:  # noqa: BLE001 - one bad texture must not stop the stage
            out.append(((src, kind, keep_alpha), None))
            out.append((("__log__", str(src), ""), f"texture {os.path.basename(src)}: {exc!r}"))
    return out


def _export_all(level, jobs, max_size, workers, progress):
    exported = {}
    if not jobs:
        return exported
    chunk_size = 16
    chunks = [jobs[i:i + chunk_size] for i in range(0, len(jobs), chunk_size)]
    args = [(level.dir, level.name, max_size, c) for c in chunks]
    if workers and workers > 1 and len(chunks) > 1:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(min(workers, len(chunks))) as pool:
            for results in pool.imap_unordered(_export_chunk, args):
                _absorb(results, exported, progress)
                progress.check()
    else:
        for a in args:
            _absorb(_export_chunk(a), exported, progress)
            progress.check()
    return exported


def _absorb(results, exported, progress):
    for key, value in results:
        if key[0] == "__log__":
            progress.log(value)
            continue
        exported[key] = value
        progress.advance(1)


STAGE = Stage(STAGE_KEY, "Materials and textures", enabled, units, run)
