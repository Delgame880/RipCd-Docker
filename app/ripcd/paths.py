"""Filesystem helpers: path sandboxing, the folder browser and file naming."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Characters that are illegal on Windows/SMB shares (ZimaOS shares are often
# mounted from Windows or read by one) plus control characters.
_ILLEGAL = r'<>:"/\\|?*\x00-\x1f'
_ILLEGAL_RE = re.compile(f"[{_ILLEGAL}]")
# Trailing dots/spaces break Windows shares.
_TRAILING_RE = re.compile(r"[. ]+$")
_WHITESPACE_RE = re.compile(r"\s+")

#: Names that must never be used as a file name.
RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_component(value: str, *, replacement: str = "_", max_length: int = 120) -> str:
    """Make ``value`` safe to use as a single path component.

    Whitespace (including tabs/newlines) is collapsed first, then characters
    that are illegal on Windows/SMB shares are replaced.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = _WHITESPACE_RE.sub(" ", text)
    text = _ILLEGAL_RE.sub(replacement, text)
    text = _TRAILING_RE.sub("", text)
    text = text.strip()
    if not text or not text.strip(replacement + " "):
        # nothing but separators/padding left
        text = "Unknown"
    if text.lower() in RESERVED:
        text += "_"
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or "Unknown"


def sanitize_relative_path(relative: str) -> str:
    """Sanitize a user supplied *relative* path (from a naming template)."""
    parts: list[str] = []
    for raw in str(relative or "").replace("\\", "/").split("/"):
        if raw in {"", ".", ".."}:
            continue
        parts.append(sanitize_component(raw))
    return "/".join(parts) or "Unknown"


def safe_filename(name: str, extension: str = "") -> str:
    """Sanitize a file name, guaranteeing the requested extension."""
    base = sanitize_component(Path(name).name if name else "track")
    if extension:
        ext = extension.lstrip(".")
        if base.lower().endswith("." + ext.lower()):
            return base
        base = sanitize_component(Path(base).stem)
        return f"{base}.{ext}"
    return base


# --------------------------------------------------------------------------- #
# Naming templates
# --------------------------------------------------------------------------- #

_ALLOWED_FIELDS = {
    "album", "album_artist", "artist", "title", "year", "genre", "track",
    "track_raw", "disc", "disc_total", "total_tracks", "format", "label",
    "catalog_number", "disc_id", "track_artist",
}


class TemplateError(ValueError):
    """Raised when a naming template cannot be rendered."""


def validate_template(template: str) -> None:
    """Raise :class:`TemplateError` when a template uses unknown fields."""
    if not template or not template.strip():
        raise TemplateError("template must not be empty")
    for _, field_name, _, _ in _parse_template(template):
        if field_name and field_name not in _ALLOWED_FIELDS:
            raise TemplateError(
                f"unknown field '{field_name}' - allowed: {', '.join(sorted(_ALLOWED_FIELDS))}"
            )


def _parse_template(template: str) -> Iterable[tuple[str, str, str, str]]:
    """Split a template into (literal, field, format_spec, conversion) parts."""
    for literal, field_name, format_spec, conversion in _FORMATTER.parse(template):
        yield literal, field_name, format_spec, conversion


# string.Formatter with a permissive attribute pattern ("track:02d" has no
# attribute access, but "track.upper" would - we do not allow attributes).
import string  # noqa: E402  (kept next to its use for readability)

_FORMATTER = string.Formatter()


def render_template(template: str, context: dict[str, Any], *, fallback: str = "Unknown") -> str:
    """Render a naming template against ``context``.

    Unknown/empty values render as ``fallback`` so the result is always usable.
    """
    values = {key: (context.get(key) if context.get(key) not in (None, "") else fallback)
              for key in _ALLOWED_FIELDS}
    values["track"] = context.get("track") or 1
    values["disc"] = context.get("disc") or 1
    try:
        rendered = template.format_map(_SafeDict(values))
    except (KeyError, ValueError, IndexError, TypeError, AttributeError) as exc:
        raise TemplateError(f"cannot render template '{template}': {exc}") from exc
    return rendered.replace("//", "/").strip("/")


class _SafeDict(dict):
    """A dict that answers missing keys with 'Unknown' instead of raising."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "Unknown"


# --------------------------------------------------------------------------- #
# Path sandbox
# --------------------------------------------------------------------------- #


class PathNotAllowed(PermissionError):
    """Raised when a path escapes the configured media roots."""


@dataclass
class PathCheck:
    ok: bool
    path: str
    error: str = ""
    exists: bool = False
    writable: bool = False
    free_bytes: int | None = None
    total_bytes: int | None = None
    filesystem: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "error": self.error,
            "exists": self.exists,
            "writable": self.writable,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "filesystem": self.filesystem,
        }


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def is_within(path: str, roots: Iterable[str]) -> bool:
    """True when ``path`` is one of ``roots`` or lives inside one of them."""
    target = _real(path)
    for root in roots:
        root_real = _real(root)
        if target == root_real or target.startswith(root_real.rstrip("/") + "/"):
            return True
    return False


def container_user() -> str:
    """``uid 1000, gid 1000`` - who the app runs as inside the container."""
    try:
        uid, gid = os.getuid(), os.getgid()
    except AttributeError:  # pragma: no cover - not POSIX
        return "unknown user"
    return f"uid {uid}, gid {gid}"


def deepest_existing(path: str) -> str:
    """The closest existing ancestor of ``path`` (or ``path`` itself)."""
    candidate = _real(path)
    while candidate and not os.path.isdir(candidate):
        parent = os.path.dirname(candidate.rstrip("/")) or "/"
        if parent == candidate:
            break
        candidate = parent
    return candidate or "/"


def check_path(path: str, roots: Iterable[str], *, must_exist: bool = False) -> PathCheck:
    """Validate a path against the allowed roots and check permissions/space.

    A path that does not exist yet is fine as long as the closest existing
    ancestor is writable - that is what makes ``Album Artist/Album`` subfolders
    work on the first rip.
    """
    candidate = _real(path)
    if not is_within(candidate, roots):
        return PathCheck(
            ok=False,
            path=candidate,
            error=(
                "path is outside the folders this container is allowed to write to. "
                "Mount the folder into the container and add it to RIPCD_MEDIA_ROOTS."
            ),
        )
    exists = os.path.isdir(candidate)
    check = PathCheck(ok=exists, path=candidate, exists=exists)
    working = candidate if exists else deepest_existing(candidate)
    writable = os.access(working, os.W_OK)
    check.writable = writable
    check.filesystem = _filesystem(working)[0]
    _fill_space(check, working)
    if not exists:
        check.ok = writable
        if not writable:
            check.error = (
                f"'{working}' is not writable by the container user (running as {container_user()}) - "
                "set PUID/PGID to the owner of the share or chown it"
            )
        elif must_exist:
            check.ok = False
            check.error = "folder does not exist"
    elif not writable:
        check.ok = False
        check.error = (
            f"'{working}' exists but is not writable by the container user "
            f"(running as {container_user()}) - set PUID/PGID to the owner of the share or chown it"
        )
    return check


def _filesystem(path: str) -> tuple[str, str]:
    """Best effort filesystem type + mount point for a path."""
    try:
        path = _real(path)
        best: tuple[str, str] = ("unknown", "/")
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                if path == mount or path.startswith(mount.rstrip("/") + "/"):
                    if len(mount) >= len(best[1]):
                        best = (parts[2], mount)
        return best
    except OSError:
        return ("unknown", "/")


def _fill_space(check: PathCheck, path: str) -> None:
    try:
        stat = os.statvfs(path)
        check.free_bytes = stat.f_bavail * stat.f_frsize
        check.total_bytes = stat.f_blocks * stat.f_frsize
        check.filesystem = _filesystem(path)[0]
    except OSError:
        pass


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


# --------------------------------------------------------------------------- #
# Folder browser
# --------------------------------------------------------------------------- #


def list_directory(path: str, roots: Iterable[str], *, show_files: bool = False) -> dict[str, Any]:
    """List a directory for the UI folder picker.

    ``path`` must live inside one of ``roots``; otherwise a
    :class:`PathNotAllowed` is raised.
    """
    roots = [_real(r) for r in roots]
    target = _real(path)
    if not is_within(target, roots):
        raise PathNotAllowed(f"{target} is not inside {', '.join(roots)}")

    if not os.path.isdir(target):
        raise FileNotFoundError(f"{target} is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        names = sorted(os.scandir(target), key=lambda e: e.name.lower())
    except PermissionError as exc:
        raise PermissionError(f"cannot read {target}: {exc.strerror or 'permission denied'}") from exc

    for entry in names:
        if entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
            if not is_dir and not show_files:
                continue
            stat = entry.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "path": os.path.join(target, entry.name),
                "is_dir": is_dir,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            }
        )

    check = check_path(target, roots)
    # Which of the roots is this directory under? Useful for the UI breadcrumb.
    root = next((r for r in sorted(roots, key=len, reverse=True)
                 if target == r or target.startswith(r.rstrip("/") + "/")), roots[0])
    parent = os.path.dirname(target)
    return {
        "path": target,
        "root": root,
        "roots": roots,
        "parent": parent if is_within(parent, roots) else None,
        "breadcrumbs": _breadcrumbs(target, root),
        "entries": entries,
        "writable": check.writable,
        "free_bytes": check.free_bytes,
        "total_bytes": check.total_bytes,
        "filesystem": check.filesystem,
    }


def _breadcrumbs(path: str, root: str) -> list[dict[str, str]]:
    """Crumbs from the allowed root down to ``path`` (all paths absolute)."""
    root = root.rstrip("/") or "/"
    relative = path[len(root):] if path.startswith(root) else path
    crumbs: list[dict[str, str]] = [{"name": os.path.basename(root) or root, "path": root}]
    current = root
    for part in [p for p in relative.split("/") if p]:
        current = f"{current.rstrip('/')}/{part}"
        crumbs.append({"name": part, "path": current})
    return crumbs
