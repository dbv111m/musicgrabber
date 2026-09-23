"""
Maintenance sweep for bulk imports whose worker thread has died.

A bulk import runs in a daemon thread, so a crash or a container restart leaves
the row saying 'processing' with nobody behind it, forever. The test instance
that prompted this had 36 of them, the oldest claiming to have been busy since
January, between them holding every search source's admission slot hostage and
making the slow suite flaky.

These tests run against a throwaway SQLite file, so nothing here touches a real
library or spawns a real download.
"""

import os
import queue
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """A real schema on a throwaway file, with its own empty connection pool."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "maintenance.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db


@pytest.fixture
def spawned(monkeypatch):
    """Catch worker spawns instead of letting them run."""
    calls = []
    import utils
    monkeypatch.setattr(utils, "spawn_daemon_thread", lambda fn, *a, **k: calls.append(a[0] if a else None))
    return calls


def _add_import(import_id, *, age_seconds=0, quiet_seconds=0, resume_count=0,
                watch_playlist_id=None, watch_artist_id=None, tracks=("pending",)):
    """Insert one import plus its tracks, aged to order."""
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports
               (id, status, total_tracks, created_at, progress_at, resume_count,
                watch_playlist_id, watch_artist_id)
               VALUES (?, 'processing', ?, datetime('now', ? || ' seconds'),
                       datetime('now', ? || ' seconds'), ?, ?, ?)""",
            (import_id, len(tracks), str(-age_seconds), str(-quiet_seconds),
             resume_count, watch_playlist_id, watch_artist_id)
        )
        for line_num, status in enumerate(tracks, 1):
            conn.execute(
                "INSERT INTO bulk_import_tracks (import_id, line_num, artist, song, status) VALUES (?, ?, 'A', 'B', ?)",
                (import_id, line_num, status)
            )
        conn.commit()


def _import_row(import_id):
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT status, resume_count, error FROM bulk_imports WHERE id = ?", (import_id,)
        ).fetchone()
    return {"status": row[0], "resume_count": row[1], "error": row[2]}


def _track_statuses(import_id):
    with db.db_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT status FROM bulk_import_tracks WHERE import_id = ? ORDER BY line_num", (import_id,)
        ).fetchall()]


# ---------------------------------------------------------------------------
# Resuming
# ---------------------------------------------------------------------------

def test_a_stalled_import_is_resumed_and_its_worker_respawned(fresh_db, spawned):
    _add_import("stalled1", age_seconds=3600, quiet_seconds=7200, tracks=("pending", "searching"))

    resumed, failed = db.cleanup_stale_bulk_imports()

    assert (resumed, failed) == (1, 0)
    assert spawned == ["stalled1"]
    assert _import_row("stalled1")["status"] == "processing"
    assert _import_row("stalled1")["resume_count"] == 1


def test_a_track_caught_mid_search_goes_back_in_the_queue(fresh_db, spawned):
    """Interrupted is not the same as failed; don't punish it for our crash."""
    _add_import("stalled2", age_seconds=3600, quiet_seconds=7200,
                tracks=("searching", "pending", "queued", "failed"))

    db.cleanup_stale_bulk_imports()

    # The searching one is rescued; everything already settled is left alone.
    assert _track_statuses("stalled2") == ["pending", "pending", "queued", "failed"]


def test_a_lively_import_is_left_well_alone(fresh_db, spawned):
    """A 1400-track import is entitled to take hours; silence is the symptom."""
    _add_import("busy", age_seconds=86400, quiet_seconds=5)

    assert db.cleanup_stale_bulk_imports() == (0, 0)
    assert spawned == []
    assert _import_row("busy")["status"] == "processing"


# ---------------------------------------------------------------------------
# Knowing when to stop
# ---------------------------------------------------------------------------

def test_an_import_that_keeps_dying_is_eventually_written_off(fresh_db, spawned):
    _add_import("doomed", age_seconds=3600, quiet_seconds=7200,
                resume_count=db.STALE_BULK_IMPORT_MAX_RESUMES)

    resumed, failed = db.cleanup_stale_bulk_imports()

    assert (resumed, failed) == (0, 1)
    assert spawned == []
    assert _import_row("doomed")["status"] == "failed"
    assert "Gave up after" in _import_row("doomed")["error"]


def test_resuming_stops_exactly_at_the_cap(fresh_db, spawned):
    """One under the cap still gets a go; the cap itself does not."""
    _add_import("nearly", age_seconds=3600, quiet_seconds=7200,
                resume_count=db.STALE_BULK_IMPORT_MAX_RESUMES - 1)

    assert db.cleanup_stale_bulk_imports() == (1, 0)
    assert _import_row("nearly")["resume_count"] == db.STALE_BULK_IMPORT_MAX_RESUMES


def test_an_ancient_import_is_not_resurrected(fresh_db, spawned):
    """Nobody wants a January import springing to life in August."""
    _add_import("ancient", age_seconds=db.STALE_BULK_IMPORT_ABANDON_AFTER + 3600,
                quiet_seconds=db.STALE_BULK_IMPORT_ABANDON_AFTER + 3600)

    resumed, failed = db.cleanup_stale_bulk_imports()

    assert (resumed, failed) == (0, 1)
    assert "older than" in _import_row("ancient")["error"]


def test_unsearched_tracks_are_failed_alongside_the_import(fresh_db, spawned):
    """Otherwise the progress numbers imply work is still happening somewhere."""
    _add_import("ancient2", age_seconds=db.STALE_BULK_IMPORT_ABANDON_AFTER + 3600,
                quiet_seconds=db.STALE_BULK_IMPORT_ABANDON_AFTER + 3600,
                tracks=("pending", "searching", "queued"))

    db.cleanup_stale_bulk_imports()

    assert _track_statuses("ancient2") == ["failed", "failed", "queued"]


# ---------------------------------------------------------------------------
# Work nobody wants any more
# ---------------------------------------------------------------------------

def test_an_import_for_a_deleted_playlist_is_never_resumed(fresh_db, spawned):
    """Reviving this downloads music for something you already threw away."""
    _add_import("orphan", age_seconds=60, quiet_seconds=7200,
                watch_playlist_id="gone-forever")

    resumed, failed = db.cleanup_stale_bulk_imports()

    assert (resumed, failed) == (0, 1)
    assert "deleted" in _import_row("orphan")["error"]


def test_an_import_for_a_deleted_artist_is_never_resumed(fresh_db, spawned):
    _add_import("orphan2", age_seconds=60, quiet_seconds=7200,
                watch_artist_id="also-gone")

    assert db.cleanup_stale_bulk_imports() == (0, 1)


def test_a_live_watched_playlist_still_gets_its_import_resumed(fresh_db, spawned):
    """The orphan rule must not swallow refreshes that are perfectly legitimate."""
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO watched_playlists (id, name, url, platform) VALUES ('alive', 'Bangers', 'http://x', 'spotify')"
        )
        conn.commit()
    _add_import("legit", age_seconds=60, quiet_seconds=7200, watch_playlist_id="alive")

    assert db.cleanup_stale_bulk_imports() == (1, 0)
    assert spawned == ["legit"]


# ---------------------------------------------------------------------------
# The boot pass
# ---------------------------------------------------------------------------

def test_startup_does_not_make_orphans_serve_out_the_timeout(fresh_db, spawned):
    """Nothing survives a restart, so waiting an hour to notice is pointless."""
    _add_import("justborn", age_seconds=5, quiet_seconds=5)

    # The ordinary sweep sees a healthy heartbeat and leaves it be...
    assert db.cleanup_stale_bulk_imports() == (0, 0)
    # ...but at boot we know for a fact there is nobody behind it.
    assert db.cleanup_stale_bulk_imports(orphaned=True) == (1, 0)
    assert spawned == ["justborn"]


def test_nothing_to_do_is_silent_and_cheap(fresh_db, spawned):
    assert db.cleanup_stale_bulk_imports() == (0, 0)
    assert db.cleanup_stale_bulk_imports(orphaned=True) == (0, 0)
    assert spawned == []


# ---------------------------------------------------------------------------
# Single jobs: the same "nothing survives a restart" rule
# ---------------------------------------------------------------------------

def _add_job(job_id, status, quiet_seconds):
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs (id, title, artist, status, progress_stage, created_at, progress_at)
               VALUES (?, 'Mein Herz brennt', 'Rammstein', ?, 'Downloading audio',
                       datetime('now', ? || ' seconds'), datetime('now', ? || ' seconds'))""",
            (job_id, status, str(-quiet_seconds), str(-quiet_seconds)),
        )
        conn.commit()


def _job(job_id):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT status, error, progress_stage FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def test_a_job_caught_mid_download_by_a_restart_is_failed_at_boot(fresh_db):
    """Thirty seconds into a download when the container went down: no thread
    is behind it any more, so it should not sit on "Downloading" for 15 minutes."""
    _add_job("fresh1", "downloading", quiet_seconds=30)
    _add_job("queued1", "queued", quiet_seconds=5)
    db.cleanup_stale_jobs(orphaned=True)
    assert _job("fresh1") == ("failed", "Interrupted by a restart", None)
    assert _job("queued1")[0] == "failed"


def test_the_periodic_sweep_still_leaves_a_live_download_alone(fresh_db):
    _add_job("alive1", "downloading", quiet_seconds=30)
    db.cleanup_stale_jobs()
    assert _job("alive1")[0] == "downloading"


def test_the_periodic_sweep_still_times_out_a_silent_download(fresh_db):
    _add_job("dead1", "downloading", quiet_seconds=db.STALE_JOB_TIMEOUT + 60)
    db.cleanup_stale_jobs()
    assert _job("dead1")[:2] == ("failed", "Timed out (no progress)")
