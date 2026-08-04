#!/usr/bin/env python3
"""Acceptance test for the Phase-0 bundle contract freeze.

Positive: the generated example bundle validates clean.
Negative: each way a bundle can be wrong is actually caught — a validator that accepts
everything would prove nothing, so every invariant the contract claims gets a test that
breaks it on purpose.

Usage:  python tools/test_bundle_contract.py
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_example_bundle  # noqa: E402
from validate_bundle import DEFAULT_SCHEMA_DIR, read_glb, validate_bundle  # noqa: E402


class BundleContractTest(unittest.TestCase):
    tmp: Path
    pristine: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="carto-bundle-test-"))
        cls.pristine = cls.tmp / "pristine"
        cls.generate(cls.pristine)

    @staticmethod
    def generate(target: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            make_example_bundle.build(target)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)) / "bundle"
        shutil.copytree(self.pristine, target)
        return target

    def errors(self, bundle: Path) -> list:
        return validate_bundle(bundle, DEFAULT_SCHEMA_DIR).errors

    def assertRejected(self, bundle: Path, needle: str) -> None:
        errors = self.errors(bundle)
        self.assertTrue(errors, "expected the bundle to be rejected, but it validated clean")
        joined = "\n".join(errors)
        self.assertIn(needle, joined, f"expected an error mentioning {needle!r}, got:\n{joined}")

    @staticmethod
    def patch_manifest(bundle: Path, mutate) -> None:
        path = bundle / "bundle.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def patch_first_instance(bundle: Path, mutate) -> None:
        path = bundle / "instances.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        mutate(record)
        lines[0] = json.dumps(record, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- positive ---------------------------------------------------------------------

    def test_example_bundle_is_valid(self):
        report = validate_bundle(self.pristine, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_glb_winding_matches_the_documented_convention(self):
        """frame.meshFrame claims front faces are CCW in the right-handed mesh frame.

        Decode the GLB and check it: for a convex mesh, the right-handed cross product of a
        triangle's edges must point away from the centroid. If the winding were inherited from
        CS2 unreversed, every face would be inside-out and every face here would fail.
        """
        gltf, _ = read_glb(self.pristine / "meshes" / "carto_example_cube.glb")
        data = (self.pristine / "meshes" / "carto_example_cube.glb").read_bytes()
        json_len, _ = struct.unpack_from("<I4s", data, 12)
        bin_start = 20 + json_len + 8

        def accessor(index):
            acc = gltf["accessors"][index]
            view = gltf["bufferViews"][acc["bufferView"]]
            base = bin_start + view["byteOffset"]
            fmt, width = {5126: ("f", 4), 5123: ("H", 2)}[acc["componentType"]]
            arity = {"VEC3": 3, "VEC2": 2, "SCALAR": 1}[acc["type"]]
            out = []
            for i in range(acc["count"]):
                off = base + i * arity * width
                values = struct.unpack_from("<" + fmt * arity, data, off)
                out.append(values if arity > 1 else values[0])
            return out

        prim = gltf["meshes"][0]["primitives"][0]
        positions = accessor(prim["attributes"]["POSITION"])
        indices = accessor(prim["indices"])
        self.assertEqual(len(positions), 24)
        self.assertEqual(len(indices), 36)

        centre = tuple(sum(p[i] for p in positions) / len(positions) for i in range(3))
        for tri in range(0, len(indices), 3):
            a, b, c = (positions[indices[tri + k]] for k in range(3))
            ab = tuple(b[i] - a[i] for i in range(3))
            ac = tuple(c[i] - a[i] for i in range(3))
            normal = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            outward = tuple((a[i] + b[i] + c[i]) / 3.0 - centre[i] for i in range(3))
            dot = sum(normal[i] * outward[i] for i in range(3))
            self.assertGreater(dot, 0.0, f"triangle at index {tri} is wound inside-out")

    def test_generation_is_deterministic(self):
        again = self.tmp / "again"
        self.generate(again)
        for path in sorted(self.pristine.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self.pristine)
                self.assertEqual(path.read_bytes(), (again / rel).read_bytes(), f"{rel} differs")

    # -- negative: manifest ------------------------------------------------------------

    def test_unknown_manifest_key_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.update({"totallyNewField": 1}))
        self.assertRejected(bundle, "unexpected property 'totallyNewField'")

    def test_wrong_schema_version_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.update({"schema": 2}))
        self.assertRejected(bundle, "expected const 1")

    def test_frame_handedness_is_frozen(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["frame"].update({"handedness": "right"}))
        self.assertRejected(bundle, "expected const 'left'")

    def test_missing_required_geo_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["geo"].pop("originLatLon"))
        self.assertRejected(bundle, "missing required property 'originLatLon'")

    def test_count_mismatch_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["counts"].update({"instances": 99}))
        self.assertRejected(bundle, "counts.instances is 99")

    def test_undeclared_missing_mesh_rejected(self):
        """An instance pointing at a mesh that is not there is an error unless the producer said so."""
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        self.assertRejected(bundle, "no_such_prefab")

    def test_declared_missing_mesh_is_a_warning_not_an_error(self):
        """warnings.jsonl (§10) is how a producer declares an asset it could not deliver.

        Dropping the instances instead would lose their placement, which the contract says must
        survive even when geometry cannot — so a declared gap has to validate, loudly.
        """
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        (bundle / "warnings.jsonl").write_text(
            json.dumps({"kind": "mesh", "assetId": "no_such_prefab",
                        "reason": "empty SubMesh buffer"}) + "\n",
            encoding="utf-8")

        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("no_such_prefab" in w for w in report.warnings),
                        f"expected a warning about the declared gap, got: {report.warnings}")

    def test_missing_mesh_is_reported_once_per_asset_not_per_instance(self):
        """One broken prefab can be referenced by tens of thousands of instances."""
        bundle = self.fresh()
        path = bundle / "instances.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        clones = []
        for i in range(5):
            clone = dict(record, assetId="no_such_prefab", id=90000 + i)
            clone.pop("parent", None)
            clones.append(json.dumps(clone, separators=(",", ":")))
        path.write_text("\n".join(lines + clones) + "\n", encoding="utf-8")

        errors = [e for e in self.errors(bundle) if "no_such_prefab" in e]
        self.assertEqual(len(errors), 1, f"expected one aggregated error, got:\n" + "\n".join(errors))
        self.assertIn("5 instances", errors[0])

    def test_missing_layer_file_rejected(self):
        bundle = self.fresh()
        (bundle / "materials.json").unlink()
        self.assertRejected(bundle, "layers.materials")

    def test_declared_raster_layer_without_raster_block_rejected(self):
        bundle = self.fresh()
        (bundle / "terrain").mkdir()
        (bundle / "terrain" / "elevation.tif").write_bytes(b"II*\x00")
        self.patch_manifest(bundle, lambda m: m["layers"].update({"terrain": "terrain/elevation.tif"}))
        self.assertRejected(bundle, "no 'rasters' block")

    def test_layer_path_escaping_the_bundle_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["layers"].update({"instances": "../instances.jsonl"}))
        self.assertRejected(bundle, "does not match pattern")

    # -- negative: instances -----------------------------------------------------------

    def test_dangling_asset_id_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        self.assertRejected(bundle, "has no mesh at")

    def test_non_unit_quaternion_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"rot": [0, 0, 0, 0.5]}))
        self.assertRejected(bundle, "not a unit quaternion")

    def test_instance_outside_bounds_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"pos": [99999.0, 0.0, 0.0]}))
        self.assertRejected(bundle, "outside bundle.json bounds")

    def test_unknown_instance_kind_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"kind": "spaceship"}))
        self.assertRejected(bundle, "is not one of")

    def test_dangling_parent_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"parent": 7777}))
        self.assertRejected(bundle, "matches no instance id")

    def test_blank_line_in_jsonl_rejected(self):
        bundle = self.fresh()
        path = bundle / "instances.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertRejected(bundle, "blank line")

    # -- negative: networks ------------------------------------------------------------

    def test_network_record_with_both_geometry_forms_rejected(self):
        bundle = self.fresh()
        path = bundle / "networks.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["assetId"] = "carto_example_cube"
        record["pos"] = [0.0, 0.0, 0.0]
        record["rot"] = [0.0, 0.0, 0.0, 1.0]
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "oneOf")

    def test_network_record_with_neither_geometry_form_rejected(self):
        bundle = self.fresh()
        path = bundle / "networks.jsonl"
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record.pop("centerline")
        record.pop("width")
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "oneOf")

    # -- negative: materials & textures -------------------------------------------------

    def test_dangling_texture_reference_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        materials["carto_example_cube_mat"]["normal"] = "textures/deadbeef.png"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "does not exist")

    def test_texture_not_named_by_its_hash_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        old = bundle / materials["carto_example_cube_mat"]["baseColor"]
        new = old.with_name("misnamed.png")
        old.rename(new)
        materials["carto_example_cube_mat"]["baseColor"] = f"textures/{new.name}"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "must be named by its own SHA-256")

    def test_unknown_material_field_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        materials["carto_example_cube_mat"]["specularGloss"] = "textures/x.png"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "unexpected property 'specularGloss'")

    # -- negative: meshes ---------------------------------------------------------------

    def test_corrupt_glb_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        path.write_bytes(b"NOPE" + path.read_bytes()[4:])
        self.assertRejected(bundle, "bad magic")

    def test_truncated_glb_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        path.write_bytes(path.read_bytes()[:-16])
        self.assertRejected(bundle, "!= file size")

    def test_glb_material_not_in_materials_json_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        path.write_text(json.dumps({"some_other_mat": {}}, indent=2) + "\n", encoding="utf-8")
        self.assertRejected(bundle, "is not a key in materials.json")

    def test_glb_image_uri_escaping_the_bundle_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        data = bytearray(path.read_bytes())
        json_len, _ = struct.unpack_from("<I4s", data, 12)
        chunk = data[20:20 + json_len].decode("utf-8")
        # Same length, so no offset in the container needs rewriting.
        broken = chunk.replace('"uri":"../textures/', '"uri":"../../xxxxx/', 1)
        self.assertNotEqual(broken, chunk)
        data[20:20 + json_len] = broken.encode("utf-8")
        path.write_bytes(bytes(data))
        self.assertRejected(bundle, "escapes the bundle directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
