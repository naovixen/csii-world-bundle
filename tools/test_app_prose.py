"""cs2beamng A0 gate — the words the app puts in front of a person.

Run: `python tools/test_app_prose.py`  (needs `pip install -e .`; instant, reads source only).

Part of the A0 gate beside `test_app_shell.py` (runner, server, headless),
`test_app_settings.py`, `test_app_incremental.py` and `test_app_features.py`; tally shared
through `shellcommon`.

This is a gate on PROSE, which is unusual enough to say why it exists: every setting carries a
label, a blurb and sometimes a detail, and those are the only documentation most people will
ever read. So they are checked like code — every cross-reference names something that exists,
no jargon the reader has no way to resolve, no working notes left in a shipped string, and
sentence case where the app is not naming a product.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from proserules import (  # noqa: E402
    cross_reference_problems,
    normalise,
    plain_language_problems,
    working_note_problems,
)
from shellcommon import check, run_gate  # noqa: E402


def test_settings_metadata():
    """E1 — `SECTIONS`, `LABELS`, `BLURBS`, `CONTROLS`: the partition, the lengths, the words.

    Decomposed into the seven sections it already had as comments, each taking the same
    `settings_mod` and key list the whole check was written against.
    """
    from cs2beamng import settings as settings_mod

    print("settings metadata (E1)")
    keys = list(settings_mod.SCHEMA)

    _check_partition(settings_mod, keys)
    _check_lengths(settings_mod, keys)
    _check_language(settings_mod, keys)
    _check_controls(settings_mod, keys)
    _check_kinds(settings_mod, keys)
    _check_sections(settings_mod, keys)
    _check_served(settings_mod, keys)


def _check_partition(settings_mod, keys):
    """Every key is in the basics strip, in exactly one sidebar section, or driven by
    another control — and nothing is in two places."""
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


def _check_lengths(settings_mod, keys):
    """One label and one blurb per key, each short enough for where it is shown."""
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


def _check_language(settings_mod, keys):
    """No jargon a reader cannot resolve, no working notes, no blurb echoing its label."""
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
        k
        for k in keys
        if normalise(settings_mod.BLURBS[k]).startswith(normalise(settings_mod.LABELS[k]))
    ]
    check(not echoes, f"no blurb opens by repeating its own label: {echoes}")


def _check_controls(settings_mod, keys):
    """Every number says what it is measured in, and no free-text box is left where the
    answers are a fixed set."""
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
    _check_answerable(settings_mod, keys)


def _check_answerable(settings_mod, keys):
    """No free-text box is left where the answers are a fixed set, and every choice a
    control offers is one the setting accepts."""
    kinds = {k: settings_mod.control_of(k) for k in keys}
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


def _check_kinds(settings_mod, keys):
    """Each key's control is the one its type and range imply."""
    kinds = {k: settings_mod.control_of(k) for k in keys}
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


def _check_sections(settings_mod, _keys):
    """E2's gate: adding a section is a DATA change, not a code change."""
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


def _check_served(settings_mod, keys):
    """What `schema_for_ui` hands the page carries the same label, blurb, control and
    section the module holds."""
    kinds = {k: settings_mod.control_of(k) for k in keys}
    ids = [s["id"] for s in settings_mod.SECTIONS]
    listed = [k for s in settings_mod.SECTIONS for _t, ks in s["blocks"] for k in ks]
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


def main():
    return run_gate(lambda _tmp: test_settings_metadata())


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_app_prose.py`."""
    assert main() in (None, 0)
