"""The shapes stage's report: what ladder every shape actually shipped with.

The numbers here are the only place a person can see what the LOD rules did — the draw distance
per kind against the game's own, where each generated level took over and with how much surface
error, and what the promotion and the imposter gate accepted or refused.
"""

import time

from .shapecollider import BOX_COLLIDER_MAX_METRES, TRUNK_WIDE_RATIO
from .shapelods import (
    DISPLACEMENT_RULE_KINDS,
)


def summarise_switches(results):
    """What the displacement rule actually did, per generated level: the distribution of the
    measured error, the thresholds it produced, and how many shapes it FLOORED - a floored level
    is one too crude to earn a place at this pixel budget, so it is drawn only in the last band
    before the shape disappears and the shape effectively keeps LOD 0 instead. Counting them is
    the honest way to see what the budget bought."""
    per_level = {}
    exempted = 0
    for row in results.values():
        for entry in row["lods"].get("switches") or []:
            if entry.get("exempt"):
                exempted += 1
                continue
            if entry.get("metres") is None:
                continue
            bucket = per_level.setdefault(
                entry["level"], {"metres": [], "px": [], "error": [], "deferred": 0, "factor": []}
            )
            bucket["metres"].append(entry["metres"])
            bucket["px"].append(entry["px"] or 0)
            bucket["error"].append(entry["errorPx"] or 0.0)
            if entry.get("deferred"):
                bucket["deferred"] += 1
                if entry.get("px"):
                    bucket["factor"].append(entry["ladderPx"] / max(entry["px"], 1e-6))
    out = {"exemptLevels": exempted}
    for level, bucket in sorted(per_level.items()):
        metres = sorted(bucket["metres"])
        error = sorted(bucket["error"])

        def at(values, fraction):
            return round(values[min(len(values) - 1, int(fraction * len(values)))], 4)

        factor = sorted(bucket["factor"])
        out[f"LOD{level}"] = {
            "shapes": len(metres),
            "metresP50": at(metres, 0.5),
            "metresP90": at(metres, 0.9),
            "metresMax": at(metres, 1.0),
            "errorPxP50": at(error, 0.5),
            "errorPxP90": at(error, 0.9),
            "identical": sum(1 for m in metres if m <= 1e-6),
            # the only shapes this rule changes: the ones whose level was too far out of shape to
            # keep the ladder's place, and how many times further out it went
            "deferred": bucket["deferred"],
            "deferredMedianFactor": (round(factor[len(factor) // 2], 2) if factor else None),
            "deferredMaxFactor": (round(factor[-1], 2) if factor else None),
        }
    return out


def report(results, demand, scan_stats, options, errors, started, progress, rewritten=0, orphans=0):
    rows_by_pair = {}
    for (asset, suffix, _variant), row in results.items():
        rows_by_pair.setdefault((asset, suffix), []).append(row)
    summary = _cull_summary(rows_by_pair, demand, progress)
    switch_summary = summarise_switches(results)
    colliders = {}
    for row in results.values():
        colliders[row["collider"]] = colliders.get(row["collider"], 0) + 1
    # trunk cylinders that are really canopy cylinders - see `trunkRatio` in `shape_job`
    trunk_ratios = [r["trunkRatio"] for r in results.values() if r.get("trunkRatio") is not None]
    trunk_wide = sum(1 for r in trunk_ratios if r > TRUNK_WIDE_RATIO)
    promoted = [r for r in results.values() if (r.get("lods") or {}).get("lod0Promoted")]
    kept_small = [r for r in results.values() if (r.get("lods") or {}).get("lod0KeptSmall")]
    # residency (Phase 10 §8): shapes shipping the game's LOD 1 as their nearest level, and the
    # authored levels left out for being byte-identical to LOD 0 - both declared, both counted
    lod0_dropped = [r for r in results.values() if (r.get("lods") or {}).get("lod0Dropped")]
    identical_levels = sum(
        len((r.get("lods") or {}).get("identicalLevels") or ()) for r in results.values()
    )
    # ...and the bake's generated levels left out for being the geometry of the level above them
    # (`shapelods._load_generated`; MeshLab stalling at one count for two targets)
    identical_generated = sum(
        int((r.get("lods") or {}).get("identicalGenerated") or 0) for r in results.values()
    )
    if lod0_dropped:
        progress.log(
            f"shapes: {len(lod0_dropped):,} shapes over authored_lod0_max_tris ship the game's "
            f"LOD 1 as their nearest level ({sum((r['lods'].get('lod0Triangles') or 0) for r in lod0_dropped):,} "
            f"LOD 0 triangles left out)"
        )
    # the refined levels, declared and counted like every other approximation
    refined = [r for r in results.values() if (r.get("lods") or {}).get("refinedFrom")]
    r_from = sum((r["lods"].get("refinedFrom") or 0) for r in refined)
    r_to = sum((r["lods"].get("refinedTo") or 0) for r in refined)
    if refined:
        progress.log(
            f"shapes: {len(refined):,} shapes ship a refined level ({r_from:,} -> {r_to:,} "
            f"triangles, {100.0 * (1.0 - r_to / r_from):.1f} % out) inside the bake's measured "
            "surface tolerance"
        )
    # ...and the shapes that KEPT full detail because the game's own LOD 1 was too far from it
    kept_far = [r for r in results.values() if (r.get("lods") or {}).get("lod0DropSkippedDistance")]
    if kept_far:
        worst = max(r["lods"]["lod0DropSkippedDistance"] for r in kept_far)
        progress.log(
            f"shapes: {len(kept_far):,} shapes keep their full-detail mesh - the game's own LOD 1 "
            f"is up to {worst:.2f} m from it (authored_lod0_max_error)"
        )
    if identical_levels:
        progress.log(
            f"shapes: {identical_levels} authored levels left out for being identical to LOD 0"
        )
    if identical_generated:
        progress.log(
            f"shapes: {identical_generated} generated levels left out for being identical to the "
            "level above them"
        )
    _log_ladder(progress, switch_summary, promoted, kept_small, options)
    return {
        "shapes": len(results),
        "lod0Promoted": len(promoted),
        "lod0KeptSmall": len(kept_small),
        "lod0Dropped": len(lod0_dropped),
        "lod0DroppedTriangles": sum((r["lods"].get("lod0Triangles") or 0) for r in lod0_dropped),
        "refinedShapes": len(refined),
        "refinedFromTriangles": r_from,
        "refinedToTriangles": r_to,
        "lod0DropSkippedDistance": len(kept_far),
        "identicalLevelsDropped": identical_levels,
        "identicalGeneratedDropped": identical_generated,
        "shapesRewritten": rewritten,
        "orphansRemoved": orphans,
        "failed": len(errors),
        "errors": errors[:20],
        "scan": scan_stats,
        "triangles": sum(r["triangles"] for r in results.values()),
        "colliders": colliders,
        "trunkFit": {
            "shapes": len(trunk_ratios),
            "wide": trunk_wide,
            "medianRatio": (
                round(sorted(trunk_ratios)[len(trunk_ratios) // 2], 3) if trunk_ratios else None
            ),
            "ratio": TRUNK_WIDE_RATIO,
        },
        "imposters": {
            "shapes": sum(1 for r in results.values() if r.get("billboardPx")),
            "ofShapes": len(results),
            "minMetres": round(options["imposter_min_metres"], 1),
            "vanillaShare": "west_coast_usa 3.1 %, italy 7.2 %, johnson_valley 4.4 %",
        },
        "detailScale": options["detail_scale"],
        "detailScaleProp": options.get("detail_scale_prop") or options["detail_scale"],
        "levelOfDetail": options["level_of_detail"],
        "drawDistance": summary,
        "approximations": _approximations(
            results,
            demand,
            rows_by_pair,
            options,
            scan_stats,
            switch_summary,
            trunk_wide,
        ),
        "seconds": round(time.monotonic() - started, 1),
    }


def _cull_summary(rows_by_pair, demand, progress):
    """Per kind: how far CS2 draws the median shape, and how far this level does."""
    by_kind = {}
    for pair, entry in demand.items():
        rows = rows_by_pair.get(pair)
        if not rows:
            continue
        bucket = by_kind.setdefault(
            entry["kind"], {"shapes": 0, "instances": 0, "gameCull": [], "ourCull": []}
        )
        bucket["shapes"] += len(rows)
        bucket["instances"] += entry["count"]
        for row in rows:
            bucket["gameCull"].append(row["gameCullMetres"])
            # distance scales inversely with the pixel threshold, so ours is the game's distance
            # times (game px / shipped px). `stopPx` is the smallest detail the shape still
            # draws - its coarsest mesh, or the billboard under it - which is where the level
            # actually stops drawing it; the null is only the size that stop stays over, and
            # reading the distance off the null overstated it by the gap between the two
            # (1.5x on every generated ladder before A5).
            stop = row.get("stopPx") or row["nullPx"]
            bucket["ourCull"].append(row["gameCullMetres"] * row["gamePx"] / max(stop, 1e-6))
    return _cull_medians(by_kind, progress)


def _cull_medians(by_kind, progress):
    summary = {}
    for kind, bucket in by_kind.items():
        game = sorted(bucket["gameCull"])
        ours = sorted(bucket["ourCull"])
        summary[kind] = {
            "shapes": bucket["shapes"],
            "instances": bucket["instances"],
            "gameCullMedian": round(game[len(game) // 2], 1),
            "ourCullMedian": round(ours[len(ours) // 2], 1),
        }
        progress.log(
            f"shapes: {kind} - {bucket['shapes']} shapes, CS2 draws the median one to "
            f"{summary[kind]['gameCullMedian']:,.0f} m, this level to "
            f"{summary[kind]['ourCullMedian']:,.0f} m"
        )
    return summary


def _log_ladder(progress, switch_summary, promoted, kept_small, options):
    """Say out loud what the two LOD rules did - the deferral and the LOD 0 promotion."""
    for name, entry in switch_summary.items():
        if not isinstance(entry, dict) or "deferred" not in entry:
            continue
        progress.log(
            f"shapes: {name} is DEFERRED past the ladder on {entry['deferred']:,} of "
            f"{entry['shapes']:,} shapes (median {entry['deferredMedianFactor']}x further "
            f"out, worst {entry['deferredMaxFactor']}x); the rest keep the ladder's own "
            f"place, which no shape now takes NEARER than"
        )
    if not kept_small:
        return
    smallest = min(int(r["lods"]["lod0KeptSmall"]) for r in kept_small)
    progress.log(
        f"shapes: LOD0 is KEPT on {len(kept_small):,} shapes that measured within "
        f"{options.get('lod0_max_error')} m of their LOD1 but hold fewer than "
        f"{options.get('lod0_min_triangles'):,} triangles once promoted (smallest "
        f"{smallest}) - a surface distance cannot see a decimation that opened the "
        f"object up, and 381 triangles is a facade you can see through. "
        f"{len(promoted):,} shapes were still promoted"
    )


def _approximations(results, demand, rows_by_pair, options, scan_stats, switch_summary, trunk_wide):
    """Every rule here with no counterpart in CS2, what it is, why, and how often it fires.

    A CLAUDE.md non-negotiable: an undeclared approximation is indistinguishable from a bug once
    its artefact appears, so each one states what it approximates and carries its own count.
    """
    return {
        **_lod_approximations(results, demand, rows_by_pair, options, switch_summary),
        **_placement_approximations(results, options, scan_stats, trunk_wide),
    }


def _lod_approximations(results, demand, rows_by_pair, options, switch_summary):
    """The three that decide how far a shape is drawn and which detail it is drawn with."""
    return {
        "lodDetailScalePerKind": {
            "what": "a PROP's cull threshold uses `lod_detail_scale_prop` instead of the "
            "global multiplier",
            "why": "CS2 draws a median prop to 1,024 m and a median building to 13,008 m, a "
            "factor of 12.7, and one global divisor preserves that ratio while "
            "destroying the relationship - at 105 m a window frame is plainly visible "
            "where at CS2's 1,024 m it is sub-pixel, so facades read as construction "
            "sites (A6.2). DECLARED TUNING: the game has no per-kind divisor. The "
            "game's actual sub-object rule - cull at the OWNER's bounds, keep your own "
            "m_MinLod (audit §70) - is NOT ported, because BeamNG holds detail "
            "thresholds per SHAPE and ownership is per INSTANCE",
            "count": sum(
                1
                for pair, entry in demand.items()
                if entry.get("kind") == "prop" and pair in rows_by_pair
            ),
            "value": options.get("detail_scale_prop") or options["detail_scale"],
        },
        "lodDetailScale": {
            "what": "every shape's cull threshold is CS2's own, multiplied by lod_detail_scale",
            "why": "CS2 draws a median prop to 1 km and a median building to 7.3 km over 1.2M "
            "objects; BeamNG cannot. 1.0 restores the game exactly.",
            "count": len(results),
            "value": options["detail_scale"],
        },
        "lodLadderSwitchPoints": {
            "what": "where LOD1/LOD2 take over: the distance at which that level's own "
            f"measured displacement from LOD 0 is worth "
            f"{options['switch_error_px']:.0f} px",
            "why": "CS2 ships no LOD chain for these prefabs (lodCap 0), so there is no game "
            "rule to port. The ladder used to be fixed multiples of the shape's CULL "
            "threshold, and that threshold is a statement about the shape's SIZE, not "
            "about its LOD's ERROR: a LOD1's displacement runs from 0 m on 603 of "
            "4,261 shapes to 37 m on the worst, so one multiple cannot serve both ends "
            "and the coarse end reads as a roof moving under props that have not moved "
            "(A6.7). Each level now takes its threshold from its own displacement, "
            "measured by the bake as the p95 distance from the generated surface back "
            "to LOD 0's. The PIXEL BUDGET is still ours: "
            "about a pixel is what fidelity would ask for and costs +384 % to +630 % "
            "drawn triangles (measured over 1.16 M placements from four viewpoints), "
            "where 6 px costs +0 % to +16 %, so `lod_switch_error_px` is a declared "
            "budget and not a measurement",
            "count": sum(1 for r in results.values() if r["lods"]["levels"] > 1),
            "value": options["switch_error_px"],
            "kinds": list(DISPLACEMENT_RULE_KINDS),
            "displacement": switch_summary,
        },
    }


def _placement_approximations(results, options, scan_stats, trunk_wide):
    """The five about what a shape collides with, what is removed, and what is not ported."""
    return {
        "treeTrunkCylinderFit": {
            "what": "a tree whose trunk collider is wider than "
            f"{TRUNK_WIDE_RATIO:.0%} of its own canopy - the trunk search found the "
            "canopy instead",
            "why": "CS2 has no collision for a planted object, so there is no rule to port. "
            "`collision.trunk_group` prefers a material run NAMED trunk/bark/stem, "
            "and on a CS2 bundle that branch never fires - the game's material names "
            "are hashed asset ids - so every tree falls through to the geometric "
            "fallback (lowest band, smallest footprint). That is right for a clean "
            "bole and wrong where the lowest band is not the trunk: a spruce whose "
            "branches sweep the ground, or a wide shrub CS2 counts as a tree because "
            "it carries growth. MEASURED on the reference city: 5 of 32 tree prefabs, "
            "4.5 % of tree items",
            "count": trunk_wide,
        },
        "oversizedBoxColliders": {
            "what": f"a shape whose footprint is over {BOX_COLLIDER_MAX_METRES:.0f} m keeps a "
            f"MESH collider even past the triangle cap, instead of a padded box - and ships NO "
            f"collider when it is also too thin for a mesh one (`oversizedThin`)",
            "why": "a padded box is a fine stand-in at the median shape's 1.6 m and a trap at "
            "1,344 m: `InternationalAirport01`'s box was 1,344 x 1,245 x 82 m, an "
            "invisible solid block a kilometre wide, and driving onto the airport put "
            "the car inside it. 34 of 1,407 boxes were over 20 m - the harbour, the "
            "power plant, stadiums. CS2 has no collision for a placed object, so the "
            "20 m is OURS and is declared here",
            "count": sum(
                1
                for r in results.values()
                if r.get("collider") in ("mesh_oversized", "oversizedThin")
            ),
            "thinDropped": sum(1 for r in results.values() if r.get("collider") == "oversizedThin"),
            "value": BOX_COLLIDER_MAX_METRES,
        },
        "orphansOfAbsentOwners": {
            "what": "a record whose owner the producer declared ABSENT is not placed either",
            "why": "not a CS2 rule — the game had those prefabs and drew the buildings. It is "
            "a consumer's obligation against a DECLARED gap (§10): the prefab did not "
            "resolve at rip time (an unowned asset pack), so we drop the building and "
            "would otherwise place its props in mid-air — 6,714 records on the "
            "reference city, up to 292 m up, 695 of them hanging off ONE absent tower. "
            "Only the absent class is dropped: a prefab that exists WITHOUT GEOMETRY "
            "keeps its 5,806 children (the lamps and benches of car parks and sports "
            "pitches, whose own surface ships in the `areas` layer), and a construction "
            "site keeps its children too — measurement says they are the "
            "ConstructionCrane01 and the site's benches, which §4 states are real. "
            "THE REMEDY AT SOURCE IS CHEAPER: enable those asset packs in CS2 and "
            "re-rip, which brings the buildings back instead of hiding their props",
            "count": (scan_stats.get("orphans") or {}).get("orphans", 0),
            "owners": (scan_stats.get("orphans") or {}).get("owners", 0),
        },
        **_removal_approximations(scan_stats),
    }


def _removal_approximations(scan_stats):
    """The two about what is taken OUT of the level, and the one CS2 rule not ported."""
    return {
        "foliageRoadClearance": {
            "what": f"a TREE standing within "
            f"{(scan_stats.get('roadclear') or {}).get('clearance', 0):.2f} m of a "
            f"drivable lane centre is not placed",
            "why": "A DECLARED DEVIATION, asked for, not a fidelity rule - CS2 clears objects "
            "when a road is BUILT and whatever survives in the save is what the city "
            "contains, so there is no game rule to port. Trees only: a bush or a "
            "flower pot is driven straight through once it stops colliding (A6.1), so "
            "removing it would cost fidelity and buy no drivability. The distance is "
            "the VEHICLE ENVELOPE, not an edge found in the data - the sweep showed "
            "trees per 0.25 m band are almost FLAT from 0.5 m out to 5 m, so there is "
            "no natural threshold to prefer. `foliage_road_clearance` 0 turns it off. "
            "Separately and always on, CS2's own `Overridden` rule removes what the "
            "game itself does not draw (instances.should_draw)",
            "count": (scan_stats.get("roadclear") or {}).get("clipped", 0),
            "value": (scan_stats.get("roadclear") or {}).get("clearance", 0),
            "ofTrees": (scan_stats.get("roadclear") or {}).get("trees", 0),
        },
        "renderingSizeSpecialCases": {
            "what": "the human-object and Standing-leg shrinks in GetRenderingSize",
            "why": "both need ObjectGeometryData flags the bundle does not ship; both only "
            "shrink the size, so the threshold errs towards drawing further",
            "count": None,
        },
    }
