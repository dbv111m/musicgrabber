"""Focused, offline tests for search-card artwork lookup shape."""

import coverart


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_album_card_falls_back_to_deezers_album_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url == coverart.ITUNES_SEARCH_URL:
            return _Response({"results": []})
        return _Response({"data": [{"cover_big": "https://images.test/haunted-house.jpg"}]})

    monkeypatch.setattr(coverart.httpx, "get", fake_get)
    coverart._URL_CACHE.clear()

    url = coverart.fetch_cover_art_url("Knife Party", "Haunted House", album=True)

    assert url == "https://images.test/haunted-house.jpg"
    assert calls[-1][0] == f"{coverart.DEEZER_SEARCH_URL}/album"
    assert 'album:"Haunted House"' in calls[-1][1]["q"]


def test_track_card_keeps_using_deezers_track_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url == coverart.ITUNES_SEARCH_URL:
            return _Response({"results": []})
        return _Response({"data": [{"album": {"cover_big": "https://images.test/track.jpg"}}]})

    monkeypatch.setattr(coverart.httpx, "get", fake_get)
    coverart._URL_CACHE.clear()

    url = coverart.fetch_cover_art_url("Knife Party", "Power Glove")

    assert url == "https://images.test/track.jpg"
    assert calls[-1][0] == coverart.DEEZER_SEARCH_URL
    assert 'track:"Power Glove"' in calls[-1][1]["q"]
