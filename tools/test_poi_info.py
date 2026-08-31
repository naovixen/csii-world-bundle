"""`stages/poi._patch_info` on a missing or unreadable `info.json`.

Two of its three return paths handed back a bare `0` where the caller unpacks `(added,
replaced)`, so a level whose `info.json` was absent or truncated (a killed build) raised
`TypeError` in the one stage that would have rewritten it, and the worker took that as fatal:
the level never rebuilt (validation pass 2026-08-31, B6).

Run: `python tools/test_poi_info.py`  (system Python; no bundle needed).
"""

import os
import sys
import tempfile

failures = 0


def check(ok, claim, detail=""):
    global failures
    if not ok:
        failures += 1
    print(("  ok   " if ok else "  FAIL ") + claim + (f"  [{detail}]" if detail else ""))


def main():
    from cs2beamng.beamng import level as level_mod
    from cs2beamng.stages import poi

    entries = [{"translationId": "Main Street", "objectname": "poi_main_street"}]
    with tempfile.TemporaryDirectory() as tmp:
        lv = level_mod.Level(os.path.join(tmp, "lvl"), "lvl", "Level")
        try:
            out = poi._patch_info(lv, entries)
            check(out == (0, 0), "no info.json: (added, replaced) is (0, 0)", repr(out))
        except TypeError as exc:
            check(False, "no info.json: (added, replaced) is (0, 0)", f"raised {exc}")
        os.makedirs(lv.path(), exist_ok=True)
        with open(lv.path("info.json"), "w", encoding="utf-8") as handle:
            handle.write('{"spawnPoints": [')  # a build killed mid-write
        try:
            out = poi._patch_info(lv, entries)
            check(out == (0, 0), "a truncated info.json: (0, 0), not a crash", repr(out))
        except TypeError as exc:
            check(False, "a truncated info.json: (0, 0), not a crash", f"raised {exc}")
        with open(lv.path("info.json"), "w", encoding="utf-8") as handle:
            handle.write(
                '{"defaultSpawnPointName": "spawn_default", "spawnPoints": '
                '[{"objectname": "spawn_default"}, {"objectname": "stale_old_poi"}]}'
            )
        added, replaced = poi._patch_info(lv, entries)
        check(
            (added, replaced) == (1, 1),
            "a real info.json: one added, one stale entry replaced",
            f"{(added, replaced)}",
        )
    print("test_poi_info: " + ("FAILED" if failures else "OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def gate():
    """pytest entry point (tools/conftest.py)."""
    assert main() == 0
