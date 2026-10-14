"""Low level access to the CD drive.

Everything here talks to the drive through the Linux CD-ROM ioctls, so RipCD
knows whether a disc is inserted, can read the table of contents (TOC), compute
the MusicBrainz disc id and eject the tray without any helper program.
``cdparanoia`` is still used - it is the only reliable audio *reader* - but it
is called from :mod:`ripcd.extract` and from the TOC fallback below.

Address convention used throughout the project (identical to libdiscid):

* ``start_lba`` / ``leadout_lba`` are *absolute MSF* frame addresses, so the
  address a drive prints as ``00:02.00`` is ``150``.  Track 1 of an audio CD
  starts at 150, and the 2 second lead-in is part of every offset.
* ``sector`` is the plain MMC/LBA value, where ``00:02.00`` is ``0``.
* ``duration_frames`` / ``total_frames`` are lengths, so they have no lead-in.

The disc id is computed exactly like ``libdiscid`` does: the same SHA-1 over
``%02X%02X`` (first, last track) plus 100 ``%08X`` offsets - the lead-out first,
then every track, unused slots zero padded.  It is checked against the reference
values of libdiscid's own test suite in ``tests/test_optical.py``.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import hashlib
import logging
import os
import re
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .extract import ExtractResult

log = logging.getLogger("ripcd.optical")

# --------------------------------------------------------------------------- #
# CD-ROM ioctls (linux/cdrom.h)
# --------------------------------------------------------------------------- #
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
CDROMEJECT = 0x5309
CDROM_GET_MCN = 0x5311
CDROMCLOSETRAY = 0x5319
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDROM_LOCKDOOR = 0x5329
CDROM_GET_CAPABILITY = 0x5331

#: ``struct cdrom_tocentry.cdte_track`` value that addresses the lead-out area
CDROM_LEADOUT = 0xAA
#: ``struct cdrom_tocentry.cdte_format``
CDROM_LBA = 0x01
CDROM_MSF = 0x02
#: ``cdte_ctrl`` bit set on data (non audio) tracks
CDROM_DATA_TRACK = 0x04

#: ``CDROM_DRIVE_STATUS`` results
CDS_NO_INFO = 1
CDS_NO_DISC = 2
CDS_TRAY_OPEN = 3
CDS_DRIVE_NOT_READY = 4
CDS_DISC_OK = 5

#: ``CDROM_DISC_STATUS`` results
CDS_DISC_NO_INFO = 100
CDS_DATA_1 = 101
CDS_DATA_2 = 102
CDS_AUDIO = 103
CDS_XA_2_1 = 104
CDS_XA_2_2 = 105
CDS_MIXED = 106

#: ``CDROM_GET_CAPABILITY`` bits
CDC_PLAY_AUDIO = 0x0008
CDC_CD_R = 0x2000
CDC_CD_RW = 0x4000
CDC_DVD = 0x8000
CDC_DVD_R = 0x10000
CDC_DVD_RAM = 0x20000

DISC_STATUS_NAMES = {
    CDS_DISC_NO_INFO: "unknown",
    CDS_DATA_1: "data",
    CDS_DATA_2: "data",
    CDS_AUDIO: "audio",
    CDS_XA_2_1: "mixed",
    CDS_XA_2_2: "mixed",
    CDS_MIXED: "mixed",
}

LEAD_IN_FRAMES = 150  # 2 seconds of lead-in
FRAMES_PER_SECOND = 75

#: Where a CD/DVD drive may show up.
DEVICE_PATTERNS = ("/dev/sr*", "/dev/scd*", "/dev/cdrom*", "/dev/cdwriter*", "/dev/dvd*", "/dev/cdrw*")

#: How long a TOC may be reused.  Long enough to make the scanner cheap, short
#: enough that swapping the disc is noticed within one poll.
TOC_TTL_SECONDS = 5.0


class OpticalError(RuntimeError):
    """Any problem while talking to the drive."""

    def __init__(self, message: str, err: OSError | None = None, device: str | None = None):
        super().__init__(message)
        self.errno = err.errno if err is not None else None
        self.device = device


class NoDiscError(OpticalError):
    """The tray is empty or open."""


class DataDiscError(OpticalError):
    """The disc in the drive carries no audio tracks at all."""


# --------------------------------------------------------------------------- #
# MSF helpers
# --------------------------------------------------------------------------- #
def frames_to_msf(frames: int) -> str:
    """Absolute frame address -> ``"mm:ss.ff"``."""
    frames = max(0, int(frames))
    minute, rest = divmod(frames, FRAMES_PER_SECOND * 60)
    second, frame = divmod(rest, FRAMES_PER_SECOND)
    return f"{minute:02d}:{second:02d}.{frame:02d}"


def lba_to_msf(lba: int) -> str:
    """MMC/LBA address -> ``"mm:ss.ff"``, i.e. the lead-in is added first."""
    return frames_to_msf(int(lba) + LEAD_IN_FRAMES)


def lba_to_frames(lba: int) -> int:
    return int(lba) + LEAD_IN_FRAMES


def frames_to_lba(frames: int) -> int:
    return int(frames) - LEAD_IN_FRAMES


def msf_to_frames(minute: int, second: int, frame: int) -> int:
    """``(minute, second, frame)`` -> absolute frame address."""
    return (int(minute) * 60 + int(second)) * FRAMES_PER_SECOND + int(frame)


def parse_msf(text: str) -> int:
    """``"mm:ss.ff"`` / ``"m:ss"`` -> absolute frame address."""
    parts = text.strip().replace(":", " ").replace(".", " ").split()
    if not parts:
        raise ValueError(f"not a timestamp: {text!r}")
    numbers = [int(part or 0) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    return msf_to_frames(*numbers[:3])


def msf_to_seconds(text: str) -> float:
    return parse_msf(text) / FRAMES_PER_SECOND


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class TrackInfo:
    """One entry of the disc's table of contents."""

    number: int
    start_lba: int = 0  # absolute MSF frames; track 1 is normally 150
    duration_frames: int = 0
    audio: bool = True
    #: length of the pre-gap in front of the track, when the TOC reports it
    pregap_frames: int = 0

    # -- addressing --------------------------------------------------------- #
    @property
    def sector(self) -> int:
        """MMC/LBA address (0 based)."""
        return self.start_lba - LEAD_IN_FRAMES

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.duration_frames

    @property
    def start(self) -> str:
        return frames_to_msf(self.start_lba)

    @property
    def duration(self) -> str:
        return frames_to_msf(self.duration_frames)

    @property
    def seconds(self) -> float:
        return self.duration_frames / FRAMES_PER_SECOND

    @property
    def duration_seconds(self) -> float:
        return self.seconds

    @property
    def start_msf(self) -> str:
        return self.start

    @property
    def end_msf(self) -> str:
        return frames_to_msf(self.end_lba)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "audio": self.audio,
            "start_lba": self.start_lba,
            "sector": self.sector,
            "duration_frames": self.duration_frames,
            "start": self.start,
            "duration": self.duration,
            "duration_seconds": round(self.seconds, 2),
            "seconds": round(self.seconds, 2),
            "pregap_frames": self.pregap_frames,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackInfo":
        return cls(
            number=int(data.get("number", 0)),
            start_lba=int(data.get("start_lba", 0)),
            duration_frames=int(data.get("duration_frames", 0)),
            audio=bool(data.get("audio", True)),
            pregap_frames=int(data.get("pregap_frames", 0)),
        )


@dataclass
class Toc:
    """Table of contents of the disc in the drive."""

    device: str = ""
    first_track: int = 1
    last_track: int = 0
    leadout_lba: int = 0
    tracks: list[TrackInfo] = field(default_factory=list)
    source: str = "ioctl"  # "ioctl" or "cdparanoia"

    # -- derived ------------------------------------------------------------ #
    @property
    def audio_tracks(self) -> list[TrackInfo]:
        return [track for track in self.tracks if track.audio]

    @property
    def data_tracks(self) -> list[TrackInfo]:
        return [track for track in self.tracks if not track.audio]

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_tracks)

    @property
    def is_mixed(self) -> bool:
        return bool(self.audio_tracks) and bool(self.data_tracks)

    @property
    def kind(self) -> str:
        """``"audio"``, ``"mixed"`` or ``"data"``."""
        if not self.audio_tracks:
            return "data"
        return "mixed" if self.data_tracks else "audio"

    @property
    def total_frames(self) -> int:
        """Length of the disc: from the first track to the lead-out."""
        if not self.tracks:
            return max(0, self.leadout_lba - LEAD_IN_FRAMES)
        return max(0, self.leadout_lba - self.tracks[0].start_lba)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / FRAMES_PER_SECOND

    @property
    def total_time(self) -> str:
        return frames_to_msf(self.total_frames)

    @property
    def audio_seconds(self) -> float:
        return sum(track.duration_frames for track in self.audio_tracks) / FRAMES_PER_SECOND

    def track(self, number: int) -> TrackInfo | None:
        for item in self.tracks:
            if item.number == number:
                return item
        return None

    def audio_only(self) -> "Toc":
        """A copy of this TOC with the data tracks removed (CD-Extra discs)."""
        tracks = [TrackInfo(**vars(track)) for track in self.audio_tracks]
        if not tracks:
            return self
        return Toc(
            device=self.device,
            first_track=tracks[0].number,
            last_track=tracks[-1].number,
            leadout_lba=tracks[-1].end_lba,
            tracks=tracks,
            source=self.source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "source": self.source,
            "first_track": self.first_track,
            "last_track": self.last_track,
            "leadout_lba": self.leadout_lba,
            "kind": self.kind,
            "has_audio": self.has_audio,
            "total_frames": self.total_frames,
            "total_seconds": round(self.total_seconds, 2),
            "total_time": self.total_time,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Toc":
        tracks = [TrackInfo.from_dict(item) for item in data.get("tracks", [])]
        return cls(
            device=str(data.get("device", "")),
            first_track=int(data.get("first_track", 1)),
            last_track=int(data.get("last_track", len(tracks))),
            leadout_lba=int(data.get("leadout_lba", 0)),
            tracks=tracks,
            source=str(data.get("source", "ioctl")),
        )


# --------------------------------------------------------------------------- #
# MusicBrainz disc id / TOC string
# --------------------------------------------------------------------------- #
def disc_id_payload(toc: Toc, *, audio_only: bool = False) -> str:
    """The exact string libdiscid hashes to build the disc id.

    ``%02X%02X`` (first track, last track) followed by 100 ``%08X`` offsets.
    Slot 0 is the lead-out, then the tracks in order, and every unused slot is
    zero.  MusicBrainz offsets include the 150 frame lead-in.
    """
    tracks = toc.audio_tracks if audio_only else toc.tracks
    if not tracks:
        return ""
    first = tracks[0].number
    last = tracks[-1].number
    leadout = tracks[-1].end_lba if audio_only else (toc.leadout_lba or tracks[-1].end_lba)
    offsets = [leadout, *[track.start_lba for track in tracks]]
    offsets.extend([0] * max(0, 100 - len(offsets)))
    parts = [f"{first:02X}", f"{last:02X}"]
    parts.extend(f"{int(value) & 0xFFFFFFFF:08X}" for value in offsets[:100])
    return "".join(parts)


def musicbrainz_disc_id(toc: Toc | None, *, audio_only: bool = False) -> str | None:
    """The MusicBrainz disc id, byte for byte the value libdiscid returns."""
    if toc is None:
        return None
    payload = disc_id_payload(toc, audio_only=audio_only)
    if not payload:
        return None
    import base64

    digest = hashlib.sha1(payload.encode("ascii")).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return encoded.replace("+", ".").replace("/", "_").replace("=", "-")


def musicbrainz_toc_string(
    toc: Toc | None, *, audio_only: bool = False, separator: str = "+"
) -> str | None:
    """The ``toc=`` value MusicBrainz expects.

    ``first+last+leadout+offset1+offset2+...`` with the same absolute MSF
    offsets as the disc id, e.g. ``1+22+303602+150+9700+...``.
    """
    if toc is None:
        return None
    tracks = toc.audio_tracks if audio_only else toc.tracks
    if not tracks:
        return None
    first = tracks[0].number
    last = tracks[-1].number
    leadout = tracks[-1].end_lba if audio_only else (toc.leadout_lba or tracks[-1].end_lba)
    return separator.join([str(first), str(last), str(leadout), *(f"{t.start_lba}" for t in tracks)])


def freedb_disc_id(toc: Toc | None, *, audio_only: bool = False) -> str | None:
    """The old freedb/CDDB disc id (nice for round-tripping, not used online)."""
    if toc is None:
        return None
    tracks = toc.audio_tracks if audio_only else toc.tracks
    if not tracks:
        return None
    leadout = tracks[-1].end_lba if audio_only else (toc.leadout_lba or tracks[-1].end_lba)
    offsets = [leadout, *[track.start_lba for track in tracks]]
    checksum = sum(sum(int(digit) for digit in str(offset // FRAMES_PER_SECOND)) for offset in offsets[1:])
    seconds = offsets[0] // FRAMES_PER_SECOND - offsets[1] // FRAMES_PER_SECOND
    last = tracks[-1].number
    return f"{((checksum % 0xFF) << 24 | seconds << 8 | last) & 0xFFFFFFFF:08x}"


def has_data_track(toc: Toc | None) -> bool:
    return bool(toc is not None and toc.data_tracks)


def toc_candidates(toc: Toc | None) -> list[tuple[str, Toc]]:
    """TOCs worth looking up, best first.

    A CD-Extra (enhanced CD) carries a data track.  MusicBrainz indexes the
    *audio* part of such discs, while libdiscid hashes the whole TOC, so both
    variants are offered and the caller tries them in order.
    """
    if toc is None or not toc.tracks:
        return []
    candidates: list[tuple[str, Toc]] = [("full", toc)]
    if has_data_track(toc):
        audio_only = toc.audio_only()
        if audio_only is not toc and audio_only.tracks:
            candidates.append(("audio-only", audio_only))
    return candidates


def disc_signature(toc: Toc | None) -> str:
    """Short, stable identifier of "which disc is in the drive".

    Used to remember metadata, to know that a job belongs to a disc and to spot
    disc changes in the scanner.  Empty when there is no disc.
    """
    if toc is None or not toc.tracks:
        return ""
    payload = disc_id_payload(toc) or ""
    parts = [
        payload or "|".join(str(track.start_lba) for track in toc.tracks),
        str(len(toc.tracks)),
        str(toc.leadout_lba),
        "".join("a" if track.audio else "d" for track in toc.tracks),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("ascii")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# cdparanoia output
# --------------------------------------------------------------------------- #
#: ``  1.    22190 [04:55.65]       55 [00:00.55]    no   no  2``
_TRACK_BRACKET_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d+)\s+\[(\d+:\d{2}\.\d{2})\]\s+(\d+)\s+\[(\d+:\d{2}\.\d{2})\]\s+(\w+)\s+(\w+)\s+(\d+)\s*$"
)
#: older builds print the lengths as timestamps and no sector numbers
_TRACK_PLAIN_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d+:\d{2}\.\d{2})\s+(\d+:\d{2}\.\d{2})\s+(\d+)\b(.*)$"
)
_TOTAL_RE = re.compile(r"^\s*TOTAL\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_TRACK_HEADER_RE = re.compile(r"^\s*track\s+length\s+begin", re.IGNORECASE)


def parse_cdparanoia_toc(text: str, device: str = "") -> Toc | None:
    """Parse the TOC listing of ``cdparanoia -Q`` into a :class:`Toc`.

    cdparanoia numbers sectors from the start of the user area, so 150 frames
    are added to every begin address: a first track printed as ``0 [00:00.00]``
    becomes ``start_lba = 150``, exactly like the drive reports it.

    Returns ``None`` when the output carries no usable table of contents.
    """
    tracks: list[TrackInfo] = []

    for line in text.splitlines():
        if _TRACK_HEADER_RE.match(line):
            continue
        match = _TRACK_BRACKET_RE.match(line)
        if match:
            number = int(match.group(1))
            length = int(match.group(2)) or parse_msf(match.group(3))
            begin = int(match.group(4))
            # the listing carries no audio flag: cdparanoia only lists readable
            # tracks, and a data track is announced with the word "data"
            audio = "data" not in line.lower()
            tracks.append(
                TrackInfo(
                    number=number,
                    start_lba=begin + LEAD_IN_FRAMES,
                    duration_frames=length,
                    audio=audio,
                )
            )
            continue
        match = _TRACK_PLAIN_RE.match(line)
        if match:
            number = int(match.group(1))
            length = parse_msf(match.group(2))
            begin_msf = parse_msf(match.group(3))
            begin = int(match.group(4))
            audio = "data" not in line.lower()
            tracks.append(
                TrackInfo(
                    number=number,
                    start_lba=(begin if begin else msf_to_frames(*_split_msf(match.group(3)))) + LEAD_IN_FRAMES,
                    duration_frames=length or begin_msf,
                    audio=audio,
                )
            )

    if not tracks:
        return None

    tracks.sort(key=lambda item: item.number)
    # Fill in the length of the last track: cdparanoia's listing stops there and
    # only the TOTAL line (audio only!) says how long the disc really is, so the
    # last track simply keeps the length the listing reported for it.
    leadout = tracks[-1].end_lba
    return Toc(
        device=device,
        first_track=tracks[0].number,
        last_track=tracks[-1].number,
        leadout_lba=leadout,
        tracks=tracks,
        source="cdparanoia",
    )


def _split_msf(text: str) -> tuple[int, int, int]:
    parts = text.strip().replace(":", ".").split(".")
    numbers = [int(part or 0) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def cdparanoia_toc(device: str, *, binary: str = "cdparanoia", timeout: float = 45.0) -> Toc | None:
    """Ask ``cdparanoia -Q`` for the TOC (fallback when the ioctls fail)."""
    try:
        proc = subprocess.run(
            [binary, "-Q", "-d", device],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cdparanoia -Q failed on %s: %s", device, exc)
        return None
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return parse_cdparanoia_toc(output, device)


# --------------------------------------------------------------------------- #
# The drive
# --------------------------------------------------------------------------- #
def _ioctl(fd: int, request: int, arg: bytes | int) -> bytes | int:
    if isinstance(arg, int):
        return fcntl.ioctl(fd, request, arg)
    buf = bytearray(arg)
    fcntl.ioctl(fd, request, buf, True)
    return bytes(buf)


def _errno_name(exc: OSError | None) -> str:
    if exc is None:
        return "ioctl failed"
    return errno.errorcode.get(exc.errno or 0, exc.strerror or str(exc.errno))


class OpticalDrive:
    """A CD/DVD drive addressed through ``/dev/sr*``.

    The class exposes a small surface - status, TOC, MCN, eject/close and
    ``extract_track`` - so :class:`ripcd.mock.MockOpticalDrive` can subclass it
    and pretend to be a drive without any hardware.
    """

    #: how long a cached TOC may be reused
    toc_ttl = TOC_TTL_SECONDS

    def __init__(self, device: str, *, vendor: str = "", model: str = "", sg_device: str = ""):
        self.device = device
        self._vendor = vendor
        self._model = model
        self._sg_device = sg_device
        self._cached_toc: Toc | None = None
        self._cached_at = 0.0
        self._capabilities: int | None = None
        self.is_mock = False

    # -- identity ----------------------------------------------------------- #
    @property
    def vendor(self) -> str:
        return self._vendor or self._sysfs("vendor") or ""

    @property
    def model(self) -> str:
        return self._model or self._sysfs("model") or ""

    @property
    def name(self) -> str:
        parts = [part.strip() for part in (self.vendor, self.model) if part and part.strip()]
        return " ".join(parts) if parts else "CD/DVD drive"

    @property
    def sg_device(self) -> str:
        return self._sg_device or self._sysfs("scsi_generic") or ""

    def _sysfs(self, attribute: str) -> str:
        name = os.path.basename(self.device.rstrip("/"))
        base = Path("/sys/class/block") / name / "device"
        if attribute == "scsi_generic":
            for root in (base, base.parent):
                candidates = sorted(root.glob("scsi_generic/*"))
                if candidates:
                    return f"/dev/{candidates[0].name}"
            return ""
        try:
            return (base / attribute).read_text().strip()
        except OSError:
            return ""

    def info(self) -> dict[str, Any]:
        """Everything the UI wants to know about the drive."""
        return {
            "device": self.device,
            "name": self.name,
            "vendor": self.vendor,
            "model": self.model,
            "label": self.name,
            "generic_device": self.sg_device,
            "mock": bool(self.is_mock),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.device} {self.name!r}>"

    # -- primitives (the mock overrides these) ------------------------------ #
    def _open(self, write: bool = False) -> int:
        flags = os.O_RDWR if write else os.O_RDONLY
        try:
            return os.open(self.device, flags | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            raise OpticalError(
                f"cannot open {self.device}: {exc.strerror or exc}",
                err=exc,
                device=self.device,
            ) from exc

    def capabilities(self) -> int:
        """``CDROM_GET_CAPABILITY`` bit mask (0 when the drive does not answer)."""
        if self._capabilities is not None:
            return self._capabilities
        value = 0
        try:
            fd = self._open()
        except OpticalError:
            value = 0
        else:
            try:
                answer = _ioctl(fd, CDROM_GET_CAPABILITY, 0)
                value = int(answer) if isinstance(answer, int) else 0
            except OSError:
                value = 0
            finally:
                os.close(fd)
        self._capabilities = max(0, value)
        return self._capabilities

    def is_cdrom(self) -> bool:
        return self.capabilities() > 0 or os.path.exists(self.device)

    def supports_audio(self) -> bool:
        capabilities = self.capabilities()
        # A drive that cannot report capabilities is probably fine (many USB
        # enclosures return 0), so only a clear "no audio" bit count as a no.
        if capabilities == 0:
            return True
        return bool(capabilities & CDC_PLAY_AUDIO) or bool(capabilities & CDC_DVD)

    def status(self) -> str:
        """``disc_ok``, ``no_disc``, ``tray_open``, ``not_ready`` or ``error``."""
        try:
            fd = self._open()
        except OpticalError:
            return "error"
        try:
            try:
                value = int(_ioctl(fd, CDROM_DRIVE_STATUS, 0) or 0)
            except OSError as exc:
                if exc.errno in {errno.ENOMEDIUM, errno.EIO, errno.ENODEV}:
                    return "no_disc"
                log.debug("drive status of %s failed: %s", self.device, _errno_name(exc))
                return "error"
            if value == CDS_DISC_OK:
                return "disc_ok"
            if value == CDS_TRAY_OPEN:
                return "tray_open"
            if value == CDS_DRIVE_NOT_READY:
                return "not_ready"
            return "no_disc"
        finally:
            os.close(fd)

    def has_disc(self) -> bool:
        return self.status() == "disc_ok"

    def disc_status(self) -> str:
        """``audio``, ``data``, ``mixed``, ``no_disc`` or ``unknown``."""
        try:
            fd = self._open()
        except OpticalError:
            return "no_disc"
        try:
            try:
                value = int(_ioctl(fd, CDROM_DISC_STATUS, 0) or 0)
            except OSError:
                return "unknown"
            return DISC_STATUS_NAMES.get(value, "unknown")
        finally:
            os.close(fd)

    def mcn(self) -> str | None:
        """Media catalogue number (the barcode of commercial CDs), if any."""
        try:
            fd = self._open()
        except OpticalError:
            return None
        try:
            try:
                raw = _ioctl(fd, CDROM_GET_MCN, bytes(24))
            except OSError:
                return None
        finally:
            os.close(fd)
        if not isinstance(raw, bytes):
            return None
        # struct cdrom_mcn: char medium_catalog_number[14]
        text = raw[:14].split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
        digits = re.sub(r"\D", "", text)
        return digits or None

    # -- table of contents -------------------------------------------------- #
    def read_toc(self, use_cache: bool = True) -> Toc | None:
        """The TOC of the disc in the drive, or ``None`` when there is none.

        Never raises: the caller (the scanner) shows the failure in the UI
        instead of crashing a background thread.
        """
        if use_cache and self._cached_toc is not None and time.monotonic() - self._cached_at < self.toc_ttl:
            return self._cached_toc
        toc: Toc | None = None
        try:
            toc = self._read_toc_ioctl()
        except OpticalError as exc:
            log.debug("ioctl TOC of %s failed: %s", self.device, exc)
        except OSError as exc:  # pragma: no cover - defensive
            log.debug("ioctl TOC of %s failed: %s", self.device, _errno_name(exc))
        if toc is None:
            try:
                toc = self._read_toc_cdparanoia()
            except Exception as exc:  # noqa: BLE001 - never break the poll loop
                log.debug("cdparanoia TOC of %s failed: %s", self.device, exc)
        self._cached_toc = toc
        self._cached_at = time.monotonic()
        return toc

    def _read_toc_ioctl(self) -> Toc | None:
        """Read the TOC with ``CDROMREADTOCHDR`` / ``CDROMREADTOCENTRY``."""
        try:
            fd = self._open()
        except OpticalError:
            return None
        try:
            return self._toc_from_fd(fd)
        except NoDiscError:
            return None
        finally:
            os.close(fd)

    def _toc_from_fd(self, fd: int) -> Toc:
        try:
            header = _ioctl(fd, CDROMREADTOCHDR, bytes(4))
        except OSError as exc:
            if exc.errno in {errno.ENOMEDIUM, errno.EIO, errno.ENODEV}:
                raise NoDiscError("the tray is empty", err=exc, device=self.device) from exc
            raise OpticalError(
                f"the drive did not report a table of contents ({_errno_name(exc)})",
                err=exc,
                device=self.device,
            ) from exc

        assert isinstance(header, bytes)
        first, last = struct.unpack("<BB", header[:2])
        if last == 0 or last < first:
            raise NoDiscError("the drive reported an empty table of contents", device=self.device)

        entries: list[tuple[int, int, bool]] = []
        for number in range(first, last + 1):
            entry = self._read_toc_entry(fd, number)
            if entry is None:
                continue
            start, audio = entry
            entries.append((number, start, audio))

        leadout = self._read_toc_entry(fd, CDROM_LEADOUT)
        leadout_lba = leadout[0] if leadout else entries[-1][1]
        if not entries:
            raise NoDiscError("the drive reported an empty table of contents", device=self.device)

        tracks: list[TrackInfo] = []
        for index, (number, start, audio) in enumerate(entries):
            end = entries[index + 1][1] if index + 1 < len(entries) else leadout_lba
            tracks.append(
                TrackInfo(
                    number=number,
                    start_lba=start,
                    duration_frames=max(0, end - start),
                    audio=audio,
                )
            )
        return Toc(
            device=self.device,
            first_track=tracks[0].number,
            last_track=tracks[-1].number,
            leadout_lba=max(leadout_lba, tracks[-1].end_lba),
            tracks=tracks,
            source="ioctl",
        )

    def _read_toc_entry(self, fd: int, number: int) -> tuple[int, bool] | None:
        """One ``struct cdrom_tocentry`` -> ``(start_frames, audio)``."""
        data = bytearray(12)
        data[0] = number & 0xFF  # cdte_track
        data[1] = CDROM_MSF  # cdte_format
        try:
            raw = _ioctl(fd, CDROMREADTOCENTRY, bytes(data))
        except OSError as exc:
            if number == CDROM_LEADOUT:
                log.debug("lead-out entry unavailable on %s: %s", self.device, _errno_name(exc))
                return None
            raise OpticalError(
                f"cannot read track {number} of the disc ({_errno_name(exc)})",
                err=exc,
                device=self.device,
            ) from exc
        assert isinstance(raw, bytes)
        minute, second, frame = raw[1], raw[2], raw[3]
        control = raw[5]
        # bit 2 of cdte_ctrl marks data tracks; the ADR field lives in the high
        # nibble, so mask the control bits before looking at it
        audio = not bool((control & 0x0F) & CDROM_DATA_TRACK)
        return msf_to_frames(minute, second, frame), audio

    def _read_toc_cdparanoia(self) -> Toc | None:
        from shutil import which

        if not which("cdparanoia"):
            return None
        return cdparanoia_toc(self.device)

    # -- tray --------------------------------------------------------------- #
    def eject(self) -> None:
        try:
            self._simple_ioctl(CDROMEJECT, "eject")
        finally:
            self._cached_toc, self._cached_at = None, 0.0

    def close_tray(self) -> None:
        try:
            self._simple_ioctl(CDROMCLOSETRAY, "close the tray")
        finally:
            self._cached_toc, self._cached_at = None, 0.0

    def lock(self, locked: bool = True) -> None:
        """Prevent the tray from being opened while ripping (best effort)."""
        try:
            fd = self._open(write=True)
        except OpticalError:
            return
        try:
            _ioctl(fd, CDROM_LOCKDOOR, 1 if locked else 0)
        except OSError as exc:  # not fatal, plenty of drives/sdks refuse
            log.debug("locking %s failed: %s", self.device, _errno_name(exc))
        finally:
            os.close(fd)

    def _simple_ioctl(self, request: int, action: str) -> None:
        last: OSError | None = None
        for writable in (False, True):
            try:
                fd = self._open(write=writable)
            except OpticalError as exc:
                last = exc
                continue
            try:
                _ioctl(fd, request, 0)
                return
            except OSError as exc:
                last = exc
            finally:
                os.close(fd)
        raise OpticalError(
            f"cannot {action} {self.device}: {_errno_name(last if isinstance(last, OSError) else None)}",
            err=last if isinstance(last, OSError) else None,
            device=self.device,
        )

    def probe(self) -> dict[str, Any]:
        """Full status report used by ``/api/drives`` and the diagnostics panel."""
        report: dict[str, Any] = {
            "device": self.device,
            "available": os.path.exists(self.device),
            "error": "",
        }
        try:
            status = self.status()
        except Exception as exc:  # noqa: BLE001 - a broken drive must not break the API
            status = "error"
            report["error"] = str(exc)
        report.update(
            {
                "status": status,
                "has_disc": status == "disc_ok",
                "disc_kind": "none",
                "track_count": 0,
                "toc_source": "",
                "total_seconds": 0.0,
            }
        )
        report.update(self.info())
        if status == "disc_ok":
            toc = self.read_toc()
            if toc is not None and toc.tracks:
                report.update(
                    {
                        "disc_kind": toc.kind,
                        "track_count": len(toc.tracks),
                        "toc_source": toc.source,
                        "total_seconds": round(toc.total_seconds, 2),
                        "total_time": toc.total_time,
                    }
                )
        return report

    # -- ripping ------------------------------------------------------------ #
    def extract_track(
        self,
        track: TrackInfo,
        outfile: str,
        *,
        paranoia: str = "full",
        speed: int = 0,
        on_progress: Any = None,
        cancel: Any = None,
        log_sink: Any = None,
    ) -> "ExtractResult":
        """Read one track to ``outfile`` (a WAV) with cdparanoia."""
        from shutil import which

        from .extract import ExtractResult, build_cdparanoia_command, run_cdparanoia, verify_wav

        binary = which("cdparanoia")
        if not binary:
            return ExtractResult(ok=False, error="cdparanoia is not installed in this container")
        if not self.has_disc():
            return ExtractResult(ok=False, error=f"no disc in {self.device}")
        command = build_cdparanoia_command(
            binary, self.device, track.number, outfile, paranoia=paranoia, speed=speed
        )
        result = run_cdparanoia(
            command,
            duration_seconds=track.duration_seconds,
            on_progress=on_progress,
            cancel=cancel,
            log_sink=log_sink,
        )
        if not result.ok:
            return result
        ok, error = verify_wav(outfile, expected_seconds=track.duration_seconds)
        if not ok:
            result.ok = False
            result.error = error
            return result
        result.output = outfile
        return result

    # -- misc --------------------------------------------------------------- #
    def exists(self) -> bool:
        return os.path.exists(self.device)

    @property
    def ejectable(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _sg_devices() -> dict[str, str]:
    """Map ``sr0`` -> ``/dev/sg0`` using sysfs when it is available."""
    mapping: dict[str, str] = {}
    for link in Path("/sys/class/block").glob("sr*/device/scsi_generic/*"):
        block = link.parent.parent.parent.name  # .../sr0/device/scsi_generic/sg0
        mapping.setdefault(block, f"/dev/{link.name}")
    return mapping


def find_device_paths() -> list[str]:
    """Every plausible CD/DVD device node, deduplicated by real path."""
    found: dict[str, str] = {}
    for pattern in DEVICE_PATTERNS:
        for path in sorted(glob.glob(pattern)):
            if path in found.values():
                continue
            try:
                real = os.path.realpath(path)
            except OSError:
                real = path
            found.setdefault(real, path)
    return [found[key] for key in sorted(found)]


def discover_drives(extra: Sequence[str] = ()) -> list[OpticalDrive]:
    """Find the CD/DVD drives: explicit devices first, then the usual nodes."""
    sg_map = _sg_devices()
    drives: list[OpticalDrive] = []
    seen: set[str] = set()
    for path in [*extra, *find_device_paths()]:
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        name = os.path.basename(path)
        drive = OpticalDrive(path, sg_device=sg_map.get(name, ""))
        if not drive.sg_device:
            prefix = "sg" if name.startswith("sr") else "sg"
            for candidate in sorted(glob.glob(f"/dev/{prefix}*")):
                if os.path.exists(candidate):
                    drive._sg_device = candidate
                    break
        drives.append(drive)
    return drives


#: Backwards compatible alias - both names show up in the code base.
find_drives = discover_drives


def cdrom_info() -> dict[str, str]:
    """Parsed ``/proc/sys/dev/cdrom/info``, when the kernel exposes it."""
    try:
        text = Path("/proc/sys/dev/cdrom/info").read_text()
    except OSError:
        return {}
    info: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            info[key.strip().lower().replace(" ", "_")] = value.strip()
    return info


def device_tree_snapshot() -> dict[str, Any]:
    """What this container can actually see - powers the diagnostics panel.

    A container with a CD drive needs both the block device (``/dev/sr0``) and
    its SCSI generic sibling (``/dev/sg0``); listing what is present makes a
    missing ``--device`` argument obvious.
    """
    devices: dict[str, bool] = {}
    for pattern in DEVICE_PATTERNS:
        for path in sorted(glob.glob(pattern)):
            devices[path] = os.path.exists(path)
    for path in ("/dev/sr0", "/dev/sg0", "/dev/sg1", "/dev/cdrom", "/dev/dvd"):
        devices.setdefault(path, os.path.exists(path))

    sr_in_sysfs = sorted(os.path.basename(path) for path in glob.glob("/sys/class/block/sr*"))
    sg_in_sysfs = sorted(path.name for path in Path("/sys/class/scsi_generic").glob("sg*"))
    info = cdrom_info()
    return {
        "devices": devices,
        "sr_in_sysfs": [f"/dev/{name}" for name in sr_in_sysfs],
        "sg_in_sysfs": [f"/dev/{name}" for name in sg_in_sysfs],
        "cdrom_info": info,
        "drives_in_kernel": info.get("drive_name_0", ""),
        "containerised": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
        "privileged_hint": (
            "Pass the CD drive into the container (devices: /dev/sr0 and the matching "
            "/dev/sg*) and add group_add for the drive's group if the rip reports "
            "'unreadable disc'."
        ),
    }


__all__ = [
    "CDC_PLAY_AUDIO",
    "CDS_AUDIO",
    "CDS_MIXED",
    "CDS_NO_INFO",
    "DataDiscError",
    "FRAMES_PER_SECOND",
    "LEAD_IN_FRAMES",
    "NoDiscError",
    "OpticalDrive",
    "OpticalError",
    "Toc",
    "TrackInfo",
    "cdparanoia_toc",
    "cdrom_info",
    "device_tree_snapshot",
    "disc_id_payload",
    "disc_signature",
    "discover_drives",
    "find_device_paths",
    "find_drives",
    "format_msf",
    "frames_to_lba",
    "frames_to_msf",
    "freedb_disc_id",
    "has_data_track",
    "lba_to_frames",
    "lba_to_msf",
    "msf_to_frames",
    "musicbrainz_disc_id",
    "musicbrainz_toc_string",
    "parse_cdparanoia_toc",
    "parse_msf",
    "toc_candidates",
]


# ``format_msf`` was the original name of :func:`frames_to_msf`.
format_msf = frames_to_msf
