"""Watched-artist search when MusicBrainz is having a lie down.

`search_artist_mbid` raises `MusicBrainzUnavailable` precisely so the API can
say "try again" rather than "no such artist". The Watched route let it escape
as a bare 500 instead, which the slow suite caught for real on 2026-09-12.
"""

import os
import queue
import sys
import threading

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import constants


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "watched-artist-search.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "watched-artist-search.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db


def test_musicbrainz_down_is_a_503_not_a_500(fresh_db, monkeypatch):
    import app
    from metadata import MusicBrainzUnavailable

    def _down(_name):
        raise MusicBrainzUnavailable("three 503s in a row")

    monkeypatch.setattr(app, "search_artist_mbid", _down)

    with pytest.raises(HTTPException) as caught:
        app.search_watched_artist("Radiohead")
    assert caught.value.status_code == 503
    assert "MusicBrainz unreachable" in caught.value.detail


def test_musicbrainz_up_still_returns_results(fresh_db, monkeypatch):
    import app

    found = [{"mbid": "a74b1b7f", "name": "Radiohead", "disambiguation": "", "score": 100}]
    monkeypatch.setattr(app, "search_artist_mbid", lambda _name: found)

    assert app.search_watched_artist("Radiohead") == {"results": found}
