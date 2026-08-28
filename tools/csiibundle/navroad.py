"""The navgraph's data model — what `csiibundle.navgraph.build` emits.

The graph story — why the geometry comes from `lanes.jsonl`, the three properties a consumer's
graph model forces, and what is approximated — is `csiibundle.navgraph`'s module docstring.
"""

from collections import namedtuple

Road = namedtuple(
    "Road",
    "key kind segment name category nodes oneway lanes_left lanes_right speed_kph elevated",
)
Road.__doc__ = """One edge of the navgraph.

* `key` — unique and stable within one build (`seg_<id>` / `conn_<a>_<b>`).
* `kind` — `"segment"` or `"connector"`.
* `segment` — the `networks.jsonl` id this came from (the FROM segment for a connector).
* `nodes` — `[(x, y, z, width), ...]` in the BUNDLE frame. `width` is the full carriageway
  width, so a consumer wanting a radius halves it.
* `oneway` — traffic runs from `nodes[0]` towards `nodes[-1]` only.
* `lanes_left` / `lanes_right` — lanes against / with that direction, counted from `Invert`.
* `speed_kph` — CS2's own `attrs.limit`, or None where it ships none.
* `elevated` — the segment's `attrs.form` is `elevated` or `tunnel`: a consumer that welds nodes
  by proximity must not weld this road's interior to whatever it passes over or under.
"""

Navgraph = namedtuple("Navgraph", "roads stats notes")
