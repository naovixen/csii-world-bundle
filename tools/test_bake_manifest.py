"""Gate for the bake framework (contract §13): `derived/derived.json` validates against
`docs/schema/derived.schema.json`, and staleness is decided PER PRODUCT from input fingerprints.

Runs against `docs/example-bundle` copied into a temp dir (so nothing under docs/ changes) with a
stub product registered for the test — the real products arrive in later phases and share this
machinery. Plain `python tools/test_bake_manifest.py`; stdlib + numpy only.
"""

import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_bundle  # noqa: E402  (the repo's stdlib schema evaluator)
from csiibundle import bake  # noqa: E402
from csiibundle.bake import manifest as bake_manifest  # noqa: E402
from csiibundle.bake.progress import Progress  # noqa: E402
from csiibundle.bake.runner import run  # noqa: E402

REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, "docs", "example-bundle")
SCHEMA = os.path.join(REPO, "docs", "schema", "derived.schema.json")


class Counting(Progress):
    def __init__(self):
        self.begun = []
        self.logs = []

    def begin(self, name, total=None, unit="items"):  # `unit`: the progress protocol
        self.begun.append((name, total))

    def log(self, message):
        self.logs.append(message)


def _stub(context, progress):
    """A product that reads `instances` and writes one small file under its path."""
    progress.begin("stub", total=3)
    out = os.path.join(context.derived, "stub")
    os.makedirs(out, exist_ok=True)
    count = 0
    with open(context.layer_path("instances"), encoding="utf-8") as handle:
        for _line in handle:
            count += 1
    for _ in range(3):
        progress.advance()
    with open(os.path.join(out, "count.txt"), "w", encoding="utf-8") as handle:
        handle.write(str(count))
    progress.end()
    return {"records": count, "_tiles": [(0, 0), (-1, 2)]}


def main():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
            print("FAIL:", msg)
        else:
            print("ok:  ", msg)

    with tempfile.TemporaryDirectory() as tmp:
        bundle = os.path.join(tmp, "bundle")
        shutil.copytree(EXAMPLE, bundle)
        registry = {"stub": bake.Product("stub", ("instances",), "stub/", _stub)}
        derived = os.path.join(bundle, "derived")

        # 1. a fresh bake writes a manifest that validates against the schema
        prog = Counting()
        outcomes = run(bundle, registry=registry, progress=prog)
        check([o.status for o in outcomes] == ["baked"], f"first run bakes the stub: {outcomes}")
        doc = json.load(open(os.path.join(derived, "derived.json"), encoding="utf-8"))
        validator = validate_bundle.Validator(
            json.load(open(SCHEMA, encoding="utf-8")), "derived.schema.json"
        )
        errors = validator.validate(doc)
        check(not errors, f"derived.json validates against derived.schema.json {errors[:3]}")
        check(doc["products"]["stub"]["tiles"] == [[0, 0], [-1, 2]], "tiles recorded")
        check(doc["products"]["stub"]["stats"]["records"] >= 1, "stats recorded")
        check(("stub", 3) in prog.begun, "product reported progress with a total")

        # 2. a second run finds it fresh and does not re-bake
        outcomes = run(bundle, registry=registry, progress=Counting())
        check([o.status for o in outcomes] == ["fresh"], f"second run is fresh: {outcomes}")

        # 3. --check reports without baking
        outcomes = run(bundle, registry=registry, progress=Counting(), check_only=True)
        check([o.status for o in outcomes] == ["fresh"], "check-only reports fresh")

        # 4. touching an INPUT layer makes it stale; touching an unrelated layer does not
        unrelated = os.path.join(bundle, "networks.jsonl")
        os.utime(unrelated, (time.time() + 5, time.time() + 5))
        outcomes = run(bundle, registry=registry, progress=Counting(), check_only=True)
        check([o.status for o in outcomes] == ["fresh"], "unrelated layer change keeps it fresh")
        instances = os.path.join(bundle, "instances.jsonl")
        os.utime(instances, (time.time() + 5, time.time() + 5))
        outcomes = run(bundle, registry=registry, progress=Counting(), check_only=True)
        check(
            [o.status for o in outcomes] == ["stale"] and "instances" in outcomes[0].reason,
            f"input change makes it stale: {outcomes}",
        )
        outcomes = run(bundle, registry=registry, progress=Counting())
        check([o.status for o in outcomes] == ["baked"], "stale product re-bakes")

        # 5. a different shaping option is a different cache
        outcomes = run(
            bundle, registry=registry, progress=Counting(), options={"tile": 128.0}, check_only=True
        )
        check([o.status for o in outcomes] == ["stale"], "different tile size is stale")

        # 6. force re-bakes
        outcomes = run(bundle, registry=registry, progress=Counting(), force=True)
        check([o.status for o in outcomes] == ["baked"], "force re-bakes")

        # 7. cancellation returns promptly and leaves nothing half-recorded
        class CancelAtOnce(Counting):
            def cancelled(self):
                return True

        def _cancellable(context, progress):
            progress.begin("c", total=10)
            for _ in range(10):
                progress.check()
                progress.advance()
            return {}

        registry2 = {"c": bake.Product("c", ("instances",), "c/", _cancellable)}
        outcomes = run(bundle, registry=registry2, progress=CancelAtOnce(), force=True)
        check([o.status for o in outcomes] == ["cancelled"], "cancel is honoured between chunks")
        doc = bake_manifest.load(derived)
        check("c" not in doc["products"], "a cancelled product is not recorded")

        # 8. quick check refuses a bundle without a manifest
        broken = os.path.join(tmp, "broken")
        os.makedirs(broken)
        try:
            run(broken, registry=registry)
            check(False, "quick check should refuse a manifest-less bundle")
        except ValueError:
            check(True, "quick check refuses a manifest-less bundle")

        # 9. the example bundle itself must not have gained a derived/ dir
        check(not os.path.isdir(os.path.join(EXAMPLE, "derived")), "docs/example-bundle untouched")

        # 10. every option a REAL product reads must be declared in its `Product.options`
        #
        # `Product.options` is what makes an option change re-bake that product. An option the
        # product's code reads but does not declare is a silent no-op: the bake logs
        # `fresh (inputs unchanged), skipped` and the build ships the previous bytes with no
        # error anywhere. `paint_kinds` did exactly that on 2026-08-18 - the setting was wired
        # end to end, the level rebuilt in 25 minutes, and 448k props still drew the untinted
        # albedo. Same failure shape as the `version` one, and the mechanism existed both times.
        framework = set(bake_manifest.SHAPING_OPTIONS) | {
            "jobs"
        }  # reshape everything / split the work
        reads = re.compile(r'options\.get\(\s*"([a-z_]+)"')
        for name, product in bake.products().items():
            source = inspect.getsource(inspect.getmodule(product.run))
            undeclared = sorted(set(reads.findall(source)) - framework - set(product.options))
            check(
                not undeclared,
                f"product {name!r} declares every option it reads"
                + (
                    f" - reads {undeclared}, declares {list(product.options)}" if undeclared else ""
                ),
            )

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all bake manifest checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
