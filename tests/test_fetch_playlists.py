"""
Platform-specific playlist fetch tests.
All are @pytest.mark.slow - they hit real external services.

Current state (2026-09-22):
  - Spotify embed: BROKEN (returns 1 track) - tests correctly catch this
  - Amazon: working (showHome JSON with rendered-page fallback)
  - Apple Music: working
  - YouTube: working (see test_playlists.py)
  - ListenBrainz: working
"""

import pytest


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

# A well-known ~50 track Spotify editorial playlist - exercises the embed path
_SPOTIFY_SHORT = "https://open.spotify.com/playlist/37i9dQZEVXbMwmF30ppw50"  # Top Songs UK, 50 tracks

# A 100+ track playlist - exercises the Playwright fallback when embed is truncated
_SPOTIFY_LONG = "https://open.spotify.com/playlist/37i9dQZF1DWWQRwui0ExPn"  # Songs to Sing in the Car, ~100 tracks


@pytest.mark.slow
def test_fetch_spotify_short(api, base_url):
    """Embed scraping path (<95 tracks). Expects at least 10 tracks."""
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _SPOTIFY_SHORT},
        timeout=30,
    )
    assert r.status_code == 200, f"fetch failed: {r.text}"
    d = r.json()
    assert d.get("platform") == "spotify"
    tracks = d.get("tracks", [])
    assert len(tracks) >= 10, (
        f"Spotify embed returned only {len(tracks)} track(s) - embed scraping may be broken"
    )


@pytest.mark.slow
def test_fetch_spotify_long_playwright(api, base_url):
    """Playlist with 100+ tracks - must surpass the 100-track embed cap.

    The embed's trackList tops out at exactly 100. If the Playwright fallback is
    broken (or times out and we silently fall back to the embed), we get exactly 100
    and no more. Asserting > 100 catches that regression; >= 50 did not.
    """
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _SPOTIFY_LONG},
        timeout=300,  # Playwright scrolling a large list is slow
    )
    assert r.status_code == 200, f"fetch failed: {r.text}"
    d = r.json()
    assert d.get("platform") == "spotify"
    tracks = d.get("tracks", [])
    assert len(tracks) > 100, (
        f"Spotify returned only {len(tracks)} tracks - the Playwright fallback isn't "
        "beating the 100-track embed cap (timeout too tight, or scroll broken)"
    )


@pytest.mark.slow
def test_fetch_spotify_track_format(api, base_url):
    """Tracks should be 'Artist - Title' strings or dicts with title/artist keys."""
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _SPOTIFY_SHORT},
        timeout=30,
    )
    assert r.status_code == 200
    tracks = r.json().get("tracks", [])
    assert tracks, "no tracks returned"
    first = tracks[0]
    # Tracks are strings "Artist - Title" or dicts
    assert isinstance(first, (str, dict)), f"unexpected track type: {type(first)}"


# ---------------------------------------------------------------------------
# Apple Music
# ---------------------------------------------------------------------------

# Apple Music Top 100: Global - plain HTTP, fast, reliably returns 100 tracks
_APPLE_MUSIC_URL = "https://music.apple.com/gb/playlist/uk-top-100/pl.d25f5d1181894928af76c85c967f8f31"


@pytest.mark.slow
def test_fetch_apple_music(api, base_url):
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _APPLE_MUSIC_URL},
        timeout=30,
    )
    assert r.status_code == 200, f"fetch failed: {r.text}"
    d = r.json()
    assert d.get("platform") == "apple"
    tracks = d.get("tracks", [])
    assert len(tracks) >= 50, (
        f"Apple Music returned only {len(tracks)} tracks"
    )


@pytest.mark.slow
def test_fetch_apple_music_count_field(api, base_url):
    d = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _APPLE_MUSIC_URL},
        timeout=30,
    ).json()
    assert "count" in d
    assert d["count"] == len(d.get("tracks", []))


# ---------------------------------------------------------------------------
# Amazon Music
# ---------------------------------------------------------------------------

# Amazon UK editorial playlist - Playwright-based, slow.
# NB: Amazon retires curated playlists over time (the old B08JJLT4L8 now
# redirects to the homepage with "this playlist is no longer available").
# If this test starts failing with 0 tracks, check the playlist still exists
# before suspecting the scraper, and swap in a fresh editorial ASIN.
_AMAZON_URL = "https://music.amazon.co.uk/playlists/B09ZHBTLFF"  # HITS by Topsify
_AMAZON_LONG_URL = "https://music.amazon.com/user-playlists/c5259a244e914ae18ffa0c1be3837d32b1r0"


@pytest.mark.slow
def test_fetch_amazon_playlist(api, base_url):
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _AMAZON_URL},
        timeout=90,  # Playwright + Amazon = slow
    )
    assert r.status_code == 200, f"fetch failed ({r.status_code}): {r.text}"
    d = r.json()
    assert d.get("platform") == "amazon"
    tracks = d.get("tracks", [])
    assert len(tracks) >= 10, (
        f"Amazon playlist returned only {len(tracks)} track(s) - page structure may have changed"
    )


@pytest.mark.slow
def test_fetch_amazon_playlist_over_one_hundred_tracks(api, base_url):
    """The JSON path must not fall back to a truncated or evicted response."""
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _AMAZON_LONG_URL},
        timeout=90,
    )
    assert r.status_code == 200, f"fetch failed ({r.status_code}): {r.text}"
    d = r.json()
    assert d.get("platform") == "amazon"
    assert len(d.get("tracks", [])) > 100


# ---------------------------------------------------------------------------
# ListenBrainz
# ---------------------------------------------------------------------------

# A stable community playlist UUID from the LB docs / well-known editors
# "Weekly Jams" for user "rob" - a LB staff member's playlist is likely to persist
_LB_PLAYLIST_UUID = "0709f0b3-f731-4d09-8e6d-09ba4a84ea31"
_LB_PLAYLIST_URL = f"https://listenbrainz.org/playlist/{_LB_PLAYLIST_UUID}/"


@pytest.mark.slow
def test_fetch_listenbrainz_playlist(api, base_url):
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _LB_PLAYLIST_URL},
        timeout=20,
    )
    # LB playlists rotate; a missing one is acceptable (deleted/rotated upstream),
    # whether the server reports it as 400 "rotated" or 404 "not found".
    if r.status_code in (400, 404) and ("rotated" in r.text.lower() or "not found" in r.text.lower()):
        pytest.skip("ListenBrainz playlist has been rotated - update UUID in test")
    assert r.status_code == 200, f"fetch failed: {r.text}"
    d = r.json()
    assert d.get("platform") == "listenbrainz"
    assert len(d.get("tracks", [])) >= 1


# ---------------------------------------------------------------------------
# Bad URL validation
# ---------------------------------------------------------------------------

def test_fetch_playlist_missing_url(api, base_url):
    r = api.post(f"{base_url}/api/fetch-playlist", json={}, timeout=10)
    assert r.status_code == 422


def test_fetch_playlist_unsupported_url(api, base_url):
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": "https://example.com/not-a-playlist"},
        timeout=10,
    )
    assert r.status_code in (400, 422), (
        f"unsupported URL should be rejected, got {r.status_code}"
    )
