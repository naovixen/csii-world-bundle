"""The progress protocol the bake reports through — small enough to implement in ten lines.

A product declares how much work it has (`begin(name, total)`), advances as it goes
(`advance(n)`), says things worth keeping (`log`), and asks whether to stop (`cancelled()`).
`total` is in whatever unit the product counts — lanes, tiles, instances — and may be `None`
when it is genuinely unknown, but every product should try: an ETA needs a denominator.

Two implementations ship here: `Progress`, the silent base a caller subclasses, and
`ConsoleProgress`, a single-line terminal renderer for the CLI. An application supplies its own
(a queue to another process, a websocket, a GUI) — the bake does not care, and must not.

**Chunk your work.** The bake's promise is that a caller hears from it at least every half
second of wall-clock. A product that does one ten-minute numpy call cannot keep that promise, so
it splits the call by tile, by segment batch, by row block — whatever is natural — and calls
`advance` between pieces. `cancelled()` is checked at the same points; a cancel therefore
returns within one chunk, not at the end.
"""

import sys
import time


class Cancelled(Exception):
    """Raised inside the bake when the caller asked it to stop; propagates out of `run`."""


class Progress:
    """The protocol. Every method has a harmless default so a caller can override only what
    it needs."""

    def begin(self, name, total=None, unit="items"):
        """A new phase of work: `name` is for humans, `total` (or None) is the denominator."""

    def advance(self, n=1):
        """`n` more units done."""

    def set_total(self, total):
        """The denominator became known (or changed) mid-phase."""

    def log(self, message):
        """A line worth keeping — counters, warnings, decisions."""

    def end(self, summary=None):
        """The current phase is complete; `summary` is a short human string or None."""

    def cancelled(self):
        """True when the caller wants the bake to stop. Checked between chunks."""
        return False

    def check(self):
        """Raise `Cancelled` if `cancelled()`; products call this between chunks."""
        if self.cancelled():
            raise Cancelled()


class ConsoleProgress(Progress):
    """One updating line per phase on a terminal, plus plain log lines. Rate-limited so a
    product advancing a million times does not spend its time printing."""

    def __init__(self, stream=None, min_interval=0.25):
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self._name = None
        self._total = None
        self._done = 0
        self._unit = "items"
        self._t0 = 0.0
        self._last = 0.0
        self._line_open = False

    def begin(self, name, total=None, unit="items"):
        self._name, self._total, self._done, self._unit = name, total, 0, unit
        self._t0 = self._last = time.monotonic()
        self._render(force=True)

    def set_total(self, total):
        self._total = total
        self._render(force=True)

    def advance(self, n=1):
        self._done += n
        self._render()

    def log(self, message):
        self._close_line()
        self.stream.write(f"  {message}\n")
        self.stream.flush()

    def end(self, summary=None):
        self._render(force=True)
        self._close_line()
        elapsed = time.monotonic() - self._t0
        tail = f" - {summary}" if summary else ""
        self.stream.write(f"[{self._name}] done in {elapsed:.1f}s{tail}\n")
        self.stream.flush()
        self._name = None

    # -- internals ---------------------------------------------------------------------------

    def _render(self, force=False):
        now = time.monotonic()
        if not force and (now - self._last) < self.min_interval:
            return
        self._last = now
        elapsed = now - self._t0
        if self._total:
            frac = min(1.0, self._done / self._total)
            bar = "#" * int(frac * 24)
            eta = (elapsed / frac - elapsed) if frac > 0 else float("nan")
            text = (
                f"[{self._name}] {bar:<24} {self._done}/{self._total} {self._unit}"
                f"  {elapsed:6.1f}s  eta {eta:6.1f}s"
            )
        else:
            text = f"[{self._name}] {self._done} {self._unit}  {elapsed:6.1f}s"
        self.stream.write("\r" + text[:118].ljust(118))
        self.stream.flush()
        self._line_open = True

    def _close_line(self):
        if self._line_open:
            self.stream.write("\n")
            self._line_open = False
