"""
Whole-album Soulseek folders.

An album searched track by track arrives as one peer's FLAC, another's MP3 and
a third's remaster, which Navidrome then splits into several albums. These
check that slskd.resolve_album_folder only offers a folder that is one peer's
complete copy of the record in one encoding, and turns down everything else.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slskd
from test_slskd_search import _FakeClient


TITLES = ["Airbag", "Paranoid Android", "Karma Police"]


def _file(path, *, ext="flac", bitrate=1000, length=280):
    return {
        "filename": path,
        "size": 30_000_000,
        "length": length,
        "isLocked": False,
        "bitRate": bitrate,
        "bitDepth": 16,
        "sampleRate": 44100,
        "extension": ext,
    }


def _peer(username, files, *, free_slot=True, speed=2_000_000):
    return {
        "username": username,
        "hasFreeUploadSlot": free_slot,
        "uploadSpeed": speed,
        "files": files,
    }


def _album(folder, ext="flac", bitrate=1000, titles=TITLES):
    return [
        _file(f"{folder}\\{n:02d} - {title}.{ext}", ext=ext, bitrate=bitrate)
        for n, title in enumerate(titles, 1)
    ]


@pytest.fixture
def slskd_album(monkeypatch):
    """A fake slskd that answers with whatever peers the test hands it."""
    monkeypatch.setattr(slskd, "slskd_enabled", lambda *a, **k: True)
    monkeypatch.setattr(slskd, "get_slskd_token", lambda: "token")
    monkeypatch.setattr(slskd, "get_setting", lambda *a, **k: "http://slskd.test")
    monkeypatch.setattr(slskd.time, "sleep", lambda _s: None)
    monkeypatch.setattr(slskd, "SLSKD_REQUIRE_FREE_SLOT", False)

    def _install(responses):
        monkeypatch.setattr(slskd.httpx, "Client", lambda **_kw: _FakeClient(responses))

    return _install


def test_a_complete_lossless_folder_supplies_every_track(slskd_album):
    folder = "Music\\Radiohead\\OK Computer (1997) [FLAC]"
    extras = [_file(f"{folder}\\13 - Lucky (Live).flac")]
    slskd_album([_peer("generous_stranger", _album(folder) + extras)])

    picks, lossless = slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES)

    assert lossless is True
    assert sorted(picks) == [0, 1, 2]
    assert {p["slskd_username"] for p in picks.values()} == {"generous_stranger"}
    assert picks[1]["slskd_filename"].endswith("02 - Paranoid Android.flac")
    assert picks[1]["source"] == "soulseek"
    assert picks[1]["video_id"].startswith("slskd_")
    assert picks[1]["source_url"].startswith("soulseek://generous_stranger/")


def test_a_folder_missing_a_track_is_not_offered(slskd_album):
    folder = "Radiohead\\OK Computer"
    slskd_album([_peer("half_a_record", _album(folder, titles=TITLES[:2]))])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_bracketed_format_tags_do_not_hide_the_album(slskd_album):
    folder = "Rock\\Radiohead\\Radiohead - 1997 - OK Computer [16-44]"
    slskd_album([_peer("tidy_tagger", _album(folder))])

    picks, _lossless = slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES)

    assert len(picks) == 3


def test_a_live_album_with_plain_track_names_is_not_the_studio_one(slskd_album):
    slskd_album([_peer("taper", _album("Radiohead\\OK Computer (Live)"))])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_a_mixed_encoding_folder_is_not_offered(slskd_album):
    folder = "Radiohead\\OK Computer"
    files = _album(folder, titles=TITLES[:2]) + _album(
        folder, ext="mp3", bitrate=320, titles=TITLES[2:]
    )
    slskd_album([_peer("magpie", files)])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_an_mp3_320_folder_counts_but_says_it_is_not_lossless(slskd_album):
    slskd_album([_peer("mp3_hoarder", _album("Radiohead\\OK Computer", ext="mp3", bitrate=320))])

    picks, lossless = slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES)

    assert lossless is False
    assert len(picks) == 3


def test_a_lower_bitrate_mp3_folder_is_not_offered(slskd_album):
    slskd_album([_peer("dialup_era", _album("Radiohead\\OK Computer", ext="mp3", bitrate=192))])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_lossless_beats_a_faster_mp3_peer(slskd_album):
    slskd_album([
        _peer("mp3_rocket", _album("Radiohead\\OK Computer", ext="mp3", bitrate=320), speed=50_000_000),
        _peer("flac_tortoise", _album("Radiohead\\OK Computer"), speed=200_000),
    ])

    picks, lossless = slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES)

    assert lossless is True
    assert {p["slskd_username"] for p in picks.values()} == {"flac_tortoise"}


def test_disc_subfolders_count_as_one_album(slskd_album):
    root = "Radiohead\\OK Computer"
    files = _album(f"{root}\\CD1", titles=TITLES[:2]) + _album(f"{root}\\CD2", titles=TITLES[2:])
    slskd_album([_peer("box_set_owner", files)])

    picks, _lossless = slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES)

    assert len(picks) == 3


def test_a_live_take_does_not_stand_in_for_the_studio_track(slskd_album):
    folder = "Radiohead\\OK Computer"
    files = _album(folder, titles=["Airbag", "Paranoid Android (Live)", "Karma Police"])
    slskd_album([_peer("bootlegger", files)])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_another_album_with_the_same_songs_is_not_this_one(slskd_album):
    slskd_album([_peer("compiler", _album("Radiohead\\The Best Of"))])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_someone_elses_album_of_the_same_name_is_not_this_one(slskd_album):
    slskd_album([_peer("namesake", _album("Coverband\\OK Computer"))])

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None


def test_soulseek_switched_off_never_searches(monkeypatch):
    monkeypatch.setattr(slskd, "slskd_enabled", lambda *a, **k: False)
    monkeypatch.setattr(
        slskd, "_run_slskd_search",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched a disabled source")),
    )

    assert slskd.resolve_album_folder("Radiohead", "OK Computer", TITLES) is None
