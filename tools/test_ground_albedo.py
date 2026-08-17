"""Gate for the ground-albedo gain target — audit §55.

WHY A GAIN EXISTS. `Game.Rendering.LightingSystem.SetupPostprocessing` puts HDRP in
`ExposureMode.Automatic`, and `DayNightCycleData` clamps it to `[DayExposureMin, DayExposureMax]`
= [4, 14] EV by day — the same pair the producer already ships in `grading.json`. CS2 meters the
scene every frame and normalises it, which is what lets its ground albedos be authored dark (grass
~0.06 linear) under a 110 klux sun. HDRP does not expose the metered value, so absolute brightness
cannot be replayed, and BeamNG has no equivalent metering on terrain. A fixed linear gain on the
albedo is the right shape of answer and is declared as an approximation.

WHAT THIS GATES. The gain's TARGET is not a matter of taste and it is not CS2's to set: its job is
to land CS2's grounds where BeamNG's own lighting was authored to receive them, so the authority is
BeamNG's shipped content. This file measures that content directly — the vanilla levels are zips
under `content/levels/`, each with `*.materials.json` naming its `TerrainMaterial` base-colour maps
— and fails if `GRASS_TARGET_LUMINANCE` has drifted away from it.

Measured 2026-08-17 over 149 ground albedos in 15 shipped levels: mean linear luminance 0.2085,
median 0.2196, p10 0.1386, p90 0.2344; the 26 grass-named ones (what the constant calibrates on)
mean 0.2147, median 0.2185. The constant was 0.16 — a guess, 27 % below BeamNG's own grass.

Run: `python tools/test_ground_albedo.py`  (system Python + numpy + Pillow; skipped when BeamNG is
not installed, so it is advisory on a machine without the game and binding on one with it).
"""
import io
import json
import os
import sys
import zipfile

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps"))

LEVELS = os.environ.get("BEAMNG_LEVELS") or \
    r"G:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels"

# How far the constant may sit from BeamNG's own grass median before this is a finding.
# CHOSEN SO THE VALUE THIS GATE REPLACED FAILS IT: the old tuned 0.16 was 0.0585 below the
# measured anchor, so a tolerance of 0.07 would have called it healthy and the gate would have
# been decoration. 0.03 is about a fifth of a stop at this level — tight enough to catch a guess,
# loose enough that re-measuring on a different BeamNG version does not thrash.
TOLERANCE = 0.03

# The constant this gate replaced, kept as its negative control (see `test_negative_control`).
SUPERSEDED_TARGET = 0.16

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def linear_luminance(rgb):
    """Rec.709 luminance of an sRGB-ish 8-bit image, in linear light.

    The same 2.2 approximation `stages/terrain.py::_linear_luminance` uses, deliberately — the
    two numbers are only comparable if they are computed the same way.
    """
    lin = (rgb.astype(numpy.float64) / 255.0) ** 2.2
    return float((0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).mean())


def vanilla_ground_albedos():
    """(level, texture, linear luminance) for every vanilla `TerrainMaterial` base colour."""
    from PIL import Image
    rows = []
    if not os.path.isdir(LEVELS):
        return rows
    for name in sorted(os.listdir(LEVELS)):
        if not name.endswith(".zip"):
            continue
        try:
            zf = zipfile.ZipFile(os.path.join(LEVELS, name))
        except (OSError, zipfile.BadZipFile):
            continue
        names = zf.namelist()
        wanted = set()
        for member in (n for n in names if n.endswith(".materials.json") and "terrain" in n.lower()):
            try:
                doc = json.loads(zf.read(member).decode("utf-8-sig"))
            except (ValueError, OSError, KeyError):
                continue
            if not isinstance(doc, dict):
                continue
            for entry in doc.values():
                if isinstance(entry, dict) and entry.get("class") == "TerrainMaterial":
                    for key, value in entry.items():
                        if isinstance(value, str) and "basecolor" in key.lower():
                            wanted.add(value.lstrip("/"))
        for rel in sorted(wanted):
            leaf = rel.lower().split("/")[-1]
            found = [n for n in names if n.lower().endswith(leaf)]
            if not found:
                continue
            try:
                image = Image.open(io.BytesIO(zf.read(found[0]))).convert("RGB")
            except Exception:  # noqa: BLE001 - one unreadable texture must not stop the sweep
                continue
            image.thumbnail((256, 256))
            rows.append((name[:-4], os.path.basename(found[0]), linear_luminance(numpy.asarray(image))))
    return rows


def main():
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow absent — skipped")
        return 0
    if not os.path.isdir(LEVELS):
        print(f"BeamNG not installed at {LEVELS} — skipped")
        return 0

    from cs2beamng.stages import terrain as stage

    rows = vanilla_ground_albedos()
    print(f"BeamNG vanilla ground albedos: {len(rows)} across "
          f"{len(set(r[0] for r in rows))} levels")
    if len(rows) < 40:
        print("  too few readable to judge — skipped")
        return 0
    allv = numpy.array([r[2] for r in rows])
    grass = numpy.array([r[2] for r in rows if "grass" in r[1].lower()])
    print(f"  all grounds : mean {allv.mean():.4f}  median {numpy.median(allv):.4f}"
          f"  p10 {numpy.percentile(allv, 10):.4f}  p90 {numpy.percentile(allv, 90):.4f}")
    if len(grass):
        print(f"  grass-named : n={len(grass)}  mean {grass.mean():.4f}"
              f"  median {numpy.median(grass):.4f}")

    target = float(stage.GRASS_TARGET_LUMINANCE)
    anchor = float(numpy.median(grass)) if len(grass) >= 5 else float(numpy.median(allv))
    print(f"  GRASS_TARGET_LUMINANCE = {target:.4f}   BeamNG's own anchor = {anchor:.4f}")
    check(abs(target - anchor) <= TOLERANCE,
          f"the gain target tracks BeamNG's own grass (|{target:.4f} - {anchor:.4f}| = "
          f"{abs(target - anchor):.4f} <= {TOLERANCE})")
    # The target must sit inside the range the engine's own content occupies, not merely near a
    # single statistic of it.
    check(numpy.percentile(allv, 10) <= target <= numpy.percentile(allv, 90),
          f"the target lies inside the p10-p90 band of vanilla ground albedo "
          f"({numpy.percentile(allv, 10):.4f}..{numpy.percentile(allv, 90):.4f})")

    # Green must be earned: the guess this replaced has to fail the same clause.
    print("negative control (the gate convicts the value it replaced):")
    check(abs(SUPERSEDED_TARGET - anchor) > TOLERANCE,
          f"the superseded 0.16 is convicted (|{SUPERSEDED_TARGET:.4f} - {anchor:.4f}| = "
          f"{abs(SUPERSEDED_TARGET - anchor):.4f} > {TOLERANCE})")

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
