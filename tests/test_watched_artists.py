"""Watched artists CRUD and MusicBrainz search tests."""

import pytest

# Radiohead's MB artist MBID - stable, well-known
_RADIOHEAD_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
_RADIOHEAD_NAME = "Radiohead"


def test_list_watched_artists(api, base_url):
    r = api.get(f"{base_url}/api/watched-artists", timeout=10)
    assert r.status_code == 200
    assert "artists" in r.json()
    assert isinstance(r.json()["artists"], list)


@pytest.mark.slow
def test_mb_artist_search(api, base_url):
    r = api.get(
        f"{base_url}/api/watched-artists/search",
        params={"q": _RADIOHEAD_NAME},
        timeout=20,
    )
    assert r.status_code == 200
    d = r.json()
    assert "results" in d
    assert len(d["results"]) >= 1
    first = d["results"][0]
    for key in ("mbid", "name"):
        assert key in first, f"artist search result missing key: {key}"


@pytest.mark.slow
def test_watched_artist_crud(api, base_url):
    """Add Radiohead, verify they appear, then delete them."""
    r = api.post(
        f"{base_url}/api/watched-artists",
        json={
            "mbid": _RADIOHEAD_MBID,
            "name": _RADIOHEAD_NAME,
            "from_date": "2099-01-01",  # future date so the seed refresh doesn't queue real downloads
        },
        timeout=60,  # first refresh hits MB to seed the back-catalogue
    )
    assert r.status_code == 200
    artist_id = r.json().get("id")
    assert artist_id, f"no id in response: {r.json()}"

    try:
        all_artists = api.get(f"{base_url}/api/watched-artists", timeout=10).json()["artists"]
        ids = [a["id"] for a in all_artists]
        assert artist_id in ids

        tracks_r = api.get(f"{base_url}/api/watched-artists/{artist_id}/tracks", timeout=15)
        assert tracks_r.status_code == 200

    finally:
        api.delete(f"{base_url}/api/watched-artists/{artist_id}", timeout=10)

    after = api.get(f"{base_url}/api/watched-artists", timeout=10).json()["artists"]
    assert artist_id not in [a["id"] for a in after]
