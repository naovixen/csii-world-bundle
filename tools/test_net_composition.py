"""Gate for the network composition port — audit §53.

THE RULE, from `Game.Rendering.BatchMeshHelpers+GenerateBatchMeshJob.GenerateCompositionMesh`.
The game never stores a road mesh. It bakes each composition piece's vertex into PARAMETER space
— `x` across the composition in [0, 1], `z` along it — and the vertex shader rebuilds the world
position as `lerp(A(z), B(z), x)`, where `A` and `B` are two of the eight cubic boundary curves
`BatchDataHelpers.Calculate{Edge,Node,Orphan}Parameters` hands over. The generator's last act
before writing the vertex is `vertex.xz = math.saturate(vertex.xz)`.

That `saturate` is the invariant this file gates. Because `x` is clamped into [0, 1] and the two
curves it interpolates ARE the composition's left and right boundaries, an assembled composition
can neither reach past its own width nor fall short of it: at any station the running surface
spans exactly `NetCompositionData.m_Width`, which the producer ships as `attrs.width`.

WHAT IT MEASURES. For every straight segment, the mesh's lateral extent in a narrow band about
its own centre, over `attrs.width`. The band is narrow on purpose — a `seg_*.glb` carries the
edge composition AND both node compositions, and a node flare is a DIFFERENT composition with its
own (larger) width whose boundary curves splay toward the junction. Widen the band and the flares
walk into it, which is why the score is reported at four band widths: a healthy bundle shows the
ratio rising monotonically with the band and sitting at 1.0000 at the centre. A mapping error in
the scale/bias pair, by contrast, would be flat across all four bands and off 1.0 at every one.

Measured on the 2026-08-16 rip (493 straight segments, 800 sampled): centre-band median 1.0000,
p95 1.0068, min 0.9982, 96.2 % within 1 %; the +/-0.50 band reads median 1.0365 — the flares.

THE NEGATIVE CONTROL IS PART OF THE TEST, per `test_node_fan.py`'s lesson: a gate whose green
means nothing is worse than no gate. The same scorer is re-run over vertices squeezed laterally
to 88 % and stretched to 112 %, and the run FAILS if either is still scored healthy.

Run: `python tools/test_net_composition.py`  (system Python + numpy; skipped when no bundle is
on disk).
"""

import json
import os
import pathlib
import random
import struct
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import itertools

from inspect_glb import read_glb

BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%LOCALAPPDATA%\..\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto"
)

# How many segments to open. The score is stable well below this; it is a runtime bound, not a
# statistical one.
SAMPLE = 800

# Segments shorter than this are all flare and have no edge-only middle to measure.
MIN_LENGTH = 30.0

# A curved segment's PCA long axis is its CHORD, which is shorter than the arc `attrs.length`
# reports, and its mid-band bulges away from that axis. Only segments whose along-extent matches
# their own length are straight enough to score.
STRAIGHT_TOLERANCE = 0.05

# The bands, as a fraction of the segment's along-extent either side of centre.
BANDS = (0.50, 0.25, 0.10, 0.04)
CENTRE_BAND = 0.04

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def positions(path):
    """Every POSITION vertex of a `.glb`, as one (n, 3) array."""
    gltf, blob = read_glb(pathlib.Path(path))
    out = []
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            index = prim.get("attributes", {}).get("POSITION")
            if index is None:
                continue
            acc = gltf["accessors"][index]
            view = gltf["bufferViews"][acc["bufferView"]]
            base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
            stride = view.get("byteStride") or 12
            count = acc["count"]
            if stride == 12:
                out.append(
                    numpy.frombuffer(blob, dtype="<f4", count=count * 3, offset=base).reshape(
                        count, 3
                    )
                )
            else:
                rows = [struct.unpack_from("<fff", blob, base + i * stride) for i in range(count)]
                out.append(numpy.asarray(rows, dtype=numpy.float32))
    return numpy.concatenate(out) if out else None


def score(vertices, width, length, squeeze=1.0):
    """The width ratio per band for one segment, or None when it is not scorable.

    `squeeze` is the negative control: it scales the lateral coordinate after the frame is found,
    standing in for a cross-section that does not reach its boundary curves (or overruns them).
    """
    flat = vertices[:, [0, 2]]
    flat = flat - flat.mean(axis=0)
    _, _, basis = numpy.linalg.svd(flat, full_matrices=False)
    along = flat @ basis[0]
    lateral = (flat @ basis[1]) * squeeze
    span = along.max() - along.min()
    if abs(span - length) > STRAIGHT_TOLERANCE * length:
        return None
    out = {}
    for band in BANDS:
        inside = numpy.abs(along) < band * span
        if inside.sum() < 20:
            return None
        out[band] = float(lateral[inside].max() - lateral[inside].min()) / width
    return out


def load_records():
    path = os.path.join(BUNDLE, "networks.jsonl")
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def collect(records, squeeze=1.0):
    """Score a fixed sample of straight segments. The sample is seeded, so runs compare."""
    meshed = sorted(r["assetId"] for r in records if r.get("assetId"))
    rng = random.Random(11)
    chosen = rng.sample(meshed, min(SAMPLE, len(meshed)))
    by_id = {r["assetId"]: r for r in records if r.get("assetId")}
    rows = {band: [] for band in BANDS}
    for asset in chosen:
        record = by_id[asset]
        width = record["attrs"].get("width")
        length = record["attrs"].get("length")
        if not width or not length or length < MIN_LENGTH:
            continue
        path = os.path.join(BUNDLE, "networks", asset + ".glb")
        if not os.path.isfile(path):
            continue
        verts = positions(path)
        if verts is None or len(verts) < 30:
            continue
        scored = score(verts, width, length, squeeze)
        if scored is None:
            continue
        for band, value in scored.items():
            rows[band].append(value)
    return {band: numpy.asarray(values) for band, values in rows.items()}


def healthy(rows):
    """The three clauses that say `saturate` held. Returns (verdict, reason)."""
    centre = rows[CENTRE_BAND]
    if len(centre) < 50:
        return False, f"only {len(centre)} scorable segments"
    median = float(numpy.median(centre))
    within = float(numpy.mean(numpy.abs(centre - 1.0) < 0.01))
    floor = float(centre.min())
    if not 0.995 <= median <= 1.005:
        return False, f"centre-band median {median:.4f} is not 1.0"
    if within < 0.95:
        return False, f"only {100 * within:.1f}% within 1% of the composition width"
    if floor < 0.99:
        return False, f"a segment spans {floor:.4f} of its width — a collapsed cross-section"
    return True, f"median {median:.4f}, {100 * within:.1f}% within 1%, floor {floor:.4f}"


def test_saturate_holds(records):
    """The measurement: the composition spans its own width, and the flares explain the rest."""
    print("composition width: the `saturate` bound on xParam")
    rows = collect(records)
    for band in BANDS:
        values = rows[band]
        if not len(values):
            continue
        print(
            f"    band +/-{band:.2f}*span  n={len(values):4d}  median {numpy.median(values):.4f}"
            f"  p95 {numpy.percentile(values, 95):.4f}  min {values.min():.4f}"
        )
    ok, reason = healthy(rows)
    check(ok, f"the centre band spans exactly the composition width ({reason})")

    # The flare signature. If the excess came from the mapping rather than from the node
    # compositions, it would not shrink as the band closes on the segment's middle.
    medians = [float(numpy.median(rows[band])) for band in BANDS if len(rows[band])]
    check(
        all(a >= b - 1e-9 for a, b in itertools.pairwise(medians)),
        f"the excess falls monotonically toward the segment centre "
        f"({' > '.join(f'{m:.4f}' for m in medians)}) - node flares, not a mapping error",
    )
    return rows


def test_negative_control(records):
    """Green must be earned: the same scorer has to convict a mesh that misses its boundaries."""
    print("negative control (the gate fails on a defective bundle):")
    for squeeze, label in (
        (0.88, "a cross-section reaching only 88% across"),
        (1.12, "a cross-section overrunning its boundary curves by 12%"),
    ):
        ok, reason = healthy(collect(records, squeeze=squeeze))
        check(not ok, f"{label} is convicted ({reason})")


def test_form_invariants(records):
    """Contract §5: a record is mesh form or centerline form, never both and never neither."""
    print("network records: the two geometry forms")
    both = [r for r in records if r.get("assetId") and r.get("centerline")]
    check(not both, f"no record carries both forms ({len(both)} found)")

    dangling = [
        r
        for r in records
        if r.get("assetId")
        and not os.path.isfile(os.path.join(BUNDLE, "networks", r["assetId"] + ".glb"))
    ]
    check(not dangling, f"every mesh-form record has its .glb ({len(dangling)} dangling)")

    lines = [r for r in records if not r.get("assetId")]
    bad = [r for r in lines if len(r.get("centerline") or ()) < 2 or not r.get("width", 0) > 0]
    check(
        not bad,
        f"every centerline-form record has >=2 points and a positive width "
        f"({len(lines)} centerline-form, {len(bad)} bad)",
    )


def main():
    if not os.path.isfile(os.path.join(BUNDLE, "networks.jsonl")):
        print(f"no bundle at {BUNDLE} — skipped")
        return 0
    records = load_records()
    print(f"bundle: {BUNDLE}\n  {len(records)} network record(s)\n")
    test_form_invariants(records)
    test_saturate_holds(records)
    test_negative_control(records)
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}:")
        for label in FAILED:
            print("  - " + label)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
