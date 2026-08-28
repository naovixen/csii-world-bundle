"""Move exact line spans out of a source file into new modules, by unique markers.

Each span is (first_line_exact, last_line_exact). The span is cut from the source and
returned verbatim, so a move cannot become an edit.
"""

import sys

SRC = "tools/blender_import_bundle.py"


def load(path=SRC):
    return open(path, encoding="utf-8").read().split("\n")


def find(lines, marker, used):
    hits = [i for i, ln in enumerate(lines) if ln == marker and i not in used]
    if len(hits) != 1:
        raise SystemExit(f"marker {marker!r} matched {len(hits)} lines")
    return hits[0]


def carve(lines, spans):
    """Return (extracted_text_blocks, remaining_lines). Spans must not overlap."""
    ranges = []
    for first, last in spans:
        a = find(lines, first, set())
        b = a + 1
        while b < len(lines) and lines[b] != last:
            b += 1
        if b >= len(lines):
            raise SystemExit(f"end marker {last!r} not found after {first!r}")
        ranges.append((a, b))
    ranges_sorted = sorted(ranges)
    for (a1, b1), (a2, _b2) in zip(ranges_sorted, ranges_sorted[1:]):
        if a2 <= b1:
            raise SystemExit(f"overlapping spans {a1}-{b1} and {a2}-")
    blocks = ["\n".join(lines[a : b + 1]) for a, b in ranges]
    drop = set()
    for a, b in ranges:
        drop.update(range(a, b + 1))
    remaining = [ln for i, ln in enumerate(lines) if i not in drop]
    return blocks, remaining


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    print(f"wrote {path}: {len(text.splitlines())} lines")


def save_source(lines, path=SRC):
    text = "\n".join(lines)
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    write(path, text)


if __name__ == "__main__":
    print(len(load()), "lines in", SRC, file=sys.stderr)
