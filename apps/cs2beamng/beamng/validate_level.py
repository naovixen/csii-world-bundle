"""Structural + one physical check of a built level folder. `check(level_dir, bundle_dir,
progress) -> (checks, problems)`.

Structural: every `items.level.json` line parses and names a `class`; `persistentId`s are
unique; `TerrainBlock.terrainFile`, `TSStatic.shapeName`, forest `shapeFile`s and material
texture paths exist; every `.dae` parses as XML; the `.ter` header, size and layer indices agree
with `.terrain.json`; exactly one `TerrainBlock` and one `LevelInfo`; at least one
`SpawnSphere`; `info.json` names an existing spawn, every `spawnPoints` entry resolves to one and
every `preview` it names exists; at least one drivable `DecalRoad` (without which BeamNG's whole
navigation layer — minimap, routing, ground markers — is silently dead), and those roads run
through `beamng.map_lua` — a port of the load-time navgraph build — because a level whose roads
crash `map.lua` never opens at all (audit A7.5).

Physical: the `.ter` height under sampled `OnGround` buildings equals `instances.ground_level`
(`pos.y − attrs.elevation`) within `GROUND_TOLERANCE` — the one check that catches a wrong
orientation, a wrong origin or a wrong scale in one go (a flipped terrain is metres off
everywhere). The tolerance covers the roadbed sink and the shallow-band clamp.

This module is the entry; the checks themselves are `levelcheck/`, one module per subject, and
`levelcheck.CHECKS` is the order they run in. What a MACHINE can hold — the residency budget,
the render target, the light manager — is `beamng/machine.py`: most of its callers are asking
about a machine with no level in hand.
"""

from .levelcheck import CHECKS, check

__all__ = ["CHECKS", "check"]
