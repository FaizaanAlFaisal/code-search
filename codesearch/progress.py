from __future__ import annotations

import os
import sys
import time


def _enabled() -> bool:
    return os.getenv("CODE_SEARCH_PROGRESS", "true").lower() not in {"0", "false", "no"}


class Progress:
    """Lightweight progress reporter for long model batches.

    Writes to stderr only (never stdout), so it can't corrupt --json output.
    On a TTY it rewrites a single line; piped/non-TTY it emits ~20 discrete
    lines so logs stay readable. Disable with CODE_SEARCH_PROGRESS=false.
    """

    def __init__(self, label: str, total: int) -> None:
        self.label = label
        self.total = total
        self.done = 0
        self.errors = 0
        self.enabled = _enabled() and total > 0
        self._tty = sys.stderr.isatty()
        self._interval = 0.3                 # tty: at most ~3 redraws/sec
        self._step = max(1, total // 20)     # non-tty: ~20 lines total
        self._last = time.monotonic()
        self._rendered = -1

    def update(self, ok: bool = True) -> None:
        self.done += 1
        if not ok:
            self.errors += 1
        if not self.enabled:
            return
        if self._tty:
            now = time.monotonic()
            if now - self._last >= self._interval:
                self._last = now
                self._render()
        elif self.done % self._step == 0:
            self._render()

    def _render(self) -> None:
        if self.done == self._rendered:
            return
        self._rendered = self.done
        pct = (self.done / self.total * 100) if self.total else 100.0
        err = f" {self.errors} err" if self.errors else ""
        msg = f"{self.label}: {self.done}/{self.total} ({pct:.0f}%){err}"
        if self._tty:
            sys.stderr.write("\r\033[K" + msg)
        else:
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    def close(self) -> None:
        if not self.enabled:
            return
        self._render()
        if self._tty:
            sys.stderr.write("\n")
        sys.stderr.flush()
