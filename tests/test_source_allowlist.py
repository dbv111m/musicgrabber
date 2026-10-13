"""Regression tests for strict watched-playlist source allow-lists."""

import os
import sqlite3
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import constants
import downloads
import search


def test_explicit_disabled_source_does_not_expand_to_all(monkeypatch):
    """A saved source that is later disabled must yield no search legs."""
    monkeypatch.setattr(search, "_enabled_sources", lambda include_soulseek=False: {
        "youtube": {"label": "YouTube"},
    })
    monkeypatch.setattr(search.servicecheck, "refresh_sources_async", lambda sources: None)
    monkeypatch.setattr(search.servicecheck, "is_source_available", lambda source: True)

    events = list(search._search_all_events(
        "Artist - Track", 10, sources=["monochrome"], include_soulseek=True
    ))

    assert events == [
        {"type": "start", "query": "Artist - Track", "sources": []},
        {"type": "done"},
    ]


def test_automated_cache_key_keeps_empty_allowlist_distinct(monkeypatch):
    monkeypatch.setattr(search, "_enabled_sources", lambda include_soulseek=False: {
        "youtube": {"label": "YouTube"},
    })
    monkeypatch.setattr(search.servicecheck, "is_source_available", lambda source: True)

    unrestricted = search._automated_search_cache_key("Track", 10, None, False)
    explicitly_empty = search._automated_search_cache_key("Track", 10, [], False)

    assert unrestricted != explicitly_empty
    assert unrestricted[3] == ("youtube",)
    assert explicitly_empty[3] == ()


def test_alternate_search_rejects_source_outside_allowlist(monkeypatch):
    calls = []
    waits = []

    def fake_search_all(query, limit, sources=None, include_soulseek=False,
                        slot_wait=0.0, status_out=None,
                        return_all_source_results=False):
        calls.append(sources)
        waits.append(slot_wait)
        # Defensive regression guard: even if a search provider misbehaves and
        # returns another source, the fallback selector must refuse it.
        return ([{
            "video_id": "yt-outside-list",
            "title": "Artist - Track",
            "channel": "Artist",
            "source": "youtube",
            "relevance_score": 100,
        }], None)

    monkeypatch.setattr(search, "search_all", fake_search_all)
    monkeypatch.setattr(search, "log_ranked_results", lambda *args, **kwargs: None)

    result = downloads._find_alternate_search_candidate(
        "Artist - Track",
        attempted_ids={"mono-failed"},
        allowed_sources={"monochrome"},
    )

    assert calls == [["monochrome"]]
    assert result is None
    # A download is riding on this search, so it must queue for a busy source
    # rather than come back empty-handed and fail the job over a collision.
    assert waits == [constants.SEARCH_SLOT_WAIT_AUTOMATED]


def test_fallback_stops_when_only_allowed_source_is_excluded(monkeypatch):
    monkeypatch.setattr(
        search,
        "search_all",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search widened")),
    )

    result = downloads._find_alternate_search_candidate(
        "Artist - Track",
        attempted_ids={"mono-failed"},
        exclude_sources={"monochrome"},
        allowed_sources={"monochrome"},
    )

    assert result is None


def test_job_allowlist_prefers_bulk_import_snapshot(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE watched_playlists (id TEXT PRIMARY KEY, preferred_sources TEXT);
        CREATE TABLE watched_playlist_tracks (playlist_id TEXT, job_id TEXT);
        CREATE TABLE bulk_imports (id TEXT PRIMARY KEY, preferred_sources TEXT);
        CREATE TABLE bulk_import_tracks (import_id TEXT, job_id TEXT);
        INSERT INTO watched_playlists VALUES ('playlist-1', 'soundcloud');
        INSERT INTO watched_playlist_tracks VALUES ('playlist-1', 'job-1');
        INSERT INTO bulk_imports VALUES ('import-1', 'monochrome');
        INSERT INTO bulk_import_tracks VALUES ('import-1', 'job-1');
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)

    assert downloads.get_job_source_allowlist("job-1") == {"monochrome"}
    assert downloads.get_job_source_allowlist("ordinary-job") is None


def test_job_allowlist_uses_playlist_for_manual_candidate(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE watched_playlists (id TEXT PRIMARY KEY, preferred_sources TEXT);
        CREATE TABLE watched_playlist_tracks (playlist_id TEXT, job_id TEXT);
        CREATE TABLE bulk_imports (id TEXT PRIMARY KEY, preferred_sources TEXT);
        CREATE TABLE bulk_import_tracks (import_id TEXT, job_id TEXT);
        INSERT INTO watched_playlists VALUES ('playlist-1', 'soundcloud,monochrome');
        INSERT INTO watched_playlist_tracks VALUES ('playlist-1', 'job-1');
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)

    assert downloads.get_job_source_allowlist("job-1") == {"soundcloud", "monochrome"}
