"""Unit tests for orphaned playlist file detection and rehoming.

Pure filesystem + a throwaway SQLite db; no server, no network, fast.
Covers watched_playlists.find_orphaned_playlist_files / move_orphans_to_singles
and the _locate_local_track_file helper that feeds the COMMENT-tagging fix.
"""
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watched_playlists as wp


def _fake_db(tmp_path: Path):
    """A minimal stand-in for db_conn with just the tables the scanner reads."""
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """CREATE TABLE watched_playlist_tracks (
               playlist_id TEXT, artist TEXT, title TEXT,
               resolved_path TEXT, removed_at TEXT, job_id TEXT)"""
    )
    conn.execute("CREATE TABLE jobs (id TEXT, artist TEXT, title TEXT)")
    conn.commit()
    conn.close()

    @contextmanager
    def fake_db_conn():
        c = sqlite3.connect(db_file)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    return fake_db_conn


def _setup(tmp_path, monkeypatch):
    """One watched playlist folder with one claimed and one orphaned track."""
    playlists_dir = tmp_path / "Playlists"
    folder = playlists_dir / "Chill"
    folder.mkdir(parents=True)
    claimed = folder / "Adele - Hello.flac"
    claimed.write_bytes(b"pretend flac")
    orphan = folder / "Rick Astley - Never Gonna Give You Up.flac"
    orphan.write_bytes(b"pretend flac, definitely not a rickroll")
    (folder / "Chill.m3u").write_text("#EXTM3U\n")  # non-audio: never an orphan

    fake_db_conn = _fake_db(tmp_path)
    with fake_db_conn() as c:
        c.execute(
            "INSERT INTO watched_playlist_tracks VALUES (?, ?, ?, NULL, NULL, NULL)",
            ("pl1", "Adele", "Hello"),
        )
        # The rickroll was mirror-removed upstream; its file lingers
        c.execute(
            "INSERT INTO watched_playlist_tracks VALUES (?, ?, ?, NULL, datetime('now'), NULL)",
            ("pl1", "Rick Astley", "Never Gonna Give You Up"),
        )
    monkeypatch.setattr(wp, "db_conn", fake_db_conn)
    monkeypatch.setattr(wp, "get_playlists_dir", lambda user_id=None: playlists_dir)

    playlist = {"id": "pl1", "name": "Chill", "user_id": None, "use_playlists_dir": 1,
                "custom_subdir": "", "sync_mode": "mirror"}
    return playlists_dir, folder, claimed, orphan, playlist


def test_scan_flags_only_unclaimed_audio(tmp_path, monkeypatch):
    _, _, claimed, orphan, playlist = _setup(tmp_path, monkeypatch)
    orphans = wp.find_orphaned_playlist_files([playlist])
    files = [o["file"] for o in orphans]
    assert str(orphan) in files
    assert str(claimed) not in files
    assert all(not f.endswith(".m3u") for f in files)


def test_append_mode_keeps_claiming_departed_tracks(tmp_path, monkeypatch):
    """Departures are now recorded in both modes, but append keeps its history.

    Only mirror mode stops listing a track that left the upstream playlist, so an
    append playlist's old files must never be offered up for rehoming.
    """
    _, _, claimed, orphan, playlist = _setup(tmp_path, monkeypatch)
    files = [o["file"] for o in wp.find_orphaned_playlist_files([{**playlist, "sync_mode": "append"}])]
    assert files == []
    # ...and the same playlist in mirror mode still disowns it
    files = [o["file"] for o in wp.find_orphaned_playlist_files([{**playlist, "sync_mode": "mirror"}])]
    assert str(orphan) in files


def test_scan_honours_resolved_path_claims(tmp_path, monkeypatch):
    """A track whose stem doesn't match its metadata is still claimed via resolved_path."""
    _, folder, _, _, playlist = _setup(tmp_path, monkeypatch)
    oddball = folder / "totally different filename.flac"
    oddball.write_bytes(b"renamed but tracked")
    with wp.db_conn() as c:
        c.execute(
            "INSERT INTO watched_playlist_tracks VALUES (?, ?, ?, ?, NULL, NULL)",
            ("pl1", "Someone", "Something", str(oddball)),
        )
    files = [o["file"] for o in wp.find_orphaned_playlist_files([playlist])]
    assert str(oddball) not in files


def test_scan_pools_claims_for_shared_custom_folder(tmp_path, monkeypatch):
    """Two playlists sharing a custom folder must not flag each other's tracks."""
    shared = tmp_path / "Shared"
    fake_db_conn = _fake_db(tmp_path)
    monkeypatch.setattr(wp, "db_conn", fake_db_conn)
    monkeypatch.setattr(wp, "resolve_custom_subdir", lambda sub, user_id=None: tmp_path / sub)

    # Both playlists land in Shared/<SafeName>; give them the same name so they
    # genuinely share a folder (a real-world "two users watch the same list" case).
    folder = shared / "Party"
    folder.mkdir(parents=True)
    a = folder / "Artist A - Song A.flac"
    a.write_bytes(b"a")
    b = folder / "Artist B - Song B.flac"
    b.write_bytes(b"b")
    with fake_db_conn() as c:
        c.execute("INSERT INTO watched_playlist_tracks VALUES ('p1', 'Artist A', 'Song A', NULL, NULL, NULL)")
        c.execute("INSERT INTO watched_playlist_tracks VALUES ('p2', 'Artist B', 'Song B', NULL, NULL, NULL)")

    playlists = [
        {"id": "p1", "name": "Party", "user_id": None, "use_playlists_dir": 0, "custom_subdir": "Shared"},
        {"id": "p2", "name": "Party", "user_id": None, "use_playlists_dir": 0, "custom_subdir": "Shared"},
    ]
    assert wp.find_orphaned_playlist_files(playlists) == []


def test_move_orphan_relocates_file_and_lyrics(tmp_path, monkeypatch):
    _, folder, _, orphan, playlist = _setup(tmp_path, monkeypatch)
    lrc = orphan.with_suffix(".lrc")
    lrc.write_text("[00:00.00] never gonna...")
    singles = tmp_path / "Singles"
    monkeypatch.setattr(wp, "get_download_dir", lambda artist, user_id=None: singles / artist)

    result = wp.move_orphans_to_singles([str(orphan)], [playlist])
    assert len(result["moved"]) == 1
    dest = singles / "Rick Astley" / orphan.name
    assert dest.exists()
    assert dest.with_suffix(".lrc").exists()
    assert not orphan.exists() and not lrc.exists()


def test_move_refuses_files_outside_playlist_folders(tmp_path, monkeypatch):
    _, _, _, _, playlist = _setup(tmp_path, monkeypatch)
    outsider = tmp_path / "precious.flac"
    outsider.write_bytes(b"do not touch")
    monkeypatch.setattr(wp, "get_download_dir", lambda artist, user_id=None: tmp_path / "Singles" / artist)

    result = wp.move_orphans_to_singles([str(outsider)], [playlist])
    assert result["moved"] == []
    assert len(result["skipped"]) == 1
    assert outsider.exists()


def test_move_never_overwrites_existing_single(tmp_path, monkeypatch):
    _, _, _, orphan, playlist = _setup(tmp_path, monkeypatch)
    singles = tmp_path / "Singles"
    dest_dir = singles / "Rick Astley"
    dest_dir.mkdir(parents=True)
    (dest_dir / orphan.name).write_bytes(b"the incumbent")
    monkeypatch.setattr(wp, "get_download_dir", lambda artist, user_id=None: singles / artist)

    result = wp.move_orphans_to_singles([str(orphan)], [playlist])
    assert result["moved"] == []
    assert len(result["skipped"]) == 1
    assert orphan.exists()  # original stays put
    assert (dest_dir / orphan.name).read_bytes() == b"the incumbent"


def test_locate_local_track_file_returns_path_for_tagging(tmp_path, monkeypatch):
    """The COMMENT-tagging fix needs the actual path back, not just a yes/no."""
    found_file = tmp_path / "Adele - Hello.flac"
    found_file.write_bytes(b"x")
    monkeypatch.setattr(wp, "check_duplicate", lambda a, t, user_id=None: found_file)

    found, local = wp._locate_local_track_file("Chill", False, "Adele", "Hello")
    assert found is True
    assert local == found_file

    # Navidrome-only hit: exists somewhere, but nothing local to tag
    monkeypatch.setattr(wp, "check_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(wp, "check_navidrome_duplicate", lambda a, t, user_id=None: Path("Hello"))
    found, local = wp._locate_local_track_file("Chill", False, "Adele", "Hello")
    assert found is True
    assert local is None

    # Nothing anywhere
    monkeypatch.setattr(wp, "check_navidrome_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(wp, "check_lidarr_duplicate", lambda a, t, user_id=None: None)
    found, local = wp._locate_local_track_file("Chill", False, "Adele", "Hello")
    assert found is False
    assert local is None


def test_locate_local_track_file_checks_lidarr_and_returns_m3u_path(tmp_path, monkeypatch):
    """A Lidarr-only duplicate must heal watched state instead of re-queueing forever."""
    lidarr_path = tmp_path / "Artists" / "Olivia Rodrigo" / "cigarette smoke.flac"
    monkeypatch.setattr(wp, "check_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(wp, "check_navidrome_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(wp, "check_lidarr_duplicate", lambda a, t, user_id=None: lidarr_path)

    found, local = wp._locate_local_track_file(
        "Top Songs - United Kingdom", False, "Olivia Rodrigo", "cigarette smoke"
    )

    assert found is True
    assert local == lidarr_path


def test_locate_local_track_file_lidarr_sentinel_blocks_redownload(monkeypatch):
    monkeypatch.setattr(wp, "check_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(wp, "check_navidrome_duplicate", lambda a, t, user_id=None: None)
    monkeypatch.setattr(
        wp, "check_lidarr_duplicate",
        lambda a, t, user_id=None: Path("cigarette smoke"),
    )

    found, local = wp._locate_local_track_file(
        "Top Songs - United Kingdom", False, "Olivia Rodrigo", "cigarette smoke"
    )

    assert found is True
    assert local is None


def test_reconcile_append_history_heals_only_inactive_historical_rows(monkeypatch):
    rows = {
        "historical-missing": {
            "downloaded_at": None, "removed_at": None, "job_status": None,
        },
        "current-missing": {
            "downloaded_at": None, "removed_at": None, "job_status": None,
        },
        "already-downloaded": {
            "downloaded_at": "2026-01-01", "removed_at": None, "job_status": None,
        },
        "still-downloading": {
            "downloaded_at": None, "removed_at": None, "job_status": "downloading",
        },
        # Departures are recorded in both modes now, and this function only ever
        # runs for append playlists, so a departed-but-never-downloaded row is
        # precisely the history it exists to heal.
        "departed-history": {
            "downloaded_at": None, "removed_at": "2026-01-01", "job_status": None,
        },
        "recently-checked": {
            "downloaded_at": None, "removed_at": None, "job_status": None,
            "library_checked_at": "9999-01-01 00:00:00",
        },
    }
    resolved = []
    monkeypatch.setattr(
        wp,
        "_resolve_unresolved_watched_track",
        lambda playlist, row, fallback_artist="", fallback_title="",
               lidarr_cache=None:
            (resolved.append(row["marker"]) or (False, None, "", "")),
    )

    for track_hash, row in rows.items():
        row["marker"] = track_hash

    class RecordingConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    conn = RecordingConn()
    count = wp._reconcile_append_history(
        conn,
        {"id": "pl1", "name": "Chart"},
        rows,
        {"current-missing"},
    )

    assert count == 0
    assert sorted(resolved) == ["departed-history", "historical-missing"]
    assert sorted(params[1] for _, params in conn.calls) == ["departed-history", "historical-missing"]
