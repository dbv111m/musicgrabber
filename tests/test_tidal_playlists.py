"""
Unit tests for the Tidal playlist reader.

Tidal capped its embed player at the first 50 tracks, which turned a 204-track
playlist into a healthy-looking 50-track one with no error anywhere. Playlists
now come from api.tidal.com instead, paginated, with the embed kept on as a
degraded fallback. These tests are fed canned responses, so they stay offline;
the live check lives at the bottom behind the `slow` marker.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_or_skip(name):
    try:
        return __import__(name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"{name} dependencies unavailable: {exc.name}")


def _track(artist, title, version=None):
    return {"type": "track", "item": {"title": title, "version": version, "artist": {"name": artist}}}


def _fake_api(total, tracks, name="Modern Rock Classics"):
    """Stand in for _api_get, serving metadata then paginated item pages."""
    def _get(path, params, probe_uuid):
        if path.endswith("/items"):
            offset, limit = params["offset"], params["limit"]
            page = tracks[offset:offset + limit]
            return {"limit": limit, "offset": offset, "totalNumberOfItems": total, "items": page}
        return {"title": name, "numberOfTracks": total}
    return _get


# ---------------------------------------------------------------------------
# Pagination: the actual bug
# ---------------------------------------------------------------------------

def test_playlist_pages_past_the_old_fifty_track_ceiling(monkeypatch):
    tidal = _import_or_skip("tidal")
    tracks = [_track(f"Artist {i}", f"Title {i}") for i in range(204)]
    monkeypatch.setattr(tidal, "_api_get", _fake_api(204, tracks))

    result = tidal.fetch_tidal_playlist("425691bb-476f-44a8-bcbd-a932f4f65082")

    assert result["count"] == 204
    assert result["tracks"][0] == "Artist 0 - Title 0"
    assert result["tracks"][50] == "Artist 50 - Title 50"   # the ones the embed hid
    assert result["tracks"][-1] == "Artist 203 - Title 203"
    assert result["playlist_name"] == "Modern Rock Classics"


def test_playlist_asks_for_the_largest_page_tidal_allows(monkeypatch):
    """Tidal 400s above 100 per page, so the size is not ours to tune upwards."""
    tidal = _import_or_skip("tidal")
    constants = _import_or_skip("constants")
    seen = []
    inner = _fake_api(204, [_track(f"A{i}", f"T{i}") for i in range(204)])

    def _spy(path, params, probe_uuid):
        seen.append((path, dict(params)))
        return inner(path, params, probe_uuid)

    monkeypatch.setattr(tidal, "_api_get", _spy)
    tidal.fetch_tidal_playlist("uuid")

    item_calls = [p for path, p in seen if path.endswith("/items")]
    assert [p["offset"] for p in item_calls] == [0, 100, 200]
    assert all(p["limit"] == constants.TIDAL_API_PAGE_SIZE <= 100 for p in item_calls)
    assert all(p["countryCode"] for p in item_calls)  # required, a 400 without it


def test_playlist_stops_rather_than_looping_on_a_dishonest_total(monkeypatch):
    """If Tidal claims more items than it will hand over, stop at the empty page."""
    tidal = _import_or_skip("tidal")
    tracks = [_track("A", "T") for _ in range(10)]
    monkeypatch.setattr(tidal, "_api_get", _fake_api(9999, tracks))

    assert tidal.fetch_tidal_playlist("uuid")["count"] == 10


# ---------------------------------------------------------------------------
# Title reconstruction: watched playlists hash these strings
# ---------------------------------------------------------------------------

def test_version_is_folded_back_into_the_title(monkeypatch):
    """The embed rendered "Enter Sandman (Remastered 2021)" as one string and
    watched playlists hashed it. The API splits the two, so we glue them back."""
    tidal = _import_or_skip("tidal")
    tracks = [
        _track("Metallica", "Enter Sandman", "Remastered 2021"),
        _track("Aerosmith", "I Don't Want to Miss a Thing", 'From "Armageddon" Soundtrack'),
    ]
    monkeypatch.setattr(tidal, "_api_get", _fake_api(2, tracks))

    assert tidal.fetch_tidal_playlist("uuid")["tracks"] == [
        "Metallica - Enter Sandman (Remastered 2021)",
        'Aerosmith - I Don\'t Want to Miss a Thing (From "Armageddon" Soundtrack)',
    ]


def test_version_already_in_the_title_is_not_said_twice(monkeypatch):
    tidal = _import_or_skip("tidal")
    tracks = [_track("Alice In Chains", "Would? (2022 Remaster)", "2022 Remaster")]
    monkeypatch.setattr(tidal, "_api_get", _fake_api(1, tracks))

    assert tidal.fetch_tidal_playlist("uuid")["tracks"] == ["Alice In Chains - Would? (2022 Remaster)"]


def test_videos_and_nameless_entries_are_skipped(monkeypatch):
    tidal = _import_or_skip("tidal")
    tracks = [
        _track("Snow Patrol", "In My Arms"),
        {"type": "video", "item": {"title": "Some Video", "artist": {"name": "Someone"}}},
        {"type": "track", "item": {"title": "Orphan", "artist": None}},
    ]
    monkeypatch.setattr(tidal, "_api_get", _fake_api(3, tracks))

    assert tidal.fetch_tidal_playlist("uuid")["tracks"] == ["Snow Patrol - In My Arms"]


# ---------------------------------------------------------------------------
# Fallback and token rotation
# ---------------------------------------------------------------------------

_EMBED_HTML = """
<html><body>
  <h1 class="media-album" title="Album: Modern Rock Classics"></h1>
  <list-item product-type="track">
    <span slot="title">In My Arms</span>
    <span slot="artist"><a href="x">Snow Patrol</a></span>
  </list-item>
</body></html>
"""


def test_embed_scrape_catches_us_when_the_api_falls_over(monkeypatch):
    """50 tracks beats nothing at all."""
    tidal = _import_or_skip("tidal")

    def _dead(path, params, probe_uuid):
        raise tidal._ApiUnavailable("simulated outage")

    monkeypatch.setattr(tidal, "_api_get", _dead)
    monkeypatch.setattr(tidal, "_fetch_html", lambda url: _EMBED_HTML)

    result = tidal.fetch_tidal_playlist("uuid")
    assert result["tracks"] == ["Snow Patrol - In My Arms"]
    assert result["playlist_name"] == "Modern Rock Classics"


def test_empty_playlist_still_raises_rather_than_pretending(monkeypatch):
    tidal = _import_or_skip("tidal")
    fastapi = _import_or_skip("fastapi")
    monkeypatch.setattr(tidal, "_api_get", _fake_api(0, []))

    with pytest.raises(fastapi.HTTPException) as exc:
        tidal.fetch_tidal_playlist("uuid")
    assert exc.value.status_code == 422


def test_a_401_triggers_one_token_rescrape_and_a_retry(monkeypatch):
    """Our token is lifted from the embed's JS bundle; if Tidal rotates theirs
    we go and read the new one rather than declaring Tidal broken."""
    tidal = _import_or_skip("tidal")
    urllib_error = __import__("urllib.error", fromlist=["error"])
    monkeypatch.setattr(tidal, "_api_token", "staleToken")
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.headers.get("X-tidal-token"))
        if req.headers.get("X-tidal-token") == "staleToken":
            raise urllib_error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        class _Resp:
            def read(self_inner):
                return json.dumps({"title": "OK", "numberOfTracks": 0}).encode()
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
        return _Resp()

    monkeypatch.setattr(tidal.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(tidal, "_scrape_api_token", lambda uuid: "freshToken")

    assert tidal._api_get("/playlists/x", {"countryCode": "GB"}, "x") == {"title": "OK", "numberOfTracks": 0}
    assert calls == ["staleToken", "freshToken"]
    assert tidal._api_token == "freshToken"


def test_a_401_we_cannot_fix_gives_up_to_the_fallback(monkeypatch):
    tidal = _import_or_skip("tidal")
    urllib_error = __import__("urllib.error", fromlist=["error"])
    monkeypatch.setattr(tidal, "_api_token", "staleToken")

    def _always_401(req, timeout=None):
        raise urllib_error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(tidal.urllib.request, "urlopen", _always_401)
    monkeypatch.setattr(tidal, "_scrape_api_token", lambda uuid: None)

    with pytest.raises(tidal._ApiUnavailable):
        tidal._api_get("/playlists/x", {"countryCode": "GB"}, "x")


def test_token_scrape_reads_the_bundle_the_embed_points_at(monkeypatch):
    tidal = _import_or_skip("tidal")
    pages = {
        "https://embed.tidal.com/playlists/uuid":
            '<script src="/embed-resources/js/app-2KMARJVB.js" defer></script>',
        "https://embed.tidal.com/embed-resources/js/app-2KMARJVB.js":
            'n.append("X-Tidal-Token","TPIsV0A9lyiqKl9u");let c=await fetch(t)',
    }
    monkeypatch.setattr(tidal, "_fetch_html", lambda url: pages[url])

    assert tidal._scrape_api_token("uuid") == "TPIsV0A9lyiqKl9u"


def test_token_scrape_shrugs_rather_than_throwing(monkeypatch):
    tidal = _import_or_skip("tidal")
    monkeypatch.setattr(tidal, "_fetch_html", lambda url: "<html>no bundle here</html>")
    assert tidal._scrape_api_token("uuid") is None


# ---------------------------------------------------------------------------
# Live check
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_live_tidal_playlist_returns_more_than_fifty():
    """The bug report's playlist: 204 tracks, of which the embed showed 50."""
    tidal = _import_or_skip("tidal")
    result = tidal.fetch_tidal_playlist("425691bb-476f-44a8-bcbd-a932f4f65082")
    assert result["count"] > 50, "Tidal playlist truncated again"
    assert all(" - " in t for t in result["tracks"])
