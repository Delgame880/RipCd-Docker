"""FastAPI application: REST API + Server-Sent Events + the web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, __version__
from .artwork import CoverArtClient
from .config import (
    FORMATS,
    METADATA_PROVIDERS,
    PARANOIA_LEVELS,
    SettingsStore,
    default_media_roots,
)
from .drives import DriveProvider
from .encoder import default_quality, output_extension
from .events import EventBus, RingLog, sse_encode
from .manager import JobManager
from .metadata import AlbumMetadata, MetadataResolver, MusicBrainzClient
from .mock import MOCK_DISCS, MOCK_STATE, MockCoverArtClient, MockMetadataClient
from .optical import OpticalError, device_tree_snapshot
from .paths import (
    PathNotAllowed,
    check_path,
    list_directory,
    sanitize_component,
)
from .ripper import RipJob, make_job, preview_paths
from .scanner import DiscScanner, metadata_notify_callback
from .security import TOKEN_COOKIE, LOGIN_HTML, authorized, is_api
from .system import probe_tools, system_info

log = logging.getLogger("ripcd.server")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------- #
# Logging plumbing
# --------------------------------------------------------------------------- #


#: Every running app instance that wants log records (there is normally one,
#: but the tests build several in the same process).
LOG_SINKS: list[tuple[RingLog, EventBus]] = []


class BusLogHandler(logging.Handler):
    """Feeds log records into the in-memory ring buffer and the SSE stream."""

    def __init__(self, level: int = logging.INFO):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                message += f" ({record.exc_info[0].__name__})"
            source = record.name.replace("ripcd.", "")
            for ring, bus in list(LOG_SINKS):
                item = ring.add(message, level=record.levelname.lower(), source=source)
                bus.publish("log", item)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


# --------------------------------------------------------------------------- #
# Application context
# --------------------------------------------------------------------------- #


@dataclass
class AppContext:
    settings_store: SettingsStore
    bus: EventBus
    ring: RingLog
    drives: DriveProvider
    covers: CoverArtClient
    resolver: MetadataResolver
    runner: Any
    manager: JobManager
    scanner: DiscScanner
    mock: bool = False
    tools: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    session_token: str = ""


def build_context(*, mock: bool | None = None, config_dir: str | None = None) -> AppContext:
    if mock is None:
        mock = env_flag("RIPCD_MOCK") or env_flag("RIPCD_DEMO")
    config_dir = config_dir or os.environ.get("RIPCD_CONFIG_DIR", "/config")
    settings_store = SettingsStore(config_dir=config_dir)
    settings = settings_store.load()

    bus = EventBus()
    ring = RingLog()

    drives = DriveProvider(settings_store, mock=mock)
    resolver_client = MockMetadataClient(MOCK_STATE) if mock else MusicBrainzClient(
        cache_dir=os.path.join(config_dir, "cache", "metadata"),
        contact=settings.musicbrainz_contact,
    )
    resolver = MetadataResolver(resolver_client)
    covers = (
        MockCoverArtClient(cache_dir=os.path.join(config_dir, "cache", "covers"), state=MOCK_STATE)
        if mock
        else CoverArtClient(
            cache_dir=os.path.join(config_dir, "cache", "covers"),
            contact=settings.musicbrainz_contact,
        )
    )

    from .ripper import RipRunner

    runner = RipRunner(
        drives,
        resolver,
        covers,
        settings_store=settings_store,
        on_event=lambda event, job: manager.publish_job(job, event=event),
    )
    manager = JobManager(
        runner,
        settings_store,
        bus,
        history_path=os.path.join(config_dir, "history.json"),
    )
    scanner = DiscScanner(drives, settings_store, resolver, manager, bus)
    # Once a lookup finishes, nudge the scanner so the UI updates immediately.
    resolver.on_update = metadata_notify_callback(bus, scanner)

    return AppContext(
        settings_store=settings_store,
        bus=bus,
        ring=ring,
        drives=drives,
        covers=covers,
        resolver=resolver,
        runner=runner,
        manager=manager,
        scanner=scanner,
        mock=mock,
    )


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(*, mock: bool | None = None, config_dir: str | None = None) -> FastAPI:
    ctx = build_context(mock=mock, config_dir=config_dir)
    settings_store = ctx.settings_store

    # ---- logging ------------------------------------------------------- #
    LOG_SINKS.append((ctx.ring, ctx.bus))
    handler = BusLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger("ripcd")
    root.setLevel(logging.DEBUG if env_flag("RIPCD_DEBUG") else logging.INFO)
    if not any(isinstance(h, BusLogHandler) for h in root.handlers):
        root.addHandler(handler)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(stream)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # ---- lifespan ------------------------------------------------------ #
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.bus.bind_loop(asyncio.get_running_loop())
        ctx.tools = probe_tools()
        log.info("%s %s starting (python %s, %s)", APP_NAME, __version__, system_info()["python"], "DEMO MODE" if ctx.mock else "live mode")
        missing = [name for name, tool in ctx.tools.items() if tool.required and not tool.available]
        if missing:
            log.error("required tools are missing: %s", ", ".join(missing))
        if ctx.mock:
            log.warning("RIPCD_MOCK is on: no real drive or CD is used, everything is simulated")
        ctx.resolver.start()
        ctx.manager.start()
        ctx.scanner.start(delay=0.3)
        try:
            yield
        finally:
            ctx.scanner.stop()
            ctx.manager.stop()
            ctx.resolver.stop()
            log.info("shutting down")

    app = FastAPI(
        title=f"{APP_NAME} API",
        version=__version__,
        description="Rip audio CDs to your NAS from a browser. Built for ZimaOS/Docker.",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    # ---- auth ---------------------------------------------------------- #

    def current_token() -> str:
        return settings_store.settings.api_token or ""

    async def guard(request: Request) -> None:
        token = current_token()
        if not token or not is_api(request.url.path) or request.url.path == "/api/health":
            return
        if not authorized(token, request.headers, request.query_params, request.cookies):
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    @app.middleware("http")
    async def token_middleware(request: Request, call_next):
        token = current_token()
        path = request.url.path
        if not token or is_api(path) or path.startswith("/static"):
            response = await call_next(request)
        else:
            provided = request.query_params.get("token", "")
            if provided and authorized(token, request.headers, request.query_params, request.cookies):
                response = await call_next(request)
                response.set_cookie(TOKEN_COOKIE, provided, httponly=True, samesite="lax", path="/", max_age=90 * 24 * 3600)
                return response
            if authorized(token, request.headers, request.query_params, request.cookies):
                response = await call_next(request)
            else:
                response = HTMLResponse(LOGIN_HTML, status_code=401)
        response.headers.setdefault("X-RipCD-Version", __version__)
        return response

    # ---- helpers ------------------------------------------------------- #

    def settings():
        return settings_store.settings

    async def fresh_disc(wait_metadata: float = 0.0) -> dict[str, Any]:
        """Current disc snapshot.

        Scans synchronously on the very first request (right after start-up)
        and, when asked, waits for a pending metadata lookup so the UI never
        renders "Unknown Album" just because the lookup is still in flight.
        """
        disc = ctx.scanner.current()
        if not disc.get("device") or wait_metadata > 0:
            disc = await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=wait_metadata)
        return disc

    def path_status(path: str) -> dict[str, Any]:
        try:
            return check_path(path, settings().media_roots).to_dict()
        except OSError as exc:
            return {"ok": False, "path": path, "error": str(exc)}

    def drive_summaries() -> list[dict[str, Any]]:
        disc = ctx.scanner.current()
        active = settings().device or disc.get("device") or ""
        summaries: list[dict[str, Any]] = []
        for drive in ctx.drives.list():
            try:
                info = drive.info()
                status = drive.status()
            except Exception as exc:  # noqa: BLE001
                info, status = {"device": drive.device, "label": drive.name, "mock": bool(getattr(drive, "is_mock", False))}, "error"
                info["error"] = str(exc)
            info.update(
                {
                    "status": status,
                    "has_disc": status == "disc_ok",
                    "selected": drive.device == active or (not active and not summaries),
                    "disc_kind": disc.get("kind") if disc.get("device") == drive.device else "none",
                    "track_count": len(disc.get("tracks") or []) if disc.get("device") == drive.device else 0,
                    "disc_id": disc.get("disc_id", "") if disc.get("device") == drive.device else "",
                }
            )
            summaries.append(info)
        return summaries

    def state_payload() -> dict[str, Any]:
        current = settings()
        return {
            "app": APP_NAME,
            "version": __version__,
            "mock": ctx.mock,
            "started_at": ctx.started_at,
            "system": system_info(tools=ctx.tools or probe_tools()),
            "settings": current.public_dict(),
            "formats": [
                {
                    "id": key,
                    "label": value["label"],
                    "extension": value["ext"],
                    "kind": value["kind"],
                    "qualities": value["qualities"],
                    "default_quality": value["default_quality"],
                }
                for key, value in FORMATS.items()
            ],
            "paranoia_levels": PARANOIA_LEVELS,
            "metadata_providers": METADATA_PROVIDERS,
            "media_roots": current.media_roots,
            "output_status": path_status(current.output_dir),
            "drives": drive_summaries(),
            "disc": ctx.scanner.current(),
            "jobs": ctx.manager.list(limit=25),
            "history": ctx.manager.history[:25],
            "logs": ctx.ring.tail(limit=120),
            "queue": ctx.manager.queue_state(),
            "device_tree": device_tree_snapshot(),
        }

    # ---- meta ---------------------------------------------------------- #

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        missing = [name for name, tool in (ctx.tools or {}).items() if tool.required and not tool.available]
        return {
            "status": "ok" if not missing else "degraded",
            "version": __version__,
            "mock": ctx.mock,
            "uptime": round(time.time() - ctx.started_at, 1),
            "missing_tools": missing,
            "active_job": (ctx.manager.active().id if ctx.manager.active() else None),
        }

    @app.get("/api/system", dependencies=[Depends(guard)])
    async def system(request: Request, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            ctx.tools = probe_tools()
        return {
            "system": system_info(tools=ctx.tools or probe_tools()),
            "device_tree": device_tree_snapshot(),
            "mock": ctx.mock,
            "media_roots": settings().media_roots,
        }

    @app.get("/api/formats", dependencies=[Depends(guard)])
    async def formats() -> dict[str, Any]:
        return {
            "formats": state_payload()["formats"],
            "paranoia_levels": PARANOIA_LEVELS,
            "metadata_providers": METADATA_PROVIDERS,
        }

    @app.get("/api/state", dependencies=[Depends(guard)])
    async def state() -> dict[str, Any]:
        return state_payload()

    # ---- settings ------------------------------------------------------ #

    @app.get("/api/settings", dependencies=[Depends(guard)])
    async def get_settings() -> dict[str, Any]:
        return {"settings": settings().public_dict(), "output_status": path_status(settings().output_dir)}

    @app.put("/api/settings", dependencies=[Depends(guard)])
    async def put_settings(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
        # Validate the output folder before storing it, so the UI can react.
        if "output_dir" in patch and patch.get("output_dir"):
            path = str(patch["output_dir"])
            check = check_path(path, patch.get("media_roots") or settings().media_roots)
            if not check.ok and not check.writable:
                raise HTTPException(status_code=400, detail=check.error or f"cannot use {path}")
            if not check.exists:
                try:
                    os.makedirs(check.path, exist_ok=True)
                except OSError as exc:
                    raise HTTPException(status_code=400, detail=f"cannot create {check.path}: {exc}") from exc
            patch["output_dir"] = check.path
        try:
            updated = settings_store.update(patch)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.bus.publish("settings", updated.public_dict())
        return {"settings": updated.public_dict(), "output_status": path_status(updated.output_dir)}

    @app.post("/api/settings/reset", dependencies=[Depends(guard)])
    async def reset_settings() -> dict[str, Any]:
        updated = settings_store.reset()
        ctx.bus.publish("settings", updated.public_dict())
        return {"settings": updated.public_dict()}

    # ---- folder browser ------------------------------------------------ #

    @app.get("/api/browse", dependencies=[Depends(guard)])
    async def browse(path: str | None = None, files: bool = False) -> dict[str, Any]:
        roots = settings().media_roots
        target = path or settings().output_dir or (roots[0] if roots else "/")
        try:
            listing = await asyncio.to_thread(list_directory, target, roots, show_files=files)
        except PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        listing["mounts"] = roots
        return listing

    @app.post("/api/browse/mkdir", dependencies=[Depends(guard)])
    async def make_directory(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        parent = str(payload.get("path") or settings().output_dir)
        name = sanitize_component(str(payload.get("name") or "New folder"))
        check = check_path(parent, settings().media_roots)
        if not check.ok and not check.writable:
            raise HTTPException(status_code=400, detail=check.error or "not writable")
        target = os.path.join(check.path, name)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot create {target}: {exc}") from exc
        return {"path": target}

    # ---- drives -------------------------------------------------------- #

    @app.get("/api/drives", dependencies=[Depends(guard)])
    async def get_drives() -> dict[str, Any]:
        return {"drives": drive_summaries(), "device_tree": device_tree_snapshot()}

    @app.post("/api/drive/eject", dependencies=[Depends(guard)])
    async def eject(device: str | None = None) -> dict[str, Any]:
        drive = ctx.drives.get(device)
        if drive is None:
            raise HTTPException(status_code=404, detail="no such drive")
        try:
            await asyncio.to_thread(drive.eject)
        except OpticalError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        ctx.bus.publish("notification", {"level": "info", "title": "Disc ejected", "message": drive.device})
        await asyncio.to_thread(ctx.scanner.refresh)
        return {"ok": True, "device": drive.device}

    @app.post("/api/drive/close", dependencies=[Depends(guard)])
    async def close_tray(device: str | None = None) -> dict[str, Any]:
        drive = ctx.drives.get(device)
        if drive is None:
            raise HTTPException(status_code=404, detail="no such drive")
        try:
            await asyncio.to_thread(drive.close_tray)
        except OpticalError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await asyncio.to_thread(ctx.scanner.refresh)
        return {"ok": True, "device": drive.device}

    @app.post("/api/drives/rescan", dependencies=[Depends(guard)])
    async def rescan_devices() -> dict[str, Any]:
        ctx.drives.refresh(force=True)
        return {"drives": drive_summaries(), "device_tree": device_tree_snapshot()}

    # ---- disc & metadata ----------------------------------------------- #

    @app.get("/api/disc", dependencies=[Depends(guard)])
    async def get_disc(refresh: bool = False) -> dict[str, Any]:
        disc = ctx.scanner.current()
        # First request after start-up: scan synchronously so the UI is not empty.
        if refresh or not disc.get("device"):
            return await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=0.0)
        # A lookup may have finished since the last poll - merge it in.
        if disc.get("metadata_pending"):
            album = ctx.resolver.get(disc.get("signature") or "")
            if album is not None:
                return await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=0.0)
        return disc

    @app.post("/api/disc/refresh", dependencies=[Depends(guard)])
    async def refresh_disc(wait: float = 0) -> dict[str, Any]:
        """Re-read the TOC and, optionally, wait for metadata."""
        state = await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=max(0.0, min(wait, 30.0)))
        return state

    @app.post("/api/metadata/refresh", dependencies=[Depends(guard)])
    async def refresh_metadata(wait: float = 25, overrides: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        """Force a fresh MusicBrainz lookup for the disc in the drive."""
        disc = await fresh_disc()
        if not disc.get("has_disc"):
            raise HTTPException(status_code=400, detail="no disc in the drive")
        signature = disc.get("signature") or ""
        drive = ctx.drives.get(disc.get("device"))
        if drive is None:
            raise HTTPException(status_code=400, detail="drive not available")
        toc = await asyncio.to_thread(drive.read_toc)
        if toc is None:
            raise HTTPException(status_code=400, detail="cannot read the disc")
        mcn = await asyncio.to_thread(drive.mcn)
        ctx.resolver.forget(signature)
        album = await asyncio.to_thread(ctx.resolver.resolve_now, signature, toc, mcn or "")
        state = await asyncio.to_thread(ctx.scanner.refresh)
        return {"metadata": album.to_dict(), "disc": state}

    @app.get("/api/cover/current", dependencies=[Depends(guard)])
    async def cover_current(size: str = "500") -> Response:
        disc = await fresh_disc(wait_metadata=1.0)
        album = AlbumMetadata.from_dict(disc.get("metadata") or {})
        return await _serve_cover(ctx, album, size)

    @app.get("/api/cover/{release_id}", dependencies=[Depends(guard)])
    async def cover(release_id: str, size: str = "500", release_group: str = "") -> Response:
        album = AlbumMetadata(release_id=release_id, release_group_id=release_group)
        return await _serve_cover(ctx, album, size)

    async def _serve_cover(context: AppContext, album: AlbumMetadata, size: str) -> Response:
        if not (album.release_id or album.release_group_id):
            raise HTTPException(status_code=404, detail="no cover available for this disc")
        size = size if size in {"250", "500", "1200", "front"} else "500"
        path = await asyncio.to_thread(
            context.covers.fetch, album.release_id, size=size, release_group_id=album.release_group_id
        )
        if not path or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="cover art not found")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    # ---- jobs ---------------------------------------------------------- #

    @app.post("/api/preview", dependencies=[Depends(guard)])
    async def preview(payload: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
        disc = await fresh_disc(wait_metadata=3.0)
        if not disc.get("has_disc"):
            raise HTTPException(status_code=400, detail="no disc in the drive")
        drive = ctx.drives.get(disc.get("device"))
        toc = await asyncio.to_thread(drive.read_toc) if drive else None
        if toc is None:
            raise HTTPException(status_code=400, detail="cannot read the disc")
        payload = payload or {}
        try:
            job = make_job({**payload, "device": disc.get("device")}, settings())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        album = AlbumMetadata.from_dict(disc.get("metadata") or {})
        from .ripper import apply_overrides

        album = apply_overrides(album, job.overrides, toc, job.track_titles)
        files = preview_paths(job, album, toc)
        return {"directory": os.path.dirname(files[0]["path"]) if files else job.requested_output_dir, "files": files}

    @app.post("/api/jobs", status_code=201, dependencies=[Depends(guard)])
    async def create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        active = ctx.manager.active()
        if active is not None and not payload.get("force"):
            raise HTTPException(status_code=409, detail=f"a rip is already running ({active.id})")
        if not payload.get("device"):
            disc = await fresh_disc()
            payload["device"] = disc.get("device") or ""
        try:
            job: RipJob = make_job(payload, settings())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job.mock = ctx.mock
        log.info("rip requested: %s track(s) from %s -> %s", len(job.track_numbers) or "all", job.device or "auto", job.requested_output_dir)
        ctx.manager.submit(job)
        return job.to_dict()

    @app.get("/api/jobs", dependencies=[Depends(guard)])
    async def list_jobs(limit: int = 25) -> dict[str, Any]:
        return {"jobs": ctx.manager.list(limit=max(1, min(200, limit))), "queue": ctx.manager.queue_state()}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(guard)])
    async def get_job(job_id: str) -> dict[str, Any]:
        job = ctx.manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(guard)])
    async def cancel_job(job_id: str) -> dict[str, Any]:
        if not ctx.manager.cancel(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry", status_code=201, dependencies=[Depends(guard)])
    async def retry_job(job_id: str, payload: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
        job = ctx.manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        payload = payload or {}
        request = {
            "device": job.device,
            "output_dir": payload.get("output_dir") or job.requested_output_dir,
            "format": payload.get("format") or job.format,
            "quality": payload.get("quality") or job.quality,
            "tracks": payload.get("tracks") or [t.number for t in job.tracks],
            "overrides": {**(job.overrides or {}), **(payload.get("overrides") or {})},
            "track_titles": job.track_titles,
            "paranoia": payload.get("paranoia") or job.paranoia,
            "read_speed": job.read_speed,
        }
        new_job = make_job(request, settings())
        ctx.manager.submit(new_job)
        return new_job.to_dict()

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(guard)])
    async def delete_job(job_id: str) -> dict[str, Any]:
        if not ctx.manager.remove(job_id):
            raise HTTPException(status_code=409, detail="job is running or unknown")
        return {"ok": True}

    # ---- history ------------------------------------------------------- #

    @app.get("/api/history", dependencies=[Depends(guard)])
    async def history(limit: int = 100) -> dict[str, Any]:
        return {"history": ctx.manager.history[: max(1, min(500, limit))]}

    @app.delete("/api/history", dependencies=[Depends(guard)])
    async def clear_history() -> dict[str, Any]:
        ctx.manager.clear_history()
        return {"ok": True}

    # ---- logs & events -------------------------------------------------- #

    @app.get("/api/logs", dependencies=[Depends(guard)])
    async def logs(limit: int = 200, after: int = 0) -> dict[str, Any]:
        return {"logs": ctx.ring.tail(limit=max(1, min(1000, limit)), after=after)}

    @app.delete("/api/logs", dependencies=[Depends(guard)])
    async def clear_logs() -> dict[str, Any]:
        ctx.ring.clear()
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request, token: str = "") -> Response:
        current = settings_store.settings.api_token
        if current and not authorized(current, request.headers, request.query_params, request.cookies):
            raise HTTPException(status_code=401, detail="invalid or missing API token")

        async def stream():
            queue = ctx.bus.subscribe()
            try:
                yield sse_encode({"type": "hello", "data": {"version": __version__, "mock": ctx.mock}})
                yield sse_encode({"type": "state", "data": state_payload()})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield sse_encode(event)
            except asyncio.CancelledError:  # pragma: no cover - client went away
                raise
            finally:
                ctx.bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- demo mode ------------------------------------------------------ #

    @app.get("/api/mock/status")
    async def mock_status() -> dict[str, Any]:
        return {
            "enabled": ctx.mock,
            "inserted": MOCK_STATE.inserted_key,
            "tray_open": MOCK_STATE.tray_open,
            "track_seconds": MOCK_STATE.track_seconds,
            "generation_seconds": MOCK_STATE.generation_seconds,
            "discs": [
                {"key": key, "title": disc.title, "artist": disc.artist, "tracks": len(disc.tracks), "audio": disc.is_audio}
                for key, disc in MOCK_DISCS.items()
            ],
        }

    @app.post("/api/mock/insert", dependencies=[Depends(guard)])
    async def mock_insert(payload: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
        if not ctx.mock:
            raise HTTPException(status_code=400, detail="demo mode is not enabled (set RIPCD_MOCK=1)")
        payload = payload or {}
        key = str(payload.get("disc") or MOCK_STATE.inserted_key)
        if key not in MOCK_DISCS:
            raise HTTPException(status_code=404, detail=f"unknown demo disc '{key}'")
        MOCK_STATE.insert(key)
        if "track_seconds" in payload:
            MOCK_STATE.track_seconds = max(1.0, min(60.0, float(payload["track_seconds"])))
        if "generation_seconds" in payload:
            MOCK_STATE.generation_seconds = max(0.2, min(30.0, float(payload["generation_seconds"])))
        state = await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=3.0)
        return {"ok": True, "disc": state}

    @app.post("/api/mock/eject", dependencies=[Depends(guard)])
    async def mock_eject() -> dict[str, Any]:
        if not ctx.mock:
            raise HTTPException(status_code=400, detail="demo mode is not enabled (set RIPCD_MOCK=1)")
        MOCK_STATE.eject()
        return {"ok": True, "disc": await asyncio.to_thread(ctx.scanner.refresh)}

    @app.post("/api/mock/close", dependencies=[Depends(guard)])
    async def mock_close() -> dict[str, Any]:
        if not ctx.mock:
            raise HTTPException(status_code=400, detail="demo mode is not enabled (set RIPCD_MOCK=1)")
        MOCK_STATE.close()
        return {"ok": True, "disc": await asyncio.to_thread(ctx.scanner.refresh, wait_for_metadata=3.0)}

    # ---- static UI ------------------------------------------------------ #

    if STATIC_DIR.is_dir():
        @app.get("/")
        async def index() -> Response:
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:  # pragma: no cover - only when the image is built incorrectly
        @app.get("/")
        async def index_missing() -> Response:
            return HTMLResponse("<h1>RipCD</h1><p>The web UI is missing from this image.</p>", status_code=500)

    return app


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    host = os.environ.get("RIPCD_HOST", "0.0.0.0")
    port = int(os.environ.get("RIPCD_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("RIPCD_LOG_LEVEL", "info"))


if __name__ == "__main__":  # pragma: no cover
    main()
