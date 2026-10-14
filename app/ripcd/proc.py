"""Helpers for running external tools.

The important detail here is cancellation: reading a child's stdout with a
plain ``for line in proc.stdout`` blocks forever when the tool stops printing
(exactly what happens when a rip is canceled), so a reader thread feeds a queue
and the consumer polls it, giving us a chance to react every few hundred
milliseconds.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from typing import Callable, Iterator

#: How often the reader loop checks for cancellation.
POLL_INTERVAL = 0.2


def terminate(proc: subprocess.Popen, *, grace: float = 5.0) -> None:
    """Stop a child process (and everything it spawned)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass


def iter_output(
    proc: subprocess.Popen,
    *,
    cancel: Callable[[], bool] | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> Iterator[str]:
    """Yield lines from ``proc.stdout`` until the process ends.

    Stops early (without raising) when ``cancel()`` returns True - the caller is
    responsible for terminating the process, which keeps the two tools' error
    handling identical.
    """
    out = proc.stdout
    if out is None:  # pragma: no cover - defensive
        return
    lines: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        try:
            for line in out:
                lines.put(line)
        except (ValueError, OSError):  # pragma: no cover - pipe closed
            pass
        finally:
            lines.put(None)

    thread = threading.Thread(target=reader, name="proc-reader", daemon=True)
    thread.start()

    while True:
        try:
            item = lines.get(timeout=poll_interval)
        except queue.Empty:
            if cancel and cancel():
                return
            if proc.poll() is not None and lines.empty():
                return
            continue
        if item is None:
            return
        yield item
