"""Every place a local assignment shadows a name the module imported.

`tidy = tidy(...)` runs fine until the day `tidy` is an import rather than a function defined
above, and then it is an `UnboundLocalError` on the line that assigns it. Ruff's F823 catches
only some shapes of that, and the ones it misses are exactly what a rename-on-move produces:
refactor Phase 5 hit seven of them — `tidy`, `manifest`, `albedo_gain`, `terrain_materials`,
`stamp`, `switch_summary`, `layers` — and one reached a full 16-minute city build before failing,
because the stage it was in does not run on the example bundle.

So this walks the AST: for each function, any name it binds that the module also imports at top
level. It is a gate rather than advice; a legitimate case would need a rename, which is the right
answer anyway.

Run: `python tools/check_shadows.py`  (no dependencies; exits 1 on any finding).
"""

import ast
import subprocess
import sys

files = subprocess.run(
    ["git", "ls-files", "apps/cs2beamng/*.py", "apps/cs2beamng/**/*.py"],
    capture_output=True,
    text=True,
).stdout.split()

found = 0
for path in files:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        continue
    imported = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
    if not imported:
        continue
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                target = node.target
            if isinstance(target, ast.Name) and target.id in imported:
                print(f"{path}:{node.lineno}: local {target.id!r} shadows the import")
                found += 1
print(f"\n{found} shadowed import(s)")
sys.exit(1 if found else 0)
