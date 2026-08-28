"""One validation run's shared state — the single argument every check takes.

The checks run in order and each adds to the same blackboard: `objects` and `by_class` are read
by six of them, `missing` and `unbound` are filled by three and reported by the last, and
`checks` is the report the caller gets back. One signature for every check is what lets
`validate_level.check` hold them as a registry instead of one 549-line function.

Order is part of the contract, not an accident of layout. `problems` is a list a person reads,
and the two bulk lists — the missing files and the unbound materials — are reported at the END
by `deferred`, after every specific complaint, because twenty lines of "missing file:" above the
one sentence explaining why is the wrong way round. That was the shape of the original and it is
now something a reader can see rather than something they have to notice.
"""

import os


class LevelContext:
    """Everything a check may read or add to. Built once per `validate_level.check` call."""

    def __init__(self, level_dir, bundle_dir, progress=None, settings=None):
        self.level_dir = level_dir
        self.bundle_dir = bundle_dir
        self.progress = progress
        self.settings = settings
        self.level_name = os.path.basename(os.path.normpath(level_dir))

        #: What the caller gets back: a name -> measurement for the build report.
        self.checks = {}
        #: What is wrong, in the order it was found.
        self.problems = []

        #: `[(relative file, line number, object)]` for every object in the level tree.
        self.objects = []
        #: `class -> [object]`, the same objects grouped.
        self.by_class = {}

        #: Referenced files that are not there, reported in bulk by `deferred`.
        self.missing = []
        #: `material symbol -> the shape that binds it`, likewise reported by `deferred`.
        self.unbound = {}

        #: `[(name, path)]` — the COLLADA sources that survive on disk. `keep_dae: false` drops
        #: the ones whose `.cdae` is current, so this is often shorter than the shape count.
        self.dae_files = []
        self.seen_dae = set()
        #: `art/forest/managedItemData.json`, the forest's type table.
        self.item_data = {}

        #: `(size, heights, layers, names, block)` once the `.ter` has been read, else None.
        self.terrain = None

    def note(self, text):
        """Record something wrong with the level."""
        self.problems.append(text)

    def resolve(self, game_path):
        """A BeamNG `/levels/<name>/...` path as a real one, or None if it is not ours.

        A `core/` or `art/` path is BeamNG's own and not something this level can be missing.
        """
        if not game_path:
            return None
        p = str(game_path).replace("\\", "/")
        prefix = f"/levels/{self.level_name}/"
        if p.startswith(prefix):
            return os.path.join(self.level_dir, *p[len(prefix) :].split("/"))
        if p.startswith("levels/" + self.level_name + "/"):
            tail = p[len("levels/" + self.level_name + "/") :]
            return os.path.join(self.level_dir, *tail.split("/"))
        return None  # a core/ or art/ path: BeamNG's own, not ours to check
