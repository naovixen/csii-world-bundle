"""The joined passes — materials to textures, instances to meshes, networks, §5B lane geometry,
and the unreferenced-file sweeps. Everything here fills or reads the `Context` blackboard, in
the order `validate_bundle.py` runs it, because later passes join against earlier ones."""

from __future__ import annotations

import json
import re
from typing import Any

from .glbcheck import check_mesh, check_quat, check_texture
from .report import Context, load_declared_missing, load_json_layer, load_jsonl

# A record id is unique per logical object; the one sanctioned exception is §4.1's stack
# expansion, where every section record of one stacked instance shares that instance's id.
STACK_SECTION = re.compile(r"__stack_(first|mid|last|rest)$")


def check_materials(ctx: Context) -> None:
    """materials.json: schema, and every texture slot resolves to a hash-named PNG."""
    report = ctx.report
    ctx.materials = {}
    ctx.referenced_textures = set()
    materials_path = ctx.layer_path("materials")
    ctx.have_materials = materials_path is not None
    if not (materials_path and materials_path.is_file()):
        return
    ctx.materials = json.loads(materials_path.read_text(encoding="utf-8"))
    for err in ctx.schemas["materials"].validate(ctx.materials):
        report.error(f"materials.json: {err}")
    for mat_id, material in ctx.materials.items():
        if not isinstance(material, dict):
            continue
        for slot in ("baseColor", "normal", "mask", "emissive", "colorMask"):
            rel = material.get(slot)
            if rel is None:
                continue
            tex_path = ctx.bundle_dir / rel
            if not tex_path.is_file():
                report.error(f"materials.json[{mat_id}].{slot}: {rel!r} does not exist")
                continue
            ctx.referenced_textures.add(tex_path.resolve())
            check_texture(tex_path, rel, f"materials.json[{mat_id}].{slot}", report)


def check_instances(ctx: Context) -> None:
    """instances.jsonl: schema, the §4.1 id rule, unit quaternions, and the mesh join."""
    report = ctx.report
    ctx.instances = []
    instances_path = ctx.layer_path("instances")
    if instances_path and instances_path.is_file():
        ctx.instances = load_jsonl(
            instances_path, ctx.schemas["instance"], ctx.layers["instances"], report
        )

    mesh_dir = ctx.layer_path("meshes")
    missing_meshes: dict[str, int] = {}
    for i, record in enumerate(ctx.instances):
        rid = record.get("id")
        if rid is not None:
            is_section = bool(STACK_SECTION.search(record.get("assetId", "")))
            if rid in ctx.instance_ids and not (ctx.instance_ids[rid] and is_section):
                report.error(
                    f"instances.jsonl: duplicate id {rid} "
                    "(only §4.1 stack-section records may share an id)"
                )
            ctx.instance_ids[rid] = ctx.instance_ids.get(rid, True) and is_section
        check_quat(record.get("rot"), f"instances.jsonl line {i + 1}", report)
        asset_id = record["assetId"]
        if mesh_dir is None:
            report.error(
                f"instances.jsonl line {i + 1}: assetId {asset_id!r} "
                "but no 'meshes' layer is declared"
            )
        elif not (mesh_dir / f"{asset_id}.glb").is_file():
            # Aggregated by assetId: one broken prefab can be referenced by tens of thousands of
            # instances, and a line-per-instance report buries every other finding.
            missing_meshes[asset_id] = missing_meshes.get(asset_id, 0) + 1

    _report_missing_meshes(ctx, missing_meshes)


def _report_missing_meshes(ctx: Context, missing_meshes: dict[str, int]) -> None:
    """A gap the producer DECLARED is not a validator warning (decided 2026-08-12, PILLARS-2).

    warnings.jsonl is the contract's own channel for "this asset could not be delivered" (§10),
    and re-reporting each one made the warning count a function of how honest the producer was:
    120 of the full city's 120 warnings were assets the bundle had already declared, which
    buried the classes that mean something. They are counted in one note instead, and only an
    UNDECLARED missing mesh is still an error. Nothing is hidden - `warnings.jsonl` is the
    authority and the note points at it.
    """
    report = ctx.report
    # A producer may declare, in warnings.jsonl, an asset it could not deliver (§10). That is
    # the honest alternative to dropping the instances, which would lose their placement for
    # nothing, so a declared gap is a warning rather than an error. An *undeclared* one stays
    # an error.
    declared_missing = load_declared_missing(ctx.bundle_dir)
    declared_gaps = 0
    declared_instances = 0
    for asset_id, count in sorted(missing_meshes.items(), key=lambda kv: -kv[1]):
        where = f"{ctx.layers['meshes']}{asset_id}.glb"
        plural = "instance" if count == 1 else "instances"
        if asset_id in declared_missing:
            declared_gaps += 1
            declared_instances += count
        else:
            report.error(f"assetId {asset_id!r} ({count} {plural}) has no mesh at {where}")

    if declared_gaps:
        reasons: dict[str, int] = {}
        for asset_id in missing_meshes:
            if asset_id in declared_missing:
                reason = declared_missing[asset_id]
                reasons[reason] = reasons.get(reason, 0) + 1
        breakdown = "; ".join(
            f"{n}x {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])
        )
        report.note(
            f"{declared_gaps} assetId(s) referenced by {declared_instances} instance(s) have no "
            f"mesh, every one of them declared in warnings.jsonl (§10): {breakdown}"
        )


def check_networks(ctx: Context) -> None:
    """networks.jsonl: schema, the parent join note, mesh references and node references."""
    report = ctx.report
    ctx.networks = []
    networks_path = ctx.layer_path("networks")
    if networks_path and networks_path.is_file():
        ctx.networks = load_jsonl(
            networks_path, ctx.schemas["network"], ctx.layers["networks"], report
        )
    ctx.net_ids = {r["id"] for r in ctx.networks if "id" in r}

    # `parent` resolves into instances.jsonl or networks.jsonl when the owner is exported
    # (street furniture is owned by road edges). Owners the bundle does not carry — utility-net
    # edges, net nodes — leave a parent that resolves to no record; §4 makes parent advisory
    # grouping, never a required join, so that is a warning, aggregated because one city has
    # tens of thousands of such owners.
    unresolved_parents: dict[int, int] = {}
    for record in ctx.instances:
        parent = record.get("parent")
        if parent is not None and parent not in ctx.instance_ids and parent not in ctx.net_ids:
            unresolved_parents[parent] = unresolved_parents.get(parent, 0) + 1
    if unresolved_parents:
        total = sum(unresolved_parents.values())
        samples = ", ".join(str(p) for p in list(unresolved_parents)[:5])
        report.warn(
            f"{total} instance record(s) carry a parent that resolves to no exported record "
            f"({len(unresolved_parents)} distinct owners, e.g. {samples}); §4: such owners "
            "(utility-net edges, net nodes) are not in the bundle and parent is advisory"
        )

    net_mesh_dir = ctx.layer_path("networkMeshes")
    for i, record in enumerate(ctx.networks):
        label = f"networks.jsonl line {i + 1}"
        if "rot" in record:
            check_quat(record["rot"], label, report)
        asset_id = record.get("assetId")
        if asset_id is not None:
            if net_mesh_dir is None:
                report.error(
                    f"{label}: assetId {asset_id!r} but no 'networkMeshes' layer is declared"
                )
            elif not (net_mesh_dir / f"{asset_id}.glb").is_file():
                report.error(
                    f"{label}: assetId {asset_id!r} has no mesh in {ctx.layers['networkMeshes']}"
                )
        for key in ("startNode", "endNode"):
            if key in record and record[key] not in ctx.net_ids:
                report.error(f"{label}: {key} {record[key]} matches no network record id")


def _check_lane_catalogue(ctx: Context, catalogue: dict[str, Any]) -> None:
    """Every part of every catalogue entry names an assetId with a mesh on disk."""
    report = ctx.report
    lane_mesh_dir = ctx.layer_path("laneMeshes")
    checked_parts: set = set()
    for entry_key, entry in catalogue.items():
        for j, part in enumerate(entry.get("parts", [])):
            asset_id = part.get("assetId")
            if not asset_id:
                report.error(f"laneprefabs.json[{entry_key}].parts[{j}]: no assetId")
            elif asset_id not in checked_parts:
                checked_parts.add(asset_id)
                if lane_mesh_dir is None:
                    report.error(
                        f"laneprefabs.json[{entry_key}].parts[{j}]: "
                        f"assetId {asset_id!r} but no 'laneMeshes' layer is declared"
                    )
                elif not (lane_mesh_dir / f"{asset_id}.glb").is_file():
                    report.error(
                        f"laneprefabs.json[{entry_key}].parts[{j}]: "
                        f"assetId {asset_id!r} has no mesh in {ctx.layers['laneMeshes']}"
                    )


def check_lane_geometry(ctx: Context) -> None:
    """lanemeshes.jsonl + laneprefabs.json (contract §5B v2).

    Declared by the Phase 7 manifest as layers.laneGeometry / lanePrefabs / laneMeshes:
    parameter records against their schema, the prefab join against the catalogue, and every
    part's assetId against the lane-mesh directory.
    """
    report = ctx.report
    lanes: list[dict[str, Any]] = []
    lanes_path = ctx.layer_path("laneGeometry")
    catalogue_path = ctx.layer_path("lanePrefabs")
    if lanes_path and lanes_path.is_file():
        lanes = load_jsonl(lanes_path, ctx.schemas["lanemesh"], ctx.layers["laneGeometry"], report)

        catalogue: dict[str, Any] = {}
        if catalogue_path and catalogue_path.is_file():
            catalogue = (
                load_json_layer(
                    catalogue_path, ctx.schemas["laneprefabs"], ctx.layers["lanePrefabs"], report
                )
                or {}
            )
        elif lanes:
            report.error("layers.laneGeometry is declared but no lanePrefabs catalogue resolves")

        _check_lane_catalogue(ctx, catalogue)

        for i, record in enumerate(lanes):
            label = f"{ctx.layers['laneGeometry']} line {i + 1}"
            entry = catalogue.get(record.get("prefab"))
            if entry is None:
                report.error(
                    f"{label}: prefab {record.get('prefab')!r} is not in the lanePrefabs catalogue"
                )
                continue
            part_count = len(entry.get("parts", []))
            for index in record.get("parts", []):
                if not 0 <= index < part_count:
                    report.error(
                        f"{label}: part index {index} out of range "
                        f"(catalogue entry has {part_count})"
                    )
    ctx.counts["laneGeometry"] = len(lanes)


def scan_meshes(ctx: Context) -> None:
    """Walk every .glb: container validity, material joins, image references, sub-mesh names."""
    mesh_dir = ctx.layer_path("meshes")
    net_mesh_dir = ctx.layer_path("networkMeshes")
    lane_mesh_dir = ctx.layer_path("laneMeshes")
    ctx.mesh_files = sorted(mesh_dir.glob("*.glb")) if mesh_dir and mesh_dir.is_dir() else []
    net_files = sorted(net_mesh_dir.glob("*.glb")) if net_mesh_dir and net_mesh_dir.is_dir() else []
    lane_files = (
        sorted(lane_mesh_dir.glob("*.glb")) if lane_mesh_dir and lane_mesh_dir.is_dir() else []
    )
    for path in ctx.mesh_files:
        check_mesh(
            path,
            ctx.bundle_dir,
            ctx.materials,
            ctx.have_materials,
            ctx.referenced_textures,
            ctx.report,
            ctx.mesh_names,
        )
    for path in net_files + lane_files:
        check_mesh(
            path,
            ctx.bundle_dir,
            ctx.materials,
            ctx.have_materials,
            ctx.referenced_textures,
            ctx.report,
        )


def check_unreferenced(ctx: Context) -> None:
    """Warn on meshes no instance references and textures no material or mesh references."""
    report = ctx.report
    used_assets = {r["assetId"] for r in ctx.instances}

    # Prefabs the city places as §4.1 stack expansions. Every placement of one references a
    # section (X__stack_first/mid/last/rest) and NEVER the bare prefab, so the bare mesh, its
    # skirt, and any section this city's placements happened not to need are all unreferenced
    # by construction. `Vertical_Pillar` is the type case: 1,296 first + 6,706 mid records and
    # not one reference to `Vertical_Pillar` itself.
    stacked = {STACK_SECTION.sub("", a) for a in used_assets if STACK_SECTION.search(a)}

    def references(stem: str) -> bool:
        return (stem in used_assets) or (stem in stacked)

    for path in ctx.mesh_files:
        if references(path.stem):
            continue

        # An unplaced section of a placed stack: the producer writes every section the prefab
        # has, and which ones a given city needs depends on how tall its pillars are.
        if STACK_SECTION.sub("", path.stem) in stacked:
            continue

        # Derived files are referenced implicitly, never by an assetId of their own: a
        # foundation skirt (__base) is placed by the consumer's terrain skirting, and a state
        # variant (__teen, __elderly, __dead, __stump, __partial1/2, __full — contract §6.1) is
        # swapped in from the instance's own attrs.treeState / attrs.fullness. The first
        # full-city rip with skirts drowned the report in 1,380 of these; variants added 225
        # more.
        derived = (
            "__base",
            "__teen",
            "__elderly",
            "__dead",
            "__stump",
            "__partial1",
            "__partial2",
            "__full",
        )
        suffix = next((s for s in derived if path.stem.endswith(s)), None)
        if suffix and references(path.stem[: -len(suffix)]):
            continue

        report.warn(f"{path.name}: mesh is not referenced by any instance")

    tex_dir = ctx.layer_path("textures")
    ctx.texture_files = sorted(tex_dir.glob("*.png")) if tex_dir and tex_dir.is_dir() else []
    for path in ctx.texture_files:
        if path.resolve() not in ctx.referenced_textures:
            report.warn(f"{path.name}: texture is not referenced by any material or mesh")
