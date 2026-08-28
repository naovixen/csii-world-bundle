#!/usr/bin/env python3
"""Bundle-contract gate, asset half — meshes, GLB containers, textures, materials.

Split from `test_bundle_contract.py` (refactor Phase 4); shares `contractbase`.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from bundlecheck.glbcheck import read_glb
from contractbase import ContractCase
from validate_bundle import DEFAULT_SCHEMA_DIR, validate_bundle


class AssetContractTest(ContractCase):
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

    def test_corrupt_glb_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        path.write_bytes(b"NOPE" + path.read_bytes()[4:])
        self.assert_rejected(bundle, "bad magic")

    def test_truncated_glb_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        path.write_bytes(path.read_bytes()[:-16])
        self.assert_rejected(bundle, "!= file size")

    def test_glb_material_not_in_materials_json_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        path.write_text(json.dumps({"some_other_mat": {}}, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "is not a key in materials.json")

    def test_glb_image_uri_escaping_the_bundle_rejected(self):
        bundle = self.fresh()
        path = bundle / "meshes" / "carto_example_cube.glb"
        data = bytearray(path.read_bytes())
        json_len, _ = struct.unpack_from("<I4s", data, 12)
        chunk = data[20 : 20 + json_len].decode("utf-8")
        # Same length, so no offset in the container needs rewriting.
        broken = chunk.replace('"uri":"../textures/', '"uri":"../../xxxxx/', 1)
        self.assertNotEqual(broken, chunk)
        data[20 : 20 + json_len] = broken.encode("utf-8")
        path.write_bytes(bytes(data))
        self.assert_rejected(bundle, "escapes the bundle directory")

    def test_dangling_texture_reference_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        materials["carto_example_cube_mat"]["normal"] = "textures/deadbeef.png"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "does not exist")

    def test_texture_not_named_by_its_hash_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        old = bundle / materials["carto_example_cube_mat"]["baseColor"]
        new = old.with_name("misnamed.png")
        old.rename(new)
        materials["carto_example_cube_mat"]["baseColor"] = f"textures/{new.name}"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "must be named by its own SHA-256")

    def test_color_mask_is_a_known_map_and_is_resolved(self):
        """The map that makes colors.json applicable is a first-class texture reference.

        Two things at once, because they fail differently: the schema has to accept the key at
        all (it was added to a closed object after the freeze), and the validator has to resolve
        it like any other map rather than passing it through unchecked.
        """
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        materials["carto_example_cube_mat"]["colorMask"] = materials["carto_example_cube_mat"][
            "baseColor"
        ]
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.errors(bundle), [])

        materials["carto_example_cube_mat"]["colorMask"] = "textures/deadbeef.png"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "does not exist")

    def test_unknown_material_field_rejected(self):
        bundle = self.fresh()
        path = bundle / "materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        materials["carto_example_cube_mat"]["specularGloss"] = "textures/x.png"
        path.write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "unexpected property 'specularGloss'")

    def test_dangling_asset_id_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        self.assert_rejected(bundle, "has no mesh at")

    def test_undeclared_missing_mesh_rejected(self):
        """An instance pointing at a mesh that is not there is an error unless the producer said so."""
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        self.assert_rejected(bundle, "no_such_prefab")

    def test_declared_missing_mesh_is_noted_not_warned(self):
        """warnings.jsonl (§10) is how a producer declares an asset it could not deliver.

        Dropping the instances instead would lose their placement, which the contract says must
        survive even when geometry cannot — so a declared gap has to validate. It is a *note*,
        not a warning (PILLARS-2, 2026-08-12): warning again for something the bundle already
        declared makes the warning count a function of the producer's honesty, and on the full
        city it buried every other class under 119 of these.
        """
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        # Append: the example already declares its own undeliverable prop, and overwriting the
        # file would turn that declared gap into an undeclared one.
        path = bundle / "warnings.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8")
            + json.dumps(
                {"kind": "mesh", "assetId": "no_such_prefab", "reason": "empty SubMesh buffer"}
            )
            + "\n",
            encoding="utf-8",
        )
        self.patch_manifest(bundle, lambda m: m["counts"].update({"warnings": 2}))

        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertFalse(
            any("no_such_prefab" in w for w in report.warnings),
            f"a declared gap must not warn, got: {report.warnings}",
        )
        self.assertTrue(
            any("warnings.jsonl" in n and "empty SubMesh buffer" in n for n in report.notes),
            f"expected a note naming the declared reason, got: {report.notes}",
        )

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
        self.assertEqual(
            len(errors), 1, "expected one aggregated error, got:\n" + "\n".join(errors)
        )
        self.assertIn("5 instances", errors[0])

    def _stack_expand(self, bundle: Path) -> None:
        """Turn the example's one placed prefab into a §4.1 stack expansion: two section
        records in place of the plain one, a mesh per section, and the bare prefab mesh and
        its skirt left behind exactly as a real rip leaves them."""
        stem = "carto_example_cube"
        mesh = (bundle / "meshes" / f"{stem}.glb").read_bytes()
        for section in ("first", "last"):
            (bundle / "meshes" / f"{stem}__stack_{section}.glb").write_bytes(mesh)
        (bundle / "meshes" / f"{stem}__base.glb").write_bytes(mesh)

        path = bundle / "instances.jsonl"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["assetId"] != stem:
                out.append(line)
                continue
            for section in ("first", "last"):
                clone = dict(record)
                clone["assetId"] = f"{stem}__stack_{section}"
                out.append(json.dumps(clone, separators=(",", ":")))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        meshes = len(list((bundle / "meshes").glob("*.glb")))
        self.patch_manifest(
            bundle, lambda m: m["counts"].update({"instances": len(out), "meshes": meshes})
        )

    def test_stack_expanded_prefab_meshes_are_not_orphans(self):
        """A stacked prefab (pillar, high wall, fence) is placed only through its
        `__stack_*` sections, so the bare mesh and its skirt are unreferenced by
        construction — not a shipping mistake, and not worth a warning apiece."""
        bundle = self.fresh()
        self._stack_expand(bundle)
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        orphans = [w for w in report.warnings if "not referenced by any instance" in w]
        self.assertEqual(orphans, [])

    def test_unplaced_section_of_a_placed_stack_is_not_an_orphan(self):
        """The producer writes every section a stacked prefab has; which ones a city needs
        depends on how tall its pillars are, so a written-but-unplaced `__stack_mid` is
        normal."""
        bundle = self.fresh()
        self._stack_expand(bundle)
        mesh = (bundle / "meshes" / "carto_example_cube.glb").read_bytes()
        (bundle / "meshes" / "carto_example_cube__stack_mid.glb").write_bytes(mesh)
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        orphans = [w for w in report.warnings if "not referenced by any instance" in w]
        self.assertEqual(orphans, [])

    def test_a_genuinely_unreferenced_mesh_still_warns(self):
        """The exemptions above must not become a blanket amnesty — a mesh nothing places,
        by any route, is still what this warning is for."""
        bundle = self.fresh()
        self._stack_expand(bundle)
        mesh = (bundle / "meshes" / "carto_example_cube.glb").read_bytes()
        (bundle / "meshes" / "Nobody_Places_This.glb").write_bytes(mesh)
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        orphans = [w for w in report.warnings if "not referenced by any instance" in w]
        self.assertEqual(
            orphans, ["Nobody_Places_This.glb: mesh is not referenced by any instance"]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
