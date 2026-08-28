"""Every `module.name` a shipped package writes actually exists on that module.

A split moves a function's ADDRESS, and nothing in the language notices. `beamng/materials.py`
keeps five names and loses nine to `textureplan`; every `bm.export_texture` elsewhere still parses,
still lints clean, and raises `AttributeError` the first time it runs. Refactor Phase 5 shipped
eight of these — `tilelod.new_stats`, `bm.dds_mod`, `state.diff_shared`, `dae.glb_to_local`,
`cook._write_ladder_index`, `budget.main`, `estimate.LEARN_FLOOR_MIB`, `poi._read_poi_file` — and
found them one at a time, each by running something: a 3-second example build for five of them, a
10-minute test suite for one, a **16-minute full-city build** for one, and a person typing
`cs2beamng budget` for the last.

This finds all of that in about a second. It imports every module in the two shipped packages
(which alone catches an import error or a cycle), then walks each module's AST for the names it
binds by import and every attribute taken off one, and asks the real module object whether that
attribute is there.

What it deliberately does not do: reach into `tools/`. Those are scripts, several of which run
their body at import, and pytest covers them. This is the packages the app is built from.

Run: `python tools/check_references.py`  (needs the packages importable, i.e. `pip install -e .`;
exits 1 on any finding).
"""

import ast
import importlib
import os
import pkgutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ("cs2beamng", "csiibundle")

# Modules whose attributes are built at run time rather than written down, so an AST walk cannot
# know what they hold. Empty today; an entry here needs a reason beside it.
DYNAMIC: set[str] = set()


def every_module(package):
    """Import the package and every module under it. Returns `{dotted name: module}`."""
    out = {}
    root = importlib.import_module(package)
    out[package] = root
    for info in pkgutil.walk_packages(root.__path__, package + "."):
        out[info.name] = importlib.import_module(info.name)
    return out


def import_bindings(tree, module_name, is_package):
    """`({local name: dotted module}, [(package, node)])` for every import in this module."""
    bound = {}
    froms = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                elif "." not in alias.name:
                    bound[alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, module_name, is_package)
            if base is None:
                continue
            for alias in node.names:
                # `from x import y` binds a module only when `x.y` IS one; the caller checks
                bound[alias.asname or alias.name] = f"{base}.{alias.name}"
            froms.append((base, node))
    return bound, froms


def _resolve_from(node, module_name, is_package):
    """The absolute package a `from ... import` reads.

    A package's `__init__` is its own base: `from . import x` inside `a.b/__init__.py` means
    `a.b.x`, while the same line in `a.b.c` means `a.b.x`. One dot of difference, and getting it
    wrong reports every sibling import as unresolved.
    """
    if not node.level:
        return node.module
    parts = module_name.split(".")
    keep = len(parts) - (node.level - 1 if is_package else node.level)
    base = ".".join(parts[:keep]) if keep > 0 else parts[0]
    return f"{base}.{node.module}" if node.module else base


def check_module(name, module, modules, problems):
    source = getattr(module, "__file__", None)
    if not source or not source.endswith(".py"):
        return
    tree = ast.parse(open(source, encoding="utf-8").read())
    bound, froms = import_bindings(tree, name, os.path.basename(source) == "__init__.py")
    rel = os.path.relpath(source, ROOT).replace("\\", "/")
    _check_from_imports(froms, modules, rel, problems)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        target = bound.get(node.value.id)
        if target is None or target in DYNAMIC:
            continue
        owner = modules.get(target)
        if owner is None or not hasattr(owner, "__file__"):
            continue  # not one of ours, or `from x import y` where y is a value not a module
        if not hasattr(owner, node.attr):
            problems.append(
                f"{rel}:{node.lineno}: {node.value.id}.{node.attr} — {target} has no {node.attr!r}"
            )


def _check_from_imports(froms, modules, rel, problems):
    """`from ours import name` — the name has to be there, and this is how `budget.main` broke.

    A missing one is an ImportError at import time, so it cannot hide the way an attribute can;
    but it hides just as long, because the module that does it may only be imported by a command
    nobody runs in a test.
    """
    for base, node in froms:
        owner = modules.get(base)
        if owner is None or base in DYNAMIC:
            continue
        for alias in node.names:
            if alias.name == "*" or hasattr(owner, alias.name):
                continue
            if f"{base}.{alias.name}" in modules:
                continue  # a submodule, imported lazily
            problems.append(f"{rel}:{node.lineno}: from {base} import {alias.name} — no such name")


def main():
    sys.path.insert(0, os.path.join(ROOT, "apps"))
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    modules = {}
    problems = []
    for package in PACKAGES:
        try:
            modules.update(every_module(package))
        except Exception as exc:  # an import error IS the finding
            problems.append(f"{package}: {type(exc).__name__}: {exc}")
    for name, module in sorted(modules.items()):
        try:
            check_module(name, module, modules, problems)
        except SyntaxError as exc:
            problems.append(f"{name}: {exc}")
    for line in problems:
        print("UNRESOLVED  " + line)
    print(f"\n{len(modules)} modules imported; {len(problems)} unresolved reference(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
