"""The cost estimate — what this level will ask of the machine, before it is built.

A settings screen whose "frame" takes an hour and a half to render owes the person moving the
sliders a live answer to "will this run": every game that ships a graphics menu puts a video
memory bar under it, and this app is a graphics menu with a very slow preview. The number that
decides whether a BeamNG level runs is what it MOUNTS - every shape and texture, whether or not
it is on screen - which is what `beamng.machine.resident_budget_mib` is expressed in and
what the build's own verdict is measured against.

So this module predicts exactly the quantity the validator measures (`_resident_bytes`: the
`.cdae` beside every shape plus every texture under `art/`), out of two halves that are honest
about themselves in different ways:

**Textures — computed.** Every texture the build will write is enumerated through
`beamng.materials.plan_canonical` / `plan_areas`, the same walk the materials stage exports
from; each one's source dimensions come out of the PNG header (33 bytes), the cap is the
setting, and the file size of a block-compressed mipmapped DDS is arithmetic
(`beamng.dds.encoded_bytes`). This is not a model of the exporter - it is the exporter's own
sizing. MEASURED against a real build 2026-08-20: 7,716 of 7,716 shared files byte-for-byte,
541.3 MiB predicted against 541.3 MiB written.

**Meshes — calibrated.** WHICH shapes ship is exact, out of `meshindex` (one permissive pass
over the instance layer, read back at any settings). What each one WEIGHS as a cooked `.cdae` is
its source glTF times a ratio measured from real builds - 1.02 for a building, 0.65 for a prop,
1.03 for a tree on the reference city - blended towards the user's own results after every build
(`history.observe`). A ratio is a ratio and not a guess, but it is not arithmetic either, and
the meter says which figure is which.

Everything the estimate cannot do exactly is listed in `approximations` and shown on screen, and
after every build the report prints the estimate beside the measurement. An estimator nobody
scores is an estimator nobody can trust.
"""

import os

from . import history
from .estimatebase import MIB
from .estimatemeshes import mesh_estimate, stale_bake_note, terrain_file_bytes
from .estimatetextures import plan_is_warm, texture_estimate


def take(values, census=None, index=None, warm_only=False):
    """The whole estimate for these settings, or a `ready: False` document saying what is missing.

    `index` is a `meshindex` document; without one the mesh half cannot be scoped to the kind
    switches and is not offered at all, because a video memory figure that does not move when
    you turn the buildings off is worse than no figure.

    `warm_only` refuses to do the one slow piece - resolving the bundle's texture plan, a second
    of path joins and header reads - and asks to be called again off the request thread instead.
    """
    bundle = values.get("bundle") or ""
    out = {"ready": False, "rows": [], "approximations": [], "waiting": None}
    if not os.path.isfile(os.path.join(bundle, "bundle.json")):
        out["waiting"] = "no bundle"
        return out
    derived_dir = os.path.join(bundle, "derived")
    if not os.path.isfile(os.path.join(derived_dir, "materials.json")):
        # The first build's bake writes it. Until then there is nothing to size - and saying so
        # is the honest state, not a number with a shrug attached.
        out["waiting"] = "bake"
        return out
    # The index first: it is the slower of the two things being waited for by an order of
    # magnitude, and saying "measuring your textures" while a twenty-second pass over the
    # instance layer is what is actually holding the number up would be the wrong answer to
    # "how long is this going to take".
    if not index:
        out["waiting"] = "index"
        return out
    if warm_only and not plan_is_warm(bundle, derived_dir, values):
        out["waiting"] = "reading"
        return out

    textures = texture_estimate(values, bundle, derived_dir)
    meshes = mesh_estimate(values, bundle, index, census)

    rates = history.load()
    resident = textures["bytes"] + meshes["bytes"]
    budget = _budget(values)
    out.update(
        {
            "ready": True,
            "textures": textures,
            "meshes": meshes,
            "residentBytes": resident,
            "residentMiB": round(resident / MIB, 1),
            "diskBytes": resident + terrain_file_bytes(values, bundle),
            # The `.dae` sources are NOT in it and the basis says so rather than guessing at them:
            # nothing has measured what they weigh against the cooked files, and a made-up multiple
            # of several gigabytes is the kind of number this meter exists not to print.
            "diskBasis": (
                "shapes, textures and the heightfield; the COLLADA sources you keep are on top"
                if values.get("keep_dae", True)
                else "shapes, textures and the heightfield"
            ),
            "budget": budget,
            "approximations": (
                textures["approximations"] + meshes["approximations"] + stale_bake_note(census)
            ),
            "coefficients": {
                k: round(v, 4) for k, v in sorted(rates.items()) if k.startswith("size/")
            },
        }
    )
    out["rows"] = _rows(out, values)
    out["overSections"] = sorted(
        {row["section"] for row in out["rows"] if row.get("band") == "over" and row.get("section")}
    )
    return out


# -- the rows the meter draws ----------------------------------------------------------------------


def _budget(values):
    """What this machine's card is known to hold, read exactly the way the validator reads it, so
    the meter and the build's own verdict cannot disagree."""
    try:
        from ..beamng import machine

        vram, pixels, source = machine.render_target(values)
        return {
            "vramMiB": vram,
            "pixels": pixels,
            "source": source,
            "residentMiB": round(machine.resident_budget_mib(vram, pixels)),
            "failureMiB": round(machine.resident_failure_mib(vram, pixels)),
        }
    except Exception:
        return None


def _rows(est, values):
    """The bars. One figure with a budget it can exceed, and it warns rather than refuses: a
    person may be building deliberately for a machine that is not this one, which is what the
    three target-machine settings are for."""
    budget = est.get("budget")
    mib = est["residentMiB"]
    row = {
        "key": "vram",
        "label": "Video memory",
        "value": f"{mib:,.0f} MiB",
        "basis": _resident_basis(est),
        "section": _biggest_lever(est),
    }
    if budget:
        fit, fail = float(budget["residentMiB"]), float(budget["failureMiB"])
        row["of"] = f"of {fit:,.0f} MiB known to fit"
        row["fraction"] = mib / max(fail, 1.0)
        row["markFraction"] = fit / max(fail, 1.0)
        if mib >= fail:
            row["band"] = "over"
            row["advice"] = (
                "This is past the point where this card has been measured to run "
                "out. The level will stutter and get worse the longer you play."
            )
        elif mib > fit:
            row["band"] = "close"
            row["advice"] = (
                "This is more than your card is known to hold, and less than it is "
                "known to fail at. It may run; nobody has measured it."
            )
        else:
            row["band"] = "fits"
    else:
        row["fraction"] = 0.0
        row["band"] = "unknown"
        row["advice"] = "Run BeamNG once so it writes down what card you have."
    return [row]


def _resident_basis(est):
    meshes = est["meshes"]["bytes"] / MIB
    textures = est["textures"]["bytes"] / MIB
    return (
        f"{meshes:,.0f} MiB of shapes {est['meshes']['basis']}, "
        f"{textures:,.0f} MiB of textures computed exactly"
    )


def _biggest_lever(est):
    """The section holding the control that would move this figure most - what the meter's
    "What should I change?" jumps to. The textures are their own section; everything else in the
    mesh half is chosen in World."""
    groups = est["meshes"]["groups"]
    heaviest = max(groups.values(), key=lambda g: g["bytes"], default=None)
    if heaviest is None or est["textures"]["bytes"] > heaviest["bytes"]:
        return "textures"
    return heaviest.get("section") or "world"
