"""End-to-end tests of the HTTP API using the demo drive."""

from __future__ import annotations

import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from ripcd.mock import MOCK_DISCS, MOCK_STATE
from ripcd.server import create_app
from ripcd.system import probe_tool

FFMPEG_AVAILABLE = probe_tool("ffmpeg", check_version=False).available


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "Music").mkdir(parents=True)
    monkeypatch.setenv("RIPCD_MEDIA_ROOTS", str(media))
    monkeypatch.setenv("RIPCD_OUTPUT_DIR", str(media / "Music"))
    monkeypatch.delenv("RIPCD_TOKEN", raising=False)
    MOCK_STATE.inserted_key = "chromatic-night"
    MOCK_STATE.tray_open = False
    MOCK_STATE.track_seconds = 1.0
    MOCK_STATE.generation_seconds = 0.3
    return {"media": media, "config": tmp_path / "config", "root": tmp_path}


@pytest.fixture
def client(workspace):
    app = create_app(mock=True, config_dir=str(workspace["config"]))
    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.2)
    pytest.fail(f"job did not finish in time: {job}")


# --------------------------------------------------------------------------- #
# basics
# --------------------------------------------------------------------------- #


def test_health_and_state(client):
    health = client.get("/api/health").json()
    assert health["status"] in {"ok", "degraded"}  # degraded when cdparanoia is absent
    assert health["mock"] is True

    state = client.get("/api/state").json()
    assert state["mock"] is True
    assert state["app"] == "RipCD"
    assert [fmt["id"] for fmt in state["formats"]][:1]
    assert state["drives"][0]["device"] == "/dev/sr0"
    assert state["drives"][0]["model"].startswith("Virtual")
    assert state["settings"]["output_dir"].endswith("Music")
    assert state["output_status"]["ok"] is True
    assert isinstance(state["logs"], list)


def test_static_ui_is_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "RipCD" in index.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/icon.svg").status_code == 200


def test_disc_endpoint_reports_the_demo_disc(client):
    disc = client.get("/api/disc").json()
    assert disc["device"] == "/dev/sr0"
    assert disc["has_disc"] is True
    assert disc["kind"] == "audio"
    assert disc["disc_id"]
    assert disc["toc"]["tracks"]
    assert len(disc["tracks"]) == len(MOCK_DISCS["chromatic-night"].tracks)

    # metadata is resolved by a background worker; wait for it like the UI does
    for _ in range(100):
        disc = client.get("/api/disc").json()
        if disc.get("metadata"):
            break
        time.sleep(0.05)
    assert disc["metadata"]["title"] == "Chromatic Night"
    assert disc["tracks"][0]["title"] == "Opening Signal"
    assert disc["tracks"][0]["known"] is True
    assert disc["metadata"]["source"] == "mock"
    assert disc["auto_rip"]["enabled"] is False


def test_toc_uses_absolute_msf_addressing(client):
    """Track 1 of a normal CD must start at frame 150 (MusicBrainz convention)."""
    disc = client.get("/api/disc").json()
    assert disc["toc"]["tracks"][0]["start_lba"] == 150
    assert disc["toc"]["tracks"][0]["start"] == "00:02.00"
    assert disc["disc_id"].endswith("-") or len(disc["disc_id"]) == 28


# --------------------------------------------------------------------------- #
# drives & demo controls
# --------------------------------------------------------------------------- #


def test_eject_and_close_tray(client):
    assert client.post("/api/drive/eject").json()["ok"] is True
    disc = client.get("/api/disc?refresh=true").json()
    assert disc["has_disc"] is False
    assert disc["status"] == "tray_open"

    assert client.post("/api/drive/close").json()["ok"] is True
    disc = client.get("/api/disc?refresh=true").json()
    assert disc["has_disc"] is True


def test_mock_disc_switching(client):
    mock = client.get("/api/mock/status").json()
    assert {d["key"] for d in mock["discs"]} == set(MOCK_DISCS)

    result = client.post("/api/mock/insert", json={"disc": "short-ep"}).json()
    assert result["disc"]["tracks"][0]["title"] == "Bootstrap"
    assert len(result["disc"]["tracks"]) == 3

    assert client.post("/api/mock/insert", json={"disc": "nope"}).status_code == 404
    assert client.post("/api/mock/eject").json()["disc"]["has_disc"] is False


def test_data_cd_is_reported_as_unrippable(client):
    client.post("/api/mock/insert", json={"disc": "data-disc"})
    disc = client.get("/api/disc").json()
    assert disc["kind"] == "data"

    created = client.post("/api/jobs", json={"format": "wav"})
    assert created.status_code == 201
    job = wait_for_job(client, created.json()["id"])
    assert job["state"] == "error"
    assert "no audio tracks" in job["error"]


# --------------------------------------------------------------------------- #
# folder browser & settings
# --------------------------------------------------------------------------- #


def test_browse_and_create_folder(client, workspace):
    media = str(workspace["media"])
    listing = client.get(f"/api/browse?path={media}").json()
    assert listing["path"] == media
    assert "Music" in [entry["name"] for entry in listing["entries"]]
    assert listing["writable"] is True

    created = client.post("/api/browse/mkdir", json={"path": media, "name": "Holiday/../Photos"}).json()
    assert created["path"] == os.path.join(media, "Holiday_.._Photos") or os.path.isdir(created["path"])

    outside = client.get("/api/browse?path=/etc")
    assert outside.status_code == 403


def test_settings_update_and_validation(client, workspace):
    media = workspace["media"]
    result = client.put("/api/settings", json={"output_dir": str(media / "New" / "Place")}).json()
    assert result["settings"]["output_dir"] == str(media / "New" / "Place")
    assert os.path.isdir(media / "New" / "Place")  # created on demand

    rejected = client.put("/api/settings", json={"output_dir": "/etc"})
    assert rejected.status_code == 400
    assert "outside" in rejected.json()["detail"].lower()

    formats = client.put("/api/settings", json={"format": "mp3", "quality": "320", "paranoia": "reduced"}).json()
    assert formats["settings"]["format"] == "mp3"
    assert formats["settings"]["quality"] == "320"

    normalized = client.put("/api/settings", json={"format": "flac", "paranoia": "nonsense"}).json()
    assert normalized["settings"]["paranoia"] == "full"


def test_settings_reset(client):
    client.put("/api/settings", json={"format": "opus"})
    reset = client.post("/api/settings/reset").json()
    assert reset["settings"]["format"] == "flac"


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


def test_preview_reflects_overrides_and_templates(client, workspace):
    response = client.post(
        "/api/preview",
        json={
            "format": "mp3",
            "tracks": [1, 2],
            "overrides": {"album": "Edited Album", "album_artist": "Edited Artist"},
            "track_titles": {"1": "Custom First"},
            "album_folder_template": "{album_artist}/{album} [{year}]",
            "track_file_template": "{track:02d} - {title}",
        },
    ).json()
    assert response["directory"].endswith("Edited Artist/Edited Album [1998]")
    assert [os.path.basename(f["name"]) for f in response["files"]] == [
        "01 - Custom First.mp3",
        "02 - Chromatic Night.mp3",
    ]


def test_preview_with_broken_template_is_rejected(client):
    response = client.post("/api/preview", json={"track_file_template": "{oops}"})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# ripping
# --------------------------------------------------------------------------- #


def test_full_rip_without_encoder(client, workspace):
    """WAV needs no encoder, so this always runs (even without ffmpeg)."""
    output = str(workspace["media"] / "Music")
    created = client.post(
        "/api/jobs",
        json={
            "format": "wav",
            "tracks": [1, 2],
            "output_dir": output,
            "overrides": {"album": "Test Album", "album_artist": "Test Artist"},
        },
    )
    assert created.status_code == 201, created.text
    job = wait_for_job(client, created.json()["id"])
    assert job["state"] == "done", job
    assert job["percent"] == 100.0
    assert [t["state"] for t in job["tracks"]] == ["done", "done"]

    album_dir = os.path.join(output, "Test Artist", "Test Album")
    assert os.path.isdir(album_dir)
    files = sorted(os.listdir(album_dir))
    assert "01 - Opening Signal.wav" in files
    assert "02 - Chromatic Night.wav" in files
    assert any(name.endswith(".m3u8") for name in files)      # playlist
    assert any(name.endswith(".ripcd.json") for name in files)  # rip report
    assert all(os.path.getsize(os.path.join(album_dir, f)) > 1000 for f in files if f.endswith(".wav"))
    # temporary files are cleaned up
    assert not os.path.isdir(os.path.join(output, ".ripcd-tmp"))

    history = client.get("/api/history").json()["history"]
    assert history and history[0]["tracks_done"] == 2
    assert history[0]["state"] == "done"


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg is required to encode FLAC")
def test_rip_with_encoder_and_tags(client, workspace):
    output = str(workspace["media"] / "Music")
    created = client.post(
        "/api/jobs",
        json={"format": "flac", "quality": "5", "tracks": [1], "output_dir": output},
    )
    job = wait_for_job(client, created.json()["id"])
    assert job["state"] == "done", job
    path = job["tracks"][0]["output"]
    assert path.endswith(".flac")
    with open(path, "rb") as handle:
        assert handle.read(4) == b"fLaC"  # a real FLAC stream
    assert job["tracks"][0]["size_bytes"] > 1000


def test_job_with_explicit_track_selection_keeps_disc_numbering(client, workspace):
    output = str(workspace["media"] / "Music")
    created = client.post("/api/jobs", json={"format": "wav", "tracks": [3], "output_dir": output})
    assert created.status_code == 201, created.text
    job = wait_for_job(client, created.json()["id"])
    assert job["state"] == "done", job
    assert job["track_numbers"] == [3]
    assert job["tracks"][0]["number"] == 3
    assert job["tracks"][0]["output"].startswith(os.path.join(output, "The Demo Band", "Chromatic Night"))


def test_unknown_tracks_are_rejected(client, workspace):
    created = client.post(
        "/api/jobs",
        json={"format": "wav", "tracks": [42], "output_dir": str(workspace["media"])},
    )
    job = wait_for_job(client, created.json()["id"])
    assert job["state"] == "error"
    assert "exist on this disc" in job["error"]


def test_second_job_is_refused_while_one_runs(client, workspace):
    MOCK_STATE.generation_seconds = 3.0
    first = client.post("/api/jobs", json={"format": "wav", "tracks": [1, 2, 3], "output_dir": str(workspace["media"])})
    assert first.status_code == 201
    try:
        second = client.post("/api/jobs", json={"format": "wav", "output_dir": str(workspace["media"])})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
        forced = client.post(
            "/api/jobs", json={"format": "wav", "output_dir": str(workspace["media"]), "force": True}
        )
        assert forced.status_code == 201
    finally:
        MOCK_STATE.generation_seconds = 0.3
        client.post(f"/api/jobs/{first.json()['id']}/cancel")
        for job in client.get("/api/jobs").json()["jobs"]:
            if job["state"] in {"queued", "running"}:
                client.post(f"/api/jobs/{job['id']}/cancel")
                wait_for_job(client, job["id"])


def test_canceling_a_running_job(client, workspace):
    MOCK_STATE.generation_seconds = 5.0
    created = client.post("/api/jobs", json={"format": "wav", "output_dir": str(workspace["media"])}).json()
    time.sleep(1.0)
    assert client.post(f"/api/jobs/{created['id']}/cancel").json()["ok"] is True
    job = wait_for_job(client, created["id"], timeout=30)
    assert job["state"] == "canceled"
    assert job["percent"] < 100
    MOCK_STATE.generation_seconds = 0.3


def test_retry_and_delete_jobs(client, workspace):
    created = client.post("/api/jobs", json={"format": "wav", "tracks": [1], "output_dir": str(workspace["media"])}).json()
    wait_for_job(client, created["id"])
    retried = client.post(f"/api/jobs/{created['id']}/retry", json={}).json()
    assert retried["id"] != created["id"]
    assert retried["track_numbers"] == [1]
    wait_for_job(client, retried["id"])

    assert client.delete(f"/api/jobs/{created['id']}").json()["ok"] is True
    assert client.get(f"/api/jobs/{created['id']}").status_code == 404
    assert client.delete("/api/history").json()["ok"] is True
    assert client.get("/api/history").json()["history"] == []


def test_jobs_listing_and_queue(client):
    listing = client.get("/api/jobs").json()
    assert "jobs" in listing and "queue" in listing
    assert client.get("/api/jobs/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# metadata, cover, logs
# --------------------------------------------------------------------------- #


def test_metadata_refresh(client):
    response = client.post("/api/metadata/refresh?wait=5")
    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["title"] == "Chromatic Night"
    assert payload["disc"]["has_disc"] is True


def test_cover_endpoint_in_demo_mode(client):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed to render the demo cover")
    response = client.get("/api/cover/current?size=500")
    if response.status_code == 404:  # generated cover not available
        pytest.skip("demo cover could not be generated")
    assert response.headers["content-type"].startswith("image/")


def test_logs_are_recorded(client):
    logs = client.get("/api/logs").json()["logs"]
    assert logs, "the app should log its start-up"
    assert all("message" in line and "level" in line for line in logs)
    assert client.delete("/api/logs").json()["ok"] is True
    assert client.get("/api/logs").json()["logs"] == []


def test_system_endpoint(client):
    system = client.get("/api/system").json()
    assert "tools" in system["system"]
    assert "sr_in_sysfs" in system["device_tree"]


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #


def test_api_token_protects_everything(workspace, monkeypatch):
    monkeypatch.setenv("RIPCD_TOKEN", "s3cret-token")
    app = create_app(mock=True, config_dir=str(workspace["config"] / "auth"))
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", headers={"X-API-Token": "s3cret-token"}).status_code == 200
        assert client.get("/api/state", params={"token": "s3cret-token"}).status_code == 200
        assert client.get("/api/state", headers={"X-API-Token": "wrong"}).status_code == 401
        # health stays open for container health checks
        assert client.get("/api/health").status_code == 200

        # the web UI shows a login page and remembers the token in a cookie
        locked = client.get("/")
        assert locked.status_code == 401
        assert "API token" in locked.text
        unlocked = client.get("/", params={"token": "s3cret-token"})
        assert unlocked.status_code == 200
        assert "ripcd_token" in unlocked.cookies
        assert client.get("/api/state").status_code == 200


# --------------------------------------------------------------------------- #
# The web UI and the API must not drift apart
# --------------------------------------------------------------------------- #


def _api_routes() -> set[tuple[str, str]]:
    from ripcd.server import create_app

    app = create_app(mock=True)
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or set():
            routes.add((method.upper(), path))
    return routes


def test_every_endpoint_the_ui_calls_exists(client):
    """Every ``api.get/post/...`` in app.js has to have a matching route."""
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()
    routes = _api_routes()
    calls: set[tuple[str, str]] = set()
    for match in re.finditer(r"api\.(get|post|put|del|patch)\(\s*[`\"']([^`\"']+)", js):
        method = {"del": "DELETE"}.get(match.group(1), match.group(1).upper())
        url = re.sub(r"\$\{[^}]+\}[^/]*", "{x}", match.group(2)).split("?")[0]
        calls.add((method, url))

    assert calls, "no API calls found in app.js - the UI changed shape?"

    def matches(method: str, url: str) -> bool:
        if (method, url) in routes:
            return True
        wanted = [part for part in url.split("/") if part]
        for route_method, route_path in routes:
            if route_method != method:
                continue
            parts = [part for part in route_path.split("/") if part]
            if len(parts) != len(wanted):
                continue
            if all(part.startswith("{") or part == want for part, want in zip(parts, wanted)):
                return True
        return False

    missing = sorted(call for call in calls if not matches(*call))
    assert missing == [], f"the UI calls endpoints that do not exist: {missing}"
