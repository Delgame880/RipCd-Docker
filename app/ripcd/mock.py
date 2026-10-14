"""Demo / mock drive.

Two reasons this exists:

* you can try the whole app (UI, ripping pipeline, tags) on a machine without
  an optical drive - ``RIPCD_MOCK=1``;
* it lets the automated tests exercise the pipeline without hardware.

Nothing in here pretends to be real hardware: the UI shows a DEMO badge and the
logs say so too.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import FORMATS
from .metadata import AlbumMetadata, TrackMetadata
from .optical import (
    FRAMES_PER_SECOND,
    LEAD_IN_FRAMES,
    OpticalDrive,
    Toc,
    TrackInfo,
    disc_signature,
    musicbrainz_disc_id,
    musicbrainz_toc_string,
)

log = logging.getLogger("ripcd.mock")

MOCK_DEVICE = "/dev/sr0"


@dataclass
class MockDisc:
    """A fake CD: track titles + lengths in seconds."""

    key: str
    title: str
    artist: str
    year: str
    genre: str
    tracks: list[tuple[str, int]]
    is_audio: bool = True
    data_track: bool = False
    label: str = "Demo Records"
    catalog_number: str = "DEMO-001"

    def toc(self) -> Toc:
        entries: list[TrackInfo] = []
        lba = LEAD_IN_FRAMES  # absolute MSF addressing: track 1 starts at 150
        for index, (_title, seconds) in enumerate(self.tracks, start=1):
            frames = int(seconds * FRAMES_PER_SECOND)
            entries.append(
                TrackInfo(number=index, start_lba=lba, duration_frames=frames, audio=self.is_audio)
            )
            lba += frames
        if self.data_track:
            lba += 60 * FRAMES_PER_SECOND  # a minute long data track
            entries.append(TrackInfo(number=len(entries) + 1, start_lba=lba, duration_frames=60 * FRAMES_PER_SECOND, audio=False))
            lba += 60 * FRAMES_PER_SECOND
        if not entries:
            entries = [TrackInfo(number=1, start_lba=LEAD_IN_FRAMES, duration_frames=FRAMES_PER_SECOND * 3, audio=self.is_audio)]
            lba = entries[0].start_lba + entries[0].duration_frames
        return Toc(
            first_track=entries[0].number,
            last_track=entries[-1].number,
            leadout_lba=lba,
            tracks=entries,
            source="mock",
        )


MOCK_DISCS: dict[str, MockDisc] = {
    "chromatic-night": MockDisc(
        key="chromatic-night",
        title="Chromatic Night",
        artist="The Demo Band",
        year="1998",
        genre="Alternative Rock",
        tracks=[
            ("Opening Signal", 232),
            ("Chromatic Night", 284),
            ("Paper Lanterns", 198),
            ("Slow Static", 311),
            ("Analog Heart", 245),
            ("Neon Suburb", 267),
            ("Half Light", 176),
            ("Cathode Ray", 293),
            ("Winter Transmitter", 258),
            ("Every Street Is Blue", 302),
            ("Rerun", 214),
            ("Sign Off", 341),
        ],
    ),
    "short-ep": MockDisc(
        key="short-ep",
        title="Three Song Demo",
        artist="Tape Machine",
        year="2004",
        genre="Lo-Fi",
        tracks=[("Bootstrap", 42), ("Warm Up", 37), ("Static Bloom", 51)],
    ),
    "mixed-cd": MockDisc(
        key="mixed-cd",
        title="Enhanced Album",
        artist="Mixed Media",
        year="2001",
        genre="Electronic",
        tracks=[("Data Entry", 187), ("Packet Loss", 221), ("Checksum", 240), ("Handshake", 199)],
        data_track=True,
    ),
    "data-disc": MockDisc(
        key="data-disc",
        title="Not An Audio CD",
        artist="",
        year="",
        genre="",
        tracks=[("data", 300)],
        is_audio=False,
    ),
}


@dataclass
class MockState:
    """Global mock drive state, shared between the scanner and the API."""

    inserted_key: str = "chromatic-night"
    tray_open: bool = False
    track_seconds: float = 6.0        # how long the generated audio is
    generation_seconds: float = 1.0   # simulated time to "rip" a track
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def disc(self) -> MockDisc | None:
        if self.tray_open:
            return None
        return MOCK_DISCS.get(self.inserted_key)

    def insert(self, key: str) -> MockDisc | None:
        with self._lock:
            if key in MOCK_DISCS:
                self.inserted_key = key
                self.tray_open = False
            return MOCK_DISCS.get(self.inserted_key)

    def eject(self) -> None:
        with self._lock:
            self.tray_open = True

    def close(self) -> None:
        with self._lock:
            self.tray_open = False

    def clear(self) -> None:
        with self._lock:
            self.tray_open = True


MOCK_STATE = MockState()


def mock_toc_signature(toc: Toc) -> str:
    return "mock-" + disc_signature(toc)


# --------------------------------------------------------------------------- #
# The fake drive
# --------------------------------------------------------------------------- #


class MockOpticalDrive(OpticalDrive):
    """Duck-typed replacement for :class:`OpticalDrive`."""

    #: the demo disc can be swapped at any time, so never serve a cached TOC
    toc_ttl = 0.0

    def __init__(self, state: MockState | None = None, device: str = MOCK_DEVICE):
        super().__init__(device)
        self.state = state or MOCK_STATE
        self.is_mock = True

    # -- identity ---------------------------------------------------------- #

    def info(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "name": self.name,
            "vendor": "RipCD",
            "model": "Virtual Drive (demo)",
            "label": "RipCD Virtual CD Drive",
            "generic_device": "/dev/sg0",
            "mock": True,
        }

    def capabilities(self) -> int:
        return 0x900

    def is_cdrom(self) -> bool:
        return True

    def supports_audio(self) -> bool:
        return True

    # -- state ------------------------------------------------------------- #

    def status(self) -> str:
        if self.state.tray_open:
            return "tray_open"
        return "disc_ok" if self.state.disc else "no_disc"

    def has_disc(self) -> bool:
        return self.state.disc is not None

    def disc_status(self) -> str:
        disc = self.state.disc
        if disc is None:
            return "unknown"
        if not disc.is_audio:
            return "data"
        return "mixed" if disc.data_track else "audio"

    def mcn(self) -> str | None:
        return "1234567890123" if self.state.disc else None

    def _read_toc_ioctl(self) -> Toc | None:
        disc = self.state.disc
        if disc is None:
            return None
        toc = disc.toc()
        toc.device = self.device
        return toc

    def _read_toc_cdparanoia(self) -> Toc | None:
        return self._read_toc_ioctl()

    def eject(self) -> None:
        self.state.eject()
        self._cached_toc = None

    def close_tray(self) -> None:
        self.state.close()

    def lock(self, locked: bool = True) -> None:
        return None

    # -- ripping ----------------------------------------------------------- #

    def extract_track(
        self,
        track: TrackInfo,
        outfile: str,
        *,
        paranoia: str = "full",
        speed: int = 0,
        on_progress: Callable[[float, str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        log_sink: Callable[[str, str], None] | None = None,
    ) -> "ExtractResult":
        """Pretend to read a track: writes a short generated tone."""
        from .extract import ExtractResult

        seconds = float(self.state.track_seconds)
        total = max(1.0, self.state.generation_seconds)
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            percent = min(99.0, 100 * elapsed / total)
            if on_progress:
                on_progress(percent, f"demo read {percent:5.1f}%")
            if cancel and cancel():
                return ExtractResult(ok=False, error="canceled", canceled=True)
            if elapsed >= total:
                break
            time.sleep(0.1)
        write_tone_wav(outfile, seconds=seconds, frequency=180 + 40 * track.number, channels=2)
        if on_progress:
            on_progress(100.0, "demo read complete")
        return ExtractResult(ok=True, seconds=seconds, output=outfile)


def write_tone_wav(path: str, *, seconds: float = 6.0, frequency: float = 440.0, rate: int = 44100,
                   channels: int = 2) -> str:
    """Write a small stereo WAV with a fading sine tone (no dependencies)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frames = int(rate * seconds)
    amplitude = 0.35
    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        chunk: list[bytes] = []
        for index in range(frames):
            fade = min(1.0, index / (rate * 0.05), (frames - index) / (rate * 0.05))
            value = int(amplitude * fade * 32767 * math.sin(2 * math.pi * frequency * index / rate))
            sample = struct.pack("<h", value)
            chunk.append(sample * channels)
            if len(chunk) >= 8192:
                handle.writeframes(b"".join(chunk))
                chunk = []
        if chunk:
            handle.writeframes(b"".join(chunk))
    return path


# --------------------------------------------------------------------------- #
# Mock metadata provider
# --------------------------------------------------------------------------- #


class MockMetadataClient:
    """Fabricates metadata from the mock disc definition (demo mode only)."""

    def __init__(self, state: MockState | None = None):
        self.state = state or MOCK_STATE

    def resolve(self, toc: Toc, *, mcn: str = "", use_cache: bool = True) -> AlbumMetadata:
        disc = self.state.disc
        if disc is None:
            return AlbumMetadata(source="none", title="Unknown Album", album_artist="Unknown Artist")
        audio = toc.audio_tracks or toc.tracks
        tracks: list[TrackMetadata] = []
        for index, track in enumerate(audio):
            title = disc.tracks[index][0] if index < len(disc.tracks) else f"Track {track.number:02d}"
            tracks.append(
                TrackMetadata(
                    number=track.number,
                    title=title,
                    artist=disc.artist,
                    length_ms=int(track.seconds * 1000) if track.seconds else None,
                    recording_id=f"mock-recording-{index + 1}",
                )
            )
        return AlbumMetadata(
            source="mock",
            confidence=1.0,
            disc_id=musicbrainz_disc_id(toc) or "MOCK",
            toc_string=musicbrainz_toc_string(toc) or "",
            release_id=f"mock-release-{disc.key}",
            title=disc.title,
            album_artist=disc.artist,
            year=disc.year,
            date=disc.year,
            genre=disc.genre,
            label=disc.label,
            catalog_number=disc.catalog_number,
            barcode="0123456789012",
            country="ZZ",
            total_tracks=len(tracks),
            tracks=tracks,
            warnings=["demo mode: metadata is generated locally, MusicBrainz is not contacted"],
            fetched_at=time.time(),
        )


class MockCoverArtClient:
    """Cover-art stand-in for demo mode: a plain coloured square."""

    def __init__(self, cache_dir: str | os.PathLike[str] | None = None, state: MockState | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.state = state or MOCK_STATE
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.cache_dir = None

    def fetch(self, release_id: str, *, size: str = "500", release_group_id: str = "") -> str:
        if not self.cache_dir or not release_id:
            return ""
        path = self.cache_dir / f"{release_id}-{size}.jpg"
        if path.is_file() and path.stat().st_size > 512:
            return str(path)
        return generate_mock_cover(str(path), seed=release_id)


def generate_mock_cover(path: str, seed: str = "demo") -> str:
    """Create a simple cover image with ffmpeg when available."""
    import subprocess
    from shutil import which

    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return ""
    hue = abs(hash(seed)) % 360
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=#{_hue_to_hex(hue)}:s=500x500",
        "-frames:v", "1", path,
    ]
    try:
        subprocess.run(command, check=True, timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return path


def _hue_to_hex(hue: int) -> str:
    import colorsys

    red, green, blue = colorsys.hsv_to_rgb(hue / 360.0, 0.55, 0.55)
    return f"{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def supported_formats() -> list[str]:
    return sorted(FORMATS.keys())
