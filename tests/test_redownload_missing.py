"""Downloaded once, downloaded for good (when you ask for it).

Watched playlists and artists re-fetch a track whose file has gone missing, which
is the right call when you have binned a duff rip. It is emphatically the wrong
call when an external organiser (MusicBee and friends) moves or renames the file
afterwards: MusicGrabber cannot find it, downloads it again, the organiser moves
the copy, and round it goes until the disk gives up. The redownload_missing_files
setting is the handbrake. These tests pin both positions of it.

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


UPSTREAM = [("Blur", "Song 2"), ("Pulp", "Common People")]


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """A real MusicGrabber schema on a throwaway file, with a cold pool."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "redownload.db")
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=db._DB_POOL_SIZE))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db.db_conn


@pytest.fixture
def redownload_setting(fresh_db, monkeypatch):
    """Set redownload_missing_files in the throwaway DB, env var out of the way."""
    import settings

    monkeypatch.delenv("REDOWNLOAD_MISSING_FILES", raising=False)

    def _set(enabled: bool):
        settings.set_setting("redownload_missing_files", "true" if enabled else "false")

    return _set


# ---------------------------------------------------------------- playlists


def _seed_playlist(db_conn):
    """One playlist whose tracks are all marked downloaded, paths and all."""
    import watched_playlists as wp

    with db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_playlists (id, url, name, platform, sync_mode, make_m3u)
               VALUES ('pl1', 'https://example.test/pl', 'Britpop Chart', 'spotify', 'append', 0)"""
        )
        for artist, title in UPSTREAM:
            conn.execute(
                """INSERT INTO watched_playlist_tracks
                   (playlist_id, track_hash, artist, title, downloaded_at, resolved_path)
                   VALUES ('pl1', ?, ?, ?, datetime('now'), ?)""",
                (wp.hash_track(artist, title), artist, title, f"/music/Singles/{artist}/{artist} - {title}.flac"),
            )
        conn.commit()


def _refresh_playlist(monkeypatch, found):
    """One refresh with upstream and downloading stubbed; `found` fakes the disk."""
    import watched_playlists as wp

    queued = []
    monkeypatch.setattr(wp, "fetch_playlist_tracks", lambda *a, **kw: (list(UPSTREAM), "Britpop Chart", None))
    monkeypatch.setattr(wp, "start_bulk_import_for_tracks",
                        lambda tracks, *a, **kw: queued.extend(tracks) or "import01")
    monkeypatch.setattr(wp, "_locate_local_track_file", lambda *a, **kw: (found, None))
    monkeypatch.setattr(wp, "_resolve_unresolved_watched_track", lambda *a, **kw: (False, None, "", ""))
    monkeypatch.setattr(wp, "_tag_track_comment", lambda *a, **kw: None)
    return wp.refresh_watched_playlist("pl1"), queued


def _playlist_state(db_conn):
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        still_downloaded = conn.execute(
            """SELECT COUNT(*) FROM watched_playlist_tracks
               WHERE playlist_id = 'pl1' AND downloaded_at IS NOT NULL"""
        ).fetchall()[0][0]
        unresolved = conn.execute(
            "SELECT unresolved_tracks FROM watched_playlists WHERE id = 'pl1'"
        ).fetchall()[0][0]
    return still_downloaded, unresolved


def test_playlist_refetches_vanished_files_by_default(fresh_db, monkeypatch, redownload_setting):
    """Default behaviour is unchanged: a deleted file comes back."""
    redownload_setting(True)
    _seed_playlist(fresh_db)

    result, queued = _refresh_playlist(monkeypatch, found=False)

    assert result["missing_tracks"] == len(UPSTREAM)
    assert sorted(queued) == sorted(UPSTREAM), "the whole point of the default is that it re-queues"
    still_downloaded, unresolved = _playlist_state(fresh_db)
    assert still_downloaded == 0, "downloaded_at must be cleared so the retry sticks"
    assert unresolved == 0


def test_playlist_leaves_moved_files_alone_when_disabled(fresh_db, monkeypatch, redownload_setting):
    """MusicBee has run off with the files. We notice, and we do nothing."""
    redownload_setting(False)
    _seed_playlist(fresh_db)

    result, queued = _refresh_playlist(monkeypatch, found=False)

    assert queued == [], "not one duplicate download, thank you"
    assert result["missing_tracks"] == 0
    assert result["unresolved_tracks"] == len(UPSTREAM)
    still_downloaded, unresolved = _playlist_state(fresh_db)
    assert still_downloaded == len(UPSTREAM), "downloaded once means downloaded"
    assert unresolved == len(UPSTREAM), "the card needs the number to own up with"


def test_unresolved_count_clears_once_files_are_findable(fresh_db, monkeypatch, redownload_setting):
    """Point MusicGrabber back at the files and the warning goes away by itself."""
    redownload_setting(False)
    _seed_playlist(fresh_db)
    _refresh_playlist(monkeypatch, found=False)
    assert _playlist_state(fresh_db)[1] == len(UPSTREAM)

    _refresh_playlist(monkeypatch, found=True)

    still_downloaded, unresolved = _playlist_state(fresh_db)
    assert unresolved == 0, "stale warnings are worse than no warnings"
    assert still_downloaded == len(UPSTREAM)


def test_disabling_does_not_block_genuinely_new_tracks(fresh_db, monkeypatch, redownload_setting):
    """The handbrake is for re-downloads only; new arrivals still get queued."""
    redownload_setting(False)
    _seed_playlist(fresh_db)
    newcomer = ("Oasis", "Live Forever")

    import watched_playlists as wp
    queued = []
    monkeypatch.setattr(wp, "fetch_playlist_tracks",
                        lambda *a, **kw: (list(UPSTREAM) + [newcomer], "Britpop Chart", None))
    monkeypatch.setattr(wp, "start_bulk_import_for_tracks",
                        lambda tracks, *a, **kw: queued.extend(tracks) or "import01")
    monkeypatch.setattr(wp, "_locate_local_track_file", lambda *a, **kw: (False, None))
    monkeypatch.setattr(wp, "_resolve_unresolved_watched_track", lambda *a, **kw: (False, None, "", ""))
    monkeypatch.setattr(wp, "_tag_track_comment", lambda *a, **kw: None)
    wp.refresh_watched_playlist("pl1")

    assert queued == [newcomer], f"expected only the new track, got {queued}"


def test_playlist_can_override_global_redownload_policy(fresh_db, monkeypatch, redownload_setting):
    redownload_setting(True)
    _seed_playlist(fresh_db)
    with fresh_db() as conn:
        conn.execute(
            "UPDATE watched_playlists SET redownload_missing_files = 0 WHERE id = 'pl1'"
        )
        conn.commit()

    result, queued = _refresh_playlist(monkeypatch, found=False)

    assert queued == []
    assert result["unresolved_tracks"] == len(UPSTREAM)


def test_playlist_can_opt_back_into_redownloads(fresh_db, monkeypatch, redownload_setting):
    redownload_setting(False)
    _seed_playlist(fresh_db)
    with fresh_db() as conn:
        conn.execute(
            "UPDATE watched_playlists SET redownload_missing_files = 1 WHERE id = 'pl1'"
        )
        conn.commit()

    result, queued = _refresh_playlist(monkeypatch, found=False)

    assert sorted(queued) == sorted(UPSTREAM)
    assert result["unresolved_tracks"] == 0


def test_append_history_is_included_in_unresolved_count(fresh_db, monkeypatch, redownload_setting):
    redownload_setting(False)
    _seed_playlist(fresh_db)
    import watched_playlists as wp
    with fresh_db() as conn:
        conn.execute(
            """INSERT INTO watched_playlist_tracks
               (playlist_id, track_hash, artist, title, downloaded_at, resolved_path, removed_at)
               VALUES ('pl1', ?, 'Suede', 'Animal Nitrate', datetime('now'), ?, datetime('now'))""",
            (wp.hash_track("Suede", "Animal Nitrate"), "/music/renamed.flac"),
        )
        conn.commit()

    result, queued = _refresh_playlist(monkeypatch, found=False)

    assert queued == []
    assert result["unresolved_tracks"] == len(UPSTREAM) + 1


# ------------------------------------------------------------------ artists


def _seed_artist(db_conn):
    import watched_artists as wa

    with db_conn() as conn:
        conn.execute(
            """INSERT INTO watched_artists (id, name, mbid, from_date, watch_singles, auto_add_albums)
               VALUES ('ar1', 'Blur', 'mbid-blur', '1990-01-01', 1, 0)"""
        )
        conn.execute(
            """INSERT INTO watched_artist_tracks
               (artist_id, track_hash, artist, title, release_date, downloaded_at, resolved_path)
               VALUES ('ar1', ?, 'Blur', 'Song 2', '1997-04-07', datetime('now'), '/music/Singles/Blur/Blur - Song 2.flac')""",
            (wa.hash_track("Blur", "Song 2"),),
        )
        conn.commit()


def _refresh_artist(monkeypatch):
    import watched_artists as wa

    queued = []
    monkeypatch.setattr(wa, "fetch_artist_singles",
                        lambda mbid: [{"artist": "Blur", "title": "Song 2", "release_date": "1997-04-07"}])
    monkeypatch.setattr(wa, "start_bulk_import_for_tracks",
                        lambda tracks, *a, **kw: queued.extend(tracks) or "import01")
    monkeypatch.setattr(wa, "check_duplicate", lambda *a, **kw: None)  # file is nowhere to be found
    wa.refresh_watched_artist("ar1")
    return queued


def test_artist_refetches_vanished_files_by_default(fresh_db, monkeypatch, redownload_setting):
    redownload_setting(True)
    _seed_artist(fresh_db)

    assert _refresh_artist(monkeypatch) == [("Blur", "Song 2")]


def test_artist_leaves_moved_files_alone_when_disabled(fresh_db, monkeypatch, redownload_setting):
    """Watched artists had the same loop, and the same handbrake now stops it."""
    redownload_setting(False)
    _seed_artist(fresh_db)

    assert _refresh_artist(monkeypatch) == []
    with fresh_db() as conn:
        assert conn.execute(
            "SELECT downloaded_at FROM watched_artist_tracks WHERE artist_id = 'ar1'"
        ).fetchall()[0][0] is not None
        assert conn.execute(
            "SELECT unresolved_tracks FROM watched_artists WHERE id = 'ar1'"
        ).fetchall()[0][0] == 1
