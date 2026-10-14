"""Discovery and selection of the drive the app works with."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import SettingsStore
from .mock import MOCK_STATE, MockOpticalDrive
from .optical import OpticalDrive, discover_drives

log = logging.getLogger("ripcd.drives")


class DriveProvider:
    """Keeps the list of optical drives fresh and picks the active one."""

    def __init__(self, settings_store: SettingsStore, *, mock: bool = False, rescan_interval: float = 30.0):
        self.settings_store = settings_store
        self.mock = mock
        self.rescan_interval = rescan_interval
        self._drives: list[OpticalDrive] = []
        self._lock = threading.RLock()
        self._last_scan = 0.0

    # -- listing ------------------------------------------------------------ #

    def refresh(self, *, force: bool = False) -> list[OpticalDrive]:
        with self._lock:
            age = time.monotonic() - self._last_scan
            if not force and self._drives and age < self.rescan_interval:
                return list(self._drives)
            if self.mock:
                self._drives = [MockOpticalDrive(MOCK_STATE)]
            else:
                self._drives = discover_drives()
            self._last_scan = time.monotonic()
            if self._drives:
                log.debug("drives: %s", ", ".join(d.device for d in self._drives))
            return list(self._drives)

    def list(self, *, force: bool = False) -> list[OpticalDrive]:
        return self.refresh(force=force)

    def devices(self) -> list[str]:
        return [drive.device for drive in self.refresh()]

    # -- selection ---------------------------------------------------------- #

    def get(self, device: str | None = None) -> OpticalDrive | None:
        """Return the requested drive (or the configured/default one)."""
        drives = self.refresh()
        wanted = device or self.settings_store.settings.device or ""
        if wanted:
            for drive in drives:
                if drive.device == wanted or drive.name == wanted:
                    return drive
            if self.mock:
                return drives[0] if drives else None
            # configured device is not there right now (unplugged?)
            return None
        with_disc = [d for d in drives if _safe_has_disc(d)]
        if with_disc:
            return with_disc[0]
        return drives[0] if drives else None

    def primary_device(self) -> str:
        drive = self.get()
        return drive.device if drive else ""

    # -- convenience -------------------------------------------------------- #

    def probe_all(self, *, toc: bool = False) -> list[dict[str, Any]]:
        result = []
        for drive in self.refresh(force=toc):
            try:
                result.append(drive.probe())
            except Exception as exc:  # noqa: BLE001 - a broken drive must not break the API
                result.append({"device": drive.device, "available": False, "error": str(exc), "has_disc": False})
        return result


def _safe_has_disc(drive: OpticalDrive) -> bool:
    try:
        return drive.has_disc()
    except Exception:  # noqa: BLE001
        return False
