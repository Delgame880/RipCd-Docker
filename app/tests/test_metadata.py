"""Tests for MusicBrainz parsing, cover lookup helpers and override handling."""

from __future__ import annotations

import json

import pytest

from ripcd.metadata import (
    AlbumMetadata,
    MetadataResolver,
    MusicBrainzClient,
    TrackMetadata,
    parse_release,
    pick_best_release,
    unknown_album,
)
from ripcd.optical import Toc, TrackInfo
from ripcd.ripper import apply_overrides

RELEASE = {
    "id": "0f2b7b7d-0000-4000-8000-000000000001",
    "title": "Chromatic Night",
    "status": "Official",
    "date": "1998-05-11",
    "country": "GB",
    "barcode": "0123456789012",
    "artist-credit": [{"name": "The Demo Band"}],
    "release-group": {"id": "rg-1234", "title": "Chromatic Night", "first-release-date": "1998"},
    "label-info": [{"catalog-number": "DEMO-001", "label": {"name": "Demo Records"}}],
    "genres": [{"name": "alternative rock", "count": 3}],
    "cover-art-archive": {"front": True},
    "media": [
        {
            "position": 1,
            "format": "CD",
            "track-count": 3,
            "tracks": [
                {"id": "t1", "number": "1", "title": "Opening Signal", "length": 232000,
                 "recording": {"id": "r1", "title": "Opening Signal", "length": 232000}},
                {"id": "t2", "number": "2", "title": "Chromatic Night", "length": 284000,
                 "recording": {"id": "r2", "title": "Chromatic Night"}, "artist-credit": [{"name": "Guest Singer"}]},
                {"id": "t3", "number": "3", "title": "Paper Lanterns", "length": 198000,
                 "recording": {"id": "r3"}},
            ],
        }
    ],
}


def make_toc(count=3):
    tracks = []
    position = 150
    for number in range(1, count + 1):
        tracks.append(TrackInfo(number=number, start_lba=position, duration_frames=75 * 100))
        position += 75 * 100
    return Toc(first_track=1, last_track=count, leadout_lba=position, tracks=tracks)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_parse_release_extracts_everything():
    album = parse_release(RELEASE, make_toc(), disc_id="abc123")
    assert album.title == "Chromatic Night"
    assert album.album_artist == "The Demo Band"
    assert album.year == "1998"
    assert album.date == "1998-05-11"
    assert album.genre == "Alternative Rock"
    assert album.label == "Demo Records"
    assert album.catalog_number == "DEMO-001"
    assert album.barcode == "0123456789012"
    assert album.release_group_id == "rg-1234"
    assert album.disc_id == "abc123"
    assert album.total_tracks == 3
    assert album.cover_url.endswith("/front-500")
    assert [t.title for t in album.tracks] == ["Opening Signal", "Chromatic Night", "Paper Lanterns"]
    # a per track artist credit overrides the album artist
    assert album.track(2).artist == "Guest Singer"
    assert album.track(1).artist == "The Demo Band"
    assert album.track(1).length_ms == 232000
    assert album.track(1).recording_id == "r1"


def test_parse_release_maps_metadata_onto_the_track_numbers():
    """A disc that starts at track 4 keeps its own numbering (bonus discs)."""
    toc = make_toc(3)
    for offset, track in enumerate(toc.tracks):
        track.number += 3  # tracks 4, 5 and 6
    album = parse_release(RELEASE, toc)
    assert [t.number for t in album.tracks] == [4, 5, 6]
    assert album.track(4).title == "Opening Signal"
    assert album.track(5).title == "Chromatic Night"  # second metadata entry


def test_parse_release_handles_fewer_metadata_tracks_than_the_disc():
    album = parse_release(RELEASE, make_toc(5))
    assert album.total_tracks == 5
    assert album.track(4).title == "Track 04"
    assert any("MusicBrainz lists" in warning for warning in album.warnings)


def test_parse_release_survives_an_empty_release():
    album = parse_release({}, make_toc(2))
    assert album.title == "Unknown Album"
    assert album.album_artist == "Unknown Artist"
    assert len(album.tracks) == 2


def test_pick_best_release_prefers_exact_track_count():
    wrong = dict(RELEASE, id="wrong", media=[{"position": 1, "track-count": 9, "tracks": []}], status="Official")
    right = dict(RELEASE, id="right")
    assert pick_best_release([wrong, right], 3)["id"] == "right"
    assert pick_best_release([], 3) is None
    assert pick_best_release([None, {}], 3) is None  # no usable release


def test_unknown_album_fills_in_track_numbers():
    toc = make_toc(4)
    album = unknown_album(toc, reason="offline")
    assert album.source == "none"
    assert [t.title for t in album.tracks] == ["Track 01", "Track 02", "Track 03", "Track 04"]
    assert album.disc_id
    assert album.warnings == ["offline"]


def test_album_roundtrip_through_dict():
    album = parse_release(RELEASE, make_toc())
    clone = AlbumMetadata.from_dict(json.loads(json.dumps(album.to_dict())))
    assert clone.title == album.title
    assert [t.title for t in clone.tracks] == [t.title for t in album.tracks]
    assert clone.album_dir_context()["album"] == "Chromatic Night"


# --------------------------------------------------------------------------- #
# overrides
# --------------------------------------------------------------------------- #


def test_apply_overrides_wins_over_metadata():
    album = parse_release(RELEASE, make_toc())
    edited = apply_overrides(album, {"album": "My Title", "album_artist": "Me", "year": "2001", "genre": "Jazz"})
    assert edited.title == "My Title"
    assert edited.album_artist == "Me"
    assert edited.year == "2001"
    assert edited.date == "2001"
    assert edited.genre == "Jazz"
    assert edited.source.endswith("+manual")
    # the original stays untouched
    assert album.title == "Chromatic Night"


def test_apply_overrides_accepts_track_titles():
    album = parse_release(RELEASE, make_toc())
    edited = apply_overrides(album, {}, None, {"1": "Renamed", "9": "New Track"})
    assert edited.track(1).title == "Renamed"
    assert edited.track(9).title == "New Track"
    assert [t.number for t in edited.tracks] == [1, 2, 3, 9]


def test_apply_overrides_marks_manual_metadata():
    album = unknown_album(make_toc(1))
    edited = apply_overrides(album, {"album": "Typed by hand"})
    assert edited.source == "manual"


def test_overrides_ignore_blank_values():
    album = parse_release(RELEASE, make_toc())
    edited = apply_overrides(album, {"album": "  ", "genre": ""})
    assert edited.title == "Chromatic Night"


# --------------------------------------------------------------------------- #
# client & resolver (no network)
# --------------------------------------------------------------------------- #


class FakeClient(MusicBrainzClient):
    def __init__(self, releases=None, fail=False):
        super().__init__(cache_dir=None)
        self.releases = releases or []
        self.fail = fail
        self.calls = []

    def lookup_disc_id(self, disc_id):
        self.calls.append(("discid", disc_id))
        if self.fail:
            raise RuntimeError("network down")
        return self.releases

    def lookup_toc(self, toc_string):
        self.calls.append(("toc", toc_string))
        return self.releases

    def lookup_barcode(self, barcode):
        self.calls.append(("barcode", barcode))
        return self.releases


def test_resolver_falls_back_to_unknown_album():
    client = FakeClient(releases=[])
    album = client.resolve(make_toc())
    assert album.source == "none"
    assert any("no MusicBrainz release found" in warning for warning in album.warnings)


def test_resolver_uses_a_match():
    client = FakeClient(releases=[RELEASE])
    album = client.resolve(make_toc())
    assert album.source == "musicbrainz"
    assert album.title == "Chromatic Night"
    assert album.confidence == 1.0


def test_resolver_reports_network_errors():
    client = FakeClient(fail=True)
    album = client.resolve(make_toc())
    assert album.source == "none"
    assert any("network down" in warning for warning in album.warnings)


def test_metadata_resolver_cache_and_forget():
    client = FakeClient(releases=[RELEASE])
    resolver = MetadataResolver(client)
    toc = make_toc()
    signature = "sig-1"
    album = resolver.resolve_now(signature, toc)
    assert resolver.get(signature) is album
    resolver.resolve_now(signature, toc)
    assert len(client.calls) == 1  # served from the cache the second time
    resolver.forget(signature)
    assert resolver.get(signature) is None


def test_track_context_values():
    album = parse_release(RELEASE, make_toc())
    context = album.track_context(2, track_total=3)
    assert context["track"] == 2
    assert context["title"] == "Chromatic Night"
    assert context["track_artist"] == "Guest Singer"
    assert context["album"] == "Chromatic Night"
    assert context["total_tracks"] == 3


def test_trackmetadata_from_dict_defaults():
    meta = TrackMetadata.from_dict({})
    assert meta.number == 0
    assert meta.title == ""
    assert meta.length_ms is None


def test_client_cache_roundtrip(tmp_path):
    client = MusicBrainzClient(cache_dir=tmp_path, cache_ttl=3600)
    album = parse_release(RELEASE, make_toc())
    client._cache_write("discid-test", album.to_dict())
    cached = client._cache_read("discid-test")
    assert cached["title"] == "Chromatic Night"
    # an expired cache entry must be ignored
    cached["fetched_at"] = 0
    client._cache_write("discid-test", cached)
    stale = MusicBrainzClient(cache_dir=tmp_path, cache_ttl=1)
    assert stale._cache_read("discid-test") is None


def test_resolver_rejects_traversal_in_cache_keys(tmp_path):
    client = MusicBrainzClient(cache_dir=tmp_path)
    path = client._cache_path("../../etc/passwd")
    assert path is not None
    assert path.parent == tmp_path
    assert ".." not in path.name


@pytest.mark.parametrize("disc_id", ["", None])
def test_resolve_without_disc_id_and_without_releases(disc_id):
    client = FakeClient(releases=[])
    album = client.resolve(make_toc())
    assert album.disc_id  # computed from the TOC even without a match
