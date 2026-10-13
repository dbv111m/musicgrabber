"""Clock-controlled unit coverage for watched playlist/artist deadlines."""

import os
import sqlite3
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watched_artists as wa
import watched_playlists as wp


def _scheduler_db(tmp_path):
    db_file = tmp_path / "scheduler.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """CREATE TABLE watched_playlists (
               id TEXT PRIMARY KEY,
               name TEXT,
               enabled INTEGER,
               refresh_interval_hours REAL,
               last_checked TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE watched_artists (
               id TEXT PRIMARY KEY,
               name TEXT,
               enabled INTEGER,
               refresh_interval_hours REAL,
               last_checked TEXT
           )"""
    )
    conn.commit()
    conn.close()

    @contextmanager
    def fake_db_conn():
        connection = sqlite3.connect(db_file)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    return fake_db_conn


def test_next_deadline_honours_fractional_playlist_and_artist_intervals(tmp_path, monkeypatch):
    fake_db_conn = _scheduler_db(tmp_path)
    with fake_db_conn() as conn:
        conn.execute(
            "INSERT INTO watched_playlists VALUES ('p1', 'Half hourly', 1, 0.5, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO watched_artists VALUES ('a1', 'Half hourly', 1, 0.5, datetime('now'))"
        )

    monkeypatch.setattr(wp, "db_conn", fake_db_conn)
    monkeypatch.setattr(wa, "db_conn", fake_db_conn)
    monkeypatch.setattr(wp, "WATCHED_PLAYLIST_CHECK_HOURS", 24)
    monkeypatch.setattr(wa, "WATCHED_PLAYLIST_CHECK_HOURS", 24)

    playlist_delay = wp._seconds_until_next_playlist_check()
    artist_delay = wa._seconds_until_next_artist_check()
    assert 1795 <= playlist_delay <= 1805
    assert 1795 <= artist_delay <= 1805


def test_next_deadline_is_immediate_for_never_checked_and_bounded_globally(tmp_path, monkeypatch):
    fake_db_conn = _scheduler_db(tmp_path)
    with fake_db_conn() as conn:
        conn.execute("INSERT INTO watched_playlists VALUES ('new', 'New', 1, 0.5, NULL)")

    monkeypatch.setattr(wp, "db_conn", fake_db_conn)
    monkeypatch.setattr(wp, "WATCHED_PLAYLIST_CHECK_HOURS", 24)
    assert wp._seconds_until_next_playlist_check() == 1.0

    with fake_db_conn() as conn:
        conn.execute("UPDATE watched_playlists SET last_checked = datetime('now'), refresh_interval_hours = 48")
    assert 86395 <= wp._seconds_until_next_playlist_check() <= 86400


def test_scheduler_runs_two_consecutive_half_hour_deadlines_without_restart(tmp_path, monkeypatch):
    fake_db_conn = _scheduler_db(tmp_path)
    with fake_db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_playlists
               VALUES ('probe', '30-minute probe', 1, 0.5, datetime('now', '-31 minutes'))"""
        )

    refreshes = []

    def fake_refresh(playlist_id):
        refreshes.append(playlist_id)
        with fake_db_conn() as conn:
            conn.execute(
                "UPDATE watched_playlists SET last_checked = datetime('now') WHERE id = ?",
                (playlist_id,),
            )
        if len(refreshes) == 2:
            wp._scheduler_running = False
        return {"new_tracks": 0}

    class AdvancingEvent:
        def __init__(self):
            self.waits = []

        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout):
            self.waits.append(timeout)
            # Advance the database clock representation to the next due point;
            # no wall-clock sleeping belongs in a unit test.
            with fake_db_conn() as conn:
                conn.execute(
                    "UPDATE watched_playlists SET last_checked = datetime('now', '-31 minutes') WHERE id = 'probe'"
                )
            return False

    event = AdvancingEvent()
    monkeypatch.setattr(wp, "db_conn", fake_db_conn)
    monkeypatch.setattr(wp, "refresh_watched_playlist", fake_refresh)
    monkeypatch.setattr(wp, "_scheduler_wake_event", event)
    monkeypatch.setattr(wp, "WATCHED_PLAYLIST_CHECK_HOURS", 24)
    monkeypatch.setattr(wp.time, "sleep", lambda _seconds: None)

    wp._scheduler_running = True
    try:
        wp.watched_playlist_scheduler()
    finally:
        wp._scheduler_running = False

    assert refreshes == ["probe", "probe"]
    assert len(event.waits) == 1
    assert 1795 <= event.waits[0] <= 1805


def test_mutation_wake_helpers_signal_their_scheduler(monkeypatch):
    class RecordingEvent:
        signalled = False

        def set(self):
            self.signalled = True

    playlist_event = RecordingEvent()
    artist_event = RecordingEvent()
    monkeypatch.setattr(wp, "_scheduler_wake_event", playlist_event)
    monkeypatch.setattr(wa, "_scheduler_wake_event", artist_event)

    wp.wake_scheduler()
    wa.wake_artist_scheduler()
    assert playlist_event.signalled
    assert artist_event.signalled
