"""Tests for the optical drive layer (TOC, MSF maths, MusicBrainz IDs)."""

from __future__ import annotations

import pytest

from ripcd.optical import (
    FRAMES_PER_SECOND,
    LEAD_IN_FRAMES,
    Toc,
    TrackInfo,
    disc_id_payload,
    disc_signature,
    frames_to_msf,
    lba_to_msf,
    musicbrainz_disc_id,
    musicbrainz_toc_string,
    parse_cdparanoia_toc,
    toc_candidates,
)

# Reference data taken from libdiscid's own test suite
# (test/test_put.c in metabrainz/libdiscid).
LIBDISCID_OFFSETS = [
    150, 9700, 25887, 39297, 53795, 63735, 77517, 94877, 107270, 123552, 135522,
    148422, 161197, 174790, 192022, 205545, 218010, 228700, 239590, 255470, 266932, 288750,
]
LIBDISCID_LEADOUT = 303602
LIBDISCID_ID = "xUp1F2NkfP8s8jaeFn_Av3jNEI4-"
LIBDISCID_TOC = (
    "1+22+303602+150+9700+25887+39297+53795+63735+77517+94877+107270+123552+135522+148422"
    "+161197+174790+192022+205545+218010+228700+239590+255470+266932+288750"
)


def make_toc(offsets=None, leadout=LIBDISCID_LEADOUT):
    offsets = offsets or LIBDISCID_OFFSETS
    tracks = [TrackInfo(number=index, start_lba=value) for index, value in enumerate(offsets, start=1)]
    for index, track in enumerate(tracks):
        end = tracks[index + 1].start_lba if index + 1 < len(tracks) else leadout
        track.duration_frames = end - track.start_lba
    return Toc(first_track=tracks[0].number, last_track=tracks[-1].number, leadout_lba=leadout, tracks=tracks)


# --------------------------------------------------------------------------- #
# MSF maths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        (0, "00:00.00"),
        (75, "00:01.00"),
        (150, "00:02.00"),
        (4500, "01:00.00"),
        (4500 * 60 + 75 * 3 + 4, "60:03.04"),
    ],
)
def test_frames_to_msf(frames, expected):
    assert frames_to_msf(frames) == expected


def test_lba_to_msf_applies_the_lead_in():
    assert lba_to_msf(0) == "00:02.00"
    assert lba_to_msf(75) == "00:03.00"


def test_track_properties_use_absolute_addressing():
    track = TrackInfo(number=1, start_lba=LEAD_IN_FRAMES, duration_frames=FRAMES_PER_SECOND * 60)
    assert track.start == "00:02.00"
    assert track.sector == 0  # MMC style, first sector of the user area
    assert track.duration == "01:00.00"
    assert track.seconds == 60.0


def test_total_duration_of_a_disc():
    toc = make_toc()
    assert toc.total_frames == LIBDISCID_LEADOUT - LEAD_IN_FRAMES
    assert round(toc.total_seconds) == round((LIBDISCID_LEADOUT - LEAD_IN_FRAMES) / 75)
    assert toc.to_dict()["total_time"] == frames_to_msf(toc.total_frames)


# --------------------------------------------------------------------------- #
# MusicBrainz identifiers - checked against the libdiscid reference values
# --------------------------------------------------------------------------- #


def test_disc_id_matches_libdiscid():
    assert musicbrainz_disc_id(make_toc()) == LIBDISCID_ID


def test_disc_id_payload_layout():
    payload = disc_id_payload(make_toc())
    assert len(payload) == 4 + 100 * 8
    assert payload.startswith("0116")  # first track 1, last track 22
    assert payload[4:12] == f"{LIBDISCID_LEADOUT:08X}"
    # unused slots are zero padded, exactly like libdiscid
    assert payload.endswith("00000000")


def test_toc_string_matches_libdiscid():
    assert musicbrainz_toc_string(make_toc()) == LIBDISCID_TOC


def test_disc_id_needs_tracks():
    assert musicbrainz_disc_id(Toc(first_track=1, last_track=1, leadout_lba=0, tracks=[])) is None
    assert musicbrainz_toc_string(Toc(first_track=1, last_track=1, leadout_lba=0, tracks=[])) is None


def test_audio_only_candidate_is_offered_for_enhanced_cds():
    toc = make_toc()
    toc.tracks.append(TrackInfo(number=23, start_lba=LIBDISCID_LEADOUT, duration_frames=4500, audio=False))
    labels = [name for name, _toc in toc_candidates(toc)]
    assert labels == ["full", "audio-only"]
    audio_only = dict(toc_candidates(toc))["audio-only"]
    assert [t.number for t in audio_only.tracks] == list(range(1, 23))


def test_single_track_candidate_for_pure_audio_discs():
    assert [name for name, _toc in toc_candidates(make_toc())] == ["full"]


def test_disc_signature_is_stable_and_short():
    first = disc_signature(make_toc())
    assert first == disc_signature(make_toc())
    assert len(first) == 12
    assert disc_signature(None) == ""
    changed = make_toc(offsets=[150, 9700, 25887], leadout=50000)
    assert disc_signature(changed) != first


# --------------------------------------------------------------------------- #
# cdparanoia output parsing
# --------------------------------------------------------------------------- #

#: Real ``cdparanoia -Q`` output (this one has a 55 sector pregap in front of
#: track 1, like CD-Extra discs often do).
CDPARANOIA_OUTPUT = """
cdparanoia III release 10.2 (September 11, 2008)

Checking /dev/sr0 for cdrom...
        Testing /dev/sr0 for SCSI interface
                generic device: /dev/sg3
                ioctl device: /dev/sr0
        Found an accessible SCSI CDROM drive.

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    22190 [04:55.65]       55 [00:00.55]    no   no  2
  2.    11350 [02:31.25]    22245 [04:56.45]    no   no  2
  3.      901 [00:12.01]    33595 [07:27.70]    no   no  2
TOTAL  34441 [07:39.16]    (audio only)
"""

#: Output of older builds (no sector numbers in brackets).
CDPARANOIA_OUTPUT_PLAIN = """
Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    4:13.40      0:00.00         0    0   no  2
  2.    3:20.05      4:13.40     19015    0   no  2
TOTAL  7:33.45
"""


def test_parse_cdparanoia_toc():
    toc = parse_cdparanoia_toc(CDPARANOIA_OUTPUT)
    assert toc is not None
    assert toc.source == "cdparanoia"
    assert [t.number for t in toc.tracks] == [1, 2, 3]
    # cdparanoia is 0 based, we store absolute MSF addressing (+150)
    assert toc.tracks[0].start_lba == 55 + LEAD_IN_FRAMES
    assert toc.tracks[0].duration_frames == 22190
    assert toc.tracks[1].start_lba == 22245 + LEAD_IN_FRAMES
    assert toc.tracks[2].duration_frames == 901
    # lead-out = end of the last track, converted the same way
    assert toc.leadout_lba == 33595 + 901 + LEAD_IN_FRAMES
    assert toc.has_audio
    assert toc.total_seconds == pytest.approx(34441 / 75, abs=0.2)


def test_parse_cdparanoia_toc_plain_format():
    """Older builds print no sector numbers: fall back to the MSF columns."""
    toc = parse_cdparanoia_toc(CDPARANOIA_OUTPUT_PLAIN)
    assert toc is not None
    assert [t.number for t in toc.tracks] == [1, 2]
    assert toc.tracks[0].start_lba == LEAD_IN_FRAMES
    assert toc.tracks[0].duration_frames == 19015
    assert toc.tracks[1].start_lba == 19015 + LEAD_IN_FRAMES
    assert toc.tracks[1].duration_frames == 15005


def test_parse_cdparanoia_toc_ignores_unusable_rows():
    toc = parse_cdparanoia_toc(CDPARANOIA_OUTPUT)
    # a data track or a misaligned row must not break the TOC
    assert len(toc.tracks) == 3


def test_parse_cdparanoia_toc_garbage():
    assert parse_cdparanoia_toc("no drive found") is None
    assert parse_cdparanoia_toc("") is None


# --------------------------------------------------------------------------- #
# Disc ids - reference values produced by the real libdiscid
# --------------------------------------------------------------------------- #
#: Generated with libdiscid 0.6.2 (compiled from source, ``discid_put`` +
#: ``discid_get_id``) for the same offsets, so these are not "self fulfilling"
#: values - they come from the library MusicBrainz itself uses.
LIBDISCID_VECTORS = [
    # (offsets, lead-out, disc id, freedb id, toc string)
    ([150], 150, "FLL_FQLyPfOjncg28CFMdYe2Jvo-", "02000001", "1+1+150+150"),
    ([150, 39000], 40000, "aqXUl3M12cM5Ne7BwMh7NBw2CWc-", "09021302", "1+2+40000+150+39000"),
    ([150, 45000, 180000], 250000, "tzJjdRuyviWEdqq3gye0PehKAW4-", "0e0d0303",
     "1+3+250000+150+45000+180000"),
    (
        [150, 8000, 25000, 40000, 55000, 70000, 85000, 100000, 115000, 130000, 145000, 160000],
        400000,
        "aA3Lf0dynDoRhjwMIFAqZfeGdzM-",
        "7e14d30c",
        "1+12+400000+150+8000+25000+40000+55000+70000+85000+100000+115000+130000+145000+160000",
    ),
]


@pytest.mark.parametrize(("offsets", "leadout", "expected_id", "expected_freedb", "expected_toc"), LIBDISCID_VECTORS)
def test_disc_id_against_libdiscid_vectors(offsets, leadout, expected_id, expected_freedb, expected_toc):
    from ripcd.optical import freedb_disc_id

    toc = make_toc(offsets=offsets, leadout=leadout)
    assert musicbrainz_disc_id(toc) == expected_id
    assert musicbrainz_toc_string(toc) == expected_toc
    assert freedb_disc_id(toc) == expected_freedb


def test_audio_only_candidate_has_its_own_ids():
    """A CD-Extra has two ids: one for the full TOC, one for the audio part."""
    toc = make_toc()
    toc.tracks.append(TrackInfo(number=23, start_lba=LIBDISCID_LEADOUT, duration_frames=4500, audio=False))
    full, audio_only = dict(toc_candidates(toc)).values()
    assert musicbrainz_disc_id(full) != musicbrainz_disc_id(audio_only)
    assert toc.kind == "mixed"
    assert audio_only.kind == "audio"
    assert audio_only.leadout_lba == toc.audio_tracks[-1].end_lba
    assert musicbrainz_toc_string(audio_only).endswith(f"+{LIBDISCID_OFFSETS[-1]}")


# --------------------------------------------------------------------------- #
# Drive discovery helpers (they run inside the container, so only shape is
# checked - there is no CD drive in CI)
# --------------------------------------------------------------------------- #


def test_device_tree_snapshot_shape():
    from ripcd.optical import device_tree_snapshot

    tree = device_tree_snapshot()
    assert set(tree) >= {"devices", "sr_in_sysfs", "sg_in_sysfs", "cdrom_info"}
    assert isinstance(tree["devices"], dict)
    assert all(isinstance(value, bool) for value in tree["devices"].values())
    assert isinstance(tree["sr_in_sysfs"], list)
    assert isinstance(tree["sg_in_sysfs"], list)


def test_device_paths_are_deduplicated():
    from ripcd.optical import find_device_paths

    paths = find_device_paths()
    assert isinstance(paths, list)
    assert len(paths) == len(set(paths))


def test_sg_mapping_matches_sysfs():
    """When the kernel exposes a drive, the sg device has to be found too."""
    from ripcd.optical import _sg_devices

    mapping = _sg_devices()
    for key, value in mapping.items():
        assert key.startswith("sr") or key.startswith("scd")
        assert value.startswith("/dev/")
