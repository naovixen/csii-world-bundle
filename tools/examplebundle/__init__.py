"""The hand-authored example bundle's internals — `tools/make_example_bundle.py` is the entry.

The example is the executable half of the Phase-0 schema freeze: a tiny but *complete* bundle
that `tools/validate_bundle.py` accepts, so a consumer can be built and smoke-tested against it
with no game and no mod. Everything here is deterministic — re-running rewrites byte-identical
files.

Modules: ``payloads`` (the PNG and GLB byte builders and the cube), ``layers`` (the record
layers: assets, instances, networks, the §5A graph, §5B geometry, §7.2 appearance, §10
warnings), ``ground`` (the §8 rasters, §8b areas, §8c clip volumes), ``grading`` (the §8/§11
grading document), ``manifest`` (bounds and bundle.json).
"""
