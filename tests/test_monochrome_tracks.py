"""Tests for Monochrome's tracks API leg: discovery, ISRC matching, health."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import monochrome_tracks as tracks


class FakeResponse:
    def __init__(self, url, text="", json_body=None, status=200):
        self.url = httpx.URL(url)
        self.text = text
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("GET", str(self.url)), response=None
            )

    def json(self):
        return self._json


# A trimmed copy of the real thing, minified names and all.
BUNDLE = (
    'const Jb="https://tracks.example.st";function Ua(t){return t}'
    'class wR{constructor(e=Jb){this.baseUrl=e.replace(/\\/+$/,"")}}'
)
PAGE = '<html><script type="module" crossorigin src="./assets/index-AbC123.js"></script></html>'
RETIRED = ("<head><meta http-equiv='refresh' content='0; url=https://new.example''>"
           "</head><h1>meow :3</h1>")


@pytest.fixture(autouse=True)
def fresh_discovery(monkeypatch):
    monkeypatch.setattr(tracks, "_discovered", None)
    monkeypatch.setattr(tracks, "MONOCHROME_TRACKS_API_URL", "")
    monkeypatch.setattr(tracks, "MONOCHROME_WEB_URL", "https://old.example")
    monkeypatch.setattr(tracks.time, "sleep", lambda seconds: None)


def _serve(monkeypatch, routes):
    seen = []

    def fake_get(url, **kwargs):
        seen.append(str(url))
        for prefix, response in routes.items():
            if str(url).startswith(prefix):
                return response(url, kwargs) if callable(response) else response
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(tracks.httpx, "get", fake_get)
    return seen


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovery_follows_a_retired_domain_to_the_new_one(monkeypatch):
    """monochrome.tf answers 503 with a meta refresh; that must not stop us."""
    _serve(monkeypatch, {
        "https://old.example": FakeResponse("https://old.example", RETIRED, status=503),
        "https://new.example/assets/": FakeResponse("https://new.example/assets/index-AbC123.js", BUNDLE),
        "https://new.example": FakeResponse("https://new.example", PAGE),
    })
    assert tracks.api_base() == "https://tracks.example.st"


def test_discovery_failure_falls_back_to_last_known_address(monkeypatch):
    _serve(monkeypatch, {"https://old.example": FakeResponse("https://old.example", "", status=500)})
    assert tracks.api_base() == tracks.MONOCHROME_TRACKS_API_FALLBACK


def test_bundle_without_the_client_falls_back(monkeypatch):
    _serve(monkeypatch, {
        "https://old.example/assets/": FakeResponse("https://old.example/assets/x.js", "nothing to see"),
        "https://old.example": FakeResponse("https://old.example", PAGE),
    })
    assert tracks.api_base() == tracks.MONOCHROME_TRACKS_API_FALLBACK


def test_discovery_is_remembered_between_calls(monkeypatch):
    seen = _serve(monkeypatch, {
        "https://old.example/assets/": FakeResponse("https://old.example/assets/x.js", BUNDLE),
        "https://old.example": FakeResponse("https://old.example", PAGE),
    })
    tracks.api_base()
    tracks.api_base()
    assert len(seen) == 2  # one page, one bundle, and then nothing


def test_explicit_address_skips_discovery(monkeypatch):
    monkeypatch.setattr(tracks, "MONOCHROME_TRACKS_API_URL", "https://pinned.example/")
    _serve(monkeypatch, {})
    assert tracks.api_base() == "https://pinned.example"


# ---------------------------------------------------------------------------
# Resolving a stream
# ---------------------------------------------------------------------------

def _catalogue(monkeypatch, by_query):
    monkeypatch.setattr(tracks, "api_base", lambda: "https://tracks.test")
    queries = []

    def search(url, kwargs):
        query = kwargs["params"]["q"]
        queries.append(query)
        return FakeResponse(url, json_body={"tracks": by_query.get(query, [])})

    _serve(monkeypatch, {"https://tracks.test/search/tracks": search})
    return queries


def test_only_an_exact_isrc_match_counts(monkeypatch):
    _catalogue(monkeypatch, {"Radiohead Creep": [
        {"trackId": "111", "isrc": "GBAYE9300465", "title": "Creep - Acoustic"},
        {"trackId": "222", "isrc": "gbaye9200070", "title": "Creep"},
    ]})
    assert tracks.resolve_stream_url("GBAYE9200070", "Radiohead", "Creep") == "https://tracks.test/track/222"


def test_unplayable_tracks_are_skipped(monkeypatch):
    _catalogue(monkeypatch, {"Radiohead Creep": [
        {"trackId": "111", "isrc": "GBAYE9200070", "playable": False},
        {"trackId": "222", "isrc": "GBAYE9200070", "playable": True},
    ]})
    assert tracks.resolve_stream_url("GBAYE9200070", "Radiohead", "Creep") == "https://tracks.test/track/222"


def test_title_alone_is_tried_when_artist_and_title_miss(monkeypatch):
    queries = _catalogue(monkeypatch, {"Get Lucky": [{"trackId": "333", "isrc": "USQX91300108"}]})
    url = tracks.resolve_stream_url("USQX91300108", "Daft Punk & Pharrell Williams", "Get Lucky")
    assert url == "https://tracks.test/track/333"
    assert queries == ["Daft Punk Pharrell Williams Get Lucky", "Get Lucky"]


def test_a_miss_is_none_not_an_error(monkeypatch):
    _catalogue(monkeypatch, {})
    assert tracks.resolve_stream_url("GBZZZ9900001", "Nobody", "Nothing") is None


def test_no_title_means_no_search(monkeypatch):
    _serve(monkeypatch, {})
    assert tracks.resolve_stream_url("GBAYE9200070", "Radiohead", "") is None


def test_an_outage_raises_so_the_ladder_can_say_so(monkeypatch):
    monkeypatch.setattr(tracks, "api_base", lambda: "https://tracks.test")
    seen = _serve(monkeypatch, {"https://tracks.test": FakeResponse("https://tracks.test", status=502)})
    with pytest.raises(httpx.HTTPStatusError):
        tracks.resolve_stream_url("GBAYE9200070", "Radiohead", "Creep")
    assert len(seen) == 3  # the first try and two retries, then it gives up


def test_a_lone_gateway_blip_is_retried(monkeypatch):
    """Seen live: one 502 between clean answers. It must not fail a download."""
    monkeypatch.setattr(tracks, "api_base", lambda: "https://tracks.test")
    replies = iter([
        FakeResponse("https://tracks.test", status=502),
        FakeResponse("https://tracks.test", json_body={"tracks": [{"trackId": "7", "isrc": "GBAYE9200070"}]}),
    ])
    _serve(monkeypatch, {"https://tracks.test": lambda url, kwargs: next(replies)})
    assert tracks.resolve_stream_url("GBAYE9200070", "Radiohead", "Creep") == "https://tracks.test/track/7"


def test_a_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(tracks, "api_base", lambda: "https://tracks.test")
    seen = _serve(monkeypatch, {"https://tracks.test": FakeResponse("https://tracks.test", status=400)})
    with pytest.raises(httpx.HTTPStatusError):
        tracks.search_tracks("anything")
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_is_true_when_search_finds_something(monkeypatch):
    _catalogue(monkeypatch, {"Radiohead Creep": [{"trackId": "1", "isrc": "GBAYE9200070"}]})
    ok, reason = tracks.health()
    assert ok is True
    assert "https://tracks.test" in reason


def test_health_reports_an_outage(monkeypatch):
    monkeypatch.setattr(tracks, "api_base", lambda: "https://tracks.test")
    _serve(monkeypatch, {"https://tracks.test": FakeResponse("https://tracks.test", status=503)})
    ok, reason = tracks.health()
    assert ok is False
    assert "unreachable" in reason
