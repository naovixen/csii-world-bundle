"""The Blender half of the bundle reconstruction — one module per concern.

`tools/blender_import_bundle.py` is the entry point; everything it does that is genuinely
about Blender lives here. The engine-neutral half — the frame formula, the GLB and PNG
readers, the lane bend, the terrain lattice, the splat decode, the grading derivation — is
`tools/csiibundle/`, which never imports `bpy`, and which the BeamNG builder reads through the
same functions. Keeping one copy of each rule is what stops the two adapters drifting apart.

`survey` is the one module here that never touches `bpy`, so it is the one a test can reach.
"""
