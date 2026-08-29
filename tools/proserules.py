"""What counts as plain language in a setting's label, blurb or detail.

The tables and the three problem-finders `test_app_prose.py` runs over every string the
settings screen shows. They are here rather than beside the gate because they are the RULE and
the gate is the check: a new banned word or a new cross-reference is a change to what the app
may say, and it belongs somewhere a person can read without reading a test.

* `CROSS_REFERENCES` — a phrase that names something else in the app, and what it must name.
* `BANNED_WORDS` — jargon with a plain replacement, each paired with what to say instead.
* `WORKING_NOTES` — the phrases that mean the sentence was written for us, not for a user.
* `CAPS_OK` / `CODE_EXTENSIONS` — the words allowed to keep their capitals or their dots.
"""

import re

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


def normalise(text):
    return "".join(c for c in str(text).lower() if c.isalnum())
