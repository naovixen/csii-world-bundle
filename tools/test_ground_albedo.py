"""Gate for the ground-albedo gain target — audit §55.

WHY A GAIN EXISTS. `Game.Rendering.LightingSystem.SetupPostprocessing` puts HDRP in
`ExposureMode.Automatic`, and `DayNightCycleData` clamps it to `[DayExposureMin, DayExposureMax]`
= [4, 14] EV by day — the same pair the producer already ships in `grading.json`. CS2 meters the
scene every frame and normalises it, which is what lets its ground albedos be authored dark (grass
~0.06 linear) under a 110 klux sun. HDRP does not expose the metered value, so absolute brightness
cannot be replayed, and BeamNG has no equivalent metering on terrain. A fixed linear gain on the
albedo is the right shape of answer and is declared as an approximation.

WHAT THIS MEASURES. The gain's target is not a matter of taste and it is not CS2's to set: its job
is to land CS2's grounds where BeamNG's own lighting was authored to receive them, so the authority
is BeamNG's shipped content. This file measures that content directly — the vanilla levels are zips
under `content/levels/`, each with `*.materials.json` naming its `TerrainMaterial` base-colour maps.

Measured 2026-08-17 over 149 ground albedos in 15 shipped levels: mean linear luminance 0.2085,
median 0.2196, p10 0.1386, p90 0.2344; the 26 grass-named ones (what the constant calibrates on)
mean 0.2147, median 0.2185.

WHY IT DOES NOT ENFORCE THAT NUMBER. `GRASS_TARGET_LUMINANCE` is deliberately **held at 0.16**,
27 % below the anchor (audit §55, reverted 2026-08-17). The build that shipped 0.2185 drew black
and cream slabs on the cut faces beside deep terrain, and the cause was never established — the
larger gain amplifies the base/detail mismatch on UV-stretched faces by 1.366x, so it plausibly
made a pre-existing defect louder without being its cause (the defect is §60's planar cut-mesh UVs:
217.5 m/uv on vertical walls against 40.0 on flat ones).

So this gate reports the gap on every run and asserts only that the shipped value is the one that
was DECIDED. Changing the constant without changing this file is what it prevents; agreeing with
BeamNG is what it measures and, for now, deliberately does not require.

Run: `python tools/test_ground_albedo.py`  (system Python + numpy + Pillow; skipped when BeamNG is
not installed, so it is advisory on a machine without the game and binding on one with it).
"""

import io
import json
import os
import sys
import zipfile

import numpy

LEVELS = (
    os.environ.get("BEAMNG_LEVELS")
    or r"G:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels"
)

# The value the app deliberately ships, and the only thing this file asserts. Held below the
# measured anchor pending §60 — see the module docstring. Change one and you must change the
# other, which is the point.
DECIDED_TARGET = 0.16

# How far from BeamNG's own grass median counts as "diverged" for the REPORT (not a failure).
# 0.03 is about a fifth of a stop at this level.
TOLERANCE = 0.03

FAILED = []


def check(ok, label):
    print(("  ok    " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def linear_luminance(rgb):
    """Rec.709 luminance of an sRGB-ish 8-bit image, in linear light.

    The same 2.2 approximation `stages/terraintextures.py::_linear_luminance` uses, deliberately — the
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
        for member in (
            n for n in names if n.endswith(".materials.json") and "terrain" in n.lower()
        ):
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
            except Exception:
                continue
            image.thumbnail((256, 256))
            rows.append(
                (name[:-4], os.path.basename(found[0]), linear_luminance(numpy.asarray(image)))
            )
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

    from cs2beamng.stages import terraintextures as textures

    rows = vanilla_ground_albedos()
    print(f"BeamNG vanilla ground albedos: {len(rows)} across {len({r[0] for r in rows})} levels")
    if len(rows) < 40:
        print("  too few readable to judge — skipped")
        return 0
    allv = numpy.array([r[2] for r in rows])
    grass = numpy.array([r[2] for r in rows if "grass" in r[1].lower()])
    print(
        f"  all grounds : mean {allv.mean():.4f}  median {numpy.median(allv):.4f}"
        f"  p10 {numpy.percentile(allv, 10):.4f}  p90 {numpy.percentile(allv, 90):.4f}"
    )
    if len(grass):
        print(
            f"  grass-named : n={len(grass)}  mean {grass.mean():.4f}"
            f"  median {numpy.median(grass):.4f}"
        )

    target = float(textures.GRASS_TARGET_LUMINANCE)
    anchor = float(numpy.median(grass)) if len(grass) >= 5 else float(numpy.median(allv))
    gap = abs(target - anchor)
    print(
        f"  GRASS_TARGET_LUMINANCE = {target:.4f}   BeamNG's own anchor = {anchor:.4f}"
        f"   gap {gap:.4f}"
    )
    if gap > TOLERANCE:
        print(
            f"    DIVERGED by more than {TOLERANCE} - deliberate, pending audit section 60 "
            f"(planar cut-mesh UVs). Reported, not failed."
        )

    # The one assertion: the shipped value is the decided one. This is what stops the constant
    # drifting silently while the docstring above still explains a different number.
    check(
        abs(target - DECIDED_TARGET) < 1e-9,
        f"GRASS_TARGET_LUMINANCE is the decided {DECIDED_TARGET} (reads {target})",
    )
    # The measurement itself must stay live, or the number above is unfalsifiable.
    check(
        0.05 <= anchor <= 0.5,
        f"BeamNG's vanilla ground anchor re-measured this run ({anchor:.4f} from "
        f"{len(grass) if len(grass) >= 5 else len(allv)} textures)",
    )
    check(
        hasattr(textures, "MEASURED_GRASS_ANCHOR")
        and abs(float(textures.MEASURED_GRASS_ANCHOR) - anchor) <= TOLERANCE,
        f"the app records the measured anchor beside the shipped value "
        f"(MEASURED_GRASS_ANCHOR = {getattr(textures, 'MEASURED_GRASS_ANCHOR', None)})",
    )

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


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_ground_albedo.py`."""
    assert main() in (None, 0)
