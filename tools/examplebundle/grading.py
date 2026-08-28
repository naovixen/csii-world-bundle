"""The example bundle's colour grading and lighting document (contract §8).

A DISPLAY TRANSFORM, never baked into the shipped textures — and a snapshot of one moment,
because grading follows weather and time of day. `diagnostics` is what tells a consumer this
is the game's real grade rather than an unevaluated stack of defaults.
"""

from __future__ import annotations

import json
from pathlib import Path


def _grading_blocks() -> dict:
    """The grading chain itself: exposure through tonemapping."""
    return {
        "diagnostics": {
            "stackEvaluated": True,
            "volumesFound": 3,
            "camera": "Main Camera",
            "volumeLayerMask": -1,
            "unread": ["CloudLayer", "VolumetricClouds"],
        },
        "exposure": {
            "mode": "AutomaticHistogram",
            "fixedExposure": 0.0,
            "compensation": 0.0,
            "limitMin": -2.0,
            "limitMax": 14.0,
            "units": "EV100; `compensation` is stops",
        },
        "whiteBalance": {
            "temperature": 8.0,
            "tint": 0.0,
            "units": "both -100..100, HDRP's own scale",
        },
        "colorAdjustments": {
            "postExposure": 0.0,
            "contrast": 12.0,
            "colorFilter": [1.0, 1.0, 1.0],
            "hueShift": 0.0,
            "saturation": 8.0,
            "units": "postExposure in EV/stops; the rest on HDRP's -100..100",
        },
        "shadowsMidtonesHighlights": {
            "shadows": [1.0, 1.0, 1.02, 0.0],
            "midtones": [1.0, 1.0, 1.0, 0.0],
            "highlights": [1.02, 1.0, 0.98, 0.0],
            "shadowsStart": 0.0,
            "shadowsEnd": 0.3,
            "highlightsStart": 0.55,
            "highlightsEnd": 1.0,
            "units": "each is [r, g, b, w]: rgb a per-channel multiplier around 1.0, w an offset",
        },
        "tonemapping": {
            "mode": "ACES",
            "note": "the display transform the grading above was authored against",
            "lutContribution": 1.0,
            "hasLut": False,
        },
    }


def _lighting_block() -> dict:
    """The illumination the grading sits on top of — sun, sky, indirect, and the clock."""
    return {
        "semantics": "the illumination the grading above sits on top of. A consumer that "
        "reproduces the albedo and the grading but lights the scene white will "
        "still miss the game's colour: the sun is not white",
        "sun": {
            "color": [1.0, 1.0, 1.0],
            "useColorTemperature": False,
            "colorTemperature": 6570.0,
            "units": "`color` is the light's own filter as authored",
            "intensity": 110000.0,
            "initialIntensity": 130000.0,
            "intensityUnits": "lux for a directional light",
            "direction": [-0.35, -0.91, 0.22],
            "elevationDegrees": 66.0,
            "azimuthDegrees": 122.0,
            "directionUnits": "`direction` is the unit vector the light TRAVELS along",
            "shadows": "Soft",
            "enabled": True,
            "which": "sun",
        },
        "sky": {
            "exposure": 0.0,
            "zenithTint": [0.42, 0.56, 0.86],
            "horizonTint": [0.78, 0.82, 0.88],
            "groundTint": [0.28, 0.26, 0.24],
            "units": "HDRP PhysicallyBasedSky; this is the ambient half of the light",
        },
        "indirect": {
            "diffuseMultiplier": 1.0,
            "reflectionMultiplier": 1.0,
            "units": "multipliers on bounced light",
        },
        "time": {
            "hour": 13,
            "minute": 20,
            "day": 172,
            "year": 2026,
            "latitude": 25.033,
            "longitude": 121.5654,
            "units": "in-game clock and map position at rip time",
        },
    }


def write_grading(out_dir: Path) -> None:
    document = {
        "semantics": "the game's resolved HDRP colour grading at rip time, read from "
        "VolumeManager's post-blend volume stack. CS2's ground albedo is authored "
        "near-grey and this is what turns it into the on-screen palette. Optional: "
        "a consumer that wants the raw albedo should ignore this block entirely; "
        "one that wants the game's look should apply it as a display transform, "
        "NOT bake it into the shipped textures",
        "order": "exposure -> white balance -> color adjustments (filter, contrast, hue, "
        "saturation) -> shadows/midtones/highlights -> tonemapping",
        "weatherDependent": True,
        "note": "a snapshot of one moment: grading follows weather and time of day, so a rip "
        "taken at dusk records dusk",
    }
    document.update(_grading_blocks())
    document["lighting"] = _lighting_block()
    (out_dir / "grading.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
