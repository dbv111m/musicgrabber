"""Queue search: finding one track in a queue far too big to scroll through.

The Queue tab only ever pulls down the newest N jobs, so before this existed a
900-track import left 650 of its tracks completely unreachable from the UI.
These cover the promise that a search reaches the whole table, the promise that
it stays inside your own rows, and the promise that typing an underscore does
not quietly turn into a wildcard.
"""

import os
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import constants


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "queue-search.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "queue-search.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db


def _add_job(database, job_id, *, artist=None, title=None, album_name=None,
             playlist_name=None, status="completed", user_id="alice",
             created_at="2026-09-01 12:00:00"):
    with database.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, artist, title, album_name, playlist_name, status, user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, artist, title, album_name, playlist_name, status, user_id, created_at),
        )
        conn.commit()


def _request(user_id="alice", is_admin=True):
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id, is_admin=is_admin))


def _search(term, *, limit=20, user_id="alice", is_admin=True):
    import app
    return app.get_jobs(limit=limit, q=term, http_request=_request(user_id, is_admin))


def _ids(result):
    return {job["id"] for job in result["jobs"]}


def test_search_reaches_jobs_the_window_would_never_show(fresh_db):
    """The whole point: the match is job 900 of 900, nowhere near the newest 20."""
    import app

    for n in range(50):
        _add_job(fresh_db, f"filler-{n:03d}", artist="Filler Band",
                 title=f"Filler Track {n}", created_at=f"2026-09-02 12:{n:02d}:00")
    _add_job(fresh_db, "needle", artist="Boards of Canada",
             title="Roygbiv", created_at="2026-08-01 09:00:00")

    unfiltered = app.get_jobs(limit=20, http_request=_request())
    assert "needle" not in _ids(unfiltered), "fixture is wrong: needle should be off the window"

    assert _ids(_search("roygbiv")) == {"needle"}


@pytest.mark.parametrize("term", ["boards", "ROYGBIV", "Geogaddi", "Tuesday Mix"])
def test_search_covers_artist_title_album_and_playlist(fresh_db, term):
    _add_job(fresh_db, "needle", artist="Boards of Canada", title="Roygbiv",
             album_name="Geogaddi", playlist_name="Tuesday Mix")
    _add_job(fresh_db, "other", artist="Someone Else", title="Something Else")

    assert _ids(_search(term)) == {"needle"}


def test_underscore_in_a_search_term_is_an_underscore(fresh_db):
    """LIKE would happily read '50_Cent' as '50<anything>Cent'. It must not."""
    _add_job(fresh_db, "literal", artist="50_Cent", title="In Da Club")
    _add_job(fresh_db, "wildcard-bait", artist="50xCent", title="Decoy")

    assert _ids(_search("50_Cent")) == {"literal"}


def test_percent_in_a_search_term_does_not_match_the_lot(fresh_db):
    _add_job(fresh_db, "literal", artist="100% Pure", title="Percentage")
    _add_job(fresh_db, "innocent", artist="Nobody", title="Nothing To Do With It")

    assert _ids(_search("%")) == {"literal"}


def test_a_backslash_is_not_an_escape_hatch(fresh_db):
    """A stray backslash should find nothing, not blow up mid-query."""
    _add_job(fresh_db, "plain", artist="Nobody", title="Nothing")

    assert _search("\\")["jobs"] == []


def test_search_stays_inside_the_users_own_rows(fresh_db):
    _add_job(fresh_db, "mine", artist="Boards of Canada", title="Roygbiv", user_id="alice")
    _add_job(fresh_db, "theirs", artist="Boards of Canada", title="Roygbiv", user_id="bob")

    assert _ids(_search("roygbiv", user_id="alice", is_admin=False)) == {"mine"}


def test_totals_describe_the_whole_queue_not_the_search(fresh_db):
    """The live summary reads these. A search must not convince it the queue emptied."""
    _add_job(fresh_db, "needle", artist="Boards of Canada", title="Roygbiv", status="queued")
    for n in range(4):
        _add_job(fresh_db, f"busy-{n}", artist="Someone", title=f"Track {n}", status="queued")
    _add_job(fresh_db, "done", artist="Someone", title="Finished", status="completed")

    result = _search("roygbiv")
    assert _ids(result) == {"needle"}
    assert result["queue_totals"] == {"queued": 5, "completed": 1}


@pytest.mark.parametrize("term", [None, "", "   "])
def test_blank_search_is_no_search(fresh_db, term):
    import app

    _add_job(fresh_db, "a", artist="Anyone", title="Anything")
    _add_job(fresh_db, "b", artist="Someone", title="Something")

    assert _ids(app.get_jobs(limit=20, q=term, http_request=_request())) == {"a", "b"}


@pytest.mark.parametrize("term", ["boards", "Roygbiv", "Geogaddi", "Tuesday Mix"])
def test_search_within_bulk_import_uses_job_fields_and_user_scope(fresh_db, term):
    import app

    with fresh_db.db_conn() as conn:
        conn.execute("INSERT INTO bulk_imports (id) VALUES ('import-a')")
        conn.commit()
    for job_id, user_id in [("mine", "alice"), ("theirs", "bob"), ("outside", "alice")]:
        _add_job(fresh_db, job_id, artist="Boards of Canada", title="Roygbiv",
                 album_name="Geogaddi", playlist_name="Tuesday Mix", user_id=user_id)
        if job_id != "outside":
            with fresh_db.db_conn() as conn:
                conn.execute(
                    "INSERT INTO bulk_import_tracks (import_id, job_id, artist, line_num) "
                    "VALUES ('import-a', ?, 'Original artist', 1)", (job_id,),
                )
                conn.commit()

    result = app.get_jobs(import_id="import-a", q=term,
                          http_request=_request(is_admin=False))
    assert _ids(result) == {"mine"}
    assert result["queue_totals"] is None
