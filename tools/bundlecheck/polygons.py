"""The polygon layers — §8b surface areas, §8c clip volumes, §8d districts.

All three share one geometry contract: a record's `triangles` index into its own `nodes`. What
differs is the per-layer invariant a schema cannot state — an area's `heightRanges` runs
PARALLEL to its triangles, a clip band is ordered, a district's centroid is the node mean —
and each check records its record count in `ctx.counts` for the manifest comparison.
"""

from __future__ import annotations

import json
from typing import Any

from .report import Context, Report, load_json_layer
from .schema import is_int, is_num


def _triangles_index_nodes(nodes, triangles, label: str, report: Report, suffix: str = "") -> None:
    """Every triangle must index its own record's nodes; the first offender is reported."""
    for tri in triangles:
        if (
            not isinstance(tri, list)
            or len(tri) != 3
            or any(not is_int(i) or not 0 <= i < len(nodes) for i in tri)
        ):
            report.error(f"{label}: triangle {tri!r} does not index its {len(nodes)} nodes{suffix}")
            return


def _iter_records(path, label_stem: str, report: Report):
    """`(line label, parsed record)` per non-blank line; JSON errors reported and skipped."""
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            label = f"{label_stem} line {line_no}"
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"{label}: not valid JSON ({exc})")
                continue
            yield label, record


def _check_area_catalogue(ctx: Context) -> dict[str, Any]:
    """The areaPrefabs catalogue: every map on disk, `unresolved` counted, not invented."""
    report = ctx.report
    catalogue: dict[str, Any] = {}
    catalogue_path = ctx.layer_path("areaPrefabs")
    if catalogue_path is None:
        report.error("layers.areas is declared but no areaPrefabs catalogue is declared")
    elif catalogue_path.is_file():
        catalogue = (
            load_json_layer(
                catalogue_path, ctx.schemas["areaprefabs"], ctx.layers["areaPrefabs"], report
            )
            or {}
        )

    # The catalogue's PBR block is four separate maps, not one `texture` (audit #40 P11). A
    # dangling normalMap is exactly as broken as a dangling base map and used to go unchecked.
    unresolved = 0
    for key, entry in catalogue.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("unresolved"):
            unresolved += 1
        for slot in ("texture", "normalMap", "maskMap"):
            rel = entry.get(slot)
            if rel is not None and not (ctx.bundle_dir / rel).is_file():
                report.error(f"areaprefabs.json[{key}].{slot}: {rel!r} does not exist")

    if unresolved:
        report.note(
            f"{unresolved} of {len(catalogue)} area prefabs are `unresolved` — the save "
            "references a surface asset that was not loaded in the rip session, and the game "
            "itself drew those areas grey (§8b). Enable the asset pack and re-rip; do not "
            "invent an appearance consumer-side"
        )
    return catalogue


def check_areas(ctx: Context) -> None:
    """The surface-area invariants of contract §8b, when the optional layer is declared.

    The record shape is `area.schema.json`'s; what stays here is what a schema cannot say.
    Every record's triangles index into its own nodes and `heightRanges`, when present, runs
    PARALLEL to them — the contract's "in the same order as triangles" is a length agreement a
    consumer indexes by, so a mismatch silently mis-bands every drape. Every `prefab` resolves
    in the areaPrefabs catalogue, every catalogue map exists on disk, and `owner` joins to an
    instance (§8b calls that join total: 27,047 of 27,156 on the reference city, none dangling).
    """
    report = ctx.report
    areas_path = ctx.layer_path("areas")
    if areas_path is None or not areas_path.is_file():
        ctx.counts["areas"] = 0
        return

    catalogue = _check_area_catalogue(ctx)
    records = 0
    dangling_owners: dict[int, int] = {}
    for label, record in _iter_records(areas_path, "areas.jsonl", report):
        records += 1
        for err in ctx.schemas["area"].validate(record):
            report.error(f"{label}: {err}")

        nodes = record.get("nodes")
        triangles = record.get("triangles")
        if not isinstance(nodes, list) or not isinstance(triangles, list):
            continue  # already reported structurally
        _triangles_index_nodes(nodes, triangles, label, report)

        ranges = record.get("heightRanges")
        if isinstance(ranges, list) and len(ranges) != len(triangles):
            report.error(
                f"{label}: heightRanges has {len(ranges)} entries but the area has "
                f"{len(triangles)} triangles; §8b indexes them in parallel"
            )

        prefab = record.get("prefab")
        if isinstance(prefab, str) and prefab and prefab not in catalogue:
            report.error(f"{label}: prefab {prefab!r} is not in the areaPrefabs catalogue")

        owner = record.get("owner")
        if owner is not None and owner not in ctx.instance_ids:
            dangling_owners[owner] = dangling_owners.get(owner, 0) + 1

    if dangling_owners:
        total = sum(dangling_owners.values())
        samples = ", ".join(str(o) for o in list(dangling_owners)[:5])
        report.warn(
            f"{total} area record(s) carry an owner that resolves to no instance "
            f"({len(dangling_owners)} distinct, e.g. {samples}); §8b states this join is total, "
            "so a dangling owner means the instance layer and the area layer came from "
            "different rips"
        )

    report.note(f"{records} surface areas over {len(catalogue)} area prefabs (§8b)")
    ctx.counts["areas"] = records


def check_districts(ctx: Context) -> None:
    """§8d — the player's named districts. Same geometry contract as §8b, plus a name.

    An EMPTY file is legal and expected: most cities have no districts drawn. What is not legal
    is a triangle that does not index its own nodes, or a centroid that is not the node mean —
    the second is checked because a consumer placing a map label reads the centroid and nothing
    else, so a wrong one is invisible until a marker lands in the sea.
    """
    report = ctx.report
    path = ctx.layer_path("districts")
    if path is None or not path.is_file():
        ctx.counts["districts"] = 0
        return
    records = 0
    unnamed = 0
    for label, record in _iter_records(path, "districts.jsonl", report):
        records += 1
        for err in ctx.schemas["district"].validate(record):
            report.error(f"{label}: {err}")
        nodes = record.get("nodes")
        triangles = record.get("triangles")
        if not isinstance(nodes, list) or not isinstance(triangles, list):
            continue
        if not record.get("name"):
            unnamed += 1
        _triangles_index_nodes(nodes, triangles, label, report)
        centroid = record.get("centroid")
        if isinstance(centroid, list) and len(centroid) == 3 and nodes:
            for axis in range(3):
                mean = sum(float(n[axis]) for n in nodes) / len(nodes)
                if abs(mean - float(centroid[axis])) > 0.01:
                    report.error(
                        f"{label}: centroid[{axis}] is {centroid[axis]} but the node "
                        f"mean is {mean:.3f}; §8d defines it as the node mean"
                    )
                    break
    if unnamed:
        report.note(
            f"{unnamed} of {records} districts carry no label — the name system could not "
            f"render one; the polygon is still exact"
        )
    report.note(f"{records} districts (§8d)")
    ctx.counts["districts"] = records


def check_clip_volumes(ctx: Context) -> None:
    """The clip-volume invariants of contract §8c, when the optional layer is declared.

    This layer replaced `terrain/cutout.png` on 2026-08-08 — the precomputed intersection the
    producer used to ship. `clipvolume.schema.json` carries the record shape and the two-form
    split (curves for a net strip, an inline polygon for an area prism); what stays here is
    that a band is ordered and that an area's triangles index its own nodes. Whether a volume
    opens the right terrain is a question about the rip, not about the file.
    """
    report = ctx.report
    path = ctx.layer_path("clipVolumes")
    if path is None or not path.is_file():
        ctx.counts["clipVolumes"] = 0
        return

    kinds: dict[str, int] = {}
    records = 0
    unbounded = 0
    for label, record in _iter_records(path, "clipvolumes.jsonl", report):
        records += 1
        for err in ctx.schemas["clipvolume"].validate(record):
            report.error(f"{label}: {err}")

        record_kind = record.get("kind")
        if isinstance(record_kind, str):
            kinds[record_kind] = kinds.get(record_kind, 0) + 1

        bottom = record.get("bottom")
        if not is_num(bottom):
            continue  # already reported structurally

        # An absent top means unbounded (§8c), which is not the same as a missing field:
        # it is how an authored clip surface says "however high the cover is".
        top = record.get("top")
        if top is None:
            unbounded += 1
        elif is_num(top) and top <= bottom:
            report.error(f"{label}: band is empty or inverted (bottom {bottom}, top {top})")

        nodes = record.get("nodes")
        triangles = record.get("triangles")
        if isinstance(nodes, list) and isinstance(triangles, list):
            _triangles_index_nodes(nodes, triangles, label, report, " (§8c)")

    summary = ", ".join(f"{count} {record_kind}" for record_kind, count in sorted(kinds.items()))
    report.note(f"{records} clip volume strips ({summary}) (§8c)")
    if unbounded:
        report.note(
            f"{unbounded} clip volume(s) have no `top` — unbounded, hiding whatever "
            "covers them however high (§8c)"
        )
    ctx.counts["clipVolumes"] = records
