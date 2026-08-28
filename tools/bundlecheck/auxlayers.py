"""The auxiliary record layers — §5A lanes, §7.2 appearance, §10 warnings, §8 grading."""

from __future__ import annotations

import json

from .report import Context, load_json_layer


def check_lanes(ctx: Context) -> None:
    """The lane-graph invariants of contract §5A, when the optional layer is declared.

    `lane.schema.json` carries the record shape. What stays here is `owner`, and it is a NOTE
    rather than a check on purpose: §5A used to call it "the id of the networks.jsonl record
    this lane belongs to … absent for lanes owned by a node", and measurement falsifies both
    halves — it is the generic ECS Owner, so it can be an edge, a node (which gets no record at
    all, §5.1) or a building lot. On the reference city 21.7% resolve into networks.jsonl,
    19.1% into instances.jsonl and 59.2% into neither. Reporting the split is useful; requiring
    it to resolve would fail every correct bundle.
    """
    report = ctx.report
    path = ctx.layer_path("lanes")
    if path is None or not path.is_file():
        return

    records = 0
    kinds: dict[str, int] = {}
    to_net = to_instance = to_neither = no_owner = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                report.error(
                    f"lanes.jsonl:{line_no}: blank line (JSON Lines allows no blank lines)"
                )
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"lanes.jsonl:{line_no}: not valid JSON — {exc}")
                continue
            records += 1
            for err in ctx.schemas["lane"].validate(record):
                report.error(f"lanes.jsonl:{line_no}: {err}")

            kind = record.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1

            owner = record.get("owner")
            if owner is None:
                no_owner += 1
            elif owner in ctx.net_ids:
                to_net += 1
            elif owner in ctx.instance_ids:
                to_instance += 1
            else:
                to_neither += 1

    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    report.note(f"{records} lanes ({summary}) (§5A)")
    report.note(
        f"lane owners: {to_net} resolve into networks.jsonl, {to_instance} into "
        f"instances.jsonl, {to_neither} into neither (net nodes get no record, §5.1), "
        f"{no_owner} absent — `owner` is an advisory grouping key, not a foreign key"
    )


def check_appearance(ctx: Context) -> None:
    """`colors.json` and `lights.json` — contract §7.2, when either layer is declared.

    Both are keyed by an assetId that must be findable in the mesh layer: a sub-mesh's glTF
    node name for a palette or a procedural light, the object prefab's own assetId for a
    punctual one. An unresolvable key is not an error — it is dead weight the producer wrote
    for geometry the bundle deliberately does not carry — but it is worth counting, because it
    is the only signal that a key a consumer will look up can never match. Measured on the
    reference city: 184 of 3,620 palette keys (tree `*Child*` sub-meshes, a state §6.1 does not
    export) and 86 of 1,186 light keys (vehicles, which both rip queries exclude).
    """
    report = ctx.report
    for layer_key, schema_key, what in (
        ("colors", "colors", "colour palettes"),
        ("lights", "lights", "light entries"),
    ):
        path = ctx.layer_path(layer_key)
        if path is None or not path.is_file():
            continue
        document = load_json_layer(path, ctx.schemas[schema_key], ctx.layers[layer_key], report)
        if not isinstance(document, dict):
            continue

        entries = sum(len(v) for v in document.values() if isinstance(v, list))
        report.note(f"{entries} {what} over {len(document)} keys ({ctx.layers[layer_key]}, §7.2)")

        if not ctx.mesh_names:
            continue
        unknown = [key for key in document if key not in ctx.mesh_names]
        if unknown:
            samples = ", ".join(sorted(unknown)[:4])
            report.note(
                f"{len(unknown)} of {len(document)} {ctx.layers[layer_key]} keys match no mesh "
                f"file or glTF node name (e.g. {samples}); §7.2 keys join to the mesh layer, so "
                "these describe geometry this bundle does not carry and no consumer lookup can "
                "reach"
            )


def check_warnings(ctx: Context) -> None:
    """`warnings.jsonl` — contract §10, when the layer is declared.

    A consumer may ignore this file entirely; it is schema-validated anyway, because it is also
    how the producer DECLARES a gap, and `load_declared_missing` reads exactly `assetId` and
    `reason` off it. A line the reader cannot parse silently stops excusing anything, which is
    the failure this check exists to make loud.
    """
    report = ctx.report
    path = ctx.layer_path("warnings")
    if path is None or not path.is_file():
        return

    kinds: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                report.error(f"warnings.jsonl:{line_no}: not valid JSON — {exc}")
                continue
            for err in ctx.schemas["warning"].validate(record):
                report.error(f"warnings.jsonl:{line_no}: {err}")
            kind = record.get("kind")
            if isinstance(kind, str):
                kinds[kind] = kinds.get(kind, 0) + 1

    if kinds:
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        report.note(f"warnings.jsonl declares {sum(kinds.values())} gap(s): {summary} (§10)")


def _check_grading_diagnostics(ctx: Context, document: dict) -> None:
    """The capture's own honesty flags: an all-default block with `stackEvaluated` false or
    `volumesFound` 0 is a FAILED READ, not a neutral grade, and the first cut of this layer
    shipped exactly that."""
    report = ctx.report
    diagnostics = document.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        return
    if diagnostics.get("stackEvaluated") is False:
        report.error(
            f"{ctx.layers['grading']}: diagnostics.stackEvaluated is false — the volume "
            "stack was never evaluated, so every block in this file is HDRP's "
            "default rather than the game's grade"
        )
    elif diagnostics.get("volumesFound") == 0:
        report.error(
            f"{ctx.layers['grading']}: diagnostics.volumesFound is 0 — the stack "
            "matched no volume, so every block in this file is a default"
        )
    unread = diagnostics.get("unread")
    if isinstance(unread, list) and unread:
        report.note(
            f"{ctx.layers['grading']}: the producer names {len(unread)} volume "
            "component(s) it does not capture (diagnostics.unread) — read them "
            "before concluding the colour chain is complete"
        )


def check_grading(ctx: Context) -> None:
    """`grading.json` — contract §8, when the layer is declared."""
    report = ctx.report
    path = ctx.layer_path("grading")
    if path is None or not path.is_file():
        return

    document = load_json_layer(path, ctx.schemas["grading"], ctx.layers["grading"], report)
    if not isinstance(document, dict):
        return

    _check_grading_diagnostics(ctx, document)

    blocks = [
        k
        for k in (
            "exposure",
            "whiteBalance",
            "colorAdjustments",
            "shadowsMidtonesHighlights",
            "splitToning",
            "tonemapping",
            "vignette",
            "fog",
            "lighting",
        )
        if k in document
    ]
    report.note(f"{ctx.layers['grading']} carries {', '.join(blocks)} (§8)")

    tonemapping = document.get("tonemapping")
    if (
        isinstance(tonemapping, dict)
        and tonemapping.get("mode") == "External"
        and tonemapping.get("hasLut")
    ):
        report.warn(
            f"{ctx.layers['grading']}: tonemapping is External with a bound LUT, which is "
            "NOT exported — the grading block is incomplete and a consumer replaying it "
            "will not match the game (§8)"
        )
