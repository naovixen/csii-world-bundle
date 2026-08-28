"""Records the producer DECLARED it could not deliver, and the children they strand (§10).

`absent_prefabs` is the set of assetIds whose prefab did not resolve at all; `orphans_of_absent_owners`
walks the ownership chain so a consumer can drop the props of a building that was never placed
instead of leaving them hanging in clear sky. Both read the bundle's own declarations — an
UNDECLARED missing mesh stays a real defect (`csiibundle.instances`'s module docstring).
"""

import os

from . import reader


def absent_prefabs(bundle_dir):
    """assetIds the producer declared **absent** — the prefab itself did not resolve (§10).

    NOT the same as "declared undeliverable". `InstanceRipSystem.InstanceDeclarations` emits
    `kind: "prefab"` for two different gaps, and only one means the object is not there:

    | kind | reason | what it means |
    |---|---|---|
    | `prefab` | *managed prefab did not resolve* | **absent** — an unowned asset pack; assetId is a synthetic `unresolved_<n>` |
    | `prefab` | *prefab has no SubMesh buffer* | it EXISTS and simply has no mesh of its own |
    | `mesh` | *none of N sub-mesh(es) yielded geometry* | same — it exists |

    Measured on the reference city: 87 absent against 117 that exist without geometry. Keying on
    `kind` alone would also drop the children of prefabs that are genuinely there — and those
    children are real: the second and third rows are `ParkingLot02..14`, `CampfireSite01/02`,
    `AmericanFootballField01`, whose paving ships in the `areas` layer while their lamps, benches
    and planting ship here. `read_declared_gaps` cannot answer this because it collapses to
    `{assetId: reason}`, last line wins, and an absent prefab carries a mesh warning too.
    """
    path = reader.layer_path(bundle_dir, "warnings")
    absent = set()
    if not path or not os.path.isfile(path):
        return absent
    for record in reader.read_jsonl(path):
        asset = record.get("assetId")
        if not isinstance(asset, str):
            continue
        if record.get("kind") == "prefab" and "did not resolve" in (record.get("reason") or ""):
            absent.add(asset)
    return absent


def orphans_of_absent_owners(bundle_dir):
    """`(ids, stats)` — records whose owner chain reaches a prefab the producer declared absent.

    **A record whose owner was not placed because the producer could not deliver it is not
    placeable either.** This is not a CS2 rule to port — the game had those prefabs and drew the
    buildings — it is a consumer's obligation against a DECLARED gap (§10). Without it we drop
    the building and place its props anyway, and on the reference city that is 6,714 records
    hanging in clear sky, up to 292 m up, 695 of them belonging to one absent tower.

    Only the absent class is dropped (`absent_prefabs`). A prefab that exists without geometry
    keeps its children — 5,806 of them, the lamps and benches of car parks and sports pitches
    whose own surface ships in the `areas` layer — and a construction site keeps its children
    too, which measurement says are the `ConstructionCrane01` and the site's benches: §4 states
    the crane and site fence are real entities, and they are exactly the tall ones.

    Ownership is transitive and shallow — depth 4 at most on the reference city, 96 % of it depth
    2 — but a sub-object of a sub-object is legal, so the walk follows `parent` to the root.
    """
    absent = absent_prefabs(bundle_dir)
    stats = {"absentPrefabs": len(absent), "records": 0, "withParent": 0, "orphans": 0, "owners": 0}
    if not absent:
        return set(), stats

    parent_of, absent_ids = {}, set()
    for record in reader.read_jsonl(reader.layer_path(bundle_dir, "instances")):
        rid = record.get("id")
        if rid is None:
            continue
        stats["records"] += 1
        if record.get("assetId") in absent:
            absent_ids.add(rid)
        parent = record.get("parent")
        if parent is not None:
            parent_of[rid] = parent
            stats["withParent"] += 1

    ids, owners = set(), set()
    for rid in parent_of:
        cur, seen = parent_of.get(rid), set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in absent_ids:
                ids.add(rid)
                owners.add(cur)
                break
            cur = parent_of.get(cur)
    stats["orphans"] = len(ids)
    stats["owners"] = len(owners)
    return ids, stats
