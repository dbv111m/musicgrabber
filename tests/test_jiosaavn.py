"""Tests for the JioSaavn source: link decryption, result shaping, and the CDN host check."""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

import jiosaavn

# A real encrypted_media_url as JioSaavn served it on 2026-10-02 (Kesariya).
REAL_ENCRYPTED = "ID2ieOjCrwfgWvL5sXl4B1ImC5QfbsDyryhkSYK5IH2E7FCO52VR6yhNbcEbes5iCcja4+W8xhE0SwtCJToN4Bw7tS9a8Gtq"
REAL_DECRYPTED = "https://aac.saavncdn.com/871/c2febd353f3a076a406fa37510f31f9f_96.mp4"


def _encrypt(url: str) -> str:
    cipher = DES.new(jiosaavn._DES_KEY, DES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(url.encode(), DES.block_size))).decode()


def _song(title="Kesariya", artists=("Pritam", "Arijit Singh"), duration="268",
          hi_res="true", encrypted=REAL_ENCRYPTED, song_id="abc123"):
    return {
        "id": song_id,
        "title": title,
        "subtitle": f"{', '.join(artists)} - Brahmastra",
        "image": "https://c.saavncdn.com/871/Brahmastra-150x150.jpg",
        "more_info": {
            "duration": duration,
            "320kbps": hi_res,
            "encrypted_media_url": encrypted,
            "artistMap": {"primary_artists": [{"name": a} for a in artists]},
        },
    }


def test_real_link_decrypts():
    assert jiosaavn._decrypt_media_url(REAL_ENCRYPTED) == REAL_DECRYPTED


def test_320_flag_picks_the_320_rendition():
    url, kbps = jiosaavn._stream_url(_song()["more_info"])
    assert kbps == 320
    assert url.endswith("_320.mp4")


def test_no_320_falls_back_to_160():
    url, kbps = jiosaavn._stream_url(_song(hi_res="false")["more_info"])
    assert kbps == 160
    assert url.endswith("_160.mp4")


def test_drm_only_track_has_no_stream():
    assert jiosaavn._stream_url(_song(encrypted="")["more_info"]) is None


def test_a_link_off_some_other_host_is_refused():
    sneaky = _encrypt("https://evil.example.com/x_96.mp4")
    assert jiosaavn._stream_url(_song(encrypted=sneaky)["more_info"]) is None


def test_titles_lose_entities_and_repeated_annotations():
    assert jiosaavn._clean_text("Tum Hi Ho (From &quot;Aashiqui 2&quot;)") == 'Tum Hi Ho (From "Aashiqui 2")'
    assert jiosaavn._clean_text("Blitzkrieg Bop (1999 Remaster) (1999 Remaster)") == "Blitzkrieg Bop (1999 Remaster)"
    # Different annotations are not duplicates and both stay.
    assert jiosaavn._clean_text("Song (Live) (Remastered)") == "Song (Live) (Remastered)"


def test_search_shapes_results(monkeypatch):
    payload = {"results": [
        _song(),
        _song(title="Kesariya (Dance Mix)", hi_res="false", song_id="def456"),
        _song(title="DRM only", encrypted="", song_id="ghi789"),
    ]}
    monkeypatch.setattr(jiosaavn, "_api", lambda call, **params: payload)

    results = jiosaavn.search_jiosaavn("kesariya arijit singh", 10)

    assert len(results) == 2  # the DRM-only track is dropped
    top = next(r for r in results if r["video_id"] == "js_abc123")
    assert top["source"] == "jiosaavn"
    assert top["channel"] == "Pritam, Arijit Singh"
    assert top["quality"] == "AAC 320kbps"
    assert top["duration"] == "4:28"
    assert top["source_url"].endswith("_320.mp4")
    assert top["thumbnail"].endswith("500x500.jpg")
    assert "label_release=+40" in top["score_breakdown"]
    dance = next(r for r in results if r["video_id"] == "js_def456")
    assert dance["quality"] == "AAC 160kbps"


def test_search_failure_returns_nothing(monkeypatch):
    def boom(call, **params):
        raise RuntimeError("JioSaavn is having a lie-in")
    monkeypatch.setattr(jiosaavn, "_api", boom)
    assert jiosaavn.search_jiosaavn("anything", 10) == []


def test_cdn_host_check():
    assert jiosaavn.is_jiosaavn_url(REAL_DECRYPTED)
    assert not jiosaavn.is_jiosaavn_url("https://aac.saavncdn.com.evil.tld/x.mp4")
    assert not jiosaavn.is_jiosaavn_url(None)


def test_declared_quality_maps_to_the_shared_tiers():
    from search import quality_tier_of_result
    from upgrades import TIER_LOSSY_320, _estimate_result_tier, _kbps_to_tier
    assert quality_tier_of_result({"quality": "AAC 320kbps"}) == TIER_LOSSY_320
    assert _estimate_result_tier({"source": "jiosaavn", "quality": "AAC 320kbps"}) == (TIER_LOSSY_320, False)
    assert _estimate_result_tier({"source": "jiosaavn", "quality": "AAC 160kbps"}) == (_kbps_to_tier(160), False)


def test_api_claims_an_indian_address(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(headers)
        return Resp()

    monkeypatch.setattr(jiosaavn.httpx, "get", fake_get)
    jiosaavn._api("search.getResults", q="anything")
    assert seen["X-Forwarded-For"] == jiosaavn._INDIAN_IP


def test_health_passes_on_the_real_track(monkeypatch):
    payload = {"results": [_song(title="Lithium", artists=("Nirvana",))]}
    monkeypatch.setattr(jiosaavn, "_api", lambda call, **params: payload)
    assert jiosaavn.check_health() == (True, "")


def test_health_fails_when_only_karaoke_comes_back(monkeypatch):
    # What the UK sees when the header stops working.
    payload = {"results": [_song(title="Lithium (Originally Perfomed By Nirvana) (Karaoke Version)",
                                 artists=("ZZang KARAOKE",))]}
    monkeypatch.setattr(jiosaavn, "_api", lambda call, **params: payload)
    ok, detail = jiosaavn.check_health()
    assert not ok
    assert "geo-filtered" in detail
