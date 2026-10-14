"""Environment introspection: external tools, disk space, system info."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .paths import human_bytes

TOOLS = {
    "cdparanoia": {"purpose": "rips audio tracks from the CD (required)", "required": True},
    "ffmpeg": {"purpose": "encodes the ripped audio and writes tags/covers (required)", "required": True},
    "flac": {"purpose": "optional FLAC encoder used for verification", "required": False},
    "lame": {"purpose": "optional MP3 encoder (a fallback for ffmpeg)", "required": False},
    "oggenc": {"purpose": "optional Ogg Vorbis encoder", "required": False},
    "opusenc": {"purpose": "optional Opus encoder", "required": False},
    "eject": {"purpose": "optional, lets the container open the tray via the eject(1) fallback", "required": False},
    "blkid": {"purpose": "optional, used for diagnostics only", "required": False},
}


@dataclass
class ToolStatus:
    name: str
    available: bool
    path: str = ""
    version: str = ""
    purpose: str = ""
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "path": self.path,
            "version": self.version,
            "purpose": self.purpose,
            "required": self.required,
        }


def _version(binary: str, path: str) -> str:
    args = {
        "ffmpeg": ["-version"],
        "cdparanoia": ["-V"],
        "flac": ["--version"],
        "lame": ["--version"],
        "oggenc": ["--version"],
        "opusenc": ["--version"],
    }.get(binary)
    if not args:
        return ""
    try:
        proc = subprocess.run([path, *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (proc.stdout or "") + (proc.stderr or "")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:120]


def probe_tool(name: str, *, check_version: bool = True) -> ToolStatus:
    path = shutil.which(name) or ""
    meta = TOOLS.get(name, {})
    if not path:
        return ToolStatus(name=name, available=False, purpose=meta.get("purpose", ""), required=meta.get("required", False))
    return ToolStatus(
        name=name,
        available=True,
        path=path,
        version=_version(name, path) if check_version else "",
        purpose=meta.get("purpose", ""),
        required=meta.get("required", False),
    )


def probe_tools(*, check_version: bool = True) -> dict[str, ToolStatus]:
    return {name: probe_tool(name, check_version=check_version) for name in TOOLS}


def missing_required(tools: dict[str, ToolStatus] | None = None) -> list[str]:
    tools = tools or probe_tools(check_version=False)
    return [name for name, status in tools.items() if status.required and not status.available]


def disk_usage(path: str) -> dict[str, Any]:
    try:
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        return {
            "free_bytes": free,
            "total_bytes": total,
            "free": human_bytes(free),
            "total": human_bytes(total),
            "used_percent": round(100 * (1 - (stat.f_bavail / stat.f_blocks)), 1) if stat.f_blocks else 0.0,
        }
    except OSError as exc:
        return {"free_bytes": None, "total_bytes": None, "free": "unknown", "total": "unknown",
                "used_percent": None, "error": str(exc)}


def memory_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    kb = int(value.strip().split()[0])
                    info[key] = {"bytes": kb * 1024, "human": human_bytes(kb * 1024)}
    except (OSError, ValueError):
        pass
    return info


def system_info(*, tools: dict[str, ToolStatus] | None = None) -> dict[str, Any]:
    tools = tools or probe_tools()
    from . import APP_NAME, __version__

    return {
        "app": APP_NAME,
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "memory": memory_info(),
        "tools": {name: status.to_dict() for name, status in tools.items()},
        "missing_required_tools": missing_required(tools),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "gid": os.getgid() if hasattr(os, "getgid") else None,
    }
