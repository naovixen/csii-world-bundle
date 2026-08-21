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

import make_example_bundle
from validate_bundle import DEFAULT_SCHEMA_DIR, read_glb, validate_bundle


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

    def assert_rejected(self, bundle: Path, needle: str) -> None:
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

    # -- clip volumes, contract §8c ----------------------------------------------------

    @staticmethod
    def patch_clip_volume(bundle: Path, index: int, mutate) -> None:
        path = bundle / "clipvolumes.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[index])
        mutate(record)
        lines[index] = json.dumps(record, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_clip_volume_unknown_kind_rejected(self):
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 0, lambda r: r.update(kind="ribbon"))
        self.assert_rejected(bundle, "'ribbon' is not one of")

    def test_clip_volume_inverted_band_rejected(self):
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 0, lambda r: r.update(bottom=30.0, top=0.5))
        self.assert_rejected(bundle, "empty or inverted")

    def test_clip_volume_short_curve_rejected(self):
        """A cubic Bézier is four control points; three is a different curve, silently."""
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 0, lambda r: r.update(left=r["left"][:3]))
        self.assert_rejected(bundle, "minItems 4")

    def test_clip_volume_area_without_polygon_rejected(self):
        """Since schema 2 an area record carries its own polygon (S12.2) — the game never
        paints clip surfaces, so areas.jsonl cannot supply the triangles."""
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 1, lambda r: r.pop("nodes"))
        self.assert_rejected(bundle, "matched no oneOf branch")

    def test_clip_volume_area_with_dangling_triangle_index_rejected(self):
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 1, lambda r: r.update(triangles=[[0, 1, 999]]))
        self.assert_rejected(bundle, "does not index its")

    def test_clip_volume_area_carrying_curves_rejected(self):
        """An area clips with its polygon; boundary curves on it could disagree with it."""
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 1, lambda r: r.update(left=[[0.0, 0.0, 0.0]] * 4))
        self.assert_rejected(bundle, "matched no oneOf branch")

    def test_clip_volume_unbounded_top_accepted(self):
        """An absent `top` means unbounded, which is how a clip surface says 'however high'."""
        bundle = self.fresh()
        self.patch_clip_volume(bundle, 0, lambda r: r.pop("top"))
        self.assertEqual(self.errors(bundle), [])

    def test_bundle_without_clip_volumes_is_valid(self):
        """A bundle ripped before §8c has no clip layer at all; that is not an error."""
        bundle = self.fresh()
        (bundle / "clipvolumes.jsonl").unlink()
        self.patch_manifest(
            bundle, lambda m: (m["layers"].pop("clipVolumes"), m["counts"].pop("clipVolumes"))
        )
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])

    def test_declared_layer_without_count_rejected(self):
        """A record layer declared in `layers` must carry its count (§3) — that pairing is
        what lets a consumer detect a truncated layer added after the freeze."""
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["counts"].pop("areas"))
        self.assert_rejected(bundle, "layers.areas is declared but counts.areas is missing")

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

    def test_bundle_without_grading_is_valid(self):
        """`grading.json` is optional: a rip that found no HDRP volume stack ships no layer,
        and §1's rule is that an absent key is an absent feature."""
        bundle = self.fresh()
        (bundle / "grading.json").unlink()
        self.patch_manifest(bundle, lambda m: m["layers"].pop("grading"))
        self.assertEqual(self.errors(bundle), [])

    def test_declared_grading_layer_must_exist(self):
        """The index is a promise: a declared layer whose file is missing is an error, not a
        consumer's problem to discover."""
        bundle = self.fresh()
        (bundle / "grading.json").unlink()
        self.assert_rejected(bundle, "layers.grading")

    def test_count_without_declared_layer_rejected(self):
        """A non-zero count for an undeclared layer describes records nobody can find."""
        bundle = self.fresh()

        def mutate(manifest):
            manifest["layers"].pop("clipVolumes")
            manifest["counts"]["clipVolumes"] = 2

        self.patch_manifest(bundle, mutate)
        self.assert_rejected(bundle, "no layers.clipVolumes is declared")

    # -- lane geometry, contract §5B v2 ------------------------------------------------

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

    def test_missing_layer_file_rejected(self):
        bundle = self.fresh()
        (bundle / "materials.json").unlink()
        self.assert_rejected(bundle, "layers.materials")

    def test_declared_raster_layer_without_raster_block_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.pop("rasters"))
        self.assert_rejected(bundle, "no 'rasters' block")

    # -- negative: terrain rasters (contract §8) ---------------------------------------

    def test_raster_size_mismatch_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["rasters"].update({"size": [16, 16]}))
        self.assert_rejected(bundle, "rasters.size")

    def test_raster_wrong_bit_depth_rejected(self):
        bundle = self.fresh()
        # An 8-bit heightmap quantises the world to 256 levels; the contract requires 16.
        grid = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))["rasters"]["size"]
        flat = [0] * (grid[0] * grid[1])
        (bundle / "terrain" / "heightmap.png").write_bytes(
            make_example_bundle.make_gray_png(flat, grid[0], grid[1], 8)
        )
        self.assert_rejected(bundle, "bit depth")

    def test_raster_not_grayscale_rejected(self):
        bundle = self.fresh()
        (bundle / "terrain" / "surface_index.png").write_bytes(
            make_example_bundle.make_checker_png(32, 8)
        )
        self.assert_rejected(bundle, "colour type")

    def test_terrain_without_elevation_scale_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["rasters"].pop("elevationScale"))
        self.assert_rejected(bundle, "rasters.elevationScale is missing")

    def test_water_carrying_retired_depth_scale_rejected(self):
        # depthScale died with the depth layer (§12.1, 2026-08-07): the water raster is a
        # surface LEVEL in the elevation encoding, and the closed rasters object rejects
        # the stale key loudly rather than letting a consumer misread level as depth.
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m["rasters"].update({"depthScale": 0.03125}))
        self.assert_rejected(bundle, "depthScale")

    def test_area_triangle_out_of_range_rejected(self):
        bundle = self.fresh()
        path = bundle / "areas.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["triangles"][0] = [0, 1, 99]
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "does not index")

    def test_area_prefab_missing_from_catalogue_rejected(self):
        bundle = self.fresh()
        (bundle / "areaprefabs.json").write_text("{}\n", encoding="utf-8")
        self.assert_rejected(bundle, "not in the areaPrefabs catalogue")

    def test_surfaces_without_index_rejected(self):
        bundle = self.fresh()
        (bundle / "terrain" / "surfaces" / "index.json").unlink()
        self.assert_rejected(bundle, "has no index.json")

    def test_surfaces_index_dangling_texture_rejected(self):
        bundle = self.fresh()
        index_path = bundle / "terrain" / "surfaces" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["1"]["texture"] = "terrain/surfaces/no_such.png"
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "does not exist")

    def test_layer_path_escaping_the_bundle_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(
            bundle, lambda m: m["layers"].update({"instances": "../instances.jsonl"})
        )
        self.assert_rejected(bundle, "does not match pattern")

    # -- negative: instances -----------------------------------------------------------

    def test_dangling_asset_id_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"assetId": "no_such_prefab"}))
        self.assert_rejected(bundle, "has no mesh at")

    def test_non_unit_quaternion_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"rot": [0, 0, 0, 0.5]}))
        self.assert_rejected(bundle, "not a unit quaternion")

    def test_instance_outside_bounds_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"pos": [99999.0, 0.0, 0.0]}))
        self.assert_rejected(bundle, "outside bundle.json bounds")

    def test_unknown_instance_kind_rejected(self):
        bundle = self.fresh()
        self.patch_first_instance(bundle, lambda r: r.update({"kind": "spaceship"}))
        self.assert_rejected(bundle, "is not one of")

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

    def test_blank_line_in_jsonl_rejected(self):
        bundle = self.fresh()
        path = bundle / "instances.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "blank line")

    # -- negative: networks ------------------------------------------------------------

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

    # -- negative: materials & textures -------------------------------------------------

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

    # -- negative: meshes ---------------------------------------------------------------

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

    # -- negative: the lane graph, contract §5A ----------------------------------------

    def patch_first_lane(self, bundle: Path, mutate) -> None:
        path = bundle / "lanes.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        mutate(record)
        lines[0] = json.dumps(record, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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

    # -- negative: the lane-prefab catalogue, contract §5B ------------------------------

    def patch_lane_catalogue(self, bundle: Path, mutate) -> None:
        path = bundle / "laneprefabs.json"
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        mutate(catalogue)
        path.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")

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

    # -- negative: colours and lights, contract §7.2 ------------------------------------

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

    # -- negative: surface areas, contract §8b ------------------------------------------

    def patch_area(self, bundle: Path, mutate) -> None:
        path = bundle / "areas.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        mutate(record)
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    def test_area_height_ranges_must_run_parallel_to_triangles(self):
        """§8b says heightRanges is 'in the same order as triangles', which a consumer indexes
        by — a shorter list silently mis-bands every drape after the first."""
        bundle = self.fresh()
        self.patch_area(bundle, lambda r: r.update(heightRanges=[[0.0, 0.1]]))
        self.assert_rejected(bundle, "heightRanges has 1 entries")

    def test_area_height_ranges_accepted_when_parallel(self):
        bundle = self.fresh()
        self.patch_area(bundle, lambda r: r.update(heightRanges=[[0.0, 0.1], [0.0, 0.05]]))
        self.assertEqual(self.errors(bundle), [])

    def test_area_owner_resolving_nowhere_warns(self):
        """§8b calls this join total (27,047 of 27,156 on the reference city, none dangling),
        so a dangling owner means the instance layer and the area layer came from different
        rips — a mixed-vintage bundle, which is worth a warning rather than silence."""
        bundle = self.fresh()
        self.patch_area(bundle, lambda r: r.update(owner=7777))
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        matches = [w for w in report.warnings if "resolves to no instance" in w]
        self.assertEqual(
            len(matches), 1, f"expected one aggregated warning, got:\n{report.warnings}"
        )

    def test_area_prefab_dangling_normal_map_rejected(self):
        """The catalogue's PBR block is four separate maps; a dangling normalMap is exactly as
        broken as a dangling base map, and used to go unchecked (audit #40 P11)."""
        bundle = self.fresh()
        path = bundle / "areaprefabs.json"
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        catalogue["example_concrete_pad"]["normalMap"] = "terrain/surfaces/no_such_n.png"
        path.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "normalMap")

    def test_area_prefab_unknown_field_rejected(self):
        bundle = self.fresh()
        path = bundle / "areaprefabs.json"
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        catalogue["example_concrete_pad"]["lodBias"] = 1.0
        path.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "unexpected property 'lodBias'")

    # -- negative: warnings, contract §10 -----------------------------------------------

    def test_warning_naming_nothing_rejected(self):
        """The declared-gap amnesty is keyed on `assetId`; a line naming neither an asset nor
        an entity silently stops excusing anything, which is the failure worth catching."""
        bundle = self.fresh()
        (bundle / "warnings.jsonl").write_text(
            json.dumps({"kind": "mesh", "reason": "gone"}) + "\n", encoding="utf-8"
        )
        self.assert_rejected(bundle, "matched no anyOf branch")

    def test_warning_unknown_kind_rejected(self):
        """Kinds are owned per pass and each pass rewrites only its own; an unknown kind is a
        line no pass will ever replace, so it outlives whatever it described."""
        bundle = self.fresh()
        (bundle / "warnings.jsonl").write_text(
            json.dumps({"kind": "terrain", "assetId": "x", "reason": "gone"}) + "\n",
            encoding="utf-8",
        )
        self.assert_rejected(bundle, "'terrain' is not one of")

    # -- negative: the surface index, contract §8 ---------------------------------------

    def test_surface_index_id_out_of_range_rejected(self):
        """The raster is 8-bit, so an id above 255 names a value it cannot hold."""
        bundle = self.fresh()
        path = bundle / "terrain" / "surfaces" / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["300"] = {"name": "impossible", "texture": None}
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        self.assert_rejected(bundle, "does not match pattern")

    def test_surface_index_null_texture_accepted(self):
        """`texture` may be null when the theme texture could not be read (§8) — a stated
        absence, not a broken reference."""
        bundle = self.fresh()
        path = bundle / "terrain" / "surfaces" / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["1"]["texture"] = None
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.errors(bundle), [])

    # -- negative: colour grading, contract §8 ------------------------------------------

    def patch_grading(self, bundle: Path, mutate) -> None:
        path = bundle / "grading.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def test_grading_unevaluated_stack_rejected(self):
        """An all-default block with stackEvaluated false is a FAILED READ, not a neutral
        grade — the first cut of this layer shipped exactly that and it read as a city that
        does not tonemap."""
        bundle = self.fresh()
        self.patch_grading(bundle, lambda d: d["diagnostics"].update(stackEvaluated=False))
        self.assert_rejected(bundle, "stackEvaluated is false")

    def test_grading_no_volumes_found_rejected(self):
        bundle = self.fresh()
        self.patch_grading(bundle, lambda d: d["diagnostics"].update(volumesFound=0))
        self.assert_rejected(bundle, "volumesFound is 0")

    def test_grading_unknown_block_rejected(self):
        """`grading.json` is a closed object on purpose: a block the writer starts emitting
        without the schema knowing it is exactly the drift diagnostics.unread exists to expose,
        and it should not reach a consumer unannounced."""
        bundle = self.fresh()
        self.patch_grading(bundle, lambda d: d.update(bloom={"intensity": 0.5}))
        self.assert_rejected(bundle, "unexpected property 'bloom'")

    def test_grading_external_tonemapping_with_lut_warns(self):
        """mode External WITH a bound LUT means the grading block is incomplete — the LUT is a
        GPU 3D render texture and is not exported, so a consumer replaying this will not match
        the game and must be told."""
        bundle = self.fresh()
        self.patch_grading(
            bundle,
            lambda d: d["tonemapping"].update(
                mode="External",
                hasLut=True,
                lut={
                    "name": "DayLUT",
                    "width": 1024,
                    "height": 32,
                    "type": "RenderTexture",
                    "note": "NOT exported",
                },
            ),
        )
        report = validate_bundle(bundle, DEFAULT_SCHEMA_DIR)
        self.assertEqual(report.errors, [])
        self.assertTrue(
            any("External with a bound LUT" in w for w in report.warnings),
            f"expected an incomplete-grading warning, got: {report.warnings}",
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
