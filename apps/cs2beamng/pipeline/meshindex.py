"""The mesh index — which object shapes this city can place, and what each one weighs.

The cost meter has to answer "how much video memory will this level want" while the user is
still moving sliders, and the mesh half of that number is a function of WHICH SHAPES SHIP: a
shape is mounted whole the moment one instance of it is in view, so the cost is carried by the
shape list and not by the instance count (audit §83). Deciding that list is a pass over
`instances.jsonl` — 1.28 M records, ~15 s on the reference city — which is far too slow to run
between two keystrokes and far too important to guess at.

So it is run ONCE per bundle, at the most permissive settings there are, and the answer is
stored per shape in a form that every settings combination can be read out of without going back
to the layer:

* **`kind`** — the kind switch (`trees` / `props` / `buildings`) drops the whole row. Exact.
* **`rank`** — the density slider position at which this shape's FIRST instance survives
  (`csiibundle.instances.density_rank`, the same rule `density_keep` applies from the other
  end). The shape ships iff `rank < fraction`, at any fraction, exactly.
* **`cells`** — the 512 m grid cells the shape has an instance in, as a sorted list. A region
  build ships the shape iff one of its cells meets the region square, which is exact to the
  cell and never wrong in the direction that matters (a cell that only clips the corner keeps a
  shape the build may drop, so the estimate is an upper bound).
* **`bytes` / `lodBytes`** — the source glTF the shapes stage turns into a `.cdae`, and the
  bake's generated LOD levels for it. `generate_lods` decides whether the second is in.

What it does NOT model, declared here because an undeclared approximation is indistinguishable
from a bug: `foliage_road_clearance` is not applied (it removed 1,806 of 1,276,060 records on
the reference city and no whole shape with it, so the index would pay a spatial pass over the
lane layer to move nothing), and neither is the LOD 0 promotion, whose effect is bytes inside a
file rather than a file. Both are counted in the estimate's own approximation list.

The permissive pass runs `beamng.placement.admit` itself rather than restating it, so the
`baked` / `state` / `orphan` rules here are the build's own.
"""

import hashlib
import json
import os
import time

from csiibundle import instances as bundle_instances
from csiibundle import region as region_rule

from .. import settings as settings_mod
from ..beamng import placement

SCHEMA = "cs2beamng.meshindex/1"
CELL_METRES = 512.0
INDEX_DIR = "meshindex"

# Everything on, nothing thinned, nowhere excluded: the superset every real setting selects out
# of. `foliage_road_clearance` is 0 here because the index does not model it (see the module
# docstring), not because the build does not.
PERMISSIVE = {
    "trees": True,
    "props": True,
    "buildings": True,
    "foliage_density": 100,
    "prop_density": 100,
    "foliage_road_clearance": 0.0,
}


def key_for(bundle):
    """A cache key for this bundle's instance layer and mesh directory.

    Keyed on what the pass READS, not on the bundle path: a re-rip into the same folder changes
    the layer's size and mtime, and a new bundle elsewhere gets its own file. The mesh directory
    is stamped by its entry count and newest mtime rather than walked twice.
    """
    parts = [os.path.abspath(bundle or "")]
    for rel in ("instances.jsonl", "bundle.json"):
        path = os.path.join(bundle or "", rel)
        try:
            stat = os.stat(path)
            parts.append(f"{rel}:{stat.st_size}:{int(stat.st_mtime_ns)}")
        except OSError:
            parts.append(f"{rel}:-")
    for rel in ("meshes", os.path.join("derived", "lods")):
        path = os.path.join(bundle or "", rel)
        count = newest = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    count += 1
                    try:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime_ns)
                    except OSError:
                        pass
        except OSError:
            pass
        parts.append(f"{rel}:{count}:{newest}")
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=12).hexdigest()


def path_for(bundle):
    return os.path.join(settings_mod.app_dir(), INDEX_DIR, key_for(bundle) + ".json")


def load(bundle):
    """The stored index for this bundle, or None when there is none for these bytes."""
    try:
        with open(path_for(bundle), encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def build(bundle, progress=None, cancelled=None):
    """One permissive pass over the instance layer -> the index document, stored and returned.

    `cancelled()` is polled every 25k records so the UI can abandon it when the bundle changes
    under it; an abandoned pass writes nothing.
    """
    started = time.monotonic()
    mesh_dir = os.path.join(bundle, "meshes")
    lod_dir = os.path.join(bundle, "derived", "lods")
    settings = dict(PERMISSIVE)
    orphans, _stats = bundle_instances.orphans_of_absent_owners(bundle)

    rows = {}
    records = 0
    for record in bundle_instances.read_instances(bundle):
        records += 1
        if cancelled is not None and records % 25000 == 0 and cancelled():
            return None
        if placement.admit(record, settings, None, None, orphans) is not None:
            continue
        key = _state_stem(record, mesh_dir)
        if key is None:
            continue
        row = rows.get(key)
        if row is None:
            # `asset` as well as the key: the key is the prefab plus its STATE suffix
            # (`Oak__elderly`), and the bake's colour centres are per prefab, so the two are not
            # the same lookup. Reading centres by the key found 205 tree shapes where the build
            # wrote 253 - every state variant of a recoloured tree lost its shades.
            row = rows[key] = {
                "kind": record.get("kind"),
                "asset": record.get("assetId"),
                "count": 0,
                "rank": 1.0,
                "cells": set(),
            }
        row["count"] += 1
        kind = record.get("kind")
        if kind in ("tree", "prop"):
            row["rank"] = min(row["rank"], bundle_instances.density_rank(record))
        else:
            row["rank"] = 0.0  # not governed by a slider: always in
        pos = record.get("pos") or (0, 0, 0)
        row["cells"].add(_cell(pos[0], pos[2]))
    if progress is not None:
        progress.log(f"mesh index: {records:,} records over {len(rows):,} shapes")

    lod_bytes = _lod_bytes(lod_dir)
    centres = _variant_centres(bundle)
    out = {}
    for key, row in rows.items():
        try:
            size = os.path.getsize(os.path.join(mesh_dir, key + ".glb"))
        except OSError:
            size = 0
        out[key] = {
            "kind": row["kind"],
            "count": row["count"],
            "rank": round(row["rank"], 6),
            "bytes": size,
            "lodBytes": lod_bytes.get(key, 0),
            "variants": centres.get(row["asset"], 1),
            "cells": sorted(row["cells"]),
        }
    # `key` is in the document as well as in its filename: a consumer holding one in memory has
    # to be able to notice a re-rip into the same folder without re-reading the file.
    doc = {
        "schema": SCHEMA,
        "bundle": os.path.abspath(bundle),
        "cellMetres": CELL_METRES,
        "key": key_for(bundle),
        "records": records,
        "shapes": len(out),
        "seconds": round(time.monotonic() - started, 1),
        "shipped": out,
    }
    _store(bundle, doc)
    return doc


def _cell(x, z):
    return f"{int(x // CELL_METRES)},{int(z // CELL_METRES)}"


def _state_stem(record, mesh_dir, _cache={}):  # noqa: B006 - module-lived probe cache
    """The first mesh state of this record that is on disk, or None if none is.

    Unlike the two callers in `stages/`, this wants the whole stem and not the suffix: the key it
    builds is the prefab plus its state (`Oak__elderly`), which is what a shape is named by.
    One file probe per distinct name, cached across the whole pass.
    """
    return bundle_instances.first_mesh_state(record, mesh_dir, _cache)


def _lod_bytes(lod_dir):
    """`{shape stem: bytes}` over the bake's generated LOD files (`<stem>_LOD<n>.glb`)."""
    out = {}
    try:
        with os.scandir(lod_dir) as it:
            for entry in it:
                if not entry.name.endswith(".glb"):
                    continue
                stem = entry.name[:-4].split("_LOD")[0]
                try:
                    out[stem] = out.get(stem, 0) + entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _variant_centres(bundle):
    """`{shape stem: how many colour centres its prefab was baked with}`, default 1.

    A paint variant is a WHOLE SECOND SHAPE - the same geometry bound to a tinted material - so
    the centre count multiplies a prefab's mesh bytes, not only its textures. It is a per-PREFAB
    fact recorded by the bake and NOT the `paint_variants_tree` setting read back: the setting
    is a ceiling the bake applies to a palette that may hold fewer, and on the reference city it
    gave 116 of 137 tree shapes two centres and the other 21 one. Reading the setting instead
    counted 274 tree shapes where the build wrote 253.
    """
    path = os.path.join(bundle, "derived", "paint_variants.json")
    try:
        with open(path, encoding="utf-8") as handle:
            prefabs = (json.load(handle) or {}).get("prefabs") or {}
    except (OSError, ValueError):
        return {}
    return {name: max(1, len(rec.get("variants") or [])) for name, rec in prefabs.items()}


def _store(bundle, doc):
    path = path_for(bundle)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(doc, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


# -- reading it back at some settings --------------------------------------------------------


def select(doc, values):
    """`{kind: {"shapes", "bytes", "instancesAtFull"}}` for the shapes these settings ship.

    Every filter here is the read-back of something the pass recorded, not a model of it: the
    kind switch drops rows, `rank` decides the density slider exactly, `cells` decides the region
    to within one 512 m cell, and `variants` says how many copies of a recoloured prefab ship.

    `instancesAtFull` is named for what it is - the count at full density, not the count these
    settings place. The mesh half does not need it (a shape is mounted whole for one instance or
    ten thousand) and it must not be read as a placement figure.
    """
    region = settings_mod.region_of(values)
    want_lods = bool(values.get("generate_lods", True))
    painted = {k.strip() for k in str(values.get("paint_kinds") or "").split(",") if k.strip()}
    cells = _region_cells(region) if region else None
    out = {}
    for row in (doc.get("shipped") or {}).values():
        kind = row.get("kind")
        if not placement.kind_enabled(kind, values):
            continue
        if kind in ("tree", "prop") and row.get("rank", 1.0) >= placement.density_fraction(
            kind, values
        ):
            continue
        if cells is not None and not cells.intersection(row.get("cells") or ()):
            continue
        copies = max(1, int(row.get("variants") or 1)) if kind in painted else 1
        bucket = out.setdefault(kind, {"shapes": 0, "bytes": 0, "instancesAtFull": 0})
        bucket["shapes"] += copies
        bucket["bytes"] += copies * (
            row.get("bytes", 0) + (row.get("lodBytes", 0) if want_lods else 0)
        )
        bucket["instancesAtFull"] += row.get("count", 0)
    return out


def _region_cells(region):
    """The 512 m cells the region box touches (`csiibundle.region`)."""
    x0, x1, z0, z1 = region_rule.bounds(region)
    out = set()
    cx = int(x0 // CELL_METRES)
    while cx <= int(x1 // CELL_METRES):
        cz = int(z0 // CELL_METRES)
        while cz <= int(z1 // CELL_METRES):
            out.add(f"{cx},{cz}")
            cz += 1
        cx += 1
    return out
