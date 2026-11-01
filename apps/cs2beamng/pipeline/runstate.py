"""The build state the UI polls, and refreshing its per-stage estimates."""

from ..stages import all_stages
from . import history

# -- state helpers ---------------------------------------------------------------------------------


def empty_state():
    return {
        "status": "idle",
        "stages": [],
        "log": [],
        "startedAt": None,
        "finishedAt": None,
        "lastHeartbeat": None,
        "report": None,
        "error": None,
        "cancelRequested": False,
    }


def initial_state(settings, census):
    state = empty_state()
    rates = history.load()
    for stage in all_stages():
        enabled = stage.enabled(settings)
        units = stage.units(census, settings) if enabled else 0
        est = (
            history.stage_estimate(stage.key, units, (census or {}).get("estimates"), rates)
            if units
            else 0.0
        )
        state["stages"].append(
            {
                "key": stage.key,
                "title": stage.title,
                "status": "pending" if enabled else "skipped",
                "units": units,
                "estimateSeconds": est,
                "phase": None,
                "phaseDone": 0,
                "phaseTotal": None,
                "unit": "",
                "phaseRate": None,
                "startedAt": None,
                "finishedAt": None,
                "summary": None,
                "fraction": 0.0,
            }
        )
    return state


def copy_state(state):
    out = dict(state)
    out["stages"] = [dict(s) for s in state["stages"]]
    return out


def refresh_estimates(state, _census, now):
    """Fill elapsed / eta per stage and overall from what is known right now."""
    overall_remaining = 0.0
    known = True
    for st in state["stages"]:
        st["elapsed"] = (
            ((st.get("finishedAt") or now) - st["startedAt"]) if st.get("startedAt") else 0.0
        )
        if st["status"] == "running":
            # the current phase, from its live rate (row bar + a floor under the stage ETA)
            phase_eta = phase_frac = None
            if st.get("phaseTotal") and st.get("phaseRate"):
                remaining_units = max(0, st["phaseTotal"] - st.get("phaseDone", 0))
                phase_eta = remaining_units / max(st["phaseRate"], 1e-9)
                phase_frac = min(1.0, st.get("phaseDone", 0) / st["phaseTotal"])
            # the stage: what it reported about itself > history estimate > the phase alone
            if st.get("stageFraction") is not None:
                frac = min(1.0, max(0.0, st["stageFraction"]))
                rem = st.get("stageRemaining")
                eta = (
                    None
                    if rem is None
                    else max(0.0, rem - (now - (st.get("stageProgressAt") or now)))
                )
                if eta is not None and phase_eta is not None:
                    eta = max(eta, phase_eta)
            elif st.get("estimateSeconds"):
                eta = max(st["estimateSeconds"] - st["elapsed"], phase_eta or 0.0)
                frac = min(0.99, st["elapsed"] / max(st["estimateSeconds"], 1e-9))
            else:
                eta, frac = phase_eta, phase_frac
            st["eta"] = eta
            st["fraction"] = frac if frac is not None else 0.0
            st["phaseFraction"] = phase_frac
            if eta is None:
                known = False
            else:
                overall_remaining += eta
        elif st["status"] == "pending":
            st["eta"] = st.get("estimateSeconds")
            if st["eta"] is None:
                known = False
            else:
                overall_remaining += st["eta"]
        else:
            st["eta"] = 0.0
    total = len([s for s in state["stages"] if s["status"] != "skipped"]) or 1
    finished = len([s for s in state["stages"] if s["status"] in ("done", "fresh")])
    running_frac = sum(s.get("fraction", 0.0) for s in state["stages"] if s["status"] == "running")
    state["overall"] = {
        "elapsed": ((state.get("finishedAt") or now) - state["startedAt"])
        if state.get("startedAt")
        else 0.0,
        "eta": overall_remaining if (known and state["status"] == "running") else None,
        "fraction": (finished + running_frac) / total,
    }
