"""The lint-debt gate: ruff findings this refactor has not reached yet, and can never add to.

`ruff check` is clean over the repo except for one rule family — `ARG`, unused arguments — which
principle 3 (YAGNI) owns but which Phases 2 and 3 are the right place to clear, because the
findings split two ways and only one of them is deletable:

  * **Protocol conformance.** `enabled(settings)` and `units(census, settings)` are the stage
    contract; every stage declares both and most ignore one argument. The parameter is not dead,
    it is required. Phase 3 unifies the two stage frameworks and is where that contract is
    restated once instead of twenty times.
  * **Genuinely dead parameters** — `import_tiles(tiles, materials, level_dir, center, radius)`
    uses one of five; `collision.thickness(verts, tris=None)` never reads `tris`. Deleting one
    means touching its call sites, which is Phase 2 work, not Phase 0 work.

Rather than scatter ~80 `# noqa` comments the later phases would have to find again, the debt
sits in one ledger (`tools/ruff-debt.txt`) with the same shrink-only property as the size
allowlist: a finding not in the ledger fails, and a ledger entry that no longer fires fails as
stale. The ledger can only get shorter.

    python tools/check_ruff_debt.py            # the gate
    python tools/check_ruff_debt.py --prune    # drop entries that no longer fire
    python tools/check_ruff_debt.py --seed     # rewrite the ledger from today's findings
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "tools" / "ruff-debt.txt"
DEBT_RULES = "ARG"

FINDING = re.compile(r"^(?P<path>.+?):\d+:\d+: (?P<rule>[A-Z]+\d+) (?P<message>.*)$")
SYMBOL = re.compile(r"`([^`]+)`")

HEADER = """# Ruff findings the refactor has not reached yet. SHRINKS ONLY — see tools/check_ruff_debt.py.
#
# One line per finding, keyed by file and symbol rather than by line number, so the entry
# survives an edit above it and dies with the parameter it names.
#
# After a phase clears some:  python tools/check_ruff_debt.py --prune
"""


def findings() -> dict[str, str]:
    """{key: human text} for every debt-rule finding ruff reports right now."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "concise",
            "--select",
            DEBT_RULES,
            "--no-cache",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    found = {}
    for line in result.stdout.splitlines():
        match = FINDING.match(line.strip())
        if not match:
            continue
        path = match["path"].replace("\\", "/")
        symbol = SYMBOL.search(match["message"])
        key = f"{match['rule']} {path}::{symbol.group(1) if symbol else match['message']}"
        found[key] = f"{path}: {match['rule']} {match['message']}"
    return found


def read_ledger() -> set[str]:
    if not LEDGER.exists():
        return set()
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def write_ledger(keys: set[str]) -> None:
    LEDGER.write_text(HEADER + "\n".join(sorted(keys)) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()

    found = findings()
    if args.seed:
        write_ledger(set(found))
        print(f"seeded {LEDGER.name} with {len(found)} entries")
        return 0

    recorded = read_ledger()
    if args.prune:
        write_ledger(recorded & set(found))
        print(f"pruned {len(recorded - set(found))}; {len(recorded & set(found))} remain")
        return 0

    new = sorted(text for key, text in found.items() if key not in recorded)
    stale = sorted(recorded - set(found))
    for text in new:
        print(f"NEW LINT DEBT  {text}")
    for key in stale:
        print(f"STALE LEDGER ENTRY (fixed — remove it with --prune)  {key}")
    print(f"\nruff debt: {len(found)} findings, {len(recorded)} recorded.")
    if new or stale:
        return 1
    print("check_ruff_debt: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
