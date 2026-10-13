"""Watched playlist membership accounting.

A 50-track chart that churns weekly used to report hundreds of "tracks", because
every row ever seen was counted. These tests pin down the fix: departures are
recorded whatever the sync mode, so "what is in this playlist now" is answerable,
while append mode still keeps its history.

Real schema via db.init_db() against a throwaway database; no server, no network.
"""

import os
import queue
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


UPSTREAM_NOW = [("Blur", "Song 2"), ("Pulp", "Common People"), ("Oasis", "Live Forever")]
DEPARTED = [("Chumbawamba", "Tubthumping"), ("Aqua", "Barbie Girl")]


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """A real MusicGrabber schema on a throwaway file, with a cold pool."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "counts.db")
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=db._DB_POOL_SIZE))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db.db_conn


def _seed(db_conn, sync_mode):
    """One playlist whose upstream has moved on, leaving two tracks behind."""
    import watched_playlists as wp

    with db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_playlists (id, url, name, platform, sync_mode, make_m3u)
               VALUES ('pl1', 'https://example.test/pl', 'Britpop Chart', 'spotify', ?, 0)""",
            (sync_mode,),
        )
        for artist, title in UPSTREAM_NOW + DEPARTED:
            conn.execute(
                """INSERT INTO watched_playlist_tracks
                   (playlist_id, track_hash, artist, title, downloaded_at)
                   VALUES ('pl1', ?, ?, ?, datetime('now'))""",
                (wp.hash_track(artist, title), artist, title),
            )
        conn.commit()


def _counts(db_conn):
    """The figures the playlist card asks for, as app.py computes them."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM watched_playlist_tracks
                  WHERE playlist_id = 'pl1' AND removed_at IS NULL) AS tracked,
                (SELECT COUNT(*) FROM watched_playlist_tracks
                  WHERE playlist_id = 'pl1' AND removed_at IS NULL
                    AND downloaded_at IS NOT NULL) AS downloaded,
                (SELECT COUNT(*) FROM watched_playlist_tracks
                  WHERE playlist_id = 'pl1' AND removed_at IS NOT NULL) AS departed
        """).fetchall()[0])


def _refresh(monkeypatch, upstream):
    """Run one refresh with the upstream fetch and downloading stubbed out."""
    import watched_playlists as wp

    monkeypatch.setattr(wp, "fetch_playlist_tracks", lambda *a, **kw: (list(upstream), "Britpop Chart", None))
    monkeypatch.setattr(wp, "start_bulk_import_for_tracks", lambda *a, **kw: "import01")
    monkeypatch.setattr(wp, "_locate_local_track_file", lambda *a, **kw: (True, None))
    monkeypatch.setattr(wp, "_resolve_unresolved_watched_track", lambda *a, **kw: (False, None, "", ""))
    monkeypatch.setattr(wp, "_tag_track_comment", lambda *a, **kw: None)
    return wp.refresh_watched_playlist("pl1")


@pytest.mark.parametrize("sync_mode", ["mirror", "append"])
def test_departures_are_recorded_in_both_sync_modes(fresh_db, monkeypatch, sync_mode):
    """The count of live rows must equal the upstream playlist, not its whole history."""
    _seed(fresh_db, sync_mode)
    assert _counts(fresh_db) == {"tracked": 5, "downloaded": 5, "departed": 0}

    _refresh(monkeypatch, UPSTREAM_NOW)

    counts = _counts(fresh_db)
    assert counts["tracked"] == len(UPSTREAM_NOW), "card must show the playlist, not its archive"
    assert counts["departed"] == len(DEPARTED)
    assert counts["downloaded"] <= counts["tracked"], "158 downloaded of 150 tracks is nobody's idea of maths"


@pytest.mark.parametrize("sync_mode", ["mirror", "append"])
def test_last_track_count_agrees_with_the_live_rows(fresh_db, monkeypatch, sync_mode):
    _seed(fresh_db, sync_mode)
    _refresh(monkeypatch, UPSTREAM_NOW)

    with fresh_db() as conn:
        upstream = conn.execute(
            "SELECT last_track_count FROM watched_playlists WHERE id = 'pl1'"
        ).fetchall()[0][0]
    assert upstream == _counts(fresh_db)["tracked"] == len(UPSTREAM_NOW)


def test_a_returning_track_loses_its_departure_mark(fresh_db, monkeypatch):
    """Charts recycle. A track that comes back counts as present again, both modes."""
    _seed(fresh_db, "append")
    _refresh(monkeypatch, UPSTREAM_NOW)
    assert _counts(fresh_db)["departed"] == len(DEPARTED)

    _refresh(monkeypatch, UPSTREAM_NOW + DEPARTED[:1])

    counts = _counts(fresh_db)
    assert counts["tracked"] == len(UPSTREAM_NOW) + 1
    assert counts["departed"] == len(DEPARTED) - 1
