"""`Settings.cs` states every default twice; this is what makes the two agree.

An auto-property carries its default inline (`public int RipTextureSize { get; set; } = 1024;`)
and `SetDefaults()` assigns it again. Both are needed — the inline value is what a property has
before anything runs, and `SetDefaults` is what the options screen's "Reset to defaults" button
calls — but nothing made them say the same thing, and 147 properties is a lot of pairs to keep
in step by hand.

The failure this exists for is small and silent: `RipTextureSize` had an inline `= 1024` and no
line in `SetDefaults`, so **"Reset to defaults" reset every other ripper setting and left the
texture size wherever the user had put it**. Nothing reports that; you would only find it by
pressing the button and reading all 147.

Not every property is in scope, and the exclusions are the interesting part:

* A property with a **hand-written setter over a backing field** cannot carry an inline
  initializer at all. The ripper's `RipObjects`/`RipObjectsBuildings` family are these — a master
  writes all its subs and a sub recomputes its master — which is also why `Settings.cs`'s
  declaration ORDER is load-bearing and must not be tidied. They take their default from
  `SetDefaults` alone, correctly.
* A default that is not a literal — `Instance.CartoDataPath`, say — cannot be compared here, so
  it is checked for presence rather than for value.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(os.path.dirname(HERE), "Settings.cs")

CHECKS = 0
FAILS = []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {what}")
    if not cond:
        FAILS.append(what)


def _auto_property_defaults(source):
    """`{name: default}` for every auto-property that carries an inline initializer."""
    pattern = r"^\s*public\s+[\w\.<>\[\]\?]+\s+(\w+)\s*\{\s*get;\s*set;\s*\}\s*=\s*(.+?);\s*$"
    return {m.group(1): m.group(2).strip() for m in re.finditer(pattern, source, re.M)}


def _set_defaults_body(source):
    """The text between `SetDefaults()`'s braces."""
    start = source.index("public override void SetDefaults()")
    open_at = source.index("{", start)
    depth = 0
    i = open_at
    while True:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_at : i + 1]
        i += 1


def _assignments(body):
    """`{name: value}` for every `Name = value;` at the top level of the body."""
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"^\s*(\w+)\s*=\s*(.+?);\s*$", body, re.M)
    }


def main():
    source = open(SETTINGS, encoding="utf-8").read()
    inline = _auto_property_defaults(source)
    assigned = _assignments(_set_defaults_body(source))

    print(
        f"Settings.cs: {len(inline)} auto-properties with an inline default, "
        f"{len(assigned)} assignments in SetDefaults"
    )

    missing = sorted(set(inline) - set(assigned))
    check(
        not missing,
        "every inline default is also restored by SetDefaults; "
        f"'Reset to defaults' would not restore {missing}",
    )

    disagree = sorted(k for k in set(inline) & set(assigned) if inline[k] != assigned[k])
    for name in disagree:
        print(f"      {name}: inline {inline[name]!r} vs SetDefaults {assigned[name]!r}")
    check(not disagree, f"the two agree on every value; {len(disagree)} disagree")

    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    return 1 if FAILS else 0


def gate():
    """pytest entry point (tools/conftest.py); the same run as `python tools/test_settings_defaults.py`."""
    assert main() in (None, 0)


if __name__ == "__main__":
    raise SystemExit(main())
