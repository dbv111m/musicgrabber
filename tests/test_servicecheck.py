"""Unit tests for source health cooldown and lazy re-check behaviour."""

import itertools
import os
import sys
import threading
import time as real_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import servicecheck


def setup_function():
    servicecheck._HEALTH.clear()
    servicecheck._IN_FLIGHT.clear()
    servicecheck._search_timeout_strikes.clear()


def teardown_function():
    servicecheck._HEALTH.clear()
    servicecheck._IN_FLIGHT.clear()
    servicecheck._search_timeout_strikes.clear()


def test_failed_check_parks_source_until_cooldown(monkeypatch):
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: (False, "down"))
    monkeypatch.setattr(servicecheck.time, "time", lambda: 1000)

    entry = servicecheck.check_source("unit", force=True)

    assert entry["healthy"] is False
    assert entry["reason"] == "down"
    assert entry["disabled_until"] == 1600
    assert servicecheck.is_source_available("unit") is False


def test_source_rechecks_after_cooldown_even_inside_interval(monkeypatch):
    outcomes = itertools.chain([(False, "down")], itertools.repeat((True, "")))
    clock = {"now": 1000}

    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 60)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: next(outcomes))
    monkeypatch.setattr(servicecheck.time, "time", lambda: clock["now"])

    first = servicecheck.check_source("unit", force=True)
    assert first["healthy"] is False
    assert servicecheck.is_source_available("unit") is False

    clock["now"] = 1061
    second = servicecheck.check_source("unit")

    assert second["healthy"] is True
    assert servicecheck.is_source_available("unit") is True


def test_health_snapshot_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: (True, ""))
    servicecheck.check_source("unit", force=True)

    snap = servicecheck.health_snapshot()

    assert any(item["id"] == "unit" and item["available"] is True for item in snap)


def test_unhealthy_source_stays_parked_after_cooldown_until_recheck(monkeypatch):
    """Cooldown expiry schedules a re-probe; it is not a pardon."""
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 60)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: (False, "down"))
    clock = {"now": 1000}
    monkeypatch.setattr(servicecheck.time, "time", lambda: clock["now"])

    servicecheck.check_source("unit", force=True)
    assert servicecheck.is_source_available("unit") is False

    clock["now"] = 2000  # well past the cooldown, but no probe has cleared it yet
    assert servicecheck.is_source_available("unit") is False


def test_search_timeout_strikes_park_source_after_three(monkeypatch):
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: (True, ""))

    servicecheck.record_search_timeout("unit")
    servicecheck.record_search_timeout("unit")
    assert servicecheck.is_source_available("unit") is True

    servicecheck.record_search_timeout("unit")
    assert servicecheck.is_source_available("unit") is False


def test_search_success_resets_timeout_strikes(monkeypatch):
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", lambda: (True, ""))

    servicecheck.record_search_timeout("unit")
    servicecheck.record_search_timeout("unit")
    servicecheck.record_search_success("unit")
    servicecheck.record_search_timeout("unit")
    servicecheck.record_search_timeout("unit")

    # Strikes must be consecutive; the success wiped the first two
    assert servicecheck.is_source_available("unit") is True


def test_refresh_sources_async_returns_immediately(monkeypatch):
    release = threading.Event()
    done = threading.Event()

    def slow_check():
        release.wait(timeout=5)
        done.set()
        return True, ""

    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", slow_check)

    started = real_time.time()
    kicked = servicecheck.refresh_sources_async({"unit"})
    elapsed = real_time.time() - started

    assert kicked == ["unit"]
    assert elapsed < 1.0  # the whole point: the caller never waits on a probe
    release.set()
    assert done.wait(timeout=5)


def test_in_flight_guard_prevents_duplicate_probes(monkeypatch):
    release = threading.Event()
    calls = []

    def slow_check():
        calls.append(1)
        release.wait(timeout=5)
        return True, ""

    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_check_interval", lambda: 600)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "unit", slow_check)

    t = threading.Thread(target=lambda: servicecheck.check_source("unit"))
    t.start()
    # Wait for the first probe to register as in-flight
    for _ in range(200):
        with servicecheck._LOCK:
            if "unit" in servicecheck._IN_FLIGHT:
                break
        real_time.sleep(0.01)

    servicecheck.check_source("unit")  # must NOT start a second probe

    release.set()
    t.join(timeout=5)
    assert len(calls) == 1


def test_persisted_health_is_restored(monkeypatch):
    saved = {
        "youtube": {
            "healthy": False,
            "checked_at": 1000.0,
            "reason": "down",
            "disabled_until": 1600.0,
        }
    }
    monkeypatch.setattr(servicecheck, "_read_persisted_health", lambda: saved)

    restored = servicecheck.load_persisted_health()

    assert restored == saved
    assert servicecheck._HEALTH["youtube"]["reason"] == "down"


def test_builtin_health_check_is_persisted(monkeypatch):
    written = []
    monkeypatch.setattr(servicecheck, "_checks_enabled", lambda: True)
    monkeypatch.setattr(servicecheck, "_cooldown", lambda: 600)
    monkeypatch.setitem(servicecheck._CHECKS, "youtube", lambda: (False, "down"))
    monkeypatch.setattr(
        servicecheck,
        "_persist_health_entry",
        lambda source_id, entry: written.append((source_id, entry)),
    )

    servicecheck.check_source("youtube", force=True)

    assert written[0][0] == "youtube"
    assert written[0][1]["healthy"] is False
