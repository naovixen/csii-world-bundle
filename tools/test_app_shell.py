"""cs2beamng A0 gate — the application shell: runner (cancel, hard-cancel, dead worker, failure),
server (token, API, SSE), settings schema/groups, and the headless CLI end-to-end.

Run: `python tools/test_app_shell.py`  (needs `pip install -e .`; ~25 s; no bundle beyond
`docs/example-bundle`, no pywebview). Stub stages come from `tools/cs2beamng_stub_stages.py`
through the `CS2BEAMNG_STAGE_MODULE` hook, so the real bake never runs here except in the final
headless check. Part 1 of the A0 gate: the incremental build is `test_app_incremental.py` and
the delivery features are `test_app_features.py` (split in Phase 4, tally via `shellcommon`).
"""

import http.client
import json
import os
import re
import sys
import threading
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
from shellcommon import EXAMPLE, check, run_gate  # noqa: E402


def wait_until(pred, timeout, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(step)
    return None


def stub_settings(tmp, mode):
    from cs2beamng import settings as settings_mod

    values = settings_mod.defaults()
    values.update(
        {
            "bundle": EXAMPLE,
            "output": os.path.join(tmp, "levels"),
            "level_name": "stub_" + mode,
            "stub_mode": mode,
        }
    )
    return values


# -- settings ------------------------------------------------------------------------------------


def test_settings():
    from cs2beamng import settings as settings_mod

    print("settings")
    ui = settings_mod.schema_for_ui()
    check(
        [e["key"] for e in ui] == list(settings_mod.SCHEMA),
        "schema_for_ui covers all keys, in declaration order",
    )
    for name, preset in settings_mod.PRESETS.items():
        check(all(k in settings_mod.SCHEMA for k in preset), f"preset {name} sets only known keys")
        for k, v in preset.items():
            try:
                settings_mod.coerce(k, v)
            except ValueError:
                check(False, f"preset {name}: {k}={v!r} coerces")
    check(settings_mod.coerce("texture_max", "1024") == 1024, "numeric choice coerces to int")
    check(settings_mod.coerce("trees", "off") is False, "bool coerces")
    d = settings_mod.detected()
    check(isinstance(d.get("bundles"), list) and "platform" in d, "detected() returns lists")

    # -- D2: the basic panel, and help that fits on one line ------------------------------------
    # The point of the tiering is that the default screen stays small. A "basic" list that grows
    # to thirty keys is the problem it was made to solve, so the ceiling is checked, not just the
    # membership.
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.BASIC),
        "every BASIC key is a real setting",
    )
    check(len(settings_mod.BASIC) == len(set(settings_mod.BASIC)), "BASIC has no duplicates")
    check(
        len(settings_mod.BASIC) <= settings_mod.BASIC_MAX,
        f"BASIC stays under {settings_mod.BASIC_MAX} controls (it has {len(settings_mod.BASIC)})",
    )
    long_help = {k: len(v[2]) for k, v in settings_mod.SCHEMA.items() if len(v[2]) > 120}
    check(not long_help, f"every help string is one sentence (<=120 chars); over: {long_help}")
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.DETAILS),
        "DETAILS only describes real settings",
    )
    check(
        sorted(settings_mod.PRESET_INFO) == sorted(settings_mod.PRESETS),
        "every preset has a description",
    )
    # A fresh install names a preset in `quality_preset` and must actually BE on it, or the app
    # opens claiming a preset it is not on - and, since E2, showing a badge for a setting the
    # user never touched. This found `lod_switch_error_px` at 8.0 against high's 6.0.
    named = settings_mod.SCHEMA["quality_preset"][0]
    shipped = settings_mod.defaults()
    off = {k: (shipped[k], v) for k, v in settings_mod.PRESETS[named].items() if shipped[k] != v}
    check(not off, f"the shipped defaults ARE the {named} preset; off: {off}")
    tiers = {e["key"]: e["tier"] for e in ui}
    check(
        set(tiers) == set(settings_mod.SCHEMA) and set(tiers.values()) <= {"basic", "advanced"},
        "schema_for_ui tiers every key",
    )
    check(all(tiers[k] == "basic" for k in settings_mod.BASIC), "the BASIC keys are tiered basic")
    check(
        all(("detail" in e) == (e["key"] in settings_mod.DETAILS) for e in ui),
        "schema_for_ui ships a detail exactly where DETAILS has one",
    )


# -- E1: the settings metadata the redesigned screen is built from --------------------------------
#
# Three things are checked, and the third is the one that matters. The partition ("every key
# lands in exactly one place") is what keeps a setting from being silently dropped by a layout
# change. The lengths keep a one-line blurb one line. The LANGUAGE check is the gate on the
# actual complaint - the settings read like variable names - and it is mechanical on purpose: a
# rule a person applies by eye is a rule that decays the first time somebody is in a hurry.

# Words and shapes that have no business in a control's label or blurb. Identifiers, our own
# research vocabulary, and the section numbers of an audit nobody reading this screen has read.
# Text that only makes sense after reading the control NEXT to it. A setting reached through
# search arrives with no neighbours, and search is now a first-class route into the settings, so
# "the number above" is a description that works only where it happens to be laid out.
CROSS_REFERENCES = [
    "the number above",
    "the one above",
    "the setting above",
    "as above",
    "the slider sets",
    "the same again",
    "the same for",
    "the same as the",
    "follows the",
    "the box above",
    "the switch above",
    "listed above",
    "shown above",
    # ...and the same shapes pointing the other way, which is exactly what I reached for while
    # writing text the rule was not yet being enforced on ("the width rule below").
    "the number below",
    "the one below",
    "the setting below",
    "the box below",
    "the switch below",
    "rule below",
    "listed below",
    "shown below",
    "the map above",
    "the promotion above",
]

BANNED_WORDS = [
    "audit",
    "measured",
    "declared",
    "parity",
    "deviation",
    "vanilla",
    "reference city",
    "calibrated",
    "decompil",
    "prefab",
    "heightmap",
    "navgraph",
    "splat",
    "imposter atlas",
    "triangle budget",
    "p95",
    "p99",
    "colorset",
    "shader",
    "g-buffer",
    "mipmap",
]
# CamelCase and all-caps runs that are real words to a user, not identifiers. Kept SHORT and
# argued one by one: `AI` and `UI` are ordinary English now, and X/Y/Z are the axes a region is
# written in, which somebody using `--set region=` has to be told. A file-format acronym is NOT
# on this list - `DDS`, `PNG`, `JSON` and `GLB` read as code to the person this rule is for, and
# a line that needs one is a line that needs rewriting.
CAPS_OK = {"BeamNG", "CS2", "II", "A", "I", "AI", "UI", "X", "Y", "Z"}
CODE_EXTENSIONS = (
    ".dae",
    ".cdae",
    ".ter",
    ".png",
    ".dds",
    ".json",
    ".lua",
    ".py",
    ".exe",
    ".glb",
    ".zip",
    ".md",
)


# Shapes that mean something to us and nothing to a reader: our own document references, the
# in-game loads we numbered, the phase labels of this project's plans, and statistics vocabulary.
# A measurement is welcome on the reference page - "most of BeamNG's own colour maps are 512 or
# smaller" is useful - as long as it is STATED rather than CITED.
WORKING_NOTES = [
    (r"\baudit\b", "an audit reference"),
    (r"§\s*\d+", "a section mark"),
    (r"\b[A-Z]\d+(?:\.\d+)+\b", "a plan phase label (like A6.7)"),
    (r"\bload #\d+", "an in-game load number"),
    (r"\bp\d{2}\b", "a percentile"),
    (r"\b(?:MEASURED|DECLARED|CALIBRATED|TUNED)\b", "our own shouting"),
    (r"\breference city\b", "the reference city"),
    (r"\bcommit [0-9a-f]{7}", "a commit hash"),
    (r"\b\d{4}-\d{2}-\d{2}\b", "a date"),
]


def working_note_problems(text, what):
    """Where this string cites our notes instead of explaining the setting (empty = it explains)."""

    return [f"{what}: {why}" for pattern, why in WORKING_NOTES if re.search(pattern, text)]


def cross_reference_problems(text, what):
    """Where this string leans on a neighbouring control to be understood (empty = it stands)."""
    low = text.lower()
    return [
        f"{what}: refers to another control ({phrase!r})"
        for phrase in CROSS_REFERENCES
        if phrase in low
    ]


def plain_language_problems(text, what):
    """Why this string does not read as plain English, as a list of reasons (empty = it does).

    NOT applied to the Advanced section, which keeps its jargon on purpose: somebody changing an
    LOD switch budget needs to be told that is what they are changing, and the plain-English
    paraphrase of it says less rather than more. The length, the sentence and the
    cross-reference rules still apply there — only the vocabulary is let through.
    """

    out = []
    low = text.lower()
    if "§" in text or "`" in text:
        out.append(f"{what}: a section mark or a backtick")
    for word in BANNED_WORDS:
        if word in low:
            out.append(f"{what}: the word {word!r}")
    for ext in CODE_EXTENSIONS:
        if ext in low:
            out.append(f"{what}: the file extension {ext!r}")
    if re.search(r"[A-Za-z]_[A-Za-z]", text):
        out.append(f"{what}: an identifier with an underscore")
    if re.search(r"[A-Za-z]\.[A-Za-z]", text):
        out.append(f"{what}: a dotted identifier")
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        if token in CAPS_OK or token.islower() or token[1:].islower():
            continue
        out.append(f"{what}: the identifier-looking word {token!r}")
    if re.search(r"\d[, ]\d{3}", text):
        out.append(f"{what}: a figure out of the research")
    if "%" in text or "#" in text:
        out.append(f"{what}: a per-cent or a hash")
    if re.search(r"20\d\d-\d\d-\d\d", text):
        out.append(f"{what}: a date")
    return out


def _norm(text):
    return "".join(c for c in str(text).lower() if c.isalnum())


def test_settings_metadata():
    """E1 - `SECTIONS`, `LABELS`, `BLURBS`, `CONTROLS`: the partition, the lengths, the words."""
    from cs2beamng import settings as settings_mod

    print("settings metadata (E1)")
    keys = list(settings_mod.SCHEMA)

    # -- the partition: basics strip, one sidebar section, or driven by another control ---------
    placed = {}
    for key in keys:
        placed.setdefault(settings_mod.section_of(key), []).append(key)
    listed = [k for s in settings_mod.SECTIONS for _t, ks in s["blocks"] for k in ks]
    check(sorted(listed) == sorted(set(listed)), "no key is in two sidebar sections")
    check(
        all(k in settings_mod.SCHEMA for k in listed + settings_mod.BASICS),
        "every key a section or the basics strip names is a real setting",
    )
    everywhere = set(listed) | set(settings_mod.BASICS)
    driven = [k for k in keys if k not in everywhere]
    check(
        sorted(driven) == sorted(settings_mod.DRIVEN_BY),
        f"DRIVEN_BY names exactly the keys with no control: {sorted(set(driven) ^ set(settings_mod.DRIVEN_BY))}",
    )
    check(
        len(placed.get("basics", [])) == len(settings_mod.BASICS)
        and sum(len(v) for v in placed.values()) == len(keys),
        "every key lands in exactly one place",
    )
    ids = [s["id"] for s in settings_mod.SECTIONS]
    check(
        len(ids) == len(set(ids))
        and all(s.get("group") and s.get("label") for s in settings_mod.SECTIONS),
        "every section has a unique id, a group and a label",
    )
    section_choice = settings_mod.SCHEMA["ui_section"][1].split(":", 1)[1].split("|")
    check(section_choice == ids, "ui_section offers exactly the sections that exist")

    # -- labels and blurbs: one for every key, one line each ------------------------------------
    check(
        sorted(settings_mod.LABELS) == sorted(keys),
        "every key has a label, and no label is orphaned",
    )
    check(
        sorted(settings_mod.BLURBS) == sorted(keys),
        "every key has a blurb, and no blurb is orphaned",
    )
    long_blurbs = {k: len(v) for k, v in settings_mod.BLURBS.items() if len(v) > 100}
    check(not long_blurbs, f"every blurb is one line (<=100 chars); over: {long_blurbs}")
    long_labels = {k: len(v) for k, v in settings_mod.LABELS.items() if len(v) > 34}
    check(not long_labels, f"every label fits a nav column (<=34 chars); over: {long_labels}")
    unsentenced = [
        k
        for k, v in settings_mod.BLURBS.items()
        if not v[:1].isupper() or not v.rstrip().endswith(".")
    ]
    check(not unsentenced, f"every blurb is a sentence: {unsentenced}")

    # -- the language --------------------------------------------------------------------------
    problems = []
    for key in (k for k in keys if not settings_mod.is_technical(k)):
        problems += plain_language_problems(settings_mod.LABELS[key], f"{key} label")
        problems += plain_language_problems(settings_mod.BLURBS[key], f"{key} blurb")
    check(
        not problems,
        f"outside Advanced, no label or blurb reads like code ({len(problems)}): {problems[:6]}",
    )
    # E5: and the SAME rule on `help`. It is the CLI's own one-liner and it is printed on the
    # generated reference page, so it is user-facing text that the label/blurb gate never saw -
    # `view_distance` was still introducing itself as `LevelInfo.visibleDistance` there. What
    # leaves a help string is not deleted: the measured half of it moves into `DETAILS`.
    helps = []
    for key in (k for k in keys if not settings_mod.is_technical(k)):
        helps += plain_language_problems(settings_mod.SCHEMA[key][2], f"{key} help")
    check(
        not helps, f"outside Advanced, no help string reads like code ({len(helps)}): {helps[:6]}"
    )
    # ...and the LONG form, everywhere including Advanced. `DETAILS` is the only text on the
    # reference page with room to ramble, and it used to ramble at US: audit section numbers,
    # in-game load numbers, phase labels, percentiles and the reference city's own counts. Every
    # one of those already lives in the code that makes the decision, with the workings; on a
    # page a person reads it is a citation to a document they do not have.
    notes = []
    for key in keys:
        notes += working_note_problems(settings_mod.DETAILS.get(key, ""), f"{key} detail")
    check(not notes, f"no long explanation cites our own working notes ({len(notes)}): {notes[:6]}")
    over = {k: len(v) for k, v in settings_mod.DETAILS.items() if len(v) > 700}
    check(not over, f"every long explanation stays a few sentences (<=700 chars); over: {over}")
    check(
        any(settings_mod.is_technical(k) for k in keys), "the Advanced section is marked technical"
    )
    crossed = []
    for key in keys:  # this one applies EVERYWHERE, jargon or not
        crossed += cross_reference_problems(settings_mod.LABELS[key], f"{key} label")
        crossed += cross_reference_problems(settings_mod.BLURBS[key], f"{key} blurb")
        # ...and the long form, which is read on a page where nothing is above or below anything
        crossed += cross_reference_problems(settings_mod.DETAILS.get(key, ""), f"{key} detail")
    check(
        not crossed,
        f"nothing written about a setting leans on a neighbouring control ({len(crossed)}): {crossed[:6]}",
    )
    echoes = [
        k for k in keys if _norm(settings_mod.BLURBS[k]).startswith(_norm(settings_mod.LABELS[k]))
    ]
    check(not echoes, f"no blurb opens by repeating its own label: {echoes}")

    # -- units, subsets, and no box a person cannot fill in --------------------------------------
    kinds = {k: settings_mod.control_of(k) for k in keys}
    bare = [
        k
        for k, c in kinds.items()
        if c == "number"
        and k not in settings_mod.UNITS
        and settings_mod.section_of(k) not in ("basics", "driven")
    ]
    check(not bare, f"every number control says what it is measured in: {bare}")
    check(
        all(k in settings_mod.SCHEMA and kinds[k] == "number" for k in settings_mod.UNITS),
        "UNITS only labels number controls",
    )
    # A free-text box whose valid answers are a known, fixed set is a box a person cannot fill
    # in without already knowing the answer. There must be none of them left on screen.
    guessy = [
        k
        for k, c in kinds.items()
        if c == "text" and settings_mod.section_of(k) not in ("basics", "driven")
    ]
    check(not guessy, f"no free-text box is left where the answers are a fixed set: {guessy}")
    check(
        all(kinds[k] == "categories" for k in settings_mod.CHOICE_SETS),
        "a fixed answer set is drawn as checkboxes",
    )
    check(
        all(
            k in settings_mod.SCHEMA and v in settings_mod.SCHEMA
            for k, v in settings_mod.SUBSET_OF.items()
        ),
        "SUBSET_OF relates real settings",
    )
    # E6: the sentence and the behaviour. `DRIVEN_BY` tells a person what sets this key and
    # `DRIVES` is what actually does it; a key driven by a rule nobody wrote, or a rule driving a
    # key that still claims a control, is the two of them disagreeing.
    driven_targets = {target for target, _rule in settings_mod.DRIVES.values()}
    check(
        driven_targets <= set(settings_mod.DRIVEN_BY),
        f"everything a rule drives is declared as having no control: "
        f"{sorted(driven_targets - set(settings_mod.DRIVEN_BY))}",
    )
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.DRIVES),
        "DRIVES is keyed on real settings",
    )
    check(
        settings_mod.driven_changes({"networks": False}) == {"terrain_cut": False}
        and settings_mod.driven_changes({"networks": False, "terrain_cut": True}) == {},
        "driven_changes fills in what you did not say and leaves what you did",
    )
    check(
        all(k in settings_mod.SUBSET_OF.values() for k in settings_mod.PARENT_OFF),
        "PARENT_OFF only names keys that are somebody's parent",
    )
    # a child is laid out under its parent, so the indent it is drawn with is not a lie
    misplaced = []
    for section in settings_mod.SECTIONS:
        for _title, block in section["blocks"]:
            for at, key in enumerate(block):
                parent = settings_mod.SUBSET_OF.get(key)
                if parent and parent not in block[:at]:
                    misplaced.append(key)
    check(
        not misplaced, f"every subset is laid out after its parent, in the same block: {misplaced}"
    )

    # -- controls ------------------------------------------------------------------------------
    known = {
        "switch",
        "choice",
        "path",
        "text",
        "number",
        "percent",
        "distance",
        "clock",
        "categories",
        "segmented",
        "region",
        "poi",
    }
    check(
        set(kinds.values()) <= known,
        f"every control kind is one the UI can draw: {set(kinds.values()) - known}",
    )
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.CONTROLS),
        "CONTROLS only overrides real settings",
    )
    check(
        all(k in settings_mod.SCHEMA for k in settings_mod.RANGES),
        "RANGES only sizes real settings",
    )
    check(
        all(kinds[k] in ("percent", "distance") for k in settings_mod.RANGES),
        "a range belongs to a slider",
    )
    check(
        all(k in settings_mod.RANGES for k in settings_mod.ZERO_LABEL),
        "a zero label belongs to a slider that reaches zero",
    )

    # -- E2's gate: a new section is a DATA change ------------------------------------------------
    # Adding one must touch nothing but `SECTIONS` — not the schema, not the layout, not the
    # setting that remembers where you were. Adding one here and reading it back out is the same
    # test as doing it on a scratch branch, minus the branch.
    extra = {
        "id": "weather",
        "group": "Level",
        "label": "Weather",
        "tagline": "Rain and cloud",
        "blocks": [(None, ["water"])],
    }
    # `SECTIONS` is patched where it is declared, which is the only place a person adding a
    # section would touch. Nothing else is patched: `ui_section`'s choices used to be a second
    # hand-written statement of the section ids and had to be patched too, and are now derived.
    from cs2beamng.settings import catalog
    from cs2beamng.settings import sections as sections_mod

    real = sections_mod.SECTIONS
    try:
        sections_mod.SECTIONS = [*real, extra]
        served = settings_mod.sections_for_ui()
        check(
            served[-1]["id"] == "weather"
            and served[-1]["blocks"] == [{"title": None, "keys": ["water"]}],
            "a section added to SECTIONS is served with no other edit",
        )
        rebuilt = {s.key: s for s in catalog.build_settings()}
        check(
            "weather" in (rebuilt["ui_section"].choices or []),
            "...and the sidebar can remember it",
        )
    finally:
        sections_mod.SECTIONS = real

    # -- what the page is served -----------------------------------------------------------------
    payload = settings_mod.sections_for_ui()
    check(
        [s["id"] for s in payload] == ids
        and [k for s in payload for b in s["blocks"] for k in b["keys"]] == listed,
        "sections_for_ui ships the sections whole",
    )
    ui = {e["key"]: e for e in settings_mod.schema_for_ui()}
    check(
        all(
            ui[k]["label"] == settings_mod.LABELS[k]
            and ui[k]["blurb"] == settings_mod.BLURBS[k]
            and ui[k]["section"] == settings_mod.section_of(k)
            and ui[k]["control"] == kinds[k]
            for k in keys
        ),
        "schema_for_ui carries the label, blurb, control and section",
    )
    check(
        all(("drivenBy" in ui[k]) == (k in settings_mod.DRIVEN_BY) for k in keys),
        "schema_for_ui says what drives a key exactly where something does",
    )


def test_bake_stage_cancel():
    """The bake reports a cancelled product as an outcome; the stage must turn that into
    `Cancelled` (a cancel once ended as DONE, with validate running on a half-baked cache)."""
    from collections import namedtuple

    from cs2beamng.stages import BuildContext
    from cs2beamng.stages import bake as bake_stage
    from csiibundle.bake import runner as bake_runner
    from csiibundle.bake.progress import Cancelled, Progress

    print("bake stage: cancelled outcome")
    Outcome = namedtuple("Outcome", "name status reason seconds stats")
    real = bake_runner.run
    bake_runner.run = lambda *a, **k: [
        Outcome("materials", "cancelled", "cancelled by caller", 1.0, None)
    ]
    try:
        ctx = BuildContext(
            settings={"tile_size": 256},
            bundle=EXAMPLE,
            level_dir="",
            derived_dir="",
            census={},
            region=None,
            report={},
            jobs=1,
        )
        try:
            bake_stage.run(ctx, Progress())
            check(False, "cancelled outcome raises Cancelled")
        except Cancelled:
            check(True, "cancelled outcome raises Cancelled")
    finally:
        bake_runner.run = real


# -- runner ---------------------------------------------------------------------------------------


def run_mode(tmp, mode, action=None, timeout=30, act_on_progress=True):
    """Start the stub pipeline in `mode`, optionally run `action(runner)` once the work stage is
    running (and, with `act_on_progress`, has advanced at least once), and return
    (final_state, seconds_from_action_to_end, max_state_call_ms)."""
    from cs2beamng.pipeline.runner import Runner

    runner = Runner()
    runner.start(stub_settings(tmp, mode))
    acted_at = None
    slowest_state_ms = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        t1 = time.monotonic()
        state = runner.state()
        slowest_state_ms = max(slowest_state_ms, (time.monotonic() - t1) * 1000)
        work = next((s for s in state["stages"] if s["key"] == "work"), None)
        ready = (
            work is not None
            and work["status"] == "running"
            and (not act_on_progress or work.get("phaseDone", 0) > 0)
        )
        if action is not None and acted_at is None and ready:
            action(runner)
            acted_at = time.monotonic()
        if state["status"] != "running":
            return state, (time.monotonic() - acted_at) if acted_at else None, slowest_state_ms
        time.sleep(0.05)
    return runner.state(), None, slowest_state_ms


def test_runner(tmp):
    from cs2beamng.pipeline import runner as runner_mod

    print("runner: ok")
    state, _, slow = run_mode(tmp, "ok")
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "done", f"status done ({state['status']})")
    check(by["skipme"]["status"] == "skipped", "disabled stage reported skipped")
    check(
        all(by[k]["status"] == "done" for k in ("prep", "work", "wrap")), "all enabled stages done"
    )
    check(
        state.get("report") and "work" in state["report"]["stages"],
        "report carries the stage results",
    )
    check(
        any("hello from the stub" in e["text"] for e in state["log"]),
        "stage log reached the runner",
    )
    check(state["overall"]["fraction"] == 1.0, "overall fraction 1.0 when done")
    check(slow < 100, f"state() never blocked (slowest {slow:.1f} ms)")

    print("runner: cancel mid-stage")
    state, latency, _ = run_mode(tmp, "slow", action=lambda r: r.cancel())
    by = {s["key"]: s for s in state["stages"]}
    check(state["status"] == "cancelled", f"status cancelled ({state['status']})")
    check(
        by["work"]["status"] == "cancelled" and by["wrap"]["status"] == "pending",
        "work cancelled, wrap never ran",
    )
    check(latency is not None and latency < 1.0, f"cancel returned within 1 s ({latency})")

    print("runner: worker ignores cancel -> hard kill")
    old = runner_mod.HARD_CANCEL_SECONDS
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(
            tmp, "ignore_cancel", action=lambda r: r.cancel(), act_on_progress=False
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(state["status"] == "cancelled", f"stubborn worker ends cancelled ({state['status']})")
    check(
        latency is not None and latency < 4.0,
        f"terminated within HARD_CANCEL_SECONDS + slack ({latency})",
    )
    check(
        any("terminated" in e["text"] for e in state["log"]), "log says the worker was terminated"
    )

    print(
        "runner: repeated cancel does not postpone the hard kill; hard-killed stage has finishedAt"
    )
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:

        def cancel_twice(r):
            r.cancel()
            threading.Timer(
                1.0, r.cancel
            ).start()  # a second cancel 1 s later must not reset the deadline

        state, latency, _ = run_mode(
            tmp, "ignore_cancel", action=cancel_twice, act_on_progress=False
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(
        state["status"] == "cancelled" and latency is not None and latency < 4.5,
        f"second cancel did not extend the deadline ({latency})",
    )
    work = next(s for s in state["stages"] if s["key"] == "work")
    check(
        work.get("finishedAt") is not None and work.get("elapsed") == state["stages"][1]["elapsed"],
        "hard-killed stage carries finishedAt",
    )
    check(
        sum(1 for e in state["log"] if "cancel requested" in e["text"]) == 1,
        "one 'cancel requested' log line, not two",
    )

    print("runner: hard cancel with a live pool")
    runner_mod.HARD_CANCEL_SECONDS = 2.0
    try:
        state, latency, _ = run_mode(
            tmp, "pool_stubborn", action=lambda r: r.cancel(), act_on_progress=False, timeout=40
        )
    finally:
        runner_mod.HARD_CANCEL_SECONDS = old
    check(
        state["status"] == "cancelled" and latency is not None and latency < 6.0,
        f"worker with a stubborn pool ends cancelled in time ({state['status']}, {latency})",
    )

    print("runner: worker dies")
    state, _, slow = run_mode(tmp, "crash")
    check(state["status"] == "failed", f"dead worker -> failed ({state['status']})")
    check("exited" in (state.get("error") or ""), f"error names the exit ({state.get('error')})")
    check(slow < 100, f"state() stayed responsive while the worker died (slowest {slow:.1f} ms)")

    print("runner: stage spawns a pool (non-daemon worker)")
    state, _, _ = run_mode(tmp, "children")
    by = {s["key"]: s for s in state["stages"]}
    check(
        state["status"] == "done" and by["work"]["status"] == "done",
        f"stage with a child pool runs ({state['status']}: {by['work'].get('summary')})",
    )
    check(
        (state.get("report") or {})
        .get("stages", {})
        .get("work", {})
        .get("result", {})
        .get("squares")
        == 30,
        "pool result came back",
    )

    print("runner: stage raises")
    state, _, _ = run_mode(tmp, "fail")
    by = {s["key"]: s for s in state["stages"]}
    check(
        state["status"] == "failed" and by["work"]["status"] == "failed", "raising stage -> failed"
    )
    check(any("Traceback" in e["text"] for e in state["log"]), "traceback is in the log")
    check(
        state.get("report") and state["report"].get("failed", {}).get("stage") == "work",
        "report names the failed stage",
    )


# -- server ---------------------------------------------------------------------------------------


class Client:
    def __init__(self, url, token):
        u = urlparse(url)
        self.host, self.port, self.token = u.hostname, u.port, token

    def call(self, method, path, body=None, token=True, raw_body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"X-Token": self.token} if token else {}
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return resp.status, raw.decode("utf-8", "replace")

    def sse(self, since=0, want=("state", "log"), timeout=5.0):
        """Read the event stream until every wanted event name has arrived once."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.request("GET", f"/events?t={self.token}&since={since}")
        resp = conn.getresponse()
        seen = {}
        name = None
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < timeout and not all(w in seen for w in want):
                line = resp.readline().decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    seen.setdefault(name, json.loads(line[6:]))
                    name = None
        finally:
            conn.close()
        return resp.status, resp.getheader("Content-Type"), seen


def test_server(tmp):
    from cs2beamng import settings as settings_mod
    from cs2beamng.server import AppServer

    print("server")
    server = AppServer(values=stub_settings(tmp, "slow"))
    url = server.start()
    client = Client(url, server.token)
    try:
        status, page = client.call("GET", "/", token=False)
        check(
            status == 200 and server.token in page and "app.js" in page,
            "GET / serves the UI with the token injected",
        )
        status, _ = client.call("GET", "/static/app.js", token=False)
        check(status == 200, "static app.js served")
        status, _ = client.call("GET", "/static/../server.py", token=False)
        check(status == 404, "static path traversal refused")
        status, _ = client.call("GET", "/api/state", token=False)
        check(status == 403, "API without token -> 403")
        status, _ = client.call("POST", "/api/start", token=False)
        check(status == 403, "POST without token -> 403")
        # keep-alive: a rejected POST with a body must not poison the next request on the socket
        conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
        conn.request(
            "POST",
            "/api/start",
            body=b'{"values": {"trees": false}}',
            headers={"Content-Type": "application/json"},
        )
        r1 = conn.getresponse()
        r1.read()
        try:
            conn.request("GET", "/api/state", headers={"X-Token": server.token})
            r2 = conn.getresponse()
            r2.read()
            second = r2.status
        except (http.client.HTTPException, OSError):
            second = "reconnect"  # the server closed the connection: also fine
        conn.close()
        check(
            r1.status == 403 and second in (200, "reconnect"),
            f"keep-alive survives a rejected POST ({second})",
        )
        status, payload = client.call("GET", "/api/settings")
        check(
            status == 200 and payload["values"]["bundle"] == EXAMPLE and payload["schema"],
            "GET /api/settings",
        )
        check(payload["canBrowse"] is False, "no folder picker outside a native window")
        status, payload = client.call(
            "POST", "/api/settings", {"values": {"foliage_density": "42", "nope": 1}}
        )
        check(
            payload["values"]["foliage_density"] == 42
            and payload["values"]["quality_preset"] == "custom",
            "POST /api/settings coerces, and ANY quality change puts the row on Custom",
        )
        check(
            payload["presetBase"] == "high" and payload["presetChanged"] == ["foliage_density"],
            f"the payload keeps the base and says what moved off it ({payload['presetChanged']})",
        )
        check(
            payload["hasCustom"] is True,
            "the custom settings are remembered the moment there are any",
        )
        check(
            [s["id"] for s in payload["sections"]] == [s["id"] for s in settings_mod.SECTIONS],
            "E2: the payload carries the sidebar",
        )
        # naming your level folder is not a quality choice and must not move the row
        _s, named = client.call("POST", "/api/settings", {"values": {"level_title": "Whatever"}})
        check(
            named["values"]["quality_preset"] == "custom",
            "a basics-strip change leaves the row where it is",
        )
        check(any("nope" in e for e in payload["errors"]), "unknown key reported")
        status, payload = client.call("POST", "/api/settings", raw_body=b"{not json")
        check(status == 400, "malformed body -> 400, not silently ignored")
        status, payload = client.call("POST", "/api/preset", {"name": "low"})
        check(
            payload["values"]["quality_preset"] == "low"
            and payload["values"]["foliage_density"] == 30,
            "preset applied",
        )
        check(payload["presetChanged"] == [], "a freshly applied preset has nothing moved off it")
        # ...and Custom comes back to what the user had, which is the point of it always being there
        status, back = client.call("POST", "/api/preset", {"name": "custom"})
        check(
            status == 200
            and back["values"]["quality_preset"] == "custom"
            and back["values"]["foliage_density"] == 42,
            f"Custom restores the remembered settings ({back['values']['foliage_density']})",
        )
        status, payload = client.call("POST", "/api/preset", {"name": "bogus"})
        check(status == 400, "unknown preset -> 400")
        status, census = client.call("GET", "/api/census")
        check(
            status == 200 and census["bundle"] and census["bundle"]["counts"],
            "census of the example bundle",
        )
        t1 = time.monotonic()
        status, _census2 = client.call("GET", "/api/census")
        check(status == 200 and (time.monotonic() - t1) < 0.5, "census is cached")

        # -- E6: the settings with no control of their own, set by the one that drives them -----
        # The rule lives in the APP, so it is asserted through the app's own API. `--set` does
        # not pass through here and stays literal.
        status, payload = client.call("POST", "/api/settings", {"values": {"networks": False}})
        check(
            payload["values"]["terrain_cut"] is False,
            "roads off takes the ground cut with it - nothing to see in the hole",
        )
        status, payload = client.call("POST", "/api/settings", {"values": {"networks": True}})
        check(payload["values"]["terrain_cut"] is True, "...and roads back on brings it back")
        status, payload = client.call("POST", "/api/settings", {"values": {"foliage_density": 0}})
        check(
            payload["values"]["trees"] is False,
            "a density slider at zero turns its kind off, rather than keeping none of it",
        )
        status, payload = client.call("POST", "/api/settings", {"values": {"foliage_density": 40}})
        check(payload["values"]["trees"] is True, "...and above zero turns it back on")
        status, payload = client.call("POST", "/api/settings", {"values": {"prop_density": 0}})
        check(
            payload["values"]["props"] is False and payload["values"]["trees"] is True,
            "each slider drives its own kind and no other",
        )
        status, payload = client.call(
            "POST", "/api/settings", {"values": {"networks": False, "terrain_cut": True}}
        )
        check(
            payload["values"]["terrain_cut"] is True,
            "naming both halves means both halves: the rule fills in, it does not overrule",
        )
        client.call("POST", "/api/settings", {"values": {"networks": True, "prop_density": 100}})

        status, payload = client.call("POST", "/api/start")
        check(status == 200 and payload["ok"], "POST /api/start")
        status, payload = client.call("POST", "/api/start")
        check(status == 409, "second start while running -> 409")
        # concurrent starts after this one finishes: exactly one wins, the other gets 409 (never 500)
        server.runner.cancel()
        wait_until(lambda: not server.runner.running(), 5.0)
        codes = []

        def _start():
            codes.append(client.call("POST", "/api/start")[0])

        threads = [threading.Thread(target=_start) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check(sorted(codes) == [200, 409, 409], f"racing starts -> one 200, rest 409 ({codes})")
        wait_until(lambda: server.runner.running(), 2.0)
        status, ctype, seen = client.sse()
        check(status == 200 and ctype.startswith("text/event-stream"), "SSE stream")
        check(
            "state" in seen and seen["state"]["status"] == "running" and "log" not in seen["state"],
            "state event (without log)",
        )
        check("log" in seen and seen["log"] and "seq" in seen["log"][0], "log delta event")
        status, payload = client.call("POST", "/api/cancel")
        check(payload["ok"], "POST /api/cancel")
        done = wait_until(lambda: not server.runner.running(), 5.0)
        check(done is not None, "build stopped after cancel")
        status, state = client.call("GET", "/api/state")
        check(
            state["status"] == "cancelled" and isinstance(state["log"], list),
            "GET /api/state after cancel carries the log",
        )
        status, payload = client.call("GET", "/api/report")
        check(status == 200 and "report" in payload, "GET /api/report")
        # a second build on the same connection: log seq must keep climbing or the client drops lines
        last_seq = state["log"][-1]["seq"] if state["log"] else 0
        client.call("POST", "/api/settings", {"values": {"level_name": "stub_ok2"}})
        server.values["stub_mode"] = "ok"
        status, payload = client.call("POST", "/api/start")
        check(status == 200 and payload["ok"], "second build starts")
        wait_until(lambda: not server.runner.running(), 10.0)
        status, state2 = client.call("GET", "/api/state")
        check(
            state2["log"] and state2["log"][0]["seq"] > last_seq,
            "log seq is monotonic across builds",
        )
        status, payload = client.call("POST", "/api/open", {"what": "nowhere"})
        check(payload["ok"] is False, "open of nothing is refused politely")
        status, payload = client.call("POST", "/api/browse", {"key": "bundle"})
        check(status == 501, "browse without a window -> 501")
        # a second server on the same port must fail loudly (Windows SO_REUSEADDR would let it bind)
        try:
            AppServer(port=server.port, values=stub_settings(tmp, "ok")).start()
            check(False, "second server on the same port refused")
        except SystemExit:
            check(True, "second server on the same port refused")
        status, payload = client.call("POST", "/api/quit")
        check(status == 200, "POST /api/quit")
        stopped = wait_until(lambda: server.stopped.is_set(), 3.0)
        check(stopped is not None, "server stops after quit")
    finally:
        try:
            server.stop()
        except Exception:
            pass


# -- headless ---------------------------------------------------------------------------------------


def test_headless(tmp):
    print("headless CLI on docs/example-bundle (real stages)")
    os.environ.pop("CS2BEAMNG_STAGE_MODULE", None)
    from cs2beamng.cli import main

    out = os.path.join(tmp, "levels", "cli_example")
    t0 = time.monotonic()
    code = main(["build", EXAMPLE, "--out", out, "--set", "jobs=1"])
    check(code == 0, f"exit code 0 ({code}) in {time.monotonic() - t0:.1f}s")
    report = os.path.join(out, "cs2beamng_report.json")
    check(os.path.isfile(report), "report written")
    if os.path.isfile(report):
        with open(report, encoding="utf-8") as handle:
            doc = json.load(handle)
        check(
            "bake" in doc["stages"] and "validate" in doc["stages"], "report lists bake + validate"
        )


def _body(tmp):
    test_settings()
    test_settings_metadata()
    test_bake_stage_cancel()
    test_runner(tmp)
    test_server(tmp)
    test_headless(tmp)


def main():
    return run_gate(_body)


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_app_shell.py`."""
    assert main() in (None, 0)
