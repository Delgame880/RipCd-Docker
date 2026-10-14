"""Tests for job construction, naming and file layout."""

from __future__ import annotations

import os

import pytest

from ripcd.config import Settings
from ripcd.metadata import AlbumMetadata, TrackMetadata, unknown_album
from ripcd.optical import Toc, TrackInfo
from ripcd.ripper import (
    RipError,
    RipJob,
    album_directory,
    apply_overrides,
    build_tags,
    make_job,
    new_job_id,
    preview_paths,
    track_filename,
    unique_directory,
    validate_job_request,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(media_roots=[str(tmp_path)], output_dir=str(tmp_path / "Music")).normalized()


@pytest.fixture
def toc():
    tracks = [
        TrackInfo(number=index, start_lba=150 + (index - 1) * 75 * 60, duration_frames=75 * 60)
        for index in range(1, 4)
    ]
    return Toc(first_track=1, last_track=3, leadout_lba=150 + 3 * 75 * 60, tracks=tracks)


def album(toc):
    base = unknown_album(toc)
    return apply_overrides(
        base,
        {"album": "My Album", "album_artist": "My Artist", "year": "2004", "genre": "Pop"},
    )


# --------------------------------------------------------------------------- #
# ids & directories
# --------------------------------------------------------------------------- #


def test_new_job_id_is_sortable_and_unique():
    first, second = new_job_id(), new_job_id()
    assert first != second
    assert len(first) == len("20260101-120000-8f3a")  # date-time + 4 random hex
    assert first[:15].count("-") == 1
    assert first[:15] <= second[:15]  # sortable by creation time


def test_unique_directory_appends_a_counter(tmp_path):
    target = tmp_path / "Album"
    assert unique_directory(str(target)) == str(target)
    target.mkdir()
    assert unique_directory(str(target)) == str(target)  # empty folders are reused
    (target / "01 - track.flac").write_bytes(b"x")
    assert unique_directory(str(target)) == str(target) + " (2)"
    os.makedirs(f"{target} (2)")
    (tmp_path / "Album (2)" / "f").write_bytes(b"x")
    assert unique_directory(str(target)) == str(target) + " (3)"


def test_album_directory_uses_the_template(tmp_path, toc):
    meta = album(toc)
    directory = album_directory(str(tmp_path), meta, "{album_artist}/{album}")
    assert directory == str(tmp_path / "My Artist" / "My Album")


def test_album_directory_sanitizes_and_supports_empty_template(tmp_path, toc):
    meta = album(toc)
    meta.title = "AC/DC: Live?"
    meta.album_artist = ".."
    directory = album_directory(str(tmp_path), meta, "{album_artist}/{album}")
    assert directory.startswith(str(tmp_path))
    assert ".." not in os.path.relpath(directory, str(tmp_path))
    assert album_directory(str(tmp_path), meta, "") == str(tmp_path)


def test_album_directory_never_escapes_the_output_folder(tmp_path, toc):
    meta = album(toc)
    meta.title = "../../../../etc"
    meta.album_artist = ".."
    directory = album_directory(str(tmp_path), meta, "{album_artist}/{album}")
    assert os.path.realpath(directory).startswith(str(tmp_path))


def test_album_directory_survives_a_broken_template(tmp_path, toc):
    directory = album_directory(str(tmp_path), album(toc), "{nonexistent}/{album}")
    assert os.path.realpath(directory).startswith(str(tmp_path))


# --------------------------------------------------------------------------- #
# tags & file names
# --------------------------------------------------------------------------- #


def test_track_filename_uses_the_template(settings, toc):
    job = RipJob(id="x", device="/dev/sr0", requested_output_dir=settings.output_dir, format="flac")
    job.track_file_template = "{track:02d} - {title}"
    name = track_filename(job, album(toc), toc.tracks[0], track_total=3)
    assert name == "01 - Track 01.flac"


def test_track_filename_sanitizes_illegal_characters(settings, toc):
    job = RipJob(id="x", device="/dev/sr0", requested_output_dir=settings.output_dir, format="mp3")
    meta = album(toc)
    meta.tracks[0].title = 'AC/DC: "Back in Black"'
    name = track_filename(job, meta, toc.tracks[0], track_total=3)
    assert "/" not in name and '"' not in name and ":" not in name
    assert name.endswith(".mp3")


def test_build_tags_contains_the_expected_keys(settings, toc):
    meta = album(toc)
    meta.release_id = "release-mbid"
    meta.disc_id = "discid"
    meta.tracks[0].recording_id = "recording-mbid"
    job = RipJob(id="x", device="/dev/sr0", requested_output_dir=settings.output_dir)
    tags = build_tags(job, meta, 1, track_total=3)
    assert tags["title"] == "Track 01"
    assert tags["album"] == "My Album"
    assert tags["album_artist"] == "My Artist"
    assert tags["track"] == 1
    assert tags["track_total"] == 3
    assert tags["date"] == "2004"
    assert tags["musicbrainz_album_id"] == "release-mbid"
    assert tags["musicbrainz_trackid"] == "recording-mbid"
    assert tags["musicbrainz_discid"] == "discid"
    assert "comment" in tags


def test_build_tags_uses_the_track_artist_when_known(settings, toc):
    meta = album(toc)
    meta.tracks[0] = TrackMetadata(number=1, title="Solo", artist="Guest")
    job = RipJob(id="x", device="/dev/sr0", requested_output_dir=settings.output_dir)
    assert build_tags(job, meta, 1, track_total=3)["artist"] == "Guest"


def test_preview_paths_lists_only_selected_tracks(settings, toc):
    job = RipJob(
        id="x",
        device="/dev/sr0",
        requested_output_dir=settings.output_dir,
        track_numbers=[2, 3],
        format="flac",
    )
    files = preview_paths(job, album(toc), toc)
    assert [f["track"] for f in files] == ["2", "3"]
    assert all(f["path"].startswith(job.requested_output_dir) for f in files)


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #


def test_validate_job_request_defaults(settings):
    values = validate_job_request({}, settings)
    assert values["format"] == "flac"
    assert values["quality"] == "8"
    assert values["track_numbers"] == []
    assert values["requested_output_dir"] == str(settings.output_dir)


def test_validate_job_request_rejects_bad_format(settings):
    with pytest.raises(RipError):
        validate_job_request({"format": "mp3-please"}, settings)


def test_validate_job_request_rejects_output_outside_the_roots(settings):
    with pytest.raises(RipError):
        validate_job_request({"output_dir": "/etc"}, settings)


def test_validate_job_request_rejects_bad_templates(settings):
    with pytest.raises(RipError):
        validate_job_request({"track_file_template": "{nope}"}, settings)


def test_validate_job_request_cleans_track_numbers(settings):
    values = validate_job_request({"tracks": ["2", 2, 100, "x", "-1", 0, 9]}, settings)
    assert values["track_numbers"] == [2, 9]  # deduplicated, sorted, in range


def test_validate_job_request_normalizes_quality(settings):
    values = validate_job_request({"format": "mp3", "quality": "not-a-quality"}, settings)
    assert values["quality"] == "v0"
    assert validate_job_request({"format": "mp3", "quality": "320"}, settings)["quality"] == "320"


def test_make_job_creates_a_queued_job(settings):
    job = make_job({"tracks": [1, 2], "format": "ogg", "quality": "q8"}, settings, auto=True)
    assert job.state == "queued"
    assert job.auto is True
    assert job.track_numbers == [1, 2]
    assert job.quality == "q8"
    assert job.selected_numbers == [1, 2]


def test_job_roundtrip_through_dict(settings, toc):
    job = make_job({"tracks": [1], "format": "flac"}, settings)
    job.tracks = []
    from ripcd.ripper import TrackProgress

    job.tracks = [TrackProgress(number=1, title="Track 01", state="done", rip_percent=100, encode_percent=100)]
    payload = job.to_dict()
    # the job itself is still queued, so it is not at 100% yet
    assert payload["percent"] == 99.9
    assert payload["track_numbers"] == [1]
    job.state = "done"
    assert job.to_dict()["percent"] == 100.0
    clone = RipJob.from_dict(job.to_dict())
    assert clone.id == job.id
    assert clone.tracks[0].title == "Track 01"
    assert clone.track_numbers == [1]


def test_job_cancel_and_progress(settings):
    job = make_job({"tracks": [1, 2]}, settings)
    assert not job.cancel_requested()
    job.request_cancel()
    assert job.cancel_requested()
    assert job.is_active
    from ripcd.ripper import TrackProgress

    job.tracks = [
        TrackProgress(number=1, state="done", rip_percent=100, encode_percent=100),
        TrackProgress(number=2, state="ripping", rip_percent=50),
    ]
    job.recompute()
    assert 60 < job.percent < 99
    job.state = "done"
    assert job.recompute() == 100.0


def test_job_log_is_bounded(settings):
    job = make_job({}, settings)
    for index in range(600):
        job.add_log(f"line {index}")
    assert len(job.log) <= 500
    assert "line 599" in job.log[-1]
