#!/usr/bin/env python3
"""Bundle-contract gate, ground half — §8 rasters, §8b areas, §8c clip volumes, grading.

Split from `test_bundle_contract.py` (refactor Phase 4); shares `contractbase`.
"""

from __future__ import annotations

import json
from pathlib import Path

from contractbase import ContractCase
from examplebundle import payloads
from validate_bundle import DEFAULT_SCHEMA_DIR, validate_bundle


class GroundContractTest(ContractCase):
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

    def test_declared_raster_layer_without_raster_block_rejected(self):
        bundle = self.fresh()
        self.patch_manifest(bundle, lambda m: m.pop("rasters"))
        self.assert_rejected(bundle, "no 'rasters' block")

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
            payloads.make_gray_png(flat, grid[0], grid[1], 8)
        )
        self.assert_rejected(bundle, "bit depth")

    def test_raster_not_grayscale_rejected(self):
        bundle = self.fresh()
        (bundle / "terrain" / "surface_index.png").write_bytes(payloads.make_checker_png(32, 8))
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

    def patch_area(self, bundle: Path, mutate) -> None:
        path = bundle / "areas.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        mutate(record)
        path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

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

    def patch_grading(self, bundle: Path, mutate) -> None:
        path = bundle / "grading.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

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


if __name__ == "__main__":
    import unittest

    unittest.main()
