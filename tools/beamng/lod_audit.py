"""Audit a built level's LOD ladders by simulating Torque's detail selection (Phase 10 §8.13).

    python tools/beamng/lod_audit.py <level_dir> [--hand-over 2500]

Reads `cs2beamng_state/shapes.state.json` (every shape's detail sizes, null, billboard, radius,
game limits, ring membership - what the shapes stage wrote) and the instances state's ring
cells, and for each shape walks the camera from 10 m to the game's cull asking Torque's own
question at every step - "which detail has the largest size not above the projected pixel
size?" - then checks the rules the level is built to:

  R1  sizes strictly decreasing, every mesh size above the null, the billboard between them
  R2  a ring member is drawn at every distance up to the hand-over and never beyond it; its
      cell is drawn beyond and never nearer; the gap / overlap between the two, in metres
  R3  a tree is LOD 0 then the billboard - no generated level in between
  R4  a building carries no generated (clustered) level
  R5  a chain shape's switch sizes are the game's limits times the one uniform scale
  R6  a shape over `authored_lod0_max_tris` starts at the game's LOD 1
  R7  a prop's ladder is the deferred generated ladder, no billboard unless asked
  R8  a billboard sits one pixel over the null (its size is its far stop, so the card runs
      from the coarsest mesh to the cull with no null band in between)
  R9  only a tree carries a runtime billboard (the engine bakes one atlas per shape at play
      time and never frees it; vanilla's are all vegetation)

Exit status 1 when any rule fails. The same numbers `cs2beamng budget` reads, asked a
different question: not "how many triangles" but "is anything drawn twice, or not at all".
"""

import argparse
import itertools
import json
import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from csiibundle import lod, lodchain


def load_state(level_dir, stage):
    path = os.path.join(level_dir, "cs2beamng_state", f"{stage}.state.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def shape_rows(level_dir):
    doc = load_state(level_dir, "shapes")
    results = doc["shared"]["shapes"]["results"]
    pairs = results["$pairs"] if isinstance(results, dict) and "$pairs" in results else results
    out = {}
    for key, row in pairs:
        k = tuple(key["$tuple"]) if isinstance(key, dict) else tuple(key)
        out[k] = row
    return out, doc["shared"]["shapes"].get("options") or {}


def kind_of(row):
    lods = row.get("lods") or {}
    if row.get("tree") is not None:
        return "tree"
    if "authored" in lods or "far" in lods or "farRing" in lods:
        return "building"
    return "prop"


def select(sizes, null_px, billboard_px, p):
    """Torque: the detail with the largest size <= p. Returns 'mesh:<i>', 'billboard', 'null'
    or 'none'."""
    cands = [(s, f"mesh:{i}") for i, s in enumerate(sizes)]
    if billboard_px:
        cands.append((billboard_px, "billboard"))
    if null_px:
        cands.append((null_px, "null"))
    cands = [c for c in cands if c[0] <= p]
    if not cands:
        return "none"
    return max(cands)[1]


def sweep(radius, sizes, null_px, billboard_px, far_m, step=1.05):
    """[(metres, what)] from 10 m out to `far_m`, geometric steps."""
    out = []
    d = 10.0
    while d <= far_m:
        out.append((d, select(sizes, null_px, billboard_px, lod.screen_px(radius, d))))
        d *= step
    return out


def last_build_report(level_dir):
    """The last build's report, or None (the reason printed) when the state on disk is not its."""
    # a build whose shapes stage failed leaves the PREVIOUS build's state behind, and an audit
    # of that is an audit of the wrong level (2026-08-30: a KeyError'd stage, exit 1, audited)
    try:
        with open(os.path.join(level_dir, "cs2beamng_report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        for stage in ("shapes", "instances"):
            status = ((report.get("stages") or {}).get(stage) or {}).get("status")
            if status not in (None, "done", "skipped", "unchanged"):
                print(
                    f"the last build's {stage} stage is {status!r} - the state on disk is an older build's; not auditing"
                )
                return None
        if report.get("status") not in (None, "ok", "done", "built"):
            print(f"the last build's status is {report.get('status')!r} - not auditing")
            return None
    except OSError:
        print("no cs2beamng_report.json - a build is in progress or never finished; not auditing")
        return None
    return report


def bake_lod_index(report):
    """The bake's `lods.json` assets, or {} when the bundle's index cannot be read."""
    # The §8.15 building gate: generated levels ARE legal on a building whose baked levels the
    # MeshLab backend won (`stages/shapejobs._lod_backend` — on-surface by construction, the
    # lattice rule does not apply). The bake index is the authority that gate read, so R4
    # reads the same one rather than trusting a second recording of it.
    try:
        bundle = (report.get("settings") or {}).get("bundle") or ""
        with open(os.path.join(bundle, "derived", "lods", "lods.json"), encoding="utf-8") as handle:
            return json.load(handle).get("assets") or {}
    except (OSError, ValueError):
        return {}


def ring_cells(level_dir):
    """The far-ring cells the instances stage recorded, or [] when there are none."""
    try:
        far = load_state(level_dir, "instances")["result"].get("farRing") or {}
        return far.get("cells") or []
    except (OSError, KeyError):
        return []


def shape_of(row):
    """One shapes-state row, read the way every rule reads it."""
    name, lods, bb = row["file"], row.get("lods") or {}, row.get("billboardPx")
    return SimpleNamespace(
        name=name,
        lods=lods,
        kind=kind_of(row),
        radius=float(row.get("radius") or 0.0),
        sizes=[int(s) for s in lods.get("detailSizes") or []],
        null_px=int(row.get("nullPx") or 0),
        bb=int(bb) if bb else None,
        cull_m=float(row.get("gameCullMetres") or 0.0),
    )


def check_r1(fail, s):
    """R1: sizes strictly decreasing, above the null, the billboard between them."""
    if any(a <= b for a, b in itertools.pairwise(s.sizes)):
        fail("R1", s.name, f"sizes not strictly decreasing: {s.sizes}")
    if s.sizes and min(s.sizes) <= s.null_px:
        fail("R1", s.name, f"a mesh size {min(s.sizes)} at or under the null {s.null_px}")
    if s.bb is not None and not (s.null_px < s.bb < (min(s.sizes) if s.sizes else s.bb + 1)):
        fail(
            "R1",
            s.name,
            f"billboard {s.bb} not between the null {s.null_px} and the meshes {s.sizes}",
        )


def check_kind(fail, s, key, options, lod_index):
    """R3 / R4 / R7 / R9: what each kind of shape may carry."""
    generated = [g for g in (s.lods.get("switches") or []) if g.get("level")]
    if s.kind == "tree":
        if len(s.sizes) != 1 and not s.lods.get("authored"):
            # vegetation with the game's own chain (flower pots, beds) keeps the chain
            fail("R3", s.name, f"a tree ships {len(s.sizes)} mesh levels, not 1")
        tris0 = (s.lods.get("triangles") or [0])[0]
        if s.bb is None and tris0 > 1000 and not s.lods.get("authored"):
            # a flower pot never earns a billboard; a canopy always must
            fail("R3", s.name, f"a tree of {tris0:,} triangles with no billboard")
    elif s.kind == "building":
        if s.bb is not None:
            fail("R9", s.name, "a building with a runtime billboard")
        entry = lod_index.get(f"{key[0]}{key[1]}") or {}
        backends = entry.get("backends")
        if backends is None or isinstance(backends, str):
            backends = [entry.get("backend") if backends is None else backends] * 2
        # PER LEVEL: a generated level on a building is legal only where THAT level's
        # backend is MeshLab (`stages/shapejobs._lod_backend`), not where either level's was
        meshlab_levels = {i + 1 for i, b in enumerate(backends) if b == "meshlab"}
        bad = [g["level"] for g in generated if g["level"] not in meshlab_levels]
        if bad and not s.lods.get("authored"):
            fail("R4", s.name, f"a building with generated level(s) {bad} not shaped by MeshLab")
        if s.lods.get("lod0Promoted") and 1 not in meshlab_levels:
            fail("R4", s.name, "a building with LOD 0 promoted to a level MeshLab did not shape")
    else:
        if s.bb is not None and not options.get("imposters", True):
            fail("R7", s.name, "a prop billboard with imposters off")
        if s.bb is not None:
            fail("R9", s.name, "a prop with a runtime billboard")


def chain_scales(row, kind, radius, options):
    # the game's limits times the shape's one scale; the kind is the build's when it
    # recorded it (`kind` in the row), else every kind's scale is a candidate
    real_kind = row.get("kind") or kind
    scales = {float(options.get("detail_scale") or 1.0)}
    if real_kind == "prop" and options.get("detail_scale_prop"):
        scales = {float(options["detail_scale_prop"])}
    elif real_kind == "tree" and options.get("detail_scale_tree"):
        scales = {float(options["detail_scale_tree"])}
    elif (
        real_kind == "building"
        and options.get("detail_scale_large")
        and radius >= float(options.get("large_radius_m") or 0.0)
    ):
        scales = {float(options["detail_scale_large"])}
    if not row.get("kind"):
        for key in ("detail_scale_prop", "detail_scale_tree", "detail_scale_large"):
            if options.get(key):
                scales.add(float(options[key]))
    return scales


def check_r5(fail, s, row, options):
    """R5: chain switch distances are the game's. True when the shape carries the game's chain."""
    limits = s.lods.get("gameLodLimits")
    if not (limits and s.radius > 0):
        return False
    scales = chain_scales(row, s.kind, s.radius, options)
    expect = [
        max(1.0, lodchain.limit_px(s.radius, lim, options.get("level_of_detail", 0.7)) * sc)
        for lim in limits
        for sc in scales
    ]
    # the shipped sizes are a subset (levels under the hand-over drop; lod1_drop; lod0 drop)
    shipped = set(s.sizes)
    expect_set = (
        {int(math.ceil(e)) for e in expect}
        | {int(round(e)) for e in expect}
        | {int(e) for e in expect}
    )
    strange = [z for z in shipped if z not in expect_set and z != s.null_px + 1]
    # a size clamped strictly-decreasing by one is allowed (the writer's -1 rounding guard)
    strange = [z for z in strange if (z + 1) not in expect_set]
    if strange:
        fail("R5", s.name, f"switch sizes {strange} are not the game's {sorted(expect_set)}")
    return True


def check_r6_r8(fail, s):
    """R6: a dropped LOD 0 leaves a smaller nearest level. R8: the billboard runs to the null."""
    lods = s.lods
    if lods.get("lod0Dropped"):
        if (
            lods.get("triangles")
            and lods.get("lod0Triangles")
            and lods["triangles"][0] >= lods["lod0Triangles"]
        ):
            fail("R6", s.name, "LOD 0 dropped but the nearest level is not smaller than it")
    # R8: a billboard's own size is its FAR STOP, so it must sit one pixel over the null -
    # the card then draws from the coarsest mesh down to the cull (vanilla:
    # pine_radiata_forest.dae ships meshes a800/a350/a150 and bb_autobillboard25). A
    # billboard parked at the old takeover size leaves [null, bb) selecting the null:
    # SpruceTree01 vanished from 99 m to 601 m while R1 passed (2026-08-31).
    if s.bb is not None and s.bb > s.null_px + 1:
        fail(
            "R8",
            s.name,
            f"billboard {s.bb} not adjacent to the null {s.null_px} - nothing drawn in [{s.null_px}, {s.bb}) px",
        )


def check_r2_member(fail, s, hand_over):
    """R2, the ring pair: the gap to the hand-over (negative: overlap); None off the ring."""
    far = s.lods.get("far") or {}
    if not (far.get("ring") and hand_over > 0 and s.radius > 0):
        return None
    # the building is drawn while p >= its coarsest size, i.e. d < stop_m
    stop_m = lod.distance_at_px(s.radius, min(s.sizes)) if s.sizes else 0.0
    walk = sweep(s.radius, s.sizes, s.null_px, s.bb, max(s.cull_m, hand_over * 1.2))
    drawn_beyond = [d for d, w in walk if w.startswith("mesh") and d > stop_m * 1.001]
    absent_within = [d for d, w in walk if not w.startswith("mesh") and d < stop_m * 0.999]
    if drawn_beyond:
        fail("R2", s.name, f"drawn at {drawn_beyond[0]:.0f} m, beyond its stop at {stop_m:.0f} m")
    if absent_within:
        fail(
            "R2",
            s.name,
            f"not drawn at {absent_within[0]:.0f} m, inside its stop at {stop_m:.0f} m",
        )
    # against the hand-over: the building must not stop short of it by more than the
    # rounding of one pixel can explain, and never run past it into the ring
    gap = hand_over - stop_m
    if stop_m < hand_over * 0.999:
        fail(
            "R2",
            s.name,
            f"stops at {stop_m:.0f} m, {gap:.0f} m short of the {hand_over:.0f} m hand-over (a gap where nothing of it is drawn)",
        )
    if s.bb is not None:
        fail("R2", s.name, "a ring member with a runtime billboard under the ring")
    return gap


def audit_shapes(fail, rows, options, lod_index, hand_over):
    """Every per-shape rule; returns (counts, the widest gap, the widest overlap)."""
    counts = {"shapes": len(rows), "tree": 0, "building": 0, "prop": 0, "ring": 0, "chain": 0}
    gap_max = overlap_max = 0.0
    for key, row in sorted(rows.items(), key=lambda kv: kv[1]["file"]):
        s = shape_of(row)
        counts[s.kind] += 1
        check_r1(fail, s)
        check_kind(fail, s, key, options, lod_index)
        if check_r5(fail, s, row, options):
            counts["chain"] += 1
        check_r6_r8(fail, s)
        gap = check_r2_member(fail, s, hand_over)
        if gap is None:
            continue
        counts["ring"] += 1
        if gap > 0:
            gap_max = max(gap_max, gap)
        else:
            overlap_max = max(overlap_max, -gap)
    return counts, gap_max, overlap_max


def audit_cells(fail, cells, hand_over):
    """R1 / R2 for the ring cells: their own sizes, and where they start against the hand-over."""
    for c in cells:
        r = float(c["radius"])
        near, ring, null = int(c["nearPx"]), int(c["ringPx"]), int(c["nullPx"])
        if not (near > ring > null >= 1):
            fail("R1", c["name"], f"cell sizes near {near} ring {ring} null {null}")
        start_m = lod.distance_at_px(r, near)
        end_m = lod.distance_at_px(r, ring)
        walk = sweep(r, [near, ring], null, None, end_m * 1.5)
        ring_near = [d for d, w in walk if w == "mesh:1" and d < start_m * 0.999]
        if ring_near:
            fail(
                "R2",
                c["name"],
                f"ring drawn at {ring_near[0]:.0f} m, nearer than its start {start_m:.0f} m",
            )
        if start_m > hand_over * 1.001:
            fail("R2", c["name"], f"cell starts at {start_m:.0f} m, past the hand-over")
        if start_m < hand_over * 0.9:
            fail("R2", c["name"], f"cell starts at {start_m:.0f} m, well short of the hand-over")


def print_report(counts, cells, hand_over, gap_max, overlap_max, fails, examples):
    """The totals, then each rule's verdict and its first few failures; True when all passed."""
    print(
        f"shapes {counts['shapes']:,}: {counts['building']:,} buildings ({counts['chain']:,} with the game's chain, {counts['ring']:,} ring members), {counts['prop']:,} props, {counts['tree']:,} trees; ring cells {len(cells)}"
    )
    if cells:
        print(
            f"hand-over {hand_over:.0f} m: buildings stop up to {gap_max:.0f} m short of it (a gap) and up to {overlap_max:.0f} m past it (an overlap, inside the ring)"
        )
    else:
        print(
            "no ring cells recorded in instances.state.json (built before the report carried them)"
        )
    ok = True
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"):
        bad = fails.get(rule) or []
        print(f"  {rule}: {'ok' if not bad else f'{len(bad):,} FAIL'}")
        for name, why in bad[:examples]:
            print(f"      {name}: {why}")
        ok = ok and not bad
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("level_dir")
    parser.add_argument("--hand-over", type=float, default=None)
    parser.add_argument("--examples", type=int, default=5)
    args = parser.parse_args()

    report = last_build_report(args.level_dir)
    if report is None:
        return 2
    rows, options = shape_rows(args.level_dir)
    lod_index = bake_lod_index(report)
    hand_over = args.hand_over or float(options.get("far_ring_metres") or 0.0)
    cells = ring_cells(args.level_dir)

    fails = {}

    def fail(rule, name, why):
        fails.setdefault(rule, []).append((name, why))

    counts, gap_max, overlap_max = audit_shapes(fail, rows, options, lod_index, hand_over)
    audit_cells(fail, cells, hand_over)
    ok = print_report(counts, cells, hand_over, gap_max, overlap_max, fails, args.examples)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
