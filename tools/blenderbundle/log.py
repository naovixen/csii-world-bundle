"""Console narration for the Blender adapter: phase headers, timings, progress, colours.

One module-level `LOG` because there is one console. Every count the A/B gate reads
(`tools/ab_compare.py --counts`) goes through here, so the styling and the numbers stay
separable — see the class docstring for the `in N.N s` convention that keeps them so.
"""

import os
import sys
import time


class Log:
    """Styled console narration: phase headers, timings, progress, colours.

    Colours and in-place progress bars only when stdout is a real console and NO_COLOR
    is unset; piped output (Select-String workflows) gets plain, line-oriented text.
    All counts stay in the text — the A/B gate diffs the numbers, not the styling.

    `done()` appends its elapsed time as `in N.N s` rather than `(N.N s)`: the A/B gate
    (tools/ab_compare.py --counts) strips exactly that `in <N> s` suffix before comparing
    the numbers on a line, so a phase-closing summary that also matches one of its count
    markers (`lanes:`, `networks:`, `terrain:`, `instance(s)`, …) still gates cleanly. The
    parenthesised form the gate does not know about would inject a run-to-run-varying
    number into every such line and fail the comparison on jitter alone. Calling `done()`
    with no phase open (self._phase_started is None — the phase already closed itself, or
    was never opened) appends nothing, which is how a final recap line that already spells
    out its own `in N.N s` avoids getting a second, redundant timing suffix.
    """

    def __init__(self):
        self.tty = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if self.tty and os.name == "nt":
            os.system("")  # enable VT processing in classic consoles
        self._phase_started = None
        self._bar_open = False
        self._last_draw = 0.0

    def _c(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.tty else text

    def phase(self, title):
        self.progress_end()
        self._phase_started = time.time()
        print(self._c("1;36", f"==> {title}"))

    def done(self, summary):
        self.progress_end()
        elapsed = f" in {time.time() - self._phase_started:.1f} s" if self._phase_started else ""
        self._phase_started = None
        print(self._c("32", f"    {summary}{elapsed}"))

    def end_phase(self):
        """Force a phase closed without printing anything, in case its own body never did.

        A phase's build_* function can early-return (a missing layer file) without ever
        reaching its own `done()` call, leaving `_phase_started` stale. Left alone, that
        stale timestamp is invisible until some LATER, unrelated `done()` call inherits it
        — printing a second `in N.N s` suffix on a line that may already have its own (the
        final "placed N instance(s) …" recap is exactly such a line). Called once at the
        natural end of each phase's owning block in `main()`, regardless of whether the
        phase's body already closed itself; harmless (a no-op) when it did.
        """
        self.progress_end()
        self._phase_started = None

    def step(self, msg):
        self.progress_end()
        print(f"  {msg}")

    def warn(self, msg):
        self.progress_end()
        print(self._c("31" if "CONTRACT VIOLATION" in msg else "33", f"  {msg}"))

    def progress(self, label, done, total):
        now = time.time()
        if self.tty:
            if now - self._last_draw < 0.1 and done != total:
                return
            self._last_draw = now
            if total:
                width = 28
                fill = int(width * done / max(total, 1))
                bar = "#" * fill + "-" * (width - fill)
                sys.stdout.write(f"\r  [{bar}] {label} {done}/{total}   ")
            else:
                sys.stdout.write(f"\r  … {label} {done}   ")
            sys.stdout.flush()
            self._bar_open = True
        else:
            stride = max(1, (total // 20) if total else 5000)
            if done % stride == 0 or done == total:
                print(f"  … {label} {done}" + (f"/{total}" if total else ""))

    def progress_end(self):
        if self._bar_open:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()
            self._bar_open = False


LOG = Log()
