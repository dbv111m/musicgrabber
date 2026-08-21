import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# download_leg_healthy
# ---------------------------------------------------------------------------

def test_download_leg_healthy_prefers_browser_without_launching_it(monkeypatch):
    import monochrome
    monkeypatch.setattr(
        "monochrome_browser.browser_fallback_health",
        lambda: (True, "browser-authenticated playback available on demand"),
    )
    monkeypatch.setattr(
        "qbdlx.download_leg_healthy",
        lambda: (_ for _ in ()).throw(AssertionError("qbdlx probe should not run")),
    )
    ok, reason = monochrome.download_leg_healthy()
    assert ok is True
    assert "browser-authenticated" in reason


def test_download_leg_healthy_uses_qbdlx_when_browser_unavailable(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_browser.browser_fallback_health", lambda: (False, "Chrome failed twice"))
    monkeypatch.setattr("qbdlx.qbdlx_enabled", lambda: True)
    monkeypatch.setattr("qbdlx.download_leg_healthy", lambda: (True, "3/28 shared tokens usable this cycle"))
    ok, reason = monochrome.download_leg_healthy()
    assert ok is True
    assert "shared tokens" in reason


def test_download_leg_healthy_reports_both_failures(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_browser.browser_fallback_health", lambda: (False, "Chrome failed twice"))
    monkeypatch.setattr("qbdlx.qbdlx_enabled", lambda: True)
    monkeypatch.setattr("qbdlx.download_leg_healthy", lambda: (False, "0/28 shared tokens usable this cycle"))
    ok, reason = monochrome.download_leg_healthy()
    assert ok is False
    assert "Chrome failed twice" in reason
    assert "0/28 shared tokens" in reason


# ---------------------------------------------------------------------------
# Browser-first stream fallback chain
# ---------------------------------------------------------------------------

def test_preview_uses_browser_before_qbdlx(monkeypatch):
    import monochrome
    calls = []
    monkeypatch.setattr(
        "monochrome_browser.resolve_unified_stream_url",
        lambda *a, **k: calls.append(("browser", a[0], a[1])) or "https://mono.test/preview.mp4",
    )
    monkeypatch.setattr(
        "qbdlx.resolve_qobuz_stream_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("qbdlx should not run")),
    )
    assert monochrome.get_monochrome_preview_url("GBAYE9200070") == "https://mono.test/preview.mp4"
    assert calls == [("browser", "GBAYE9200070", "LOSSLESS")]


def test_preview_uses_qbdlx_when_browser_has_nothing(monkeypatch):
    import monochrome
    calls = []
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr(
        "qbdlx.resolve_qobuz_stream_url",
        lambda isrc, quality_fmt: calls.append((isrc, quality_fmt)) or "https://qobuz.test/preview.flac",
    )
    assert monochrome.get_monochrome_preview_url("GBAYE9200070") == "https://qobuz.test/preview.flac"
    assert calls == [("GBAYE9200070", 7)]


def test_preview_never_falls_back_to_lossy_qbdlx_format(monkeypatch):
    import monochrome
    calls = []
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr(
        "qbdlx.resolve_qobuz_stream_url",
        lambda isrc, quality_fmt: calls.append((isrc, quality_fmt)),
    )
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome.get_monochrome_preview_url("GBAYE9200070")
    assert calls == [("GBAYE9200070", 7)]


def test_preview_rescues_isrc_with_artist_and_title(monkeypatch):
    import monochrome
    browser_calls = []
    def fake_browser(isrc, *args, **kwargs):
        browser_calls.append(isrc)
        return "https://mono.test/rescued.mp4" if isrc == "GBNEW2500001" else None
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", fake_browser)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "GBNEW2500001")
    url = monochrome.get_monochrome_preview_url(
        "monochrome://123?isrc=GBOLD2500001&quality=LOSSLESS&src=deezer",
        artist_hint="Artist", title_hint="Track",
    )
    assert url == "https://mono.test/rescued.mp4"
    assert browser_calls == ["GBOLD2500001", "GBNEW2500001"]


def test_preview_uses_tidal_lossless_as_final_leg(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    tidal_calls = []
    monkeypatch.setattr(
        monochrome, "_tidal_stream_url",
        lambda tidal_id, quality: tidal_calls.append((tidal_id, quality)) or "https://tidal.test/lossless.flac",
    )
    url = monochrome.get_monochrome_preview_url(
        "monochrome://18420572?isrc=GBZZZ9900001&quality=HI_RES_LOSSLESS&src=tidal"
    )
    assert url == "https://tidal.test/lossless.flac"
    assert tidal_calls == [("18420572", "LOSSLESS")]


def test_download_fails_cleanly_when_browser_and_qbdlx_have_nothing(monkeypatch, tmp_path):
    import monochrome
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    output = tmp_path / "track.flac"
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome.download_monochrome_track(
            "monochrome://not-tidal?isrc=GBAYE9200070&quality=LOSSLESS", output,
        )
    assert not output.exists()


def test_browser_fallback_is_tried_before_qbdlx(monkeypatch):
    import monochrome
    monkeypatch.setattr(
        "monochrome_browser.resolve_unified_stream_url",
        lambda *a, **k: "https://monochrome.test/browser.flac",
    )
    monkeypatch.setattr(
        "qbdlx.resolve_qobuz_stream_url",
        lambda *a: (_ for _ in ()).throw(AssertionError("qbdlx must not run")),
    )
    assert monochrome._resolve_monochrome_stream_url(
        "monochrome://123?isrc=GBAYE9200070&quality=LOSSLESS",
        artist_hint="Radiohead", title_hint="Creep",
    ) == "https://monochrome.test/browser.flac"


def test_qbdlx_runs_when_browser_leg_has_nothing(monkeypatch):
    import monochrome
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: "https://qobuz.test/fast.flac")
    assert monochrome._resolve_monochrome_stream_url(
        "monochrome://123?isrc=GBAYE9200070&quality=LOSSLESS",
        artist_hint="Radiohead", title_hint="Creep",
    ) == "https://qobuz.test/fast.flac"


# ---------------------------------------------------------------------------
# Source default enabled
# ---------------------------------------------------------------------------

def test_monochrome_enabled_defaults_to_true_with_no_db_row(monkeypatch):
    """Fresh install has no DB row; monochrome_enabled() must return True."""
    import monochrome
    monkeypatch.setattr("settings.get_setting_bool",
                        lambda key, default=False, **kw: default)

    assert monochrome.monochrome_enabled() is True


def test_source_registry_has_monochrome_default_enabled_true():
    import search
    cfg = search.SOURCE_REGISTRY.get("monochrome", {})
    assert cfg.get("default_enabled") is True, (
        "Monochrome must default to enabled so fresh installs show results"
    )


# ---------------------------------------------------------------------------
# Legacy search tests (unchanged)
# ---------------------------------------------------------------------------

def test_monochrome_search_retries_with_punctuation_normalised(monkeypatch):
    import monochrome

    calls = []

    class FakeResponse:
        def __init__(self, items):
            self._items = items

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": self._items}}

    def fake_get(url, params, headers, timeout, follow_redirects=False):
        calls.append(params["s"])
        if params["s"] == "Artist1 Artist2 trackName":
            return FakeResponse([
                {
                    "id": "123",
                    "title": "trackName",
                    "artist": {"name": "Artist1"},
                    "duration": 180,
                    "isrc": "GBABC1234567",
                    "mediaMetadata": {"tags": ["LOSSLESS"]},
                    "album": {"title": "Album", "cover": ""},
                }
            ])
        return FakeResponse([])

    monkeypatch.setattr(monochrome, "_hifi_api_url", lambda: "https://api.example.test")
    monkeypatch.setattr(monochrome.httpx, "get", fake_get)

    results = monochrome.search_monochrome("Artist1, Artist2 - trackName", limit=5)

    assert calls == ["Artist1, Artist2 - trackName", "Artist1 Artist2 trackName"]
    assert results
    assert results[0]["source"] == "monochrome"


def test_monochrome_search_searches_normalised_variant_even_when_exact_query_returns_items(monkeypatch):
    import monochrome

    calls = []

    class FakeResponse:
        def __init__(self, items):
            self._items = items

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": self._items}}

    def fake_get(url, params, headers, timeout, follow_redirects=False):
        calls.append(params["s"])
        if params["s"] == "Artist Featured Track":
            return FakeResponse([
                {
                    "id": "456",
                    "title": "Track",
                    "artist": {"name": "Artist"},
                    "duration": 219,
                    "isrc": "USGF19942501",
                    "mediaMetadata": {"tags": ["HIRES_LOSSLESS"]},
                    "album": {"title": "Album", "cover": ""},
                }
            ])
        return FakeResponse([
            {
                "id": "999",
                "title": "Irrelevant Raw Hit",
                "artist": {"name": "Other"},
                "duration": 180,
                "isrc": "USGF19949999",
                "mediaMetadata": {"tags": ["LOSSLESS"]},
                "album": {"title": "Other Album", "cover": ""},
            }
        ])

    monkeypatch.setattr(monochrome, "_hifi_api_url", lambda: "https://api.example.test")
    monkeypatch.setattr(monochrome.httpx, "get", fake_get)

    results = monochrome.search_monochrome("Artist, Featured - Track", limit=5)

    assert calls == ["Artist, Featured - Track", "Artist Featured Track"]
    assert results
    assert results[0]["title"] == "Track"


def test_monochrome_search_continues_to_normalised_variant_after_exact_query_error(monkeypatch):
    import monochrome

    calls = []

    class FakeResponse:
        def __init__(self, items):
            self._items = items

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": self._items}}

    class BrokenResponse:
        def raise_for_status(self):
            raise RuntimeError("503 Service Unavailable")

    def fake_get(url, params, headers, timeout, follow_redirects=False):
        calls.append(params["s"])
        if params["s"] == "ILLENIUM Emma Grace Brave Soul":
            return FakeResponse([
                {
                    "id": "mono-1",
                    "title": "Brave Soul",
                    "artist": {"name": "ILLENIUM & Emma Grace"},
                    "duration": 217,
                    "isrc": "USAT22100001",
                    "mediaMetadata": {"tags": ["LOSSLESS"]},
                    "album": {"title": "Fallen Embers", "cover": ""},
                }
            ])
        return BrokenResponse()

    monkeypatch.setattr(monochrome, "_hifi_api_url", lambda: "https://api.example.test")
    monkeypatch.setattr(monochrome.httpx, "get", fake_get)

    results = monochrome.search_monochrome("ILLENIUM, Emma Grace - Brave Soul", limit=5)

    assert calls == ["ILLENIUM, Emma Grace - Brave Soul", "ILLENIUM Emma Grace Brave Soul"]
    assert results
    assert results[0]["source"] == "monochrome"
    assert results[0]["title"] == "Brave Soul"


def test_monochrome_search_falls_back_to_next_hifi_api_endpoint(monkeypatch):
    import monochrome

    calls = []
    monochrome._hifi_api_url_cache = None

    class FakeResponse:
        def __init__(self, items):
            self._items = items

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"items": self._items}}

    class BrokenResponse:
        def raise_for_status(self):
            raise RuntimeError("503 Service Unavailable")

    def fake_get(url, params, headers, timeout, follow_redirects=False):
        calls.append((url, params["s"]))
        if url.startswith("https://dead.example.test"):
            return BrokenResponse()
        return FakeResponse([
            {
                "id": "mono-2",
                "title": "Brave Soul",
                "artist": {"name": "ILLENIUM & Emma Grace"},
                "duration": 277,
                "isrc": "ZZOPM2106210",
                "mediaMetadata": {"tags": ["LOSSLESS"]},
                "album": {"title": "Fallen Embers", "cover": ""},
            }
        ])

    monkeypatch.setattr(
        monochrome,
        "_hifi_api_url",
        lambda: "https://dead.example.test,https://working.example.test",
    )
    monkeypatch.setattr(monochrome.httpx, "get", fake_get)

    results = monochrome.search_monochrome("ILLENIUM, Emma Grace - Brave Soul", limit=5)

    assert calls[0][0] == "https://dead.example.test/search"
    assert any(url == "https://working.example.test/search" for url, _query in calls)
    assert results
    assert results[0]["source"] == "monochrome"
    assert monochrome._hifi_api_url_cache == "https://working.example.test"


def test_hifi_search_leg_stops_at_its_wall_clock_budget(monkeypatch):
    import monochrome
    clock = {"now": 100.0}
    calls = []

    monkeypatch.setattr(monochrome, "MONOCHROME_HIFI_SEARCH_BUDGET", 5.0)
    monkeypatch.setattr(monochrome, "_hifi_api_urls", lambda: ["https://one.test", "https://two.test"])
    monkeypatch.setattr(monochrome, "_monochrome_search_queries", lambda query: [query, f"{query} alt"])
    monkeypatch.setattr(monochrome.time, "monotonic", lambda: clock["now"])

    def slow_failure(url, params, headers, timeout, follow_redirects):
        calls.append((url, timeout))
        clock["now"] += timeout
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(monochrome.httpx, "get", slow_failure)

    assert monochrome._hifi_search_leg("Artist - Track", 5) == []
    assert calls == [("https://one.test/search", 5.0)]


# ---------------------------------------------------------------------------
# qbdlx direct-Qobuz search fallback (when the hifi-api search leg is down)
# ---------------------------------------------------------------------------

def _make_qobuz_catalog_item(track_id=8767428, isrc="USQX91300809", title="Get Lucky",
                             artist="Daft Punk", album="Random Access Memories",
                             streamable=True, version=""):
    """A minimal Qobuz catalog/search track item, shaped like the real API."""
    return {
        "id": track_id,
        "isrc": isrc,
        "title": title,
        "performer": {"name": artist},
        "album": {"title": album, "image": {"large": "https://img.test/large.jpg"}},
        "duration": 247,
        "version": version,
        "streamable": streamable,
    }


def test_qbdlx_search_fallback_maps_items(monkeypatch):
    import monochrome
    monkeypatch.setattr("qbdlx.search_qobuz_catalog",
                        lambda q, limit: [_make_qobuz_catalog_item()])

    results = monochrome._qbdlx_search_fallback("daft punk get lucky", 5)

    assert len(results) == 1
    r = results[0]
    assert r["source"] == "monochrome"
    assert r["quality"] == "LOSSLESS"          # free tokens cap here, labelled honestly
    assert r["channel"] == "Daft Punk"
    assert r["title"] == "Get Lucky"
    assert r["thumbnail"] == "https://img.test/large.jpg"
    assert r["source_url"].startswith("monochrome://8767428?")
    assert "isrc=USQX91300809" in r["source_url"]
    assert "quality=LOSSLESS" in r["source_url"]
    assert any("qbdlx-direct" in b for b in r["score_breakdown"])


def test_qbdlx_search_fallback_skips_no_isrc_and_unstreamable(monkeypatch):
    import monochrome
    items = [
        _make_qobuz_catalog_item(track_id=1, isrc="", title="No ISRC"),
        _make_qobuz_catalog_item(track_id=2, streamable=False, title="Not streamable"),
        _make_qobuz_catalog_item(track_id=3, isrc="GBABC1234567", title="Good"),
    ]
    monkeypatch.setattr("qbdlx.search_qobuz_catalog", lambda q, limit: items)

    results = monochrome._qbdlx_search_fallback("x", 5)

    assert [r["title"] for r in results] == ["Good"]


def test_qbdlx_search_fallback_dedupes_by_isrc(monkeypatch):
    import monochrome
    items = [
        _make_qobuz_catalog_item(track_id=10, isrc="USQX91300809", title="Get Lucky"),
        _make_qobuz_catalog_item(track_id=11, isrc="USQX91300809", title="Get Lucky (dupe)"),
    ]
    monkeypatch.setattr("qbdlx.search_qobuz_catalog", lambda q, limit: items)

    results = monochrome._qbdlx_search_fallback("x", 5)

    assert len(results) == 1


def test_search_monochrome_falls_back_to_qbdlx_when_hifi_api_down(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_hifi_api_urls", lambda: ["https://dead.example.test"])

    def boom(*a, **k):
        raise RuntimeError("hifi-api dead")
    monkeypatch.setattr(monochrome.httpx, "get", boom)
    monkeypatch.setattr("qbdlx.search_qobuz_catalog",
                        lambda q, limit: [_make_qobuz_catalog_item()])

    results = monochrome.search_monochrome("daft punk get lucky", 5)

    assert results
    assert results[0]["title"] == "Get Lucky"
    assert any("qbdlx-direct" in b for b in results[0]["score_breakdown"])


# ---------------------------------------------------------------------------
# Deezer ISRC leg (v2.9.4): search, rescue, and the Tidal stream fallback
# ---------------------------------------------------------------------------

def _make_deezer_item(track_id=62847142, isrc="GB28K1100036", title="Titanium (feat. Sia)",
                      artist="David Guetta", album="Nothing but the Beat",
                      duration=245, rank=956365, title_version=""):
    """A minimal Deezer /search track item, shaped like the real API."""
    return {
        "id": track_id,
        "isrc": isrc,
        "title": title,
        "title_version": title_version,
        "artist": {"name": artist},
        "album": {"title": album, "cover_big": "https://img.test/cover.jpg"},
        "duration": duration,
        "rank": rank,
    }


def test_isrc_valid():
    import monochrome
    assert monochrome._isrc_valid("GB28K1100036")
    assert monochrome._isrc_valid("usum71703861")     # case-insensitive
    assert not monochrome._isrc_valid("QT&JC2622262") # Tidal's finest ampersand
    assert not monochrome._isrc_valid("QTJC2622262")  # eleven characters
    assert not monochrome._isrc_valid("")
    assert not monochrome._isrc_valid("GB28K110003X") # designation must be digits


def test_parse_tidal_track_payload_shapes():
    import base64
    import json as jsonlib
    import monochrome

    assert monochrome._parse_tidal_track_payload(
        {"data": {"OriginalTrackUrl": "http://cdn.test/a.flac"}}
    ) == "http://cdn.test/a.flac"
    assert monochrome._parse_tidal_track_payload(
        {"data": {"urls": ["http://cdn.test/b.flac"]}}
    ) == "http://cdn.test/b.flac"
    bts = base64.b64encode(jsonlib.dumps({"urls": ["http://cdn.test/c.flac"]}).encode()).decode()
    assert monochrome._parse_tidal_track_payload(
        {"data": {"manifest": bts, "manifestMimeType": "application/vnd.tidal.bts"}}
    ) == "http://cdn.test/c.flac"
    # DASH means Widevine means no; and error payloads yield nothing.
    assert monochrome._parse_tidal_track_payload(
        {"data": {"manifest": bts, "manifestMimeType": "application/dash+xml"}}
    ) == ""
    assert monochrome._parse_tidal_track_payload({"detail": "Upstream API error"}) == ""


def test_deezer_search_leg_verifies_against_qobuz(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(),
                            _make_deezer_item(track_id=2, isrc="GB28K1100170", title="Titanium (hi-res edition)"),
                            _make_deezer_item(track_id=3, isrc="GB28K1100999", title="Titanium (not on Qobuz)"),
                        ])

    def fake_lookup(isrc):
        if isrc == "GB28K1100036":
            return [{"id": 1, "isrc": isrc, "hires": False}], False
        if isrc == "GB28K1100170":
            return [{"id": 2, "isrc": isrc, "hires": True}], False
        return [], False  # clean miss: Qobuz has never heard of it
    monkeypatch.setattr(monochrome, "_qobuz_isrc_lookup", fake_lookup)

    results = monochrome._deezer_search_leg("david guetta titanium", 5)

    assert len(results) == 2  # the clean miss was dropped
    qualities = {r["quality"] for r in results}
    assert qualities == {"LOSSLESS", "HI_RES_LOSSLESS"}
    assert all("src=deezer" in r["source_url"] for r in results)
    assert all(r["source"] == "monochrome" for r in results)
    assert all(any("via=deezer-isrc" in b for b in r["score_breakdown"]) for r in results)


def test_deezer_search_leg_keeps_unverified_when_direct_lookup_is_down(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [_make_deezer_item()])
    monkeypatch.setattr(monochrome, "_qobuz_isrc_lookup", lambda isrc: ([], True))

    results = monochrome._deezer_search_leg("david guetta titanium", 5)

    assert len(results) == 1
    assert results[0]["quality"] == "LOSSLESS"
    assert any("qobuz_unverified" in b for b in results[0]["score_breakdown"])


def test_deezer_search_leg_skips_junk_isrcs(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="QT&JC2622262", title="Junk ISRC"),
                            _make_deezer_item(isrc=""),
                        ])
    monkeypatch.setattr(monochrome, "_qobuz_isrc_lookup",
                        lambda isrc: ([{"id": 1, "isrc": isrc, "hires": False}], False))

    assert monochrome._deezer_search_leg("whatever", 5) == []


def test_deezer_isrc_rescue_prefers_studio_version(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="GBCEE0300050", title="Titanium (Live At Wembley)",
                                              title_version="(Live At Wembley)"),
                            _make_deezer_item(isrc="GB28K1100036", title="Titanium (feat. Sia)"),
                        ])

    rescued = monochrome._deezer_isrc_rescue("David Guetta", "Titanium", "QT&JC2622262")

    assert rescued == "GB28K1100036"


def test_deezer_isrc_rescue_returns_empty_on_weak_match(monkeypatch):
    import monochrome
    monkeypatch.setattr(monochrome, "_deezer_search_tracks",
                        lambda q, limit: [
                            _make_deezer_item(isrc="FRXXX9900001", title="Entirely Different Song",
                                              artist="Someone Else"),
                        ])

    assert monochrome._deezer_isrc_rescue("David Guetta", "Titanium", "") == ""


def test_deezer_isrc_rescue_no_hints_no_network(monkeypatch):
    import monochrome

    def boom(*a, **k):
        raise AssertionError("should not search Deezer without hints")
    monkeypatch.setattr(monochrome, "_deezer_search_tracks", boom)

    assert monochrome._deezer_isrc_rescue("", "", "QT&JC2622262") == ""


def test_search_monochrome_skips_hifi_when_deezer_delivers(monkeypatch):
    import monochrome
    fake_results = [
        {"relevance_score": 100 - i, "source_url": f"monochrome://x{i}?isrc=GBABC123456{i}&src=deezer"}
        for i in range(5)
    ]
    monkeypatch.setattr(monochrome, "_deezer_search_leg", lambda q, limit: fake_results)

    def boom(*a, **k):
        raise AssertionError("hifi leg should not run when Deezer fills the limit")
    monkeypatch.setattr(monochrome, "_hifi_search_leg", boom)

    results = monochrome.search_monochrome("query", 5)
    assert len(results) == 5


def test_search_monochrome_tops_up_from_hifi_and_dedupes(monkeypatch):
    import monochrome
    deezer = [{"relevance_score": 90, "source_url": "monochrome://1?isrc=GB28K1100036&src=deezer"}]
    hifi = [
        {"relevance_score": 80, "source_url": "monochrome://2?isrc=GB28K1100036&src=tidal"},  # dupe
        {"relevance_score": 70, "source_url": "monochrome://3?isrc=USUM71703861&src=tidal"},
    ]
    monkeypatch.setattr(monochrome, "_deezer_search_leg", lambda q, limit: deezer)
    monkeypatch.setattr(monochrome, "_hifi_search_leg", lambda q, limit: hifi)

    results = monochrome.search_monochrome("query", 5)

    assert len(results) == 2
    assert results[0]["source_url"].startswith("monochrome://1")
    assert results[1]["source_url"].startswith("monochrome://3")


class _FakeStreamResponse:
    """Pretend httpx.stream context manager serving a small FLAC-ish blob."""
    def __init__(self, payload=b"FLACDATA" * 16):
        self._payload = payload
        self.headers = {"content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size=65536):
        yield self._payload


def test_download_rescues_malformed_isrc_via_deezer(monkeypatch, tmp_path):
    import monochrome
    rescue_calls = []
    monkeypatch.setattr(
        monochrome, "_deezer_isrc_rescue",
        lambda artist, title, bad_isrc: rescue_calls.append((artist, title, bad_isrc)) or "GB28K1100036",
    )
    stream_calls = []
    monkeypatch.setattr(
        "monochrome_browser.resolve_unified_stream_url",
        lambda isrc, *a, **k: stream_calls.append(isrc) or "https://cdn.test/track.flac",
    )
    monkeypatch.setattr(monochrome.httpx, "stream", lambda *a, **k: _FakeStreamResponse())
    output = tmp_path / "track.flac"
    monochrome.download_monochrome_track(
        "monochrome://12345?isrc=QT%26JC2622262&quality=LOSSLESS&src=tidal",
        output, artist_hint="David Guetta", title_hint="Titanium",
    )
    assert rescue_calls == [("David Guetta", "Titanium", "QT&JC2622262")]
    assert stream_calls == ["GB28K1100036"]
    assert output.exists() and output.stat().st_size > 0

def test_download_decrypts_browser_cenc_resource_without_persisting_key(monkeypatch, tmp_path):
    import monochrome

    monkeypatch.setattr(
        monochrome, "_resolve_monochrome_stream_url", lambda *a, **k: "https://cdn.test/encrypted.mp4"
    )
    monkeypatch.setattr("monochrome_browser.pop_decryption_key", lambda url: "a1" * 16)
    monkeypatch.setattr(
        monochrome.httpx, "stream", lambda *a, **k: _FakeStreamResponse(b"encrypted-media")
    )
    calls = []

    def fake_ffmpeg(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"fLaCclean-audio")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(monochrome.subprocess, "run", fake_ffmpeg)
    output = tmp_path / "track.flac"

    monochrome.download_monochrome_track("monochrome://123?isrc=TEST", output)

    assert output.read_bytes().startswith(b"fLaC")
    assert not (tmp_path / "track.flac.encrypted.mp4").exists()
    assert calls and calls[0][calls[0].index("-decryption_key") + 1] == "a1" * 16


def test_download_falls_back_to_tidal_stream_for_tidal_results(monkeypatch, tmp_path):
    import monochrome
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    tidal_calls = []
    monkeypatch.setattr(
        monochrome, "_tidal_stream_url",
        lambda tidal_id, quality: tidal_calls.append((tidal_id, quality)) or "https://tidal.test/track.flac",
    )
    monkeypatch.setattr(monochrome.httpx, "stream", lambda *a, **k: _FakeStreamResponse())
    output = tmp_path / "track.flac"
    monochrome.download_monochrome_track(
        "monochrome://18420572?isrc=GBZZZ9900001&quality=LOSSLESS&src=tidal", output,
    )
    assert tidal_calls == [("18420572", "LOSSLESS")]
    assert output.exists()

def test_download_never_uses_tidal_stream_for_non_tidal_results(monkeypatch, tmp_path):
    import monochrome
    monkeypatch.setattr("monochrome_browser.resolve_unified_stream_url", lambda *a, **k: None)
    monkeypatch.setattr("qbdlx.resolve_qobuz_stream_url", lambda *a: None)
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "")
    monkeypatch.setattr(
        monochrome, "_tidal_stream_url",
        lambda *a: (_ for _ in ()).throw(AssertionError("Tidal must not run for non-Tidal results")),
    )
    output = tmp_path / "track.flac"
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome.download_monochrome_track(
            "monochrome://8767428?isrc=GBZZZ9900001&quality=LOSSLESS&src=qbdlx", output,
        )
    assert not output.exists()

def test_monochrome_playlist_urls_detect_as_monochrome():
    from watched_playlists import detect_playlist_platform

    platform, playlist_id = detect_playlist_platform(
        "https://monochrome.tf/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
    )

    assert platform == "monochrome"
    assert playlist_id == "0dfc3b10-fbdb-4419-bf54-11b90051fa6c"


def test_monochrome_playlist_fetch_uses_tidal_embed_not_hifi_api(monkeypatch):
    # A Monochrome playlist share is a Tidal playlist UUID wearing Monochrome's
    # hostname; it should resolve via the same embed-page scrape as a plain
    # tidal.com link, not hifi-api, which has been dead since Tidal revoked its
    # OAuth client.
    import watched_playlists

    def fake_fetch_tidal_playlist(uuid):
        assert uuid == "0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
        return {"tracks": ["Kylie Cantrall - Carrie Bradshaw"], "playlist_name": "Chill Pop", "count": 1}

    monkeypatch.setattr(watched_playlists, "fetch_tidal_playlist", fake_fetch_tidal_playlist)

    tracks, name, warning, original_ids = watched_playlists.fetch_playlist_tracks(
        "https://monochrome.tf/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c", "monochrome",
    )

    assert tracks == [("Kylie Cantrall", "Carrie Bradshaw")]
    assert name == "Chill Pop"
