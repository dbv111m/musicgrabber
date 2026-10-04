"""Persistent acquisition ledger and automatic quality-ladder regressions."""

import os
import queue
import sqlite3
import sys
import threading
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acquisition
import bulk_import
import db
import downloads
import notifications
import search


def _candidate(source, video_id, quality, score=80, title="Track", artist="Artist"):
    return {
        "source": source,
        "video_id": video_id,
        "quality": quality,
        "relevance_score": score,
        "title": f"{artist} - {title}",
        "artist": artist,
        "channel": artist,
    }


def _capture_notifications(monkeypatch):
    """Record what would actually be sent, and send none of it."""
    sent = []

    def should_notify(notification_type, status, error=None, user_id=None):
        sent.append((notification_type, status, error))
        return False

    monkeypatch.setattr(notifications, "_should_notify", should_notify)
    return sent


def _worker_fails(job_id, error):
    """What a real worker does on failure: mark the job and announce it."""
    downloads._update_job(job_id, status="failed", error=error)
    downloads.send_notification(
        notification_type="error", title="Track", artist="Artist",
        status="failed", error=error,
    )


def test_automatic_ladder_keeps_lossless_ahead_of_lossy_preference():
    candidates = [
        _candidate("freemp3cloud", "mp3", "320kbps", score=99),
        _candidate("monochrome", "flac", "LOSSLESS", score=70),
    ]

    ranked, rejected = acquisition.rank_automatic_candidates(
        candidates, "Artist", "Track", priority_source="freemp3cloud"
    )

    assert rejected == []
    assert [candidate["video_id"] for candidate in ranked] == ["flac", "mp3"]


def test_preferred_source_breaks_tie_inside_quality_tier():
    candidates = [
        _candidate("soulseek", "peer-a", "FLAC", score=95),
        _candidate("monochrome", "mono", "LOSSLESS", score=80),
    ]

    ranked, _ = acquisition.rank_automatic_candidates(
        candidates, "Artist", "Track", priority_source="monochrome"
    )

    assert [candidate["video_id"] for candidate in ranked] == ["mono", "peer-a"]


def test_wrong_lossless_recording_is_rejected_before_quality_ranking():
    candidates = [
        _candidate("monochrome", "wrong", "LOSSLESS", title="Different Song", artist="Other"),
        _candidate("youtube", "right", None, title="Track", artist="Artist"),
    ]

    ranked, rejected = acquisition.rank_automatic_candidates(
        candidates, "Artist", "Track"
    )

    assert [candidate["video_id"] for candidate in ranked] == ["right"]
    assert [candidate["video_id"] for candidate in rejected] == ["wrong"]


def test_unknown_lossy_sources_fall_through_direct_then_soundcloud_then_youtube():
    candidates = [
        _candidate("youtube", "yt", None),
        _candidate("soundcloud", "sc", None),
        _candidate("zvu4no", "direct", "MP3"),
    ]

    ranked, _ = acquisition.rank_automatic_candidates(candidates, "Artist", "Track")

    assert [candidate["video_id"] for candidate in ranked] == ["direct", "sc", "yt"]


def test_automatic_search_can_rank_every_capped_source_result(monkeypatch):
    def fake_events(*args, **kwargs):
        yield {"type": "start", "query": "Artist - Track", "sources": ["a", "b"]}
        yield {"type": "source", "source": "a", "status": "done", "results": [
            {"video_id": "a1", "relevance_score": 100},
            {"video_id": "a2", "relevance_score": 90},
        ]}
        yield {"type": "source", "source": "b", "status": "done", "results": [
            {"video_id": "b1", "relevance_score": 80},
        ]}
        yield {"type": "done"}

    monkeypatch.setattr(search, "_search_all_events", fake_events)

    ordinary, _ = search.search_all("Artist - Track", limit=1)
    automatic, _ = search.search_all(
        "Artist - Track", limit=1, return_all_source_results=True
    )

    assert [result["video_id"] for result in ordinary] == ["a1"]
    assert [result["video_id"] for result in automatic] == ["a1", "a2", "b1"]


def test_exact_album_resolution_honours_monochrome_exclusion(monkeypatch):
    monkeypatch.setattr(
        bulk_import,
        "db_conn",
        lambda: (_ for _ in ()).throw(
            AssertionError("excluded Monochrome shortcut touched the database")
        ),
    )

    assert bulk_import._resolve_album_up_front(
        "import-1", "/music/Albums/Artist/Album", ["youtube", "soundcloud"]
    ) == {}


def test_requested_youtube_video_fallback_is_explicit_and_respects_allowlist(monkeypatch):
    monkeypatch.setattr(
        downloads,
        "get_setting_bool",
        lambda key, default=False, user_id=None: key == "youtube_requested_video_fallback",
    )

    candidate = downloads._requested_video_fallback_candidate(
        "abcdefghijk", "Artist", "Track", {"youtube", "monochrome"}, "user-1"
    )

    assert candidate == {
        "video_id": "abcdefghijk",
        "source": "youtube",
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "artist": "Artist",
        "title": "Track",
        "requested_video_fallback": True,
    }
    assert downloads._requested_video_fallback_candidate(
        "abcdefghijk", "Artist", "Track", {"monochrome"}, "user-1"
    ) is None


def test_target_survives_jobs_and_cycles_with_append_only_attempts(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE acquisition_targets (
            id TEXT PRIMARY KEY, user_id TEXT, owner_type TEXT NOT NULL,
            owner_key TEXT NOT NULL, mode TEXT NOT NULL, artist TEXT, title TEXT,
            isrc TEXT, allowed_sources TEXT, priority_source TEXT,
            convert_audio INTEGER, destination_json TEXT,
            cycle_count INTEGER DEFAULT 0, attempt_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', last_attempt_at TIMESTAMP,
            last_error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,
            UNIQUE(owner_type, owner_key)
        );
        CREATE TABLE acquisition_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL,
            cycle_number INTEGER NOT NULL, job_id TEXT, sequence INTEGER NOT NULL,
            source TEXT NOT NULL, candidate_id TEXT, candidate_title TEXT,
            candidate_artist TEXT, candidate_quality TEXT,
            status TEXT DEFAULT 'attempting', error TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,
            UNIQUE(target_id, cycle_number, sequence)
        );
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(acquisition, "db_conn", fake_db_conn)

    target_id = acquisition.ensure_acquisition_target(
        owner_type="watched_playlist",
        owner_key="playlist-1:track-hash",
        mode="automatic",
        artist="Artist",
        title="Track",
        user_id="user-1",
        allowed_sources="monochrome,youtube",
    )
    assert acquisition.ensure_acquisition_target(
        owner_type="watched_playlist",
        owner_key="playlist-1:track-hash",
        mode="automatic",
        artist="Artist",
        title="Track",
        user_id="user-1",
        allowed_sources="youtube",
    ) == target_id

    first_cycle = acquisition.begin_acquisition_cycle(target_id)
    first_attempt = acquisition.start_acquisition_attempt(
        target_id, first_cycle, "job-old", _candidate("monochrome", "mono", "LOSSLESS")
    )
    acquisition.finish_acquisition_attempt(first_attempt, "failed", "proxy unavailable")

    second_cycle = acquisition.begin_acquisition_cycle(target_id)
    second_attempt = acquisition.start_acquisition_attempt(
        target_id, second_cycle, "job-new", _candidate("youtube", "yt", None)
    )
    acquisition.finish_acquisition_attempt(second_attempt, "completed")

    target = conn.execute(
        "SELECT cycle_count, attempt_count, status, allowed_sources FROM acquisition_targets"
    ).fetchone()
    attempts = conn.execute(
        "SELECT cycle_number, job_id, source, status, error FROM acquisition_attempts ORDER BY id"
    ).fetchall()

    assert target == (2, 2, "completed", "youtube")
    assert attempts == [
        (1, "job-old", "monochrome", "failed", "proxy unavailable"),
        (2, "job-new", "youtube", "completed", None),
    ]


def test_saved_candidates_keep_download_fields_for_manual_rescue():
    candidate = {
        "video_id": "direct-1",
        "title": "Artist - Track",
        "artist": "Artist",
        "channel": "Artist",
        "source": "freemp3cloud",
        "source_url": "https://meln.top/audio/direct-1.mp3",
        "quality": "320kbps",
        "relevance_score": 91,
        "score_breakdown": ["title_match=100"],
    }

    summary = acquisition.acquisition_candidate_summary(candidate)

    assert summary["source_url"] == candidate["source_url"]
    assert summary["score"] == 91
    assert summary["breakdown"] == ["title_match=100"]
    assert acquisition.acquisition_candidate_key(summary) == acquisition.acquisition_candidate_key(candidate)


def test_stored_rescue_candidates_span_queue_rows_and_deduplicate():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, acquisition_target_id TEXT, video_id TEXT,
            source TEXT, source_url TEXT, slskd_username TEXT,
            slskd_filename TEXT, slskd_size INTEGER
        );
        CREATE TABLE search_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, query TEXT,
            decision_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO jobs VALUES (
            'job-1', 'target-1', 'yt-picked', 'youtube',
            'https://www.youtube.com/watch?v=yt-picked', NULL, NULL, NULL
        );
    """)
    selected = acquisition.acquisition_candidate_summary(
        _candidate("youtube", "yt-picked", None)
    )
    runner = acquisition.acquisition_candidate_summary(
        _candidate("monochrome", "mono-runner", "LOSSLESS")
        | {"source_url": "monochrome://track/mono-runner?isrc=GB123"}
    )
    conn.execute(
        "INSERT INTO search_decisions (job_id, query, decision_json) VALUES (?, ?, ?)",
        ("job-1", "Artist - Track", __import__("json").dumps({
            "selected": selected,
            "runners_up": [runner, runner],
        })),
    )

    candidates = acquisition.stored_rescue_candidates(conn, "target-1")

    assert [candidate["video_id"] for candidate in candidates] == [
        "yt-picked", "mono-runner"
    ]
    assert candidates[0]["source_url"].endswith("yt-picked")
    assert candidates[1]["source_url"].startswith("monochrome://")
    assert all(candidate["candidate_key"] for candidate in candidates)


def test_automatic_cycle_falls_through_and_records_each_source(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cycle.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    monkeypatch.setattr(acquisition, "db_conn", db.db_conn)
    monkeypatch.setattr(downloads, "db_conn", db.db_conn)
    db.init_db()

    target_id = acquisition.ensure_acquisition_target(
        owner_type="watched_playlist",
        owner_key="playlist-1:track-hash",
        mode="automatic",
        artist="Artist",
        title="Track",
        user_id="user-1",
    )
    cycle = acquisition.begin_acquisition_cycle(target_id)
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, video_id, artist, title, status, source,
                acquisition_target_id, acquisition_cycle)
               VALUES ('job-1', 'mono', 'Artist', 'Track', 'queued',
                       'monochrome', ?, ?)""",
            (target_id, cycle),
        )
        conn.commit()

    def fake_provider(job_id, video_id, *args, source_url=None, **kwargs):
        if video_id == "mono":
            _worker_fails(job_id, "lossless unavailable")
        else:
            downloads._update_job(job_id, status="completed", error=None)

    next_candidates = iter([
        _candidate("youtube", "yt", None),
    ])
    monkeypatch.setattr(downloads, "process_download", fake_provider)
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *args, **kwargs: True)
    sent = _capture_notifications(monkeypatch)
    monkeypatch.setattr(
        downloads,
        "_find_alternate_search_candidate",
        lambda *args, **kwargs: next(next_candidates, None),
    )

    downloads.process_acquisition_cycle(
        "job-1",
        _candidate("monochrome", "mono", "LOSSLESS"),
        "Artist",
        "Track",
        target_id=target_id,
        cycle=cycle,
        automatic=True,
        convert_audio=False,
    )

    with db.db_conn() as conn:
        target = conn.execute(
            "SELECT status, cycle_count, attempt_count, last_error FROM acquisition_targets"
        ).fetchone()
        attempts = conn.execute(
            "SELECT source, status, error FROM acquisition_attempts ORDER BY sequence"
        ).fetchall()
        job = conn.execute(
            "SELECT status, source, source_history FROM jobs WHERE id = 'job-1'"
        ).fetchone()

    assert target == ("completed", 1, 2, None)
    assert attempts == [
        ("monochrome", "failed", "lossless unavailable"),
        ("youtube", "completed", None),
    ]
    assert job == ("completed", "youtube", '["monochrome", "youtube"]')
    # Monochrome's failure was rescued, so nobody hears about it.
    assert sent == []


def test_manual_cycle_never_calls_cross_source_selector(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "manual.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    monkeypatch.setattr(acquisition, "db_conn", db.db_conn)
    monkeypatch.setattr(downloads, "db_conn", db.db_conn)
    db.init_db()

    target_id = acquisition.ensure_acquisition_target(
        owner_type="manual", owner_key="job-1", mode="manual",
        artist="Artist", title="Track", user_id="user-1",
        allowed_sources="soulseek",
    )
    cycle = acquisition.begin_acquisition_cycle(target_id)
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, video_id, artist, title, status, source,
                acquisition_target_id, acquisition_cycle)
               VALUES ('job-1', 'peer-file', 'Artist', 'Track', 'queued',
                       'soulseek', ?, ?)""",
            (target_id, cycle),
        )
        conn.commit()

    monkeypatch.setattr(
        downloads,
        "process_slskd_download",
        lambda job_id, *args, **kwargs: _worker_fails(job_id, "peer vanished"),
    )
    sent = _capture_notifications(monkeypatch)
    monkeypatch.setattr(
        downloads,
        "_find_alternate_search_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("manual selection crossed sources")
        ),
    )

    downloads.process_acquisition_cycle(
        "job-1",
        {
            **_candidate("soulseek", "peer-file", "FLAC"),
            "slskd_username": "peer",
            "slskd_filename": "Artist/Track.flac",
            "slskd_size": 123,
        },
        "Artist",
        "Track",
        target_id=target_id,
        cycle=cycle,
        automatic=False,
        convert_audio=False,
        allowed_sources={"soulseek"},
    )

    with db.db_conn() as conn:
        assert conn.execute(
            "SELECT status, attempt_count, last_error FROM acquisition_targets"
        ).fetchone() == ("failed", 1, "peer vanished")
    # One failure notice, from the cycle, not one per attempt.
    assert sent == [("error", "failed", "peer vanished")]


def test_later_watched_refresh_reuses_target_without_reusing_queue_job(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "watched-cycles.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    monkeypatch.setattr(acquisition, "db_conn", db.db_conn)
    monkeypatch.setattr(bulk_import, "db_conn", db.db_conn)
    monkeypatch.setattr(bulk_import, "spawn_daemon_thread", lambda *args, **kwargs: None)
    db.init_db()

    first_import = bulk_import.start_bulk_import_for_tracks(
        [("Artist", "Track")],
        convert_audio=False,
        watch_playlist_id="playlist-1",
        user_id="user-1",
    )
    second_import = bulk_import.start_bulk_import_for_tracks(
        [("Artist", "Track")],
        convert_audio=False,
        watch_playlist_id="playlist-1",
        user_id="user-1",
    )

    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT import_id, acquisition_target_id
               FROM bulk_import_tracks ORDER BY id"""
        ).fetchall()
        target_count = conn.execute("SELECT COUNT(*) FROM acquisition_targets").fetchone()[0]

    assert [row[0] for row in rows] == [first_import, second_import]
    assert rows[0][1] == rows[1][1]
    assert target_count == 1
