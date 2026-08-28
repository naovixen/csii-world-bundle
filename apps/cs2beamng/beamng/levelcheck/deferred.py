"""The two bulk lists, reported after every specific complaint.

`missing` and `unbound` are filled by three checks between them and are capped lists of the same
kind of thing. They go last on purpose: twenty lines of "missing file:" above the one sentence
saying why the level will not load is the wrong way round for the person reading it. In the
549-line function this was the last four lines, and the ordering was a property of where they
happened to sit rather than something anybody had decided.
"""


def check_deferred(ctx):
    """Report the unbound materials and the missing files gathered by the checks above."""
    for sym, where in list(ctx.unbound.items())[:20]:
        ctx.note(f"material {sym!r} bound by {where} has no mapTo in any main.materials.json")
    for m in ctx.missing[:20]:
        ctx.note("ctx.missing file: " + m)
