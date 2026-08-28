"""Every material a shape binds is defined exactly once, in a file BeamNG will read.

Three failures this closes, each seen in a real load: a texture path under `art/` that is not
there; a name defined TWICE, because BeamNG mounts every loose `*.materials.json` under `levels/`
and the compiler's auto-generated placeholder shadows the real one; and a plain `Material`
sharing a file with `TerrainMaterial` entries, which no vanilla level does and which left our cut
meshes drawing as flat grey while this validator called them defined.
"""

import json
import os
import re


def check_materials(ctx):
    """Material files parse, their textures exist, names are unique, and bindings resolve."""
    # material texture paths under art/
    art = os.path.join(ctx.level_dir, "art")
    material_files = 0
    if os.path.isdir(art):
        for root, _d, files in os.walk(art):
            for name in files:
                if name.endswith(".materials.json"):
                    material_files += 1
                    with open(os.path.join(root, name), encoding="utf-8") as handle:
                        try:
                            doc = json.load(handle)
                        except ValueError:
                            ctx.note(f"{name}: not JSON")
                            continue
                    for mat_name, mat in (doc or {}).items():
                        if not isinstance(mat, dict):
                            continue
                        for key, value in mat.items():
                            if key.endswith(("Tex", "Map")) and isinstance(value, str) and value:
                                path = ctx.resolve(value)
                                if path is not None and not os.path.isfile(path):
                                    ctx.missing.append(f"material {mat_name}: {key} {value}")
    ctx.checks["materialFiles"] = material_files
    ctx.checks["missingFiles"] = len(ctx.missing)
    # every material a .dae binds must be a mapTo somewhere in the level (else BeamNG's orange checker);
    # and no name may be defined TWICE — BeamNG mounts every loose *.materials.json under levels/, so a
    # second definition (the headless compiler's auto-generated `diffuseColor 0.8` placeholder beside
    # a shape, load #10) shadows the real one. We write materials under art/cs2/ and art/terrains/
    # only: a materials file under art/shapes/ is never ours.
    mapto = set()
    defined_in = {}  # lower-case name -> [relative file, ...]
    stray = []
    mixed_class_files = []
    shapes_root = os.path.join(art, "shapes")
    if os.path.isdir(art):
        for root, _d, files in os.walk(art):
            for name in files:
                if name.endswith(".materials.json"):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, ctx.level_dir).replace("\\", "/")
                    if os.path.commonpath(
                        [os.path.abspath(full), os.path.abspath(shapes_root)]
                    ) == os.path.abspath(shapes_root):
                        stray.append(rel)
                    try:
                        with open(full, encoding="utf-8") as handle:
                            doc = json.load(handle)
                    except ValueError:
                        continue
                    # A plain `Material` must NOT share a file with `TerrainMaterial` entries.
                    # Measured across every shipped vanilla level: art/terrains/*.materials.json
                    # holds 191 TerrainMaterial and ZERO plain Material entries, and not one
                    # vanilla file mixes the classes. We shipped 7 ground mesh materials in there
                    # for three loads; they were DEFINED, so this validator passed, and the cut
                    # meshes still drew as flat untextured grey. Defined is not the same as
                    # applied — that is the blind spot this check closes.
                    classes = {
                        str(m.get("class")) for m in (doc or {}).values() if isinstance(m, dict)
                    }
                    if "TerrainMaterial" in classes and "Material" in classes:
                        mixed_class_files.append(rel)
                    for key, mat in (doc or {}).items():
                        if not isinstance(mat, dict):
                            continue
                        if mat.get("mapTo"):
                            mapto.add(str(mat["mapTo"]).lower())
                        for label in {str(mat.get("name") or key), str(mat.get("mapTo") or "")}:
                            if label:
                                defined_in.setdefault(label.lower(), []).append(rel)
    duplicates = {n: sorted(set(f)) for n, f in defined_in.items() if len(set(f)) > 1}
    ctx.checks["strayMaterialFiles"] = len(stray)
    ctx.checks["mixedClassMaterialFiles"] = len(mixed_class_files)
    for rel in mixed_class_files[:10]:
        ctx.note(
            f"{rel}: plain 'Material' entries share a file with 'TerrainMaterial' entries - no vanilla "
            "level does this (191 TerrainMaterial / 0 Material across the shipped levels); put the "
            "plain materials in their own art/<dir>/main.materials.json"
        )
    ctx.checks["duplicateMaterials"] = len(duplicates)
    for rel in stray[:10]:
        ctx.note(
            f"{rel}: a materials file under art/shapes/ - BeamNG's compiler/importer auto-generated it "
            "(opaque placeholders that shadow the level's materials); the cook stage removes these"
        )
    for n, files in list(duplicates.items())[:10]:
        ctx.note(
            f"material {n!r} is defined in {len(files)} files ({', '.join(files)}): one shadows the other"
        )
    ctx.unbound = {}
    mat_re = re.compile(r'<material id="[^"]*" name="([^"]+)"')
    for name, path in ctx.dae_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                head = handle.read(262144)
        except OSError:
            continue
        for sym in mat_re.findall(head):
            if sym.lower() not in mapto:
                ctx.unbound.setdefault(sym, name)
    for cover in ctx.by_class.get("GroundCover", []):
        if str(cover.get("material") or "").lower() not in mapto:
            ctx.unbound.setdefault(str(cover.get("material")), cover.get("name"))
    ctx.checks["unboundMaterials"] = len(ctx.unbound)
