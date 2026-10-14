"""Audio encoding with ffmpeg.

``cdparanoia`` produces a WAV; ffmpeg does everything after that: transcode,
tag and (where the container supports it) embed the cover art.  Building the
command line is a pure function so it can be unit tested without ffmpeg.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .config import FORMATS
from .proc import iter_output, terminate
from .system import probe_tool

log = logging.getLogger("ripcd.encoder")

#: Codec + quality settings per output format.
_CODECS: dict[str, dict[str, Any]] = {
    "flac": {"codec": "flac", "extra": ["-compression_level", "{quality}"]},
    "alac": {"codec": "alac", "extra": []},
    "wav": {"codec": "pcm_s16le", "extra": []},
    "mp3": {
        "codec": "libmp3lame",
        "extra": {
            "v0": ["-q:a", "0", "-joint_stereo", "1"],
            "v2": ["-q:a", "2", "-joint_stereo", "1"],
            "320": ["-b:a", "320k"],
            "192": ["-b:a", "192k"],
        },
    },
    "ogg": {
        "codec": "libvorbis",
        "extra": {"q8": ["-q:a", "8"], "q6": ["-q:a", "6"], "q4": ["-q:a", "4"]},
    },
    "opus": {
        "codec": "libopus",
        "extra": {"256": ["-b:a", "256k"], "192": ["-b:a", "192k"], "128": ["-b:a", "128k"]},
    },
    "aac": {
        "codec": "aac",
        "extra": {"256": ["-b:a", "256k"], "192": ["-b:a", "192k"], "128": ["-b:a", "128k"]},
    },
}

#: How the cover art is attached, per container.
COVER_MODE = {
    "flac": "attached_pic",
    "mp3": "id3",
    "alac": "attached_pic",
    "aac": "attached_pic",
    "wav": "none",
    "ogg": "sidecar",
    "opus": "sidecar",
}

#: Canonical tag -> ffmpeg metadata key for Vorbis comments (FLAC/OGG/Opus).
_VORBIS_KEYS = {
    "title": "TITLE",
    "artist": "ARTIST",
    "album": "ALBUM",
    "album_artist": "ALBUMARTIST",
    "date": "DATE",
    "original_date": "ORIGINALDATE",
    "genre": "GENRE",
    "track": "TRACKNUMBER",
    "track_total": "TRACKTOTAL",
    "disc": "DISCNUMBER",
    "disc_total": "DISCTOTAL",
    "label": "LABEL",
    "catalog_number": "CATALOGNUMBER",
    "barcode": "BARCODE",
    "comment": "COMMENT",
    "composer": "COMPOSER",
    "musicbrainz_album_id": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_artist_id": "MUSICBRAINZ_ARTISTID",
    "musicbrainz_albumartist_id": "MUSICBRAINZ_ALBUMARTISTID",
    "musicbrainz_releasegroup_id": "MUSICBRAINZ_RELEASEGROUPID",
    "musicbrainz_trackid": "MUSICBRAINZ_TRACKID",
    "musicbrainz_releasetrackid": "MUSICBRAINZ_RELEASETRACKID",
    "musicbrainz_discid": "MUSICBRAINZ_DISCID",
}

#: Canonical tag -> ffmpeg metadata key for ID3v2/M4A (lowercase names).
_ID3_KEYS = {key: key for key in _VORBIS_KEYS}
_ID3_KEYS.update({"track": "track", "track_total": "track", "disc": "disc", "disc_total": "disc"})


def output_extension(fmt: str) -> str:
    return FORMATS.get(fmt, FORMATS["flac"])["ext"]


def default_quality(fmt: str) -> str:
    return FORMATS.get(fmt, FORMATS["flac"])["default_quality"]


def resolve_quality(fmt: str, quality: str) -> str:
    qualities = FORMATS.get(fmt, FORMATS["flac"])["qualities"]
    if quality in qualities:
        return quality
    return default_quality(fmt)


def encoder_available(fmt: str, tools: dict[str, Any] | None = None) -> bool:
    """Whether ffmpeg (and the codec) can produce this format here."""
    tools = tools or {}
    ffmpeg = tools.get("ffmpeg")
    if ffmpeg is not None and not getattr(ffmpeg, "available", False):
        return False
    if ffmpeg is None and not probe_tool("ffmpeg", check_version=False).available:
        return False
    return fmt in _CODECS


def codec_arguments(fmt: str, quality: str) -> list[str]:
    entry = _CODECS.get(fmt)
    if not entry:
        raise ValueError(f"unsupported output format: {fmt}")
    quality = resolve_quality(fmt, quality)
    extra = entry["extra"]
    if isinstance(extra, dict):
        selected = extra.get(quality) or extra.get(resolve_quality(fmt, quality)) or next(iter(extra.values()))
        return [str(x) for x in selected]
    return [str(x).format(quality=quality) for x in extra]


@dataclass
class EncodeRequest:
    """Everything needed to encode a single track."""

    source_wav: str
    output: str
    fmt: str
    quality: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    cover: str | None = None

    @property
    def extension(self) -> str:
        return output_extension(self.fmt)

    @property
    def embeds_cover(self) -> bool:
        return bool(self.cover and os.path.isfile(self.cover) and COVER_MODE.get(self.fmt) != "none")


def _metadata_pairs(fmt: str, tags: dict[str, str]) -> list[str]:
    """Translate canonical tags into ``-metadata`` arguments."""
    vorbis = fmt in {"flac", "ogg", "opus"}
    mapping = _VORBIS_KEYS if vorbis else _ID3_KEYS
    resolved: dict[str, str] = {}

    def put(target: str, value: Any) -> None:
        if value in (None, ""):
            return
        resolved[target] = str(value)

    for key, value in tags.items():
        if value in (None, ""):
            continue
        # track/track_total and disc/disc_total are merged below.
        if key in {"track_total", "disc_total"}:
            continue
        target = mapping.get(key)
        if not target:
            continue
        if key == "track" and not vorbis and tags.get("track_total"):
            put(target, f"{value}/{tags['track_total']}")
        elif key == "disc" and not vorbis and tags.get("disc_total"):
            put(target, f"{value}/{tags['disc_total']}")
        else:
            put(target, value)

    if vorbis:
        put(mapping["track_total"], tags.get("track_total"))
        put(mapping["disc_total"], tags.get("disc_total"))

    pairs: list[str] = []
    for key, value in resolved.items():
        pairs += ["-metadata", f"{key}={value}"]
    return pairs


def build_ffmpeg_command(ffmpeg: str, request: EncodeRequest) -> list[str]:
    """Build the ffmpeg command line for one track."""
    fmt = request.fmt
    mode = COVER_MODE.get(fmt, "none")
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-y",
        "-i", request.source_wav,
    ]
    if request.embeds_cover and mode in {"attached_pic", "id3"}:
        command += ["-i", request.cover or ""]

    command += ["-map", "0:a"]
    if request.embeds_cover and mode in {"attached_pic", "id3"}:
        command += ["-map", "1:v", "-c:v", "copy"]
        if mode == "attached_pic":
            command += ["-disposition:v", "attached_pic"]
        else:
            command += [
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
            ]

    command += ["-vn"] if not (request.embeds_cover and mode in {"attached_pic", "id3"}) else []
    command += ["-c:a", _CODECS[fmt]["codec"]]
    command += codec_arguments(fmt, request.quality)
    command += _metadata_pairs(fmt, request.tags)
    if fmt == "mp3":
        command += ["-id3v2_version", "3"]
    command += [request.output]
    return [part for part in command if part != ""]


_PROGRESS_RE = re.compile(r"^(out_time_us|out_time_ms|out_time|progress|speed|total_size)=(.*)$")


def parse_ffmpeg_progress(line: str) -> tuple[str, str] | None:
    match = _PROGRESS_RE.match(line.strip())
    if not match:
        return None
    return match.group(1), match.group(2).strip()


@dataclass
class EncodeResult:
    ok: bool
    output: str = ""
    seconds: float = 0.0
    error: str = ""
    log: list[str] = field(default_factory=list)


def run_ffmpeg(
    command: Iterable[str],
    *,
    duration_seconds: float = 0.0,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    log_sink: Callable[[str, str], None] | None = None,
) -> EncodeResult:
    """Run an ffmpeg command, reporting progress in percent."""
    started = time.monotonic()
    command = list(command)
    log.debug("running: %s", " ".join(command))
    lines: list[str] = []
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
        return EncodeResult(ok=False, error=f"cannot start ffmpeg: {exc}")

    error_tail: list[str] = []
    try:
        for raw in iter_output(proc, cancel=cancel):
            line = raw.rstrip("\n")
            parsed = parse_ffmpeg_progress(line)
            if parsed:
                key, value = parsed
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        micro = int(value)
                    except ValueError:
                        continue
                    # ffmpeg reports microseconds for out_time_us; older builds
                    # label milliseconds but emit microseconds as well.
                    seconds = micro / 1_000_000
                    if duration_seconds > 0 and on_progress:
                        on_progress(min(99.0, 100 * seconds / duration_seconds), value)
                continue
            if line:
                lines.append(line)
                if len(error_tail) < 12:
                    error_tail.append(line)
        if cancel and cancel():
            terminate(proc)
            return EncodeResult(ok=False, error="canceled", log=lines)
        returncode = proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001 - surface anything as a failure
        terminate(proc)
        return EncodeResult(ok=False, error=f"ffmpeg failed: {exc}", log=lines)

    elapsed = time.monotonic() - started
    if returncode != 0:
        message = " ".join(error_tail[-4:]) or f"ffmpeg exited with {returncode}"
        if log_sink:
            log_sink("error", message)
        return EncodeResult(ok=False, error=message, seconds=elapsed, log=lines)
    if on_progress:
        on_progress(100.0, "")
    return EncodeResult(ok=True, seconds=elapsed, log=lines)


def verify_output(path: str, *, min_bytes: int = 1024) -> tuple[bool, str]:
    """Sanity check an encoded file (exists, plausible size, decodable header)."""
    if not os.path.isfile(path):
        return False, "output file was not created"
    size = os.path.getsize(path)
    if size < min_bytes:
        return False, f"output file is suspiciously small ({size} bytes)"
    return True, ""
