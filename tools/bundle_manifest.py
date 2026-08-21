"""A diffable digest of a bundle, so a producer change can be proved to have changed nothing else.

Producer work cannot be validated by anyone but the user at the keyboard: it takes a ~28-minute
in-game rip to see what the mod now writes. That makes "did my change alter anything I did not
intend?" the hard question of every producer phase, and a 6.5 GB directory is not an answer you
can hold in your head or copy aside.

So: one JSON file, a few hundred KB, carrying a sha256 and a size for every file, a record count
for every JSONL layer, and the manifest's own declared counts. Diff two of them and the answer is
a list of paths, not an impression.

    python tools/bundle_manifest.py --out golden.json              # the bundle it can find
    python tools/bundle_manifest.py <bundle> --out after.json
    python tools/bundle_manifest.py --compare golden.json after.json

`--compare` exits 1 when anything differs, so it can gate. Refactor Phase 0 captures the golden;
every producer phase after Phase 11 diffs against it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

SCHEMA = "csiibundle.manifest-digest/1"
CHUNK = 1 << 20

# Directories a rip did not write. `derived/` is the offline bake's output — it is a function of
# the bundle plus the consumer's code, so hashing it into a PRODUCER baseline would report a
# consumer change as a producer one.
SKIP_DIRS = {"derived", "GeoJSON", "GeoTIFF", "Shapefile", "Styles"}


def default_bundle_dir():
    """Where the bundle usually is — the app's own answer, not a second one."""
    from csiibundle.bake import locate

    return locate.default_bundle_dir()


def sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def count_records(path: Path) -> int:
    """Non-blank lines in a JSONL layer — the layer's record count, read without parsing."""
    total = 0
    with open(path, "rb") as handle:
        for line in handle:
            if line.strip():
                total += 1
    return total


def walk(bundle: Path):
    """Every file a rip wrote, bundle-relative, sorted, with `derived/` and the vector exports out."""
    for dirpath, dirnames, filenames in os.walk(bundle):
        rel = Path(dirpath).relative_to(bundle)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS or rel.parts)
        for name in sorted(filenames):
            yield (rel / name).as_posix()


def digest(bundle: Path, progress=True) -> dict:
    files, layers = {}, {}
    started = time.time()
    paths = list(walk(bundle))
    for index, rel in enumerate(paths):
        path = bundle / rel
        sha, size = sha256_of(path)
        files[rel] = {"sha256": sha, "bytes": size}
        if rel.endswith(".jsonl"):
            layers[rel] = count_records(path)
        if progress and index % 2000 == 0:
            print(f"  {index}/{len(paths)} files…", file=sys.stderr)
    manifest = {}
    manifest_path = bundle / "bundle.json"
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = {"schema": loaded.get("schema"), "counts": loaded.get("counts")}
        except (OSError, ValueError) as exc:
            manifest = {"unreadable": str(exc)}
    return {
        "schema": SCHEMA,
        "bundle": str(bundle),
        "files": len(files),
        "bytes": sum(entry["bytes"] for entry in files.values()),
        "seconds": round(time.time() - started, 1),
        "manifestCounts": manifest,
        "layerRecords": layers,
        "sha256": files,
    }


def _describe(entry):
    return f"{entry['sha256'][:12]} {entry['bytes']}B"


def compare(before: dict, after: dict) -> int:
    """Print every difference. Returns the number of them."""
    old, new = before["sha256"], after["sha256"]
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    for path in removed:
        print(f"GONE     {path}  ({_describe(old[path])})")
    for path in added:
        print(f"NEW      {path}  ({_describe(new[path])})")
    for path in changed:
        print(f"CHANGED  {path}  {_describe(old[path])} -> {_describe(new[path])}")
    counts = 0
    for layer in sorted(set(before["layerRecords"]) | set(after["layerRecords"])):
        a = before["layerRecords"].get(layer)
        b = after["layerRecords"].get(layer)
        if a != b:
            counts += 1
            print(f"RECORDS  {layer}: {a} -> {b}")
    if before.get("manifestCounts") != after.get("manifestCounts"):
        print(f"MANIFEST counts: {before.get('manifestCounts')} -> {after.get('manifestCounts')}")
        counts += 1
    total = len(added) + len(removed) + len(changed) + counts
    print(
        f"\n{total} difference(s): {len(added)} new, {len(removed)} gone, "
        f"{len(changed)} changed, {counts} count change(s)."
    )
    return total


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", nargs="?", help="bundle directory (default: where it usually is)")
    parser.add_argument("--out", help="write the digest here (default: stdout)")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args(argv)

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        return 1 if compare(before, after) else 0

    bundle = Path(args.bundle) if args.bundle else Path(default_bundle_dir())
    if not (bundle / "bundle.json").is_file():
        print(f"no bundle.json in {bundle}", file=sys.stderr)
        return 2
    result = digest(bundle)
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(
            f"{result['files']} files, {result['bytes'] / 1e9:.2f} GB, "
            f"{result['seconds']} s -> {args.out}"
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
