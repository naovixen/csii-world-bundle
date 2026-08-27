"""Running work across processes without losing cancellation or progress.

Both halves of this repo do the same thing in the same shape: take a list of independent jobs,
run them in a pool when there are enough to be worth one, fall back to a plain loop when there
are not, and report each result as it arrives. Written out eight times — four bake products and
four `cs2beamng` stages — it was eight chances to get the same three rules wrong:

* **A pool is not always faster.** Spawning processes costs hundreds of milliseconds each and
  every argument has to be pickled across; below a threshold the serial loop wins outright. Each
  caller's threshold is its own (a tile job is big, a mask is small), which is why it is an
  argument rather than a constant here.
* **The serial path must behave exactly like the pooled one.** If it does not, a bug appears at
  `jobs > 1` and vanishes under a debugger running with `jobs = 1` — the worst shape a bug has.
  Here there is one loop body and the pool is a detail inside it.
* **Cancellation has to be honoured between results.** `progress.check()` raises `Cancelled` when
  the user presses stop, and a pool that never calls it runs to completion however hard they
  press. The bake promises to notice "at least every half second", and that promise is kept here
  rather than in each caller.

`progress` is duck-typed on `csiibundle.bake.progress.Progress` — `check()` and `advance(n)`.
The app's `QueueProgress` subclasses it, which is why one helper serves both sides.
"""

from multiprocessing import Pool, get_context


def each(
    job,
    args,
    workers,
    progress,
    ordered=False,
    chunksize=1,
    min_parallel=None,
    spawn=False,
    advance=True,
    total=None,
):
    """Yield `job(a)` for every `a` in `args`, in a pool when that is worth it.

    Args:
        job: the callable, run in a worker — must be importable at module scope and picklable.
        args: one argument per job. Consumed into a list only if it is not already one.
        workers: how many processes at most. 1 or less means the serial loop.
        progress: something with `check()` and `advance(n)`.
        ordered: yield in the order of `args`. `False` uses `imap_unordered`, which lets a slow
            job stop holding up the ones behind it — correct unless the caller zips results back
            against its input.
        chunksize: jobs handed to a worker at a time. Larger amortises the IPC per job; smaller
            balances better when jobs differ in cost.
        min_parallel: the fewest jobs worth a pool. Defaults to `chunksize`, which is the smallest
            number at which chunking means anything.
        spawn: force a spawn context. The `cs2beamng` stages need it — a forked worker inherits
            the parent's half-built level object and its open handles. The bake does not, and
            passing it there would change how its workers get the rasters they were given.
        advance: advance progress by one per result. True for a caller whose progress total counts
            JOBS. `False` for one whose total counts something else and which advances itself —
            `stages/networks` counts SEGMENTS and advances by however many a tile held, so one
            per tile would make its bar mean nothing.
        total: how many jobs `args` will yield, when `args` is a GENERATOR that must stay lazy.
            Only the threshold and the process count need the length, and `imap` consumes lazily,
            so a caller that streams can keep streaming. `bake/tiles` must: its generator POPS
            each tile's geometry out of the accumulated buckets as it yields, so the memory is
            released tile by tile — materialising the list first holds a whole product's geometry
            at once, which on the areas product is hundreds of megabytes.

    Yields:
        Each job's return value.
    """
    if total is None:
        args = args if isinstance(args, (list, tuple)) else list(args)
        total = len(args)
    if total <= 0:
        return

    threshold = chunksize if min_parallel is None else min_parallel
    if workers > 1 and total > threshold:
        context = get_context("spawn") if spawn else None
        pool_of = context.Pool if context is not None else Pool
        with pool_of(processes=min(workers, total)) as pool:
            imap = pool.imap if ordered else pool.imap_unordered
            for result in imap(job, args, chunksize=chunksize):
                progress.check()
                if advance:
                    progress.advance()
                yield result
        return

    for a in args:
        progress.check()
        if advance:
            progress.advance()
        yield job(a)
