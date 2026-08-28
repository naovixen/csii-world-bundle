"""What THIS MACHINE can hold, and what it is set up to draw with.

The other half of the budget question. `beamng/budget.py` measures what a level COSTS; this says
what a card at a resolution is known to be able to mount, where the measured failure line is, and
which renderer BeamNG is actually configured for. It lives apart from the level validator because
its callers mostly are not validating a level: the settings page's "target machine" line, the
cost estimate and `doctor` all ask these questions with no level in hand.

Every number here is measured or declared. The one estimate is marked as such where it is made.
"""

import os
import re

VANILLA_MAX_RESIDENT_MIB = 1375  # west_coast_usa, measured 2026-08-18 (BeamNG 0.39)
BEAMNG_DX12_MIN_VRAM_MIB = 5632  # the engine's own printed "5.50 GB minimum"
REFERENCE_PIXELS = 1920 * 1080  # the resolution the vanilla anchor is quoted at

# DECLARED APPROXIMATION — this one term is an estimate, not a measurement. BeamNG's deferred
# targets are named in `lua/ge/client` (`deferred`, `matinfo`, `lightinfo`, `ssao`, velocity,
# backBuffer, the post chain's own) but their FORMATS are set in the native renderer and are not
# readable from the install, so their exact bytes per pixel cannot be read off disk. 100 MiB per
# megapixel is roughly a dozen full-resolution targets averaging four bytes a pixel plus the
# half-resolution chains. It is second order here and deliberately so: it moves the budget by
# 288 MiB across the whole span from 1080p to 3440x1440, so being wrong by a factor of two moves
# it less than the texture cap does. If it ever stops being second order, measure it.
RENDER_TARGET_MIB_PER_MPIXEL = 100.0

# The reference machine, used when the caller does not say. Not a recommendation — it is this
# project's own test card (RTX 3070, 8 GB) at the resolution the vanilla anchor is quoted at.
REFERENCE_VRAM_MIB = 8018


def resident_budget_mib(vram_mib=REFERENCE_VRAM_MIB, pixels=REFERENCE_PIXELS):
    """What a level is KNOWN to be able to mount on a `vram_mib` card at `pixels` pixels, in MiB.

    This is a LOWER bound on the true ceiling, and deliberately so. `west_coast_usa` mounting
    1,375 MiB on a card at the engine's minimum proves that at least 1,375 MiB fits there — not
    that 1,375 MiB is all that fits. BeamNG does not size its levels to the limit of its own
    minimum spec, so the real ceiling is somewhere above what this returns.

    The other end is known too, and `resident_failure_mib` is it: 5,224 MiB on an 8,018 MiB card
    at 3440x1440 failed hard. So for that machine the true ceiling is somewhere in
    [3,473, 5,224) MiB, and the only way to narrow it is a load that reports zero failed
    allocations. Build under this and it fits; between this and the failure line it is unknown.
    """
    headroom = max(0.0, float(vram_mib) - BEAMNG_DX12_MIN_VRAM_MIB)
    extra_mpixels = (float(pixels) - REFERENCE_PIXELS) / 1.0e6
    budget = VANILLA_MAX_RESIDENT_MIB + headroom - RENDER_TARGET_MIB_PER_MPIXEL * extra_mpixels
    # a card below the engine's own minimum still has to hold SOMETHING; the floor is well under
    # vanilla's median rather than zero, and the note that follows will be loud either way
    return max(384.0, budget)


# The other end of the bracket: what is KNOWN to fail, scaled the same way. A5's `cs2_city_fine`
# mounted 5,224 MiB on an 8,018 MiB card at 3440x1440 and D3D12 failed 131,073 texture creates in
# one session. Anything at or above this on an equivalent machine is not a risk, it is the
# measured failure. MEASURED, one point — narrow it by loading and reading `totalFailures`.
FAILURE_RESIDENT_MIB = 5224
FAILURE_VRAM_MIB = 8018
FAILURE_PIXELS = 3440 * 1440


def resident_failure_mib(vram_mib=REFERENCE_VRAM_MIB, pixels=REFERENCE_PIXELS):
    """The measured failure point, moved to another card and resolution on the same terms."""
    delta_vram = float(vram_mib) - FAILURE_VRAM_MIB
    delta_mpixels = (float(pixels) - FAILURE_PIXELS) / 1.0e6
    return max(
        resident_budget_mib(vram_mib, pixels) + 1.0,
        FAILURE_RESIDENT_MIB + delta_vram - RENDER_TARGET_MIB_PER_MPIXEL * delta_mpixels,
    )


def _texture_reduction():
    """`(level, source)` — BeamNG's own `textureReductionLevel`, which DIVIDES texture VRAM by 4^n.

    Read from source (`lua/ge/extensions/core/settings/textureQuality.lua`):

        Lowest -> textureReductionLevel = 2      (two mips dropped: 1/16 the bytes)
        Low    -> 1                              (1/4)
        Normal and above -> 0                    (as shipped)

    **This is the variable the resident budget was blind to.** `_resident_bytes` counts what the
    level holds ON DISK, and a level shipping 717 MiB of textures occupies ~45 MiB of them at
    Lowest and all 717 at Normal. So the same level has two footprints an order of magnitude
    apart depending on a setting the budget never read — which is why one session survived 20
    minutes and the next started failing 47 seconds after loading with nothing changed in the
    level at all.

    `onApply` calls `reloadTextures()`, so CHANGING it mid-session reloads every texture: that is
    a VRAM spike, and it is what preceded the failures on 2026-08-19, not the pause menu.
    """
    base = os.path.join(os.path.expanduser("~"), "AppData", "Local", "BeamNG", "BeamNG.drive")
    if not os.path.isdir(base):
        return 0, "assumed Normal (no BeamNG userfolder)"
    newest = None
    for entry in os.listdir(base):
        path = os.path.join(base, entry, "settings", "game-settings.json")
        if os.path.isfile(path) and (
            newest is None or os.path.getmtime(path) > os.path.getmtime(newest)
        ):
            newest = path
    if newest is None:
        return 0, "assumed Normal (no game-settings.json)"
    try:
        with open(newest, encoding="utf-8", errors="replace") as handle:
            found = re.search(r'"textureReductionLevel"\s*:\s*(\d+)', handle.read())
    except OSError:
        return 0, "assumed Normal (game-settings.json unreadable)"
    if not found:
        return 0, "assumed Normal (not set)"
    level = int(found.group(1))
    return level, f"textureReductionLevel {level} " + {
        0: "(Normal+)",
        1: "(Low)",
        2: "(Lowest)",
    }.get(level, "(custom)")


def light_manager():
    """`(name, source)` — the lighting path the user's BeamNG is set to render with.

    **A local light does nothing at all under `Basic Lighting`, and the level cannot tell.**
    BeamNG has two lighting managers and the preference names which is live. Advanced Lighting is
    the deferred path: a z+normals prepass into a g-buffer, which every local point and spot light
    is shaded from, and which the clustered pipeline (frustum/size/occlusion/importance culling,
    flare substitution) sits on top of. Basic Lighting has no prepass at all — the engine's own
    documentation says so, twice: *"With Basic Lighting, we do not have a prepass texture to
    lookup per-pixel depth and therefore cannot use our rendering techniques that depend on it"*,
    and a material field is described as *"determines if the material should receive lights in
    Basic Lighting. Has no effect in Advanced Lighting."*

    So on a Basic Lighting profile our 25,000 SpotLights are 13 MiB of scene graph and ~5 s of
    load that can never light one pixel. That is not a defect in the lights and no amount of
    photometry fixes it — it is the wrong renderer for them, and the only thing the build can
    honestly do is SAY SO before the load rather than after it (2026-08-19, audit §82).

    `$pref::lightManager` and `$pref::Reflect::deferredLighting` both carry it; either being set
    to the basic path is enough.
    """
    base = os.path.join(os.path.expanduser("~"), "AppData", "Local", "BeamNG", "BeamNG.drive")
    if not os.path.isdir(base):
        return None, "no BeamNG userfolder"
    newest = None
    for entry in os.listdir(base):
        path = os.path.join(base, entry, "settings", "game-settings.json")
        if os.path.isfile(path) and (
            newest is None or os.path.getmtime(path) > os.path.getmtime(newest)
        ):
            newest = path
    if newest is None:
        return None, "no game-settings.json"
    try:
        with open(newest, encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None, "game-settings.json unreadable"
    found = re.search(r'"lightManager"\s*:\s*"([^"]+)"', raw)
    deferred = re.search(r'"deferredLighting"\s*:\s*(true|false)', raw, re.I)
    name = found.group(1) if found else None
    if name is None and deferred is not None:
        name = "Advanced Lighting" if deferred.group(1).lower() == "true" else "Basic Lighting"
    return name, "game-settings.json"


def render_target(settings=None):
    """`(vram_mib, pixels, source)` — the machine the budget is derived for.

    Settings win when set; otherwise the numbers are READ, not assumed. BeamNG prints both at
    startup, in the logs the user's own sessions leave behind:

        Dedicated video memory: 8018 MB
        AntialiasLevel: 0, ... Resolution: 3440x1440, wideScreen: false

    which is the difference between a budget for this machine and a budget for a machine we
    imagined. Both are MiB despite the "MB" — the same line's byte count, 8407482368, is 8018
    times 1024 squared exactly.

    Reads the ROTATED logs too, newest first, and takes each number from the newest log that
    actually carries it. A session that started and quit without opening a window prints the
    VRAM and never prints a resolution — that happened on 2026-08-19 07:49 and the budget
    silently fell back to 1080p while still claiming it had read the log, which is worse than
    not reading it. `source` now names each half separately, so a fallback says so.
    """
    settings = settings or {}
    vram = int(settings.get("gpu_vram_mib") or 0)
    width = int(settings.get("render_width") or 0)
    height = int(settings.get("render_height") or 0)
    pixels = width * height
    if vram and pixels:
        return vram, pixels, "from settings"

    log_vram, log_pixels = _target_from_logs()
    parts = []
    if vram:
        parts.append("card from settings")
    elif log_vram:
        vram = log_vram
        parts.append("card from beamng.log")
    else:
        vram = REFERENCE_VRAM_MIB
        parts.append("REFERENCE card, no beamng.log")
    if pixels:
        parts.append("resolution from settings")
    elif log_pixels:
        pixels = log_pixels
        parts.append("resolution from beamng.log")
    else:
        pixels = REFERENCE_PIXELS
        parts.append("REFERENCE 1080p, no resolution logged")
    return vram, pixels, ", ".join(parts)


def _target_from_logs():
    """`(vram_mib, pixels)` read out of the BeamNG logs, either half `None` if never printed.

    Newest log first, and each half taken from the newest log that actually carries it — a
    session that started and quit without opening a window prints the VRAM and never prints a
    resolution.
    """
    log_vram = log_pixels = None
    for path in _beamng_logs():
        if log_vram is not None and log_pixels is not None:
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if log_vram is None:
                        found = re.search(r"Dedicated video memory:\s*(\d+)\s*MB", line)
                        if found:
                            log_vram = int(found.group(1))
                    if log_pixels is None:
                        found = re.search(r"Resolution:\s*(\d+)x(\d+)", line)
                        if found:
                            log_pixels = int(found.group(1)) * int(found.group(2))
                    if log_vram is not None and log_pixels is not None:
                        break
        except OSError:
            continue
    return log_vram, log_pixels


def _beamng_logs():
    """Every `beamng*.log` under the user's BeamNG folder, newest first.

    BeamNG rotates them (`beamng.log`, `beamng.1.log`, …), and the newest is not always the most
    informative — a session that quit before opening a window logs almost nothing.
    """
    base = os.path.join(os.path.expanduser("~"), "AppData", "Local", "BeamNG", "BeamNG.drive")
    found = []
    if not os.path.isdir(base):
        return found
    for entry in os.listdir(base):
        folder = os.path.join(base, entry)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if re.match(r"^beamng(\.\d+)?\.log$", name):
                path = os.path.join(folder, name)
                try:
                    found.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    return [path for _mtime, path in sorted(found, reverse=True)]
