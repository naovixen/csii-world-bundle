"""The bundle-contract test base — a pristine example bundle per class, mutated per case.

Support for the `test_bundle_contract*` / `test_contract_*` gates (split from one 949-line
class in refactor Phase 4), not a test itself: pytest collects the subclasses, each of which
regenerates the pristine bundle once and copies it fresh per case.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import make_example_bundle
from validate_bundle import DEFAULT_SCHEMA_DIR, validate_bundle


class ContractCase(unittest.TestCase):
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
