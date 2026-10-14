"""Track extraction with cdparanoia.

Kept separate from the drive classes so it can be unit tested without any
hardware.  ``cdparanoia`` is the only tool that does genuinely reliable audio
extraction (jitter correction + scratch repair), so it is the only ripper we
drive.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .proc import iter_output, terminate

log = logging.getLogger("ripcd.extract")

#: cdparanoia writes its progress bar to stderr; it looks like
#: `` 1:23.45 [ 0:12.34] <...>`` - we pick up every ``m:ss.ff`` we see.
_MSF_RE = re.compile(r"(\d+):(\d{2})[.:](\d{2})")

#: Smilies cdparanoia prints when a disc has problems.
_ERROR_MARKERS = (
    "scsi/atapi transport error",
    "gave up trying to perform a correction",
    "aborted read due to known, uncorrectable error",
    "unreported loss of streaming",
)

PARANOIA_FLAGS = {
    "full": [],           # default: overlap + intra-read verification
    "reduced": ["-Y"],    # no intra-read verification
    "none": ["-Z"],       # raw read, fastest
}


@dataclass
class ExtractResult:
    ok: bool
    output: str = ""
    seconds: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    canceled: bool = False


def build_cdparanoia_command(
    binary: str,
    device: str,
    track: int,
    outfile: str,
    *,
    paranoia: str = "full",
    speed: int = 0,
) -> list[str]:
    """``cdparanoia -w -e -d /dev/sr0 3 track03.wav``."""
    command = [binary, "-w", "-e", "-d", device]
    command += PARANOIA_FLAGS.get(paranoia, [])
    if speed:
        command += ["-S", str(int(speed))]
    command += ["--", str(int(track)), outfile]
    return command


def parse_progress(text: str) -> list[float]:
    """Return every ``m:ss.ff`` timestamp found in ``text``, in seconds."""
    values: list[float] = []
    for match in _MSF_RE.finditer(text):
        values.append(int(match.group(1)) * 60 + int(match.group(2)) + int(match.group(3)) / 75.0)
    return values


def run_cdparanoia(
    command: Iterable[str],
    *,
    duration_seconds: float = 0.0,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    log_sink: Callable[[str, str], None] | None = None,
) -> ExtractResult:
    """Run cdparanoia and report progress as a percentage of the track."""
    command = list(command)
    started = time.monotonic()
    log.debug("running: %s", " ".join(command))
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return ExtractResult(ok=False, error=f"cannot start cdparanoia: {exc}")

    warnings: list[str] = []
    lines: list[str] = []
    position = 0.0
    try:
        for raw in iter_output(proc, cancel=cancel):
            stripped = raw.strip()
            if stripped:
                lines.append(stripped)
                if len(lines) > 400:
                    del lines[:200]
                lowered = stripped.lower()
                if any(marker in lowered for marker in _ERROR_MARKERS):
                    if stripped not in warnings:
                        warnings.append(stripped)
                        if log_sink:
                            log_sink("warning", stripped)
            values = parse_progress(stripped)
            if values:
                position = max(position, max(values))
            if duration_seconds > 0 and on_progress:
                percent = min(99.0, 100 * position / duration_seconds)
                on_progress(percent, stripped)
        if cancel and cancel():
            terminate(proc)
            return ExtractResult(ok=False, error="canceled", canceled=True, log=lines, warnings=warnings)
        returncode = proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001
        terminate(proc)
        return ExtractResult(ok=False, error=f"cdparanoia failed: {exc}", log=lines, warnings=warnings)

    elapsed = time.monotonic() - started
    if returncode != 0:
        message = next((line for line in reversed(lines) if line), f"cdparanoia exited with {returncode}")
        return ExtractResult(ok=False, error=message, seconds=elapsed, log=lines, warnings=warnings)
    if on_progress:
        on_progress(100.0, "")
    return ExtractResult(ok=True, seconds=elapsed, log=lines, warnings=warnings)


def verify_wav(path: str, *, expected_seconds: float = 0.0) -> tuple[bool, str]:
    """Check that the WAV we just produced looks sane."""
    if not os.path.isfile(path):
        return False, "cdparanoia did not produce a file"
    size = os.path.getsize(path)
    if size <= 44:
        return False, "the produced WAV is empty"
    try:
        import wave

        with wave.open(path, "rb") as handle:
            duration = handle.getnframes() / float(handle.getframerate() or 44100)
    except Exception:  # noqa: BLE001 - an unreadable header means a broken rip
        return False, "the produced WAV has no readable header"
    if expected_seconds and duration < expected_seconds * 0.5:
        return False, f"the produced WAV is much shorter than the track ({duration:.0f}s)"
    return True, ""
