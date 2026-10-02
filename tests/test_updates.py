"""Tests for the update check: tag filtering, version comparison, caching, opt-out."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import updates


class FakeResponse:
    def __init__(self, tags, status=200):
        self._tags = tags
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("GET", "https://gitlab.com"), response=None
            )

    def json(self):
        return [{"name": name} for name in self._tags]


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setitem(updates._cache, "latest", None)
    monkeypatch.setitem(updates._cache, "expires_at", 0.0)
    monkeypatch.setattr(updates, "get_setting_bool", lambda key, default=False: True)
    monkeypatch.setattr(updates, "VERSION", "4.2.4")


def _serve(monkeypatch, tags, status=200):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(tags, status)

    monkeypatch.setattr(updates.httpx, "get", fake_get)
    return calls


def test_newer_release_is_reported(monkeypatch):
    _serve(monkeypatch, ["v4.3.0", "v4.2.4"])
    status = updates.get_update_status()
    assert status["latest"] == "4.3.0"
    assert status["update_available"] is True


def test_archive_tags_are_not_releases(monkeypatch):
    _serve(monkeypatch, ["archive/qbdlx", "v4.2.4", "archive/zzz-v9.9.9"])
    status = updates.get_update_status()
    assert status["latest"] == "4.2.4"
    assert status["update_available"] is False


def test_versions_compare_numerically_not_alphabetically(monkeypatch):
    monkeypatch.setattr(updates, "VERSION", "4.9.0")
    _serve(monkeypatch, ["v4.9.0", "v4.10.0"])
    assert updates.get_update_status()["update_available"] is True


def test_dev_build_ahead_of_release_is_not_behind(monkeypatch):
    monkeypatch.setattr(updates, "VERSION", "4.3.0")
    _serve(monkeypatch, ["v4.2.4"])
    assert updates.get_update_status()["update_available"] is False


def test_answer_is_cached(monkeypatch):
    calls = _serve(monkeypatch, ["v4.3.0"])
    updates.get_update_status()
    updates.get_update_status()
    assert len(calls) == 1


def test_gitlab_failure_is_quiet_and_cached(monkeypatch):
    calls = _serve(monkeypatch, [], status=503)
    status = updates.get_update_status()
    updates.get_update_status()
    assert status["latest"] is None
    assert status["update_available"] is False
    assert len(calls) == 1


def test_disabled_never_asks_gitlab(monkeypatch):
    monkeypatch.setattr(updates, "get_setting_bool", lambda key, default=False: False)
    calls = _serve(monkeypatch, ["v9.0.0"])
    status = updates.get_update_status()
    assert status["enabled"] is False
    assert status["update_available"] is False
    assert calls == []
