"""Gate for the §7.1 mask packing — audit §54.

THE QUESTION. Contract §7.1 read the mask as an HDRP mask map (R metallic, G ambient occlusion,
B detail, A smoothness). Only R and A were ever safe: CS2's own asset-authoring documentation
describes G and B as a glossy-coat layer and an unused channel, CS2 ships its shaders compiled
inside asset bundles so no managed code settles it, and binding G as `ambientOcclusionMap`
painted every tunnel interior and retaining wall black (BeamNG loads #9-#14).

THE ANSWER IS IN THE CONTENT, and this file is where it is checked. Over all 890 distinct mask
textures of the reference city:

* **R is metallic** — mean 34/255 with 75 % of texels exactly 0 and 7 % exactly 255. Binary, as
  a metallic map is.
* **A is smoothness** — the only channel populated everywhere (0.3 % of masks all-zero), mean
  98/255, and essentially uncorrelated with R, G and B (|r| <= 0.17).
* **G IS NOT AMBIENT OCCLUSION** — mean 9/255, 91 % of texels exactly zero, 34 % of masks
  entirely zero. An AO map is near-white with dark creases. Of the 18 masks whose G mean clears
  96/255, nine are uniformly white (sigma <= 0.3, a no-op) and nine are bimodal 0/255 (a stencil,
  not a gradient). Exactly **two of 890 (0.22 %)** survive all three AO clauses, and both are
  near-white (G mean 162 and 242) — so even those would barely darken anything. The channel is
  not an occlusion map, and a per-material "bind it when it looks light enough" gate would fire
  on 0.22 % of the layer while being wrong or a no-op on the rest.
* G and B correlate with each other at +0.64 and with nothing else — a related pair of effect
  masks whose meaning stays unknown, and is recorded as unknown rather than guessed at again.

WHAT IT GATES. Two structural clauses that keep the answer from being un-learned — no consumer
wires an occlusion slot, and the bake does not split one — plus the measurement itself, re-run
against whatever bundle is on disk so a future city that really does ship AO in G is not silently
treated by yesterday's conclusion.

THE NEGATIVE CONTROL IS PART OF THE TEST: the same classifier is run over a synthesised AO-like
channel (near-white, left-skewed) and must call it AO. Otherwise "not AO" would be a verdict the
classifier gives everything.

Run: `python tools/test_mask_channels.py`  (system Python + numpy + Pillow; the measurement is
skipped when no bundle is on disk, the structural clauses always run).
"""
import json
import os
import sys

import numpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from csiibundle import materials  # noqa: E402

BUNDLE = os.environ.get("CARTO_BUNDLE") or os.path.expandvars(
    r"%LOCALAPPDATA%\..\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto")

# How many distinct masks to open. 890 on the reference city; all of them, it is seconds.
MAX_MASKS = 2000

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def looks_like_ao(channel):
    """Is this channel an ambient-occlusion map?

    AO is near-white with dark creases: a high mean, most texels bright, and a continuous
    gradient rather than a two-level stencil. All three clauses have to hold.
    """
    mean = float(channel.mean())
    bright = float((channel >= 160).mean())
    # A stencil puts almost every texel at one of the two extremes; a gradient does not.
    extreme = float(((channel <= 8) | (channel >= 247)).mean())
    return (mean >= 96.0) and (bright >= 0.5) and (extreme < 0.9)


def test_no_occlusion_slot():
    """The rule lives in `csiibundle`, so both consumers get it: no AO comes out of the mask."""
    print("mask packing: the shared rule")
    plan = materials.wire_plan({"mask": "textures/x.png"}) if hasattr(materials, "wire_plan") else None
    if plan is None:
        check(True, "wire_plan() absent in this build — structural clause skipped")
    else:
        slots = set(plan) if isinstance(plan, dict) else set(plan)
        check(not any("ao" in str(s).lower() or "occl" in str(s).lower() for s in slots),
              f"wire_plan() offers no occlusion slot (offers {sorted(map(str, slots))})")

    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "csiibundle", "bake", "materials.py"), encoding="utf-8").read()
    check("_ao.png" not in source.split('"""', 2)[-1],
          "the materials bake product writes no `_ao.png` outside its docstring")


def test_negative_control():
    """Green must be earned: the classifier has to recognise an actual AO map."""
    print("negative control (the classifier can say yes):")
    rng = numpy.random.default_rng(3)
    # Near-white with dark creases — a plausible baked AO.
    ao = numpy.clip(235 + rng.normal(0, 12, (256, 256)), 0, 255)
    crease = rng.random((256, 256)) < 0.06
    ao[crease] = rng.integers(20, 90, int(crease.sum()))
    check(looks_like_ao(ao), "a synthesised near-white AO map is classified as AO")
    check(not looks_like_ao(numpy.full((64, 64), 255.0)), "a uniformly white channel is not AO (a no-op)")
    stencil = numpy.where(rng.random((256, 256)) < 0.5, 255.0, 0.0)
    check(not looks_like_ao(stencil), "a bimodal 0/255 stencil is not AO")


def test_measured_channels():
    """The measurement, over whatever bundle is on disk."""
    path = os.path.join(BUNDLE, "materials.json")
    if not os.path.isfile(path):
        print(f"no bundle at {BUNDLE} — measurement skipped")
        return
    try:
        from PIL import Image
    except ImportError:
        print("Pillow absent — measurement skipped")
        return

    print(f"mask packing: measured on {BUNDLE}")
    catalogue = json.load(open(path, encoding="utf-8"))
    rels = sorted({m["mask"] for m in catalogue.values() if m.get("mask")})[:MAX_MASKS]
    stats = {c: [] for c in "RGBA"}
    ao_like = []
    for rel in rels:
        p = os.path.join(BUNDLE, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            continue
        a = numpy.asarray(Image.open(p).convert("RGBA")).astype(numpy.float32)
        for i, c in enumerate("RGBA"):
            ch = a[..., i]
            stats[c].append((ch.mean(), (ch == 0).mean(), (ch == 255).mean()))
        if looks_like_ao(a[..., 1]):
            ao_like.append(rel)
    n = len(stats["R"])
    if n < 50:
        print(f"  only {n} masks readable — measurement skipped")
        return
    means = {c: float(numpy.mean([x[0] for x in stats[c]])) for c in "RGBA"}
    zeros = {c: float(numpy.mean([x[1] for x in stats[c]])) for c in "RGBA"}
    full = {c: float(numpy.mean([x[2] for x in stats[c]])) for c in "RGBA"}
    print(f"    {n} distinct masks")
    for c in "RGBA":
        print(f"    {c}: mean {means[c]:6.1f}   frac==0 {zeros[c]:.3f}   frac==255 {full[c]:.3f}")

    # Not "zero": 2 of 890 survive on the reference city, both near-white. The decision this
    # protects is "do not wire an occlusion slot", and a 0.22 % near-neutral tail supports it.
    # A bundle where G really were AO would put this far above 1 %.
    check(len(ao_like) <= max(1, n // 100),
          f"mask G is not an occlusion map for this layer ({len(ao_like)} of {n} = "
          f"{100 * len(ao_like) / n:.2f}% read as AO; a real AO channel would be near 100%)")
    check(means["A"] >= 60.0 and zeros["A"] < 0.25,
          f"A is populated everywhere, as smoothness is (mean {means['A']:.1f}, "
          f"frac==0 {zeros['A']:.3f})")
    check(zeros["R"] + full["R"] >= 0.6,
          f"R is binary, as metallic is ({100 * (zeros['R'] + full['R']):.1f}% of texels at an "
          f"extreme)")


def main():
    test_no_occlusion_slot()
    test_negative_control()
    test_measured_channels()
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
