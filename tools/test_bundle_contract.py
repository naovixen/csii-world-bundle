#!/usr/bin/env python3
"""Acceptance test for the Phase-0 bundle contract freeze — manifest, ids, records.

Positive: the generated example bundle validates clean. Negative: each way a bundle's
manifest, record identities or network geometry can be wrong is actually caught. The mesh,
texture and glb invariants are `test_contract_assets.py`; the lane layers
`test_contract_lanes.py`; the ground layers `test_contract_ground.py` (split from one class,
refactor Phase 4).

Usage:  python tools/test_bundle_contract.py
"""

from __future__ import annotations

import json
from pathlib import Path

from contractbase import ContractCase
from validate_bundle import DEFAULT_SCHEMA_DIR, validate_bundle


class ManifestAndRecordsTest(ContractCase):
    def test_example_bundle_is_valid(self):
        report = validate_bundle(self.pristine, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_generation_is_deterministic(self):
        again = self.tmp / "again"
        self.generate(again)
        for path in sorted(self.pristine.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self.pristine)
                self.assertEqual(path.read_bytes(), (again / rel).read_bytes(), f"{rel} differs")

    def test_unknown_manifest_key_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.update({"totallyNewField": 1}))
        self.assert_rejected(bundle, "unexpected property 'totallyNewField'")

    def test_wrong_schema_version_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.update({"schema": 1}))
        self.assert_rejected(bundle, "expected const 2")

    def test_frame_handedness_is_frozen(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["frame"].update({"handedness": "right"}))
        self.assert_rejected(bundle, "expected const 'left'")

    def test_missing_required_geo_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["geo"].pop("originLatLon"))
        self.assert_rejected(bundle, "missing required property 'originLatLon'")

    def test_count_mismatch_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["counts"].update({"instances": 99}))
        self.assert_rejected(bundle, "counts.instances is 99")

    def test_declared_layer_without_count_rejected(self):
        """A record layer declared in `layers` must carry its count (§3) — that pairing is
        what lets a consumer detect a truncated layer added after the freeze."""
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["counts"].pop("areas"))
        self.assert_rejected(bundle, "layers.areas is declared but counts.areas is missing")

    def test_count_without_declared_layer_rejected(self):
        """A non-zero count for an undeclared layer describes records nobody can find."""
        bundle = self.fresh()

        def mutate(manifest):
            manifest["layers"].pop("clipVolumes")
            manifest["counts"]["clipVolumes"] = 2

        self.patch_manifest(bundle, mutate)
        self.assert_rejected(bundle, "no layers.clipVolumes is declared")

    def test_missing_layer_file_rejected(self):
        bundle = self.fresh()
        (bundle / "materials.json").unlink()
        self.assert_rejected(bundle, "layers.materials")

    def test_layer_path_escaping_the_bundle_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(
            bundle, lambda m: m["layers"].update({"instances": "../instances.jsonl"})
        )
        self.assert_rejected(bundle, "does not match pattern")

    def test_blank_line_in_jsonl_rejected(self):
        bundle = self.fresh()
        path = bundle / "instances.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "blank line")

    def test_instance_outside_bounds_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"pos": [99999.0, 0.0, 0.0]}))
        self.assert_rejected(bundle, "outside bundle.json bounds")

    def test_unknown_instance_kind_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"kind": "spaceship"}))
        self.assert_rejected(bundle, "is not one of")

    def test_non_unit_quaternion_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"rot": [0, 0, 0, 0.5]}))
        self.assert_rejected(bundle, "not a unit quaternion")

    def _append_shared_id_records(self, bundle: Path, *suffixes: str) -> None:
        """Append records sharing id 91000 whose assetIds are the cube plus each suffix,
        with the mesh files present so only the id rule is under test."""
        path = bundle / "instances.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        cube = bundle / "meshes" / f"{record['assetId']}.glb"
        for suffix in set(suffixes):
            if suffix:
                (bundle / "meshes" / f"{record['assetId']}{suffix}.glb").write_bytes(
                    cube.read_bytes()
                )
        clones = []
        for suffix in suffixes:
            clone = dict(record, id=91000, assetId=record["assetId"] + suffix)
            clone.pop("parent", None)
            clones.append(json.dumps(clone, separators=(",", ":")))
        path.write_text("\n".join(lines + clones) + "\n", encoding="utf-8")

        def mutate(manifest):
            manifest["counts"]["instances"] += len(clones)
            manifest["counts"]["meshes"] = len(list((bundle / "meshes").glob("*.glb")))

        self.patch_manifest(bundle, mutate)

    def test_stack_sections_sharing_an_id_accepted(self):
        """§4.1: every section record of one stacked instance shares that instance's id —
        the only sanctioned repetition of an id."""
        bundle = self.fresh()
        self._append_shared_id_records(bundle, "__stack_first", "__stack_mid")
        self.assertEqual(self.errors(bundle), [])

    def test_duplicate_id_outside_a_stack_rejected(self):
        bundle = self.fresh()
        self._append_shared_id_records(bundle, "", "")
        self.assert_rejected(bundle, "duplicate id 91000")

    def test_parent_resolving_to_network_record_accepted(self):
        """Street furniture is owned by road edges: parent joins into networks.jsonl too (§4)."""
        bundle = self.fresh()
        network_id = json.loads(
            (bundle / "networks.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )["id"]
        self.patch_first_instance(bundle, lambda r: r.update({"parent": network_id}))
        self.assertEqual(self.errors(bundle), [])

    def test_unresolvable_parent_is_one_aggregated_warning(self):
        """Owners the bundle does not carry (utility edges, net nodes) leave a dangling
        parent; §4 makes parent advisory, so that is a warning — and one line, not one per
        record, because a real city has tens of thousands of them."""
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"parent": 7777}))
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        matches = [w for w in report.warnings if "resolves to no exported record" in w]
        self.assertEqual(
            len(matches), 1, f"expected one aggregated warning, got:\n{report.warnings}"
        )

    def test_network_record_with_both_geometry_forms_rejected(self):
        bundle = self.fresh()
        path = bundle / "networks.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["assetId"] = "carto_example_cube"
        record["pos"] = [0.0, 0.0, 0.0]
        record["rot"] = [0.0, 0.0, 0.0, 1.0]
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "oneOf")

    def test_network_record_with_neither_geometry_form_rejected(self):
        bundle = self.fresh()
        path = bundle / "networks.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record.pop("centerline")
        record.pop("width")
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "oneOf")


if __name__ == "__main__":
    import unittest

    unittest.main()
