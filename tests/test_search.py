"""
Search endpoint tests. All tests here hit real external services so they're
marked 'slow'. Run the fast suite with: pytest -m "not slow"
"""

import pytest

from live_media import search_song


RESULT_KEYS = ["video_id", "title", "relevance_score", "source"]


@pytest.mark.slow
def test_search_returns_results(api, base_url):
    r = api.post(
        f"{base_url}/api/search",
        json={"query": search_song(), "limit": 5, "source": "youtube"},
        timeout=30,
    )
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert len(results) >= 1, "YouTube search returned no results"


@pytest.mark.slow
def test_search_result_shape(api, base_url):
    r = api.post(
        f"{base_url}/api/search",
        json={"query": search_song(), "limit": 3, "source": "youtube"},
        timeout=30,
    )
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert results, "no results returned"
    for item in results:
        for key in RESULT_KEYS:
            assert key in item, f"search result missing key: {key}"


@pytest.mark.slow
def test_search_all_sources(api, base_url):
    """Multi-source search should return results from at least one source."""
    r = api.post(
        f"{base_url}/api/search",
        json={"query": search_song(), "limit": 10, "source": "all"},
        timeout=45,
    )
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert len(results) >= 1


@pytest.mark.slow
def test_search_scores_are_numeric(api, base_url):
    r = api.post(
        f"{base_url}/api/search",
        json={"query": search_song(), "limit": 5, "source": "youtube"},
        timeout=30,
    )
    results = r.json().get("results", [])
    for item in results:
        assert isinstance(item["relevance_score"], (int, float)), (
            f"relevance_score is not numeric: {item['relevance_score']}"
        )


def test_search_empty_query_rejected(api, base_url):
    r = api.post(
        f"{base_url}/api/search",
        json={"query": "", "limit": 5},
        timeout=10,
    )
    assert r.status_code == 422  # Pydantic min_length=1 validation error


def test_search_invalid_body(api, base_url):
    r = api.post(f"{base_url}/api/search", json={}, timeout=10)
    assert r.status_code == 422


def test_monochrome_preview_rejects_non_monochrome_url(api, base_url):
    r = api.get(
        f"{base_url}/api/preview/mono_test",
        params={
            "source": "monochrome",
            "url": "https://example.test/track?isrc=GBAYE9200070",
        },
        timeout=10,
    )
    assert r.status_code == 400


def test_retired_mp3phoenix_preview_is_refused(api, base_url):
    """Old results can linger in a browser tab; asking for their preview gets a
    plain refusal rather than a fetch from a source that no longer exists."""
    r = api.get(
        f"{base_url}/api/preview/px_old_result",
        params={"source": "mp3phoenix", "url": "https://mp3phoenix.net/getmp3/123"},
        timeout=10,
    )
    assert r.status_code == 400


def test_retired_mp3phoenix_download_is_refused_not_misrouted(api, base_url):
    """An unknown source used to fall through to the YouTube path and fail on a
    'truncated YouTube ID'. It should be refused at the door, and plainly."""
    r = api.post(
        f"{base_url}/api/download",
        json={"video_id": "px_old_result", "title": "Old", "source": "mp3phoenix",
              "source_url": "https://mp3phoenix.net/getmp3/123"},
        timeout=10,
    )
    assert r.status_code == 400
    assert "Unknown source" in r.json()["detail"]


@pytest.mark.slow
def test_search_response_includes_unavailable_sources(api, base_url):
    r = api.post(
        f"{base_url}/api/search",
        json={"query": "shape check", "limit": 1, "source": "all"},
        timeout=30,
    )
    assert r.status_code == 200
    d = r.json()
    assert "unavailable_sources" in d
    assert isinstance(d["unavailable_sources"], list)


# ----- Live search progress stream (NDJSON) -----

import json


def _collect_stream_events(api, base_url, query, source="all", timeout=60):
    """POST to the streaming endpoint and parse the NDJSON into a list of events."""
    with api.post(
        f"{base_url}/api/search/stream",
        json={"query": query, "limit": 10, "source": source},
        stream=True,
        timeout=timeout,
    ) as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if not line:
                continue
            events.append(json.loads(line))
        return events


@pytest.mark.slow
def test_search_stream_event_sequence(api, base_url):
    """The stream opens with 'start', emits a 'source' per source, ends with 'done'."""
    events = _collect_stream_events(api, base_url, search_song(), source="all")
    assert events, "stream returned no events"
    assert events[0]["type"] == "start"
    assert isinstance(events[0].get("sources"), list)
    assert events[-1]["type"] == "done"

    # Every announced source should report a terminal status exactly once.
    announced = set(events[0]["sources"])
    reported = {e["source"] for e in events if e["type"] == "source"}
    assert announced.issubset(reported), f"sources without a status: {announced - reported}"
    for e in events:
        if e["type"] == "source":
            # "busy" is a terminal status in its own right: the source was still
            # occupied when we ran out of patience, so we never got to ask it
            # anything. Deliberately not folded in with "error", because the two
            # mean very different things to whoever is reading the stream.
            assert e["status"] in {"done", "timeout", "skipped", "error", "busy"}


@pytest.mark.slow
def test_search_stream_done_carries_token_and_parked(api, base_url):
    events = _collect_stream_events(api, base_url, search_song(), source="all")
    done = events[-1]
    assert done["type"] == "done"
    assert "search_token" in done
    assert isinstance(done.get("unavailable_sources"), list)


@pytest.mark.slow
def test_search_stream_results_have_expected_shape(api, base_url):
    events = _collect_stream_events(api, base_url, search_song(), source="youtube")
    done_sources = [e for e in events if e["type"] == "source" and e["status"] == "done"]
    flat = [r for e in done_sources for r in e.get("results", [])]
    assert flat, "stream produced no results"
    for item in flat:
        for key in RESULT_KEYS:
            assert key in item, f"streamed result missing key: {key}"
