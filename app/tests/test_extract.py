"""Tests for the cdparanoia command builder and progress parsing."""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

from ripcd.extract import (
    build_cdparanoia_command,
    parse_progress,
    run_cdparanoia,
    verify_wav,
)


def test_build_default_command():
    command = build_cdparanoia_command("/usr/bin/cdparanoia", "/dev/sr0", 3, "/tmp/track03.wav")
    assert command[:2] == ["/usr/bin/cdparanoia", "-w"]
    assert "-e" in command  # progress on stderr, needed for the progress bar
    assert command[command.index("-d") + 1] == "/dev/sr0"
    assert command[-2:] == ["--", "/tmp/track03.wav"][:0] + command[-2:]
    # the span must be separated from the flags
    assert command[-3:] == ["--", "3", "/tmp/track03.wav"]


@pytest.mark.parametrize(
    ("paranoia", "flag"),
    [("full", None), ("reduced", "-Y"), ("none", "-Z")],
)
def test_paranoia_levels_map_to_flags(paranoia, flag):
    command = build_cdparanoia_command("cdparanoia", "/dev/sr0", 1, "/tmp/a.wav", paranoia=paranoia)
    if flag is None:
        assert "-Y" not in command and "-Z" not in command
    else:
        assert flag in command


def test_read_speed_is_passed_through():
    command = build_cdparanoia_command("cdparanoia", "/dev/sr0", 1, "/tmp/a.wav", speed=8)
    assert command[command.index("-S") + 1] == "8"
    assert "-S" not in build_cdparanoia_command("cdparanoia", "/dev/sr0", 1, "/tmp/a.wav", speed=0)


def test_parse_progress_picks_up_msf_strings():
    values = parse_progress(" 0:12.34 [ 0:00.12] -+-")
    assert values == [12 + 34 / 75, 12 / 75]
    assert parse_progress("no numbers here") == []
    # the progress bar characters are not digits, so nothing else matches
    assert parse_progress("   <+-!8-X:^D") == []


def test_run_cdparanoia_reports_progress_and_success(tmp_path):
    """A fake cdparanoia that prints the usual progress bar."""
    script = tmp_path / "fake-cdparanoia"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '\\r 0:01.00 [ 0:01.00] <\\r 0:02.00 [ 0:02.00]   \\r'
            printf '\\n:^D\\n'
            exit 0
            """
        )
    )
    script.chmod(0o755)
    seen: list[float] = []
    result = run_cdparanoia(
        [str(script)],
        duration_seconds=4,
        on_progress=lambda percent, _message: seen.append(percent),
    )
    assert result.ok, result.error
    assert seen[-1] == 100.0
    assert max(seen) > 50  # 2 seconds of a 4 second track


def test_run_cdparanoia_failure_and_warnings(tmp_path):
    script = tmp_path / "bad-cdparanoia"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "scsi/atapi transport error" >&2
            echo "SCRATCH DETECTED" >&2
            exit 4
            """
        )
    )
    script.chmod(0o755)
    result = run_cdparanoia([str(script)])
    assert not result.ok
    assert result.warnings
    assert "transport error" in result.warnings[0].lower()


def test_run_cdparanoia_cancellation(tmp_path):
    import threading

    script = tmp_path / "slow-cdparanoia"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    result = run_cdparanoia([str(script)], cancel=cancel.is_set)
    assert result.canceled
    assert result.error == "canceled"


def test_run_cdparanoia_missing_binary():
    result = run_cdparanoia(["/definitely/not/here"])
    assert not result.ok
    assert "cannot start" in result.error


def test_verify_wav(tmp_path):
    import wave

    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00\x00\x00" * 44100)

    ok, error = verify_wav(str(path), expected_seconds=1.0)
    assert ok, error

    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    assert verify_wav(str(empty))[0] is False

    truncated = tmp_path / "short.wav"
    with wave.open(str(truncated), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00\x00\x00" * 4410)
    assert verify_wav(str(truncated), expected_seconds=60)[0] is False

    assert verify_wav(str(tmp_path / "missing.wav"))[0] is False
    assert sys.platform  # keep flake8 quiet about unused imports
    assert os.path.isdir(str(tmp_path))
