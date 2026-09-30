"""Restoring a rejected download must also repair its Queue history."""

import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import constants


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "trash-restore.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trash-restore.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db


def _add_failed(database, job_id, final_path=None):
    with database.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs (id, artist, title, status, error, final_path, user_id)
               VALUES (?, 'The Testers', 'Restored Take', 'failed', 'Wrong version', ?, 'alice')""",
            (job_id, final_path),
        )
        conn.commit()


def test_restore_completes_failed_job_with_recorded_path(fresh_db, tmp_path):
    import app

    restored = tmp_path / "Singles" / "The Testers" / "Restored Take.flac"
    _add_failed(fresh_db, "exact-path", str(restored))

    assert app._complete_restored_failed_job(
        restored, "alice", False, "The Testers", "Restored Take"
    ) == "exact-path"

    with fresh_db.db_conn() as conn:
        row = conn.execute(
            "SELECT status, error, final_path, file_deleted FROM jobs WHERE id = 'exact-path'"
        ).fetchone()
    assert row == ("completed", None, str(restored), 0)


def test_restore_does_not_guess_between_duplicate_legacy_failures(fresh_db, tmp_path):
    import app

    _add_failed(fresh_db, "first")
    _add_failed(fresh_db, "second")
    restored = tmp_path / "Singles" / "The Testers" / "Restored Take.flac"

    assert app._complete_restored_failed_job(
        restored, "alice", False, "The Testers", "Restored Take"
    ) is None

    with fresh_db.db_conn() as conn:
        statuses = conn.execute("SELECT status FROM jobs ORDER BY id").fetchall()
    assert statuses == [("failed",), ("failed",)]


def _add_watched_download(database, file_path: Path):
    with database.db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_playlists
               (id, url, name, platform, make_m3u, user_id)
               VALUES ('playlist-1', 'https://example.test/list', 'Test List',
                       'spotify', 1, NULL)"""
        )
        conn.execute(
            """INSERT INTO jobs
               (id, artist, title, status, final_path, user_id)
               VALUES ('old-job', 'The Testers', 'Wrong Take', 'completed', ?, NULL)""",
            (str(file_path),),
        )
        conn.execute(
            """INSERT INTO watched_playlist_tracks
               (playlist_id, track_hash, artist, title, downloaded_at,
                resolved_path, job_id)
               VALUES ('playlist-1', 'stable-track', 'The Testers', 'Wrong Take',
                       datetime('now'), ?, 'old-job')""",
            (str(file_path),),
        )
        conn.commit()


def test_clearing_job_history_nulls_watched_playlist_link(fresh_db, tmp_path):
    audio = tmp_path / "music" / "The Testers" / "Wrong Take.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    _add_watched_download(fresh_db, audio)

    with fresh_db.db_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = 'old-job'")
        conn.commit()
        row = conn.execute(
            """SELECT job_id, downloaded_at, resolved_path
               FROM watched_playlist_tracks
               WHERE playlist_id = 'playlist-1' AND track_hash = 'stable-track'"""
        ).fetchone()

    assert row[0] is None
    assert row[1] is not None
    assert row[2] == str(audio)


def test_v17_migration_cleans_existing_dangling_job_links(fresh_db):
    with fresh_db.db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_playlists (id, url, name, platform)
               VALUES ('playlist-1', 'https://example.test/list', 'Test List', 'spotify')"""
        )
        conn.execute(
            """INSERT INTO watched_playlist_tracks
               (playlist_id, track_hash, artist, title, job_id)
               VALUES ('playlist-1', 'stable-track', 'The Testers', 'Wrong Take',
                       'already-gone')"""
        )
        conn.execute(
            "UPDATE settings SET value = '16' WHERE key = 'db_version'"
        )
        conn.commit()

    fresh_db.init_db()

    with fresh_db.db_conn() as conn:
        row = conn.execute(
            """SELECT job_id FROM watched_playlist_tracks
               WHERE playlist_id = 'playlist-1' AND track_hash = 'stable-track'"""
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM settings WHERE key = 'db_version'"
        ).fetchone()[0]
    assert row == (None,)
    assert version == "21"


def test_replace_uses_stable_track_identity_after_job_is_cleared(
    fresh_db, tmp_path, monkeypatch
):
    import app
    import utils

    audio = tmp_path / "music" / "The Testers" / "Wrong Take.flac"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    _add_watched_download(fresh_db, audio)
    with fresh_db.db_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = 'old-job'")
        conn.commit()

    trashed = []
    rebuilt = []
    monkeypatch.setattr(app, "authoritative_audio_file", lambda path, user_id: Path(path))
    monkeypatch.setattr(
        utils,
        "move_to_trash",
        lambda path, user_id=None: trashed.append(path) or tmp_path / "trash" / path.name,
    )
    monkeypatch.setattr(
        app,
        "rebuild_watched_playlist_m3u",
        lambda *args, **kwargs: rebuilt.append((args, kwargs)),
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id=None, is_admin=True))

    result = app.delete_watched_playlist_track_file(
        "playlist-1", "stable-track", request
    )

    with fresh_db.db_conn() as conn:
        state = conn.execute(
            """SELECT job_id, downloaded_at, resolved_path
               FROM watched_playlist_tracks
               WHERE playlist_id = 'playlist-1' AND track_hash = 'stable-track'"""
        ).fetchone()
    assert trashed == [audio]
    assert rebuilt and rebuilt[0][0][0] == "playlist-1"
    assert state == (None, None, None)
    assert result["track_hash"] == "stable-track"
