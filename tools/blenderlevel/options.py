"""The level oracle's command line."""

import sys


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    opts = {
        "level": None,
        "center": None,
        "radius": 250.0,
        "products": None,
        "terrain": True,
        "save": None,
        "ter": None,
        "forest": True,
        "forest_max": 40000,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--level":
            opts["level"] = argv[i + 1]
            i += 2
        elif a == "--center":
            opts["center"] = (float(argv[i + 1]), float(argv[i + 2]))
            i += 3
        elif a == "--radius":
            opts["radius"] = float(argv[i + 1])
            i += 2
        elif a == "--products":
            opts["products"] = set(argv[i + 1].split(","))
            i += 2
        elif a == "--no-terrain":
            opts["terrain"] = False
            i += 1
        elif a == "--no-forest":
            opts["forest"] = False
            i += 1
        elif a == "--forest-max":
            opts["forest_max"] = int(argv[i + 1])
            i += 2
        elif a == "--ter":
            # diagnostic: another level's .ter (same size/origin) in place of this level's — the
            # A/B of a terrain-stage change against the shapes that did not change
            opts["ter"] = argv[i + 1]
            i += 2
        elif a == "--save":
            opts["save"] = argv[i + 1]
            i += 2
        else:
            raise SystemExit(f"unknown argument {a}")
    if not opts["level"] or not opts["center"]:
        raise SystemExit("--level and --center are required")
    return opts
