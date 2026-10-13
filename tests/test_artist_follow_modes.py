"""Following an artist splits into two independent switches: singles and albums.

Before v4 the two were welded together; watching an artist always meant watching
their singles, and albums were an optional extra bolted on top. Now you can have
either, or both, and an albums-only follow must not go hoovering up every B-side
the artist has ever released.

Real schema via db.init_db() against a throwaway database, MusicBrainz fully
mocked, no server and no network. Same style as test_artist_albums.py.
"""

import os
import queue
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """A real MusicGrabber schema on a throwaway file, with a cold pool."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "follow_modes.db")
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=db._DB_POOL_SIZE))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db.db_conn


def _seed_artist(db_conn, artist_id, name, mbid, auto_add_albums=0, watch_singles=1):
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_artists
               (id, name, mbid, from_date, auto_add_albums, watch_singles)
               VALUES (?, ?, ?, '2000-01-01', ?, ?)""",
            (artist_id, name, mbid, auto_add_albums, watch_singles),
        )
        conn.commit()


def test_watch_singles_defaults_to_on(fresh_db):
    """Every artist watched before this existed was watched for singles, so the
    column defaults to 1. Nobody should wake up quietly unfollowed."""
    with fresh_db() as conn:
        conn.execute(
            """INSERT INTO watched_artists (id, name, mbid, from_date)
               VALUES ('a1', 'Radiohead', 'mbid-1', '2000-01-01')"""
        )
        conn.commit()
        row = conn.execute("SELECT watch_singles FROM watched_artists WHERE id = 'a1'").fetchone()
    assert row[0] == 1


def test_albums_only_follow_never_asks_musicbrainz_for_singles(fresh_db, monkeypatch):
    """The headline rule: watch_singles=0 means no singles fetch, no singles queued.

    Not merely 'fetched and then discarded'; the request is never made at all,
    because MusicBrainz allows one request a second and wasting them on an
    answer nobody wants is just rude.
    """
    import watched_artists as wa

    _seed_artist(fresh_db, "a1", "Radiohead", "mbid-1", auto_add_albums=1, watch_singles=0)

    def fail_if_called(mbid):
        pytest.fail("fetch_artist_singles must not be called for an albums-only follow")

    monkeypatch.setattr(wa, "fetch_artist_singles", fail_if_called)
    monkeypatch.setattr(wa, "fetch_artist_albums", lambda mbid: [
        {"title": "Kid A", "year": "2000", "release_mbid": "rel-2", "release_group_mbid": "rg-2"},
    ])
    monkeypatch.setattr(
        wa, "start_bulk_import_for_tracks",
        lambda *a, **kw: pytest.fail("an albums-only follow must not queue singles"),
    )
    monkeypatch.setattr(wa.albums, "queue_album_download", lambda *a, **kw: {"import_id": "imp-1"})

    result = wa.refresh_watched_artist("a1")

    assert result["new_tracks"] == 0
    assert result["queued"] == 0
    # The album half still ran, which is the entire point of the follow.
    assert result["new_albums"] == 1
    assert result["albums_queued"] == 1

    with fresh_db() as conn:
        state = conn.execute("SELECT refresh_state FROM watched_artists WHERE id = 'a1'").fetchone()
    assert state[0] == "idle"


def test_singles_follow_still_fetches_singles(fresh_db, monkeypatch):
    """The other half of the same coin: watch_singles=1 behaves exactly as before."""
    import watched_artists as wa

    _seed_artist(fresh_db, "a1", "Radiohead", "mbid-1", auto_add_albums=0, watch_singles=1)

    calls = []
    monkeypatch.setattr(wa, "fetch_artist_singles", lambda mbid: (calls.append(mbid) or [
        {"artist": "Radiohead", "title": "Creep", "release_date": "2020-01-01",
         "release_mbid": "rel-single"},
    ]))
    # Takes user_id since CR-004: the library scan is scoped to the watch's owner.
    monkeypatch.setattr(wa, "check_duplicate", lambda artist, title, user_id=None: None)
    monkeypatch.setattr(wa, "start_bulk_import_for_tracks", lambda *a, **kw: "import-singles")

    result = wa.refresh_watched_artist("a1")

    assert calls == ["mbid-1"]
    assert result["new_tracks"] == 1
    assert result["queued"] == 1


def test_albums_only_refresh_leaves_existing_single_counts_alone(fresh_db, monkeypatch):
    """Switching an artist to albums-only must not wipe the singles they already
    have tracked. The tracks stay in the table and the count keeps counting them,
    it just stops growing."""
    import watched_artists as wa

    _seed_artist(fresh_db, "a1", "Radiohead", "mbid-1", auto_add_albums=1, watch_singles=0)
    with fresh_db() as conn:
        for i in range(3):
            conn.execute(
                """INSERT INTO watched_artist_tracks
                   (artist_id, track_hash, artist, title, downloaded_at)
                   VALUES ('a1', ?, 'Radiohead', ?, datetime('now'))""",
                (f"hash-{i}", f"Track {i}"),
            )
        conn.commit()

    monkeypatch.setattr(wa, "fetch_artist_singles", lambda mbid: pytest.fail("no singles fetch"))
    monkeypatch.setattr(wa, "fetch_artist_albums", lambda mbid: [])

    result = wa.refresh_watched_artist("a1")

    assert result["total_tracked"] == 3, "existing tracked singles must survive the mode switch"
    with fresh_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM watched_artist_tracks WHERE artist_id = 'a1'"
        ).fetchone()[0]
        stored = conn.execute(
            "SELECT last_track_count FROM watched_artists WHERE id = 'a1'"
        ).fetchone()[0]
    assert count == 3
    assert stored == 3
