"""
Watched playlist CRUD tests and playlist-fetch tests.

fetch-playlist tests hit real external services - marked slow.
CRUD tests only touch the DB, so they're fast.
"""

import pytest


# A stable, YouTube-curated playlist for testing (the old "YouTube Rewind 2012"
# fixture was deleted upstream, hence the periodic 502).
_YT_PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"  # "Popular Music Videos"


WATCHED_PL_KEYS = [
    "id", "url", "name", "platform", "refresh_interval_hours",
    "last_checked", "last_track_count",
    # Membership accounting: what the playlist holds now, and what has left it.
    "tracked_count", "downloaded_count", "departed_count",
]


def test_list_watched_playlists(api, base_url):
    r = api.get(f"{base_url}/api/watched-playlists", timeout=10)
    assert r.status_code == 200
    assert "playlists" in r.json()
    assert isinstance(r.json()["playlists"], list)


def test_list_watched_playlists_shape(api, base_url):
    r = api.get(f"{base_url}/api/watched-playlists", timeout=10)
    playlists = r.json()["playlists"]
    for pl in playlists:
        for key in WATCHED_PL_KEYS:
            assert key in pl, f"watched playlist missing key: {key}"


def test_downloaded_never_exceeds_the_current_track_count(api, base_url):
    """The shape of Tristan's bug: "158 downloaded" against a 150-track playlist."""
    playlists = api.get(f"{base_url}/api/watched-playlists", timeout=10).json()["playlists"]
    for pl in playlists:
        assert pl["downloaded_count"] <= pl["tracked_count"], (
            f"{pl['name']}: {pl['downloaded_count']} downloaded of {pl['tracked_count']} tracked"
        )


def _forget_playlist(api, base_url, url):
    """Delete any existing watch for this URL.

    A run that dies between create and delete leaves the row behind, and then
    every later run gets a 409 forever more. One sulky run should not poison the
    suite until somebody goes in with a torch and a SQLite prompt.
    """
    r = api.get(f"{base_url}/api/watched-playlists", timeout=10)
    if r.status_code != 200:
        return
    for pl in r.json().get("playlists", []):
        if pl.get("url") == url:
            api.delete(f"{base_url}/api/watched-playlists/{pl['id']}", timeout=10)


def test_watched_playlist_crud(api, base_url):
    """Add, retrieve, update, and delete a watched playlist - full lifecycle."""
    url = _YT_PLAYLIST_URL

    # Clear out any leftovers from a previous run before we start.
    _forget_playlist(api, base_url, url)

    # Create - server fetches the playlist immediately; 502 means the platform was
    # unreachable at test time, not a server bug. Skip rather than fail.
    r = api.post(
        f"{base_url}/api/watched-playlists",
        json={"url": url, "refresh_interval_hours": 48, "make_m3u": False},
        timeout=20,
    )
    if r.status_code in (502, 503, 504):
        pytest.skip(f"platform fetch failed ({r.status_code}) - external service unavailable")

    assert r.status_code == 200, f"unexpected status {r.status_code}: {r.text}"
    pl_id = r.json().get("id")
    assert pl_id, f"no id in create response: {r.json()}"

    try:
        # Read back
        r2 = api.get(f"{base_url}/api/watched-playlists/{pl_id}", timeout=10)
        assert r2.status_code == 200
        d = r2.json()
        assert d.get("url") == url or d.get("playlist", {}).get("url") == url

        # Update refresh interval
        r3 = api.put(
            f"{base_url}/api/watched-playlists/{pl_id}",
            json={"refresh_interval_hours": 72},
            timeout=10,
        )
        assert r3.status_code == 200

        # Check it in the list
        all_pl = api.get(f"{base_url}/api/watched-playlists", timeout=10).json()["playlists"]
        ids = [p["id"] for p in all_pl]
        assert pl_id in ids, "added playlist not in list"

    finally:
        # Always clean up
        api.delete(f"{base_url}/api/watched-playlists/{pl_id}", timeout=10)

    # Verify it's gone
    all_pl_after = api.get(f"{base_url}/api/watched-playlists", timeout=10).json()["playlists"]
    assert pl_id not in [p["id"] for p in all_pl_after]


def test_watched_playlist_schedule(api, base_url):
    r = api.get(f"{base_url}/api/watched-playlists/schedule", timeout=10)
    assert r.status_code == 200


def test_watched_playlist_get_missing_returns_404(api, base_url):
    r = api.get(f"{base_url}/api/watched-playlists/doesnotexist", timeout=10)
    assert r.status_code == 404


@pytest.mark.slow
def test_fetch_youtube_playlist(api, base_url):
    """Fetch a real YouTube playlist and verify track list shape."""
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": _YT_PLAYLIST_URL},
        timeout=45,
    )
    assert r.status_code == 200
    d = r.json()
    assert "tracks" in d, f"no 'tracks' in response: {list(d.keys())}"
    tracks = d["tracks"]
    assert len(tracks) >= 1, "playlist fetch returned no tracks"
    # fetch-playlist returns each track as an "Artist - Title" string.
    for t in tracks:
        assert isinstance(t, str) and t.strip(), f"track is not a non-empty string: {t!r}"
    assert any(" - " in t for t in tracks), "no track looked like 'Artist - Title'"


@pytest.mark.slow
def test_fetch_playlist_bad_url(api, base_url):
    r = api.post(
        f"{base_url}/api/fetch-playlist",
        json={"url": "https://www.youtube.com/playlist?list=DOESNOTEXIST123"},
        timeout=30,
    )
    # Should be 400 or 422, definitely not 500
    assert r.status_code in (400, 422, 404), (
        f"bad playlist URL returned unexpected status {r.status_code}"
    )
