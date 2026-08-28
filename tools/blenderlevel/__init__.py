"""The BeamNG level oracle's Blender half — one module per thing the level holds.

`tools/blender_import_level.py` is the entry point. It reads what BeamNG reads, out of the
level folder and nothing from the bundle, so a render answers "what will the game draw here"
for the parts Blender can show. Its companion is `tools/blender_import_bundle.py`, the
PRODUCER-side truth: import the same region from both and the difference is the app's doing.
"""


def log(msg):
    print(f"[level] {msg}", flush=True)
