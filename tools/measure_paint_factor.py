"""How much fidelity a per-variant `baseColorFactor` costs against a tinted albedo (A4 §2.4).

A forest item carries no per-instance colour, so CS2's §7.2 palette tint has to be baked: the
`materials` bake writes one tinted albedo per (source albedo, colour-set centre). At 1.33 MiB
each that is what made `paint_kinds=tree,prop` unaffordable — ~7,700 textures.

The tint is a **pure per-texel multiplier**. `tints.apply_reference` chains
`base = lerp(base, base*c_i, mask_i)` over the three channels, and each step is
`base * ((1 - w_i) + w_i * c_i)`, so

    tinted(texel) = base(texel) * M(texel),   M = prod_i ((1 - w_i) + w_i * c_i)

with `M` depending only on the colour mask and the centre — never on the albedo. A single
`baseColorFactor` per variant replaces `M(texel)` by one constant, which costs **zero textures**.
This tool measures what that costs in pixels, so the threshold is chosen from the distribution
rather than guessed:

    python tools/measure_paint_factor.py [bundle] [--limit N] [--json out.json]

Reported per (material, variant), in 8-bit sRGB over the texels the albedo actually shows
(alpha > 0): the max and the 99th-percentile absolute channel error, and the fraction of texels
the mask touches at all. Nothing is written into the bundle.
"""

import argparse
import json
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from csiibundle import png, tints  # noqa: E402
from csiibundle.bake.locate import default_bundle_dir  # noqa: E402


def srgb_to_linear(arr):
    a = numpy.asarray(arr, dtype=numpy.float64) / 255.0
    return numpy.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def linear_to_srgb8(arr):
    a = numpy.clip(arr, 0.0, 1.0)
    s = numpy.where(a <= 0.0031308, a * 12.92, 1.055 * numpy.power(a, 1 / 2.4) - 0.055)
    return numpy.clip(numpy.rint(s * 255.0), 0, 255).astype(numpy.uint8)


def resample_nearest(img, h, w):
    if img.shape[0] == h and img.shape[1] == w:
        return img
    rows = numpy.clip((numpy.arange(h) * img.shape[0] / h).astype(numpy.int64), 0, img.shape[0] - 1)
    cols = numpy.clip((numpy.arange(w) * img.shape[1] / w).astype(numpy.int64), 0, img.shape[1] - 1)
    return img[rows][:, cols]


def multiplier(mask_rgb, colours):
    """`M(texel)` — the per-texel RGB multiplier `tints.apply_reference` amounts to."""
    out = numpy.ones((*mask_rgb.shape[:2], 3), dtype=numpy.float64)
    for i in range(tints.CHANNELS):
        w = mask_rgb[..., i][..., None]
        c = numpy.asarray(colours[i][:3], dtype=numpy.float64)
        out = out * ((1.0 - w) + w * c)
    return out


def best_factor(m, weight):
    """The constant that minimises the weighted squared error against `M` per channel."""
    w = weight[..., None]
    denom = w.sum()
    if denom <= 0:
        return m.reshape(-1, 3).mean(axis=0)
    return (m * w).reshape(-1, 3).sum(axis=0) / float(denom)


def measure(bundle, derived, limit=None, progress=True):
    with open(os.path.join(derived, "materials.json"), encoding="utf-8") as handle:
        table = json.load(handle)
    with open(os.path.join(derived, "paint_variants.json"), encoding="utf-8") as handle:
        variants_doc = json.load(handle)
    # variantKey -> the centre it was clustered at (the bake keys the texture by a sha of it)
    import hashlib

    centre_of = {}
    for entry in (variants_doc.get("prefabs") or {}).values():
        for centre in entry.get("variants") or []:
            key = hashlib.sha256(json.dumps(centre).encode("utf-8")).hexdigest()[:12]
            centre_of.setdefault(key, centre)

    rows = []
    materials = table.get("materials") or {}
    todo = [
        (name, rec)
        for name, rec in sorted(materials.items())
        if ((rec.get("derived") or {}).get("variants"))
    ]
    if limit:
        todo = todo[: int(limit)]
    for index, (name, rec) in enumerate(todo):
        base_path = os.path.join(bundle, rec.get("baseColor") or "")
        mask_path = os.path.join(bundle, rec.get("colorMask") or "")
        if not (os.path.isfile(base_path) and os.path.isfile(mask_path)):
            continue
        try:
            base = png.rgba_array(base_path)
            mask = png.rgba_array(mask_path)
        except Exception as exc:
            print(f"  {name}: unreadable ({exc!r})", file=sys.stderr)
            continue
        h, w = base.shape[:2]
        factor_down = 1
        while max(h, w) // factor_down > 512:  # the error is a per-texel statistic; 512 is plenty
            factor_down *= 2
        if factor_down > 1:
            base = png.downsample_box(base, factor_down)
            h, w = base.shape[:2]
        mask_r = resample_nearest(mask, h, w).astype(numpy.float64) / 255.0
        base_lin = srgb_to_linear(base[:, :, :3])
        alpha = base[:, :, 3].astype(numpy.float64) / 255.0
        touched = float((mask_r[:, :, :3].max(axis=2) > 1.0 / 255.0).mean())
        for vkey in sorted((rec.get("derived") or {}).get("variants") or {}):
            centre = centre_of.get(vkey)
            if centre is None:
                continue
            colours = [(*tuple(centre[i]), 1.0) for i in range(3)]
            m = multiplier(mask_r[:, :, :3], colours)
            exact8 = linear_to_srgb8(base_lin * m).astype(numpy.int16)
            factor = best_factor(m, alpha)
            approx8 = linear_to_srgb8(base_lin * factor[None, None, :]).astype(numpy.int16)
            err = numpy.abs(exact8 - approx8)
            visible = alpha > 0.5
            if visible.any():
                err = err[visible]
            flat = err.reshape(-1)
            rows.append(
                {
                    "material": name,
                    "variant": vkey,
                    "factor": [round(float(v), 5) for v in factor],
                    "maxErr": int(flat.max()) if flat.size else 0,
                    "p99Err": int(numpy.percentile(flat, 99)) if flat.size else 0,
                    "meanErr": round(float(flat.mean()), 3) if flat.size else 0.0,
                    "maskedFraction": round(touched, 4),
                }
            )
        if progress and (index + 1) % 25 == 0:
            print(f"  {index + 1}/{len(todo)} materials", file=sys.stderr)
    return rows


def summarise(rows):
    if not rows:
        return {"pairs": 0}
    max_errs = sorted(r["maxErr"] for r in rows)
    p99s = sorted(r["p99Err"] for r in rows)

    def pct(values, q):
        return values[min(len(values) - 1, int(q * len(values)))]

    out = {
        "pairs": len(rows),
        "materials": len({r["material"] for r in rows}),
        "maxErr": {
            "median": pct(max_errs, 0.5),
            "p90": pct(max_errs, 0.9),
            "p99": pct(max_errs, 0.99),
            "worst": max_errs[-1],
        },
        "p99Err": {"median": pct(p99s, 0.5), "p90": pct(p99s, 0.9), "worst": p99s[-1]},
    }
    for threshold in (2, 4, 8, 16, 32):
        under = sum(1 for r in rows if r["maxErr"] <= threshold)
        out[f"underMax{threshold}"] = {"pairs": under, "fraction": round(under / len(rows), 4)}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", nargs="?", default=None)
    parser.add_argument("--derived", default=None)
    parser.add_argument("--limit", type=int, default=None, help="only the first N materials")
    parser.add_argument("--json", default=None, help="write the per-pair rows here")
    args = parser.parse_args(argv)
    bundle = os.path.abspath(args.bundle or default_bundle_dir())
    derived = os.path.abspath(args.derived or os.path.join(bundle, "derived"))
    rows = measure(bundle, derived, args.limit)
    summary = summarise(rows)
    print(json.dumps(summary, indent=1, sort_keys=True))
    worst = sorted(rows, key=lambda r: -r["maxErr"])[:10]
    print("\nworst 10 (material, variant, maxErr, p99, maskedFraction):")
    for r in worst:
        print(
            f"  {r['material'][:52]:52} {r['variant']} {r['maxErr']:4d} {r['p99Err']:4d} {r['maskedFraction']:.3f}"
        )
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"summary": summary, "rows": rows}, handle, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
