"""Metadata lookup: MusicBrainz disc ID + Cover Art Archive.

Only the public MusicBrainz web service is used (no API key needed) and every
request is rate limited to 1/second as their guidelines require.  Results are
cached on disk so a second rip of the same CD needs no network at all.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .optical import Toc, musicbrainz_disc_id, musicbrainz_toc_string, toc_candidates

log = logging.getLogger("ripcd.metadata")

MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = f"RipCD/{__version__} (https://github.com/Delgame880/RipCd-Docker)"

#: MusicBrainz asks for <= 1 request per second.
_MIN_INTERVAL = 1.05
_rate_lock = threading.Lock()
_last_request = 0.0


class MetadataError(RuntimeError):
    pass


def _throttle() -> None:
    global _last_request
    with _rate_lock:
        delta = time.monotonic() - _last_request
        if delta < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - delta)
        _last_request = time.monotonic()


def http_get_json(url: str, *, timeout: float = 20.0, contact: str = "") -> Any:
    headers = {"User-Agent": USER_AGENT + (f" {contact}" if contact else ""), "Accept": "application/json"}
    request = urllib.request.Request(url, headers=headers)
    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed host)
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise MetadataError(f"MusicBrainz returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise MetadataError(f"MusicBrainz request failed: {exc}") from exc


def http_get_bytes(url: str, *, timeout: float = 30.0, contact: str = "", limit: int = 8 * 1024 * 1024) -> tuple[bytes, str] | None:
    headers = {"User-Agent": USER_AGENT + (f" {contact}" if contact else ""), "Accept": "image/*"}
    request = urllib.request.Request(url, headers=headers)
    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed host)
            data = response.read(limit)
            return data, response.headers.get("Content-Type", "image/jpeg")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class TrackMetadata:
    number: int
    title: str
    artist: str = ""
    length_ms: int | None = None
    recording_id: str = ""
    release_track_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackMetadata":
        return cls(
            number=int(data.get("number") or 0),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
            length_ms=data.get("length_ms"),
            recording_id=str(data.get("recording_id") or ""),
            release_track_id=str(data.get("release_track_id") or ""),
        )


@dataclass
class AlbumMetadata:
    """Everything we know about the disc currently in the drive."""

    source: str = "none"                    # musicbrainz | manual | none
    confidence: float = 0.0
    disc_id: str = ""
    toc_string: str = ""
    release_id: str = ""
    release_group_id: str = ""
    title: str = ""
    album_artist: str = ""
    year: str = ""
    date: str = ""
    genre: str = ""
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    country: str = ""
    disc: int = 1
    disc_total: int = 1
    total_tracks: int = 0
    cover_url: str = ""
    cover_path: str = ""
    tracks: list[TrackMetadata] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetched_at: float = 0.0

    # -- helpers ---------------------------------------------------------- #

    def track(self, number: int) -> TrackMetadata | None:
        for item in self.tracks:
            if item.number == number:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tracks"] = [asdict(t) for t in self.tracks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlbumMetadata":
        payload = dict(data or {})
        payload["tracks"] = [TrackMetadata.from_dict(t) for t in payload.get("tracks") or []]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def album_dir_context(self) -> dict[str, Any]:
        return {
            "album": self.title,
            "album_artist": self.album_artist,
            "artist": self.album_artist,
            "year": self.year,
            "genre": self.genre,
            "label": self.label,
            "catalog_number": self.catalog_number,
            "disc_id": self.disc_id,
            "format": "",
            "total_tracks": self.total_tracks,
            "disc": self.disc,
            "disc_total": self.disc_total,
        }

    def track_context(self, number: int, *, track_total: int = 0) -> dict[str, Any]:
        meta = self.track(number)
        context = self.album_dir_context()
        context.update(
            {
                "track": number,
                "track_raw": f"{number:02d}",
                "title": meta.title if meta else f"Track {number:02d}",
                "track_artist": (meta.artist if meta and meta.artist else self.album_artist),
                "total_tracks": track_total or self.total_tracks,
            }
        )
        return context


def unknown_album(toc: Toc, *, disc_id: str = "", reason: str = "") -> AlbumMetadata:
    """The fallback when no metadata source matched."""
    tracks = [
        TrackMetadata(number=t.number, title=f"Track {t.number:02d}")
        for t in (toc.audio_tracks or toc.tracks)
    ]
    warnings = []
    if reason:
        warnings.append(reason)
    return AlbumMetadata(
        source="none",
        confidence=0.0,
        disc_id=disc_id or (musicbrainz_disc_id(toc) or ""),
        toc_string=musicbrainz_toc_string(toc) or "",
        title="Unknown Album",
        album_artist="Unknown Artist",
        total_tracks=len(tracks),
        tracks=tracks,
        warnings=warnings,
        fetched_at=time.time(),
    )


# --------------------------------------------------------------------------- #
# MusicBrainz parsing
# --------------------------------------------------------------------------- #


def _artist_credit(credits: list[dict[str, Any]] | None) -> str:
    if not credits:
        return ""
    parts: list[str] = []
    for credit in credits:
        name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
        parts.append(f"{name}{credit.get('joinphrase') or ''}")
    return "".join(parts).strip()


def _primary_genre(release: dict[str, Any]) -> str:
    for key in ("genres", "tags"):
        entries = release.get(key) or []
        if isinstance(entries, list) and entries:
            best = sorted(entries, key=lambda e: e.get("count", 0), reverse=True)[0]
            if best.get("name"):
                return str(best["name"]).title()
    group = release.get("release-group") or {}
    for key in ("genres", "tags"):
        entries = group.get(key) or []
        if isinstance(entries, list) and entries:
            best = sorted(entries, key=lambda e: e.get("count", 0), reverse=True)[0]
            if best.get("name"):
                return str(best["name"]).title()
    return ""


def _pick_media(release: dict[str, Any], track_count: int) -> dict[str, Any]:
    media = release.get("media") or []
    if not media:
        return {}
    # Prefer the medium that has as many tracks as the disc in the drive.
    exact = [m for m in media if (m.get("track-count") or len(m.get("tracks") or [])) == track_count]
    if exact:
        return exact[0]
    audio = [m for m in media if (m.get("format") or "").upper() not in {"CD", "DVD"} or True]
    return audio[0] if audio else media[0]


def parse_release(release: dict[str, Any], toc: Toc, *, source: str = "musicbrainz",
                  confidence: float = 1.0, disc_id: str = "") -> AlbumMetadata:
    """Turn a MusicBrainz release JSON object into :class:`AlbumMetadata`."""
    audio_tracks = toc.audio_tracks or toc.tracks
    media = _pick_media(release, len(audio_tracks))
    mb_tracks = media.get("tracks") or []

    tracks: list[TrackMetadata] = []
    for index, toc_track in enumerate(audio_tracks):
        if index < len(mb_tracks):
            raw = mb_tracks[index]
            title = raw.get("title") or (raw.get("recording") or {}).get("title") or f"Track {toc_track.number:02d}"
            artist = _artist_credit(raw.get("artist-credit")) or _artist_credit(release.get("artist-credit"))
            length = raw.get("length") or (raw.get("recording") or {}).get("length")
            tracks.append(
                TrackMetadata(
                    number=toc_track.number,
                    title=title,
                    artist=artist,
                    length_ms=int(length) if length else None,
                    recording_id=(raw.get("recording") or {}).get("id", "") or "",
                    release_track_id=raw.get("id", "") or "",
                )
            )
        else:
            tracks.append(TrackMetadata(number=toc_track.number, title=f"Track {toc_track.number:02d}"))

    label_info = (release.get("label-info") or [{}])
    first_label = label_info[0] if label_info else {}
    label = ((first_label or {}).get("label") or {}).get("name", "") or ""
    release_group = release.get("release-group") or {}
    date = release.get("date") or release_group.get("first-release-date") or ""

    warnings: list[str] = []
    if mb_tracks and len(mb_tracks) != len(audio_tracks):
        warnings.append(
            f"MusicBrainz lists {len(mb_tracks)} track(s) but the disc has {len(audio_tracks)} audio track(s)"
        )

    return AlbumMetadata(
        source=source,
        confidence=confidence,
        disc_id=disc_id or (musicbrainz_disc_id(toc) or ""),
        toc_string=musicbrainz_toc_string(toc) or "",
        release_id=release.get("id", "") or "",
        release_group_id=release_group.get("id", "") or "",
        title=release.get("title", "") or release_group.get("title", "") or "Unknown Album",
        album_artist=_artist_credit(release.get("artist-credit")) or "Unknown Artist",
        year=(date or "")[:4],
        date=date or "",
        genre=_primary_genre(release),
        label=label,
        catalog_number=(first_label or {}).get("catalog-number", "") or "",
        barcode=release.get("barcode", "") or "",
        country=release.get("country", "") or "",
        disc=int(media.get("position") or 1),
        disc_total=len(release.get("media") or []) or 1,
        total_tracks=len(tracks),
        cover_url=f"https://coverartarchive.org/release/{release.get('id')}/front-500" if release.get("id") else "",
        tracks=tracks,
        warnings=warnings,
        fetched_at=time.time(),
    )


def pick_best_release(releases: Iterable[dict[str, Any]], track_count: int) -> dict[str, Any] | None:
    """Choose the most plausible release out of a MusicBrainz result list."""
    candidates = [r for r in releases if r]
    if not candidates:
        return None

    def score(release: dict[str, Any]) -> tuple[int, int, int, int]:
        media = release.get("media") or []
        exact = any((m.get("track-count") or len(m.get("tracks") or [])) == track_count for m in media)
        official = 1 if (release.get("status") or "").lower() in {"official", ""} else 0
        has_date = 1 if release.get("date") else 0
        has_cover = 1 if release.get("cover-art-archive", {}).get("front") else 0
        return (1 if exact else 0, official, has_cover, has_date)

    return sorted(candidates, key=score, reverse=True)[0]


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


class MusicBrainzClient:
    def __init__(self, cache_dir: str | Path | None = None, contact: str = "", cache_ttl: int = 30 * 24 * 3600):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.contact = contact
        self.cache_ttl = cache_ttl
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.cache_dir = None

    # -- cache ------------------------------------------------------------- #

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(key))
        safe = re.sub(r"\.{2,}", "_", safe).strip(".")[:80]
        return self.cache_dir / f"{safe or 'cache'}.json"

    def _cache_read(self, key: str) -> dict[str, Any] | None:
        path = self._cache_path(key)
        if not path or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if self.cache_ttl and time.time() - float(data.get("fetched_at") or 0) > self.cache_ttl:
            return None
        return data

    def _cache_write(self, key: str, data: dict[str, Any]) -> None:
        path = self._cache_path(key)
        if not path:
            return
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    # -- lookups ----------------------------------------------------------- #

    def lookup_disc_id(self, disc_id: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"inc": "artists+artist-credits+recordings+release-groups+labels+genres", "fmt": "json"}
        )
        url = f"{MB_BASE}/discid/{urllib.parse.quote(disc_id)}?{query}"
        data = http_get_json(url, contact=self.contact)
        if not isinstance(data, dict):
            return []
        return data.get("releases") or []

    def lookup_toc(self, toc_string: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "toc": toc_string,
                "inc": "artists+artist-credits+recordings+release-groups+labels+genres",
                "fmt": "json",
            }
        )
        url = f"{MB_BASE}/discid/-?{query}"
        data = http_get_json(url, contact=self.contact)
        if not isinstance(data, dict):
            return []
        return data.get("releases") or []

    def lookup_barcode(self, barcode: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"query": f"barcode:{barcode}", "fmt": "json", "limit": 8})
        url = f"{MB_BASE}/release/?{query}"
        data = http_get_json(url, contact=self.contact)
        if not isinstance(data, dict):
            return []
        return data.get("releases") or []

    def lookup_release(self, release_id: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {"inc": "artists+artist-credits+recordings+release-groups+labels+genres", "fmt": "json"}
        )
        url = f"{MB_BASE}/release/{urllib.parse.quote(release_id)}?{query}"
        data = http_get_json(url, contact=self.contact)
        return data if isinstance(data, dict) else None

    # -- high level -------------------------------------------------------- #

    def resolve(self, toc: Toc, *, mcn: str = "", use_cache: bool = True) -> AlbumMetadata:
        """Disc ID -> fuzzy TOC -> barcode. Returns the first usable match."""
        track_count = len(toc.audio_tracks or toc.tracks)
        primary_id = musicbrainz_disc_id(toc) or ""
        errors: list[str] = []

        def lookup(stage, *args):
            """Call a lookup helper; any failure is reported, never raised."""
            try:
                return stage(*args)
            except MetadataError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - network/parse surprises
                errors.append(f"metadata lookup failed: {exc}")
            return []

        for label, candidate in toc_candidates(toc):
            disc_id = musicbrainz_disc_id(candidate) or ""
            cache_key = f"discid-{disc_id}" if disc_id else ""
            if use_cache and cache_key:
                cached = self._cache_read(cache_key)
                if cached:
                    album = AlbumMetadata.from_dict(cached)
                    album.warnings.append("served from local cache")
                    return album

            releases: list[dict[str, Any]] = []
            if disc_id:
                releases = lookup(self.lookup_disc_id, disc_id)
            if not releases and label == "full":
                toc_string = musicbrainz_toc_string(candidate)
                if toc_string:
                    releases = lookup(self.lookup_toc, toc_string)
            if releases:
                best = pick_best_release(releases, track_count)
                if best:
                    confidence = 1.0 if (best.get("id") and label == "full") else 0.7
                    album = parse_release(best, toc, confidence=confidence, disc_id=disc_id)
                    if len(releases) > 1:
                        album.warnings.append(f"{len(releases)} releases matched - picked the closest one")
                    album.warnings.append(f"matched by {label} TOC ({disc_id})")
                    if cache_key:
                        self._cache_write(cache_key, album.to_dict())
                    return album

        if mcn:
            releases = lookup(self.lookup_barcode, mcn)
            if releases:
                best = pick_best_release(releases, track_count)
                if best:
                    album = parse_release(best, toc, confidence=0.5, disc_id=primary_id)
                    album.warnings.append("matched by disc barcode (MCN)")
                    return album

        reason = "no MusicBrainz release found for this disc"
        if errors:
            reason = f"{reason} ({errors[0]})"
        return unknown_album(toc, disc_id=primary_id, reason=reason)


# --------------------------------------------------------------------------- #
# Resolver with a background worker + cache
# --------------------------------------------------------------------------- #


class MetadataResolver:
    """Resolves disc metadata off the request path.

    The scanner schedules a lookup when a new disc shows up; the API reads the
    cache and the ripper can block on a synchronous resolve.
    """

    def __init__(self, client: MusicBrainzClient, on_update=None):
        self.client = client
        self.on_update = on_update
        self._cache: dict[str, AlbumMetadata] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._queue: list[tuple[str, Toc, str, bool]] = []
        self._queue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metadata", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -- public API -------------------------------------------------------- #

    def get(self, signature: str) -> AlbumMetadata | None:
        with self._lock:
            return self._cache.get(signature)

    def set(self, signature: str, album: AlbumMetadata) -> None:
        with self._lock:
            self._cache[signature] = album

    def schedule(self, signature: str, toc: Toc, mcn: str = "", *, refresh: bool = False) -> None:
        """Queue a lookup (no-op when one is already running for that disc)."""
        if not signature:
            return
        with self._lock:
            if signature in self._inflight:
                return
            if self._cache.get(signature) and not refresh:
                return
            self._inflight[signature] = threading.Event()
        with self._queue_lock:
            self._queue.append((signature, toc, mcn, refresh))
        self._wake.set()

    def wait(self, signature: str, timeout: float = 0.0) -> AlbumMetadata | None:
        with self._lock:
            event = self._inflight.get(signature)
        if event is not None and timeout:
            event.wait(timeout)
        return self.get(signature)

    def resolve_now(self, signature: str, toc: Toc, mcn: str = "") -> AlbumMetadata:
        """Blocking lookup, used by the ripper."""
        cached = self.get(signature)
        if cached is not None and cached.source != "none":
            return cached
        album = self.client.resolve(toc, mcn=mcn)
        self.set(signature, album)
        return album

    def forget(self, signature: str) -> None:
        with self._lock:
            self._cache.pop(signature, None)

    # -- worker ------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=5)
            self._wake.clear()
            while True:
                with self._queue_lock:
                    if not self._queue:
                        break
                    signature, toc, mcn, _refresh = self._queue.pop(0)
                try:
                    album = self.client.resolve(toc, mcn=mcn)
                    self.set(signature, album)
                    if self.on_update:
                        self.on_update(signature, album)
                except Exception as exc:  # noqa: BLE001 - never kill the worker
                    log.warning("metadata lookup failed: %s", exc)
                finally:
                    with self._lock:
                        event = self._inflight.pop(signature, None)
                    if event is not None:
                        event.set()
            if self._stop.is_set():
                break
