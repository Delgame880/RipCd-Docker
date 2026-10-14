"""Tests for the ffmpeg command builder, tagging and progress parsing."""

from __future__ import annotations

import pytest

from ripcd.config import FORMATS
from ripcd.encoder import (
    COVER_MODE,
    EncodeRequest,
    build_ffmpeg_command,
    codec_arguments,
    default_quality,
    output_extension,
    parse_ffmpeg_progress,
    resolve_quality,
    verify_output,
)

TAGS = {
    "title": "Song Title",
    "artist": "Track Artist",
    "album": "The Album",
    "album_artist": "The Album Artist",
    "date": "1997-03-01",
    "genre": "Rock",
    "track": 3,
    "track_total": 12,
    "disc": 1,
    "disc_total": 2,
    "musicbrainz_album_id": "release-mbid",
    "musicbrainz_discid": "discid-abc",
}


def request(**overrides):
    params = {
        "source_wav": "/tmp/track03.wav",
        "output": "/tmp/03 - Song.flac",
        "fmt": "flac",
        "quality": "8",
        "tags": TAGS,
        "cover": None,
    }
    params.update(overrides)
    return EncodeRequest(**params)


def metadata_for(command, key):
    """Value of a ``-metadata key=value`` pair in a command line."""
    for index, token in enumerate(command):
        if token != "-metadata":
            continue
        value = command[index + 1]
        if value.startswith(f"{key}="):
            return value.split("=", 1)[1]
    return None


def test_every_format_has_a_codec_and_an_extension():
    for name, spec in FORMATS.items():
        assert spec["ext"], name
        assert spec["default_quality"] in spec["qualities"], name
        assert output_extension(name) == spec["ext"]
        command = build_ffmpeg_command("ffmpeg", request(fmt=name, output=f"/tmp/out.{spec['ext']}"))
        assert command[command.index("-c:a") + 1], name
        assert codec_arguments(name, spec["default_quality"]) is not None


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError):
        codec_arguments("not-a-format", "")


@pytest.mark.parametrize(
    ("fmt", "quality", "expected"),
    [
        ("mp3", "320", ["-b:a", "320k"]),
        ("mp3", "v0", ["-q:a", "0", "-joint_stereo", "1"]),
        ("ogg", "q6", ["-q:a", "6"]),
        ("opus", "192", ["-b:a", "192k"]),
        ("flac", "8", ["-compression_level", "8"]),
        ("wav", "", []),
    ],
)
def test_quality_maps_to_codec_arguments(fmt, quality, expected):
    assert codec_arguments(fmt, quality) == expected


def test_bad_quality_falls_back_to_the_default():
    assert resolve_quality("mp3", "nonsense") == default_quality("mp3")
    assert resolve_quality("flac", None) == "8"


def test_vorbis_tags_use_vorbis_comment_names():
    command = build_ffmpeg_command("ffmpeg", request(fmt="flac"))
    assert metadata_for(command, "TITLE") == "Song Title"
    assert metadata_for(command, "ALBUMARTIST") == "The Album Artist"
    assert metadata_for(command, "TRACKNUMBER") == "3"
    assert metadata_for(command, "TRACKTOTAL") == "12"
    assert metadata_for(command, "DISCTOTAL") == "2"
    assert metadata_for(command, "MUSICBRAINZ_ALBUMID") == "release-mbid"
    # vorbis comments are case sensitive, no lowercase duplicates
    assert metadata_for(command, "title") is None


def test_id3_tags_merge_track_and_disc_numbers():
    command = build_ffmpeg_command("ffmpeg", request(fmt="mp3", output="/tmp/a.mp3", quality="v0"))
    assert metadata_for(command, "title") == "Song Title"
    assert metadata_for(command, "album_artist") == "The Album Artist"
    assert metadata_for(command, "track") == "3/12"
    assert metadata_for(command, "disc") == "1/2"
    # an ID3v2.3 tag is needed for maximum player compatibility
    assert command[command.index("-id3v2_version") + 1] == "3"


def test_cover_art_is_attached_for_supported_containers(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)

    flac = build_ffmpeg_command("ffmpeg", request(cover=str(cover)))
    assert flac[flac.index("-map") + 1] == "0:a"
    assert "attached_pic" in flac

    mp3 = build_ffmpeg_command("ffmpeg", request(fmt="mp3", output="/tmp/a.mp3", cover=str(cover)))
    assert "title=Album cover" in mp3
    assert "comment=Cover (front)" in mp3

    # Ogg/Opus cannot store attached pictures -> the cover stays a sidecar file
    ogg = build_ffmpeg_command("ffmpeg", request(fmt="ogg", output="/tmp/a.ogg", cover=str(cover)))
    assert "attached_pic" not in ogg
    assert "-vn" in ogg


def test_missing_cover_file_is_ignored():
    command = build_ffmpeg_command("ffmpeg", request(cover="/tmp/does-not-exist.jpg"))
    assert "attached_pic" not in command


def test_command_never_leaves_empty_arguments():
    for fmt in FORMATS:
        command = build_ffmpeg_command(
            "ffmpeg", request(fmt=fmt, output=f"/tmp/out.{output_extension(fmt)}", quality=default_quality(fmt))
        )
        assert "" not in command
        assert command[0] == "ffmpeg"
        assert command[-1].startswith("/tmp/out.")


def test_cover_modes_are_declared_for_every_format():
    for fmt in FORMATS:
        assert fmt in COVER_MODE


def test_parse_ffmpeg_progress_lines():
    assert parse_ffmpeg_progress("out_time_us=1500000") == ("out_time_us", "1500000")
    assert parse_ffmpeg_progress("progress=end") == ("progress", "end")
    assert parse_ffmpeg_progress("frame=12") is None
    assert parse_ffmpeg_progress("") is None


def test_verify_output(tmp_path):
    good = tmp_path / "good.flac"
    good.write_bytes(b"fLaC" + b"0" * 4096)
    assert verify_output(str(good)) == (True, "")
    assert verify_output(str(tmp_path / "missing.flac"))[0] is False
    tiny = tmp_path / "tiny.flac"
    tiny.write_bytes(b"fLaC")
    assert verify_output(str(tiny))[0] is False
