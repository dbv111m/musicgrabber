"""
Unit tests for telegram_bot.py pure functions.

No running API server or Telegram token required. Uses a temporary SQLite DB
for cache-related tests.
"""
import os
import sys

import pytest

# Ensure the project root is on sys.path so telegram_bot imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Force test-only env before importing the module -----------------------
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["MUSICGRABBER_API_URL"] = "http://127.0.0.1:1"
os.environ["TELEGRAM_API_URL"] = ""
os.environ["ADMIN_CHAT_ID"] = "12345"

import telegram_bot

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def tmp_db(tmp_path):
    """Point BOT_DB_PATH at a temp SQLite file and init tables."""
    db = tmp_path / "test_bot.db"
    os.environ["TELEGRAM_BOT_DB"] = str(db)
    telegram_bot.init_telegram_tables()
    # Reset cache stats to clean state
    conn = telegram_bot.get_db_connection()
    conn.execute("DELETE FROM cache_stats")
    conn.execute("INSERT INTO cache_stats (id, search_requests, cache_hits) VALUES (1, 0, 0)")
    conn.commit()
    conn.close()
    yield db


# ============================================================================
# extract_url_source
# ============================================================================

@pytest.mark.parametrize("url,expected_source,expected_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/v/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://soundcloud.com/artist/track-name", "soundcloud", None),
    ("https://soundcloud.com/artist", "soundcloud", None),
])
def test_extract_url_source(url, expected_source, expected_id):
    result = telegram_bot.extract_url_source(url)
    assert result is not None
    source, video_id, source_url = result
    assert source == expected_source
    assert source_url == url
    if expected_id is not None:
        assert video_id == expected_id


def test_extract_url_source_none():
    assert telegram_bot.extract_url_source("just a search query") is None
    assert telegram_bot.extract_url_source("Arctic Monkeys - Do I Wanna Know?") is None
    assert telegram_bot.extract_url_source("") is None


# ============================================================================
# normalize_query
# ============================================================================

@pytest.mark.parametrize("query,expected", [
    ("Arctic Monkeys - Do I Wanna Know?", "arctic monkeys - do i wanna know"),
    ("  The  Beatles   Let It Be  ", "the beatles let it be"),
    ("QUEEN - Bohemian Rhapsody (Remastered)", "queen - bohemian rhapsody remastered"),
    ("", ""),
])
def test_normalize_query(query, expected):
    assert telegram_bot.normalize_query(query) == expected


# ============================================================================
# fuzzy_variants
# ============================================================================

def test_fuzzy_variants_includes_original():
    variants = telegram_bot.fuzzy_variants("the beatles let it be")
    assert "the beatles let it be" in variants


def test_fuzzy_variants_strips_stopwords():
    variants = telegram_bot.fuzzy_variants("the beatles let it be")
    assert "beatles let it be" in variants


def test_fuzzy_variants_first_two_words():
    variants = telegram_bot.fuzzy_variants("arctic monkeys do i wanna know")
    assert "arctic monkeys" in variants


def test_fuzzy_variants_last_two_words():
    variants = telegram_bot.fuzzy_variants("arctic monkeys do i wanna know")
    assert "wanna know" in variants


def test_fuzzy_variants_dedup():
    variants = telegram_bot.fuzzy_variants("hello hello")
    assert variants.count("hello") <= 1


def test_fuzzy_variants_capped_at_5():
    variants = telegram_bot.fuzzy_variants("a b c d e f g h i j")
    assert len(variants) <= 5


# ============================================================================
# rerank_results
# ============================================================================

def test_rerank_results_puts_best_match_first():
    results = [
        {"video_id": "a", "title": "Something Completely Different", "artist": "Artist X"},
        {"video_id": "b", "title": "Bohemian Rhapsody", "artist": "Queen"},
        {"video_id": "c", "title": "Another Unrelated Track", "artist": "Artist Y"},
    ]
    ranked = telegram_bot.rerank_results(results, "queen bohemian rhapsody")
    assert ranked[0]["video_id"] == "b"


def test_rerank_results_empty():
    assert telegram_bot.rerank_results([], "anything") == []


def test_rerank_results_single():
    results = [{"video_id": "x", "title": "Only Track", "artist": "Only Artist"}]
    assert telegram_bot.rerank_results(results, "query") == results


# ============================================================================
# Search cache (global, no chat_id)
# ============================================================================

def test_save_and_get_cached_search(tmp_db):
    results = [
        {"video_id": "v1", "title": "Track One", "artist": "Artist One",
         "source_url": "https://example.com/1.mp3", "quality": "320kbps", "duration": "3:30",
         "source": "youtube"},
        {"video_id": "v2", "title": "Track Two", "artist": "Artist Two",
         "source_url": "", "quality": "", "duration": "", "source": "soundcloud"},
    ]
    telegram_bot.save_search_results("the beatles let it be", "all", results)

    cached = telegram_bot.get_cached_search("the beatles let it be")
    assert cached is not None
    assert len(cached) == 2
    assert cached[0]["video_id"] == "v1"
    assert cached[0]["title"] == "Track One"
    assert cached[0]["artist"] == "Artist One"


def test_get_cached_search_case_insensitive(tmp_db):
    telegram_bot.save_search_results("The Beatles Let It Be", "all", [
        {"video_id": "v1", "title": "Track", "artist": "Artist",
         "source_url": "", "quality": "", "duration": "", "source": "youtube"},
    ])
    # Different case / extra spaces should still hit
    cached = telegram_bot.get_cached_search("  THE BEATLES  LET IT BE  ")
    assert cached is not None
    assert len(cached) == 1


def test_get_cached_search_none_when_empty(tmp_db):
    assert telegram_bot.get_cached_search("nonexistent query xyz") is None


def test_get_cached_search_overwrites(tmp_db):
    telegram_bot.save_search_results("query", "all", [
        {"video_id": "v1", "title": "First", "artist": "A",
         "source_url": "", "quality": "", "duration": "", "source": "youtube"},
    ])
    telegram_bot.save_search_results("query", "all", [
        {"video_id": "v2", "title": "Second", "artist": "B",
         "source_url": "", "quality": "", "duration": "", "source": "soundcloud"},
    ])
    cached = telegram_bot.get_cached_search("query")
    assert len(cached) == 1
    assert cached[0]["video_id"] == "v2"


# ============================================================================
# CDN link cache (no TTL, invalidate on error only)
# ============================================================================

def test_save_and_get_cached_cdn_link(tmp_db):
    conn = telegram_bot.get_db_connection()
    telegram_bot.save_cdn_link(
        conn, "v1",
        "https://source.com/track.mp3",
        "https://cdn.com/track.mp3",
        "youtube",
    )
    conn.commit()
    conn.close()
    cached = telegram_bot.get_cached_cdn_link("v1")
    assert cached == "https://cdn.com/track.mp3"


def test_get_cached_cdn_link_none_when_missing(tmp_db):
    assert telegram_bot.get_cached_cdn_link("nonexistent") is None


def test_cdn_link_updated_on_resave(tmp_db):
    conn = telegram_bot.get_db_connection()
    telegram_bot.save_cdn_link(conn, "v1", "src1", "cdn1", "youtube")
    telegram_bot.save_cdn_link(conn, "v1", "src2", "cdn2", "youtube")
    conn.commit()
    conn.close()

    cached = telegram_bot.get_cached_cdn_link("v1")
    assert cached == "cdn2"


# ============================================================================
# bot_jobs query column backfill
# ============================================================================

def test_bot_jobs_has_query_column(tmp_db):
    conn = telegram_bot.get_db_connection()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(bot_jobs)").fetchall()]
    conn.close()
    assert "query" in cols


# ============================================================================
# clear_expired_cache
# ============================================================================

def test_clear_expired_cache_removes_old_search(tmp_db, monkeypatch):
    import time as _time
    # Save a result, then monkeypatch _now to be far in the future
    telegram_bot.save_search_results("old query", "all", [
        {"video_id": "v1", "title": "Track", "artist": "Artist",
         "source_url": "", "quality": "", "duration": "", "source": "youtube"},
    ])
    # TTL is 720h = 30 days. Move clock 31 days forward.
    monkeypatch.setattr(telegram_bot, "_now", lambda: _time.time() + 31 * 86400)
    telegram_bot.clear_expired_cache()
    assert telegram_bot.get_cached_search("old query") is None


def test_clear_expired_cache_keeps_recent_search(tmp_db):
    telegram_bot.save_search_results("recent query", "all", [
        {"video_id": "v1", "title": "Track", "artist": "Artist",
         "source_url": "", "quality": "", "duration": "", "source": "youtube"},
    ])
    telegram_bot.clear_expired_cache()
    assert telegram_bot.get_cached_search("recent query") is not None


def test_clear_expired_cache_does_not_remove_cdn_links(tmp_db):
    conn = telegram_bot.get_db_connection()
    telegram_bot.save_cdn_link(conn, "v1", "src", "cdn", "youtube")
    conn.commit()
    conn.close()
    telegram_bot.clear_expired_cache()
    assert telegram_bot.get_cached_cdn_link("v1") == "cdn"


# ============================================================================
# Job cleanup
# ============================================================================

def test_cleanup_old_jobs_removes_old(tmp_db):
    import time as _time
    import uuid
    uid = str(uuid.uuid4())[:8]
    conn = telegram_bot.get_db_connection()
    # Insert an old job (31 days ago)
    old_ts = _time.time() - 31 * 86400
    conn.execute(
        "INSERT INTO bot_jobs (job_id, chat_id, title, artist, video_id, query, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
        (f"old_job_{uid}", 123, "Old Track", "Old Artist", "old_video", "old query", old_ts),
    )
    # Insert a recent job
    recent_ts = _time.time()
    conn.execute(
        "INSERT INTO bot_jobs (job_id, chat_id, title, artist, video_id, query, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
        (f"recent_job_{uid}", 123, "Recent Track", "Recent Artist", "recent_video", "recent query", recent_ts),
    )
    conn.commit()
    conn.close()

    deleted = telegram_bot.cleanup_old_jobs()
    assert deleted == 1

    # Verify recent job still exists
    conn = telegram_bot.get_db_connection()
    row = conn.execute(
        "SELECT job_id FROM bot_jobs WHERE job_id = ?", (f"recent_job_{uid}",)
    ).fetchone()
    conn.close()
    assert row is not None


def test_cleanup_old_jobs_keeps_recent(tmp_db):
    import time as _time
    import uuid
    uid = str(uuid.uuid4())[:8]
    conn = telegram_bot.get_db_connection()
    recent_ts = _time.time()
    conn.execute(
        "INSERT INTO bot_jobs (job_id, chat_id, title, artist, video_id, query, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
        (f"recent_job_{uid}", 123, "Recent Track", "Recent Artist", "recent_video", "recent query", recent_ts),
    )
    conn.commit()
    conn.close()

    deleted = telegram_bot.cleanup_old_jobs()
    assert deleted == 0


# ============================================================================
# Cache stats
# ============================================================================

def test_record_search_request_and_stats(tmp_db):
    telegram_bot.record_search_request(cache_hit=True)
    telegram_bot.record_search_request(cache_hit=True)
    telegram_bot.record_search_request(cache_hit=False)

    stats = telegram_bot.get_cache_stats()
    assert stats["search_requests"] == 3
    assert stats["cache_hits"] == 2
    assert stats["cache_misses"] == 1
    assert stats["hit_rate"] == pytest.approx(66.67, rel=0.01)


def test_record_search_request_empty_stats(tmp_db):
    stats = telegram_bot.get_cache_stats()
    assert stats["search_requests"] == 0
    assert stats["cache_hits"] == 0
    assert stats["cache_misses"] == 0
    assert stats["hit_rate"] == 0.0

