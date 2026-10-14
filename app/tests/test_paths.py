"""Tests for path sandboxing, the folder browser and file naming templates."""

from __future__ import annotations

import os

import pytest

from ripcd.paths import (
    PathNotAllowed,
    check_path,
    deepest_existing,
    human_bytes,
    is_within,
    list_directory,
    render_template,
    safe_filename,
    sanitize_component,
    sanitize_relative_path,
    validate_template,
)


# --------------------------------------------------------------------------- #
# sanitizing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Normal Title", "Normal Title"),
        ("AC/DC", "AC_DC"),
        ('He said "hi"', "He said _hi_"),
        ("a\\b:c*d?e", "a_b_c_d_e"),
        ("  padded  ", "padded"),
        ("trailing dots...", "trailing dots"),
        ("tab\tseparated", "tab separated"),
        ("new\nline", "new line"),
        ("", "Unknown"),
        ("///", "Unknown"),
        ("con", "con_"),          # reserved on Windows shares
    ],
)
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_component_length_limit():
    assert len(sanitize_component("x" * 500)) == 120


def test_sanitize_relative_path_removes_traversal():
    assert sanitize_relative_path("../../etc/passwd") == "etc/passwd"
    assert sanitize_relative_path("Artist/Album") == "Artist/Album"
    assert sanitize_relative_path("/absolute/./path/") == "absolute/path"
    assert sanitize_relative_path("") == "Unknown"


def test_safe_filename_keeps_one_extension():
    assert safe_filename("03 - Song", "flac") == "03 - Song.flac"
    assert safe_filename("03 - Song.flac", "flac") == "03 - Song.flac"
    assert safe_filename("weird/name:here", "mp3") == "name_here.mp3"
    assert safe_filename("", "ogg") == "track.ogg"


def test_human_bytes():
    assert human_bytes(None) == "unknown"
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024**3).endswith("GB")


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #


def test_render_template_with_format_spec():
    context = {"album": "The Album", "album_artist": "The Artist", "track": 3, "title": "A Song", "year": "1999"}
    assert render_template("{album_artist}/{album}", context) == "The Artist/The Album"
    assert render_template("{track:02d} - {title}", context) == "03 - A Song"
    assert render_template("{year} - {album}", context) == "1999 - The Album"


def test_render_template_uses_fallback_for_missing_values():
    assert render_template("{track:02d} - {title}", {"track": 1}) == "01 - Unknown"
    assert render_template("{album}", {"album": ""}) == "Unknown"


def test_validate_template_rejects_unknown_fields():
    validate_template("{album}/{title}")
    with pytest.raises(Exception):
        validate_template("{album}/{nope}")
    with pytest.raises(Exception):
        validate_template("")


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #


def test_is_within(tmp_path):
    root = tmp_path / "media"
    inner = root / "Music" / "Album"
    inner.mkdir(parents=True)
    assert is_within(str(inner), [str(root)])
    assert is_within(str(root), [str(root)])
    assert not is_within("/etc", [str(root)])
    # a sibling directory that merely shares a prefix must not pass
    assert not is_within(str(tmp_path / "media-other"), [str(root)])


def test_is_within_blocks_symlink_escapes(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside)
    assert not is_within(str(link), [str(root)])


def test_check_path_reports_writable_output(tmp_path):
    root = tmp_path / "media"
    (root / "Music").mkdir(parents=True)
    check = check_path(str(root / "Music"), [str(root)])
    assert check.ok and check.writable
    assert check.free_bytes and check.total_bytes
    assert check.to_dict()["ok"] is True


def test_check_path_allows_not_yet_existing_album_folders(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    deep = root / "Artist" / "Album"
    check = check_path(str(deep), [str(root)])
    assert check.ok, check.error
    assert check.exists is False
    assert check.writable is True
    assert deepest_existing(str(deep)) == str(root)


def test_check_path_rejects_paths_outside_the_roots(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    check = check_path("/etc", [str(root)])
    assert check.ok is False
    assert "outside the folders" in check.error


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #


def test_list_directory_lists_only_folders_by_default(tmp_path):
    root = tmp_path / "media"
    (root / "Music").mkdir(parents=True)
    (root / "Video").mkdir()
    (root / "notes.txt").write_text("x")
    (root / ".hidden").mkdir()

    listing = list_directory(str(root), [str(root)])
    names = [entry["name"] for entry in listing["entries"]]
    assert names == ["Music", "Video"]
    assert listing["path"] == str(root)
    assert listing["root"] == str(root)
    assert listing["parent"] is None
    assert listing["writable"] is True

    with_files = list_directory(str(root), [str(root)], show_files=True)
    assert "notes.txt" in [entry["name"] for entry in with_files["entries"]]


def test_list_directory_builds_breadcrumbs(tmp_path):
    root = tmp_path / "media"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    listing = list_directory(str(album), [str(root)])
    assert [crumb["name"] for crumb in listing["breadcrumbs"]] == ["media", "Artist", "Album"]
    assert listing["parent"] == str(root / "Artist")


def test_list_directory_blocks_other_paths(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        list_directory("/etc", [str(root)])
    with pytest.raises(FileNotFoundError):
        list_directory(str(root / "nope"), [str(root)])


def test_list_directory_reports_free_space(tmp_path):
    listing = list_directory(str(tmp_path), [str(tmp_path)])
    assert isinstance(listing["free_bytes"], int)
    assert os.path.isdir(listing["path"])
