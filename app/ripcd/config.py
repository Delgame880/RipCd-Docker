"""Configuration handling.

Two layers:

1. Environment variables (``RIPCD_*``) provide the boot defaults, so a
   container can be fully configured from ``docker-compose.yml``.
2. ``/config/settings.json`` holds whatever the user changes in the web UI.
   Values stored there win over the environment, so UI changes survive
   container restarts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

#: Container images / output formats supported by the encoder stage.
FORMATS: dict[str, dict[str, Any]] = {
    "flac": {
        "label": "FLAC (lossless)",
        "ext": "flac",
        "kind": "lossless",
        "qualities": {"8": "Compression 8 (best)", "5": "Compression 5 (faster)", "0": "Compression 0 (fastest)"},
        "default_quality": "8",
    },
    "alac": {
        "label": "ALAC / Apple Lossless",
        "ext": "m4a",
        "kind": "lossless",
        "qualities": {"": "Default"},
        "default_quality": "",
    },
    "wav": {
        "label": "WAV (uncompressed)",
        "ext": "wav",
        "kind": "lossless",
        "qualities": {"": "Default"},
        "default_quality": "",
    },
    "mp3": {
        "label": "MP3",
        "ext": "mp3",
        "kind": "lossy",
        "qualities": {
            "v0": "V0 VBR (~245 kbps, best)",
            "v2": "V2 VBR (~190 kbps, balanced)",
            "320": "320 kbps CBR",
            "192": "192 kbps CBR",
        },
        "default_quality": "v0",
    },
    "ogg": {
        "label": "Ogg Vorbis",
        "ext": "ogg",
        "kind": "lossy",
        "qualities": {"q8": "Quality 8 (~256 kbps)", "q6": "Quality 6 (~192 kbps)", "q4": "Quality 4 (~128 kbps)"},
        "default_quality": "q6",
    },
    "opus": {
        "label": "Opus",
        "ext": "opus",
        "kind": "lossy",
        "qualities": {"256": "256 kbps", "192": "192 kbps", "128": "128 kbps"},
        "default_quality": "192",
    },
    "aac": {
        "label": "AAC (M4A)",
        "ext": "m4a",
        "kind": "lossy",
        "qualities": {"256": "256 kbps", "192": "192 kbps", "128": "128 kbps"},
        "default_quality": "256",
    },
}

PARANOIA_LEVELS = {
    "full": "Maximum (verify + repair, slowest - recommended)",
    "reduced": "Reduced (intra-read verification off)",
    "none": "None (raw read, fastest - for clean discs)",
}

METADATA_PROVIDERS = {
    "musicbrainz": "MusicBrainz / Cover Art Archive",
    "none": "None (fill in manually)",
}

DEFAULT_ALBUM_TEMPLATE = "{album_artist}/{album}"
DEFAULT_TRACK_TEMPLATE = "{track:02d} - {title}"


def _env(name: str, default: str) -> str:
    value = os.environ.get(f"RIPCD_{name}")
    return default if value is None or value == "" else value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"RIPCD_{name}")
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"RIPCD_{name}")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_media_roots() -> list[str]:
    raw = _env("MEDIA_ROOTS", _env("MEDIA_ROOT", "/media"))
    roots = [p.strip().rstrip("/") or "/" for p in raw.split(",") if p.strip()]
    return roots or ["/media"]


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    """All user-tunable options."""

    # Where rips may be written. ``media_roots`` is the sandbox of the file
    # browser; ``output_dir`` is the directory the rips land in.
    media_roots: list[str] = field(default_factory=default_media_roots)
    output_dir: str = field(default_factory=lambda: _env("OUTPUT_DIR", "/media/Music"))
    album_folder_template: str = DEFAULT_ALBUM_TEMPLATE
    track_file_template: str = DEFAULT_TRACK_TEMPLATE

    # Encoding
    format: str = field(default_factory=lambda: _env("FORMAT", "flac"))
    quality: str = ""  # empty -> the format's default quality
    embed_cover: bool = True
    write_m3u: bool = True
    keep_wav: bool = False

    # Drive / scanner
    device: str = ""  # empty -> first disc drive found
    autoscan: bool = True
    poll_interval: int = field(default_factory=lambda: _env_int("POLL_INTERVAL", 5))
    auto_rip: bool = False
    auto_rip_delay: int = 20  # seconds between disc detection and auto start
    eject_after_rip: bool = True
    read_speed: int = 0  # 0 = drive default (maximum)
    paranoia: str = "full"

    # Metadata
    metadata_provider: str = "musicbrainz"
    musicbrainz_contact: str = field(default_factory=lambda: _env("MUSICBRAINZ_CONTACT", ""))
    fetch_cover: bool = True
    overrides: dict[str, str] = field(default_factory=dict)

    # Behaviour
    api_token: str = field(default_factory=lambda: _env("TOKEN", ""))
    job_retention: int = 50  # finished jobs kept in history
    temp_dir: str = field(default_factory=lambda: _env("TEMP_DIR", ""))  # empty -> <output_dir>/.ripcd-tmp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("api_token", None)
        data["api_token_set"] = bool(self.api_token)
        return data

    # -- validation ------------------------------------------------------- #

    def normalized(self) -> "Settings":
        """Return a copy with sane values (never trust the UI)."""
        data = self.to_dict()
        if data["format"] not in FORMATS:
            data["format"] = "flac"
        if data["quality"] not in FORMATS[data["format"]]["qualities"]:
            data["quality"] = FORMATS[data["format"]]["default_quality"]
        if data["paranoia"] not in PARANOIA_LEVELS:
            data["paranoia"] = "full"
        if data["metadata_provider"] not in METADATA_PROVIDERS:
            data["metadata_provider"] = "musicbrainz"
        data["poll_interval"] = max(2, min(120, int(data["poll_interval"] or 5)))
        data["auto_rip_delay"] = max(0, min(600, int(data["auto_rip_delay"] or 0)))
        data["read_speed"] = max(0, min(72, int(data["read_speed"] or 0)))
        data["job_retention"] = max(5, min(1000, int(data["job_retention"] or 50)))
        data["album_folder_template"] = (data["album_folder_template"] or DEFAULT_ALBUM_TEMPLATE).strip()
        data["track_file_template"] = (data["track_file_template"] or DEFAULT_TRACK_TEMPLATE).strip()
        roots = [r.strip().rstrip("/") or "/" for r in (data.get("media_roots") or []) if str(r).strip()]
        data["media_roots"] = roots or default_media_roots()
        if not data["output_dir"]:
            data["output_dir"] = data["media_roots"][0]
        overrides = {k: str(v) for k, v in (data.get("overrides") or {}).items() if str(v).strip()}
        data["overrides"] = overrides
        return Settings(**data)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class SettingsStore:
    """Loads, caches and persists :class:`Settings`."""

    def __init__(self, config_dir: str | os.PathLike[str] | None = None, path: str | os.PathLike[str] | None = None):
        self.config_dir = Path(config_dir or _env("CONFIG_DIR", "/config"))
        self.path = Path(path) if path else self.config_dir / "settings.json"
        self._lock = threading.RLock()
        self._settings = Settings()

    # -- io --------------------------------------------------------------- #

    def load(self) -> Settings:
        with self._lock:
            base = Settings()  # environment-provided defaults
            stored: dict[str, Any] = {}
            try:
                if self.path.is_file():
                    stored = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                allowed = set(Settings().to_dict().keys())
                for key, value in stored.items():
                    if key in allowed:
                        setattr(base, key, value)
            self._settings = base.normalized()
            return self._settings

    def save(self, settings: Settings | None = None) -> Settings:
        with self._lock:
            if settings is not None:
                self._settings = settings.normalized()
            payload = self._settings.to_dict()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                # A read-only /config should not break the app, it just means
                # UI changes are not persisted.
                pass
            return self._settings

    # -- access ------------------------------------------------------------ #

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    def update(self, patch: dict[str, Any]) -> Settings:
        """Apply a partial update from the UI/API and persist it."""
        with self._lock:
            current = self._settings.to_dict()
            allowed = set(current.keys())
            for key, value in patch.items():
                if key in allowed and value is not None:
                    current[key] = value
            updated = Settings(**current).normalized()
            self._settings = updated
            self.save()
            return updated

    def reset(self) -> Settings:
        with self._lock:
            try:
                self.path.unlink()
            except OSError:
                pass
            return self.load()
