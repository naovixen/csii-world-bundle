#!/usr/bin/env python3
"""Bundle-contract gate, lane half — §5A lanes, §5B lane geometry, §7.2 appearance.

Split from `test_bundle_contract.py` (refactor Phase 4); shares `contractbase`.
"""

from __future__ import annotations

import json
from pathlib import Path

from contractbase import ContractCase
from validate_bundle import DEFAULT_SCHEMA_DIR, validate_bundle


class LaneContractTest(ContractCase):
    def patch_first_lane(self, bundle: Path, mutate) -> None:
        path = bundle / "lanes.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        mutate(record)
        lines[0] = json.dumps(record, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def patch_lane_catalogue(self, bundle: Path, mutate) -> None:
        path = bundle / "laneprefabs.json"
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        mutate(catalogue)
        path.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")

    def test_lane_record_with_unknown_prefab_rejected(self):
        bundle = self.fresh()
        path = bundle / "lanemeshes.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["prefab"] = "No_Such_Prefab"
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "not in the lanePrefabs catalogue")

    def test_lane_part_index_out_of_range_rejected(self):
        bundle = self.fresh()
        path = bundle / "lanemeshes.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["parts"] = [5]
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "out of range")

    def test_lane_tunnel_tag_accepted(self):
        bundle = self.fresh()
        path = bundle / "lanemeshes.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["tunnel"] = True
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])

    def test_lane_tunnel_false_rejected(self):
        # The writer omits the key for surface lanes; an explicit false is a producer bug.
        bundle = self.fresh()
        path = bundle / "lanemeshes.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["tunnel"] = False
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "expected const True")

    def test_lane_part_mesh_missing_rejected(self):
        bundle = self.fresh()
        for path in (bundle / "lanemeshes").glob("*.glb"):
            path.unlink()
        self.assert_rejected(bundle, "has no mesh in lanemeshes/")

    def test_lanemeshes_without_catalogue_rejected(self):
        bundle = self.fresh()
        (bundle / "laneprefabs.json").unlink()
        self.assert_rejected(bundle, "no lanePrefabs catalogue resolves")

    def test_lane_unknown_kind_rejected(self):
        """§5A's kinds are a closed set the producer derives from the lane entity's own
        component; an unknown one means a consumer's traffic filter silently drops it."""
        bundle = self.fresh()
        self.patch_first_lane(bundle, lambda r: r.update(kind="monorail"))
        self.assert_rejected(bundle, "'monorail' is not one of")

    def test_lane_single_point_path_rejected(self):
        """A one-point path is not a path — nothing can travel it, and a consumer building
        geometry from it divides by a zero-length segment."""
        bundle = self.fresh()
        self.patch_first_lane(bundle, lambda r: r.update(path=r["path"][:1]))
        self.assert_rejected(bundle, "minItems 2")

    def test_lane_secondary_false_rejected(self):
        """The writer omits `secondary` for placed lanes; an explicit false is a producer bug,
        and it matters because 'derived vs placed' is a filter a consumer chooses deliberately."""
        bundle = self.fresh()
        self.patch_first_lane(bundle, lambda r: r.update(secondary=False))
        self.assert_rejected(bundle, "expected const True")

    def test_lane_unknown_field_rejected(self):
        bundle = self.fresh()
        self.patch_first_lane(bundle, lambda r: r.update(congestion=0.5))
        self.assert_rejected(bundle, "unexpected property 'congestion'")

    def test_lane_owner_resolving_nowhere_is_accepted(self):
        """`owner` is the generic ECS owner, not a foreign key: on a real city 59% of them
        point at net NODES, which get no record at all (§5.1). Requiring it to resolve would
        fail every correct bundle, so the validator reports the split as a note instead."""
        bundle = self.fresh()
        # The example's three lanes all resolve, so the mutated one is the ONLY unresolvable
        # owner and the note must say "1 into neither". Asserting on the bare phrase would
        # pass without the mutation at all — the validator emits it unconditionally — and
        # would stay green if it stopped counting unresolvable owners entirely.
        self.patch_first_lane(bundle, lambda r: r.update(owner=7777))
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertTrue(
            any("1 into neither" in n for n in report.notes),
            f"expected the note to count exactly one, got: {report.notes}",
        )

    def test_lane_owner_note_counts_zero_when_all_resolve(self):
        """The other half of the gate above: unmutated, the count must be zero. Without this
        the assertion could be satisfied by a validator that always reported 1."""
        report = validate_bundle(self.pristine, DEFAULT_SCHEMA_DIR)
        self.assertTrue(
            any("0 into neither" in n for n in report.notes),
            f"expected a zero count on the pristine example, got: {report.notes}",
        )

    def test_lane_prefab_unknown_category_rejected(self):
        bundle = self.fresh()
        self.patch_lane_catalogue(bundle, lambda c: c["Example_Fence"].update(category="powerline"))
        self.assert_rejected(bundle, "'powerline' is not one of")

    def test_lane_part_without_bounds_rejected(self):
        """The part's z span IS the tile length the tile count is measured in, so a part
        without bounds cannot be tiled at all."""
        bundle = self.fresh()
        self.patch_lane_catalogue(bundle, lambda c: c["Example_Fence"]["parts"][0].pop("boundsMax"))
        self.assert_rejected(bundle, "missing required property 'boundsMax'")

    def test_lane_part_unknown_decal_role_rejected(self):
        """`decalRole` replaced an assetId string-match precisely so the vocabulary is closed;
        an absent key means unknown, but a wrong one would be read as a class."""
        bundle = self.fresh()
        self.patch_lane_catalogue(
            bundle, lambda c: c["Example_Fence"]["parts"][0].update(decal=True, decalRole="kerb")
        )
        self.assert_rejected(bundle, "'kerb' is not one of")

    def test_color_variation_needs_three_channels(self):
        """A ColorSet is exactly three channels because the colorMask has exactly three
        selector channels; two would leave a mask channel selecting nothing."""
        bundle = self.fresh()
        path = bundle / "colors.json"
        colors = json.loads(path.read_text(encoding="utf-8"))
        entry = colors["carto_example_cube"][0]
        entry["channels"] = entry["channels"][:2]
        path.write_text(json.dumps(colors, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "minItems 3")

    def test_punctual_light_without_position_rejected(self):
        """The two families are told apart by shape: a `kind: punctual` entry that carries no
        position matches neither, rather than silently falling back to the centroid rule that
        would move every streetlamp to the middle of its pole."""
        bundle = self.fresh()
        path = bundle / "lights.json"
        lights = json.loads(path.read_text(encoding="utf-8"))
        lights["carto_example_cube"][1].pop("position")
        path.write_text(json.dumps(lights, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "matched no oneOf branch")

    def test_appearance_key_matching_no_mesh_is_noted(self):
        """A §7.2 key joins to the mesh layer. One that matches nothing is not an error — a
        real rip writes 184 palette keys for tree states §6.1 does not export, and 86 light
        keys for vehicles both rip queries exclude — but no consumer lookup can ever reach it,
        which is worth saying once."""
        bundle = self.fresh()
        path = bundle / "colors.json"
        colors = json.loads(path.read_text(encoding="utf-8"))
        colors["Nothing_Has_This_Submesh"] = colors["carto_example_cube"]
        path.write_text(json.dumps(colors, indent=2) + "\n", encoding="utf-8")
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertTrue(
            any("match no mesh file" in n for n in report.notes),
            f"expected an unmatched-key note, got: {report.notes}",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
