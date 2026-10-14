"""The rip engine: one job walks a disc from TOC to tagged files."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import __version__
from .artwork import CoverArtClient
from .config import FORMATS, Settings
from .drives import DriveProvider
from .encoder import (
    COVER_MODE,
    EncodeRequest,
    build_ffmpeg_command,
    output_extension,
    resolve_quality,
    run_ffmpeg,
    verify_output,
)
from .metadata import AlbumMetadata, MetadataResolver, TrackMetadata
from .optical import OpticalDrive, Toc, TrackInfo, disc_signature
from .paths import (
    PathNotAllowed,
    check_path,
    render_template,
    safe_filename,
    sanitize_component,
    sanitize_relative_path,
    validate_template,
)
from .system import probe_tool

log = logging.getLogger("ripcd.ripper")

TRACK_STATES = ("pending", "ripping", "encoding", "done", "error", "canceled", "skipped")
JOB_STATES = ("queued", "running", "done", "error", "canceled")

#: How much of a track's progress bar is reading vs encoding.
RIP_WEIGHT = 0.6


class RipError(RuntimeError):
    """Raised when a job cannot continue."""


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


@dataclass
class TrackProgress:
    number: int
    title: str = ""
    artist: str = ""
    duration_seconds: float = 0.0
    state: str = "pending"
    stage: str = ""
    rip_percent: float = 0.0
    encode_percent: float = 0.0
    message: str = ""
    output: str = ""
    size_bytes: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def percent(self) -> float:
        if self.state in {"done"}:
            return 100.0
        if self.state in {"canceled", "skipped"}:
            return 0.0
        if self.state == "error":
            return round(self.rip_percent * RIP_WEIGHT + self.encode_percent * (1 - RIP_WEIGHT), 1)
        value = self.rip_percent * RIP_WEIGHT + self.encode_percent * (1 - RIP_WEIGHT)
        return round(min(99.9, value), 1)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["percent"] = self.percent
        return data


@dataclass
class RipJob:
    """A queued or running rip."""

    id: str
    device: str
    requested_output_dir: str
    format: str = "flac"
    quality: str = ""
    track_numbers: list[int] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)
    track_titles: dict[str, str] = field(default_factory=dict)
    paranoia: str = "full"
    read_speed: int = 0
    embed_cover: bool = True
    write_m3u: bool = True
    keep_wav: bool = False
    eject_after_rip: bool = True
    fetch_cover: bool = True
    metadata_provider: str = "musicbrainz"
    album_folder_template: str = "{album_artist}/{album}"
    track_file_template: str = "{track:02d} - {title}"

    state: str = "queued"
    stage: str = "waiting"
    message: str = ""
    percent: float = 0.0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    disc_id: str = ""
    disc_signature: str = ""
    disc_total_seconds: float = 0.0
    output_dir: str = ""
    temp_dir: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tracks: list[TrackProgress] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    cover_path: str = ""
    cover_url: str = ""
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    bytes_written: int = 0
    mock: bool = False
    auto: bool = False

    _cancel: bool = field(default=False, repr=False)

    # -- state ------------------------------------------------------------- #

    @property
    def selected_numbers(self) -> list[int]:
        """Explicitly selected tracks, or the tracks known so far (empty = all)."""
        if self.track_numbers:
            return sorted(self.track_numbers)
        return [t.number for t in self.tracks]

    @property
    def is_active(self) -> bool:
        return self.state in {"queued", "running"}

    def request_cancel(self) -> None:
        self._cancel = True

    def cancel_requested(self) -> bool:
        return self._cancel

    def add_log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {level.upper():<7} {message}"
        self.log.append(line)
        if len(self.log) > 500:
            del self.log[:250]

    def computed_percent(self) -> float:
        """Overall progress derived from the per-track progress."""
        if self.state == "done":
            return 100.0
        if self.tracks:
            done = sum(t.percent for t in self.tracks) / (100.0 * len(self.tracks))
            return round(min(99.9, done * 100), 1)
        return self.percent if 0 <= self.percent <= 100 else 0.0

    def recompute(self) -> float:
        """Refresh :attr:`percent` from the per-track progress."""
        self.percent = self.computed_percent()
        return self.percent

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device,
            "output_dir": self.output_dir or self.requested_output_dir,
            "requested_output_dir": self.requested_output_dir,
            "format": self.format,
            "quality": resolve_quality(self.format, self.quality),
            "extension": output_extension(self.format),
            "paranoia": self.paranoia,
            "read_speed": self.read_speed,
            "track_numbers": list(self.track_numbers),
            "overrides": dict(self.overrides),
            "album_folder_template": self.album_folder_template,
            "track_file_template": self.track_file_template,
            "state": self.state,
            "stage": self.stage,
            "message": self.message,
            "percent": self.computed_percent(),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "disc_id": self.disc_id,
            "disc_signature": self.disc_signature,
            "disc_total_seconds": self.disc_total_seconds,
            "metadata": self.metadata,
            "tracks": [t.to_dict() for t in self.tracks],
            "files": self.files,
            "cover_path": self.cover_path,
            "cover_url": self.cover_url,
            "warnings": self.warnings,
            "log": self.log,
            "bytes_written": self.bytes_written,
            "mock": self.mock,
            "auto": self.auto,
            "elapsed": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RipJob":
        payload = dict(data)
        payload.pop("elapsed", None)
        payload.pop("extension", None)
        fields = set(TrackProgress.__dataclass_fields__)  # type: ignore[attr-defined]
        tracks = [
            TrackProgress(**{k: v for k, v in track.items() if k in fields})
            for track in payload.pop("tracks", [])
        ]
        job = cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})  # type: ignore[attr-defined]
        job.tracks = tracks
        return job


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def apply_overrides(album: AlbumMetadata, overrides: dict[str, str], toc: Toc | None = None,
                    track_titles: dict[str, str] | None = None) -> AlbumMetadata:
    """User supplied values win over anything a metadata provider returned."""
    result = AlbumMetadata.from_dict(album.to_dict())
    mapping = {
        "album": "title",
        "album_artist": "album_artist",
        "artist": "album_artist",
        "year": "year",
        "date": "date",
        "genre": "genre",
        "label": "label",
        "catalog_number": "catalog_number",
        "barcode": "barcode",
    }
    changed = False
    for key, attribute in mapping.items():
        value = str((overrides or {}).get(key) or "").strip()
        if value:
            setattr(result, attribute, value)
            changed = True
    if overrides and overrides.get("year") and not overrides.get("date"):
        result.date = str(overrides["year"])
    if track_titles:
        for number, title in track_titles.items():
            if not str(title).strip():
                continue
            changed = True
            meta = result.track(int(number))
            if meta is None:
                result.tracks.append(TrackMetadata(number=int(number), title=str(title)))
            else:
                meta.title = str(title)
        result.tracks.sort(key=lambda t: t.number)
    if changed:
        if result.source in {"none", ""}:
            result.source = "manual"
        else:
            result.source = f"{result.source}+manual"
    return result


def unique_directory(path: str) -> str:
    """Return ``path`` or ``path (2)``, ``path (3)``... when it is taken."""
    if not os.path.exists(path):
        return path
    try:
        if not os.listdir(path):
            return path
    except OSError:
        return path
    base = path
    for index in range(2, 100):
        candidate = f"{base} ({index})"
        if not os.path.exists(candidate):
            return candidate
    return f"{base} ({int(time.time())})"


def album_directory(output_dir: str, album: AlbumMetadata, template: str) -> str:
    """Compute the album folder for a job (empty template = output dir itself)."""
    template = (template or "").strip()
    if not template:
        return output_dir
    try:
        rendered = render_template(template, album.album_dir_context(), fallback="Unknown")
    except Exception:  # noqa: BLE001 - a broken template must not lose a rip
        rendered = f"{album.album_artist}/{album.title}"
    relative = sanitize_relative_path(rendered)
    return os.path.join(output_dir, relative) if relative else output_dir


def track_filename(job: RipJob, album: AlbumMetadata, track: TrackInfo, *, track_total: int) -> str:
    context = album.track_context(track.number, track_total=track_total)
    try:
        rendered = render_template(job.track_file_template, context, fallback="Unknown")
    except Exception:  # noqa: BLE001
        rendered = f"{track.number:02d} - {context.get('title') or 'Track'}"
    name = safe_filename(rendered.replace("/", "-"), output_extension(job.format))
    return name


def build_tags(job: RipJob, album: AlbumMetadata, track_number: int, *, track_total: int) -> dict[str, Any]:
    meta = album.track(track_number)
    title = (meta.title if meta else "") or f"Track {track_number:02d}"
    artist = (meta.artist if meta else "") or album.album_artist
    tags: dict[str, Any] = {
        "title": title,
        "artist": artist,
        "album": album.title,
        "album_artist": album.album_artist,
        "date": album.date or album.year,
        "genre": album.genre,
        "track": track_number,
        "track_total": track_total,
        "disc": album.disc,
        "disc_total": album.disc_total,
        "label": album.label,
        "catalog_number": album.catalog_number,
        "barcode": album.barcode,
        "comment": f"Ripped with RipCD {__version__}",
        "musicbrainz_album_id": album.release_id,
        "musicbrainz_releasegroup_id": album.release_group_id,
        "musicbrainz_discid": album.disc_id,
        "musicbrainz_trackid": (meta.recording_id if meta else ""),
        "musicbrainz_releasetrackid": (meta.release_track_id if meta else ""),
    }
    return {key: value for key, value in tags.items() if value not in (None, "")}


def _playlist_name(album: AlbumMetadata) -> str:
    base = sanitize_component(f"{album.album_artist} - {album.title}" if album.album_artist else album.title)
    return f"{base}.m3u8"


def _info_file_name(album: AlbumMetadata) -> str:
    base = sanitize_component(f"{album.album_artist} - {album.title}" if album.album_artist else album.title)
    return f"{base}.ripcd.json"


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


class RipRunner:
    """Executes a single :class:`RipJob`. One instance per app."""

    def __init__(
        self,
        drives: DriveProvider,
        resolver: MetadataResolver,
        covers: CoverArtClient,
        *,
        settings_store,
        on_event: Callable[[str, Any], None] | None = None,
    ):
        self.drives = drives
        self.resolver = resolver
        self.covers = covers
        self.settings_store = settings_store
        self.on_event = on_event or (lambda *_args, **_kwargs: None)

    # -- public ------------------------------------------------------------- #

    def run(self, job: RipJob) -> RipJob:
        settings = self.settings_store.settings
        job.state = "running"
        job.started_at = time.time()
        job.stage = "reading"
        job.message = "reading the disc"
        self._emit("job", job)

        try:
            self._run(job, settings)
        except RipError as exc:
            job.state = "error"
            job.error = str(exc)
            job.add_log(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - never leave a job hanging
            log.exception("unexpected rip failure")
            job.state = "error"
            job.error = f"unexpected error: {exc}"
            job.add_log(job.error, "error")
        finally:
            job.finished_at = time.time()
            if job.state == "running":
                job.state = "done"
            if job.state == "done":
                job.percent = 100.0
                job.stage = "done"
            self._cleanup_temp(job)
            self._emit("job", job)
        return job

    # -- pipeline ----------------------------------------------------------- #

    def _run(self, job: RipJob, settings: Settings) -> None:
        ffmpeg = probe_tool("ffmpeg", check_version=False)
        drive = self.drives.get(job.device)
        if drive is None:
            raise RipError(
                f"drive {job.device or '(auto)'} was not found - check that the device is passed to the container "
                "(/dev/sr0 and /dev/sg*)"
            )
        job.device = drive.device
        job.mock = bool(getattr(drive, "is_mock", False))

        self._check_space(job, settings)

        toc = drive.read_toc()
        if toc is None or not toc.tracks:
            raise RipError("no readable disc in the drive (or the drive cannot report its table of contents)")
        job.disc_signature = disc_signature(toc)
        job.disc_total_seconds = toc.total_seconds
        job.add_log(f"TOC: {len(toc.tracks)} track(s), {toc.to_dict()['total_time']} total")

        # Only real audio tracks are ripped: an enhanced CD's data track and a
        # plain data CD must never be fed to cdparanoia.
        audio_tracks = toc.audio_tracks
        if not audio_tracks:
            raise RipError(
                "this disc has no audio tracks - it looks like a data CD (CD-ROM), nothing to rip"
            )
        by_number = {t.number: t for t in audio_tracks}
        if job.track_numbers:
            selected = [by_number[n] for n in sorted(job.track_numbers) if n in by_number]
            if not selected:
                raise RipError(
                    f"none of the selected tracks ({', '.join(str(n) for n in job.track_numbers)}) exist on this disc"
                )
        else:
            selected = list(audio_tracks)  # no explicit selection = the whole disc
        if not selected:
            raise RipError("this disc has no audio tracks to rip")

        # ---- metadata ----------------------------------------------------- #
        job.stage = "metadata"
        job.message = "looking up the disc"
        self._emit("job", job)
        album = self._resolve_metadata(job, drive, toc)

        # ---- destination -------------------------------------------------- #
        job.output_dir = unique_directory(album_directory(job.requested_output_dir, album, job.album_folder_template))
        check = check_path(job.output_dir, settings.media_roots)
        if not check.ok and not check.writable:
            raise RipError(check.error or f"cannot write to {job.output_dir}")
        try:
            os.makedirs(job.output_dir, exist_ok=True)
        except OSError as exc:
            raise RipError(f"cannot create {job.output_dir}: {exc}") from exc
        job.add_log(f"writing to {job.output_dir}")

        # ---- cover art ---------------------------------------------------- #
        if job.fetch_cover and job.metadata_provider != "none":
            cover = self._fetch_cover(job, album)
            if cover:
                job.cover_path = cover
                try:
                    shutil.copyfile(cover, os.path.join(job.output_dir, "cover" + os.path.splitext(cover)[1]))
                except OSError as exc:
                    job.add_log(f"could not write cover.jpg: {exc}", "warning")

        # ---- tracks ------------------------------------------------------- #
        job.tracks = [
            TrackProgress(
                number=track.number,
                title=(album.track(track.number).title if album.track(track.number) else f"Track {track.number:02d}"),
                artist=(album.track(track.number).artist if album.track(track.number) else album.album_artist),
                duration_seconds=track.seconds,
            )
            for track in selected
        ]
        job.temp_dir = self._make_temp_dir(job, settings)
        track_total = len(audio_tracks)

        try:
            for progress, track in zip(job.tracks, selected):
                if job.cancel_requested():
                    progress.state = "canceled"
                    job.add_log(f"track {track.number:02d} canceled", "warning")
                    continue
                self._rip_track(job, drive, album, track, progress, track_total, ffmpeg)
                job.recompute()
                self._emit("job", job)
        finally:
            if job.cancel_requested():
                job.state = "canceled"
                job.message = "canceled"
                job.add_log("job canceled by the user", "warning")

        if any(t.state == "done" for t in job.tracks):
            self._finalize(job, album, track_total)

        if job.state == "running":
            failures = [t for t in job.tracks if t.state == "error"]
            if failures and len(failures) == len(job.tracks):
                job.state = "error"
                job.error = failures[0].error or "all tracks failed"
            elif failures:
                job.state = "done"
                job.warnings.append(f"{len(failures)} track(s) failed")
                job.message = "finished with warnings"
            else:
                job.state = "done"
                job.message = "finished"

        if job.eject_after_rip and job.state in {"done", "canceled"}:
            try:
                drive.eject()
                job.add_log("disc ejected")
            except Exception as exc:  # noqa: BLE001
                job.add_log(f"eject failed: {exc}", "warning")

    # -- steps -------------------------------------------------------------- #

    def _resolve_metadata(self, job: RipJob, drive: OpticalDrive, toc: Toc) -> AlbumMetadata:
        mcn = ""
        try:
            mcn = drive.mcn() or ""
        except Exception:  # noqa: BLE001
            mcn = ""
        signature = disc_signature(toc)
        if job.metadata_provider == "none":
            from .metadata import unknown_album

            album = unknown_album(toc, reason="metadata lookup disabled")
        else:
            album = self.resolver.resolve_now(signature, toc, mcn)
            job.add_log(f"metadata source: {album.source} ({album.title} - {album.album_artist})")
        album = apply_overrides(album, job.overrides, toc, job.track_titles)
        job.disc_id = album.disc_id
        job.metadata = album.to_dict()
        job.cover_url = album.cover_url
        for warning in album.warnings:
            if warning not in job.warnings:
                job.warnings.append(warning)
        return album

    def _fetch_cover(self, job: RipJob, album: AlbumMetadata) -> str:
        if not (album.release_id or album.release_group_id):
            return ""
        job.add_log("fetching cover art")
        try:
            path = self.covers.fetch(album.release_id, size="500", release_group_id=album.release_group_id)
        except Exception as exc:  # noqa: BLE001
            job.add_log(f"cover art lookup failed: {exc}", "warning")
            return ""
        if path:
            album.cover_path = path
            job.metadata = album.to_dict()
        return path or ""

    def _check_space(self, job: RipJob, settings: Settings) -> None:
        check = check_path(job.requested_output_dir, settings.media_roots)
        if not check.ok and not check.writable:
            raise RipError(check.error or f"cannot write to {job.requested_output_dir}")
        if check.free_bytes is not None:
            needed = 800 * 1024 * 1024
            if check.free_bytes < needed:
                job.warnings.append(
                    f"only {check.free_bytes // (1024 * 1024)} MB free in {check.path} - a full CD can need ~800 MB"
                )

    def _make_temp_dir(self, job: RipJob, settings: Settings) -> str:
        base = getattr(settings, "temp_dir", "") or os.path.join(job.requested_output_dir, ".ripcd-tmp")
        path = os.path.join(base, job.id)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise RipError(f"cannot create the temporary folder {path}: {exc}") from exc
        return path

    def _cleanup_temp(self, job: RipJob) -> None:
        if job.temp_dir and os.path.isdir(job.temp_dir):
            try:
                shutil.rmtree(job.temp_dir, ignore_errors=True)
            except OSError:
                pass
        # remove the container folder when it is empty
        parent = os.path.dirname(job.temp_dir) if job.temp_dir else ""
        if parent and os.path.isdir(parent):
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass

    def _rip_track(
        self,
        job: RipJob,
        drive: OpticalDrive,
        album: AlbumMetadata,
        track: TrackInfo,
        progress: TrackProgress,
        track_total: int,
        ffmpeg,
    ) -> None:
        started = time.monotonic()
        title = progress.title
        job.stage = "ripping"
        job.message = f"reading track {track.number:02d} - {title}"
        progress.state = "ripping"
        progress.stage = "reading"
        self._emit("job", job)

        wav = os.path.join(job.temp_dir, f"track{track.number:02d}.wav")

        def on_rip(percent: float, _message: str) -> None:
            progress.rip_percent = percent
            progress.stage = "reading"
            job.recompute()
            self._emit("progress", job)

        result = drive.extract_track(
            track,
            wav,
            paranoia=job.paranoia,
            speed=job.read_speed,
            on_progress=on_rip,
            cancel=job.cancel_requested,
            log_sink=lambda level, message: job.add_log(f"track {track.number:02d}: {message}", level),
        )
        if not result.ok:
            if result.canceled:
                progress.state = "canceled"
                progress.stage = ""
                return
            progress.state = "error"
            progress.error = result.error
            job.add_log(f"track {track.number:02d} failed to read: {result.error}", "error")
            return
        for warning in result.warnings:
            progress.warnings.append(warning)
        progress.rip_percent = 100.0

        # ---- encode ------------------------------------------------------- #
        filename = track_filename(job, album, track, track_total=track_total)
        target = os.path.join(job.output_dir, filename)
        tags = build_tags(job, album, track.number, track_total=track_total)
        progress.stage = "encoding"
        progress.state = "encoding"
        job.stage = "encoding"
        job.message = f"encoding {filename}"
        self._emit("job", job)

        def on_encode(percent: float, _message: str) -> None:
            progress.encode_percent = percent
            progress.stage = "encoding"
            job.recompute()
            self._emit("progress", job)

        ok, error = self._encode_track(job, ffmpeg, wav, target, tags, track, on_encode)
        if not ok:
            if job.cancel_requested():
                progress.state = "canceled"
            else:
                progress.state = "error"
                progress.error = error
                job.add_log(f"track {track.number:02d} failed to encode: {error}", "error")
            self._remove_quietly(target)
            self._remove_quietly(wav)
            return

        size = os.path.getsize(target) if os.path.isfile(target) else 0
        job.bytes_written += size
        progress.state = "done"
        progress.stage = ""
        progress.rip_percent = 100.0
        progress.encode_percent = 100.0
        progress.output = target
        progress.size_bytes = size
        progress.seconds = round(time.monotonic() - started, 1)
        if target not in job.files:
            job.files.append(target)
        job.add_log(f"track {track.number:02d} written: {filename} ({size / 1024 / 1024:.1f} MB)")

        if not job.keep_wav:
            self._remove_quietly(wav)

    def _encode_track(
        self,
        job: RipJob,
        ffmpeg,
        wav: str,
        target: str,
        tags: dict[str, Any],
        track: TrackInfo,
        on_progress: Callable[[float, str], None],
    ) -> tuple[bool, str]:
        if job.format == "wav" or not ffmpeg.available:
            if not ffmpeg.available and job.format != "wav":
                if not job.mock:
                    return False, "ffmpeg is not installed in this image"
                # demo mode without ffmpeg: keep the WAV under the target name
                job.add_log("ffmpeg missing - demo mode keeps the raw WAV instead", "warning")
            try:
                os.replace(wav, target)
            except OSError as exc:
                return False, f"cannot write {target}: {exc}"
            return True, ""

        cover = job.cover_path if job.embed_cover and COVER_MODE.get(job.format) in {"attached_pic", "id3"} else None
        request = EncodeRequest(
            source_wav=wav,
            output=target,
            fmt=job.format,
            quality=job.quality,
            tags={k: str(v) for k, v in tags.items()},
            cover=cover,
        )
        result = run_ffmpeg(
            build_ffmpeg_command(ffmpeg.path, request),
            duration_seconds=track.seconds,
            on_progress=on_progress,
            cancel=job.cancel_requested,
            log_sink=lambda level, message: job.add_log(f"track {track.number:02d}: {message}", level),
        )
        if not result.ok:
            return False, result.error
        ok, problem = verify_output(target)
        return (True, "") if ok else (False, problem)

    def _finalize(self, job: RipJob, album: AlbumMetadata, track_total: int) -> None:
        job.stage = "finalizing"
        job.message = "writing playlist and metadata"
        self._emit("job", job)

        done = [t for t in job.tracks if t.state == "done" and t.output]
        if job.write_m3u and done:
            playlist = os.path.join(job.output_dir, _playlist_name(album))
            try:
                with open(playlist, "w", encoding="utf-8") as handle:
                    handle.write("#EXTM3U\n")
                    for track in sorted(done, key=lambda t: t.number):
                        handle.write(f"#EXTINF:{int(track.duration_seconds)},{track.artist} - {track.title}\n")
                        handle.write(os.path.basename(track.output) + "\n")
                job.files.append(playlist)
            except OSError as exc:
                job.add_log(f"could not write the playlist: {exc}", "warning")

        info = {
            "app": f"RipCD {__version__}",
            "ripped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "job_id": job.id,
            "disc_id": album.disc_id,
            "disc_toc": album.toc_string,
            "device": job.device,
            "format": job.format,
            "quality": resolve_quality(job.format, job.quality),
            "tracks": track_total,
            "metadata": album.to_dict(),
            "overrides": job.overrides,
            "files": [os.path.basename(f) for f in job.files],
            "warnings": job.warnings,
            "mock": job.mock,
        }
        info_path = os.path.join(job.output_dir, _info_file_name(album))
        try:
            with open(info_path, "w", encoding="utf-8") as handle:
                json.dump(info, handle, indent=2, default=str)
            job.files.append(info_path)
        except OSError as exc:
            job.add_log(f"could not write {os.path.basename(info_path)}: {exc}", "warning")

        job.add_log(f"finished: {len(done)} track(s) in {job.output_dir}")

    # -- utilities ---------------------------------------------------------- #

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _emit(self, event: str, job: RipJob) -> None:
        try:
            self.on_event(event, job)
        except Exception:  # noqa: BLE001 - events must never break a rip
            log.debug("event handler failed", exc_info=True)


def validate_job_request(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Validate/normalise a job request coming from the UI."""
    from .encoder import default_quality

    fmt = str(payload.get("format") or settings.format)
    if fmt not in FORMATS:
        raise RipError(f"unsupported format '{fmt}'")
    quality = str(payload.get("quality") or "")
    if quality and quality not in FORMATS[fmt]["qualities"]:
        quality = default_quality(fmt)

    output_dir = str(payload.get("output_dir") or settings.output_dir)
    if not output_dir.strip():
        raise RipError("no output folder selected")
    try:
        check = check_path(output_dir, settings.media_roots)
    except (OSError, PathNotAllowed) as exc:
        raise RipError(str(exc)) from exc
    if not check.ok and not check.writable:
        raise RipError(check.error or f"cannot write to {output_dir}")

    tracks: list[int] = []
    for value in payload.get("tracks") or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 99 and number not in tracks:
            tracks.append(number)
    tracks.sort()

    album_template = str(payload.get("album_folder_template") or settings.album_folder_template)
    track_template = str(payload.get("track_file_template") or settings.track_file_template)
    try:
        validate_template(album_template or "{album}")
        validate_template(track_template)
    except Exception as exc:  # noqa: BLE001
        raise RipError(str(exc)) from exc

    overrides = {str(k): str(v) for k, v in (payload.get("overrides") or {}).items() if str(v).strip()}
    track_titles = {
        str(int(k)): str(v) for k, v in (payload.get("track_titles") or {}).items() if str(v).strip()
    }

    return {
        "device": str(payload.get("device") or settings.device or ""),
        "requested_output_dir": check.path or output_dir,
        "format": fmt,
        "quality": quality or default_quality(fmt),
        "track_numbers": tracks,
        "overrides": overrides,
        "track_titles": track_titles,
        "paranoia": str(payload.get("paranoia") or settings.paranoia),
        "read_speed": int(payload.get("read_speed") or settings.read_speed or 0),
        "embed_cover": bool(payload.get("embed_cover", settings.embed_cover)),
        "write_m3u": bool(payload.get("write_m3u", settings.write_m3u)),
        "keep_wav": bool(payload.get("keep_wav", settings.keep_wav)),
        "eject_after_rip": bool(payload.get("eject_after_rip", settings.eject_after_rip)),
        "fetch_cover": bool(payload.get("fetch_cover", settings.fetch_cover)),
        "metadata_provider": str(payload.get("metadata_provider") or settings.metadata_provider),
        "album_folder_template": album_template,
        "track_file_template": track_template,
    }


def make_job(payload: dict[str, Any], settings: Settings, *, auto: bool = False) -> RipJob:
    values = validate_job_request(payload, settings)
    job = RipJob(id=new_job_id(), auto=auto, **values)  # type: ignore[arg-type]
    job.mock = bool(payload.get("mock"))
    return job


def preview_paths(job: RipJob, album: AlbumMetadata, toc: Toc) -> list[dict[str, str]]:
    """What the files would be called - shown in the UI before ripping."""
    directory = album_directory(job.requested_output_dir, album, job.album_folder_template)
    audio = toc.audio_tracks or toc.tracks
    selected = [t for t in audio if not job.track_numbers or t.number in job.track_numbers]
    result = []
    for track in selected:
        name = track_filename(job, album, track, track_total=len(audio))
        result.append({"track": str(track.number), "name": name, "path": os.path.join(directory, name)})
    return result
