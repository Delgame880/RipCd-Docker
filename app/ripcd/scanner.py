"""Automatic disc detection.

A single background thread polls the drive, notices insert/eject, resolves
metadata in the background and (optionally) starts ripping by itself.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .config import SettingsStore
from .drives import DriveProvider
from .events import EventBus
from .metadata import AlbumMetadata, MetadataResolver
from .manager import JobManager
from .optical import Toc, device_diagnosis, disc_signature
from .ripper import RipJob, make_job

log = logging.getLogger("ripcd.scanner")


class DiscScanner:
    def __init__(
        self,
        drives: DriveProvider,
        settings_store: SettingsStore,
        resolver: MetadataResolver,
        manager: JobManager,
        bus: EventBus,
    ):
        self.drives = drives
        self.settings_store = settings_store
        self.resolver = resolver
        self.manager = manager
        self.bus = bus

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._auto_pending: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self, *, delay: float = 0.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(delay,), name="disc-scanner", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def request_refresh(self) -> None:
        """Wake the scanner up right now (e.g. metadata has arrived)."""
        self._wake.set()

    # -- state -------------------------------------------------------------- #

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "device": "",
            "device_label": "",
            "mock": False,
            "status": "unknown",
            "has_disc": False,
            "kind": "none",
            "toc": {},
            "tracks": [],
            "disc_id": "",
            "signature": "",
            "metadata": {},
            "metadata_pending": False,
            "inserted_at": 0.0,
            "auto_rip": {"enabled": False, "armed": False, "seconds_left": 0, "skipped": ""},
            "error": "",
            "hint": "",
            "updated_at": time.time(),
        }

    def current(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def refresh(self, *, wait_for_metadata: float = 0.0) -> dict[str, Any]:
        """Re-read the disc right now (used by the 'rescan' button)."""
        state = self._scan_once(force_toc=True)
        if wait_for_metadata and state.get("metadata_pending"):
            album = self.resolver.wait(state["signature"], timeout=wait_for_metadata)
            if album is not None:
                state = self._scan_once(force_toc=False)
        return state

    # -- loop --------------------------------------------------------------- #

    def _run(self, delay: float) -> None:
        if delay:
            time.sleep(delay)
        while not self._stop.is_set():
            interval = max(2, int(self.settings_store.settings.poll_interval or 5))
            if self.current().get("metadata_pending"):
                interval = min(interval, 1)
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001 - the scanner must never die
                log.warning("scan failed: %s", exc)
            self._wake.wait(timeout=interval)
            self._wake.clear()

    # -- one poll ----------------------------------------------------------- #

    def _scan_once(self, *, force_toc: bool = False) -> dict[str, Any]:
        settings = self.settings_store.settings
        previous = self.current()
        drive = self.drives.get()
        now = time.time()

        if drive is None:
            diagnosis = device_diagnosis()
            state = self._empty_state()
            state.update(
                {
                    "status": "no_drive",
                    "error": diagnosis.get("problem") or "no optical drive found",
                    "hint": diagnosis.get("hint") or "",
                    "updated_at": now,
                }
            )
            self._commit(previous, state, event="drives")
            return state

        try:
            info = drive.info()
            status = drive.status()
        except Exception as exc:  # noqa: BLE001
            state = self._empty_state()
            state.update({"device": drive.device, "status": "error", "error": str(exc), "updated_at": now})
            self._commit(previous, state, event="drives")
            return state

        state = self._empty_state()
        state.update(
            {
                "device": drive.device,
                "device_label": info.get("label", drive.name),
                "mock": bool(info.get("mock")),
                "status": status,
                "updated_at": now,
            }
        )

        state["auto_rip"]["enabled"] = bool(settings.auto_rip)

        if status in {"tray_open", "no_disc"}:
            self._auto_pending = None
            self._commit(previous, state, event="disc")
            return state

        if status == "permission":
            state.update(
                {
                    "error": drive.last_error or f"no permission to open {drive.device}",
                    "hint": (
                        "The device node exists but the app cannot open it. Run the container as "
                        "root (PUID=0, PGID=0) or pass the group that owns the drive with "
                        "group_add (see Settings -> Diagnostics)."
                    ),
                }
            )
            self._auto_pending = None
            self._commit(previous, state, event="disc")
            return state

        # "disc_ok" is the normal answer, but an inconclusive one is not a
        # reason to give up: the TOC is the ground truth, so try to read it.
        toc: Toc | None = None
        try:
            toc = drive.read_toc(use_cache=not force_toc)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"cannot read the table of contents: {exc}"

        if toc is None or not toc.tracks:
            diagnosis = device_diagnosis(drives=[])
            state.update(
                {
                    "has_disc": False,
                    "kind": "unknown",
                    "error": state.get("error")
                    or drive.last_error
                    or "the disc could not be read (the drive did not report a table of contents)",
                    "hint": diagnosis.get("hint", ""),
                }
            )
            self._auto_pending = None
            self._commit(previous, state, event="disc")
            return state

        signature = disc_signature(toc)
        mcn = ""
        try:
            mcn = drive.mcn() or ""
        except Exception:  # noqa: BLE001
            mcn = ""

        state.update(
            {
                "has_disc": True,
                "kind": "audio" if toc.has_audio and not toc.is_mixed else ("mixed" if toc.is_mixed else "data"),
                "toc": toc.to_dict(),
                "signature": signature,
                "disc_id": self._disc_id(toc),
            }
        )
        state["inserted_at"] = previous.get("inserted_at") if previous.get("signature") == signature else now

        album = self.resolver.get(signature)
        if album is None and settings.metadata_provider != "none":
            self.resolver.schedule(signature, toc, mcn)
            state["metadata_pending"] = True
            album = self.resolver.get(signature)
        if album is not None:
            state["metadata"] = album.to_dict()
            state["metadata_pending"] = False

        state["tracks"] = self._merge_tracks(toc, album)
        state["auto_rip"] = self._auto_rip_info(settings, state, previous, toc, signature)

        self._commit(previous, state, event="disc")
        return state

    @staticmethod
    def _disc_id(toc: Toc) -> str:
        from .optical import musicbrainz_disc_id

        return musicbrainz_disc_id(toc) or ""

    @staticmethod
    def _merge_tracks(toc: Toc, album: AlbumMetadata | None) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        for track in toc.tracks:
            meta = album.track(track.number) if album else None
            entry = track.to_dict()
            entry.update(
                {
                    "title": (meta.title if meta else "") or f"Track {track.number:02d}",
                    "artist": (meta.artist if meta else "") or (album.album_artist if album else ""),
                    "known": bool(meta),
                }
            )
            tracks.append(entry)
        return tracks

    def _auto_rip_info(
        self,
        settings,
        state: dict[str, Any],
        previous: dict[str, Any],
        toc: Toc,
        signature: str,
    ) -> dict[str, Any]:
        info: dict[str, Any] = {"enabled": bool(settings.auto_rip), "armed": False, "seconds_left": 0, "skipped": ""}
        if not settings.auto_rip:
            return info
        if not toc.has_audio or state.get("kind") == "data":
            info["skipped"] = "not an audio CD"
            return info
        if self.manager.has_active_for(signature):
            info["skipped"] = "already ripping this disc"
            return info
        if self.manager.already_ripped(signature):
            info["skipped"] = "this disc was already ripped"
            return info
        if state.get("metadata_pending") and not settings.overrides:
            # Give the metadata lookup a chance to finish before starting.
            pass

        if self._auto_pending is None or self._auto_pending.get("signature") != signature:
            self._auto_pending = {"signature": signature, "due_at": time.time() + max(0, settings.auto_rip_delay)}

        due_at = self._auto_pending["due_at"]
        remaining = max(0.0, due_at - time.time())
        info.update({"armed": True, "seconds_left": int(round(remaining))})

        if remaining <= 0:
            self._auto_pending = None
            if self._start_auto_rip(settings, signature):
                info.update({"armed": False, "skipped": "auto rip started", "started": True})
        return info

    def _start_auto_rip(self, settings, signature: str) -> bool:
        payload = {
            "device": self.drives.primary_device(),
            "output_dir": settings.output_dir,
            "format": settings.format,
            "quality": settings.quality,
            "overrides": settings.overrides,
            "metadata_provider": settings.metadata_provider,
        }
        try:
            job: RipJob = make_job(payload, settings, auto=True)
            job.disc_signature = signature
            job.add_log("started automatically (auto-rip is enabled)")
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-rip could not start: %s", exc)
            self.bus.publish(
                "notification",
                {"level": "error", "title": "Auto-rip failed to start", "message": str(exc)},
            )
            return False
        self.manager.submit(job)
        self.bus.publish(
            "notification",
            {
                "level": "info",
                "title": "Auto-rip started",
                "message": f"job {job.id} - {settings.format.upper()} into {settings.output_dir}",
                "job": job.id,
            },
        )
        return True

    # -- change detection --------------------------------------------------- #

    def _commit(self, previous: dict[str, Any], state: dict[str, Any], *, event: str) -> None:
        with self._lock:
            self._state = state

        changed = (
            previous.get("signature") != state.get("signature")
            or previous.get("has_disc") != state.get("has_disc")
            or previous.get("device") != state.get("device")
            or previous.get("status") != state.get("status")
            or previous.get("metadata_pending") != state.get("metadata_pending")
            or bool(state.get("metadata")) != bool(previous.get("metadata"))
            or previous.get("auto_rip") != state.get("auto_rip")
        )
        if not changed:
            return

        # Log insert/eject so the activity feed shows something useful.
        if state["has_disc"] and not previous.get("has_disc"):
            label = state.get("metadata", {}).get("title") or "unknown album"
            log.info("disc inserted in %s (%s)", state["device"], label)
            self.bus.publish(
                "notification",
                {
                    "level": "info",
                    "title": "Disc inserted",
                    "message": f"{state.get('metadata', {}).get('title') or 'New disc'} in {state.get('device')}",
                },
            )
        elif previous.get("has_disc") and not state["has_disc"]:
            log.info("disc removed from %s", state.get("device") or previous.get("device"))
            self.bus.publish(
                "notification",
                {"level": "info", "title": "Disc removed", "message": f"{previous.get('device', 'drive')} is empty"},
            )

        self.bus.publish(event, state)
        self.bus.publish("disc", state)


def metadata_notify_callback(bus: EventBus, scanner: "DiscScanner | None" = None) -> Callable[[str, AlbumMetadata], None]:
    """Called by the metadata worker when a lookup finishes."""

    def _callback(signature: str, album: AlbumMetadata) -> None:
        log.info("metadata for %s: %s - %s", signature, album.album_artist, album.title)
        bus.publish("metadata", {"signature": signature, "metadata": album.to_dict()})
        if scanner is not None:
            # refresh the disc snapshot so the UI shows the album right away
            scanner.request_refresh()

    return _callback
