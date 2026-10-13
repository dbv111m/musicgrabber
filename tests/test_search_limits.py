"""Unit tests for balancing merged search results across enabled sources."""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search
from models import SearchResult


@pytest.fixture(autouse=True)
def fixed_search_admission_setting(monkeypatch):
    """These admission tests do not need the live settings database."""
    monkeypatch.setattr(search, "get_setting_int", lambda *_a, **_k: 1)
    with search._SOURCE_SEARCH_SLOTS_LOCK:
        search._SOURCE_SEARCH_SLOTS.clear()
    yield
    with search._SOURCE_SEARCH_SLOTS_LOCK:
        search._SOURCE_SEARCH_SLOTS.clear()


def test_search_result_contract_names_the_rank_as_relevance():
    payload = SearchResult(
        video_id="result-1",
        title="Track",
        channel="Artist",
        duration="3:30",
        thumbnail="",
    ).model_dump()

    assert payload["relevance_score"] == 40
    assert "quality_score" not in payload


def test_browser_projection_keeps_soulseek_artist_separate_from_peer():
    payload = search.project_search_result({
        "video_id": "slskd_deadbeef",
        "title": "Ignorance",
        "artist": "Paramore",
        "channel": "2jqll9htuy62asp1wu",
        "duration": "3:39",
        "thumbnail": "",
        "source": "soulseek",
        "quality": "FLAC",
        "relevance_score": 300,
        "slskd_username": "2jqll9htuy62asp1wu",
        "slskd_filename": r"@@kvkwm\Music\Paramore\Brand New Eyes\2. Ignorance.flac",
        "slskd_size": 40_000_000,
        "queue_length": 7,
    })

    assert payload["artist"] == "Paramore"
    assert payload["channel"] == payload["slskd_username"] == "2jqll9htuy62asp1wu"
    assert payload["queue_length"] == 7


def test_source_search_slot_rejects_overlap_and_recovers(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    def slow_search(_query, _limit):
        started.set()
        release.wait(timeout=5)
        return []

    cfg = {"search_fn": slow_search}
    worker = threading.Thread(
        target=search._run_source_search,
        args=("unit", cfg, "query", 5),
    )
    worker.start()
    assert started.wait(timeout=2)

    with pytest.raises(search.SourceSearchBusy):
        search._run_source_search("unit", cfg, "second query", 5)

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert search._run_source_search("unit", {"search_fn": lambda q, n: [q]}, "third", 5) == ["third"]


def test_soulseek_empty_retry_can_be_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(search, "slskd_enabled", lambda: True)
    monkeypatch.setattr(search, "search_slskd", lambda *args, **kwargs: calls.append(1) or [])
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)

    assert search.search_soulseek("Artist - Track", retry_empty=False) == []
    assert len(calls) == 1

    assert search.search_soulseek("Artist - Track", retry_empty=True) == []
    assert len(calls) == 3


def test_multi_source_run_disables_soulseek_empty_retry(monkeypatch):
    calls = []

    def fake_soulseek(_query, _limit, retry_empty=True):
        calls.append(retry_empty)
        return []

    monkeypatch.setattr(search, "search_soulseek", fake_soulseek)
    search._run_source_search(
        "soulseek",
        {"search_fn": fake_soulseek},
        "Artist - Track",
        5,
        retry_empty_soulseek=False,
    )

    assert calls == [False]


def test_single_source_can_fill_requested_result_page():
    assert search._per_source_result_cap("youtube", 15, 1) == 15


def test_two_sources_split_requested_result_page():
    assert search._per_source_result_cap("youtube", 15, 2) == 8
    assert search._per_source_result_cap("soundcloud", 15, 2) == 8


def test_normal_multi_source_caps_are_preserved():
    assert search._per_source_result_cap("youtube", 15, 4) == 6
    assert search._per_source_result_cap("soundcloud", 15, 4) == 4


def test_event_stream_uses_expanded_cap_for_sparse_sources(monkeypatch):
    def results_for(source_name):
        def search_source(_query, limit):
            return [
                {"video_id": f"{source_name}-{i}", "relevance_score": 100 - i}
                for i in range(limit)
            ]
        return search_source

    active = {
        "youtube": {"search_fn": results_for("youtube")},
        "soundcloud": {"search_fn": results_for("soundcloud")},
    }
    monkeypatch.setattr(search, "_enabled_sources", lambda include_soulseek=False: active)
    monkeypatch.setattr(search, "_apply_blacklist_filter", lambda items, source=None: items)
    monkeypatch.setattr(search, "_mb_duration_lookup", lambda query: None)
    monkeypatch.setattr(search, "_mb_album_lookup", lambda query: None)
    monkeypatch.setattr(search.servicecheck, "record_search_success", lambda source: None)

    events = list(search._search_all_events(
        "Artist - Track",
        15,
        include_soulseek=True,
        enforce_availability=False,
    ))
    batches = [event for event in events if event.get("status") == "done"]

    assert len(batches) == 2
    assert {event["count"] for event in batches} == {8}


def test_automated_search_cache_returns_isolated_copies(monkeypatch):
    calls = []
    search.clear_automated_search_cache()
    monkeypatch.setattr(search, "_automated_search_cache_key", lambda *args: ("key",))

    def fake_search_all(*args, **kwargs):
        calls.append(1)
        return ([{"video_id": "one", "relevance_score": 10}], {"album_title": "Album"})

    monkeypatch.setattr(search, "search_all", fake_search_all)
    first, _ = search.search_all_cached("Artist - Track", 10)
    first[0]["relevance_score"] = 999
    second, album = search.search_all_cached("Artist - Track", 10)

    assert len(calls) == 1
    assert second[0]["relevance_score"] == 10
    assert album == {"album_title": "Album"}


def test_automated_search_cache_expires(monkeypatch):
    clock = {"now": 1000.0}
    calls = []
    search.clear_automated_search_cache()
    monkeypatch.setattr(search, "_automated_search_cache_key", lambda *args: ("key",))
    monkeypatch.setattr(search.time, "time", lambda: clock["now"])
    monkeypatch.setattr(search, "AUTOMATED_SEARCH_CACHE_TTL_SECONDS", 10)
    monkeypatch.setattr(
        search,
        "search_all",
        lambda *args, **kwargs: (calls.append(1) or [{"video_id": str(len(calls))}], None),
    )

    search.search_all_cached("Artist - Track", 10)
    clock["now"] += 11
    results, _ = search.search_all_cached("Artist - Track", 10)

    assert len(calls) == 2
    assert results[0]["video_id"] == "2"


def test_automated_search_cache_does_not_remember_empty_failures(monkeypatch):
    calls = []
    search.clear_automated_search_cache()
    monkeypatch.setattr(search, "_automated_search_cache_key", lambda *args: ("key",))

    def fake_search_all(*args, **kwargs):
        calls.append(1)
        return ([], None)

    monkeypatch.setattr(search, "search_all", fake_search_all)
    search.search_all_cached("Artist - Missing", 10)
    search.search_all_cached("Artist - Missing", 10)

    assert len(calls) == 2


def _race_setup(monkeypatch, mb_delay):
    """One quick source and a MusicBrainz lookup that takes its time."""
    def quick(_query, limit):
        return [
            {"video_id": "wrong-length", "source": "jiosaavn", "duration": "3:38", "relevance_score": 200},
            {"video_id": "right-length", "source": "jiosaavn", "duration": "5:55", "relevance_score": 190},
        ]

    def slow_mb(_query):
        time.sleep(mb_delay)
        return 355.0

    monkeypatch.setattr(search, "_enabled_sources", lambda include_soulseek=False: {"jiosaavn": {"search_fn": quick}})
    monkeypatch.setattr(search, "_apply_blacklist_filter", lambda items, source=None: items)
    monkeypatch.setattr(search, "_mb_duration_lookup", slow_mb)
    monkeypatch.setattr(search, "_mb_album_lookup", lambda query: None)
    monkeypatch.setattr(search.servicecheck, "record_search_success", lambda source: None)


def test_sources_quicker_than_musicbrainz_still_get_the_duration_score(monkeypatch):
    _race_setup(monkeypatch, mb_delay=0.3)

    events = list(search._search_all_events("Queen - Bohemian Rhapsody", 10, enforce_availability=False))
    kinds = [e["type"] for e in events]
    assert kinds.index("source") < kinds.index("rescore") < kinds.index("done")

    rescored = {r["video_id"]: r["relevance_score"] for r in next(e for e in events if e["type"] == "rescore")["results"]}
    assert rescored == {"wrong-length": 200 - 60, "right-length": 190 + 40}


def test_blocking_search_ranks_by_the_late_duration_score(monkeypatch):
    _race_setup(monkeypatch, mb_delay=0.3)
    monkeypatch.setattr(search.servicecheck, "refresh_sources_async", lambda names: None)
    monkeypatch.setattr(search.servicecheck, "is_source_available", lambda name: True)
    results, _ = search.search_all("Queen - Bohemian Rhapsody", 10)
    assert [r["video_id"] for r in results] == ["right-length", "wrong-length"]
