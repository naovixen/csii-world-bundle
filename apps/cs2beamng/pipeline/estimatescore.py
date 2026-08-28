"""The estimator's own score: what the last build actually weighed against what this said.

Small and permanent on purpose. A meter nobody can check is a meter nobody should believe."""

from . import history
from .estimatebase import MIB

# -- scoring it against the build ------------------------------------------------------------------

# Below this much shipped geometry a build says nothing about a ratio: a `.cdae` carries a
# header and a material table whatever it holds, so a level of a handful of shapes measures that
# overhead and not the compression. The example bundle's one-shape level is exactly this case,
# and without the floor a test build would rewrite the user's calibration with its own noise.
LEARN_FLOOR_MIB = 64.0


def score(estimated, measured):
    """`estimated vs actual` for the build report and the screen after it.

    The one line that keeps the estimator honest. `measured` is the validator's `resident` block,
    which counts the same bytes this predicted.
    """
    if not estimated or not measured:
        return None
    if float(measured.get("totalMiB") or 0) < LEARN_FLOOR_MIB:
        return None
    out = {}
    for name, got in (
        ("mesh", measured.get("meshMiB")),
        ("texture", measured.get("textureShippedMiB")),
        ("total", measured.get("totalMiB")),
    ):
        want = {
            "mesh": estimated.get("meshes", {}).get("bytes", 0) / MIB,
            "texture": estimated.get("textures", {}).get("bytes", 0) / MIB,
            "total": estimated.get("residentBytes", 0) / MIB,
        }[name]
        if got is None:
            continue
        out[name] = {
            "estimatedMiB": round(want, 1),
            "actualMiB": round(float(got), 1),
            "errorPercent": round(100.0 * (want - float(got)) / max(float(got), 1e-6), 1),
        }
    return out


def observe(estimated, measured, index=None, values=None):
    """Blend one build's measured bytes back into the per-kind ratios.

    Only the halves that are RATIOS move: the texture half is arithmetic and has nothing to
    learn. The per-kind ratio cannot be separated out of a single total, so what is blended is
    the one factor that carries the whole mesh half - and `size/builds` counts how many of the
    user's own builds are in it, which is what the meter's basis line reports.
    """
    if not estimated or not measured:
        return
    actual = measured.get("meshMiB")
    predicted = estimated.get("meshes", {}).get("bytes", 0) / MIB
    if not actual or predicted <= 0 or float(actual) < LEARN_FLOOR_MIB:
        return
    correction = float(actual) / predicted
    rates = history.load()
    for key in list(rates):
        if key.startswith("size/cdae_per_source_"):
            history.observe(key, rates[key] * correction)
    history.observe("size/builds", min(8, int(rates.get("size/builds") or 0) + 1), blend=False)
