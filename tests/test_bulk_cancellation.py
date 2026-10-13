"""Import cancellation stops future work without erasing Queue history."""

import os
import queue
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bulk_import
import db


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cancellation.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    monkeypatch.setattr(bulk_import, "db_conn", db.db_conn)
    db.init_db()
    return db


def _seed_import(import_id="import-1", watch_playlist_id=None):
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports
               (id, status, total_tracks, watch_playlist_id, progress_at)
               VALUES (?, 'processing', 5, ?, datetime('now'))""",
            (import_id, watch_playlist_id),
        )
        jobs = [
            ("queued-job", "queued"),
            ("active-job", "downloading"),
            ("done-job", "completed"),
        ]
        for job_id, status in jobs:
            conn.execute(
                """INSERT INTO jobs (id, video_id, artist, title, status)
                   VALUES (?, ?, 'Artist', 'Track', ?)""",
                (job_id, job_id, status),
            )
        tracks = [
            (1, "pending", None),
            (2, "searching", None),
            (3, "queued", "queued-job"),
            (4, "queued", "active-job"),
            (5, "completed", "done-job"),
        ]
        for line_num, status, job_id in tracks:
            conn.execute(
                """INSERT INTO bulk_import_tracks
                   (import_id, line_num, artist, song, status, job_id)
                   VALUES (?, ?, 'Artist', ?, ?, ?)""",
                (import_id, line_num, f"Track {line_num}", status, job_id),
            )
        conn.commit()


def test_cancel_retains_history_and_allows_only_active_file_to_finish(fresh_db):
    _seed_import()

    result = bulk_import.request_bulk_import_cancellation("import-1")

    assert result == {"imports": 1, "tracks": 3, "jobs": 1, "active": 1}
    with db.db_conn() as conn:
        import_row = conn.execute(
            "SELECT status, cancel_requested FROM bulk_imports WHERE id = 'import-1'"
        ).fetchone()
        tracks = conn.execute(
            """SELECT status FROM bulk_import_tracks
               WHERE import_id = 'import-1' ORDER BY line_num"""
        ).fetchall()
        jobs = conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()

    assert import_row == ("cancelling", 1)
    assert [row[0] for row in tracks] == [
        "cancelled", "cancelled", "cancelled", "queued", "completed"
    ]
    assert jobs == [
        ("active-job", "downloading"),
        ("done-job", "completed"),
        ("queued-job", "cancelled"),
    ]


def test_watched_playlist_cancellation_can_share_delete_transaction(fresh_db):
    _seed_import("watch-import", watch_playlist_id="watch-1")
    with db.db_conn() as conn:
        result = bulk_import.request_bulk_import_cancellation(
            watch_playlist_id="watch-1", conn=conn
        )
        # The caller can delete watched rows before committing the same atomic
        # transaction; no worker can observe a deleted watch without its import
        # also carrying the cancellation request.
        conn.execute("DELETE FROM watched_playlists WHERE id = 'watch-1'")
        conn.commit()

    assert result["imports"] == 1
    with db.db_conn() as conn:
        assert conn.execute(
            "SELECT cancel_requested FROM bulk_imports WHERE id = 'watch-import'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
