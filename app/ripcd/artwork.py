"""Cover art: fetch from the Cover Art Archive, cache it locally."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .metadata import http_get_bytes

log = logging.getLogger("ripcd.artwork")

CAA_BASE = "https://coverartarchive.org"
SIZES = ("500", "250", "1200", "front")

_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF8": "gif",
    b"RIFF": "webp",
}


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def sniff_extension(data: bytes) -> str:
    for magic, ext in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return ext
    return "jpg"


class CoverArtClient:
    """Downloads and caches front covers."""

    def __init__(self, cache_dir: str | os.PathLike[str] | None = None, contact: str = ""):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.contact = contact
        if self.cache_dir:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.cache_dir = None

    def cached_path(self, release_id: str, size: str = "500") -> str:
        if not self.cache_dir or not release_id:
            return ""
        for ext in ("jpg", "png", "gif", "webp"):
            candidate = self.cache_dir / f"{release_id}-{size}.{ext}"
            if candidate.is_file() and candidate.stat().st_size > 512:
                return str(candidate)
        return ""

    def fetch(self, release_id: str, *, size: str = "500", release_group_id: str = "") -> str:
        """Return the path of a cached cover, downloading it if needed."""
        if not release_id and not release_group_id:
            return ""
        key = release_id or f"rg-{release_group_id}"
        existing = self.cached_path(key, size)
        if existing:
            return existing

        urls: list[str] = []
        if release_id:
            urls.append(f"{CAA_BASE}/release/{release_id}/front-{size}")
        if release_group_id:
            urls.append(f"{CAA_BASE}/release-group/{release_group_id}/front-{size}")
        if release_id:
            urls.append(f"{CAA_BASE}/release/{release_id}/front")

        for url in urls:
            result = http_get_bytes(url, contact=self.contact)
            if not result:
                continue
            data, _content_type = result
            if not _looks_like_image(data):
                continue
            if not self.cache_dir:
                continue
            path = self.cache_dir / f"{key}-{size}.{sniff_extension(data)}"
            try:
                path.write_bytes(data)
                log.info("cover art saved to %s", path)
                return str(path)
            except OSError as exc:
                log.warning("cannot cache cover art: %s", exc)
                return ""
        return ""
