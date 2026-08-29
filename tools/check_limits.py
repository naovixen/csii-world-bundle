"""The size gate: no fork-owned file over 400 lines, no function over 60.

The ceiling is a CI gate, not discipline (docs/plans/refactor.md, "Rules that govern every
phase"). Upstream Carto code is exempt by authorship — `upstream-files.txt` is the boundary,
derived from git, not from judgement.

The allowlist (`tools/limits-allowlist.txt`) seeds the gate green on a repo that breaches it in
57 files, and **shrinks only**: an entry that no longer violates is an error, so the allowlist
cannot carry a fixed line past the phase that fixed it, and every addition shows up in review.

    python tools/check_limits.py                # the gate
    python tools/check_limits.py --prune        # drop entries that no longer violate
    python tools/check_limits.py --seed         # rewrite the allowlist from today's violations
    python tools/check_limits.py --verify-upstream
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

FILE_MAX = 400
FUNC_MAX = 60
SOURCE_EXTENSIONS = {".cs", ".py", ".js", ".css"}
FORK_POINT = "43a05fb"  # last upstream commit: Taipei Native, "1.0.16 Update", 2026-06-23

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_LIST = ROOT / "upstream-files.txt"
ALLOWLIST = ROOT / "tools" / "limits-allowlist.txt"

# Words that can precede `(...)` and a block without the result being a function.
NOT_A_CALLABLE = {
    "if",
    "else",
    "for",
    "foreach",
    "while",
    "do",
    "switch",
    "case",
    "catch",
    "try",
    "using",
    "lock",
    "fixed",
    "unsafe",
    "checked",
    "unchecked",
    "return",
    "new",
    "await",
    "yield",
    "throw",
    "in",
    "is",
    "as",
    "stackalloc",
    "sizeof",
    "typeof",
    "nameof",
    "with",
}


def git(*args: str) -> list[str]:
    """Run git with -z and split on NUL, so paths containing spaces survive."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), *args, "-z"], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.split("\0") if line]


def upstream_paths() -> set[str]:
    """The exemption list, read from the committed artifact."""
    if not UPSTREAM_LIST.exists():
        raise SystemExit(f"missing {UPSTREAM_LIST.name}; run --verify-upstream to regenerate")
    lines = UPSTREAM_LIST.read_text(encoding="utf-8").splitlines()
    return {line for line in lines if line and not line.startswith("#")}


def fork_sources() -> list[str]:
    """Every source file the fork owns, tracked or not yet, in sorted order.

    Settings.cs, Mod.cs and Instance.cs are MIXED — upstream files carrying fork-added members —
    and this gate exempts them whole, because authorship is per-file here and splitting it per
    member is not worth automating. Refactor Phase 9 moves those members (the ripper button
    handlers, `Fit()`, `SetDefaults`' 147 duplicate initializers) into fork-owned files, and the
    gate picks them up there.

    `--others --exclude-standard` is why a NEW file is gated. This was `ls-files` alone, which
    lists only what git already tracks, so a file written but not yet `git add`ed was invisible:
    running the gate before committing a split — which is the moment it is for — passed on the
    old files while the new ones went unread. Refactor Phase 5 committed three over-limit
    functions that way and called the gate clean. `--exclude-standard` keeps `.gitignore` doing
    its job, so build output and caches stay out.
    """
    exempt = upstream_paths()
    listed = set(git("ls-files")) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(
        path
        for path in listed
        if os.path.splitext(path)[1] in SOURCE_EXTENSIONS and path not in exempt
    )


def blank_out(text: str, dialect: str) -> str:
    """Replace string and comment *contents* with spaces, preserving every newline.

    Brace matching cannot tell a `{` in code from one in a string or a comment, and both
    appear constantly in this repo's C# (JSON idioms) and JS (template literals).
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        if two == "//":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif two == "/*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:end]))
            i = end
        elif ch in "\"'`" or (ch == "@" and text[i + 1 : i + 2] == '"'):
            i = _skip_literal(text, i, out, dialect)
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_literal(text: str, start: int, out: list[str], dialect: str) -> int:
    """Consume one string literal beginning at `start`, emitting blanks. Returns the next index."""
    verbatim = text[start] == "@"
    quote = '"' if verbatim else text[start]
    i = start + (2 if verbatim else 1)
    n = len(text)
    while i < n:
        ch = text[i]
        if verbatim and ch == quote:
            if text[i + 1 : i + 2] == quote:  # "" is an escaped quote in a verbatim string
                i += 2
                continue
            i += 1
            break
        if not verbatim and ch == "\\":
            i += 2
            continue
        if not verbatim and ch == "\n" and quote != "`":
            break  # unterminated: a regex literal or an apostrophe in a comment we mis-read
        if ch == quote:
            i += 1
            break
        i += 1
    if dialect == "py":  # triple quotes are handled by ast, not here
        pass
    out.append("".join(c if c == "\n" else " " for c in text[start:i]))
    return i


def python_functions(text: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) for every def in a Python file, nested ones included."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise SystemExit(f"cannot parse: {exc}") from exc
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            found.append((node.name, start, node.end_lineno or node.lineno))
    return found


def brace_functions(text: str, dialect: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) for every C#/JS function, by brace matching.

    A `{` opens a function body when the code before it is `)` — optionally followed by a
    constructor chain (`: base(...)`), a generic constraint (`where T : class`) or, in JS, `=>`
    — and the identifier before the matching `(` is not a control-flow keyword.
    """
    code = blank_out(text, dialect)
    line_of = _line_index(code)
    found = []
    for match in re.finditer(r"\{", code):
        opener = match.start()
        head = _function_head(code, opener, dialect)
        if head is None:
            continue
        name, decl = head
        close = _match_brace(code, opener)
        if close is None:
            continue
        found.append((name, line_of(decl), line_of(close)))
    return found


def _line_index(code: str):
    starts = [0]
    for i, ch in enumerate(code):
        if ch == "\n":
            starts.append(i + 1)
    import bisect

    return lambda offset: bisect.bisect_right(starts, offset)


BETWEEN = re.compile(r"^\s*(:\s*(base|this)\s*\([^;{}]*\)\s*)?(where\s[^;{}]*)?$")
DECL = re.compile(r"([A-Za-z_$][\w$]*)\s*$")


def _function_head(code: str, opener: int, dialect: str) -> tuple[str, int] | None:
    """The (name, declaration offset) of the function `code[opener]` opens, or None."""
    before = code[:opener]
    arrow = dialect == "js" and before.rstrip().endswith("=>")
    if arrow:
        before = before.rstrip()[:-2]
    tail = before.rstrip()
    if not tail.endswith(")"):
        return None
    if not arrow and not BETWEEN.match(code[len(tail) : opener]):
        return None
    paren = _match_paren(code, len(tail) - 1)
    if paren is None:
        return None
    name_match = DECL.search(code[:paren])
    if name_match is None:
        return (("=>" if arrow else "?"), paren) if arrow else None
    name = name_match.group(1)
    if name in NOT_A_CALLABLE:
        return None
    return name, name_match.start(1)


def _match_paren(code: str, close: int) -> int | None:
    depth = 0
    for i in range(close, -1, -1):
        if code[i] == ")":
            depth += 1
        elif code[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return None


def _match_brace(code: str, opener: int) -> int | None:
    depth = 0
    for i in range(opener, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def functions_in(path: str, text: str) -> list[tuple[str, int, int]]:
    ext = os.path.splitext(path)[1]
    if ext == ".py":
        return python_functions(text)
    if ext == ".cs":
        return brace_functions(text, "cs")
    if ext == ".js":
        return brace_functions(text, "js")
    return []


def function_key(path: str) -> str:
    """The path a FUNCTION's allowlist entry is keyed by.

    For a C# partial class the file is not part of a member's identity: `TerrainRipSystem.cs`
    and `TerrainRipSystem.Areas.cs` are one type, and `Type.Aspect.cs` is the convention for
    splitting one. Keying a member by its file would mean that moving it between two files of
    the SAME class invalidates its allowlist entry and needs a new one — which the shrink-only
    rule forbids, so a pure file split would be blocked on decomposing functions it never
    touched. Keying by the type keeps the entry stable across exactly the move that does not
    change the function, and no other: a member that moves to a DIFFERENT type still loses its
    entry, which is right, because that is a real change.

    Everything else keeps its own path — a Python module's file IS its identity.
    """
    if not path.endswith(".cs"):
        return path
    directory, _, filename = path.rpartition("/")
    type_name = filename[: -len(".cs")].split(".")[0]
    return f"{directory}/{type_name}" if directory else type_name


def violations() -> list[tuple[str, str]]:
    """Every breach as (key, human description). The key is what the allowlist stores."""
    found = []
    for path in fork_sources():
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        length = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if length > FILE_MAX:
            found.append((f"file {path}", f"{path}: {length} lines (max {FILE_MAX})"))
        for name, start, end in functions_in(path, text):
            span = end - start + 1
            if span > FUNC_MAX:
                key = f"func {function_key(path)}::{name}"
                found.append((key, f"{path}:{start} {name}(): {span} lines (max {FUNC_MAX})"))
    return found


def read_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


ALLOWLIST_HEADER = """# Known breaches of the 400-line file / 60-line function ceilings, seeded when the gate went in.
#
# This list SHRINKS ONLY. An entry that no longer violates is an error, not a courtesy — the
# gate reports it as stale and `--prune` removes it. Adding an entry is therefore always a
# visible, deliberate diff, and a phase that fixes a file cannot leave its entry behind.
#
# Regenerate after a phase:  python tools/check_limits.py --prune
"""


def write_allowlist(keys: set[str]) -> None:
    body = "\n".join(sorted(keys))
    ALLOWLIST.write_text(ALLOWLIST_HEADER + body + "\n", encoding="utf-8", newline="\n")


def verify_upstream() -> int:
    """`upstream-files.txt` must still equal the fork point's tree, intersected with HEAD."""
    head = set(git("ls-files"))
    expected = {p for p in git("ls-tree", "-r", "--name-only", FORK_POINT) if p in head}
    listed = upstream_paths()
    ok = True
    for path in sorted(expected - listed):
        print(f"missing from upstream-files.txt: {path}")
        ok = False
    for path in sorted(listed - expected):
        print(f"listed but not upstream (or deleted): {path}")
        ok = False
    print(f"upstream-files.txt: {len(listed)} paths, {'ok' if ok else 'STALE'}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prune", action="store_true", help="drop entries that no longer violate")
    parser.add_argument("--seed", action="store_true", help="rewrite the allowlist from scratch")
    parser.add_argument("--verify-upstream", action="store_true")
    args = parser.parse_args()

    if args.verify_upstream:
        return verify_upstream()

    found = violations()
    keys = {key for key, _ in found}
    if args.seed:
        write_allowlist(keys)
        print(f"seeded {ALLOWLIST.name} with {len(keys)} entries")
        return 0

    allowed = read_allowlist()
    if args.prune:
        write_allowlist(allowed & keys)
        print(f"pruned {len(allowed - keys)} stale entries; {len(allowed & keys)} remain")
        return 0

    new = [text for key, text in found if key not in allowed]
    stale = sorted(allowed - keys)
    for text in new:
        print(f"OVER LIMIT  {text}")
    for key in stale:
        print(f"STALE ALLOWLIST ENTRY (fixed — remove it with --prune)  {key}")
    files = sum(1 for k in keys if k.startswith("file "))
    print(
        f"\n{len(fork_sources())} fork files checked; {files} over {FILE_MAX} lines, "
        f"{len(keys) - files} functions over {FUNC_MAX}; {len(allowed)} allowlisted."
    )
    if new or stale:
        return 1
    print("check_limits: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
