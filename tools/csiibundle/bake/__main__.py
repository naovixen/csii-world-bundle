"""CLI: `python -m csiibundle.bake [bundle] [options]` (also installed as `csiibundle-bake`).

    csiibundle-bake                          # the default bundle (ModsData/Carto), all products
    csiibundle-bake <bundle> --check         # report fresh/stale per product, bake nothing
    csiibundle-bake <bundle> --only markings,areas --region 2994 1853 300
    csiibundle-bake <bundle> --force         # re-bake everything
    csiibundle-bake <bundle> --derived-dir D # cache somewhere other than <bundle>/derived/

Exit status: 0 when every requested product is fresh or baked, 1 when the bundle failed the
quick check or a product failed, 130 on cancel (Ctrl-C).
"""

import argparse
import os
import sys

from . import products as _registry
from .locate import default_bundle_dir
from .progress import ConsoleProgress
from .runner import DEFAULT_OPTIONS, run


def _parse(argv):
    parser = argparse.ArgumentParser(prog="csiibundle-bake", description=__doc__.split("\n\n")[0])
    parser.add_argument("bundle", nargs="?", default=None,
                        help="bundle directory (default: the CS2 ModsData export for this OS)")
    parser.add_argument("--derived-dir", default=None, help="where to write the cache (default <bundle>/derived)")
    parser.add_argument("--only", default=None, help="comma-separated product names")
    parser.add_argument("--force", action="store_true", help="re-bake even when fresh")
    parser.add_argument("--check", action="store_true", help="report freshness only")
    parser.add_argument("--tile", type=float, default=DEFAULT_OPTIONS["tile"], help="tile edge in metres")
    parser.add_argument("--diagonal", choices=("nw_se", "ne_sw", "envelope"), default=DEFAULT_OPTIONS["diagonal"],
                        help="heightfield triangulation assumed when conforming to terrain")
    parser.add_argument("--region", nargs=3, type=float, metavar=("X", "Z", "R"), default=None,
                        help="bundle-frame disc to limit world-space products to")
    parser.add_argument("--jobs", type=int, default=1, help="worker processes for products that can use them")
    parser.add_argument("--list", action="store_true", help="list registered products and exit")
    parser.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE",
                        help="product option (JSON value if it parses, else string), e.g. --opt paint_kinds=[\"tree\",\"prop\"]")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    registry = _registry()
    if args.list:
        for name, product in registry.items():
            print(f"{name:14s} -> {product.path:28s} inputs: {', '.join(product.inputs)}")
        return 0

    bundle = os.path.abspath(args.bundle or default_bundle_dir())
    if not os.path.isdir(bundle):
        print(f"bundle directory not found: {bundle}", file=sys.stderr)
        return 1
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    extra = {}
    for item in args.opt:
        key, _, value = item.partition("=")
        try:
            import json as _json
            extra[key] = _json.loads(value)
        except ValueError:
            extra[key] = value
    options = {"tile": args.tile, "diagonal": args.diagonal, "jobs": args.jobs, **extra,
               "region": None if args.region is None else
               {"centerX": args.region[0], "centerZ": args.region[1], "radius": args.region[2]}}

    progress = ConsoleProgress()
    progress.log(f"bundle: {bundle}")
    try:
        outcomes = run(bundle, derived_dir=args.derived_dir, options=options, only=only,
                       force=args.force, progress=progress, check_only=args.check)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130

    width = max([len(o.name) for o in outcomes] + [7])
    for o in outcomes:
        extra = f"  {o.seconds:.1f}s" if o.status == "baked" else ""
        print(f"{o.name:{width}s}  {o.status.upper():9s} {o.reason}{extra}")
    if not outcomes:
        print("no products registered" if not registry else "nothing requested")
    failed = any(o.status in ("failed",) for o in outcomes)
    cancelled = any(o.status == "cancelled" for o in outcomes)
    return 130 if cancelled else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
