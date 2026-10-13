"""Album duplicate matching stays fuzzy inside Albums and nowhere else."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import albums


def _album_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "Albums"
    root.mkdir()
    monkeypatch.setattr(albums, "get_albums_dir", lambda user_id=None: root)
    monkeypatch.setattr(
        albums,
        "get_setting_bool",
        lambda name, default=False, user_id=None: True,
    )
    return root


def test_equivalent_album_folder_and_numbered_track_are_reused(monkeypatch, tmp_path):
    root = _album_root(monkeypatch, tmp_path)
    existing_dir = root / "Knife Party" / "Lost Souls EP"
    existing_dir.mkdir(parents=True)
    existing_file = existing_dir / "01 - Ghost Train.flac"
    existing_file.write_bytes(b"audio")

    status = albums.album_track_status(
        "Knife Party",
        "Lost Souls",
        [{"position": "1", "title": "Ghost Train", "isrc": None}],
    )

    assert status["album_dir"] == existing_dir
    assert status["existing_tracks"][0]["title"] == "Ghost Train"
    assert albums.find_existing_album_track(
        "Knife Party", "Lost Souls", "Ghost Train"
    ) == existing_file


def test_punctuation_only_artist_and_album_differences_match(monkeypatch, tmp_path):
    root = _album_root(monkeypatch, tmp_path)
    existing_dir = root / "AC DC" / "Back-in Black"
    existing_dir.mkdir(parents=True)
    track = existing_dir / "Hells Bells.mp3"
    track.write_bytes(b"audio")

    assert albums.find_existing_album_track(
        "AC/DC", "Back in Black", "Hell's Bells"
    ) == track


def test_lookalike_single_outside_albums_is_never_considered(monkeypatch, tmp_path):
    _album_root(monkeypatch, tmp_path)
    unrelated = tmp_path / "Singles" / "Knife Party"
    unrelated.mkdir(parents=True)
    (unrelated / "Ghost Train.mp3").write_bytes(b"audio")

    assert albums.find_existing_album_track(
        "Knife Party", "Lost Souls", "Ghost Train"
    ) is None
    assert albums.album_on_disk("Knife Party", "Lost Souls") is False


def test_album_audio_count_is_confined_to_matching_album_folder(monkeypatch, tmp_path):
    root = _album_root(monkeypatch, tmp_path)
    album_dir = root / "RAYE" / "My 21st Century Blues"
    album_dir.mkdir(parents=True)
    (album_dir / "Escapism.flac").write_bytes(b"audio")
    (album_dir / "Hard Out Here.opus").write_bytes(b"audio")
    (album_dir / "cover.jpg").write_bytes(b"image")
    unrelated = root / "RAYE" / "Another Album"
    unrelated.mkdir()
    (unrelated / "Elsewhere.mp3").write_bytes(b"audio")

    assert albums.album_audio_file_count("RAYE", "My 21st Century Blues") == 2
    assert albums.album_on_disk("RAYE", "My 21st Century Blues") is True


@pytest.mark.parametrize(
    ("raw_status", "import_status", "actual", "expected", "display_status"),
    [
        ("queued", "completed", 15, 15, "complete"),
        ("queued", "completed", 12, 15, "incomplete"),
        ("queued", "processing", 12, 15, "downloading"),
        ("queued", "pending", 0, 15, "queued"),
        ("seen", None, 9, None, "on_disk"),
        ("queued", "completed", 0, 15, "failed"),
        ("queued", "cancelled", 0, 15, "cancelled"),
    ],
)
def test_watched_album_display_state_uses_live_disk_and_import_truth(
    raw_status, import_status, actual, expected, display_status
):
    state = albums.watched_album_display_state(
        raw_status, import_status, actual, expected
    )

    assert state["display_status"] == display_status
    assert state["audio_file_count"] == actual
    assert state["expected_track_count"] == expected
    assert state["on_disk"] is (actual > 0)


def test_completed_album_replaces_historical_queued_label():
    state = albums.watched_album_display_state(
        "queued", "completed", 17, 17
    )

    assert state == {
        "display_status": "complete",
        "status_detail": "17/17 tracks on disk",
        "on_disk": True,
        "audio_file_count": 17,
        "expected_track_count": 17,
    }
