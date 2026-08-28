"""One setting, declared once.

Everything the app knows about a knob — its type, its default, the three lengths of prose that
describe it, how it is drawn, and what it depends on — is a field of this record. It used to be
sixteen dictionaries all keyed by setting name (`SCHEMA`, `LABELS`, `BLURBS`, `DETAILS`, `UNITS`,
`SUBSET_OF`, `CONTROLS`, `DRIVEN_BY`, `NEEDS`, `DRIVES`, `RANGES`, `CHOICE_SETS`, `ZERO_LABEL`,
`PARENT_OFF`, plus `SECTIONS` and `GROUPS`), which meant adding a setting was up to sixteen edits
and nothing caught a miss — a key with no `LABELS` entry raised `KeyError` on the settings page,
at render time, in front of the user.

Here a missing label is a `TypeError` at import: `label` and `blurb` are required arguments.
The tables that other code still imports (`SCHEMA`, `LABELS`, …) are derived from these records
in `catalog.py`, so they stay exactly what they were and there is one place to write them.
"""

from dataclasses import dataclass, field

# What a key looks like on screen where its type alone does not say. A setting that leaves
# `control` unset falls back on what its `kind` implies (see `screen.control_of`).
CONTROL_BY_KIND = {
    "bool": "switch",
    "choice": "choice",
    "path": "path",
    "str": "text",
    "int": "number",
    "float": "number",
}


@dataclass(frozen=True)
class Setting:
    """A single setting. `key` is its name everywhere: `--set key=value`, the JSON, the UI."""

    key: str

    # -- the contract: what `--set`, the saved JSON and the pipeline see -------------------------
    #: The value when nothing has been saved. `defaults()` overrides three of these with paths
    #: discovered on this machine.
    default: object
    #: `bool | int | float | str | path | choice:a|b|c`. `coerce` reads it to turn a string from
    #: the CLI or the UI into the setting's type.
    kind: str

    # -- the three lengths of prose ---------------------------------------------------------------
    #: Plain name. A label says WHAT the thing is; the blurb says what it does and what it costs.
    #: Two rules the test suite enforces: a blurb never opens by repeating its own label, and no
    #: label or blurb REFERS TO ANOTHER CONTROL ("the number above", "the same again", "the slider
    #: sets it"). A setting whose text only makes sense after reading its neighbour's is a setting
    #: that breaks the moment it is reached through search, which is a first-class route into it.
    label: str = ""
    #: One line. Outside the Advanced section, in the words of somebody who has never read this
    #: repository; inside it, in the words of the thing itself. What changes in the world first,
    #: then what it costs.
    blurb: str = ""
    #: One sentence, written for the generated reference page rather than for a control.
    help: str = ""
    #: The long form, for the settings whose answer needed an argument: what was measured, what it
    #: cost, and why the default is the default. The UI shows `help` always and this behind a "why"
    #: disclosure, and `docs/settings.md` is generated from it. It is neither softened nor deleted
    #: — 31 of the help strings had grown into audit notes of up to 2,320 characters, which is the
    #: right amount of detail in the wrong place: every control printed its essay, always.
    detail: str | None = None

    # -- how it is drawn --------------------------------------------------------------------------
    #: Overrides the control kind its type implies: `percent`, `distance`, `clock`, `region`,
    #: `categories`, `segmented`, `poi`.
    control: str | None = None
    #: What a number is measured in, printed beside the box. A box reading "0.8" with nothing
    #: after it is a box that has not been answered — metres? feet? a fraction? — and every number
    #: control that is not a bare count has one.
    unit: str | None = None
    #: `(min, max, step, suffix)` for a slider.
    range: tuple | None = None
    #: What a slider prints at its floor, where zero means something other than "none".
    zero_label: str | None = None
    #: Every free-text box whose set of valid answers is FIXED AND KNOWN, as that set of
    #: `(id, label)` pairs. A box a person cannot answer without already knowing the answer is a
    #: design failure: nothing on screen said that furniture categories are exactly these three.
    choice_set: list | None = None

    # -- what it depends on -----------------------------------------------------------------------
    #: This setting only means anything while the named one is on, AND IS OWNED BY IT. The UI
    #: indents it under its parent and greys it out when the parent is off, so "which of these
    #: belong to which" is answered by the layout rather than by reading. Turning the last of a
    #: parent's switches off turns the parent off too — a parent with nothing left under it is
    #: off, and pretending otherwise is how a level ships an empty layer.
    subset_of: str | None = None
    #: The value this setting takes when the last thing under it is switched off.
    parent_off: str | None = None
    #: Gated by the named setting but NOT OWNED by it. Two reasons a pair lands here rather than in
    #: `subset_of`: the two live in different sections (a tuning ceiling belongs with the tuning
    #: numbers even though it does nothing while the layer it caps is off), or this is not one of
    #: the parent's SWITCHES and so cannot be the last of them — emptying your own list of
    #: fast-travel points asks for the derived ones, not for no fast travel at all. These grey
    #: out; they do not indent, and they never switch a parent off.
    needs: str | None = None
    #: This setting has no control of its own, and this names the thing on screen that now sets
    #: it. It keeps its `--set` name and its entry in the reference page; what it loses is a box
    #: nobody could answer without already knowing the answer. The rule that does the driving is
    #: `values.DRIVES`, so the sentence and the behaviour cannot say different things.
    driven_by: str | None = None

    #: Filled in by `catalog`: the section id this key is placed in, or `"basics"` / `"driven"`.
    section: str = field(default="", compare=False)

    @property
    def base_kind(self) -> str:
        """`kind` without a `choice:` payload."""
        return self.kind.split(":", 1)[0]

    @property
    def choices(self) -> list[str] | None:
        """The literal choices of a `choice:` setting, else None."""
        if not self.kind.startswith("choice:"):
            return None
        return self.kind.split(":", 1)[1].split("|")
