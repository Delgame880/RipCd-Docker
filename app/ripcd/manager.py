"""Job queue and rip history."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .events import EventBus
from .ripper import RipJob, RipRunner

log = logging.getLogger("ripcd.manager")

#: Finished jobs kept in the in-memory list / history file.
HISTORY_LIMIT = 200


class JobManager:
    """Runs rip jobs one at a time (there is only one disc drive)."""

    def __init__(
        self,
        runner: RipRunner,
        settings_store,
        bus: EventBus,
        *,
        history_path: str | Path | None = None,
    ):
        self.runner = runner
        self.settings_store = settings_store
        self.bus = bus
        self.history_path = Path(history_path) if history_path else None
        self.jobs: dict[str, RipJob] = {}
        self.order: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_progress_emit = 0.0
        self.history: list[dict[str, Any]] = []
        self._load_history()

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="rip-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- public API --------------------------------------------------------- #

    def submit(self, job: RipJob) -> RipJob:
        with self._lock:
            job.state = "queued"
            job.stage = "waiting"
            job.message = "queued"
            job.add_log("job queued")
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._trim()
        self.bus.publish("job", job.to_dict())
        self.bus.publish("queue", self.queue_state())
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> RipJob | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list(self, *, limit: int = 50, active_first: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self.jobs[job_id] for job_id in self.order if job_id in self.jobs]
        jobs.sort(
            key=lambda job: (
                0 if job.is_active else 1,
                -(job.finished_at or job.started_at or job.created_at),
            )
            if active_first
            else -job.created_at
        )
        return [job.to_dict() for job in jobs[:limit]]

    def active(self) -> RipJob | None:
        with self._lock:
            for job_id in self.order:
                job = self.jobs.get(job_id)
                if job and job.is_active:
                    return job
        return None

    def queue_state(self) -> dict[str, Any]:
        active = self.active()
        waiting = [j.id for j in self._all() if j.state == "queued"]
        return {
            "active": active.to_dict() if active else None,
            "queued": len(waiting),
            "queued_ids": waiting,
        }

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.request_cancel()
        if job.state == "queued":
            job.state = "canceled"
            job.message = "canceled before it started"
            job.finished_at = time.time()
            job.add_log("canceled while queued", "warning")
            self._finished(job)
        else:
            job.message = "canceling..."
            job.add_log("cancel requested by the user", "warning")
        self.bus.publish("job", job.to_dict())
        return True

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job.is_active:
                return False
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)
        self.bus.publish("queue", self.queue_state())
        return True

    def clear_history(self) -> None:
        with self._lock:
            keep = {job.id: job for job in self._all() if job.is_active}
            self.jobs = keep
            self.order = [job_id for job_id in self.order if job_id in keep]
            self.history = []
        self._save_history()
        self.bus.publish("queue", self.queue_state())

    def already_ripped(self, signature: str) -> dict[str, Any] | None:
        """Was a disc with this signature successfully ripped before?"""
        if not signature:
            return None
        for record in self.history:
            if record.get("disc_signature") == signature and record.get("state") == "done":
                return record
        with self._lock:
            for job in self._all():
                if (
                    job.disc_signature == signature
                    and job.state in {"done", "running", "queued"}
                    and job is not None
                ):
                    return job.to_dict()
        return None

    def has_active_for(self, signature: str) -> bool:
        if not signature:
            return False
        with self._lock:
            return any(job.disc_signature == signature and job.is_active for job in self._all())

    # -- worker ------------------------------------------------------------- #

    def _worker(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                continue
            job = self.get(item)
            if job is None or job.state == "canceled":
                continue
            try:
                self.runner.run(job)
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed", item)
                job.state = "error"
                job.error = "internal error - see the container logs"
                job.finished_at = time.time()
            finally:
                self._finished(job)
                self.bus.publish("queue", self.queue_state())
                self._queue.task_done()

    def _finished(self, job: RipJob) -> None:
        self._add_history(job)
        self.bus.publish("job", job.to_dict())
        self.bus.publish("notification", {
            "level": "success" if job.state == "done" else "error",
            "title": {
                "done": "Rip finished",
                "error": "Rip failed",
                "canceled": "Rip canceled",
            }.get(job.state, "Rip finished"),
            "message": f"{job.metadata.get('title') or 'Disc'} - {len([t for t in job.tracks if t.state == 'done'])} track(s)",
            "job": job.id,
        })

    # -- history ------------------------------------------------------------ #

    def _all(self) -> list[RipJob]:
        return [self.jobs[job_id] for job_id in self.order if job_id in self.jobs]

    def _trim(self) -> None:
        keep = self.settings_store.settings.job_retention
        finished = [job_id for job_id in self.order if (self.jobs.get(job_id) and not self.jobs[job_id].is_active)]
        while len(self.order) > keep + 10 and len(finished) > keep:
            job_id = finished.pop(0)
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)

    def _add_history(self, job: RipJob) -> None:
        record = {
            "id": job.id,
            "state": job.state,
            "finished_at": job.finished_at,
            "disc_id": job.disc_id,
            "disc_signature": job.disc_signature,
            "device": job.device,
            "format": job.format,
            "output_dir": job.output_dir or job.requested_output_dir,
            "title": (job.metadata or {}).get("title", ""),
            "album_artist": (job.metadata or {}).get("album_artist", ""),
            "year": (job.metadata or {}).get("year", ""),
            "cover_url": job.cover_url,
            "tracks_done": len([t for t in job.tracks if t.state == "done"]),
            "tracks_total": len(job.tracks),
            "bytes_written": job.bytes_written,
            "elapsed": round((job.finished_at or time.time()) - (job.started_at or job.created_at), 1),
            "error": job.error,
            "mock": job.mock,
            "files": [Path(f).name for f in job.files],
        }
        with self._lock:
            self.history = [r for r in self.history if r.get("id") != job.id]
            self.history.insert(0, record)
            self.history = self.history[:HISTORY_LIMIT]
        self._save_history()

    def _load_history(self) -> None:
        if not self.history_path or not self.history_path.is_file():
            return
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.history = data[:HISTORY_LIMIT]
        except (OSError, json.JSONDecodeError):
            self.history = []

    def _save_history(self) -> None:
        if not self.history_path:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.history, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.history_path)
        except OSError as exc:
            log.debug("cannot persist history: %s", exc)

    # -- events ------------------------------------------------------------- #

    def publish_job(self, job: RipJob, *, event: str = "job") -> None:
        """Throttled job update used for progress events."""
        if event == "progress":
            now = time.monotonic()
            if now - self._last_progress_emit < 0.25 and job.percent < 99.9:
                return
            self._last_progress_emit = now
        self.bus.publish(event, job.to_dict())


def jobs_to_dict(jobs: Iterable[RipJob]) -> list[dict[str, Any]]:
    return [job.to_dict() for job in jobs]
