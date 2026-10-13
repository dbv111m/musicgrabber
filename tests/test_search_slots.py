"""
Queueing for a provider's admission slot, rather than barging in or giving up.

Each source has one slot so several searches cannot hammer it at once. That slot
used to be grabbed non-blocking, which suited exactly one caller (an impatient
user retyping their query) and nobody else: a bulk import got an instant refusal
that looked identical to "this provider has nothing", and wrote thirteen RAYE
tracks off as missing while they sat on Monochrome the whole time.

Callers now say how long they will queue. These tests pin both halves: that
waiting actually works, and that a busy source is never again mistaken for an
empty one.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search


@pytest.fixture(autouse=True)
def clean_slots(monkeypatch):
    # Slot tests exercise admission, not the settings database. Pin the default
    # so a worker from the preceding test cannot still be resolving a live
    # setting while the fixture resets the registry beneath it.
    monkeypatch.setattr(search, "get_setting_int", lambda *_a, **_k: 1)
    with search._SOURCE_SEARCH_SLOTS_LOCK:
        search._SOURCE_SEARCH_SLOTS.clear()
    yield
    with search._SOURCE_SEARCH_SLOTS_LOCK:
        search._SOURCE_SEARCH_SLOTS.clear()


def _occupy(source, hold):
    """Park a search in *source*'s slot until the returned event is set."""
    release = threading.Event()
    started = threading.Event()

    def _slow(_query, _limit):
        started.set()
        release.wait(timeout=hold)
        return []

    worker = threading.Thread(
        target=search._run_source_search,
        args=(source, {"search_fn": _slow}, "occupying query", 5),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2), "occupying search never started"
    return release, worker


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

def test_a_patient_caller_gets_the_slot_once_it_frees_up():
    """The whole point: wait a moment and you get served, not refused."""
    release, worker = _occupy("unit", hold=5)
    threading.Timer(0.2, release.set).start()

    result = search._run_source_search(
        "unit", {"search_fn": lambda q, n: [q]}, "patient query", 5, slot_wait=3
    )

    assert result == ["patient query"]
    worker.join(timeout=2)


def test_patience_is_bounded_and_gives_up_honestly():
    """A wait that never ends is just a hang with better manners."""
    release, worker = _occupy("unit", hold=5)

    started = time.perf_counter()
    with pytest.raises(search.SourceSearchBusy) as excinfo:
        search._run_source_search(
            "unit", {"search_fn": lambda q, n: [q]}, "impatient", 5, slot_wait=0.3
        )
    waited = time.perf_counter() - started

    assert 0.3 <= waited < 2.5, f"waited {waited:.2f}s, expected to give up near 0.3s"
    assert "busy" in str(excinfo.value)
    release.set()
    worker.join(timeout=2)


def test_zero_wait_keeps_the_original_barge_in_or_leave_behaviour():
    """Existing callers that want an instant answer still get one."""
    release, worker = _occupy("unit", hold=5)

    with pytest.raises(search.SourceSearchBusy):
        search._run_source_search("unit", {"search_fn": lambda q, n: [q]}, "q", 5)

    release.set()
    worker.join(timeout=2)


def test_waiting_on_one_source_does_not_block_another():
    """Slots are per provider; a busy YouTube must not stall Monochrome."""
    release, worker = _occupy("busy_source", hold=5)

    result = search._run_source_search(
        "other_source", {"search_fn": lambda q, n: [q]}, "free", 5, slot_wait=3
    )

    assert result == ["free"]
    release.set()
    worker.join(timeout=2)


def test_configured_limit_allows_two_searches_but_not_three(monkeypatch):
    monkeypatch.setattr(search, "get_setting_int", lambda *_a, **_k: 2)
    release = threading.Event()
    both_started = threading.Event()
    started_count = 0
    started_lock = threading.Lock()

    def _slow(_query, _limit):
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 2:
                both_started.set()
        release.wait(timeout=5)
        return []

    workers = [
        threading.Thread(
            target=search._run_source_search,
            args=("unit", {"search_fn": _slow}, f"q{idx}", 5),
            daemon=True,
        )
        for idx in range(2)
    ]
    for worker in workers:
        worker.start()
    assert both_started.wait(timeout=2), "two configured searches did not start"

    with pytest.raises(search.SourceSearchBusy):
        search._run_source_search("unit", {"search_fn": _slow}, "third", 5)

    release.set()
    for worker in workers:
        worker.join(timeout=2)


# ---------------------------------------------------------------------------
# Busy is not empty
# ---------------------------------------------------------------------------

def _fake_fanout(monkeypatch, sources):
    """Point the multi-source fan-out at a made-up registry."""
    monkeypatch.setattr(search, "_enabled_sources", lambda include_soulseek=False: sources)
    monkeypatch.setattr(search.servicecheck, "refresh_sources_async", lambda *a, **k: [])
    monkeypatch.setattr(search.servicecheck, "is_source_available", lambda _n: True)
    monkeypatch.setattr(search.servicecheck, "record_search_success", lambda _n: None)
    monkeypatch.setattr(search.servicecheck, "record_search_timeout", lambda _n: None)
    monkeypatch.setattr(search, "_mb_duration_lookup", lambda _q: None)
    monkeypatch.setattr(search, "_mb_album_lookup", lambda _q: None)
    # Blacklisting and tier stamping both want the database, which has nothing
    # to do with slot behaviour and would drag a schema into a threading test.
    monkeypatch.setattr(search, "_apply_blacklist_filter", lambda results, source=None: results)
    monkeypatch.setattr(search, "_stamp_quality_tiers", lambda _batch: None)


def test_a_busy_source_is_reported_as_busy_not_as_an_error(monkeypatch):
    _fake_fanout(monkeypatch, {"unit": {"search_fn": lambda q, n: []}})
    release, worker = _occupy("unit", hold=5)

    statuses = [
        ev["status"] for ev in search._search_all_events("q", 5, slot_wait=0.2)
        if ev["type"] == "source"
    ]

    assert statuses == ["busy"], f"expected a busy event, got {statuses}"
    release.set()
    worker.join(timeout=2)


def test_search_all_tells_the_caller_which_sources_never_answered(monkeypatch):
    """An empty list plus a busy source means "we did not ask", not "no match"."""
    _fake_fanout(monkeypatch, {"unit": {"search_fn": lambda q, n: []}})
    release, worker = _occupy("unit", hold=5)

    status = {}
    results, _album = search.search_all("q", 5, slot_wait=0.2, status_out=status)

    assert results == []
    assert status["busy"] == ["unit"]
    release.set()
    worker.join(timeout=2)


def test_a_genuinely_empty_source_reports_nothing_busy(monkeypatch):
    """The other half of the distinction, which is the one that must stay honest."""
    _fake_fanout(monkeypatch, {"unit": {"search_fn": lambda q, n: []}})

    status = {}
    results, _album = search.search_all("q", 5, slot_wait=0.2, status_out=status)

    assert results == []
    assert status["busy"] == []
    assert status["timeout"] == []


def test_status_out_is_optional(monkeypatch):
    """Callers that do not care must not have to care."""
    _fake_fanout(monkeypatch, {"unit": {"search_fn": lambda q, n: []}})

    results, album = search.search_all("q", 5)

    assert results == []
    assert album is None
